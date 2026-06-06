# California Fruit OpenAI Sorting Quality Verifier

Render-ready web app for uploading sorting-quality packet PDFs and generating:

- AI verified PDF
- Issues CSV
- Trace JSON
- Cross-reference matrix XLSX
- Summary PNG

## Deploy On Render

1. Push this folder to GitHub.
2. In Render, create a new Blueprint from the repo.
3. Add this environment variable in Render:

```text
OPENAI_API_KEY=your_openai_api_key
```

On first visit after deploy, the app will ask you to create the first Admin user. You can also pre-create it from Render environment variables:

```text
ADMIN_EMAIL=owner@example.com
ADMIN_PASSWORD=choose_a_strong_password
```

After login, Admin users can manage additional users at `/users`. Roles are `Admin`, `Reviewer`, and `Viewer`.

The Standard instance is configured in `render.yaml`. Uploaded/generated files, review status, corrected-page history, and runtime YAML settings are stored on a Render persistent disk mounted at `/var/data/sqr-verifier`.

Runtime settings are available at `/settings`. The editor validates YAML before saving and keeps timestamped backups on the persistent disk. This changes rule behavior without narrowing the AI extraction: packet-specific values returned under `all_fields` are still auto-added to the cross-reference matrix.

The default OpenAI vision model is `gpt-5.4-mini`. `FULL_PACKET_FIELD_DISCOVERY=true` makes GPT-5.4 Mini read every page first, so packet-specific fields from `all_fields` are flattened into the cross-reference matrix automatically. Tesseract still runs on every page as a fallback.

## Current Workflow Features

- Search, filter, assign, and track packet review status.
- Open, print, or replace any detected bookmarked page.
- Conservative customer/carrier normalization based on reviewed client runs.
- Separate Food Safety Forms workspace for scanned daily/monthly forms.
- Visual form-template regions plus required text, date, and signature rules.
- Client regression analysis:

```text
python scripts/run_client_regression.py --samples "../Clients run packets"
```

- Rule normalization tests:

```text
python -m unittest sqr_verifier_v2.tests.test_rule_normalization -v
```

See `RENDER_DEPLOY.md` for more details.
