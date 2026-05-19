# Render Deployment

This app is ready to deploy as a Render web service.

## What Render Uses

- `render.yaml` defines the service, Docker runtime, health check, and environment.
- `Dockerfile` installs system OCR/PDF tools reliably on Render:
  - `poppler-utils` for `pdftoppm`
  - `tesseract-ocr` / `tesseract-ocr-eng` for printed-text OCR
- `runtime.txt` pins Python 3.12.
- `requirements.txt` installs the Python app dependencies.

## Required Environment Variables

Set these in Render:

```text
VISION_PROVIDER=openai
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=gpt-5.5
OPENAI_REASONING_EFFORT=none
SQR_DATA_DIR=/tmp/sqr-verifier
MAX_UPLOAD_MB=150
```

For offline demos against the included cached pages:

```text
VISION_PROVIDER=mock
```

## Deploy Steps

1. Push this folder to a GitHub repository.
2. In Render, choose **New +** then **Blueprint**.
3. Select the repository.
4. Render will read `render.yaml` and create the web service.
5. Add `OPENAI_API_KEY` in the service environment.
6. Deploy.

On the free tier, uploaded packets and generated artifacts are stored at `/tmp/sqr-verifier`. This is ephemeral storage: files can disappear when Render restarts or redeploys the service. Free services also spin down when idle, so the first request after inactivity can be slow. That is fine for demos, but production should move to a paid Render disk, S3-compatible storage, or a database-backed artifact store.

Cost control: printed forms are handled by Tesseract first. GPT-5.5 vision OCR is reserved for low-text pages, high-marking pages, and handwriting-heavy form types configured in `sqr_verifier_v2/config/rules.yaml`.

## Local Run

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m uvicorn sqr_verifier_v2.app.main:app --reload
```

On Windows, packet processing requires `tesseract.exe` and `pdftoppm.exe` on PATH. The current machine already has Tesseract, but Poppler / `pdftoppm` is not on PATH.
