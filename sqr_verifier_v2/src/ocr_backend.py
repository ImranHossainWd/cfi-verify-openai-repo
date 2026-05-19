"""
Pluggable OCR backend.

Two backends:
  1. TesseractBackend — fast, free, good on printed text, BAD on handwriting.
  2. VisionBackend    — calls a vision-capable LLM. Excellent on handwriting,
                        signatures, sticky notes, crossouts/corrections.

The verifier picks Tesseract for cheap pages (mostly printed forms),
escalates to Vision when:
  - Tesseract returned too few chars (likely handwriting-heavy page), or
  - The page has high marking density (yellow highlighter / red ink), or
  - The page is one of the form types that always has handwriting (SQR Checkoff,
    Bin Tag, Stamp Log, etc.).

Vision backend providers (production):
  - anthropic   — Claude 3.5 Sonnet / Opus / Haiku via Anthropic Messages API
  - openai      — GPT-4o / GPT-4 Vision via OpenAI API
  - google_docai — Google Document AI (purpose-built handwriting OCR)
  - mock        — reads a pre-baked JSON cache (used for offline demo runs)
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# Tesseract backend
# ---------------------------------------------------------------------------

class TesseractBackend:
    name = "tesseract"

    def __init__(self, dpi_hint: int = 150):
        self.dpi_hint = dpi_hint

    def extract(self, image_path: str) -> Dict[str, Any]:
        """Return {'raw_text': str, 'confidence_estimate': float}."""
        out_base = str(Path(tempfile.gettempdir()) / f"_tess_out_{os.getpid()}")
        error = None
        try:
            subprocess.run(
                ["tesseract", image_path, out_base, "-l", "eng"],
                check=True, timeout=20,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            text = Path(out_base + ".txt").read_text() if Path(out_base + ".txt").exists() else ""
        except Exception as e:
            error = str(e)
            text = ""
        # Confidence: cheap heuristic = chars / words ratio + letter ratio
        n_chars = len(text.strip())
        confidence = min(1.0, n_chars / 1500.0)
        return {
            "raw_text": text,
            "confidence_estimate": confidence,
            "char_count": n_chars,
            "backend": self.name,
            "error": error,
        }


# ---------------------------------------------------------------------------
# Vision backends
# ---------------------------------------------------------------------------

VISION_PROMPT = """You are reading a single scanned page of a sorting-quality packet from a dried-fruit processor (California Fruit Inc., Sanger CA). The page may be an invoice, purchase order, production request, certificate of analysis, final packed product sheet, bill of lading, sorting quality report, lab findings, sort-out form, bin tag, pull ticket, stamp log, or a defect bag photo.

The page contains BOTH printed form text AND handwritten markings. Your job is to read EVERYTHING on the page — every label, every value, every checkbox, every initial, every date, every number — printed AND handwritten — and return structured JSON.

EXHAUSTIVE-CAPTURE RULE: Beyond the schema fields listed below, you MUST also return an `all_fields` object containing every other piece of data on the page that didn't fit the schema. Treat this as: "if the page were a paper form and someone asked me to type up everything written on it, what would I type?" — every total, every percentage, every weight, every line number, every pallet number, every sample count, every column heading, every cell value, every signature, every seal number, every sensitivity setting, every time-stamp, every average, every range. **Do not silently drop data.** If a multi-column table appears (like a defect breakdown across 8 sample groups), capture every cell. If green highlighter or pink pen marks a specific column or row, ALSO note that as a structured `highlighted_regions` array — those marks usually indicate which column applies to the current order.

