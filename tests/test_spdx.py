# -*- coding: utf-8 -*-
#
# Copyright (C) 2026 Vinay Sajip.
# Licensed to the Python Software Foundation under a contributor agreement.
# See LICENSE.txt and CONTRIBUTORS.txt.
#
"""Tests for distlib.spdx."""
from __future__ import unicode_literals

from compat import unittest

from distlib.spdx import (SPDXError, LicenseCatalog, License, With, And, Or, parse_license_expression,
                          normalize_license_expression, extract_license_identifiers)

CATALOG = LicenseCatalog(
    licenses=('MIT', 'Apache-2.0', 'BSD-3-Clause', 'GPL-2.0-only'),
    exceptions=('LLVM-exception', 'Classpath-exception-2.0'),
)


class SPDXParseTestCase(unittest.TestCase):

    def test_simple_identifier(self):
        node = parse_license_expression('MIT')
        self.assertEqual(node, License('MIT'))
        self.assertEqual(node.normalize(), 'MIT')

    def test_compound_expression_structure(self):
        node = parse_license_expression('(MIT OR Apache-2.0) AND BSD-3-Clause')
        self.assertIsInstance(node, And)
        self.assertIsInstance(node.left, Or)
        self.assertTrue(node.left.parenthesized)
        self.assertEqual(node.left.left, License('MIT'))
        self.assertEqual(node.left.right, License('Apache-2.0'))
        self.assertEqual(node.right, License('BSD-3-Clause'))

    def test_precedence(self):
        # AND binds tighter than OR
        node = parse_license_expression('MIT OR Apache-2.0 AND BSD-3-Clause')
        self.assertIsInstance(node, Or)
        self.assertIsInstance(node.right, And)

    def test_with_exception(self):
        node = parse_license_expression('Apache-2.0 WITH LLVM-exception')
        self.assertIsInstance(node, With)
        self.assertEqual(node.license, License('Apache-2.0'))
        self.assertEqual(node.exception, 'LLVM-exception')
        self.assertEqual(list(node.identifiers()), ['Apache-2.0'])
        self.assertEqual(list(node.exception_identifiers()), ['LLVM-exception'])

    def test_license_ref(self):
        node = parse_license_expression('LicenseRef-Proprietary AND MIT')
        self.assertIsInstance(node, And)
        self.assertEqual(node.left, License('LicenseRef-Proprietary'))
        node = parse_license_expression('DocumentRef-foo:LicenseRef-bar')
        self.assertEqual(node, License('DocumentRef-foo:LicenseRef-bar'))

    def test_syntax_errors(self):
        for text in ('', '   ', 'MIT AND', 'OR MIT', '(MIT OR Apache-2.0', 'MIT Apache-2.0', 'MIT OR )',
                     'MIT WITH', '(MIT) WITH)', 'MIT $$'):
            self.assertRaises(SPDXError, parse_license_expression, text)

    def test_with_rejects_compound(self):
        # WITH binds to the immediately preceding license, so this is valid
        node = parse_license_expression('MIT AND Apache-2.0 WITH LLVM-exception')
        self.assertIsInstance(node, And)
        self.assertIsInstance(node.right, With)
        # ... but a parenthesised compound expression can't take WITH
        self.assertRaises(SPDXError, parse_license_expression, '(MIT OR Apache-2.0) WITH LLVM-exception')

    def test_non_text_rejected(self):
        self.assertRaises(SPDXError, parse_license_expression, None)
        self.assertRaises(SPDXError, parse_license_expression, 42)


class SPDXNormalizeTestCase(unittest.TestCase):

    def test_operators_normalised(self):
        self.assertEqual(normalize_license_expression('mit and apache-2.0 or bsd-3-clause'),
                         'mit AND apache-2.0 OR bsd-3-clause')

    def test_parentheses_preserved(self):
        self.assertEqual(normalize_license_expression('(MIT OR Apache-2.0) AND BSD-3-Clause'),
                         '(MIT OR Apache-2.0) AND BSD-3-Clause')

    def test_whitespace_collapsed(self):
        self.assertEqual(normalize_license_expression('  MIT\n\tAND   Apache-2.0 '), 'MIT AND Apache-2.0')

    def test_catalog_case_normalisation(self):
        self.assertEqual(normalize_license_expression('(mit OR apache-2.0) AND bsd-3-clause', CATALOG),
                         '(MIT OR Apache-2.0) AND BSD-3-Clause')

    def test_exception_case_normalisation(self):
        self.assertEqual(normalize_license_expression('apache-2.0 with llvm-exception', CATALOG),
                         'Apache-2.0 WITH LLVM-exception')

    def test_catalog_as_iterable(self):
        self.assertEqual(normalize_license_expression('mit', ['MIT']), 'MIT')

    def test_license_ref_case_preserved(self):
        # LicenseRef values are case-sensitive and never rewritten
        self.assertEqual(normalize_license_expression('LicenseRef-proprietary AND mit', CATALOG),
                         'LicenseRef-proprietary AND MIT')

    def test_unknown_identifier_with_catalog(self):
        self.assertRaises(SPDXError, normalize_license_expression, 'FooBar-1.0', CATALOG)
        self.assertRaises(SPDXError, normalize_license_expression, 'MIT OR FooBar-1.0', CATALOG)

    def test_unknown_exception_with_catalog(self):
        self.assertRaises(SPDXError, normalize_license_expression, 'MIT WITH Foo-exception', CATALOG)

    def test_unknown_identifier_without_catalog(self):
        # Without a catalogue, only syntax is checked
        self.assertEqual(normalize_license_expression('FooBar-1.0 AND MIT'), 'FooBar-1.0 AND MIT')

    def test_license_ref_not_checked_against_catalog(self):
        self.assertEqual(normalize_license_expression('LicenseRef-Proprietary AND MIT', CATALOG),
                         'LicenseRef-Proprietary AND MIT')


class SPDXIdentifiersTestCase(unittest.TestCase):

    def test_extract_identifiers(self):
        self.assertEqual(extract_license_identifiers('(MIT OR Apache-2.0) AND MIT AND LicenseRef-X'),
                         ['Apache-2.0', 'LicenseRef-X', 'MIT'])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
