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

The Standard instance is configured in `render.yaml`. Uploaded/generated files are stored in `/tmp/sqr-verifier`, which is temporary unless you later attach persistent storage.

The default OpenAI vision model is `gpt-5.4-mini`. `FULL_PACKET_FIELD_DISCOVERY=true` makes GPT-5.4 Mini read every page first, so packet-specific fields from `all_fields` are flattened into the cross-reference matrix automatically. Tesseract still runs on every page as a fallback.

See `RENDER_DEPLOY.md` for more details.
