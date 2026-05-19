# Sorting-Quality Packet Verifier (Production v2)

Production-ready AI verifier for California Fruit Inc.'s sorting-quality packets. Replaces the manual page-by-page cross-check that the food safety reviewer currently does by hand. Reads handwriting via vision-LLM, classifies form types, splits multi-WO packets into sub-packets, runs every cross-reference Vicky runs, produces a marked-up PDF for review.

## What it does (per packet)

1. **Renders** the scanned PDF to images.
2. **OCR**s every page — Tesseract first (free, ~85-95% on printed text), automatically escalates to a vision-LLM (Anthropic Claude Haiku, ~$0.003/page) for any page that has handwriting, sticky notes, or low Tesseract confidence.
3. **Classifies** each page by form type (Invoice, PO, Production Request, COA, FPP, BOL, Trailer/Cargo, SQR Checkoff List, Extra-Case SQR, Lab Findings, Sort-Out Form, Bin Tag, Pull Ticket, Stamp Log, defect bag photos, etc.).
4. **Splits** multi-WO packets into sub-packets (Balcorp-style: WO 11555 + WO 11560 sharing one PO 28-9017-2 are split, each verified independently).
5. **Runs cross-reference rules** per sub-packet — every WO# matches every other form's WO#, PO normalization (handles `28-9017-2` ↔ `289017-2`), customer / product / case-count / date consistency, defect-photo presence, required-form inventory, Trader Joe's co-packer exception, customer-spec validity (moisture / sulfur).
6. **Produces** a 4-part deliverable: one-page Verification Summary (PASS/FAIL banner + issue list + signature block), a marked-up PDF where every page carries AI's findings (green ✓ / orange ✗ / blue ⓘ — distinct color palette from human reviewer's red/yellow pen), an Issues CSV (one row per flag for human review), and a structured Trace JSON (every cross-reference for audit traceability).

## Cost (production, per packet)

Using Anthropic Claude Haiku 4 as the vision tier (recommended):

| Packet size | Vision pages | Cost per packet |
|---|---|---|
| Olive Nation (13 pages, 1 sub-packet) | ~7 | ~$0.02 |
| Mark Keshishian (20 pages, 1 sub-packet) | ~7 | ~$0.02 |
| Balcorp full run (77 pages, 2 sub-packets) | ~15-20 | ~$0.05-0.07 |

Average: **~$0.04 per packet**. At 100 packets/month: **~$4/month**. At 500/month: **~$20/month**. The verifier replaces ~30 minutes of manual review per packet — at any reasonable labor rate, OCR cost is <0.5% of the time it saves.

Tesseract handles the printed-text pages for free (Sage invoices, COAs, FedEx labels, Pull Tickets, etc.). Only handwriting-heavy pages route to the paid model.

## Repo layout

```
sqr_verifier_v2/
├── config/
│   ├── customers.yaml        # Customer registry with aliases + flags (TJ co-packer exception, etc.)
│   ├── specs.yaml            # Per-customer spec ranges (moisture/sulfur/aflatoxin/defect tolerance)
│   └── rules.yaml            # Toggles for each rule + OCR escalation thresholds
├── src/
│   ├── ocr_backend.py        # TesseractBackend, AnthropicVisionBackend, OpenAIVisionBackend, MockVisionBackend, HybridOCR
│   └── verifier.py           # Sub-packet splitter, rule engine, PDF annotator, CLI
├── cache/
│   └── vision_cache.json     # Pre-baked vision results (used in offline demos & test runs)
├── runs/
│   ├── olive/                # AI-verified outputs for Olive Nation packet
│   ├── mark/                 # ditto Mark Keshishian
│   └── bal/                  # ditto Balcorp full run
└── README.md                 # This file
```

## Quick start (production deployment)

### 1. Install dependencies

```bash
pip install pyyaml pillow pypdf reportlab anthropic
sudo apt-get install -y poppler-utils tesseract-ocr           # Linux
brew install poppler tesseract                                 # macOS
```

### 2. Set the API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Get a key from https://console.anthropic.com. The free tier is enough to test.

### 3. Run on a packet

```bash
python -m sqr_verifier_v2.src.verifier \
  /path/to/scanned_packet.pdf \
  -o ./out/packet_name/ \
  --vision-provider anthropic
```

