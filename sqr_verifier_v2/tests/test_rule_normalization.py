from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from verifier import Config, customer_equivalent, normalize_carrier, normalize_company_name, normalize_po


class RuleNormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = Config.load(ROOT / "config")

    def test_legal_suffixes_are_equivalent(self):
        self.assertTrue(customer_equivalent("Taza Trading, Inc.", "Taza Trading", self.config))
        self.assertTrue(customer_equivalent("Capitol Food Company", "Capitol Food Co", self.config))

    def test_reviewed_ocr_variants_are_equivalent(self):
        self.assertTrue(customer_equivalent("Tropican", "TropiCon Foods, Inc.", self.config))
        self.assertTrue(customer_equivalent("Bella Viva", "Bella Viva Orchards, Inc.", self.config))

    def test_company_normalization_is_conservative(self):
        self.assertNotEqual(normalize_company_name("Trader Joe's"), normalize_company_name("Capitol Food Co"))

    def test_carrier_family_normalization(self):
        self.assertEqual(normalize_carrier("Fed Ex Freight"), normalize_carrier("FedEx Freight Priority"))
        self.assertEqual(normalize_carrier("Best Overnite Express"), normalize_carrier("Best overnight"))

    def test_po_formatting(self):
        self.assertEqual(normalize_po("PO-0015520"), normalize_po("15520"))


if __name__ == "__main__":
    unittest.main()
