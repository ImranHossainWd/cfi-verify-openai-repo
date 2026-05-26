from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import traceback
import urllib.error
import urllib.request
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import yaml
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from verifier import verify_pdf  # noqa: E402


APP_NAME = "California Fruit OpenAI Sorting Quality Verifier"
APP_VERSION = "2026-05-26-reviewed-output-files"
PREVIEW_MAX_ROWS = int(os.environ.get("PREVIEW_MAX_ROWS", "250"))
PREVIEW_MAX_COLS = int(os.environ.get("PREVIEW_MAX_COLS", "60"))
DATA_DIR = Path(os.environ.get("SQR_DATA_DIR", ROOT / "web_data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
JOBS_FILE = DATA_DIR / "jobs.json"
INSPECTIONS_FILE = DATA_DIR / "inspections.json"
BOARD_FILE = DATA_DIR / "issue_board.json"
TEMPLATES_FILE = DATA_DIR / "form_templates.json"
MONTHLY_DIR = DATA_DIR / "monthly"
BUNDLED_CONFIG_DIR = ROOT / "config"
CONFIG_DIR = Path(os.environ.get("SQR_CONFIG_DIR", DATA_DIR / "config")).resolve()
VISION_CACHE = Path(os.environ.get("VISION_CACHE_PATH", ROOT / "cache" / "vision_cache.json")).resolve()
DEFAULT_VISION_PROVIDER = os.environ.get("VISION_PROVIDER", "openai").strip().lower() or "openai"
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
FULL_PACKET_FIELD_DISCOVERY = os.environ.get("FULL_PACKET_FIELD_DISCOVERY", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "150"))
PROVIDERS = ["openai", "anthropic", "mock"]
RESOLVED_ISSUE_STATUSES = {"Accepted as Is", "False Positive", "Resolved"}
CONFIG_EDITABLE_FILES = {
    "rules": "rules.yaml",
    "customers": "customers.yaml",
    "specs": "specs.yaml",
}

for directory in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, CONFIG_DIR, MONTHLY_DIR):
    directory.mkdir(parents=True, exist_ok=True)
for bundled_file in BUNDLED_CONFIG_DIR.glob("*.yaml"):
    runtime_file = CONFIG_DIR / bundled_file.name
    if not runtime_file.exists():
        shutil.copy2(bundled_file, runtime_file)

app = FastAPI(title=APP_NAME)
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "app" / "templates")
jobs_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    stem = Path(value).stem
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip(".-")
    return cleaned[:80] or "packet"


def load_jobs() -> Dict[str, Dict[str, Any]]:
    if not JOBS_FILE.exists():
        return {}
    try:
        return json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def save_jobs(jobs: Dict[str, Dict[str, Any]]) -> None:
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(JOBS_FILE)


def get_job(job_id: str) -> Dict[str, Any]:
    jobs = load_jobs()
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def update_job(job_id: str, **changes: Any) -> Dict[str, Any]:
    with jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id, {})
        job.update(changes)
        job["updated_at"] = utc_now()
        jobs[job_id] = job
        save_jobs(jobs)
    return job


def issue_key(job_id: str, issue: Dict[str, Any]) -> str:
    return f"{job_id}:{issue.get('key') or issue.get('id')}"


def flags_only_summary(summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not summary:
        return summary
    clean = dict(summary)
    clean["info_count"] = 0
    flags = [item for item in clean.get("issues", []) if item.get("check_status") == "fail"]
    active_flags = [item for item in flags if item.get("status", "Open") not in RESOLVED_ISSUE_STATUSES]
    reviewed_flags = [item for item in flags if item.get("status", "Open") in RESOLVED_ISSUE_STATUSES]
    clean["issues"] = active_flags
    clean["reviewed_issues"] = reviewed_flags
    clean["active_fail_count"] = len(active_flags)
    clean["reviewed_fail_count"] = len(reviewed_flags)
    clean["fail_count"] = len(active_flags)
    clean["pass_count"] = int(clean.get("pass_count") or 0) + len(reviewed_flags)
    if flags and not active_flags:
        clean["overall"] = "PASS"
    clean["failures"] = [item for item in clean.get("failures", [])]
    return clean


def apply_board_review(job: Dict[str, Any]) -> Dict[str, Any]:
    board = load_json_file(BOARD_FILE, {})
    summary = job.get("summary") or {}
    for issue in summary.get("issues", []) + summary.get("reviewed_issues", []):
        state = board.get(issue_key(job["id"], issue), {})
        if state:
            issue.update({k: v for k, v in state.items() if k in {
                "status", "comment", "assignee", "due_date", "priority", "updated_at"
            }})
    job["summary"] = flags_only_summary(summary)
    return job


def public_job(job: Dict[str, Any]) -> Dict[str, Any]:
    clean = dict(job)
    clean = apply_board_review(clean)
    return clean


def audit_event(job: Dict[str, Any], action: str, detail: str, **extra: Any) -> Dict[str, Any]:
    events = list(job.get("audit_trail") or [])
    event = {
        "id": f"AUD-{len(events) + 1:04d}",
        "at": utc_now(),
        "user": "local-reviewer",
        "action": action,
        "detail": detail,
    }
    event.update(extra)
    events.append(event)
    job["audit_trail"] = events
    return job


def config_file_path(config_name: str) -> Path:
    filename = CONFIG_EDITABLE_FILES.get(config_name)
    if not filename:
        raise HTTPException(status_code=404, detail="Config file not found")
    path = (CONFIG_DIR / filename).resolve()
    if CONFIG_DIR not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="Config file not found")
    return path


