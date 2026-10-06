# -*- coding: utf-8 -*-
#
# Copyright (C) 2026 Vinay Sajip.
# Licensed to the Python Software Foundation under a contributor agreement.
# See LICENSE.txt and CONTRIBUTORS.txt.
#
"""
Support for SPDX license expressions, as used by the ``License-Expression``
field defined in PEP 639 (Core Metadata 2.4).

This module provides structure-preserving parsing and normalisation of SPDX
license expressions. It handles the ``AND``, ``OR`` and ``WITH`` operators,
parentheses and ``LicenseRef``/``DocumentRef`` custom license references.

No identifier catalogue is bundled and nothing is ever fetched from the
network: callers may optionally supply a :class:`LicenseCatalog` (or a plain
iterable of known identifiers) which is then used to validate identifiers and
to normalise their case.
"""

import re

from . import DistlibException

__all__ = [
    'SPDXError', 'LicenseCatalog', 'License', 'With', 'And', 'Or', 'parse_license_expression',
    'normalize_license_expression', 'extract_license_identifiers'
]


class SPDXError(DistlibException):
    """Raised when an SPDX license expression is invalid."""


# An SPDX "idstring" as used in license identifiers and LicenseRef values.
_IDSTRING_RE = re.compile(r'^[A-Za-z0-9.-]+$')

# Custom license references, e.g. LicenseRef-Proprietary or
# DocumentRef-foo:LicenseRef-bar (see the SPDX specification).
_LICENSEREF_RE = re.compile(r'^(DocumentRef-[A-Za-z0-9.-]+:)?LicenseRef-[A-Za-z0-9.-]+$')

_OPERATORS = ('AND', 'OR', 'WITH')

_TOKEN_RE = re.compile(r'\s*(\(|\)|[^\s()]+)')


def is_license_ref(ident):
    """Return True if *ident* is a custom license reference (LicenseRef)."""
    return bool(_LICENSEREF_RE.match(ident))


class LicenseCatalog(object):
    """
    A catalogue of known SPDX license and exception identifiers.

    The catalogue is provided by the caller - nothing is downloaded. Lookups
    are case-insensitive and return the canonical (catalogue) spelling.
    """

    def __init__(self, licenses=(), exceptions=()):
        self.licenses = dict((ident.lower(), ident) for ident in licenses)
        self.exceptions = dict((ident.lower(), ident) for ident in exceptions)

    def license(self, ident):
        """Return the canonical spelling of a license identifier, or None."""
        return self.licenses.get(ident.lower())

    def exception(self, ident):
        """Return the canonical spelling of an exception identifier, or None."""
        return self.exceptions.get(ident.lower())


def _coerce_catalog(catalog):
    if catalog is not None and not isinstance(catalog, LicenseCatalog):
        catalog = LicenseCatalog(catalog)
    return catalog


class Node(object):
    """Base class for nodes of a parsed license expression."""

    parenthesized = False

    def normalize(self, catalog=None):
        raise NotImplementedError

    def identifiers(self):
        """Yield the license identifiers used in this node."""
        raise NotImplementedError

    def exception_identifiers(self):
        """Yield the exception identifiers (after WITH) used in this node."""
        raise NotImplementedError


class License(Node):
    """A single license identifier, e.g. ``MIT`` or ``LicenseRef-Foo``."""

    def __init__(self, ident):
        self.ident = ident

    def normalize(self, catalog=None):
        result = self.ident
        if catalog is not None and not is_license_ref(result):
            canonical = catalog.license(result)
            if canonical is not None:
                result = canonical
        return result

    def identifiers(self):
        yield self.ident

    def exception_identifiers(self):
        return
        yield

    def __eq__(self, other):
        return isinstance(other, License) and other.ident == self.ident

    def __repr__(self):
        return 'License(%r)' % self.ident


class With(Node):
    """A license with an exception, e.g. ``Apache-2.0 WITH LLVM-exception``."""

    def __init__(self, license, exception):
        self.license = license
        self.exception = exception

    def normalize(self, catalog=None):
        exception = self.exception
        if catalog is not None:
            canonical = catalog.exception(exception)
            if canonical is not None:
                exception = canonical
        return '%s WITH %s' % (self.license.normalize(catalog), exception)

    def identifiers(self):
        return self.license.identifiers()

    def exception_identifiers(self):
        yield self.exception

    def __eq__(self, other):
        return (isinstance(other, With) and other.license == self.license and other.exception == self.exception)

    def __repr__(self):
        return 'With(%r, %r)' % (self.license, self.exception)


