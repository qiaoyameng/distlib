# -*- coding: utf-8 -*-
#
# Copyright (C) 2012-2026 The Python Software Foundation.
# See LICENSE.txt and CONTRIBUTORS.txt.
#
"""SPDX license expression parsing and canonicalisation.

Implements the SPDX License Expression grammar used by PEP 639 (see the
SPDX specification, annex IV). Expressions are parsed into a small
syntax tree which preserves the structure of the input (AND/OR/WITH,
parenthesised groups and LicenseRef values); canonical rendering only
adds parentheses where required by precedence.

License identifiers are *not* downloaded from the SPDX license list.
Callers may pass in a catalogue of known identifiers; without one, only
syntactic validation is performed.
"""
from __future__ import unicode_literals

import re

from . import DistlibException

__all__ = [
    'SPDXExpressionError', 'SPDXLicense', 'SPDXLicenseRef', 'SPDXWithException',
    'SPDXAnd', 'SPDXOr', 'parse_spdx_expression', 'canonicalize_spdx_expression',
    'iter_spdx_identifiers',
]


class SPDXExpressionError(DistlibException):
    """An SPDX license expression could not be parsed or validated."""


# Token regex: barewords (identifiers, LicenseRef/DocumentRef) or
# punctuation. Whitespace is skipped.
_TOKEN_RE = re.compile(r'\s*(?:([()])|([^\s()]+))', re.ASCII)

_IDENT_RE = re.compile(r'^[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)*$')
_REF_PART_RE = _IDENT_RE


class SPDXNode(object):
    """Base class for SPDX expression syntax-tree nodes."""

    def __str__(self):
        return self.to_string()

    def __eq__(self, other):
        return type(self) is type(other) and self.__dict__ == other.__dict__

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return '<%s %s>' % (self.__class__.__name__, self.to_string())

    def to_string(self, parent_prec=0):
        raise NotImplementedError


class SPDXLicense(SPDXNode):
    """A simple SPDX license identifier, optionally with a trailing ``+``."""

    def __init__(self, name, or_later=False):
        self.name = name
        self.or_later = or_later

    def to_string(self, parent_prec=0):
        return self.name + ('+' if self.or_later else '')


class SPDXLicenseRef(SPDXNode):
    """A user-defined LicenseRef, optionally namespaced with DocumentRef."""

    def __init__(self, name, document=None):
        self.name = name
        self.document = document

    def to_string(self, parent_prec=0):
        if self.document:
            return 'DocumentRef-%s:LicenseRef-%s' % (self.document, self.name)
        return 'LicenseRef-%s' % self.name


class SPDXWithException(SPDXNode):
    """A license carrying a WITH exception."""

    precedence = 3

    def __init__(self, license, exception):
        self.license = license
        self.exception = exception

    def to_string(self, parent_prec=0):
        return '%s WITH %s' % (self.license.to_string(self.precedence), self.exception)


class SPDXAnd(SPDXNode):
    """An AND conjunction of two sub-expressions."""

    precedence = 2

    def __init__(self, left, right, grouped=False):
        self.left = left
        self.right = right
        self.grouped = grouped

    def to_string(self, parent_prec=0):
        text = '%s AND %s' % (self.left.to_string(self.precedence),
                              self.right.to_string(self.precedence))
        if self.grouped and parent_prec > self.precedence:
            text = '(%s)' % text
        return text


class SPDXOr(SPDXNode):
    """An OR disjunction of two sub-expressions."""

    precedence = 1

    def __init__(self, left, right, grouped=False):
        self.left = left
        self.right = right
        self.grouped = grouped

    def to_string(self, parent_prec=0):
        text = '%s OR %s' % (self.left.to_string(self.precedence),
                             self.right.to_string(self.precedence))
        if self.grouped and parent_prec > self.precedence:
            text = '(%s)' % text
        return text


