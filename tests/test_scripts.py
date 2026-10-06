# -*- coding: utf-8 -*-
#
# Copyright (C) 2012-2026 Vinay Sajip.
# Licensed to the Python Software Foundation under a contributor agreement.
# See LICENSE.txt and CONTRIBUTORS.txt.
#
from __future__ import unicode_literals

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
try:
    import venv
except ImportError:
    venv = None

from compat import unittest
from support import DistlibTestCase

from distlib import DistlibException
from distlib.compat import fsencode, sysconfig
from distlib.scripts import ScriptMaker, enquote_executable
from distlib.util import get_executable

HERE = os.path.abspath(os.path.dirname(__file__))

COPIED_SCRIPT = '''#!python
# This is a copied script
'''

MADE_SCRIPT = 'made = dummy.module:main'


class ScriptTestCase(DistlibTestCase):

    def setUp(self):
        source_dir = os.path.join(HERE, 'scripts')
        target_dir = tempfile.mkdtemp(prefix='distlib-test-')
        self.maker = ScriptMaker(source_dir, target_dir, add_launchers=False)

    def tearDown(self):
        shutil.rmtree(self.maker.target_dir)

    @unittest.skipIf(sysconfig.is_python_build(), 'Test not appropriate for '
                     'Python source builds')
    def test_shebangs(self):
        executable = fsencode(get_executable())
        for fn in ('foo.py', 'script1.py', 'script2.py', 'script3.py', 'shell.sh'):
            files = self.maker.make(fn)
            self.assertEqual(len(files), 1)
            d, f = os.path.split(files[0])
            self.assertEqual(f, fn)
            self.assertEqual(d, self.maker.target_dir)
            if fn.endswith('.py') and fn != 'foo.py':  # no shebang in foo.py
                with open(files[0], 'rb') as f:
                    content = f.read()
                # The interpreter appears verbatim either on the direct
                # shebang line or (when the path is too long) inside the
                # POSIX trampoline.
                self.assertIn(executable, content)

    def test_shebangs_custom_executable(self):
        srcdir = tempfile.mkdtemp(prefix='distlib-test-')
        self.addCleanup(shutil.rmtree, srcdir)
        dstdir = tempfile.mkdtemp(prefix='distlib-test-')
        self.addCleanup(shutil.rmtree, dstdir)
        maker = ScriptMaker(srcdir, dstdir, add_launchers=False)
        maker.executable = 'this_should_appear_in_the_shebang_line(中文)'
        # let's create the script to be copied. It has a vanilla shebang line,
        # with some Unicode in it.
        fn = os.path.join(srcdir, 'copied')
        with open(fn, 'w') as f:
            f.write(COPIED_SCRIPT)
        # Let's ask the maker to copy the script, and see what the shebang is
        # in the copy.
        filenames = maker.make('copied')
        with open(filenames[0], 'rb') as f:
            actual = f.readline().decode('utf-8')
        self.assertIn(maker.executable, actual)
        # Now let's make a script from a callable
        filenames = maker.make(MADE_SCRIPT)
        with open(filenames[0], 'rb') as f:
            actual = f.readline().decode('utf-8')
        self.assertIn(maker.executable, actual)

    @unittest.skipIf(os.name != 'posix', 'Test only appropriate for '
                     'POSIX systems')
    def test_custom_shebang(self):
        # Construct an executable with a space in it
        self.maker.executable = 'an executable with spaces'
        filenames = self.maker.make('script1.py')
        with open(filenames[0], 'rb') as f:
            first_line = f.readline()
            second_line = f.readline()
        self.assertEqual(first_line, b'#!/bin/sh\n')
        # Every word of the executable is quoted as data, so spaces in the
        # path don't change argument boundaries.
        self.assertEqual(second_line,
                         b"''''exec \"an executable with spaces\" \"$0\" \"$@\" # '''\n")
        # Python 3.3 cannot create a venv in an existing directory
        if venv and sys.version_info[:2] >= (3, 4):
            if sys.platform == 'darwin':
                # Supposedly 512, but various symlinks mean that temp folder
                # names get larger than you'd expect ... might vary on different
                # OS versions, too
                dlen = 220
            else:
                dlen = 127
            dstdir = tempfile.mkdtemp(suffix='cataaaaaa' + 'a' * dlen)
            self.addCleanup(shutil.rmtree, dstdir)
            bindir = os.path.join(dstdir, 'bin')
            maker = ScriptMaker(self.maker.source_dir, bindir, add_launchers=False)
            # See issue #204. Changed to use symlinks on POSIX
            venv.create(dstdir, symlinks=(os.name != 'nt'))
            maker.executable = os.path.join(bindir, 'python')
            filenames = maker.make('script8.py')
            p = subprocess.Popen(filenames[0], shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = p.communicate()
            self.assertEqual(p.returncode, 0)
            self.assertEqual(stderr, b'')
            expected = os.path.realpath(maker.executable)  # symlinks on OS X
            actual = os.path.realpath(stdout.strip())
            self.assertEqual(actual, expected.encode('utf-8'))

    @unittest.skipIf(os.name != 'posix', 'Test only appropriate for '
                     'POSIX systems')
    def test_cross_env_shebang(self):
        # Construct an executable with a path that wouldn't ordinarily be a problem
        self.maker.executable = '/path/to/python3'

        # Set the cross-compiling marker attribute, then clean up.
        try:
            sys.cross_compiling = True
            filenames = self.maker.make('script1.py')
        finally:
            delattr(sys, "cross_compiling")

        with open(filenames[0], 'rb') as f:
            first_line = f.readline()
            second_line = f.readline()
        self.assertEqual(first_line, b'#!/bin/sh\n')
        self.assertEqual(second_line,
                         b"''''exec \"/path/to/python3\" \"$0\" \"$@\" # '''\n")

    def test_multiple(self):
        specs = ('foo.py', 'script1.py', 'script2.py', 'script3.py', 'shell.sh', 'uwsgi_part')
        files = self.maker.make_multiple(specs)
        self.assertEqual(len(specs), len(files))
        expected = set(specs)
        self.assertEqual(expected, set([os.path.basename(f) for f in files]))
        ofiles = os.listdir(self.maker.target_dir)
        self.assertEqual(expected, set(ofiles))

    def test_generation(self):
        self.maker.clobber = True

        def do_test(maker, version_info):
            for name in ('main', 'other_main'):
                for options in (None, {}, {'gui': False}, {'gui': True}):
                    gui = options and options.get('gui', False)
                    spec = 'foo = foo:' + name
                    files = maker.make(spec, options)
                    self.assertEqual(len(files), 2)
                    actual = set()
                    for f in files:
                        d, f = os.path.split(f)
                        actual.add(f)
                    if os.name == 'nt':  # pragma: no cover
                        if gui:
                            ext = 'pyw'
                        else:
                            ext = 'py'
                        expected = set(['foo.%s' % ext, 'foo-%s.%s.%s' % (version_info[0], version_info[1], ext)])
                    else:
                        expected = set(['foo', 'foo-%s.%s' % (version_info[0], version_info[1])])
                    self.assertEqual(actual, expected)
                    self.assertEqual(d, maker.target_dir)
                    for fn in files:
                        with open(fn, 'r') as f:
                            text = f.read()
                        # self.assertIn("_resolve('foo', '%s')" % name, text)
                        if options and options['gui'] and os.name == 'nt':  # pragma: no cover
                            first_line, rest = text.split('\n', 1)
                            self.assertIn('pythonw', first_line)

        do_test(self.maker, sys.version_info)
        # See issue 134. Test for specifying the target Python version
        self.maker.version_info = version_info = (4, 4)  # just any other Python version
        do_test(self.maker, version_info)

    def test_clobber(self):
        files = self.maker.make('foo = foo:main')
        saved_files = files
        self.assertGreaterEqual(len(files), 2)  # foo, foo-X.Y
        files = self.maker.make('foo = foo:main')
        self.assertFalse(files)
        self.maker.clobber = True
        files = self.maker.make('foo = foo:main')
        self.assertEqual(files, saved_files)

    @unittest.skipIf(os.name != 'nt', 'Test is Windows-specific')
    def test_launchers(self):  # pragma: no cover
        tlauncher = self.maker._get_launcher('t')
        self.maker.add_launchers = True
        specs = ('foo.py', 'script1.py', 'script2.py', 'script3.py', 'shell.sh')
        files = self.maker.make_multiple(specs)
        self.assertEqual(len(specs), len(files))
        filenames = set([os.path.basename(f) for f in files])
        self.assertEqual(filenames, set(('foo.py', 'script1.exe', 'script2.exe', 'script3.exe', 'shell.sh')))
        for fn in files:
            if not fn.endswith('.exe'):
                continue
            with open(fn, 'rb') as f:
                data = f.read()
            self.assertTrue(data.startswith(tlauncher))

    @unittest.skipIf(os.name != 'nt', 'Test is Windows-specific')
    def test_launcher_run(self):
        self.maker.add_launchers = True
        files = self.maker.make('script6.py')
        self.assertEqual(len(files), 1)
        p = subprocess.Popen([files[0], 'Test Argument'],
                             stdout=subprocess.PIPE,
                             stdin=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        stdout, stderr = p.communicate('input'.encode('ascii'))
        actual = stdout.decode('ascii').replace('\r\n', '\n')
        expected = textwrap.dedent("""
            script6.exe
            ['Test Argument']
            'input'
            non-optimized
            """).lstrip()
        self.assertEqual(actual, expected)

    @unittest.skipIf(os.name != 'nt', 'Test is Windows-specific')
    def test_launcher_run_with_interpreter_args(self):
        srcdir = tempfile.mkdtemp(prefix='distlib-test-')
        self.addCleanup(shutil.rmtree, srcdir)
        dstdir = tempfile.mkdtemp(prefix='distlib-test-')
        self.addCleanup(shutil.rmtree, dstdir)
        maker = ScriptMaker(srcdir, dstdir, add_launchers=True)

        # add '-O' option to shebang to run in optimized mode
        with open(os.path.join(HERE, 'scripts', 'script6.py'), 'r') as src:
            with open(os.path.join(srcdir, 'script6-optimized.py'), 'w') as dst:
                shebang = src.readline().rstrip()
                dst.write(shebang + " -O\n")
                dst.write(src.read())

        files = maker.make('script6-optimized.py')
        self.assertEqual(len(files), 1)
        p = subprocess.Popen([files[0], 'Test Argument'],
                             stdout=subprocess.PIPE,
                             stdin=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        stdout, stderr = p.communicate('input'.encode('ascii'))
        actual = stdout.decode('ascii').replace('\r\n', '\n')
        expected = textwrap.dedent("""
            script6-optimized.exe
            ['Test Argument']
            'input'
            """).lstrip()  # 'non-optimized' is not printed this time
        self.assertEqual(actual, expected)

    @unittest.skipIf(os.name != 'nt', 'Test is Windows-specific')
    def test_windows(self):  # pragma: no cover
        wlauncher = self.maker._get_launcher('w')
        tlauncher = self.maker._get_launcher('t')
        self.maker.add_launchers = True
        # executable = os.path.normcase(sys.executable).encode('utf-8')
        executable = sys.executable.encode('utf-8')
        if b'python3.' in executable:
            wexecutable = executable.replace(b'python3.', b'pythonw3.')
        else:
            wexecutable = executable.replace(b'python.', b'pythonw.')
        files = self.maker.make('script4.py')
        self.assertEqual(len(files), 1)
        filenames = set([os.path.basename(f) for f in files])
        self.assertEqual(filenames, set(['script4.exe']))
        for fn in files:
            with open(fn, 'rb') as f:
                data = f.read()
            self.assertTrue(data.startswith(wlauncher))
            self.assertIn(executable, data)
        # Now test making scripts gui and console
        files = self.maker.make('foo = foo:main', {'gui': True})
        self.assertEqual(len(files), 2)
        filenames = set([os.path.basename(f) for f in files])
        specific = '%s.%s' % sys.version_info[:2]
        self.assertEqual(filenames, set(('foo.exe', 'foo-%s.exe' % specific)))
        for fn in files:
            with open(fn, 'rb') as f:
                data = f.read()
            self.assertTrue(data.startswith(wlauncher))
            self.assertIn(wexecutable, data)

        files = self.maker.make('foo = foo:main')
        self.assertEqual(len(files), 2)
        filenames = set([os.path.basename(f) for f in files])
        self.assertEqual(filenames, set(('foo.exe', 'foo-%s.exe' % specific)))
        for fn in files:
            with open(fn, 'rb') as f:
                data = f.read()
            self.assertTrue(data.startswith(tlauncher))
            self.assertIn(executable, data)

    @unittest.skipIf(os.name != 'nt', 'Test is Windows-specific')
    def test_windows_run(self):
        self.maker.add_launchers = True
        files = self.maker.make('script7.pyw')
        self.assertEqual(len(files), 1)

        test_output = os.path.join(self.maker.target_dir, 'test_output.txt')
        p = subprocess.Popen([files[0], test_output, 'Test Argument'],
                             stdout=subprocess.PIPE,
                             stdin=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        stdout, stderr = p.communicate()
        self.assertFalse(stdout)
        self.assertFalse(stderr)
        with open(test_output, 'rb') as f:
            actual = f.read().decode('ascii')
        self.assertEqual(actual, 'Test Argument')

    def test_dry_run(self):
        self.maker.dry_run = True
        self.maker.variants = set([''])
        specs = ('foo.py', 'bar = foo:main')
        files = self.maker.make_multiple(specs)
        self.assertEqual(len(specs), len(files))
        if os.name == 'nt':  # pragma: no cover
            bar = 'bar.py'
        else:
            bar = 'bar'
        self.assertEqual(set(('foo.py', bar)), set([os.path.basename(f) for f in files]))
        ofiles = os.listdir(self.maker.target_dir)
        self.assertFalse(ofiles)

    def test_script_run(self):
        if sys.version_info[:2] < (3, 13):
            target = 'cgi:print_directory'
        else:
            target = 'logging:getHandlerNames'
        files = self.maker.make('test = %s' % target)
        self.assertEqual(len(files), 2)
        p = subprocess.Popen([sys.executable, files[0]], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = p.communicate()
        if sys.version_info[:2] < (3, 13):
            self.assertIn(b'<H3>Current Working Directory:</H3>', stdout)
            self.assertIn(os.getcwd().encode('utf-8'), stdout)
        else:
            self.assertIn(b'frozenset(', stderr)
            self.assertEqual(p.returncode, 1)

    @unittest.skipUnless(os.name == 'posix', 'Test only valid for POSIX')
    def test_mode(self):
        # save test runner's original umask and ensure default 022
        saved_umask = os.umask(0o022)
        try:
            self.maker.set_mode = False
            files = self.maker.make('foo = foo:main')
            self.assertEqual(len(files), 2)
            for f in files:
                self.assertIn(os.stat(f).st_mode & 0o7777, (0o644, 0o664))
            self.maker.set_mode = True
            files = self.maker.make('bar = bar:main')
            self.assertEqual(len(files), 2)
            for f in files:
                self.assertIn(os.stat(f).st_mode & 0o7777, (0o755, 0o775))
        finally:
            # restore the test runner's original umask
            os.umask(saved_umask)

    def test_interpreter_args(self):
        executable = fsencode(get_executable())
        # Each list entry is one argument: spaces and quotes are data, not
        # shell syntax, so they are carried verbatim. Multiple tokens force the
        # POSIX trampoline (the direct shebang can only carry one token).
        options = {'interpreter_args': ['-E', 'foo bar', 'baz frobozz']}
        self.maker.variants = set([''])
        files = self.maker.make('foo = bar:baz', options=options)
        self.assertEqual(len(files), 1)
        with open(files[0], 'rb') as f:
            lines = f.read().splitlines()
        if os.name == 'posix':
            self.assertEqual(lines[0], b'#!/bin/sh')
            self.assertTrue(lines[1].startswith(b"''''exec "))
            exec_line = lines[1]
            # the executable word and each argument are quoted separately
            for needle in (executable, b'"-E"', b'"foo bar"', b'"baz frobozz"'):
                self.assertIn(needle, exec_line)
            # and the script's own arguments are appended verbatim
            self.assertIn(b'"$0" "$@"', exec_line)
        else:
            shebang_line = lines[0]
            if not sysconfig.is_python_build():
                self.assertIn(executable, shebang_line)
            self.assertIn(b' -E foo bar baz frobozz', shebang_line)

    def test_interpreter_args_string_compat(self):
        # A plain string still works for backwards compatibility; it is split
        # with POSIX shell rules, honoring quoting.
        self.maker.variants = set([''])
        self.maker.executable = 'mypython'
        files = self.maker.make('foo = bar:baz', {'interpreter_args': '-E -S'})
        with open(files[0], 'rb') as f:
            shebang_line = f.readline()
        self.assertEqual(shebang_line, b'#!/bin/sh\n')
        with open(files[0], 'rb') as f:
            exec_line = f.read().splitlines()[1]
        self.assertIn(b'"-E" "-S"', exec_line)
        # a single safe token uses the direct shebang
        files = self.maker.make('baz = bar:baz', {'interpreter_args': '-E'})
        with open(files[0], 'rb') as f:
            shebang_line = f.readline()
        self.assertEqual(shebang_line, b'#!mypython -E\n')

    @unittest.skipIf(os.name != 'posix', 'Test only appropriate for '
                     'POSIX systems')
    def test_interpreter_args_execution(self):
        # End-to-end checks: an "interpreter" which just echoes its own argv,
        # driven through a real temporary directory whose path contains spaces
        # and quotes. Generated console entry points and copied prebuilt
        # scripts must yield identical interpreter argument sequences, and the
        # script's own arguments must be appended verbatim (no eval).
        root = tempfile.mkdtemp(prefix="distlib it's \"a\" ")
        self.addCleanup(shutil.rmtree, root)
        bindir = os.path.join(root, "bin dir")
        srcdir = os.path.join(root, 'src')
        os.makedirs(bindir)
        os.makedirs(srcdir)

        # The probe interpreter prints each received argv token on its own line.
        probe = os.path.join(root, "py dir'quot", 'probe-interp')
        os.makedirs(os.path.dirname(probe))
        with open(probe, 'w') as f:
            f.write('#!/bin/sh\n'
                    'for token in "$@"; do printf "%s\\n" "$token"; done\n')
        os.chmod(probe, 0o755)

        # A prebuilt script in the wheel, complete with its own shebang args.
        prebuilt_args = ['-E', 'foo bar', "a'b", 'a"b', 'a;b', '$HOME', '']
        quoted_args = ' '.join(
            '"%s"' % arg.replace('\\', '\\\\').replace('"', '\\"')
            for arg in prebuilt_args)
        with open(os.path.join(srcdir, 'prebuilt.py'), 'w') as f:
            f.write('#!python %s\nprint("prebuilt")\n' % quoted_args)

        maker = ScriptMaker(srcdir, bindir, add_launchers=False)
        maker.variants = set([''])
        maker.clobber = True
        maker.force = True
        maker.executable = probe

        interp_args = ['-E', 'foo bar', "a'b", 'a"b', 'a;b', '$HOME', '']
        copied = maker.make('prebuilt.py')
        made = maker.make('prebuilt-ep = somepkg:main',
                          {'interpreter_args': interp_args})

        def tokens(path, *user_args):
            p = subprocess.Popen([path] + list(user_args),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = p.communicate()
            self.assertEqual(p.returncode, 0, stderr)
            return stdout.decode('utf-8').splitlines()

        user_args = ['user arg one', 'two']
        copied_tokens = tokens(copied[0], *user_args)
        made_tokens = tokens(made[0], *user_args)

        # Token 0..n-3 are the interpreter args, then $0 (the script path),
        # then the script's own arguments appended verbatim.
        expected_interp = ['-E', 'foo bar', "a'b", 'a"b', 'a;b', '$HOME', '']
        self.assertEqual(copied_tokens[:-3], expected_interp)
        self.assertEqual(made_tokens[:-3], expected_interp)
        # Both generation routes produce the same interpreter argument vector.
        self.assertEqual(copied_tokens[:-3], made_tokens[:-3])
        # The last two tokens are the script's own arguments, unchanged.
        self.assertEqual(copied_tokens[-2:], user_args)
        self.assertEqual(made_tokens[-2:], user_args)
        # $0 points at each script respectively.
        self.assertEqual(copied_tokens[-3], copied[0])
        self.assertEqual(made_tokens[-3], made[0])

    @unittest.skipIf(os.name != 'posix', 'Test only appropriate for '
                     'POSIX systems')
    def test_long_path_fallback_and_empty_args(self):
        # A short, simple executable with no args uses the direct shebang.
        root = tempfile.mkdtemp(prefix='distlib-test-')
        self.addCleanup(shutil.rmtree, root)
        maker = ScriptMaker(root, root, add_launchers=False)
        maker.variants = set([''])
        maker.executable = '/usr/bin/python3'
        files = maker.make('short = pkg:main')
        with open(files[0], 'rb') as f:
            self.assertEqual(f.readline(), b'#!/usr/bin/python3\n')

        # A very long executable path forces the trampoline, even with no
        # interpreter args at all.
        long_exe = os.path.join(root, 'p' * 110, 'python')
        maker.executable = long_exe
        files = maker.make('long = pkg:main', {'interpreter_args': []})
        with open(files[0], 'rb') as f:
            self.assertEqual(f.readline(), b'#!/bin/sh\n')
            exec_line = f.readline()
            self.assertIn(('"%s"' % long_exe).encode('utf-8'), exec_line)
            self.assertTrue(exec_line.startswith(b"''''exec "))
            self.assertIn(b'"$0" "$@" # ' + b"'''", exec_line)

        # An empty-string argument cannot live in a direct shebang and also
        # forces the trampoline.
        maker.executable = '/usr/bin/python3'
        files = maker.make('empty = pkg:main', {'interpreter_args': ['']})
        with open(files[0], 'rb') as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], b'#!/bin/sh')
        self.assertIn(b'"" "$0" "$@"', lines[1])

    def test_args_on_copy(self):
        self.maker.variants = set([''])
        self.maker.executable = 'mypython'
        files = self.maker.make('script5.py')
        with open(files[0]) as f:
            actual = f.readline().strip()
        self.assertEqual(actual, '#!mypython -mzippy.activate')
        if not sysconfig.is_python_build():
            self.maker.executable = None
            os.remove(files[0])
            files = self.maker.make('script5.py')
            with open(files[0]) as f:
                content = f.read()
            executable = get_executable()
            # A short path yields a direct shebang ...
            direct = '#!%s -mzippy.activate' % executable
            # ... while a path that's too long (or contains spaces) yields the
            # POSIX trampoline, where the interpreter and its argument are
            # quoted individually as data.
            trampoline = 'exec "%s" "-mzippy.activate" "$0" "$@"' % executable
            self.assertTrue(direct in content or trampoline in content,
                            'neither direct shebang nor trampoline found in %r' % content)

    def test_enquote_executable(self):
        for executable, expected in (('/no/spaces', '/no/spaces'), ('/i have/space', '"/i have/space"'),
                                     ('"/space prequoted"', '"/space prequoted"'),
                                     ('/usr/bin/env nospaces', '/usr/bin/env nospaces'), ('/usr/bin/env with spaces',
                                                                                          '/usr/bin/env "with spaces"'),
                                     ('/usr/bin/env "pre spaced"', '/usr/bin/env "pre spaced"')):
            self.assertEqual(enquote_executable(executable), expected)

    def test_variant_configuration(self):
        spec = 'foo = foo:main'
        files = self.maker.make(spec)
        if os.name == 'nt':
            ext = '.py'
        else:
            ext = ''
        basenames = ('foo%s' % ext, 'foo-%s.%s%s' % (self.maker.version_info[:2] + (ext, )))
        expected = set([os.path.join(self.maker.target_dir, s) for s in basenames])
        self.assertEqual(expected, set(files))
        self.maker.variant_separator = ''
        self.maker.clobber = True
        files = self.maker.make(spec)
        basenames = ('foo%s' % ext, 'foo%s.%s%s' % (self.maker.version_info[:2] + (ext, )))
        expected = set([os.path.join(self.maker.target_dir, s) for s in basenames])
        self.assertEqual(expected, set(files))

        class CustomMaker(ScriptMaker):

            def get_script_filenames(self, name):
                result = super(CustomMaker, self).get_script_filenames(name)
                if 'X.Y' in self.variants:
                    result.add('%s%s.%s' % ((name, ) + self.version_info[:2]))
                return result

        maker = CustomMaker(self.maker.source_dir, self.maker.target_dir, add_launchers=False)
        maker.clobber = True
        files = maker.make(spec)
        basenames = ('foo%s' % ext, 'foo%s.%s%s' % (self.maker.version_info[:2] + (ext, )),
                     'foo-%s.%s%s' % (self.maker.version_info[:2] + (ext, )))
        expected = set([os.path.join(self.maker.target_dir, s) for s in basenames])
        self.assertEqual(expected, set(files))
        if os.name == 'nt':
            maker.add_launchers = True
            files = maker.make(spec)
            expected = set([e.replace('.py', '.exe') for e in expected])
            self.assertEqual(expected, set(files))

    def test_path_doesnt_escape_script_dir(self):
        """
        Check that a crafted entry point name doesn't escape the scripts directory.
        """
        # First, create a directory unconnected to any script being installed and put
        # a known good script in it. Then make a script from an entry point crafted to overwrite it
        # and see if it's caught.
        known_good_dir = tempfile.mkdtemp(prefix='distlib-test-')
        self.addCleanup(shutil.rmtree, known_good_dir)
        fn = os.path.join(known_good_dir, 'known_good')
        with open(fn, 'w') as f:
            f.write('KNOWN GOOD')
        self.maker.clobber = True
        self.maker.variants = set(('',))
        specs = (
            os.path.join('..', os.path.basename(known_good_dir), 'known_good') + '=foo:bar',
            os.path.join('..', '..', os.path.basename(known_good_dir), 'known_good') + '=foo:bar',
        )
        for spec in specs:
            with self.assertRaises(DistlibException) as ctx:
                self.maker.make(spec)
            self.assertEqual(str(ctx.exception), 'Attempt to escape script directory')

if __name__ == '__main__':  # pragma: no cover
    unittest.main()
