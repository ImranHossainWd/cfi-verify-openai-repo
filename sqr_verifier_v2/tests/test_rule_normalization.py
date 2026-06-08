from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from verifier import (
    Config,
    PageRecord,
    SubPacket,
    customer_equivalent,
    is_processor_header_customer,
    is_source_or_support_page,
    kg_to_lb,
    looks_like_package_count,
    metal_detector_verification_row_used,
    normalize_carrier,
    normalize_company_name,
    normalize_po,
    office_signoff_present,
    po_equivalent,
    run_subpacket_checks,
    wo_equivalent,
)


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
        self.assertTrue(customer_equivalent("TropiCon Foods, Inc.", "Tropicon Foods, Inc.", self.config))
        self.assertTrue(customer_equivalent("Capitol Food Company - Fresca Warehouse", "Capitol Food Co", self.config))
        self.assertTrue(customer_equivalent("TROPICON FOODS c/o LINEAGE LOGISTICS", "Tropicon Foods, Inc.", self.config))
        self.assertTrue(customer_equivalent("New Customer LLC c/o Cold Storage", "New Customer", self.config))

    def test_company_normalization_is_conservative(self):
        self.assertNotEqual(normalize_company_name("Trader Joe's"), normalize_company_name("Capitol Food Co"))

    def test_carrier_family_normalization(self):
        self.assertEqual(normalize_carrier("Fed Ex Freight"), normalize_carrier("FedEx Freight Priority"))
        self.assertEqual(normalize_carrier("Best Overnite Express"), normalize_carrier("Best overnight"))
        self.assertEqual(normalize_carrier("Vector Logistics"), normalize_carrier("Vektor Logistics"))
        self.assertEqual(normalize_carrier("FedEx"), normalize_carrier("FedEx Freight"))
        self.assertEqual(normalize_carrier("Carrier"), "")

    def test_po_formatting(self):
        self.assertEqual(normalize_po("PO-0015520"), normalize_po("15520"))
        self.assertTrue(po_equivalent("Verbal/Hugh 4/25/2026", "VERBAL/HUGH4/25/26"))
        self.assertTrue(po_equivalent("Vega/Hugh 4/25/26", "VERBAL/HUGH4/25/26"))
        self.assertTrue(po_equivalent("260941601", "26041601"))

    def test_wo_ocr_tolerance(self):
        self.assertTrue(wo_equivalent("1593", "11583"))
        self.assertTrue(wo_equivalent("1556", "11556"))
        self.assertTrue(wo_equivalent("11562", "11552"))

    def test_processor_header_not_customer(self):
        self.assertTrue(is_processor_header_customer("California Fruit Inc."))
        self.assertTrue(is_processor_header_customer("California Fruit Basket"))

    def test_package_counts_are_not_case_counts(self):
        pallet_page = PageRecord(1, "", "BOL", "BOL", {"cases": 1, "all_fields": {"pieces": "1 pallet"}})
        plts_page = PageRecord(2, "", "Bill of Lading", "BOL", {"cases": 2, "all_fields": {"PLTS": "2 PLTS"}})
        stamp_page = PageRecord(3, "", "Stamp Log", "STAMP", {"cases": 1, "all_fields": {"label count": 1}})
        self.assertTrue(looks_like_package_count(pallet_page))
        self.assertTrue(looks_like_package_count(plts_page))
        self.assertTrue(looks_like_package_count(stamp_page))

    def test_weight_unit_conversion_and_gross_context(self):
        self.assertAlmostEqual(kg_to_lb(12.5) * 480, 13228.8, delta=1.0)

    def test_office_required_only_when_metal_detector_row_used(self):
        unused = {"all_fields": {"case_metal_detector_verification": ""}, "initials_present": []}
        printed_blank = {
            "case_metal_detector_verification": {
                "row_used": False,
                "date": "Date",
                "pallet_bin": "Pallet/Bin #",
                "passed": "Passed",
                "failed": "Failed",
                "initials": "Initials",
                "office": "Office",
            },
            "office_verification_present": False,
        }
        used_missing_office = {
            "case_metal_detector_verification": {
                "row_used": True,
                "date": "5/1/26",
                "pallet": "1",
                "result": "Pass",
                "office_checked": False,
            },
            "office_verification_present": False,
        }
        used_with_arbitrary_text = {
            "case_metal_detector_verification": {"row_used": True, "date": "handwritten note"},
            "office_verification_present": False,
        }
        blank_row_with_unrelated_form_values = {
            "case_metal_detector_verification": {
                "row_used": False,
                "date": "5/8/26",
                "initials": "AA",
                "office_checked": False,
            },
            "initials_present": [
                {"location": "Verification", "value": "AA"},
                {"location": "2nd Verification", "value": "EL"},
            ],
        }
        benzler_p51 = {
            "case_metal_detector_verification": {
                "row_used": True,
                "date": "5-6-26",
                "pallet_bin": "Pallet 1",
                "passed": True,
                "failed": False,
                "initials": "SA",
                "office_checked": False,
            },
            "office_verification_present": False,
        }
        used_with_office = {
            "case_metal_detector_verification": {
                "row_used": True,
                "date": "5/1/26",
                "pallet": "1",
                "result": "Pass",
                "office_checked": True,
            },
            "office_verified_by": "AA",
        }
        used_blank_table_office_with_page_signatures = {
            "case_metal_detector_verification": {
                "row_used": True,
                "date": "5/6/26",
                "pallet_bin": "Pallet 1",
                "passed": True,
                "initials": "SA",
                "office_checked": None,
                "office": "",
            },
            "office_verification_present": True,
            "office_verified_by": "EL",
            "initials_present": [
                {"location": "Verification", "value": "AA"},
                {"location": "2nd Verification", "value": "EL"},
            ],
        }
        old_result_with_one_ambiguous_value = {
            "case_metal_detector_verification": {"date": "5/8/26"},
            "initials_present": [{"location": "Verification", "value": "AA"}],
        }
        old_result_with_two_row_values = {
            "case_metal_detector_verification": {"date": "5/6/26", "pallet_bin": "Pallet 1"},
        }
        self.assertFalse(metal_detector_verification_row_used(unused))
        self.assertFalse(metal_detector_verification_row_used(printed_blank))
        self.assertFalse(metal_detector_verification_row_used(blank_row_with_unrelated_form_values))
        self.assertTrue(metal_detector_verification_row_used(used_missing_office))
        self.assertTrue(metal_detector_verification_row_used(used_with_arbitrary_text))
        self.assertTrue(metal_detector_verification_row_used(benzler_p51))
        self.assertFalse(metal_detector_verification_row_used(old_result_with_one_ambiguous_value))
        self.assertTrue(metal_detector_verification_row_used(old_result_with_two_row_values))
        self.assertFalse(office_signoff_present(used_missing_office))
        self.assertFalse(office_signoff_present(used_blank_table_office_with_page_signatures))
        self.assertTrue(office_signoff_present(used_with_office))

    def test_source_coa_customer_and_cases_are_context(self):
        source_coa = PageRecord(
            52,
            "",
            "Certificate of Analysis (COA)",
            "COA",
            {
                "customer": "Lone Star",
                "cases": 728,
                "all_fields": {"source lot": "original lot support"},
            },
        )
        self.assertTrue(is_source_or_support_page(source_coa))

    def test_source_coa_does_not_fail_customer_or_case_count(self):
        primary_page = PageRecord(
            10,
            "",
            "SQR Checkoff List",
            "SQR_CHK",
            {"wo": "11623", "po": "VERBALHUGH2026-04-25", "customer": "Benzler Farms", "product": "Raisins", "cases": 1},
        )
        source_coa = PageRecord(
            59,
            "",
            "Certificate of Analysis (COA)",
            "COA",
            {
                "wo": "99999",
                "po": "SOURCE",
                "customer": "Lone Star",
                "product": "Raisins",
                "cases": 451,
                "all_fields": {"source lot": "original lot support"},
            },
        )
        sp = SubPacket(
            index=0,
            pages=[primary_page, source_coa],
            primary_wo="11623",
            primary_po="VERBALHUGH2026-04-25",
            primary_customer="Benzler Farms",
            primary_product="Raisins",
        )
        run_subpacket_checks(sp, self.config, self.config.find_customer("Benzler Farms"))
        bad = [
            check for check in sp.checks
            if check.status == "fail"
            and check.pages == [59]
            and ("Customer on" in check.name or "Case count on" in check.name)
        ]
        self.assertEqual(bad, [])

    def test_stamp_log_uses_subpacket_primary_wo_when_discovered_set_is_empty(self):
        stamp_page = PageRecord(
            63,
            "",
            "Stamp Log",
            "STAMP",
            {"wo": "11623", "customer": "Example Customer"},
        )
        sp = SubPacket(
            index=0,
            pages=[stamp_page],
            primary_wo="11623",
            primary_customer="Example Customer",
        )
        run_subpacket_checks(sp, self.config, None)
        failures = [
            check for check in sp.checks
            if check.status == "fail"
            and check.pages == [63]
            and check.name.startswith("WO# on Stamp Log")
        ]
        self.assertEqual(failures, [])

    def test_extra_cases_used_source_po_is_not_compared_to_final_po(self):
        primary_page = PageRecord(
            10,
            "",
            "SQR Checkoff List",
            "SQR_CHK",
            {"wo": "11623", "po": "1321391", "customer": "Example Customer"},
        )
        source_page = PageRecord(
            69,
            "",
            "Extra Cases USED form",
            "XC_USED",
            {
                "wo": "53151",
                "po": "Verbal/Hugh 4/25/26",
                "customer": "Example Customer",
                "all_fields": {"original source WO": "53151"},
            },
        )
        sp = SubPacket(
            index=0,
            pages=[primary_page, source_page],
            primary_wo="11623",
            primary_po="1321391",
            primary_customer="Example Customer",
        )
        run_subpacket_checks(sp, self.config, None)
        failures = [
            check for check in sp.checks
            if check.status == "fail"
            and check.pages == [69]
            and check.name.startswith("PO# on Extra Cases USED form")
        ]
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
