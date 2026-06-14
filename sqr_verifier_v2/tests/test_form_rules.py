from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from form_rules import normalize_mark, validate_structured_form, value_in_tolerance


class DailyMonthlyFormRuleTests(unittest.TestCase):
    def test_status_normalization(self):
        self.assertEqual(normalize_mark("P"), "pass")
        self.assertEqual(normalize_mark("INSP"), "inspected")
        self.assertEqual(normalize_mark("LUB"), "lubricated")
        self.assertEqual(normalize_mark("N/A"), "not_used")

    def test_used_equipment_requires_wash(self):
        checks = validate_structured_form("equipment_washdown", {
            "rows": [{"equipment": "Dicer", "used": True, "wash": ""}],
        })
        self.assertEqual(checks[0]["status"], "fail")

    def test_preop_matches_equipment_usage(self):
        related = [{"observations": {"rows": [
            {"equipment": "Sorting Line #1", "used": True},
            {"equipment": "Dicer", "used": False},
        ]}}]
        checks = validate_structured_form("preop", {
            "rows": [
                {"equipment": "Line 1", "status": "P"},
                {"equipment": "Dicer", "status": "I"},
            ],
        }, related=related)
        self.assertTrue(all(item["status"] == "pass" for item in checks))

    def test_failed_dicer_inspection_needs_action_and_count(self):
        checks = validate_structured_form("dicer_blades", {
            "equipment_used": True,
            "inspections": [
                {"result": "Pass"},
                {"result": "Fail", "corrective_action": "", "blade_count": ""},
                {"result": "Pass"},
                {"result": "Pass"},
            ],
        }, {"required_inspections": 4})
        self.assertEqual(checks[-1]["status"], "fail")

    def test_calibration_tolerance(self):
        self.assertTrue(value_in_tolerance("25.01", "24.18", "25.1"))
        self.assertFalse(value_in_tolerance("26.0", "24.18", "25.1"))

    def test_sanitizer_return_to_service(self):
        checks = validate_structured_form("backpack_sanitizer", {
            "rows": [{"date": "1/20/2026", "initials": "JA", "sanitizer_used": "yes"}],
            "idle_over_month": True,
            "cleaned_before_use": False,
        })
        self.assertEqual(checks[-1]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
