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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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
APP_VERSION = "2026-05-19-openai-gpt55"
DATA_DIR = Path(os.environ.get("SQR_DATA_DIR", ROOT / "web_data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
JOBS_FILE = DATA_DIR / "jobs.json"
CONFIG_DIR = Path(os.environ.get("SQR_CONFIG_DIR", ROOT / "config")).resolve()
VISION_CACHE = Path(os.environ.get("VISION_CACHE_PATH", ROOT / "cache" / "vision_cache.json")).resolve()
DEFAULT_VISION_PROVIDER = os.environ.get("VISION_PROVIDER", "openai").strip().lower() or "openai"
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "150"))
PROVIDERS = ["openai", "anthropic", "mock"]

for directory in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

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


def summarize_report(report: Any) -> Dict[str, Any]:
    return {
        "overall": report.overall,
        "pass_count": report.n_pass,
        "fail_count": report.n_fail,
        "info_count": report.n_info,
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
        "warnings": [],
    }


def output_files(job: Dict[str, Any]) -> List[Dict[str, str]]:
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
        {"label": label, "filename": path.name, "url": f"/jobs/{job['id']}/download/{path.name}"}
        for label, path in candidates
        if path.exists()
    ]


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
        update_job(
            job_id,
            status="complete",
            completed_at=utc_now(),
            message="Verification complete with OCR warnings" if warnings else "Verification complete",
            summary=summary,
        )
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
    jobs = sorted(load_jobs().values(), key=lambda j: j.get("created_at", ""), reverse=True)
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
    job = get_job(job_id)
    return templates.TemplateResponse(
        "job.html",
        {
            "request": request,
            "job": job,
            "files": output_files(job),
            "app_name": APP_NAME,
        },
    )


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> Dict[str, Any]:
    job = get_job(job_id)
    return {**job, "files": output_files(job)}


@app.get("/jobs/{job_id}/download/{filename}")
async def download(job_id: str, filename: str) -> FileResponse:
    job = get_job(job_id)
    out_dir = Path(job["output_dir"]).resolve()
    path = (out_dir / filename).resolve()
    if out_dir not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=filename)


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


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/diagnostics")
async def diagnostics() -> Dict[str, Any]:
    css_path = ROOT / "app" / "static" / "app.css"
    return {
        "status": "ok",
        "app_version": APP_VERSION,
        "vision_provider": DEFAULT_VISION_PROVIDER,
        "anthropic_model": ANTHROPIC_MODEL,
        "openai_model": OPENAI_MODEL,
        "anthropic_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")),
        "tesseract_path": shutil.which("tesseract"),
        "pdftoppm_path": shutil.which("pdftoppm"),
        "css_exists": css_path.exists(),
        "css_size": css_path.stat().st_size if css_path.exists() else 0,
        "data_dir": str(DATA_DIR),
        "config_dir": str(CONFIG_DIR),
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
