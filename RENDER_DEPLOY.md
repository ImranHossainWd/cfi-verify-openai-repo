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
OPENAI_MODEL=gpt-5.4-mini
OPENAI_REASONING_EFFORT=none
FULL_PACKET_FIELD_DISCOVERY=true
SQR_DATA_DIR=/var/data/sqr-verifier
SQR_CONFIG_DIR=/var/data/sqr-verifier/config
MAX_UPLOAD_MB=150
AUTH_SESSION_DAYS=14
```

Optional login bootstrap variables:

```text
ADMIN_EMAIL=owner@example.com
ADMIN_PASSWORD=choose_a_strong_password
```

If you do not set those two variables, the first browser visit opens a one-time setup screen to create the first Admin account. After that, Admin users can add Reviewers and Viewers at `/users`.

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

Uploaded packets, generated artifacts, users, sessions, review status, and corrected-page history are stored on the Render disk at `/var/data/sqr-verifier`.

Packet field discovery: with `FULL_PACKET_FIELD_DISCOVERY=true`, GPT-5.4 Mini reads every page so each packet's unique fields can be discovered before the cross-reference matrix is built. Packet-specific values returned under `all_fields` are flattened into first-class matrix rows automatically. To reduce OpenAI usage later, set `FULL_PACKET_FIELD_DISCOVERY=false`; then printed forms are handled by Tesseract first and vision is reserved for low-text pages, high-marking pages, and handwriting-heavy form types configured in `sqr_verifier_v2/config/rules.yaml`.

Persistent storage: the Blueprint attaches a 10 GB Render disk at `/var/data/sqr-verifier`. Only files under that path survive deploys/restarts, so keep `SQR_DATA_DIR` and `SQR_CONFIG_DIR` pointed there on the Standard instance.

The same disk now also stores Food Safety Form uploads, form verification results, visual template samples, packet assignments, and workflow history. No additional Render environment variables are required for these features.

## Local Run

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m uvicorn sqr_verifier_v2.app.main:app --reload
```

On Windows, packet processing requires `tesseract.exe` and `pdftoppm.exe` on PATH. The current machine already has Tesseract, but Poppler / `pdftoppm` is not on PATH.
