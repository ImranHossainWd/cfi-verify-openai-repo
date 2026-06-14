from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional


EQUIPMENT_ALIASES = {
    "sorting line 1": ("sorting line 1", "line 1", "line #1"),
    "sorting line 2": ("sorting line 2", "line 2", "line #2"),
    "dicer": ("dicer",),
    "processor": ("processor",),
    "grinder tumbler": ("grinder", "tumbler", "grinder/tumbler"),
    "case metal detector": ("case metal detector", "metal detector"),
    "loose metal detector": ("loose metal detector",),
    "scale": ("scale", "laboratory scale", "production scale"),
    "backpack sanitizer": ("backpack sanitizer", "sanitizer"),
}


def normalize_mark(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "", (value or "").lower())
    if clean in {"p", "pass", "passed", "yes", "y", "ok", "x", "check", "checked"}:
        return "pass"
    if clean in {"i", "insp", "inspect", "inspected", "inspection"}:
        return "inspected"
    if clean in {"f", "fail", "failed", "no", "n"}:
        return "fail"
    if clean in {"na", "notapplicable", "notused", "unused"}:
        return "not_used"
    if clean in {"lub", "lubricated", "lubrication"}:
        return "lubricated"
    if clean in {"c", "chain", "chains"}:
        return "chains"
    return clean


def parse_numeric(value: Any) -> Optional[float]:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    return float(match.group()) if match else None


def value_in_tolerance(value: Any, minimum: Any, maximum: Any) -> Optional[bool]:
    number = parse_numeric(value)
    low = parse_numeric(minimum)
    high = parse_numeric(maximum)
    if number is None or low is None or high is None:
        return None
    return low <= number <= high


def equipment_key(label: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", " ", (label or "").lower()).strip()
    for canonical, aliases in EQUIPMENT_ALIASES.items():
        if any(alias in clean for alias in aliases):
            return canonical
    return clean


def _check(name: str, status: str, detail: str, pages: Optional[List[int]] = None) -> Dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "pages": pages or []}


PRINTED_PLACEHOLDERS = {
    "date", "initials", "time", "start time", "end time", "equipment",
    "name", "status", "result", "pass", "fail", "verified by",
    "comments", "findings", "not used", "inspection only",
}


