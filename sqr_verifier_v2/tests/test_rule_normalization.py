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
    page_has_relevant_po,
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
        self.assertTrue(customer_equivalent("New Customer, Inc. (A.M.S.)", "New Customer", self.config))
        self.assertTrue(customer_equivalent("Tom & Gloescer", "Torn & Glasser", self.config))
        self.assertTrue(customer_equivalent("Tom & Gleessr", "Torn & Glasser", self.config))
        self.assertTrue(customer_equivalent("Narth Valley", "North Velley", self.config))
        self.assertFalse(customer_equivalent("North Valley", "South Harbor", self.config))

    def test_company_normalization_is_conservative(self):
        self.assertNotEqual(normalize_company_name("Trader Joe's"), normalize_company_name("Capitol Food Co"))

    def test_carrier_family_normalization(self):
        self.assertEqual(normalize_carrier("Fed Ex Freight"), normalize_carrier("FedEx Freight Priority"))
        self.assertEqual(normalize_carrier("Best Overnite Express"), normalize_carrier("Best overnight"))
        self.assertEqual(normalize_carrier("Vector Logistics"), normalize_carrier("Vektor Logistics"))
        self.assertEqual(normalize_carrier("FedEx"), normalize_carrier("FedEx Freight"))
        self.assertEqual(normalize_carrier("T Force"), normalize_carrier("TForce Inc"))
        self.assertEqual(normalize_carrier("Xpress Global"), normalize_carrier("Xpress Global LLC XGS1"))
        self.assertEqual(normalize_carrier("Xpress Global LLC"), normalize_carrier("Xpress Global LLC XGS1"))
        self.assertEqual(normalize_carrier("Carrier"), "")
        self.assertEqual(normalize_carrier("name of trucking company delivering to customer"), "")

    def test_po_formatting(self):
        self.assertEqual(normalize_po("PO-0015520"), normalize_po("15520"))
        self.assertTrue(po_equivalent("Verbal/Hugh 4/25/2026", "VERBAL/HUGH4/25/26"))
        self.assertTrue(po_equivalent("Vega/Hugh 4/25/26", "VERBAL/HUGH4/25/26"))
        self.assertTrue(po_equivalent("260941601", "26041601"))
        self.assertTrue(po_equivalent("16196", "161596"))
        self.assertTrue(po_equivalent("132129-1", "1321391"))

    def test_page_po_candidates_override_wrong_first_read(self):
        page = PageRecord(
            9, "", "Trailer / Cargo Inspection", "TRAILER",
            {"po": "16196", "po_candidates": ["161596"]},
        )
        self.assertTrue(page_has_relevant_po(page, "161596"))

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

    def test_pretest_values_are_not_compared_to_final_coa_stage(self):
        coa = PageRecord(
            6, "", "Certificate of Analysis", "COA",
            {"wo": "11607", "customer": "Example Customer", "moisture_pct": 28.0, "sulfur_ppm": 1920},
        )
        pretest = PageRecord(
            23, "", "Pre-Test", "PRETEST",
            {"wo": "11607", "customer": "Example Customer", "moisture_pct": 27.5, "sulfur_ppm": 1856},
        )
        sp = SubPacket(
            index=0, pages=[coa, pretest], primary_wo="11607",
            primary_customer="Example Customer",
        )
        run_subpacket_checks(sp, self.config, None)
        failures = [
            check for check in sp.checks
            if check.status == "fail"
            and ("Moisture cross-page" in check.name or "Sulfur ppm cross-page" in check.name)
        ]
        self.assertEqual(failures, [])

    def test_gross_shipping_weight_can_exceed_calculated_net(self):
        po_page = PageRecord(
            3, "", "Customer PO", "PO",
            {
                "wo": "11605", "po": "123456", "customer": "Example Customer",
                "cases": 80, "unit_lbs": 25, "total_lbs": 2040,
                "all_fields": {"weight label": "Gross Weight"},
            },
        )
        sp = SubPacket(
            index=0, pages=[po_page], primary_wo="11605",
            primary_po="123456", primary_customer="Example Customer",
        )
        run_subpacket_checks(sp, self.config, None)
        failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name.startswith("Total weight calc")
        ]
        self.assertEqual(failures, [])

    def test_required_form_can_match_elsewhere_by_wo(self):
        current = PageRecord(
            5, "", "Final Packed Product Sheet", "FPP",
            {"wo": "11609", "customer": "Example Customer", "product": "Raisins"},
        )
        shared_coa = PageRecord(
            64, "", "Certificate of Analysis", "COA",
            {"wo": "11609", "customer": "Example Customer", "product": "Raisins"},
        )
        shared_stamp = PageRecord(
            65, "", "Stamp Log", "STAMP",
            {"wo": "11609", "customer": "Example Customer", "product": "Raisins"},
        )
        sp = SubPacket(
            index=1, pages=[current], primary_wo="11609",
            primary_customer="Example Customer", primary_product="Raisins",
        )
        run_subpacket_checks(
            sp, self.config, None,
            packet_pages=[current, shared_coa, shared_stamp],
        )
        missing = [
            check for check in sp.checks
            if check.status == "fail"
            and check.name in {
                "Required form: Certificate of Analysis",
                "Required form: Stamp Log",
            }
        ]
        self.assertEqual(missing, [])

    def test_source_coa_can_match_through_extra_case_source_wo(self):
        current = PageRecord(
            10, "", "SQR Checkoff List", "SQR_CHK",
            {
                "wo": "11620", "customer": "Benzler Farms",
                "product": "Golden Raisins",
            },
        )
        bridge = PageRecord(
            51, "", "SQR (Extra Case)", "SQR_XC",
            {
                "wo": "11620", "customer": "Benzler Farms",
                "product": "Golden Raisins",
                "all_fields": {"original WO": "11552"},
            },
        )
        source_coa = PageRecord(
            59, "", "Certificate of Analysis", "COA",
            {
                "wo": "11552", "customer": "Lone Star",
                "product": "Golden Raisins", "cases": 451,
                "all_fields": {"source lot": "original lot support"},
            },
        )
        sp = SubPacket(
            index=0, pages=[current], primary_wo="11620",
            primary_customer="Benzler Farms", primary_product="Golden Raisins",
        )
        run_subpacket_checks(
            sp, self.config, self.config.find_customer("Benzler Farms"),
            packet_pages=[current, bridge, source_coa],
        )
        failures = [
            check for check in sp.checks
            if check.status == "fail"
            and check.name == "Required form: Certificate of Analysis"
        ]
        self.assertEqual(failures, [])

    def test_unrelated_source_coa_does_not_satisfy_required_form(self):
        current = PageRecord(
            10, "", "SQR Checkoff List", "SQR_CHK",
            {
                "wo": "11620", "customer": "Example Customer",
                "product": "Golden Raisins",
            },
        )
        bridge = PageRecord(
            51, "", "SQR (Extra Case)", "SQR_XC",
            {
                "wo": "11620", "customer": "Example Customer",
                "product": "Golden Raisins",
                "all_fields": {"original WO": "11552"},
            },
        )
        unrelated_coa = PageRecord(
            59, "", "Certificate of Analysis", "COA",
            {
                "wo": "99999", "customer": "Other Source",
                "product": "Pitted Prunes",
                "all_fields": {"source lot": "unrelated original lot"},
            },
        )
        sp = SubPacket(
            index=0, pages=[current], primary_wo="11620",
            primary_customer="Example Customer", primary_product="Golden Raisins",
        )
        run_subpacket_checks(
            sp, self.config, None,
            packet_pages=[current, bridge, unrelated_coa],
        )
        failures = [
            check for check in sp.checks
            if check.status == "fail"
            and check.name == "Required form: Certificate of Analysis"
        ]
        self.assertEqual(len(failures), 1)

    def test_source_vendor_subpacket_does_not_require_final_order_forms(self):
        vendor_po = PageRecord(
            6, "", "Customer PO", "PO",
            {"customer": "Source Vendor Inc.", "product": "Apricots", "wo": "99881"},
        )
        sp = SubPacket(
            index=1, pages=[vendor_po], primary_wo="99881",
            primary_customer="Source Vendor Inc.", primary_product="Apricots",
        )
        packet_customer = self.config.find_customer("Trader Joe's")
        run_subpacket_checks(sp, self.config, packet_customer, packet_pages=[vendor_po])
        required_failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name.startswith("Required form:")
        ]
        customer_failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name.startswith("Customer on")
        ]
        self.assertEqual(required_failures, [])
        self.assertEqual(customer_failures, [])

    def test_copacker_vendor_po_is_support_even_when_current_wo_is_present(self):
        vendor_po = PageRecord(
            15, "", "Customer PO", "PO",
            {
                "customer": "Garry Packing, Inc.", "wo": "11582",
                "product": "Apricots",
            },
        )
        sp = SubPacket(
            index=0, pages=[vendor_po], primary_wo="11582",
            primary_customer="Trader Joe's", primary_product="Apricots",
        )
        run_subpacket_checks(
            sp, self.config, None, packet_pages=[vendor_po],
        )
        customer_failures = [
            check for check in sp.checks
            if check.status == "fail"
            and check.name.startswith("Customer on Customer PO")
        ]
        self.assertEqual(customer_failures, [])

    def test_source_wo_quantity_is_not_added_to_final_order_total(self):
        sqr = PageRecord(
            4, "", "SQR Checkoff List", "SQR_CHK",
            {"wo": "11610", "customer": "Example Customer", "cases": 70},
        )
        source_coa = PageRecord(
            12, "", "Source COA", "COA",
            {
                "wo": "11500", "customer": "Source Vendor", "cases": 170,
                "all_fields": {"source lot": "original lot"},
            },
        )
        bol = PageRecord(
            7, "", "Bill of Lading", "BOL",
            {"wo": "11610", "customer": "Example Customer", "cases": 70},
        )
        sp = SubPacket(
            index=0, pages=[sqr, source_coa, bol], primary_wo="11610",
            primary_customer="Example Customer",
        )
        run_subpacket_checks(sp, self.config, None)
        failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name.startswith("Case count on Bill of Lading")
        ]
        self.assertEqual(failures, [])

    def test_repeated_alternate_vendor_po_pages_are_source_support(self):
        vendor_page_1 = PageRecord(
            15, "", "Customer PO", "PO",
            {"customer": "Source Packing, Inc.", "wo": "99101", "product": "Apricots"},
        )
        vendor_page_2 = PageRecord(
            20, "", "Shipping label", "SHIP_LABEL",
            {"customer": "Source Packing Inc", "wo": "99102", "product": "Apricots"},
        )
        sp = SubPacket(
            index=1,
            pages=[vendor_page_1, vendor_page_2],
            primary_wo="11582",
            primary_customer="Trader Joe's",
            primary_product="Apricots",
        )
        packet_customer = self.config.find_customer("Trader Joe's")
        run_subpacket_checks(
            sp, self.config, packet_customer,
            packet_pages=[vendor_page_1, vendor_page_2],
        )
        customer_failures = [
            check for check in sp.checks
            if check.status == "fail"
            and (
                check.name.startswith("Customer on Customer PO")
                or check.name.startswith("Customer on Shipping label")
            )
        ]
        required_failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name.startswith("Required form:")
        ]
        self.assertEqual(customer_failures, [])
        self.assertEqual(required_failures, [])

    def test_stamp_log_can_be_shared_by_product_across_work_orders(self):
        current = PageRecord(
            15, "", "Customer PO", "PO",
            {"wo": "11582", "customer": "Example Customer", "product": "Apricots"},
        )
        shared_stamp = PageRecord(
            40, "", "Stamp Log", "STAMP",
            {"wo": "11583", "customer": "Example Customer", "product": "Apricots"},
        )
        sp = SubPacket(
            index=0, pages=[current], primary_wo="11582",
            primary_customer="Example Customer", primary_product="Apricots",
        )
        run_subpacket_checks(
            sp, self.config, None, packet_pages=[current, shared_stamp],
        )
        failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name == "Required form: Stamp Log"
        ]
        self.assertEqual(failures, [])

    def test_unassigned_packet_stamp_log_satisfies_current_subpackets(self):
        current = PageRecord(
            13, "", "Final Packed Product Sheet", "FPP",
            {"wo": "11582", "customer": "Trader Joe's", "product": "Apricot Slabs"},
        )
        unassigned_stamp = PageRecord(21, "", "Stamp Log", "STAMP", {})
        sp = SubPacket(
            index=0, pages=[current], primary_wo="11582",
            primary_customer="Trader Joe's", primary_product="Apricot Slabs",
        )
        run_subpacket_checks(
            sp, self.config, self.config.find_customer("Trader Joe's"),
            packet_pages=[current, unassigned_stamp],
        )
        failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name == "Required form: Stamp Log"
        ]
        self.assertEqual(failures, [])

    def test_stamp_log_for_related_wo_covers_same_customer_production_group(self):
        current_wo_page = PageRecord(
            13, "", "Final Packed Product Sheet", "FPP",
            {"wo": "11582", "customer": "Trader Joe's", "product": "Apricot Slabs"},
        )
        related_wo_page = PageRecord(
            14, "", "Final Packed Product Sheet", "FPP",
            {"wo": "11583", "customer": "Trader Joe's", "product": "Choice Apricots"},
        )
        shared_stamp = PageRecord(
            21, "", "Stamp Log", "STAMP",
            {"wo": "11582", "customer": "Trader Joe's", "product": "Apricot Slabs"},
        )
        sp = SubPacket(
            index=1, pages=[related_wo_page], primary_wo="11583",
            primary_customer="Trader Joe's", primary_product="Choice Apricots",
        )
        run_subpacket_checks(
            sp, self.config, self.config.find_customer("Trader Joe's"),
            packet_pages=[current_wo_page, related_wo_page, shared_stamp],
        )
        failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name == "Required form: Stamp Log"
        ]
        self.assertEqual(failures, [])

    def test_conflicting_stamp_log_does_not_satisfy_required_form(self):
        current = PageRecord(
            13, "", "Final Packed Product Sheet", "FPP",
            {"wo": "11582", "customer": "Current Customer", "product": "Apricot Slabs"},
        )
        conflicting_stamp = PageRecord(
            21, "", "Stamp Log", "STAMP",
            {"wo": "99999", "customer": "Other Customer", "product": "Raisins"},
        )
        sp = SubPacket(
            index=0, pages=[current], primary_wo="11582",
            primary_customer="Current Customer", primary_product="Apricot Slabs",
        )
        run_subpacket_checks(
            sp, self.config, None,
            packet_pages=[current, conflicting_stamp],
        )
        failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name == "Required form: Stamp Log"
        ]
        self.assertEqual(len(failures), 1)

    def test_multi_row_fpp_aggregate_is_not_compared_to_one_row_count(self):
        fpp = PageRecord(
            13,
            "",
            "Final Packed Product Sheet",
            "FPP",
            {
                "wo": "11582",
                "customer": "Trader Joe's",
                "cases": 15,
                "unit_lbs": 25,
                "total_lbs": 12750,
            },
        )
        sp = SubPacket(
            index=0, pages=[fpp], primary_wo="11582",
            primary_customer="Trader Joe's",
        )
        run_subpacket_checks(sp, self.config, None)
        failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name.startswith("Total weight calc")
        ]
        self.assertEqual(failures, [])
        passed = [
            check for check in sp.checks
            if check.status == "pass" and check.name.startswith("Total weight calc")
        ]
        self.assertEqual(len(passed), 1)
        self.assertIn("15 lines", passed[0].detail)
        self.assertIn("34 bags", passed[0].detail)

    def test_weight_uses_supported_row_case_count_not_wrong_page_level_number(self):
        fpp = PageRecord(
            14, "", "Final Packed Product Sheet", "FPP",
            {
                "wo": "11582",
                "customer": "Example Customer",
                "cases": 100,
                "unit_lbs": 25,
                "total_lbs": 9750,
                "all_fields": {
                    "right_table_rows": [
                        {"cases_bags": "39 Bags", "unit_size_lbs": "25", "line_total_lbs": "975"}
                    ]
                },
            },
        )
        sp = SubPacket(
            index=0, pages=[fpp], primary_wo="11582",
            primary_customer="Example Customer",
        )
        run_subpacket_checks(sp, self.config, None)
        failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name.startswith("Total weight calc")
        ]
        self.assertEqual(failures, [])

    def test_weight_mismatch_still_fails_without_supporting_case_row(self):
        fpp = PageRecord(
            14, "", "Final Packed Product Sheet", "FPP",
            {
                "wo": "11582",
                "customer": "Example Customer",
                "cases": 100,
                "unit_lbs": 25,
                "total_lbs": 9750,
                "all_fields": {"unrelated": "no case row"},
            },
        )
        sp = SubPacket(
            index=0, pages=[fpp], primary_wo="11582",
            primary_customer="Example Customer",
        )
        run_subpacket_checks(sp, self.config, None)
        failures = [
            check for check in sp.checks
            if check.status == "fail" and check.name.startswith("Total weight calc")
        ]
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()