class _Compound(Node):

    operator = None

    def __init__(self, left, right):
        self.left = left
        self.right = right

    def normalize(self, catalog=None):
        result = '%s %s %s' % (self.left.normalize(catalog), self.operator, self.right.normalize(catalog))
        if self.parenthesized:
            result = '(%s)' % result
        return result

    def identifiers(self):
        for ident in self.left.identifiers():
            yield ident
        for ident in self.right.identifiers():
            yield ident

    def exception_identifiers(self):
        for ident in self.left.exception_identifiers():
            yield ident
        for ident in self.right.exception_identifiers():
            yield ident

    def __eq__(self, other):
        return (isinstance(other, type(self)) and other.left == self.left and other.right == self.right
                and other.parenthesized == self.parenthesized)

    def __repr__(self):
        return '%s(%r, %r)' % (type(self).__name__, self.left, self.right)


class And(_Compound):
    operator = 'AND'


class Or(_Compound):
    operator = 'OR'


class _Parser(object):
    """Recursive-descent parser for the SPDX expression subset of PEP 639."""

    def __init__(self, text):
        self.text = text
        self.tokens = self._tokenize(text)
        self.pos = 0

    @staticmethod
    def _tokenize(text):
        tokens = []
        pos = 0
        while pos < len(text):
            m = _TOKEN_RE.match(text, pos)
            if not m:
                if text[pos:].strip():
                    raise SPDXError('Invalid character in license expression: %r' % text[pos:])
                break
            pos = m.end()
            token = m.group(1)
            if token in ('(', ')'):
                tokens.append(token)
            elif token.upper() in _OPERATORS:
                tokens.append(token.upper())
            else:
                tokens.append(('ID', token))
        return tokens

    def _peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def _next(self):
        token = self._peek()
        self.pos += 1
        return token

    @staticmethod
    def _token_text(token):
        if isinstance(token, tuple):
            return token[1]
        return token

    def parse(self):
        if not self.tokens:
            raise SPDXError('Empty license expression')
        result = self._or()
        if self._peek() is not None:
            raise SPDXError('Unexpected token in license expression: %r' % (self._token_text(self._peek()), ))
        return result

    def _or(self):
        left = self._and()
        while self._peek() == 'OR':
            self._next()
            left = Or(left, self._and())
        return left

    def _and(self):
        left = self._with()
        while self._peek() == 'AND':
            self._next()
            left = And(left, self._with())
        return left

    def _with(self):
        node = self._primary()
        if self._peek() == 'WITH':
            self._next()
            token = self._next()
            if not (isinstance(token, tuple) and token[0] == 'ID'):
                raise SPDXError('Expected an exception identifier after WITH')
            if isinstance(node, (And, Or)):
                raise SPDXError('WITH requires a single license identifier, not a compound expression')
            node = With(node, token[1])
        return node

    def _primary(self):
        token = self._next()
        if token == '(':
            node = self._or()
            if self._next() != ')':
                raise SPDXError('Missing closing parenthesis in license expression')
            node.parenthesized = True
            return node
        if isinstance(token, tuple) and token[0] == 'ID':
            ident = token[1]
            if not _IDSTRING_RE.match(ident) and not is_license_ref(ident):
                raise SPDXError('Invalid license identifier: %r' % ident)
            return License(ident)
        if token is None:
            raise SPDXError('Unexpected end of license expression')
        raise SPDXError('Unexpected token in license expression: %r' % (self._token_text(token), ))


def _validate_identifiers(node, catalog):
    for ident in node.identifiers():
        if is_license_ref(ident):
            continue
        if catalog.license(ident) is None:
            raise SPDXError('Unknown license identifier: %r' % ident)
    for ident in node.exception_identifiers():
        if catalog.exception(ident) is None:
            raise SPDXError('Unknown license exception identifier: %r' % ident)


def parse_license_expression(text, catalog=None):
    """
    Parse an SPDX license expression and return its structure (a tree of
    :class:`License`, :class:`With`, :class:`And` and :class:`Or` nodes).

    If *catalog* is given (a :class:`LicenseCatalog` or an iterable of known
    license identifiers), license and exception identifiers are validated
    against it and an :class:`SPDXError` is raised for unknown identifiers.
    ``LicenseRef``/``DocumentRef`` references are always accepted.
    """
    if not isinstance(text, str):
        raise SPDXError('License expression must be text, not %r' % type(text).__name__)
    node = _Parser(text).parse()
    catalog = _coerce_catalog(catalog)
    if catalog is not None:
        _validate_identifiers(node, catalog)
    return node


def normalize_license_expression(text, catalog=None):
    """
    Parse an SPDX license expression and return its normalised form:
    operators are upper-cased, identifiers use the canonical catalogue
    spelling (if a catalogue is supplied) and the parenthesisation of the
    source expression is preserved.
    """
    catalog = _coerce_catalog(catalog)
    return parse_license_expression(text, catalog).normalize(catalog)


def extract_license_identifiers(text, catalog=None):
    """
    Parse an SPDX license expression and return the sorted set of license
    identifiers it uses (including any ``LicenseRef`` references).
    """
    return sorted(set(parse_license_expression(text, catalog).identifiers()))
