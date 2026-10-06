# -*- coding: utf-8 -*-
#
# Copyright (C) 2013-2026 Vinay Sajip.
# Licensed to the Python Software Foundation under a contributor agreement.
# See LICENSE.txt and CONTRIBUTORS.txt.
#
from io import BytesIO
import logging
import os
import re
import shlex
import struct
import sys
import time
from zipfile import ZipInfo

from . import DistlibException
from .compat import sysconfig, detect_encoding, ZipFile, string_types
from .resources import finder
from .util import (FileOperator, get_export_entry, convert_path, get_executable, get_platform, in_venv,
                   is_in_directory)

logger = logging.getLogger(__name__)

_DEFAULT_MANIFEST = '''
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
 <assemblyIdentity version="1.0.0.0"
 processorArchitecture="X86"
 name="%s"
 type="win32"/>

 <!-- Identify the application security requirements. -->
 <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
 <security>
 <requestedPrivileges>
 <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
 </requestedPrivileges>
 </security>
 </trustInfo>
</assembly>'''.strip()

# check if Python is called on the first line with this expression
FIRST_LINE_RE = re.compile(b'^#!.*pythonw?[0-9.]*([ \t].*)?$')
SCRIPT_TEMPLATE = r'''# -*- coding: utf-8 -*-
import re
import sys
if __name__ == '__main__':
    from %(module)s import %(import_name)s
    sys.argv[0] = re.sub(r'(-script\.pyw|\.exe)?$', '', sys.argv[0])
    sys.exit(%(func)s())
'''

# Whitespace separating tokens in a shebang line (spaces or tabs).
_SHEBANG_WHITESPACE_RE = re.compile(br'[ \t]+')
# Inside double quotes, only these characters need backslash-escaping in a
# POSIX shell. Spaces, single quotes and other punctuation stay literal.
_SHELL_ESCAPE_CHARS_RE = re.compile(r'([$`"\\])')
# Characters which are safe to put, unquoted, in a direct shebang line. The
# kernel treats the whole tail of a shebang as an opaque string (on Linux it
# is handed to the interpreter as a single argument), so anything containing
# whitespace or quotes cannot be expressed faithfully in a direct shebang.
_DIRECT_SHEBANG_SAFE_RE = re.compile(r'[A-Za-z0-9_@%+=:,./-]*\Z')
# Tokens embedded in generated files must be single-line and NUL-free.
_SHEBANG_FORBIDDEN_CHARS_RE = re.compile(r'[\x00\r\n]')


def _normalize_interpreter_args(interpreter_args):
    """
    Normalize interpreter arguments into a list of strings.

    A list or tuple of strings is the documented, data-safe form: each entry
    is one argument, passed verbatim to the interpreter. A plain string is
    also accepted for backwards compatibility and split using POSIX shell
    rules, so that single- and double-quoted whitespace is honored.
    """
    if interpreter_args is None:
        result = []
    elif isinstance(interpreter_args, (list, tuple)):
        result = [str(arg) for arg in interpreter_args]
    elif isinstance(interpreter_args, string_types):
        result = _split_shebang_tail(interpreter_args)
    else:
        raise TypeError('interpreter_args must be a list, tuple or string, '
                        'not %s' % type(interpreter_args).__name__)
    return result


def _split_shebang_tail(data):
    """
    Split the portion of a shebang line following the interpreter into
    whitespace-separated tokens, honoring single and double quotes. Returns a
    list of strings. If the quoting is malformed, fall back to splitting on
    bare whitespace, treating quotes as ordinary characters.
    """
    if isinstance(data, bytes):
        data = data.decode('utf-8')
    try:
        result = shlex.split(data, posix=True)
    except ValueError:
        result = [part.decode('utf-8')
                  for part in _SHEBANG_WHITESPACE_RE.split(data.encode('utf-8')) if part]
    return result


def _quote_shell_token(token):
    """
    Quote one token so that a POSIX shell recovers it exactly as data.

    Double quotes are used rather than single quotes (with the characters that
    remain special inside double quotes backslash-escaped), so that the
    generated trampoline cannot collide with the surrounding triple
    single-quoted Python string.
    """
    return '"%s"' % _SHELL_ESCAPE_CHARS_RE.sub(r'\\\1', token)


