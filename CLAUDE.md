# Job Search Dashboard — Claude Project Context

## Project overview
A self-hosted job aggregator web dashboard. Fetches jobs from 6+ portals, scores them against a resume using local heuristic matching (F1 keyword overlap), and displays them with filters.

## Project location
**Main folder:** `/Users/vinoth/new_project/`
**GitHub:** `https://github.com/gitvinoth/job-search`
**Branch strategy:** feature branches → PR → merge to main → `git fetch origin main && git reset --hard origin/main`

## How to run
```bash
cd /Users/vinoth/new_project
source .venv/bin/activate
python -m job_search.web --config config.json
# Opens at http://localhost:5000 (autoPort if busy)
```

## Key files
| File | Purpose |
|------|---------|
| `job_search/web.py` | Flask dashboard — HTML template, routes, scoring |
| `job_search/portals.py` | Portal scrapers: RSS, Talent500 sitemap, HTML scraper |
| `job_search/rss_client.py` | RSS fetcher — returns JobFeedItem with location + published |
| `job_search/local_llm.py` | Local heuristic scoring (F1 keyword overlap, no API key) |
| `job_search/config.py` | Settings dataclass + loader |
| `config.json` | User config: job_role, location, resume_summary, rss_feed_url |
| `portals.json` | Portal registry: name, type, url, enabled, filter_keywords |
| `.env` | API keys only (Airtable, Gemini, Claude) — never commit |

## Architecture

### Portal system
- `portals.json` defines all portals with `type`: `rss`, `talent500`, `html_scraper`, `sitemap`
- `portals.py` → `FETCHERS` dict dispatches by type
- `filter_keywords` per portal keeps only role-relevant jobs from general boards
- Talent500 URL is built dynamically from `job_role` in settings

### Scoring
- `LocalHeuristicLLM.match_score()` — F1 keyword overlap between job text and enriched resume
- Enriched resume = `resume_summary + job_role + location` (location-aware)
- Score range: 0–100%

### JobFeedItem dataclass
```python
JobFeedItem(title, link, description, location="", published="")
```
`location` and `published` added for sidebar filters.

### ScoredJob dataclass
```python
ScoredJob(title, link, description, summary, score, source, fetched_at, location, published)
```

### Web dashboard (web.py)
- Cache: 5 min TTL per portal, keyed by `portal_name::url`
- `_live` dict: mutable settings (job_role, location, resume_summary) — updated by Settings modal
- On settings save: clears cache, re-wires Talent500 URL, persists to config.json
- User Feed: runtime-only portal (not saved to portals.json), injected from config.rss_feed_url

### Frontend (inline JS in web.py HTML template)
- `allJobs` = full job list from `/api/jobs`
- `activeLocations` = Set (multi-select), empty = all
- `activeFeed` = source tab filter
- `activeDateFilter`, `activeVisitedFilter`, `activeScoreFilter` = sidebar filters
- `renderAll()` → `buildLocCounts()` + `renderTabs()` + `renderGrid()`
- Tab counts reflect active non-source filters (location, date, visited, score)
- Visited tracking: localStorage key `jsd_visited` → `{url: timestamp}`
- `normalizeLocation()` maps raw location strings to buckets (Bangalore, Remote, United Kingdom, Germany, etc.)

## Portals status
| Portal | Type | Status |
|--------|------|--------|
| User Feed | rss | Active (from config.rss_feed_url) |
| Talent500 | talent500 | Active — sitemap + keyword filter |
| RemoteOK | rss | Active |
| We Work Remotely – Programming | rss | Active |
| We Work Remotely – DevOps | rss | Active |
| Jobspresso | rss | Active + filter_keywords |
| NoDesk | rss | Active (nodesk.co/remote-jobs/index.xml) |
| JSRemotely | html_scraper | Active (javascript.jobs/remote) |
| Cutshort | html_scraper | Active |
| Crossover | html_scraper | Active + filter_keywords |
| LinkedIn, Indeed, Glassdoor, etc. | rss | Disabled — need RSS.app feed URL |
| Wellfound, Toptal | html_scraper | Disabled — 403 blocks |

## Common tasks

### Change job role / location
Open dashboard → Settings → update fields → Save & Re-score
(also persists to config.json)

### Add a new portal
Add entry to `portals.json`:
```json
{
  "name": "My Portal",
  "type": "rss",
  "url": "https://example.com/feed.xml",
  "enabled": true,
  "filter_keywords": ["engineer", "developer"]
}
```

### New feature branch workflow
```bash
git checkout -b feat/your-feature
# make changes, test
git add <files>
git commit -m "feat: description"
git push -u origin feat/your-feature
# open PR on GitHub → merge → git fetch origin main && git reset --hard origin/main
```

## Dependencies
```
flask>=3.0.0
feedparser>=6.0.11
requests>=2.31.0
certifi>=2024.2.2
python-dotenv>=1.0.1
anthropic>=0.40.0   # CLI mode only
google-genai>=1.0.0 # CLI mode only
```
