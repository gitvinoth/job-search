# job-search

Runnable **Daily Job Collector** (RSS → optional HTML fetch → **Gemini** → **Airtable**) plus a [Make.com blueprint](./MAKE_JOB_AGENT_BLUEPRINT.md). **Job role**, **location**, and **RSS URL** live in `config.json` so you can retarget searches without code changes.

Repository: [github.com/gitvinoth/job-search](https://github.com/gitvinoth/job-search)

## Prerequisites

- Python 3.10+
- [Airtable](https://airtable.com/) base with a table and [personal access token](https://airtable.com/create/tokens)
- [Google AI Studio](https://aistudio.google.com/) API key for Gemini

## Airtable table (required field names)

Create these **exact** field names (types in parentheses):

| Field | Type |
|-------|------|
| Job Link | Single line text |
| Title | Single line text |
| Search Role | Single line text |
| Search Location | Single line text |
| Summary | Long text |
| Match Score | Number (integer 0–100) |

Duplicate detection uses **Job Link** only.

## Setup

```bash
cd job-search
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
cp .env.example .env
```

Edit **`config.json`**: `job_role`, `location`, `rss_feed_url`, `resume_summary`, `airtable_base_id`, `airtable_table_name`.

### RSS feed URL (`rss_feed_url`)

The collector only reads jobs from **this XML feed**. The example value `https://rss.app/your-feed-id.xml` is **not real** — you will get **404** until you change it.

1. In [RSS.app](https://rss.app/) (or another RSS builder), create a feed that tracks your **job search** (e.g. LinkedIn search results page or another source the product supports).
2. Copy the feed’s **RSS / XML link** (often ends in `.xml` or contains `/feeds/`).
3. Paste it into **`rss_feed_url`** in `config.json`.
4. Check in a browser: you should see **RSS/XML**, not an HTML error or 404.

Edit **`.env`**: `AIRTABLE_TOKEN`, `GEMINI_API_KEY`. Optional: `GEMINI_MODEL` (default `gemini-1.5-pro`).

## Run

```bash
# One-shot (writes to Airtable)
python -m job_search --config config.json

# Plan only: dedupe + AI, no Airtable creates
python -m job_search --config config.json --dry-run

# No Gemini (RSS description only, match score 0)
python -m job_search --config config.json --no-ai

# Rate limit: 30s between job page fetches (default)
python -m job_search --sleep 30 --max 10

# Skip HTTP scrape; summarize RSS snippet only
python -m job_search --skip-scrape
```

Schedule with **cron**, **launchd**, **GitHub Actions**, or **Make.com** calling this command on your host.

## Project layout

| Path | Purpose |
|------|---------|
| [MAKE_JOB_AGENT_BLUEPRINT.md](./MAKE_JOB_AGENT_BLUEPRINT.md) | Make.com module mapping and prompts |
| [job_search/](./job_search/) | Python implementation |
| `config.example.json` | Template for `config.json` (not committed with secrets) |
| `.env.example` | Template for `.env` |

## Notes

- Many job sites block simple HTTP GETs; if summaries are empty, use `--skip-scrape` and rely on RSS text, or adjust hosting / headers (see `collector.py`).
- Keep RSS/search text under **32 words** when configuring feeds that depend on short queries (see blueprint).

## Troubleshooting

### `SSL: CERTIFICATE_VERIFY_FAILED` when fetching RSS

RSS is loaded with `requests` and [certifi](https://pypi.org/project/certifi/)’s CA bundle. If it still fails:

- **Python.org macOS installer:** run **Install Certificates.command** (in `/Applications/Python 3.x/`).
- **Corporate proxy / custom roots:** configure your system or `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` to point at your CA bundle.

### Gemini: `google.generativeai` / FutureWarning

Use **`google-genai`** only. After pulling the latest code, reinstall and remove the old package:

```bash
pip install -r requirements.txt
pip uninstall -y google-generativeai
```

This project uses `from google import genai` ([migration guide](https://ai.google.dev/gemini-api/docs/migrate)).

### `ImportError: cannot import name 'genai' from 'google'`

The legacy **`google-generativeai`** package installs `google/generativeai` and prevents the new SDK from exposing `google.genai`. Fix:

```bash
pip uninstall -y google-generativeai
pip install -r requirements.txt
python -c "from google import genai; print('ok')"
```