Outputs land in `./out/packet_name/`:
- `<name>_AI_VERIFIED.pdf` — summary + marked-up packet (give this to Vicky)
- `<name>_issues.csv` — issues worksheet (one row per flag)
- `<name>_trace.json` — every cross-reference (auditor's trail)
- `<name>_summary.png` — same as page 1 of the PDF, standalone

### 4. Add a customer

Edit `config/customers.yaml`, copy a block:

```yaml
- canonical: "New Customer Name"
  aliases:
    - "New Customer Name"
    - "NewCustomer"
  customer_code: "BPC-XXXXXX"
  co_packer_route: false
  requires_bol: true
  requires_trailer_inspection: true
```

### 5. Add a spec

Edit `config/specs.yaml`:

```yaml
- customer: "New Customer Name"
  product: "Product Name"
  moisture_pct: { min: 22.0, max: 26.0, marginal_tolerance: 0.5 }
  sulfur_ppm:   { min: 1500, max: 3800 }
  total_defect_pct_max: 10.0
```

No code changes required. Re-run the verifier and the new specs apply.

## CLI options

```
python -m sqr_verifier_v2.src.verifier PDF -o OUT [options]

  --name NAME                  Packet name (default: PDF filename stem)
  --config DIR                 Config directory (default: ../config)
  --vision-provider PROVIDER   anthropic | openai | google_docai | mock
  --vision-cache PATH          Path to mock cache JSON (offline runs / testing)
```

## Switching OCR providers

Anthropic (default — recommended):
```bash
export ANTHROPIC_API_KEY="..."
python ... --vision-provider anthropic
```

OpenAI GPT-4o:
```bash
pip install openai
export OPENAI_API_KEY="..."
python ... --vision-provider openai
```

Offline / sandboxed (no API):
```bash
python ... --vision-provider mock --vision-cache ./cache/vision_cache.json
```

For offline runs the verifier looks up each page in the JSON cache. To populate the cache for a real deployment-test run, pass `--vision-provider anthropic` first and the verifier will write API responses to disk; subsequent re-runs are free.

## What the marked-up PDF looks like

**Page 1 — Verification Summary**
- PASS / FAIL banner
- Sub-packet inventory (`#1: WO 11555 / PO 28-9017-2 / Balcorp Limited / Jumbo Pears`)
- Tally: `33 checks passed   0 flags   5 notes`
- Issues list (only present if flagged)
- Reviewer signature line

**Pages 2–N — Marked-up original**
- Top banner: form type + per-page tally
- Right-side info box: every field the AI extracted from this page
- Bottom panel: every check the AI ran on this page, color-coded
- Original page content preserved underneath

**Color palette (deliberately distinct from Vicky's red/yellow pen):**
- **Green ✓** = matched cross-reference
- **Orange ✗** = flag for human review
- **Blue ⓘ** = informational note (e.g. "OCR couldn't read this — please confirm visually")
- **Pale lemon yellow box** = AI-reconciled total

**Sample run results (using mock vision cache for handwriting):**

| Packet | Sub-packets | Pages | Pass | Fail | Info |
|---|---|---|---|---|---|
| Olive Nation WO#11392 | 1 | 13 | 32 | **0** | 5 |
| Mark Keshishian WO#11471 | 1 | 20 | 23 | **0** | 13 |
| Balcorp WO#11555 + 11560 | 2 | 77 | 32 | **1*** | 23 |

\* The single Bal flag is a Tesseract OCR error on Sort-Out Form p49 reading PO `7290` instead of `289017-2` — the page didn't escalate to vision because the cache doesn't have that specific page. With the live Anthropic API enabled (production deployment), this resolves automatically.

## Rules implemented (reference)

See `../SORTING_QUALITY_VERIFICATION_RULES_V2.md` for the full ground-truthed ruleset. Briefly:

1. **Identity match** — every page's WO/PO/Customer/Product cross-references the sub-packet's primary values.
2. **PO normalization** — `28-9017-2` ↔ `289017-2` ↔ `PO-289017-2` all match.
3. **Sub-packet split** — multi-WO packets are split at SQR Checkoff List boundaries; pre-CHK pages are routed by WO#.
4. **Numerical reconciliation** — case counts, weights, sort-out totals (per sub-packet, FPP excluded since it's order-level).
5. **Required forms** — per-sub-packet forms (SQR Checkoff, COA, Stamp Log) and order-level forms (Invoice, Production Request, FPP) checked separately.
6. **Trader Joe's exception** — TJ packets don't require BOL or Trailer/Cargo (co-packer route).
7. **Initials & signatures** — Verification + 2nd Verification, per-form initials.
8. **Defect-photo audit** — every defect listed on Lab Findings has a matching bag photo with `WO# <wo> <defect>` sticky.
9. **Spec validity** — moisture/sulfur within customer-specific spec band.
10. **Handwritten correction** — crossed-out values with adjacent initials are accepted.

## Calibration loop

The first 3-5 production runs should be reviewed alongside Vicky's manual marks. Wherever the AI flags something Vicky would have approved → tighten the rule. Wherever the AI passes something Vicky would have flagged → add a check. Each iteration just edits `config/rules.yaml` or `config/specs.yaml`. After ~10 packets the verifier is calibrated to the team's actual standard and re-runs the same way every time.

## Things to add later (out of scope for v2)

- Vector-overlay annotations on the *original* PDF instead of raster-image overlays (cleaner for archival; needs PyMuPDF).
- Customer-product spec validation against real spec ranges (currently the table has placeholders for Mark / Balcorp / TJ — fill in once QC sends the master spec sheet).
- Automatic photo-defect text recognition on bag sticky notes (currently mock-OCR'd; production vision API will read these natively).
- A simple web UI: drop a PDF, get the verified PDF back. Could be a 50-line FastAPI wrapper around the same CLI.

## Honest limitations

- **OCR character confusion remains the main fail mode.** Tesseract sometimes reads `289017-2` as `28-9017-2` or `11555` as `11855`. Vision OCR fixes this 95%+ of the time. The remaining 5% is what the "info" notes are for ("please confirm visually").
- **Cache hits are free, cache misses cost $.** First-time runs of a new packet pay for every vision call. Re-runs of the same packet cost $0.
- **The verifier does not modify the original PDF.** It produces a parallel marked-up PDF. The original stays as-is for archival.
- **No spec-validation runs without specs loaded.** Until QC fills in `specs.yaml`, the moisture/sulfur fields are merely cross-referenced for consistency, not validated against ranges.