class _Tokenizer(object):
    def __init__(self, expression):
        self.expression = expression
        self.tokens = []
        pos = 0
        length = len(expression)
        while pos < length:
            m = _TOKEN_RE.match(expression, pos)
            if not m or m.end() == pos:
                raise SPDXExpressionError('Invalid character in SPDX expression '
                                          'at position %d: %r' % (pos, expression[pos:]))
            if m.group(1):
                self.tokens.append((m.group(1), m.start()))
            else:
                word = m.group(2)
                if word in ('AND', 'OR', 'WITH'):
                    self.tokens.append((word, m.start()))
                else:
                    self.tokens.append(('WORD', word, m.start()))
            pos = m.end()
        self.index = 0

    def peek(self):
        if self.index < len(self.tokens):
            return self.tokens[self.index]
        return None

    def next(self):
        token = self.peek()
        if token is not None:
            self.index += 1
        return token


def _make_lookup(catalogue):
    """Return a case-insensitive lookup mapping for a catalogue."""
    lookup = {}
    for identifier in catalogue or ():
        lookup[identifier.upper()] = identifier
    return lookup


def _validate_identifier(word, position, lookup, known=True, is_exception=False):
    kind = 'license exception' if is_exception else 'license'
    if not _IDENT_RE.match(word):
        raise SPDXExpressionError('Invalid %s identifier %r at position %d'
                                  % (kind, word, position))
    if known:
        canonical = lookup.get(word.upper())
        if canonical is None:
            raise SPDXExpressionError('Unknown SPDX %s identifier %r' % (kind, word))
        return canonical
    return word


def _parse_ref(word, position):
    if word.startswith('DocumentRef-'):
        rest = word[len('DocumentRef-'):]
        if ':' not in rest:
            raise SPDXExpressionError('Malformed SPDX license reference %r '
                                      'at position %d' % (word, position))
        document, ref = rest.split(':', 1)
        if not document or not _REF_PART_RE.match(document):
            raise SPDXExpressionError('Invalid DocumentRef in %r at position %d'
                                      % (word, position))
        if not ref.startswith('LicenseRef-'):
            raise SPDXExpressionError('DocumentRef must be followed by a '
                                      'LicenseRef in %r at position %d' % (word, position))
        name = ref[len('LicenseRef-'):]
        if not name or not _REF_PART_RE.match(name):
            raise SPDXExpressionError('Invalid LicenseRef in %r at position %d'
                                      % (word, position))
        return SPDXLicenseRef(name, document)
    if word.startswith('LicenseRef-'):
        name = word[len('LicenseRef-'):]
        if not name or not _REF_PART_RE.match(name):
            raise SPDXExpressionError('Invalid LicenseRef in %r at position %d'
                                      % (word, position))
        return SPDXLicenseRef(name)
    return None