Pay specific attention to handwriting:
- Work-order numbers (WO#) — usually 5 digits like 11392, 11471, 11555, 11560.
- Purchase order numbers (PO#) — 4-7 digits, sometimes with a suffix like "289017-2".
- Customer name (printed or handwritten at top).
- Product description (printed code like "PEACHES-DICED-SINGLE" or handwritten name like "Single Diced Peaches" or "Jumbo Peaches").
- Dates (production dates, ship dates, inspection dates, "Date Order Completed").
- Numbers: cases, bags, weight per case, total weight, moisture %, sulfur ppm, crop year.
- Initials and signatures (any handwritten 2-3 letter codes — SA, RA, MA, JM, HS, etc.).
- Any defect-bag sticky-note text like "WO# 11560 Hair" or "WO# 11560 Pit Fragment".
- Crossouts and corrections (e.g. one number crossed out and another written next to it with initials).

Return STRICTLY this JSON shape (no markdown, no commentary):

{
  "form_type_guess": "<one of: invoice, customer_po, production_request, coa, fpp, bol, trailer_cargo, sqr_checkoff, sqr_extra_case, sqr_full, lab_findings, sort_out_findings, defect_bag_photo, container_workmanship, pretest, bin_tag, pull_ticket, sort_out_form, stamp_log, extra_cases_used, loose_metal_detector, case_metal_detector, shipping_label, unknown>",
  "form_title_text": "<the exact form title as printed at top>",
  "wo_numbers": ["<5-digit numbers found anywhere>"],
  "po_numbers": ["<PO numbers found, including suffixes>"],
  "invoice_number": "<INV004XXX if present, else null>",
  "bol_number": "<BOL00XXXX if present, else null>",
  "customer_name": "<customer name as it appears, else null>",
  "product_description": "<the product being shipped/inspected, NOT boilerplate examples, else null>",
  "product_code": "<Sage code like PEACHES-DICED-SINGLE if present, else null>",
  "dates": [{"label": "Ship Date / Production Date / Date Inspected / etc.", "value": "MM/DD/YYYY"}],
  "quantities": [{"description": "Total of 1 Case @ 25 lbs", "cases": 1, "lbs_per_case": 25, "total_lbs": 25}],
  "moisture_pct": <number or null>,
  "sulfur_ppm": <number or null>,
  "aflatoxin": "<value if present>",
  "crop_year": "<year(s) if present>",
  "initials_present": [{"location": "Verification / 2nd Verification / QC / DOC / etc.", "value": "<initials>"}],
  "checkbox_status": [{"item": "1. COVER SHEET", "checked": true}],
  "is_defect_bag_photo": <true|false>,
  "defect_bag_label": "<sticky note text e.g. 'WO# 11560 Pit Fragment' if this is a defect photo, else null>",
  "metal_detector_findings": "<FINDINGS or NO FINDINGS or null>",
  "handwritten_corrections": [{"crossed_out": "11342", "replaced_with": "11392", "initialed": true}],
  "highlighted_regions": [
    {"region": "<description e.g. 'column 5 of moisture row'>", "marker_color": "<green|yellow|pink|red>",
     "values": ["<the values in that highlighted region>"],
     "interpretation": "<why is it highlighted? e.g. 'Cleo's mark — this column is the order-specific re-test'"}
  ],
  "all_fields": {
     "<exhaustive object capturing every other data point on the page that didn't fit the schema above>": "<value>"
  },
  "notes": "<anything important you noticed that doesn't fit other fields>"
}

Return ONLY the JSON object. No prose around it."""


class AnthropicVisionBackend:
    """
    Production vision backend using Anthropic's Claude API.

    Requires:
        - `anthropic` Python package: pip install anthropic
        - ANTHROPIC_API_KEY env var
        - Internet access from the deployment environment

    Cost (as of 2026): Claude 3.5 Sonnet = ~$3 per 1M input tokens.
    A typical scanned page = ~1500 input tokens (image) + ~500 output tokens.
    Estimate: ~$0.005 per page. A 13-page packet ≈ $0.07. A 77-page packet ≈ $0.40.
    """
    name = "anthropic_vision"

    def __init__(self, model: str = "claude-sonnet-4-20250514",
                 api_key: Optional[str] = None):
        self.model = os.environ.get("ANTHROPIC_MODEL", model)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def extract(self, image_path: str) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Set the env var or pass api_key="
                "to AnthropicVisionBackend(). For offline runs, use MockVisionBackend.")
        try:
            import anthropic  # type: ignore
        except ImportError:
            raise RuntimeError("anthropic package not installed. pip install anthropic")

        client = anthropic.Anthropic(api_key=self.api_key)
        with open(image_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode("ascii")

        msg = client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }],
        )
        text = msg.content[0].text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to recover JSON from a code-fenced response
            i = text.find("{")
            j = text.rfind("}")
            data = json.loads(text[i:j+1]) if i >= 0 and j > i else {"raw_text": text}
        data["raw_text"] = data.get("raw_text", text)
        data["backend"] = self.name
        data["confidence_estimate"] = 0.95   # vision OCR is reliable
        return data


class OpenAIVisionBackend:
    """
    OpenAI vision backend using the HTTPS Responses API directly.
    Requires: OPENAI_API_KEY env var.
    """
    name = "openai_vision"

    def __init__(self, model: str = "gpt-5.5", api_key: Optional[str] = None):
        self.model = os.environ.get("OPENAI_MODEL", model)
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")

    def _responses_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI API error {exc.code}: {detail}") from exc

    @staticmethod
    def _extract_text(resp: Dict[str, Any]) -> str:
        if resp.get("output_text"):
            return str(resp["output_text"]).strip()
        chunks: List[str] = []
        for item in resp.get("output", []) or []:
            for content in item.get("content", []) or []:
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    chunks.append(str(content["text"]))
        return "\n".join(chunks).strip()

    def extract(self, image_path: str) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set.")
        with open(image_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode("ascii")
        image_url = f"data:image/png;base64,{img_b64}"
        resp = self._responses_create({
            "model": self.model,
            "input": [{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": VISION_PROMPT},
                        {"type": "input_image", "image_url": image_url},
                    ],
            }],
            "reasoning": {"effort": os.environ.get("OPENAI_REASONING_EFFORT", "none")},
            "max_output_tokens": 4096,
        })
        text = self._extract_text(resp) or json.dumps(resp)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            i = text.find("{"); j = text.rfind("}")
            data = json.loads(text[i:j+1]) if i >= 0 and j > i else {"raw_text": text}
        data["raw_text"] = data.get("raw_text", text)
        data["backend"] = self.name
        data["confidence_estimate"] = 0.95
        return data


class MockVisionBackend:
    """
    Reads from a pre-baked JSON cache file. Used for offline / sandboxed runs
    where no network is available, and for unit tests. The cache is keyed by
    image path; values are the same shape as live API responses.

    To populate the cache for a real deployment-test run:
        cache = MockVisionBackend('/path/to/cache.json')
        cache.populate_from(real_backend, ['p-01.png', 'p-02.png', ...])
    """
    name = "mock_vision"

    def __init__(self, cache_path: str):
        self.cache_path = Path(cache_path)
        if self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text())
        else:
            self.cache = {}

    def extract(self, image_path: str) -> Dict[str, Any]:
        # Try multiple lookup strategies so the cache works no matter how the
        # caller structures its rendering directory:
        #   1. <grandparent>_<filename> e.g. "olive_p-09.png"  (packet-aware)
        #   2. <parent>_<filename>      e.g. "pages_p-09.png"
        #   3. <filename>               e.g. "p-09.png"
        p = Path(image_path)
        candidates = []
        try:
            candidates.append(f"{p.parent.parent.name}_{p.name}")
        except Exception:
            pass
        candidates.append(f"{p.parent.name}_{p.name}")
        candidates.append(p.name)
        for key in candidates:
            if key in self.cache:
                d = dict(self.cache[key])
                d.setdefault("backend", self.name)
                d.setdefault("confidence_estimate", 0.95)
                d.setdefault("raw_text", "")
                return d
        return {
            "raw_text": "",
            "backend": self.name,
            "confidence_estimate": 0.0,
            "error": f"Page not in mock cache: tried {candidates}",
        }

    def populate_from(self, backend, image_paths: List[str]) -> None:
        for p in image_paths:
            key = Path(p).name
            if key in self.cache:
                continue
            self.cache[key] = backend.extract(p)
        self.cache_path.write_text(json.dumps(self.cache, indent=2))


# ---------------------------------------------------------------------------
# Hybrid orchestrator
# ---------------------------------------------------------------------------

@dataclass
class OCRConfig:
    primary_backend: str = "tesseract"
    handwriting_backend: str = "vision"
    vision_provider: str = "mock"          # anthropic / openai / google_docai / mock
    vision_cache_path: Optional[str] = None
    vision_trigger_min_chars: int = 80
    vision_trigger_marking_pct: float = 0.5
    vision_trigger_form_codes: List[str] = field(default_factory=list)


class HybridOCR:
    """
    Runs Tesseract on every page; escalates to vision OCR when handwriting
    likely matters. Returns a unified result dict.
    """
    def __init__(self, config: OCRConfig):
        self.config = config
        self.tess = TesseractBackend()
        self.vision = self._make_vision()

    def _make_vision(self):
        p = self.config.vision_provider
        if p == "anthropic":
            return AnthropicVisionBackend()
        if p == "openai":
            return OpenAIVisionBackend()
        if p == "mock":
            cp = self.config.vision_cache_path or "/tmp/vision_cache.json"
            return MockVisionBackend(cp)
        raise ValueError(f"unknown vision_provider: {p}")

    def should_escalate(self, tess_result: Dict, page_meta: Dict) -> bool:
        printed_form_codes = {"INV", "PO", "PROD_REQ", "COA", "FPP", "BOL", "SHIP_LABEL"}
        form_code = page_meta.get("form_code")
        char_count = tess_result.get("char_count", 0)
        if tess_result.get("char_count", 0) < self.config.vision_trigger_min_chars:
            return True
        if form_code in printed_form_codes:
            return False
        if (page_meta.get("yellow_pct", 0) + page_meta.get("red_pct", 0)
                > self.config.vision_trigger_marking_pct):
            return True
        if form_code in self.config.vision_trigger_form_codes:
            return True
        return False

    def extract(self, image_path: str, page_meta: Optional[Dict] = None) -> Dict[str, Any]:
        meta = page_meta or {}
        tess = self.tess.extract(image_path)
        result = {
            "tesseract": tess,
            "vision": None,
            "fields": {},
        }
        if self.should_escalate(tess, meta):
            try:
                vision = self.vision.extract(image_path)
                result["vision"] = vision
            except Exception as e:
                result["vision_error"] = str(e)
        return result