def load_config_panels(selected: str = "rules", message: str = "", error: str = "") -> Dict[str, Any]:
    panels = []
    for key, filename in CONFIG_EDITABLE_FILES.items():
        path = config_file_path(key)
        panels.append(
            {
                "key": key,
                "filename": filename,
                "selected": key == selected,
                "content": path.read_text(encoding="utf-8"),
            }
        )
    return {
        "panels": panels,
        "selected": selected if selected in CONFIG_EDITABLE_FILES else "rules",
        "message": message,
        "error": error,
    }


def rules_config() -> Dict[str, Any]:
    return yaml.safe_load(config_file_path("rules").read_text(encoding="utf-8")) or {}


def save_rules_config(data: Dict[str, Any]) -> None:
    path = config_file_path("rules")
    backup = path.with_suffix(path.suffix + f".{utc_now().replace(':', '-')}.bak")
    shutil.copy2(path, backup)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def get_form_templates() -> Dict[str, Dict[str, Any]]:
    custom = load_json_file(TEMPLATES_FILE, {})
    data = rules_config()
    expected = (
        data.get("rules", {})
        .get("field_coverage_audit", {})
        .get("expected_fields_by_form", {})
        or {}
    )
    templates = {}
    for code, fields in expected.items():
        templates[code] = {
            "code": code,
            "label": custom.get(code, {}).get("label", _human_form_label(code)),
            "fields": list(fields or []),
            "active": custom.get(code, {}).get("active", True),
        }
    for code, item in custom.items():
        templates.setdefault(
            code,
            {
                "code": code,
                "label": item.get("label", _human_form_label(code)),
                "fields": item.get("fields", []),
                "active": item.get("active", True),
            },
        )
    return dict(sorted(templates.items()))


def _human_form_label(code: str) -> str:
    return code.replace("_", " ").title()


def save_form_template(code: str, label: str, fields_text: str, active: bool) -> None:
    clean_code = re.sub(r"[^A-Za-z0-9_]+", "_", code.strip().upper()).strip("_")
    if not clean_code:
        raise HTTPException(status_code=400, detail="Form code is required.")
    fields = [re.sub(r"[^A-Za-z0-9_]+", "_", item.strip().lower()).strip("_")
              for item in re.split(r"[,\n]+", fields_text)]
    fields = [item for item in fields if item]
    templates = get_form_templates()
    templates[clean_code] = {
        "code": clean_code,
        "label": label.strip() or _human_form_label(clean_code),
        "fields": fields,
        "active": active,
    }
    save_json_file(TEMPLATES_FILE, templates)
    data = rules_config()
    coverage = data.setdefault("rules", {}).setdefault("field_coverage_audit", {})
    expected = coverage.setdefault("expected_fields_by_form", {})
    expected[clean_code] = fields
    save_rules_config(data)


def issue_board_items() -> List[Dict[str, Any]]:
    board = load_json_file(BOARD_FILE, {})
    items: List[Dict[str, Any]] = []
    for job in sorted([public_job(j) for j in load_jobs().values()], key=lambda j: j.get("created_at", ""), reverse=True):
        summary = job.get("summary") or {}
        for issue in summary.get("issues", []) + summary.get("reviewed_issues", []):
            key = issue_key(job["id"], issue)
            state = board.get(key, {})
            items.append({
                "board_key": key,
                "job_id": job["id"],
                "packet_name": job.get("packet_name"),
                "created_at": job.get("created_at"),
                **issue,
                **state,
                "history": state.get("history", []),
            })
    return items


def issue_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"open": 0, "corrected": 0, "resolved": 0, "total": len(items)}
    for item in items:
        status = (item.get("status") or "Open").lower()
        if status == "corrected":
            counts["corrected"] += 1
        elif status in {"resolved", "accepted as is", "false positive"}:
            counts["resolved"] += 1
        else:
            counts["open"] += 1
    return counts


def issue_type(name: str, detail: str) -> str:
    text = f"{name} {detail}".lower()
    if "required form" in text or "present" in text and "not detected" in text:
        return "Missing Document"
    if "field coverage" in text or "not detected" in text:
        return "Missing Field"
    if "cross-page" in text or "≠" in text or "disagrees" in text:
        return "Mismatch"
    if "weight calc" in text or "case count" in text or "total" in text:
        return "Math Error"
    if "signature" in text or "initial" in text:
        return "Missing Initials/Signature"
    if "spec" in text or "outside" in text:
        return "Out of Spec"
    if "defect" in text or "photo" in text or "evidence" in text:
        return "Missing Evidence"
    if "metal" in text:
        return "Metal Detector"
    return "Review"


