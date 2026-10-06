# -*- coding: utf-8 -*-
#
# Copyright (C) 2012-2026 The Python Software Foundation.
# See LICENSE.txt and CONTRIBUTORS.txt.
#
"""Tests for distlib.spdx."""
from __future__ import unicode_literals

from compat import unittest
from support import DistlibTestCase

from distlib.spdx import (SPDXAnd, SPDXLicense, SPDXLicenseRef, SPDXOr, SPDXWithException,
                          SPDXExpressionError, canonicalize_spdx_expression,
                          iter_spdx_identifiers, parse_spdx_expression)

LICENSES = ('MIT', 'Apache-2.0', 'BSD-2-Clause', 'BSD-3-Clause', 'GPL-2.0-only')
EXCEPTIONS = ('Classpath-exception-2.0', 'Autoconf-exception-3.0')


class SPDXParserTestCase(DistlibTestCase):

    def test_simple(self):
        node = parse_spdx_expression('MIT')
        self.assertIsInstance(node, SPDXLicense)
        self.assertEqual(node.name, 'MIT')
        self.assertFalse(node.or_later)
        self.assertEqual(node.to_string(), 'MIT')

    def test_or_later(self):
        node = parse_spdx_expression('GPL-2.0+')
        self.assertTrue(node.or_later)
        self.assertEqual(node.to_string(), 'GPL-2.0+')

    def test_compound_structure(self):
        node = parse_spdx_expression('MIT AND (BSD-2-Clause OR Apache-2.0)')
        self.assertIsInstance(node, SPDXAnd)
        self.assertEqual(node.left.name, 'MIT')
        group = node.right
        self.assertIsInstance(group, SPDXOr)
        self.assertTrue(group.grouped)
        self.assertEqual(group.left.name, 'BSD-2-Clause')
        self.assertEqual(group.right.name, 'Apache-2.0')
        self.assertEqual(node.to_string(),
                         'MIT AND (BSD-2-Clause OR Apache-2.0)')

    def test_or_within_group(self):
        expression = '(MIT OR BSD-2-Clause) AND Apache-2.0'
        node = parse_spdx_expression(expression)
        self.assertIsInstance(node, SPDXAnd)
        self.assertIsInstance(node.left, SPDXOr)
        self.assertTrue(node.left.grouped)
        self.assertEqual(node.to_string(), expression)

    def test_with(self):
        node = parse_spdx_expression('GPL-2.0-only WITH Classpath-exception-2.0')
        self.assertIsInstance(node, SPDXWithException)
        self.assertEqual(node.license.name, 'GPL-2.0-only')
        self.assertEqual(node.exception, 'Classpath-exception-2.0')
        self.assertEqual(node.to_string(),
                         'GPL-2.0-only WITH Classpath-exception-2.0')

    def test_compound_with(self):
        expression = 'MIT AND (GPL-2.0-only WITH Classpath-exception-2.0 OR Apache-2.0)'
        node = parse_spdx_expression(expression)
        self.assertEqual(node.to_string(), expression)

    def test_license_ref(self):
        node = parse_spdx_expression('LicenseRef-Proprietary')
        self.assertIsInstance(node, SPDXLicenseRef)
        self.assertEqual(node.name, 'Proprietary')
        self.assertIsNone(node.document)
        self.assertEqual(node.to_string(), 'LicenseRef-Proprietary')

        node = parse_spdx_expression('DocumentRef-SPA:LicenseRef-Proprietary')
        self.assertEqual(node.document, 'SPA')
        self.assertEqual(node.name, 'Proprietary')
        self.assertEqual(node.to_string(),
                         'DocumentRef-SPA:LicenseRef-Proprietary')

        node = parse_spdx_expression('MIT OR LicenseRef-Custom')
        self.assertIsInstance(node, SPDXOr)
        self.assertIsInstance(node.right, SPDXLicenseRef)

    def test_associative_chains(self):
        node = parse_spdx_expression('MIT AND BSD-2-Clause AND Apache-2.0')
        self.assertIsInstance(node, SPDXAnd)
        self.assertIsInstance(node.left, SPDXAnd)
        self.assertEqual(node.to_string(),
                         'MIT AND BSD-2-Clause AND Apache-2.0')

    def test_redundant_parentheses(self):
        self.assertEqual(canonicalize_spdx_expression('((((MIT))))'), 'MIT')
        expression = 'MIT AND (BSD-2-Clause OR Apache-2.0)'
        self.assertEqual(canonicalize_spdx_expression('(%s)' % expression),
                         expression)

    def test_canonicalise_from_catalogue(self):
        node = parse_spdx_expression('mit AND (bsd-2-clause OR apache-2.0)',
                                     LICENSES, EXCEPTIONS)
        self.assertEqual(node.to_string(),
                         'MIT AND (BSD-2-Clause OR Apache-2.0)')
        node = parse_spdx_expression('gpl-2.0-only WITH CLASSPATH-EXCEPTION-2.0',
                                     LICENSES, EXCEPTIONS)
        self.assertEqual(node.to_string(),
                         'GPL-2.0-only WITH Classpath-exception-2.0')

    def test_keywords_must_be_uppercase(self):
        # SPDX keywords are case-sensitive; lower-case operators are not
        # valid and leave stray identifier tokens.
        for expression in ('mit and bsd-2-clause', 'mit or bsd-2-clause',
                           'mit with x'):
            with self.assertRaises(SPDXExpressionError):
                parse_spdx_expression(expression)

    def test_unknown_identifier_diagnostic(self):
        with self.assertRaises(SPDXExpressionError) as cm:
            parse_spdx_expression('NoSuchLicense', LICENSES, EXCEPTIONS)
        self.assertIn('Unknown SPDX license identifier', str(cm.exception))
        with self.assertRaises(SPDXExpressionError) as cm:
            parse_spdx_expression('MIT WITH NoSuchException', LICENSES,
                                  EXCEPTIONS)
        self.assertIn('Unknown SPDX license exception identifier',
                      str(cm.exception))

    def test_unknown_identifier_without_catalogue(self):
        # Without a catalogue, only grammar validity is enforced and the
        # spelling is preserved.
        node = parse_spdx_expression('MyCompany-1.0 OR LicenseRef-Private')
        self.assertEqual(node.to_string(),
                         'MyCompany-1.0 OR LicenseRef-Private')

    def test_mixed_operators_need_parentheses(self):
        for expression in ('MIT AND BSD-2-Clause OR Apache-2.0',
                           'MIT OR BSD-2-Clause AND Apache-2.0'):
            with self.assertRaises(SPDXExpressionError) as cm:
                parse_spdx_expression(expression)
            self.assertIn('parentheses', str(cm.exception))

    def test_invalid_expressions(self):
        invalid = ('', 'AND', 'MIT AND', 'AND MIT', 'MIT OR', 'WITH',
                   'MIT WITH', 'MIT WITH LicenseRef-X',
                   'LicenseRef-', 'LicenseRef MIT', 'MIT +',
                   'DocumentRef-X:MIT', 'DocumentRef-X:LicenseRef-',
                   '(MIT', 'MIT)', '()', '(', ')', '..', '..MIT', 'MIT..',
                   'MIT OR (BSD', 'MIT OR (BSD', 'MIT WIT MIT',
                   'MIT AND OR BSD', 'MIT OR OR BSD',
                   'LicenseRef-X WITH MIT')
        for expression in invalid:
            with self.assertRaises(SPDXExpressionError, msg=expression):
                parse_spdx_expression(expression)

    def test_iter_identifiers(self):
        node = parse_spdx_expression(
            'MIT AND (GPL-2.0-only WITH Classpath-exception-2.0 OR '
            'LicenseRef-Private)')
        self.assertEqual(list(iter_spdx_identifiers(node)), [
            ('license', 'MIT'),
            ('license', 'GPL-2.0-only'),
            ('exception', 'Classpath-exception-2.0'),
            ('ref', 'LicenseRef-Private'),
        ])

    def test_canonicalise_is_idempotent(self):
        for expression in ('MIT', 'MIT AND (BSD-2-Clause OR Apache-2.0)',
                           '(MIT OR BSD-2-Clause) AND GPL-2.0-only WITH '
                           'Classpath-exception-2.0',
                           '((((MIT))))'):
            once = canonicalize_spdx_expression(expression)
            twice = canonicalize_spdx_expression(once)
            self.assertEqual(once, twice)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