def _meaningful(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return any(_meaningful(item) for item in value.values())
    if isinstance(value, list):
        return any(_meaningful(item) for item in value)
    clean = re.sub(r"\s+", " ", str(value).strip().lower())
    return bool(clean) and clean not in PRINTED_PLACEHOLDERS


def row_has_user_entry(row: Dict[str, Any]) -> bool:
    """True only when a row contains entered data, not printed labels/legends."""
    return any(_meaningful(value) for key, value in row.items() if key not in {
        "equipment", "name", "instrument", "label", "row_label", "column",
    })


def _dedupe_checks(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique: List[Dict[str, Any]] = []
    seen = set()
    for item in checks:
        signature = (
            item.get("name"),
            item.get("status"),
            item.get("detail"),
            tuple(item.get("pages") or []),
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(item)
    return unique


def validate_structured_form(
    rule_family: str,
    observations: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    related: Optional[Iterable[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Apply deterministic checks to vision/template observations.

    The observations format is intentionally generic so future form templates can
    supply different equipment names and columns without changing this module.
    """
    family = (rule_family or "").strip().lower()
    config = config or {}
    related = list(related or [])
    checks: List[Dict[str, Any]] = []
    rows = observations.get("rows") or observations.get("entries") or []

    if family == "equipment_washdown":
        for row in rows:
            if not row_has_user_entry(row):
                continue
            equipment = equipment_key(str(row.get("equipment") or row.get("name") or "Equipment"))
            used = bool(row.get("used"))
            wash = normalize_mark(str(row.get("wash") or row.get("daily_wash") or ""))
            sanitation = normalize_mark(str(row.get("sanitation") or row.get("full_sanitation") or ""))
            if used:
                checks.append(_check(
                    f"Daily wash: {equipment}",
                    "pass" if wash in {"pass", "inspected"} else "fail",
                    "Wash entry is present for used equipment." if wash else "Used equipment has no daily wash entry.",
                ))
            if row.get("weekly_sanitation_due"):
                checks.append(_check(
                    f"Weekly sanitation: {equipment}",
                    "pass" if sanitation in {"pass", "inspected"} else "fail",
                    "Full sanitation is recorded." if sanitation else "Weekly full sanitation is due but not recorded.",
                ))

    elif family == "preop":
        usage: Dict[str, bool] = {}
        for peer in related:
            for row in peer.get("observations", {}).get("rows", []):
                usage[equipment_key(str(row.get("equipment") or row.get("name") or ""))] = bool(row.get("used"))
        for row in rows:
            if not row_has_user_entry(row):
                continue
            equipment = equipment_key(str(row.get("equipment") or row.get("name") or "Equipment"))
            status = normalize_mark(str(row.get("status") or row.get("result") or ""))
            used = usage.get(equipment)
            if used is True:
                ok = status == "pass"
                detail = "Used equipment is marked Pass." if ok else "Equipment was used but is not marked Pass."
            elif used is False:
                ok = status in {"inspected", "not_used"}
                detail = "Unused equipment is marked inspected/not used." if ok else "Unused equipment should be marked inspected/not used, not Pass."
            else:
                ok = bool(status)
                detail = "Inspection status is recorded." if ok else "Inspection status is blank."
            checks.append(_check(f"Pre-op status: {equipment}", "pass" if ok else "fail", detail))

    elif family == "lubrication":
        valid_statuses = {"chains", "lubricated", "inspected", "not_used", "pass"}
        expected_equipment = config.get("equipment_columns") or [
            "sorting_line_1", "sorting_line_2", "dicer", "processor", "tumbler"
        ]
        entries = observations.get("entries") or []
        if entries:
            for row in entries:
                statuses = row.get("statuses") or row.get("equipment_statuses") or {}
                active = row_has_user_entry(row)
                if not active:
                    continue
                date = str(row.get("date") or "Undated row").strip()
                invalid: List[str] = []
                for equipment in expected_equipment:
                    status = normalize_mark(str(statuses.get(equipment) or ""))
                    if status not in valid_statuses:
                        invalid.append(equipment.replace("_", " "))
                checks.append(_check(
                    f"Lubrication row: {date}",
                    "pass" if not invalid else "fail",
                    (
                        "All equipment columns contain C, LUB, INSP, or N/A."
                        if not invalid
                        else f"Missing or unsupported status for: {', '.join(invalid)}."
                    ),
                ))
        else:
            # Backward-compatible handling for older generic vision output.
            for row in rows:
                if not row_has_user_entry(row):
                    continue
                equipment_raw = str(row.get("equipment") or row.get("name") or "").strip()
                status = normalize_mark(str(row.get("status") or row.get("result") or ""))
                date = str(row.get("date") or "").strip()
                if not status and not date:
                    continue
                equipment = equipment_key(equipment_raw or "Equipment")
                ok = status in valid_statuses
                checks.append(_check(
                    f"Lubrication status: {equipment}",
                    "pass" if ok else "fail",
                    "A valid C/LUB/INSP/N/A status is recorded."
                    if ok else "Lubrication status is blank or unsupported.",
                ))

    elif family == "dicer_blades":
        used = bool(observations.get("equipment_used", True))
        inspections = observations.get("inspections") or rows
        if used:
            required = int(config.get("required_inspections", 4) or 4)
            completed = [
                item for item in inspections
                if normalize_mark(str(item.get("result") or item.get("status") or ""))
                and row_has_user_entry(item)
            ]
            checks.append(_check(
                "Dicer inspection frequency",
                "pass" if len(completed) >= required else "fail",
                f"{len(completed)} inspection(s) recorded; {required} required when the dicer is used.",
            ))
            for index, item in enumerate(completed, start=1):
                if normalize_mark(str(item.get("result") or item.get("status") or "")) == "fail":
                    action = str(item.get("corrective_action") or observations.get("corrective_action") or "").strip()
                    blade_count = parse_numeric(item.get("blade_count") or observations.get("blade_count"))
                    checks.append(_check(
                        f"Failed blade inspection {index}",
                        "pass" if action and blade_count is not None else "fail",
                        "Corrective action and blade count are documented." if action and blade_count is not None
                        else "A failed inspection requires corrective action and the number of affected blades.",
                    ))

    elif family == "calibration":
        for row in rows:
            if not row_has_user_entry(row):
                continue
            instrument = str(row.get("instrument") or row.get("name") or "Instrument")
            result = value_in_tolerance(
                row.get("value") or row.get("reading"),
                row.get("minimum", config.get("minimum")),
                row.get("maximum", config.get("maximum")),
            )
            checks.append(_check(
                f"Calibration tolerance: {instrument}",
                "pass" if result is True else "fail",
                "Reading is within the configured tolerance." if result is True
                else "Reading is outside tolerance or could not be read.",
            ))

    elif family == "backpack_sanitizer":
        for row in rows:
            if not row_has_user_entry(row):
                continue
            date = str(row.get("date") or "").strip()
            initials = str(row.get("initials") or "").strip()
            sanitizer_used = normalize_mark(str(row.get("sanitizer_used") or row.get("status") or ""))
            checks.append(_check(
                f"Sanitizer entry {date or 'undated'}",
                "pass" if date and len(initials) >= 2 and sanitizer_used else "fail",
                "Date, initials, and sanitizer-use entry are present."
                if date and len(initials) >= 2 and sanitizer_used
                else "Each sanitizer entry requires a date, initials, and use mark.",
            ))
        expected_schedule = config.get("chemical_schedule") or {}
        month = str(observations.get("month") or "").strip().lower()
        chemical = re.sub(r"[^a-z0-9]+", "", str(observations.get("chemical") or "").lower())
        expected = re.sub(r"[^a-z0-9]+", "", str(expected_schedule.get(month) or "").lower())
        if expected:
            checks.append(_check(
                "Monthly sanitizer chemical",
                "pass" if chemical == expected else "fail",
                f"Expected {expected_schedule.get(month)} for {month}; recorded {observations.get('chemical') or 'blank'}.",
            ))
        if observations.get("idle_over_month") or observations.get("repaired"):
            cleaned = bool(observations.get("cleaned_before_use"))
            checks.append(_check(
                "Clean before return to service",
                "pass" if cleaned else "fail",
                "Cleaning before use is documented." if cleaned
                else "Equipment idle over one month or repaired must be cleaned before use.",
            ))

    return _dedupe_checks(checks)