def _quote_executable_for_shell(executable):
    """
    Turn an executable specification into a list of POSIX shell words,
    quoting every word as data. This honors the ``/usr/bin/env exe`` form and
    any pre-existing quoting, so that paths containing spaces, quotes or shell
    metacharacters are passed to the interpreter verbatim.
    """
    if isinstance(executable, bytes):
        executable = executable.decode('utf-8')
    words = None
    # The ``env`` form genuinely contains multiple words; anything else is a
    # single pathname, even when it contains spaces or quotes.
    if executable.startswith('/usr/bin/env ') or executable.startswith('/bin/env '):
        try:
            words = shlex.split(executable, posix=True)
        except ValueError:  # malformed quoting
            words = None
    if words is None:
        path = executable
        # Strip a single layer of enclosing quotes if the caller pre-quoted
        # the path (as recommended for older direct shebangs).
        if len(path) >= 2 and path[0] == path[-1] and path[0] in ('"', "'"):
            path = path[1:-1]
        words = [path]
    return [_quote_shell_token(word).encode('utf-8') for word in words]

# Pre-fetch the contents of all executable wrapper stubs.
# This is to address https://github.com/pypa/pip/issues/12666.
# When updating pip, we rename the old pip in place before installing the
# new version. If we try to fetch a wrapper *after* that rename, the finder
# machinery will be confused as the package is no longer available at the
# location where it was imported from. So we load everything into memory in
# advance.

if os.name == 'nt' or (os.name == 'java' and os._name == 'nt'):
    # Issue 31: don't hardcode an absolute package name, but
    # determine it relative to the current package
    DISTLIB_PACKAGE = __name__.rsplit('.', 1)[0]

    WRAPPERS = {
        r.name: r.bytes
        for r in finder(DISTLIB_PACKAGE).iterator("")
        if r.name.endswith(".exe")
    }


def enquote_executable(executable):
    if ' ' in executable:
        # make sure we quote only the executable in case of env
        # for example /usr/bin/env "/dir with spaces/bin/jython"
        # instead of "/usr/bin/env /dir with spaces/bin/jython"
        # otherwise whole
        if executable.startswith('/usr/bin/env '):
            env, _executable = executable.split(' ', 1)
            if ' ' in _executable and not _executable.startswith('"'):
                executable = '%s "%s"' % (env, _executable)
        else:
            if not executable.startswith('"'):
                executable = '"%s"' % executable
    return executable


# Keep the old name around (for now), as there is at least one project using it!
_enquote_executable = enquote_executable