def render_pdf_page_to_png(pdf_path: Path, page_index: int, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        return output_path
    try:
        import fitz

        with fitz.open(str(pdf_path)) as doc:
            if page_index < 0 or page_index >= doc.page_count:
                raise HTTPException(status_code=404, detail="PDF page not found")
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
            pix.save(str(output_path))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Could not render comparison page: {exc}") from exc
    return output_path


def build_issues(report: Any, existing_reviews: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    reviews = existing_reviews or {}
    issues: List[Dict[str, Any]] = []
    for idx, check in enumerate([c for c in report.all_checks if c.status == "fail"], start=1):
        stable_key = "|".join(
            [
                check.status,
                str(None if check.sub_packet is None else check.sub_packet + 1),
                check.name,
                check.detail,
                ",".join(map(str, check.pages or [])),
            ]
        )
        review = reviews.get(stable_key, {})
        severity = "High"
        issues.append(
            {
                "id": f"ISS-{idx:03d}",
                "key": stable_key,
                "severity": severity,
                "status": review.get("status", "Open"),
                "issue_type": issue_type(check.name, check.detail),
                "check_status": check.status,
                "name": check.name,
                "detail": check.detail,
                "pages": check.pages,
                "sub_packet": None if check.sub_packet is None else check.sub_packet + 1,
                "comment": review.get("comment", ""),
                "updated_at": review.get("updated_at"),
            }
        )
    return issues


def merge_issue_reviews(summary: Dict[str, Any], reviews: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    review_map = reviews or {}
    for issue in summary.get("issues", []):
        if issue["key"] in review_map:
            issue.update({k: v for k, v in review_map[issue["key"]].items() if k in {"status", "comment", "updated_at"}})
    return summary


def summarize_report(report: Any) -> Dict[str, Any]:
    issues = build_issues(report)
    return {
        "overall": report.overall,
        "pass_count": report.n_pass,
        "fail_count": report.n_fail,
        "info_count": 0,
        "page_count": len(report.pages),
        "sub_packet_count": len(report.sub_packets),
        "sub_packets": [
            {
                "index": sp.index + 1,
                "wo": sp.primary_wo,
                "po": sp.primary_po,
                "customer": sp.primary_customer,
                "product": sp.primary_product,
                "cases": sp.cases,
                "total_lbs": sp.total_lbs,
            }
            for sp in report.sub_packets
        ],
        "failures": [
            {
                "name": c.name,
                "detail": c.detail,
                "pages": c.pages,
                "sub_packet": None if c.sub_packet is None else c.sub_packet + 1,
            }
            for c in report.all_checks
            if c.status == "fail"
        ],
        "issues": issues,
        "warnings": [],
    }


def output_files(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    out_dir = Path(job["output_dir"])
    packet_name = job["packet_name"]
    candidates = [
        ("Verified PDF", out_dir / f"{packet_name}_AI_VERIFIED.pdf"),
        ("Issues CSV", out_dir / f"{packet_name}_issues.csv"),
        ("Trace JSON", out_dir / f"{packet_name}_trace.json"),
        ("Cross-reference Matrix", out_dir / f"{packet_name}_cross_reference_matrix.xlsx"),
        ("Summary PNG", out_dir / f"{packet_name}_summary.png"),
    ]
    return [
        {
            "label": label,
            "filename": path.name,
            "url": f"/jobs/{job['id']}/download/{path.name}",
            "download_url": f"/jobs/{job['id']}/download/{path.name}",
            "preview_url": f"/jobs/{job['id']}/view/{path.name}",
            "table_preview_url": f"/jobs/{job['id']}/preview/{path.name}",
            "extension": path.suffix.lower().lstrip("."),
            "previewable": path.suffix.lower() in {".pdf", ".png", ".csv", ".json", ".xlsx"},
            "table_preview": path.suffix.lower() in {".csv", ".xlsx"},
        }
        for label, path in candidates
        if path.exists()
    ]


def output_file_path(job: Dict[str, Any], filename: str) -> Path:
    out_dir = Path(job["output_dir"]).resolve()
    path = (out_dir / filename).resolve()
    if out_dir not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return path


def page_image_path(job: Dict[str, Any], page_no: int) -> Path:
    if page_no < 1:
        raise HTTPException(status_code=404, detail="Page not found")
    out_dir = Path(job["output_dir"]).resolve()
    packet_name = job["packet_name"]
    candidates = [
        out_dir / "annotated_pages" / f"p-{page_no:02d}_annot.png",
        out_dir / "_work" / packet_name / "pages" / f"p-{page_no:02d}.png",
    ]
    for candidate in candidates:
        path = candidate.resolve()
        if out_dir in path.parents and path.exists():
            return path
    raise HTTPException(status_code=404, detail="Page image not found")


def _cell_to_preview(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return value
    return str(value)


def preview_table_file(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        rows: List[List[Any]] = []
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            for i, row in enumerate(reader):
                if i >= PREVIEW_MAX_ROWS:
                    break
                rows.append([_cell_to_preview(cell) for cell in row[:PREVIEW_MAX_COLS]])
        return {
            "filename": path.name,
            "type": "csv",
            "truncated": len(rows) >= PREVIEW_MAX_ROWS,
            "sheets": [{"name": "CSV", "rows": rows}],
        }
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        sheets = []
        for ws in wb.worksheets:
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= PREVIEW_MAX_ROWS:
                    break
                rows.append([_cell_to_preview(cell) for cell in row[:PREVIEW_MAX_COLS]])
            sheets.append(
                {
                    "name": ws.title,
                    "rows": rows,
                    "source_rows": ws.max_row,
                    "source_cols": ws.max_column,
                }
            )
        return {
            "filename": path.name,
            "type": "xlsx",
            "truncated": any(
                sheet.get("source_rows", 0) > PREVIEW_MAX_ROWS
                or sheet.get("source_cols", 0) > PREVIEW_MAX_COLS
                for sheet in sheets
            ),
            "sheets": sheets,
        }
    raise HTTPException(status_code=415, detail="Table preview is only available for CSV and XLSX files")


def write_reviewed_issues_csv(job: Dict[str, Any]) -> None:
    summary = (job.get("summary") or {})
    path = Path(job["output_dir"]) / f"{job['packet_name']}_issues.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["#", "Status", "Sub-packet", "Check", "Detail", "Pages", "Resolution (human)"])
        for idx, issue in enumerate(summary.get("issues", []), start=1):
            writer.writerow([
                idx,
                "FLAG",
                issue.get("sub_packet", ""),
                issue.get("name", ""),
                issue.get("detail", ""),
                ", ".join(map(str, issue.get("pages") or [])),
                issue.get("comment", ""),
            ])


def write_reviewed_summary_png(job: Dict[str, Any]) -> Optional[Path]:
    summary = job.get("summary") or {}
    out_path = Path(job["output_dir"]) / f"{job['packet_name']}_summary.png"
    try:
        from PIL import Image, ImageDraw, ImageFont

        im = Image.new("RGB", (2100, 2700), "white")
        draw = ImageDraw.Draw(im)
        try:
            big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
            h1 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
            bd = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
            rg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        except Exception:
            big = h1 = bd = rg = ImageFont.load_default()

        overall = summary.get("overall", "PASS")
        banner = (200, 240, 200) if overall == "PASS" else (255, 210, 200)
        draw.rectangle([(0, 0), (2100, 200)], fill=banner)
        draw.text((40, 30), f"AI VERIFICATION - {overall}", fill=(0, 0, 0), font=big)
        draw.text((40, 110), f"Packet: {job['packet_name']}", fill=(0, 0, 0), font=h1)

        y = 250
        rows = [
            ("Sub-packets:", str(summary.get("sub_packet_count", 0))),
            ("Customer:", (summary.get("sub_packets") or [{}])[0].get("customer", "(unknown)") if summary.get("sub_packets") else "(unknown)"),
            ("Total pages:", str(summary.get("page_count", ""))),
            ("Verified at:", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ]
        for key, value in rows:
            draw.text((60, y), key, fill=(0, 0, 0), font=bd)
            draw.text((360, y), str(value), fill=(0, 0, 0), font=rg)
            y += 50

        y += 30
        draw.text((60, y), "Result tally:", fill=(0, 0, 0), font=h1)
        y += 60
        draw.text((80, y), f"{summary.get('pass_count', 0)} checks passed", fill=(40, 140, 60), font=bd)
        y += 50
        draw.text((80, y), f"{summary.get('fail_count', 0)} flag(s) raised", fill=(235, 130, 25), font=bd)
        y += 70

        issues = summary.get("issues") or []
        if issues:
            draw.text((60, y), "Issues to review:", fill=(235, 130, 25), font=h1)
            y += 60
            for idx, issue in enumerate(issues, start=1):
                sp_tag = f" [sub-packet {issue.get('sub_packet')}]" if issue.get("sub_packet") else ""
                line = f"{idx}. {issue.get('name', '')}{sp_tag} - {issue.get('detail', '')}"
                if len(line) > 160:
                    line = line[:157] + "..."
                draw.text((80, y), line, fill=(0, 0, 0), font=rg)
                y += 36
                if y > 2460:
                    break
        else:
            draw.text((60, y), "No issues raised. Packet is accepted after review.", fill=(40, 140, 60), font=h1)

        sig_y = 2420
        draw.text((60, sig_y), "Reviewer signature: _____________________________   Date: ___________", fill=(0, 0, 0), font=bd)
        draw.text((60, sig_y + 60), "Accepted/false-positive/resolved flags are counted as passed by reviewer.", fill=(80, 80, 80), font=rg)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        im.save(out_path)
        return out_path
    except Exception as exc:  # noqa: BLE001
        print(f"Reviewed summary image skipped: {exc}")
        return None


def replace_pdf_summary_page(job: Dict[str, Any], summary_png: Path) -> None:
    pdf_path = Path(job["output_dir"]) / f"{job['packet_name']}_AI_VERIFIED.pdf"
    if not pdf_path.exists() or not summary_png.exists():
        return
    try:
        from PIL import Image
        from pypdf import PdfReader, PdfWriter

        summary_pdf = summary_png.with_suffix(".reviewed-summary.pdf")
        Image.open(summary_png).convert("RGB").save(summary_pdf)
        summary_reader = PdfReader(str(summary_pdf))
        original_reader = PdfReader(str(pdf_path))
        writer = PdfWriter()
        writer.add_page(summary_reader.pages[0])
        for page in list(original_reader.pages)[1:]:
            writer.add_page(page)
        tmp = pdf_path.with_suffix(".reviewed.tmp.pdf")
        with tmp.open("wb") as f:
            writer.write(f)
        tmp.replace(pdf_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Reviewed PDF summary replacement skipped: {exc}")


def refresh_reviewed_output_files(raw_job: Dict[str, Any]) -> None:
    if raw_job.get("status") != "complete" or not raw_job.get("summary"):
        return
    job = public_job(raw_job)
    write_reviewed_issues_csv(job)
    summary_png = write_reviewed_summary_png(job)
    if summary_png:
        replace_pdf_summary_page(job, summary_png)


def read_trace(job: Dict[str, Any]) -> Dict[str, Any]:
    trace_path = Path(job["output_dir"]) / f"{job['packet_name']}_trace.json"
    if not trace_path.exists():
        return {}
    return load_json_file(trace_path, {})


def field_confidence_rows(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    trace = read_trace(job)
    rows: List[Dict[str, Any]] = []
    for page in trace.get("pages", []) or []:
        fields = page.get("fields", {}) or {}
        confidence = float(page.get("confidence_estimate") or fields.get("_page_confidence") or 0)
        field_conf = fields.get("_field_confidence") or {}
        for key, value in fields.items():
            if key.startswith("_") or value in (None, "", [], {}):
                continue
            if isinstance(value, (list, dict)):
                continue
            rows.append({
                "page_no": page.get("page_no"),
                "form": page.get("form_label") or page.get("form_code"),
                "field": key,
                "value": value,
                "confidence": round(float(field_conf.get(key, confidence) or confidence), 3),
                "backend": page.get("ocr_backend_used"),
            })
    return rows


def write_audit_workbook(path: Path, month: str, jobs: List[Dict[str, Any]], inspections: List[Dict[str, Any]]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Packet Summary"
    headers = ["Packet", "Uploaded", "Status", "Overall", "Flags", "Open flags", "Signed by", "Decision"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", start_color="FFEAEAEA")
    for job in jobs:
        summary = job.get("summary") or {}
        issues = summary.get("issues") or []
        open_flags = sum(1 for item in issues if (item.get("status") or "Open") == "Open")
        signoff = job.get("review_signoff") or {}
        ws.append([
            job.get("packet_name"),
            job.get("created_at"),
            job.get("status"),
            summary.get("overall", ""),
            summary.get("fail_count", 0),
            open_flags,
            signoff.get("reviewer_name", ""),
            signoff.get("decision", ""),
        ])
    ws2 = wb.create_sheet("Live Inspections")
    ws2.append(["Date", "Type", "Inspector", "Area", "Result", "Corrective action"])
    for cell in ws2[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", start_color="FFEAEAEA")
    for item in inspections:
        ws2.append([
            item.get("date"),
            item.get("inspection_type"),
            item.get("inspector"),
            item.get("area"),
            item.get("result"),
            item.get("corrective_action"),
        ])
    ws3 = wb.create_sheet("SQF Open Items")
    ws3.append(["Packet", "Flag", "Assignee", "Due date", "Status", "Detail"])
    for cell in ws3[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", start_color="FFEAEAEA")
    for item in issue_board_items():
        if (item.get("status") or "Open") in {"Resolved", "Accepted as Is", "False Positive"}:
            continue
        ws3.append([
            item.get("packet_name"),
            item.get("name"),
            item.get("assignee", ""),
            item.get("due_date", ""),
            item.get("status", "Open"),
            item.get("detail"),
        ])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def run_verification(job_id: str) -> None:
    job = update_job(job_id, status="running", started_at=utc_now(), message="Rendering and OCR are in progress")
    try:
        provider = job["vision_provider"]
        if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set in the Render environment.")
        if provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set in the Render environment.")
        cache_path: Optional[str] = str(VISION_CACHE) if provider == "mock" and VISION_CACHE.exists() else None
        report = verify_pdf(
            pdf_path=job["input_path"],
            out_dir=job["output_dir"],
            config_dir=str(CONFIG_DIR),
            ocr_provider=provider,
            vision_cache_path=cache_path,
            packet_name=job["packet_name"],
        )
        vision_errors = [
            note
            for page in report.pages
            for note in page.notes
            if note.startswith("Vision OCR error:")
        ]
        n_vision = sum(1 for page in report.pages if page.ocr_backend_used == "vision")
        summary = summarize_report(report)
        summary = merge_issue_reviews(summary, job.get("issue_reviews"))
        warnings = []
        if provider in {"anthropic", "openai"} and vision_errors:
            warnings.append(
                "Vision OCR failed on one or more pages. The report was generated "
                "from Tesseract/printed-text OCR where possible. First vision error: "
                + vision_errors[0]
            )
        if provider in {"anthropic", "openai"} and n_vision == 0:
            warnings.append(
                f"{provider} vision OCR did not process any pages. This is a partial "
                "Tesseract-only report and may miss handwriting."
            )
        summary["warnings"] = warnings
        final_job = update_job(
            job_id,
            status="complete",
            completed_at=utc_now(),
            message="Verification complete with OCR warnings" if warnings else "Verification complete",
            summary=summary,
        )
        refresh_reviewed_output_files(final_job)
    except Exception as exc:  # noqa: BLE001 - show useful operator error on job page
        update_job(
            job_id,
            status="failed",
            completed_at=utc_now(),
            message=str(exc),
            error_trace=traceback.format_exc(),
        )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    jobs = sorted([public_job(j) for j in load_jobs().values()], key=lambda j: j.get("created_at", ""), reverse=True)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "jobs": jobs,
            "app_name": APP_NAME,
            "default_provider": DEFAULT_VISION_PROVIDER,
            "providers": PROVIDERS,
            "max_upload_mb": MAX_UPLOAD_MB,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request, selected: str = "rules") -> HTMLResponse:
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "app_name": APP_NAME,
            **load_config_panels(selected=selected),
        },
    )


@app.get("/issues", response_class=HTMLResponse)
async def issue_board(request: Request) -> HTMLResponse:
    items = issue_board_items()
    return templates.TemplateResponse(
        "issues.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "issues": items,
            "counts": issue_counts(items),
        },
    )


@app.post("/issues/update")
async def update_issue_board(
    board_key: str = Form(...),
    status: str = Form("Open"),
    assignee: str = Form(""),
    due_date: str = Form(""),
    priority: str = Form("Normal"),
    comment: str = Form(""),
) -> RedirectResponse:
    allowed = {"Open", "Corrected", "Accepted as Is", "False Positive", "Resolved"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported issue status.")
    board = load_json_file(BOARD_FILE, {})
    current = board.get(board_key, {})
    history = list(current.get("history") or [])
    history.append({
        "at": utc_now(),
        "status": status,
        "assignee": assignee.strip(),
        "due_date": due_date.strip(),
        "priority": priority.strip(),
        "comment": comment.strip(),
    })
    board[board_key] = {
        "status": status,
        "assignee": assignee.strip(),
        "due_date": due_date.strip(),
        "priority": priority.strip() or "Normal",
        "comment": comment.strip(),
        "updated_at": utc_now(),
        "history": history[-25:],
    }
    save_json_file(BOARD_FILE, board)
    return RedirectResponse(url="/issues", status_code=303)


@app.get("/inspections", response_class=HTMLResponse)
async def inspections(request: Request) -> HTMLResponse:
    items = sorted(load_json_file(INSPECTIONS_FILE, []), key=lambda x: x.get("created_at", ""), reverse=True)
    return templates.TemplateResponse(
        "inspections.html",
        {"request": request, "app_name": APP_NAME, "inspections": items},
    )


@app.post("/inspections")
async def create_inspection(
    inspection_type: str = Form(...),
    inspector: str = Form(""),
    area: str = Form(""),
    date: str = Form(""),
    result: str = Form("Pass"),
    observations: str = Form(""),
    corrective_action: str = Form(""),
) -> RedirectResponse:
    items = load_json_file(INSPECTIONS_FILE, [])
    items.append({
        "id": uuid4().hex[:10],
        "created_at": utc_now(),
        "inspection_type": inspection_type.strip(),
        "inspector": inspector.strip(),
        "area": area.strip(),
        "date": date.strip(),
        "result": result,
        "observations": observations.strip(),
        "corrective_action": corrective_action.strip(),
    })
    save_json_file(INSPECTIONS_FILE, items)
    return RedirectResponse(url="/inspections", status_code=303)


@app.get("/templates", response_class=HTMLResponse)
async def form_templates(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "templates.html",
        {"request": request, "app_name": APP_NAME, "templates": get_form_templates().values()},
    )


@app.post("/templates")
async def save_template(
    code: str = Form(...),
    label: str = Form(""),
    fields: str = Form(""),
    active: str = Form("true"),
) -> RedirectResponse:
    save_form_template(code, label, fields, active == "true")
    return RedirectResponse(url="/templates", status_code=303)


@app.get("/audit", response_class=HTMLResponse)
async def audit_prep(request: Request) -> HTMLResponse:
    jobs = sorted([public_job(j) for j in load_jobs().values()], key=lambda j: j.get("created_at", ""), reverse=True)
    inspections_data = sorted(load_json_file(INSPECTIONS_FILE, []), key=lambda x: x.get("created_at", ""), reverse=True)
    return templates.TemplateResponse(
        "audit.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "jobs": jobs[:100],
            "inspections": inspections_data[:100],
            "open_issues": issue_counts(issue_board_items())["open"],
        },
    )


@app.post("/audit/generate")
async def generate_audit(month: str = Form("")) -> RedirectResponse:
    month_key = month.strip() or datetime.now(timezone.utc).strftime("%Y-%m")
    jobs = [public_job(j) for j in load_jobs().values() if str(j.get("created_at", "")).startswith(month_key)]
    inspections_data = [i for i in load_json_file(INSPECTIONS_FILE, []) if str(i.get("date") or i.get("created_at", "")).startswith(month_key)]
    out = MONTHLY_DIR / f"california-fruit-audit-prep-{slugify(month_key)}.xlsx"
    write_audit_workbook(out, month_key, jobs, inspections_data)
    return RedirectResponse(url=f"/audit/download/{out.name}", status_code=303)


@app.get("/audit/download/{filename}")
async def download_audit(filename: str) -> FileResponse:
    path = (MONTHLY_DIR / filename).resolve()
    if MONTHLY_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="Audit file not found")
    return FileResponse(path, filename=path.name)


@app.post("/settings/{config_name}", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    config_name: str,
    content: str = Form(...),
) -> HTMLResponse:
    path = config_file_path(config_name)
    try:
        yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        return templates.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "app_name": APP_NAME,
                **load_config_panels(
                    selected=config_name,
                    error=f"{path.name} was not saved because the YAML is invalid: {exc}",
                ),
            },
            status_code=400,
        )
    backup = path.with_suffix(path.suffix + f".{utc_now().replace(':', '-')}.bak")
    shutil.copy2(path, backup)
    path.write_text(content, encoding="utf-8")
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "app_name": APP_NAME,
            **load_config_panels(selected=config_name, message=f"Saved {path.name}; backup created as {backup.name}."),
        },
    )


@app.post("/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    pdf: UploadFile = File(...),
    packet_name: str = Form(""),
    vision_provider: str = Form(DEFAULT_VISION_PROVIDER),
) -> RedirectResponse:
    if not pdf.filename or not pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload a PDF packet.")
    provider = vision_provider.strip().lower()
    if provider not in {"mock", "anthropic", "openai"}:
        raise HTTPException(status_code=400, detail="Unsupported vision provider.")

    job_id = uuid4().hex[:12]
    safe_name = slugify(packet_name or pdf.filename)
    input_path = UPLOAD_DIR / f"{job_id}_{slugify(pdf.filename)}.pdf"
    output_dir = OUTPUT_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    size = 0
    with input_path.open("wb") as out:
        while chunk := await pdf.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                input_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"PDF exceeds {MAX_UPLOAD_MB} MB.")
            out.write(chunk)

    job = {
        "id": job_id,
        "packet_name": safe_name,
        "original_filename": pdf.filename,
        "input_path": str(input_path),
        "output_dir": str(output_dir),
        "vision_provider": provider,
        "status": "queued",
        "message": "Queued for verification",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "summary": None,
    }
    update_job(job_id, **job)
    background_tasks.add_task(run_verification, job_id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: str) -> HTMLResponse:
    raw_job = get_job(job_id)
    refresh_reviewed_output_files(raw_job)
    job = public_job(raw_job)
    return templates.TemplateResponse(
        "job.html",
        {
            "request": request,
            "job": job,
            "files": output_files(job),
            "app_name": APP_NAME,
        },
    )


@app.get("/jobs/{job_id}/fields", response_class=HTMLResponse)
async def job_fields(request: Request, job_id: str) -> HTMLResponse:
    job = public_job(get_job(job_id))
    return templates.TemplateResponse(
        "fields.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "job": job,
            "fields": field_confidence_rows(job),
        },
    )


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> Dict[str, Any]:
    job = public_job(get_job(job_id))
    return {**job, "files": output_files(job)}


@app.post("/jobs/{job_id}/issues/{issue_id}")
async def update_issue_review(
    job_id: str,
    issue_id: str,
    status: str = Form(...),
    comment: str = Form(""),
) -> RedirectResponse:
    allowed = {"Open", "Corrected", "Accepted as Is", "False Positive", "Resolved"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported issue status.")
    with jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        summary = job.get("summary") or {}
        issue = next((item for item in summary.get("issues", []) if item.get("id") == issue_id), None)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")
        reviews = dict(job.get("issue_reviews") or {})
        reviews[issue["key"]] = {
            "status": status,
            "comment": comment.strip(),
            "updated_at": utc_now(),
        }
        job["issue_reviews"] = reviews
        issue.update(reviews[issue["key"]])
        audit_event(
            job,
            "issue_review",
            f"{issue_id} marked {status}",
            issue_id=issue_id,
            issue_type=issue.get("issue_type"),
            pages=issue.get("pages", []),
            comment=comment.strip(),
        )
        job["updated_at"] = utc_now()
        jobs[job_id] = job
        save_jobs(jobs)
    refresh_reviewed_output_files(get_job(job_id))
    return RedirectResponse(url=f"/jobs/{job_id}#review-queue", status_code=303)


@app.post("/jobs/{job_id}/signoff")
async def save_signoff(
    job_id: str,
    reviewer_name: str = Form(...),
    decision: str = Form(...),
    comments: str = Form(""),
) -> RedirectResponse:
    allowed = {"Approved", "Approved with exceptions", "Rejected", "Needs correction"}
    if decision not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported sign-off decision.")
    with jobs_lock:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        signoff = {
            "reviewer_name": reviewer_name.strip(),
            "decision": decision,
            "comments": comments.strip(),
            "signed_at": utc_now(),
        }
        job["review_signoff"] = signoff
        audit_event(
            job,
            "review_signoff",
            f"{reviewer_name.strip()} signed: {decision}",
            decision=decision,
            comments=comments.strip(),
        )
        job["updated_at"] = utc_now()
        jobs[job_id] = job
        save_jobs(jobs)
    return RedirectResponse(url=f"/jobs/{job_id}#review-signoff", status_code=303)


@app.get("/jobs/{job_id}/download/{filename}")
async def download(job_id: str, filename: str) -> FileResponse:
    job = get_job(job_id)
    path = output_file_path(job, filename)
    return FileResponse(path, filename=filename)


@app.get("/jobs/{job_id}/view/{filename}")
async def view_file(job_id: str, filename: str) -> FileResponse:
    job = get_job(job_id)
    path = output_file_path(job, filename)
    media_types = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".csv": "text/csv; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    return FileResponse(
        path,
        media_type=media_types.get(path.suffix.lower(), "application/octet-stream"),
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


@app.get("/jobs/{job_id}/preview/{filename}")
async def preview_file(job_id: str, filename: str) -> JSONResponse:
    job = get_job(job_id)
    path = output_file_path(job, filename)
    return JSONResponse(preview_table_file(path))


@app.get("/jobs/{job_id}/page/{page_no}.png")
async def page_image(job_id: str, page_no: int) -> FileResponse:
    job = get_job(job_id)
    path = page_image_path(job, page_no)
    return FileResponse(path, media_type="image/png")


@app.get("/jobs/{job_id}/compare/{event_id}/{side}.png")
async def compare_page(job_id: str, event_id: str, side: str) -> FileResponse:
    if side not in {"before", "after"}:
        raise HTTPException(status_code=404, detail="Comparison side not found")
    job = get_job(job_id)
    event = next((item for item in job.get("audit_trail") or [] if item.get("id") == event_id), None)
    if not event or event.get("action") != "page_replaced":
        raise HTTPException(status_code=404, detail="Replacement event not found")
    page_no = int(event.get("page_no") or 1)
    out_dir = Path(job["output_dir"]).resolve()
    compare_dir = out_dir / "versions" / "compare" / event_id
    if side == "before":
        pdf_path = Path(event.get("backup_pdf", "")).resolve()
        page_index = page_no - 1
    else:
        pdf_path = Path(event.get("replacement_pdf", "")).resolve()
        page_index = 0
    versions_dir = (out_dir / "versions").resolve()
    if versions_dir not in pdf_path.parents or not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Comparison PDF not found")
    image_path = render_pdf_page_to_png(pdf_path, page_index, compare_dir / f"{side}.png")
    return FileResponse(image_path, media_type="image/png")


@app.post("/jobs/{job_id}/rerun")
async def rerun_job(background_tasks: BackgroundTasks, job_id: str) -> RedirectResponse:
    job = get_job(job_id)
    out_dir = Path(job["output_dir"])
    if out_dir.exists():
        shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
    update_job(job_id, status="queued", message="Queued for re-run", summary=None)
    background_tasks.add_task(run_verification, job_id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/replace-page")
async def replace_page(
    background_tasks: BackgroundTasks,
    job_id: str,
    page_no: int = Form(...),
    replacement_pdf: UploadFile = File(...),
) -> RedirectResponse:
    if page_no < 1:
        raise HTTPException(status_code=400, detail="Page number must be 1 or greater.")
    if not replacement_pdf.filename or not replacement_pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload a PDF replacement page.")
    job = get_job(job_id)
    out_dir = Path(job["output_dir"])
    versions_dir = out_dir / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "-")
    replacement_path = versions_dir / f"{stamp}_replacement_p{page_no:02d}_{slugify(replacement_pdf.filename)}.pdf"

    size = 0
    with replacement_path.open("wb") as out:
        while chunk := await replacement_pdf.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                replacement_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"Replacement PDF exceeds {MAX_UPLOAD_MB} MB.")
            out.write(chunk)

    try:
        from pypdf import PdfReader, PdfWriter

        source = PdfReader(job["input_path"])
        replacement = PdfReader(str(replacement_path))
        if page_no > len(source.pages):
            raise HTTPException(status_code=400, detail=f"Packet only has {len(source.pages)} pages.")
        if len(replacement.pages) < 1:
            raise HTTPException(status_code=400, detail="Replacement PDF has no pages.")
        previous_input = Path(job["input_path"])
        backup_input = versions_dir / f"{stamp}_before_replace_p{page_no:02d}_{previous_input.name}"
        shutil.copy2(previous_input, backup_input)
        writer = PdfWriter()
        for i, page in enumerate(source.pages, start=1):
            writer.add_page(replacement.pages[0] if i == page_no else page)
        new_input = UPLOAD_DIR / f"{job_id}_{job['packet_name']}_page-{page_no:02d}-replaced.pdf"
        with new_input.open("wb") as f:
            writer.write(f)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not replace page: {exc}") from exc

    for child in list(out_dir.iterdir()):
        if child.name == "versions":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)

    job = update_job(
        job_id,
        input_path=str(new_input),
        status="queued",
        message=f"Page {page_no} replaced; queued for re-run",
        summary=None,
    )
    audit_event(
        job,
        "page_replaced",
        f"Page {page_no} replaced and queued for verification",
        page_no=page_no,
        backup_pdf=str(backup_input),
        replacement_pdf=str(replacement_path),
        new_packet_pdf=str(new_input),
    )
    update_job(job_id, audit_trail=job.get("audit_trail"))
    background_tasks.add_task(run_verification, job_id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok", "app_version": APP_VERSION}


@app.get("/diagnostics")
async def diagnostics() -> Dict[str, Any]:
    css_path = ROOT / "app" / "static" / "app.css"
    return {
        "status": "ok",
        "app_version": APP_VERSION,
        "vision_provider": DEFAULT_VISION_PROVIDER,
        "anthropic_model": ANTHROPIC_MODEL,
        "openai_model": OPENAI_MODEL,
        "full_packet_field_discovery": FULL_PACKET_FIELD_DISCOVERY,
        "preview_max_rows": PREVIEW_MAX_ROWS,
        "preview_max_cols": PREVIEW_MAX_COLS,
        "anthropic_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")),
        "tesseract_path": shutil.which("tesseract"),
        "pdftoppm_path": shutil.which("pdftoppm"),
        "css_exists": css_path.exists(),
        "css_size": css_path.stat().st_size if css_path.exists() else 0,
        "data_dir": str(DATA_DIR),
        "config_dir": str(CONFIG_DIR),
        "bundled_config_dir": str(BUNDLED_CONFIG_DIR),
    }


@app.get("/diagnostics/anthropic")
async def diagnostics_anthropic() -> Dict[str, Any]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not set")
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with exactly: ok"}],
        )
        return {
            "status": "ok",
            "model": ANTHROPIC_MODEL,
            "response": msg.content[0].text,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/diagnostics/openai")
async def diagnostics_openai() -> Dict[str, Any]:
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set")
    try:
        payload = {
            "model": OPENAI_MODEL,
            "input": "Reply with exactly: ok",
            "reasoning": {"effort": os.environ.get("OPENAI_REASONING_EFFORT", "none")},
            "max_output_tokens": 16,
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data.get("output_text") or ""
        if not text:
            chunks = []
            for item in data.get("output", []) or []:
                for content in item.get("content", []) or []:
                    if content.get("text"):
                        chunks.append(str(content["text"]))
            text = "\n".join(chunks)
        return {
            "status": "ok",
            "model": OPENAI_MODEL,
            "response": text.strip(),
        }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=500, detail=f"OpenAI API error {exc.code}: {detail}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
