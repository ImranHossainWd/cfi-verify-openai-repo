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

The free tier is configured in `render.yaml`. Uploaded/generated files are stored in `/tmp/sqr-verifier`, which is temporary on Render free tier.

The default OpenAI vision model is `gpt-5.4-mini`. Tesseract reads printed pages first; GPT-5.4 Mini is used only for pages that need vision OCR.

See `RENDER_DEPLOY.md` for more details.