class _Parser(object):
    def __init__(self, expression, licenses=None, exceptions=None):
        self.expression = expression
        self.tokenizer = _Tokenizer(expression)
        self.has_licenses = licenses is not None
        self.has_exceptions = exceptions is not None
        self.licenses = _make_lookup(licenses)
        self.exceptions = _make_lookup(exceptions)

    def parse(self):
        if self.tokenizer.peek() is None:
            raise SPDXExpressionError('Empty SPDX license expression')
        node = self.parse_or()
        if self.tokenizer.peek() is not None:
            token = self.tokenizer.peek()
            raise SPDXExpressionError('Unexpected token %r at position %d in '
                                      'SPDX expression' % (token[0], token[-1]))
        return node

    def parse_or(self):
        left = self.parse_and()
        token = self.tokenizer.peek()
        while token and token[0] == 'OR':
            # SPDX: AND binds tighter than OR; mixing them without
            # parentheses is ambiguous and not allowed.
            if isinstance(left, SPDXAnd) and not left.grouped:
                raise SPDXExpressionError('AND cannot be mixed with OR without '
                                          'parentheses at position %d' % token[1])
            self.tokenizer.next()
            right = self.parse_and()
            if isinstance(right, SPDXAnd) and not right.grouped:
                raise SPDXExpressionError('AND cannot be mixed with OR without '
                                          'parentheses at position %d' % token[1])
            left = SPDXOr(left, right)
            token = self.tokenizer.peek()
        return left

    def parse_and(self):
        left = self.parse_with()
        token = self.tokenizer.peek()
        while token and token[0] == 'AND':
            self.tokenizer.next()
            right = self.parse_with()
            if isinstance(right, SPDXOr) and not right.grouped:
                position = token[1]
                raise SPDXExpressionError('OR cannot be mixed with AND without '
                                          'parentheses at position %d' % position)
            left = SPDXAnd(left, right)
            token = self.tokenizer.peek()
        return left

    def parse_with(self):
        node = self.parse_atom()
        token = self.tokenizer.peek()
        if token and token[0] == 'WITH':
            position = token[1]
            if not isinstance(node, SPDXLicense):
                raise SPDXExpressionError('WITH may only follow a simple license '
                                          'identifier at position %d' % position)
            self.tokenizer.next()
            exception_token = self.tokenizer.next()
            if exception_token is None or exception_token[0] != 'WORD':
                raise SPDXExpressionError('Expected a license exception identifier '
                                          'after WITH at position %d' % position)
            word = exception_token[1]
            ref = _parse_ref(word, exception_token[2])
            if ref is not None:
                raise SPDXExpressionError('LicenseRef cannot be used as a license '
                                          'exception: %r' % word)
            if word.endswith('+'):
                raise SPDXExpressionError('The + operator cannot be used on a '
                                          'license exception: %r' % word)
            exception = _validate_identifier(word, exception_token[2], self.exceptions,
                                             known=self.has_exceptions,
                                             is_exception=True)
            node = SPDXWithException(node, exception)
        return node

    def parse_atom(self):
        token = self.tokenizer.next()
        if token is None:
            raise SPDXExpressionError('Expected a license identifier, but reached '
                                      'the end of the SPDX expression')
        if token[0] == ')':
            raise SPDXExpressionError('Unbalanced closing parenthesis at '
                                      'position %d' % token[1])
        if token[0] in ('AND', 'OR', 'WITH'):
            raise SPDXExpressionError('Unexpected operator %r at position %d'
                                      % (token[0], token[1]))
        if token[0] == '(':
            position = token[1]
            node = self.parse_or()
            closing = self.tokenizer.next()
            if closing is None or closing[0] != ')':
                raise SPDXExpressionError('Missing closing parenthesis for '
                                          'group opened at position %d' % position)
            if isinstance(node, (SPDXAnd, SPDXOr)):
                node.grouped = True
            return node
        word, position = token[1], token[2]
        ref = _parse_ref(word, position)
        if ref is not None:
            return ref
        or_later = word.endswith('+')
        bare = word[:-1] if or_later else word
        if not bare:
            raise SPDXExpressionError('The + operator must follow a license '
                                      'identifier at position %d' % position)
        canonical = _validate_identifier(bare, position, self.licenses,
                                         known=self.has_licenses)
        return SPDXLicense(canonical, or_later)


def parse_spdx_expression(expression, licenses=None, exceptions=None):
    """Parse *expression* and return the root of the syntax tree.

    *licenses* and *exceptions* are optional iterables of identifiers known
    to the caller. When provided, identifiers are looked up
    case-insensitively and canonicalised to the catalogue spelling; when
    omitted, only the grammar is checked and identifiers are preserved as
    written.
    """
    return _Parser(expression, licenses, exceptions).parse()


def canonicalize_spdx_expression(expression, licenses=None, exceptions=None):
    """Parse *expression* and return its canonical textual form."""
    return parse_spdx_expression(expression, licenses, exceptions).to_string()


def iter_spdx_identifiers(node):
    """Yield ``(kind, value)`` pairs for the licenses and exceptions in *node*.

    ``kind`` is one of ``'license'`` (a plain identifier), ``'ref'`` (a
    LicenseRef) or ``'exception'``.
    """
    if isinstance(node, SPDXLicense):
        yield ('license', node.name)
    elif isinstance(node, SPDXLicenseRef):
        yield ('ref', node.to_string())
    elif isinstance(node, SPDXWithException):
        for item in iter_spdx_identifiers(node.license):
            yield item
        yield ('exception', node.exception)
    else:
        for child in (node.left, node.right):
            for item in iter_spdx_identifiers(child):
                yield item