class ScriptMaker(object):
    """
    A class to copy or create scripts from source scripts or callable
    specifications.
    """
    script_template = SCRIPT_TEMPLATE

    executable = None  # for shebangs

    def __init__(self, source_dir, target_dir, add_launchers=True, dry_run=False, fileop=None):
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.add_launchers = add_launchers
        self.force = False
        self.clobber = False
        # It only makes sense to set mode bits on POSIX.
        self.set_mode = (os.name == 'posix') or (os.name == 'java' and os._name == 'posix')
        self.variants = set(('', 'X.Y'))
        self._fileop = fileop or FileOperator(dry_run)

        self._is_nt = os.name == 'nt' or (os.name == 'java' and os._name == 'nt')
        self.version_info = sys.version_info

    def _get_alternate_executable(self, executable, options):
        if options.get('gui', False) and self._is_nt:  # pragma: no cover
            dn, fn = os.path.split(executable)
            fn = fn.replace('python', 'pythonw')
            executable = os.path.join(dn, fn)
        return executable

    if sys.platform.startswith('java'):  # pragma: no cover

        def _is_shell(self, executable):
            """
            Determine if the specified executable is a script
            (contains a #! line)
            """
            try:
                with open(executable) as fp:
                    return fp.read(2) == '#!'
            except (OSError, IOError):
                logger.warning('Failed to open %s', executable)
                return False

        def _fix_jython_executable(self, executable):
            if self._is_shell(executable):
                # Workaround for Jython is not needed on Linux systems.
                import java

                if java.lang.System.getProperty('os.name') == 'Linux':
                    return executable
            elif executable.lower().endswith('jython.exe'):
                # Use wrapper exe for Jython on Windows
                return executable
            return '/usr/bin/env %s' % executable

    def _build_shebang(self, executable, args):
        """
        Build a shebang line.

        ``executable`` is the interpreter executable (bytes) and ``args`` is a
        list of interpreter argument tokens (strings).

        When the interpreter and its arguments can be expressed faithfully by a
        direct shebang (off POSIX, or a short, space-free executable with at
        most one simple argument), the direct form is used. Otherwise a POSIX
        trampoline is generated: /bin/sh execs the interpreter with every path
        and argument quoted as data, preserving token boundaries even when
        paths contain spaces, quotes or shell metacharacters.

        The trampoline is also a valid Python file: the shell-only line is a
        triple-quoted string expression, so Python never sees it (and emits no
        escape-sequence warnings).

        See also: http://www.in-ulm.de/~mascheck/various/shebang/#length
                  https://hg.mozilla.org/mozilla-central/file/tip/mach
        """
        for token in [executable.decode('utf-8')] + list(args):
            if _SHEBANG_FORBIDDEN_CHARS_RE.search(token):
                raise ValueError('Shebang components must not contain '
                                 'newlines or NUL bytes: %r' % token)
        if os.name != 'posix':
            simple_shebang = True
        elif getattr(sys, "cross_compiling", False):
            # In a cross-compiling environment, the shebang will likely be a
            # script; this *must* be invoked with the "safe" version of the
            # shebang, or else using os.exec() to run the entry script will
            # fail, raising "OSError 8 [Errno 8] Exec format error".
            simple_shebang = False
        else:
            post_interp = self._render_direct_args(args)
            # Add 3 for '#!' prefix and newline suffix.
            shebang_length = len(executable) + len(post_interp) + 3
            if sys.platform == 'darwin':
                max_shebang_length = 512
            else:
                max_shebang_length = 127
            # On Linux the kernel hands the whole post-interpreter tail to the
            # interpreter as a single argument; at most one token can be
            # expressed faithfully there (and only if it contains no
            # whitespace). Empty tokens cannot be expressed in a shebang at
            # all. Anything else requires the shell trampoline.
            simple_shebang = (b' ' not in executable and b'\t' not in executable and
                              shebang_length <= max_shebang_length and len(args) <= 1 and
                              all(args) and
                              all(_DIRECT_SHEBANG_SAFE_RE.match(arg) for arg in args))

        if simple_shebang:
            result = b'#!' + executable + self._render_direct_args(args) + b'\n'
        else:
            words = _quote_executable_for_shell(executable)
            words.extend(_quote_shell_token(arg).encode('utf-8') for arg in args)
            result = b'#!/bin/sh\n'
            result += b"''''exec " + b' '.join(words) + b' "$0" "$@" # ' + b"'''\n"
        return result

    def _render_direct_args(self, args):
        """
        Render interpreter argument tokens for a direct shebang. Tokens are
        separated by a single space, exactly as a shebang reader expects.
        """
        if not args:
            return b''
        return b' ' + b' '.join(arg.encode('utf-8') for arg in args)

    def _get_shebang(self, encoding, args=None, options=None):
        enquote = True
        if self.executable:
            executable = self.executable
            enquote = False  # assume this will be taken care of
        elif not sysconfig.is_python_build():
            executable = get_executable()
        elif in_venv():  # pragma: no cover
            executable = os.path.join(sysconfig.get_path('scripts'), 'python%s' % sysconfig.get_config_var('EXE'))
        else:  # pragma: no cover
            if os.name == 'nt':
                # for Python builds from source on Windows, no Python executables with
                # a version suffix are created, so we use python.exe
                executable = os.path.join(sysconfig.get_config_var('BINDIR'),
                                          'python%s' % (sysconfig.get_config_var('EXE')))
            else:
                executable = os.path.join(
                    sysconfig.get_config_var('BINDIR'),
                    'python%s%s' % (sysconfig.get_config_var('VERSION'), sysconfig.get_config_var('EXE')))
        if options:
            executable = self._get_alternate_executable(executable, options)

        if sys.platform.startswith('java'):  # pragma: no cover
            executable = self._fix_jython_executable(executable)

        # Normalise case for Windows - COMMENTED OUT
        # executable = os.path.normcase(executable)
        # N.B. The normalising operation above has been commented out: See
        # issue #124. Although paths in Windows are generally case-insensitive,
        # they aren't always. For example, a path containing a ẞ (which is a
        # LATIN CAPITAL LETTER SHARP S - U+1E9E) is normcased to ß (which is a
        # LATIN SMALL LETTER SHARP S' - U+00DF). The two are not considered by
        # Windows as equivalent in path names.

        # If the user didn't specify an executable, it may be necessary to
        # cater for executable paths with spaces (not uncommon on Windows).
        # On POSIX the quoting is handled by _build_shebang itself (the direct
        # form is used when there are no spaces, otherwise every word is
        # quoted as data in the shell trampoline).
        if enquote and os.name != 'posix':
            executable = enquote_executable(executable)
        # Issue #51: don't use fsencode, since we later try to
        # check that the shebang is decodable using utf-8.
        executable = executable.encode('utf-8')
        args = _normalize_interpreter_args(args)
        # in case of IronPython, play safe and enable frames support
        if sys.platform == 'cli' and '-X:Frames' not in args and '-X:FullFrames' not in args:  # pragma: no cover
            args.append('-X:Frames')
        shebang = self._build_shebang(executable, args)
        # Python parser starts to read a script using UTF-8 until
        # it gets a #coding:xxx cookie. The shebang has to be the
        # first line of a file, the #coding:xxx cookie cannot be
        # written before. So the shebang has to be decodable from
        # UTF-8.
        try:
            shebang.decode('utf-8')
        except UnicodeDecodeError:  # pragma: no cover
            raise ValueError('The shebang (%r) is not decodable from utf-8' % shebang)
        # If the script is encoded to a custom encoding (use a
        # #coding:xxx cookie), the shebang has to be decodable from
        # the script encoding too.
        if encoding != 'utf-8':
            try:
                shebang.decode(encoding)
            except UnicodeDecodeError:  # pragma: no cover
                raise ValueError('The shebang (%r) is not decodable '
                                 'from the script encoding (%r)' % (shebang, encoding))
        return shebang

    def _get_script_text(self, entry):
        return self.script_template % dict(
            module=entry.prefix, import_name=entry.suffix.split('.')[0], func=entry.suffix)

    manifest = _DEFAULT_MANIFEST

    def get_manifest(self, exename):
        base = os.path.basename(exename)
        return self.manifest % base

    def _write_script(self, names, shebang, script_bytes, filenames, ext):
        use_launcher = self.add_launchers and self._is_nt
        if not use_launcher:
            script_bytes = shebang + script_bytes
        else:  # pragma: no cover
            if ext == 'py':
                launcher = self._get_launcher('t')
            else:
                launcher = self._get_launcher('w')
            stream = BytesIO()
            with ZipFile(stream, 'w') as zf:
                source_date_epoch = os.environ.get('SOURCE_DATE_EPOCH')
                if source_date_epoch:
                    date_time = time.gmtime(int(source_date_epoch))[:6]
                    zinfo = ZipInfo(filename='__main__.py', date_time=date_time)
                    zf.writestr(zinfo, script_bytes)
                else:
                    zf.writestr('__main__.py', script_bytes)
            zip_data = stream.getvalue()
            script_bytes = launcher + shebang + zip_data
        for name in names:
            outname = os.path.abspath(os.path.join(self.target_dir, name))
            if not is_in_directory(outname, self.target_dir):
                raise DistlibException('Attempt to escape script directory')
            if use_launcher:  # pragma: no cover
                n, e = os.path.splitext(outname)
                if e.startswith('.py'):
                    outname = n
                outname = '%s.exe' % outname
                try:
                    self._fileop.write_binary_file(outname, script_bytes)
                except Exception:
                    # Failed writing an executable - it might be in use.
                    logger.warning('Failed to write executable - trying to '
                                   'use .deleteme logic')
                    dfname = '%s.deleteme' % outname
                    if os.path.exists(dfname):
                        os.remove(dfname)  # Not allowed to fail here
                    os.rename(outname, dfname)  # nor here
                    self._fileop.write_binary_file(outname, script_bytes)
                    logger.debug('Able to replace executable using '
                                 '.deleteme logic')
                    try:
                        os.remove(dfname)
                    except Exception:
                        pass  # still in use - ignore error
            else:
                if self._is_nt and not outname.endswith('.' + ext):  # pragma: no cover
                    outname = '%s.%s' % (outname, ext)
                if os.path.exists(outname) and not self.clobber:
                    logger.warning('Skipping existing file %s', outname)
                    continue
                self._fileop.write_binary_file(outname, script_bytes)
                if self.set_mode:
                    self._fileop.set_executable_mode([outname])
            filenames.append(outname)

    variant_separator = '-'

    def get_script_filenames(self, name):
        result = set()
        if '' in self.variants:
            result.add(name)
        if 'X' in self.variants:
            result.add('%s%s' % (name, self.version_info[0]))
        if 'X.Y' in self.variants:
            result.add('%s%s%s.%s' % (name, self.variant_separator, self.version_info[0], self.version_info[1]))
        return result

    def _make_script(self, entry, filenames, options=None):
        args = []
        if options:
            args = options.get('interpreter_args', [])
        shebang = self._get_shebang('utf-8', args, options=options)
        script = self._get_script_text(entry).encode('utf-8')
        scriptnames = self.get_script_filenames(entry.name)
        if options and options.get('gui', False):
            ext = 'pyw'
        else:
            ext = 'py'
        self._write_script(scriptnames, shebang, script, filenames, ext)

    def _copy_script(self, script, filenames):
        adjust = False
        script = os.path.join(self.source_dir, convert_path(script))
        outname = os.path.join(self.target_dir, os.path.basename(script))
        if not self.force and not self._fileop.newer(script, outname):
            logger.debug('not copying %s (up-to-date)', script)
            return

        # Always open the file, but ignore failures in dry-run mode --
        # that way, we'll get accurate feedback if we can read the
        # script.
        try:
            f = open(script, 'rb')
        except IOError:  # pragma: no cover
            if not self.dry_run:
                raise
            f = None
        else:
            first_line = f.readline()
            if not first_line:  # pragma: no cover
                logger.warning('%s is an empty file (skipping)', script)
                return

            match = FIRST_LINE_RE.match(first_line.replace(b'\r\n', b'\n'))
            if match:
                adjust = True
                # Tokenize the arguments so that copied scripts go through the
                # same direct/trampoline handling as generated entry points.
                copied_args = _split_shebang_tail(match.group(1) or b'')

        if not adjust:
            if f:
                f.close()
            self._fileop.copy_file(script, outname)
            if self.set_mode:
                self._fileop.set_executable_mode([outname])
            filenames.append(outname)
        else:
            logger.info('copying and adjusting %s -> %s', script, self.target_dir)
            if not self._fileop.dry_run:
                encoding, lines = detect_encoding(f.readline)
                f.seek(0)
                shebang = self._get_shebang(encoding, copied_args)
                if b'pythonw' in first_line:  # pragma: no cover
                    ext = 'pyw'
                else:
                    ext = 'py'
                n = os.path.basename(outname)
                self._write_script([n], shebang, f.read(), filenames, ext)
            if f:
                f.close()

    @property
    def dry_run(self):
        return self._fileop.dry_run

    @dry_run.setter
    def dry_run(self, value):
        self._fileop.dry_run = value

    if os.name == 'nt' or (os.name == 'java' and os._name == 'nt'):  # pragma: no cover
        # Executable launcher support.
        # Launchers are from https://bitbucket.org/vinay.sajip/simple_launcher/

        def _get_launcher(self, kind):
            if struct.calcsize('P') == 8:  # 64-bit
                bits = '64'
            else:
                bits = '32'
            platform_suffix = '-arm' if get_platform() == 'win-arm64' else ''
            name = '%s%s%s.exe' % (kind, bits, platform_suffix)
            if name not in WRAPPERS:
                msg = ('Unable to find resource %s in package %s' %
                       (name, DISTLIB_PACKAGE))
                raise ValueError(msg)
            return WRAPPERS[name]

    # Public API follows

    def make(self, specification, options=None):
        """
        Make a script.

        :param specification: The specification, which is either a valid export
                              entry specification (to make a script from a
                              callable) or a filename (to make a script by
                              copying from a source location).
        :param options: A dictionary of options controlling script generation.
        :return: A list of all absolute pathnames written to.
        """
        filenames = []
        entry = get_export_entry(specification)
        if entry is None:
            self._copy_script(specification, filenames)
        else:
            self._make_script(entry, filenames, options=options)
        return filenames

    def make_multiple(self, specifications, options=None):
        """
        Take a list of specifications and make scripts from them,
        :param specifications: A list of specifications.
        :return: A list of all absolute pathnames written to,
        """
        filenames = []
        for specification in specifications:
            filenames.extend(self.make(specification, options))
        return filenames
