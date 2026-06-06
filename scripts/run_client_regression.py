from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "sqr_verifier_v2" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from verifier import Config, customer_equivalent, normalize_carrier  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


SUPPORTING_LABELS = {
    "SQR / Lab Findings", "Lab Findings", "Sort-Out Findings", "Case Metal Detector",
    "Loose Metal Detector", "Container Workmanship", "Pretest sheet", "Bin Tag",
    "Pull Ticket", "Extra Cases USED form", "Sort-Out Form", "Stamp Log", "(unidentified)",
}


def expected_reduction(check: str, detail: str, config: Config) -> bool:
    customer = re.search(r"Page customer (.+?) (?:≠|â‰ ) sub-packet primary (.+)$", detail)
    if customer:
        page_customer, primary = customer.groups()
        form = re.search(r"Customer on (.+?) \(p\d+\)", check)
        return customer_equivalent(page_customer, primary, config) or bool(form and form.group(1) in SUPPORTING_LABELS)
    carrier = re.search(r"Carrier = (.+?) on pages .*? disagrees with (.+?) on pages", detail)
    if carrier:
        left, right = carrier.groups()
        return normalize_carrier(left) == normalize_carrier(right) or not normalize_carrier(left) or not normalize_carrier(right)
    if check.startswith("Case count on "):
        if any(label in check for label in SUPPORTING_LABELS):
            return True
        if "sum of WO totals" in detail:
            return True
        if re.search(r"(?:Bill of Lading|Trailer / Cargo Inspection).*\b[12](?:\.0)? cs\b", f"{check} {detail}"):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Estimate client-run flags addressed by current conservative rules.")
    parser.add_argument(
        "--samples",
        default=str(ROOT.parent / "Clients run packets"),
        help="Folder containing client run output folders.",
    )
    args = parser.parse_args()
    samples = Path(args.samples)
    if not samples.exists():
        print(f"Client sample folder not found: {samples}")
        return 1
    config = Config.load(ROOT / "sqr_verifier_v2" / "config")
    total = reduced = 0
    for issues_path in sorted(samples.glob("*/*_issues.csv")):
        with issues_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        addressed = [row for row in rows if expected_reduction(row.get("Check", ""), row.get("Detail", ""), config)]
        total += len(rows)
        reduced += len(addressed)
        print(f"{issues_path.parent.name}: {len(addressed)}/{len(rows)} previous flags covered")
        for row in rows:
            if row not in addressed:
                print(f"  REVIEW: {row.get('Check')} - {row.get('Detail')}")
    print(f"Covered by conservative rules: {reduced}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
