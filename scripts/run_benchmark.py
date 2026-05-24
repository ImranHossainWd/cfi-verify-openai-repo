from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "sqr_verifier_v2" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from verifier import verify_pdf  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run verifier benchmark packets.")
    parser.add_argument("--manifest", default=str(ROOT / "sqr_verifier_v2" / "tests" / "benchmark_packets.json"))
    parser.add_argument("--provider", default=os.environ.get("VISION_PROVIDER", "mock"))
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    packets = data.get("packets", [])
    if not packets:
        print("No benchmark packets are listed yet.")
        return 0

    base_output = Path(args.output_dir) if args.output_dir else Path(tempfile.mkdtemp(prefix="sqr-benchmark-"))
    base_output.mkdir(parents=True, exist_ok=True)
    failures = 0
    for packet in packets:
        pdf_path = Path(packet["path"]).expanduser()
        if not pdf_path.is_absolute():
            pdf_path = (ROOT / pdf_path).resolve()
        if not pdf_path.exists():
            print(f"MISS {packet['name']}: {pdf_path} not found")
            failures += 1
            continue
        out_dir = base_output / packet["name"]
        report = verify_pdf(
            pdf_path=str(pdf_path),
            out_dir=str(out_dir),
            config_dir=str(ROOT / "sqr_verifier_v2" / "config"),
            ocr_provider=args.provider,
            packet_name=packet["name"],
        )
        print(
            f"{packet['name']}: {report.overall} "
            f"({report.n_pass} pass, {report.n_fail} flags, {report.n_info} notes)"
        )
        expected_overall = packet.get("expected_overall")
        if expected_overall and report.overall != expected_overall:
            print(f"  expected {expected_overall}")
            failures += 1

    print(f"Benchmark output: {base_output}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
