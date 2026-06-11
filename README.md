# Job Search Dashboard

A self-hosted **live job aggregator dashboard** — pulls from 6+ portals (Talent500, RemoteOK, We Work Remotely, NoDesk, JSRemotely, and your custom RSS feed), scores jobs against your resume using local heuristic matching, and lets you filter by location, date, score, and visited status.

Repository: [github.com/gitvinoth/job-search](https://github.com/gitvinoth/job-search)

---

## Quick Start — Run on Any Machine

### 1. Prerequisites

- **Python 3.10+** — verify with `python3 --version`
- **Git**
- No API keys required for the web dashboard (scoring is fully local)

### 2. Clone & install

```bash
git clone https://github.com/gitvinoth/job-search.git
cd job-search

# Create virtual environment
python3 -m venv .venv

# Activate
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows PowerShell

# Install dependencies
pip install -r requirements.txt
```

### 3. Create your config

```bash
cp config.example.json config.json
```

Edit `config.json` with your details:

```json
{
  "job_role": "Senior Data Engineer, Staff Data Engineer",
  "location": "Remote India",
  "rss_feed_url": "https://rss.app/feeds/YOUR-FEED-ID.xml",
  "resume_summary": "3–5 sentence summary of your skills and target role",
  "airtable_base_id": "appXXXXXXXXXXXXXX",
  "airtable_table_name": "Jobs"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `job_role` | ✅ | Comma-separated job titles you're targeting |
| `location` | ✅ | Preferred location (e.g. `Remote India`, `Bangalore`, `Remote`) |
| `resume_summary` | ✅ | Short bio of your skills — used for match scoring |
| `rss_feed_url` | Optional | RSS.app or any RSS feed URL for an extra portal |
| `airtable_*` | Optional | Only needed for the CLI Airtable export feature |

### 4. Launch the dashboard

```bash
python -m job_search.web --config config.json
```

Open **http://localhost:5000** in your browser. If port 5000 is busy, the app auto-selects a free port — check your terminal for the URL.

---

## Dashboard Features

| Feature | Details |
|---------|---------|
| **Multi-portal tabs** | Talent500, RemoteOK, We Work Remotely, NoDesk, JSRemotely, your RSS feed |
| **Location filter** | Multi-select dropdown — pick Bangalore, Remote, London, Germany etc. simultaneously |
| **Date filter** | Last 1 min / 1 hour / 24 hours / 1 week / 2 weeks / 1 month |
| **Score filter** | Show 20+, 40+, 60%+ matched jobs only |
| **Visited tracking** | Click Apply → job marked visited (browser localStorage, per device) |
| **Settings panel** | Change job role, location, resume on the fly — re-scores everything immediately |
| **Search box** | Filter by title, description, source, or location keyword |

---

## For Contributors — Running a Different Config

Each contributor runs their **own `config.json`** with their own job role, location and resume. The file is tracked in git (no secrets), so you can commit your config or keep it local:

```bash
# Each person does this once after cloning:
cp config.example.json config.json
# Then edit config.json with YOUR role, location, resume
```

`config.json` contains no API keys. API keys (Airtable, Gemini, Claude) go in `.env` only:

```bash
cp .env.example .env
# Edit .env — add AIRTABLE_TOKEN if you want Airtable export
```

---

## Adding Your Own RSS Feed

1. Go to [rss.app](https://rss.app) → Create feed → paste a job search URL (LinkedIn, Indeed, etc.)
2. Copy the generated RSS URL
3. Paste it into `config.json` as `rss_feed_url`
4. Refresh the dashboard — it appears as the **User Feed** tab

---

## Configuring Portals (`portals.json`)

Portals are defined in `portals.json`. Enable/disable any portal:

```json
{ "name": "NoDesk", "type": "rss", "url": "https://nodesk.co/remote-jobs/index.xml", "enabled": true }
```

Portals that need RSS.app setup (LinkedIn, Indeed, Glassdoor, Wellfound) have a `_setup` field with instructions. Set the `url` field to your RSS.app feed URL, then set `"enabled": true`.

---

## Project Structure

```
job-search/
├── config.json           ← Your personal settings (edit this, safe to commit)
├── config.example.json   ← Template — copy to config.json
├── portals.json          ← Job portal definitions
├── requirements.txt      ← Python dependencies
├── .env                  ← API keys (never commit this)
├── .env.example          ← Template for .env
└── job_search/
    ├── web.py            ← Flask dashboard (main entry point)
    ├── portals.py        ← Portal scrapers (RSS, Talent500, HTML)
    ├── rss_client.py     ← RSS feed fetcher with date/location parsing
    ├── local_llm.py      ← Local heuristic scoring (no API key needed)
    ├── config.py         ← Settings loader
    └── __main__.py       ← CLI for Airtable export
```

---

## CLI Mode (Airtable export)

```bash
cp .env.example .env
# Edit .env — add AIRTABLE_TOKEN

python -m job_search --config config.json          # write to Airtable
python -m job_search --config config.json --dry-run  # preview only
python -m job_search --config config.json --no-ai    # skip scoring
```

---

## Troubleshooting

### `SSL: CERTIFICATE_VERIFY_FAILED`
```bash
pip install --upgrade certifi
# macOS Python.org installer: run "Install Certificates.command" in /Applications/Python 3.x/
```

### Port 5000 in use
The app auto-picks a free port. Check your terminal:
```
Job Search Dashboard: http://127.0.0.1:XXXX
```

### Talent500 returns 0 jobs
Make sure `job_role` in `config.json` has recognizable tech keywords like `"Data Engineer"` or `"Software Engineer"`.

### Gemini `ImportError` or `404 NOT_FOUND`
```bash
pip uninstall -y google-generativeai
pip install -r requirements.txt
```
Set `GEMINI_MODEL=gemini-2.0-flash` in `.env` if you see model-not-found errors.
