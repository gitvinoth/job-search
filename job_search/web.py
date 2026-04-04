"""Flask web dashboard – browse RSS job feeds on localhost."""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import List

import requests as http_requests
from flask import Flask, jsonify, render_template_string, request

from job_search.config import load_settings
from job_search.local_llm import LocalHeuristicLLM, _strip_html_to_text
from job_search.rss_client import JobFeedItem, fetch_feed_items

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Talent500 HTML scraper (no RSS/API available — scrape the job list page)
# ---------------------------------------------------------------------------
def _fetch_talent500_jobs(url: str) -> list[JobFeedItem]:
    """Fetch Talent500 jobs via their XML sitemap and filter by search term.

    Talent500 is fully JS-rendered with a private API, but their sitemap
    at /jobs-sitemap.xml lists every job URL.  We parse the sitemap,
    filter URLs by keyword, and derive title/company/location from the
    URL slug (e.g. /jobs/company/title-location-ID/).
    """
    # Extract search_term from the original URL for filtering
    search_m = re.search(r'search_term=([^&]+)', url)
    keywords = search_m.group(1).replace("+", " ").lower().split() if search_m else ["data", "engineer"]

    headers = {"User-Agent": "Mozilla/5.0 (compatible; JobSearchCollector/0.1)"}
    r = http_requests.get(
        "https://talent500.com/jobs-sitemap.xml",
        headers=headers, timeout=30,
    )
    r.raise_for_status()

    # Parse all job URLs from sitemap
    all_urls = re.findall(r'<loc>(https://talent500\.com/jobs/[^<]+)</loc>', r.text)

    items: list[JobFeedItem] = []
    for job_url in all_urls:
        # URL format: /jobs/company/slug-with-title-location-T500-ID/
        slug = job_url.rstrip("/").split("/")[-1]  # e.g. "senior-data-engineer-bengaluru-T500-24860"
        slug_lower = slug.lower()

        # Filter: all keywords must appear in the slug
        if not all(kw in slug_lower for kw in keywords):
            continue

        # Derive title from slug: remove T500-ID suffix, replace hyphens with spaces
        clean = re.sub(r'-t500-\d+$', '', slug, flags=re.IGNORECASE)
        parts = clean.replace("-", " ").title()

        # Extract company from URL path
        company = job_url.rstrip("/").split("/")[-2].replace("-", " ").title()

        title = f"{parts} @ {company}"
        desc = f"{parts} — {company} (Talent500)"

        items.append(JobFeedItem(title=title, link=job_url, description=desc))

    logger.info("Talent500 sitemap: found %d matching jobs (keywords=%s, total=%d)",
                len(items), keywords, len(all_urls))
    return items



DEFAULT_FEEDS: list[dict] = [
    {
        "name": "User Feed",
        "url": "",  # filled from config.json
        "category": "custom",
    },

    # ---- Indeed ----
    # Create RSS.app feeds from: https://www.indeed.com/jobs?q=data+engineer&l=Remote
    {
        "name": "Indeed – Data Engineer",
        "url": "",  # Replace with your RSS.app feed for Indeed data engineer search
        "category": "indeed",
    },
    {
        "name": "Indeed – Senior Data Engineer",
        "url": "",  # Replace with your RSS.app feed for Indeed senior data engineer search
        "category": "indeed",
    },
    {
        "name": "Indeed – Staff Data Engineer",
        "url": "",  # Replace with your RSS.app feed for Indeed staff data engineer search
        "category": "indeed",
    },
    {
        "name": "Talent500",
        "url": "https://talent500.com/joblist/?search_term=Data+Engineer&experience_range=6-10+years&offset=0&limit=20",
        "category": "talent500",
    },
    # ---- LinkedIn (via RSS.app – replace with your feed) ----
    {
        "name": "LinkedIn – Data Engineer (RSS.app)",
        "url": "https://rss.app/feeds/6dUWERqAQ0aEHTu1.xml",
        "category": "linkedin",
    },

    {
        "name": "Hacker News – Who is Hiring?",
        "url": "https://hnrss.org/whoishiring/jobs",
        "category": "tech",
    },
    {
        "name": "RemoteOK – Remote Jobs",
        "url": "https://remoteok.com/remote-jobs.rss",
        "category": "remote",
    },
    {
        "name": "We Work Remotely",
        "url": "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "category": "remote",
    },
    {
        "name": "GitHub Jobs (via RSS.app)",
        "url": "",  # placeholder – users can add their own
        "category": "tech",
    },
]


@dataclass
class ScoredJob:
    title: str
    link: str
    description: str
    summary: str
    score: int
    source: str
    fetched_at: str


def _build_app(config_path: str = "config.json") -> Flask:
    app = Flask(__name__)

    # ---- shared state ----
    _cache: dict[str, list[ScoredJob]] = {}
    _last_refresh: dict[str, float] = {}
    CACHE_TTL = 300  # 5 min

    from dotenv import load_dotenv
    load_dotenv()
    settings = load_settings(config_path)
    llm = LocalHeuristicLLM()

    # Mutable settings that can be changed at runtime via the Settings panel
    _live = {
        "job_role": settings.job_role,
        "location": settings.location,
        "resume_summary": settings.resume_summary,
    }

    # Wire user feed
    feeds = []
    for f in DEFAULT_FEEDS:
        if f["name"] == "User Feed":
            if settings.rss_feed_url:
                feeds.append({**f, "url": settings.rss_feed_url})
        elif f["url"]:
            feeds.append(f)

    def _fetch_and_score(feed: dict) -> list[ScoredJob]:
        url = feed["url"]
        if not url:
            return []
        now = time.time()
        if url in _cache and (now - _last_refresh.get(url, 0)) < CACHE_TTL:
            return _cache[url]

        try:
            if feed.get("category") == "talent500":
                items = _fetch_talent500_jobs(url)
            else:
                items = fetch_feed_items(url)
        except Exception as e:
            logger.warning("Feed %s failed: %s", feed["name"], e)
            return _cache.get(url, [])

        jobs: list[ScoredJob] = []
        for item in items:
            plain = _strip_html_to_text(item.description)
            summary = llm.summarize_job_html(
                job_role=_live["job_role"],
                location=_live["location"],
                html_or_text=item.description,
            )
            # Include job_role and location in resume text so they
            # contribute to the keyword-overlap score.
            enriched_resume = (
                f"{_live['resume_summary']} {_live['job_role']} {_live['location']}"
            )
            score = llm.match_score(
                job_summary=summary,
                job_role=_live["job_role"],
                location=_live["location"],
                resume_summary=enriched_resume,
            )
            jobs.append(ScoredJob(
                title=item.title,
                link=item.link,
                description=plain[:300],
                summary=summary,
                score=score,
                source=feed["name"],
                fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            ))
        jobs.sort(key=lambda j: j.score, reverse=True)
        _cache[url] = jobs
        _last_refresh[url] = now
        return jobs

    # ================================================================
    #  HTML template (single-page, self-contained)
    # ================================================================
    HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Search Dashboard</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--muted:#94a3b8;
--accent:#38bdf8;--green:#22c55e;--yellow:#eab308;--red:#ef4444;--orange:#f97316}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:var(--bg);color:var(--text);line-height:1.6}
.container{max-width:1200px;margin:0 auto;padding:20px}
header{display:flex;justify-content:space-between;align-items:center;padding:20px 0;
border-bottom:1px solid var(--border);margin-bottom:24px;flex-wrap:wrap;gap:12px}
h1{font-size:1.6rem;font-weight:700}
h1 span{color:var(--accent)}
.stats{display:flex;gap:16px;flex-wrap:wrap}
.stat{background:var(--card);padding:8px 16px;border-radius:8px;text-align:center;
border:1px solid var(--border)}
.stat-val{font-size:1.4rem;font-weight:700;color:var(--accent)}
.stat-label{font-size:.75rem;color:var(--muted);text-transform:uppercase}
.controls{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px;align-items:center}
.controls input,.controls select{background:var(--card);color:var(--text);border:1px solid var(--border);
padding:8px 12px;border-radius:6px;font-size:.9rem}
.controls input{flex:1;min-width:200px}
.controls select{min-width:140px}
.btn{background:var(--accent);color:#0f172a;border:none;padding:8px 18px;border-radius:6px;
cursor:pointer;font-weight:600;font-size:.9rem;transition:opacity .2s}
.btn:hover{opacity:.85}
.btn.loading{opacity:.5;pointer-events:none}
.feed-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.feed-tab{padding:6px 14px;border-radius:20px;font-size:.8rem;cursor:pointer;
background:var(--card);border:1px solid var(--border);color:var(--muted);transition:all .2s}
.feed-tab.active{background:var(--accent);color:#0f172a;border-color:var(--accent);font-weight:600}
.feed-tab .count{margin-left:4px;opacity:.7}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:18px;transition:transform .15s,border-color .15s;position:relative;overflow:hidden}
.card:hover{transform:translateY(-2px);border-color:var(--accent)}
.card-header{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:8px}
.card-title{font-size:1rem;font-weight:600;flex:1}
.card-title a{color:var(--text);text-decoration:none}
.card-title a:hover{color:var(--accent)}
.score-badge{padding:4px 10px;border-radius:20px;font-size:.8rem;font-weight:700;white-space:nowrap}
.score-high{background:rgba(34,197,94,.15);color:var(--green)}
.score-med{background:rgba(234,179,8,.15);color:var(--yellow)}
.score-low{background:rgba(249,115,22,.15);color:var(--orange)}
.score-none{background:rgba(239,68,68,.1);color:var(--red)}
.card-desc{font-size:.85rem;color:var(--muted);margin-bottom:10px;
display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.card-meta{display:flex;justify-content:space-between;font-size:.75rem;color:var(--muted)}
.source-tag{background:rgba(56,189,248,.1);color:var(--accent);padding:2px 8px;border-radius:4px}
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty h2{font-size:1.2rem;margin-bottom:8px;color:var(--text)}
.bar{position:absolute;top:0;left:0;height:3px;background:var(--accent);transition:width .3s}
footer{text-align:center;padding:30px 0;color:var(--muted);font-size:.8rem;border-top:1px solid var(--border);margin-top:30px}
</style>
</head>
<body>
<div class="container">
<header>
  <h1>Job <span>Search</span> Dashboard</h1>
  <div class="stats">
    <div class="stat"><div class="stat-val" id="totalJobs">-</div><div class="stat-label">Total Jobs</div></div>
    <div class="stat"><div class="stat-val" id="avgScore">-</div><div class="stat-label">Avg Score</div></div>
    <div class="stat"><div class="stat-val" id="topMatch">-</div><div class="stat-label">Top Match</div></div>
    <div class="stat"><div class="stat-val" id="feedCount">-</div><div class="stat-label">Feeds</div></div>
  </div>
</header>

<div class="controls">
  <input type="text" id="search" placeholder="Search jobs by title, description...">
  <select id="sortBy">
    <option value="score">Sort by Score</option>
    <option value="title">Sort by Title</option>
    <option value="source">Sort by Source</option>
  </select>
  <select id="minScore">
    <option value="0">All Scores</option>
    <option value="20">Score 20+</option>
    <option value="40">Score 40+</option>
    <option value="60">Score 60+</option>
  </select>
  <button class="btn" id="refreshBtn" onclick="refresh()">Refresh Feeds</button>
  <button class="btn" style="background:#64748b" onclick="openSettings()">Settings</button>
</div>

<!-- Settings Modal -->
<div id="settingsModal" style="display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.6);
  display:none;align-items:center;justify-content:center">
  <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;
    width:90%;max-width:600px;max-height:90vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 style="font-size:1.2rem">Job Search Settings</h2>
      <button onclick="closeSettings()" style="background:none;border:none;color:var(--muted);
        font-size:1.5rem;cursor:pointer">&times;</button>
    </div>
    <label style="display:block;margin-bottom:4px;color:var(--muted);font-size:.85rem">Job Titles</label>
    <input type="text" id="setRole" style="width:100%;background:var(--bg);color:var(--text);
      border:1px solid var(--border);padding:10px;border-radius:6px;margin-bottom:12px;font-size:.9rem"
      placeholder="e.g. Data Engineer, Senior Data Engineer">
    <label style="display:block;margin-bottom:4px;color:var(--muted);font-size:.85rem">Location</label>
    <input type="text" id="setLocation" style="width:100%;background:var(--bg);color:var(--text);
      border:1px solid var(--border);padding:10px;border-radius:6px;margin-bottom:12px;font-size:.9rem"
      placeholder="e.g. Remote United States">
    <label style="display:block;margin-bottom:4px;color:var(--muted);font-size:.85rem">Resume Summary / Job Description</label>
    <textarea id="setResume" rows="6" style="width:100%;background:var(--bg);color:var(--text);
      border:1px solid var(--border);padding:10px;border-radius:6px;margin-bottom:16px;font-size:.85rem;
      resize:vertical" placeholder="Describe your skills, experience, and target role..."></textarea>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn" style="background:#64748b" onclick="closeSettings()">Cancel</button>
      <button class="btn" id="saveSettingsBtn" onclick="saveSettings()">Save &amp; Re-score</button>
    </div>
    <p id="settingsStatus" style="margin-top:10px;font-size:.85rem;color:var(--green);display:none"></p>
  </div>
</div>

<div class="feed-tabs" id="feedTabs"></div>
<div class="grid" id="grid"></div>
<div class="empty" id="empty" style="display:none">
  <h2>No jobs found</h2>
  <p>Try adjusting your filters or refresh the feeds.</p>
</div>

<footer>
  Job Search Dashboard &middot; Targeting: <strong>{{ job_role }}</strong> in <strong>{{ location }}</strong>
  &middot; Auto-refreshes every 5 min
</footer>
</div>

<script>
let allJobs=[], activeFeed='all';

async function fetchJobs(){
  const r=await fetch('/api/jobs');
  return r.json();
}

function scoreClass(s){
  if(s>=60) return 'score-high';
  if(s>=35) return 'score-med';
  if(s>=15) return 'score-low';
  return 'score-none';
}

function renderTabs(jobs){
  const sources={};
  jobs.forEach(j=>{sources[j.source]=(sources[j.source]||0)+1});
  let html=`<div class="feed-tab ${activeFeed==='all'?'active':''}" onclick="setFeed('all')">All<span class="count">(${jobs.length})</span></div>`;
  Object.entries(sources).sort((a,b)=>b[1]-a[1]).forEach(([s,c])=>{
    html+=`<div class="feed-tab ${activeFeed===s?'active':''}" onclick="setFeed('${s.replace(/'/g,"\\'")}')">
      ${s}<span class="count">(${c})</span></div>`;
  });
  document.getElementById('feedTabs').innerHTML=html;
}

function renderGrid(jobs){
  const q=document.getElementById('search').value.toLowerCase();
  const sort=document.getElementById('sortBy').value;
  const minS=parseInt(document.getElementById('minScore').value);

  let filtered=jobs.filter(j=>{
    if(activeFeed!=='all' && j.source!==activeFeed) return false;
    if(j.score<minS) return false;
    if(q && !j.title.toLowerCase().includes(q) && !j.description.toLowerCase().includes(q)) return false;
    return true;
  });

  if(sort==='title') filtered.sort((a,b)=>a.title.localeCompare(b.title));
  else if(sort==='source') filtered.sort((a,b)=>a.source.localeCompare(b.source)||b.score-a.score);
  else filtered.sort((a,b)=>b.score-a.score);

  // stats
  document.getElementById('totalJobs').textContent=filtered.length;
  const avg=filtered.length?Math.round(filtered.reduce((s,j)=>s+j.score,0)/filtered.length):0;
  document.getElementById('avgScore').textContent=avg;
  document.getElementById('topMatch').textContent=filtered.length?filtered.reduce((m,j)=>Math.max(m,j.score),0):'-';
  document.getElementById('feedCount').textContent=new Set(jobs.map(j=>j.source)).size;

  if(!filtered.length){
    document.getElementById('grid').innerHTML='';
    document.getElementById('empty').style.display='block';
    return;
  }
  document.getElementById('empty').style.display='none';

  document.getElementById('grid').innerHTML=filtered.map(j=>`
    <div class="card">
      <div class="bar" style="width:${j.score}%"></div>
      <div class="card-header">
        <div class="card-title"><a href="${j.link}" target="_blank" rel="noopener">${esc(j.title)}</a></div>
        <span class="score-badge ${scoreClass(j.score)}">${j.score}%</span>
      </div>
      <div class="card-desc">${esc(j.description)}</div>
      <div class="card-meta">
        <span class="source-tag">${esc(j.source)}</span>
        <span>${j.fetched_at}</span>
      </div>
    </div>`).join('');
}

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}

function setFeed(f){activeFeed=f;renderTabs(allJobs);renderGrid(allJobs);}

async function refresh(){
  const btn=document.getElementById('refreshBtn');
  btn.classList.add('loading');btn.textContent='Loading...';
  try{
    const data=await fetchJobs();
    allJobs=data.jobs||[];
    renderTabs(allJobs);renderGrid(allJobs);
    if(data.settings){
      document.querySelector('footer').innerHTML=
        'Job Search Dashboard &middot; Targeting: <strong>'+esc(data.settings.job_role)+'</strong> in <strong>'+esc(data.settings.location)+'</strong> &middot; Auto-refreshes every 5 min';
    }
  }catch(e){console.error(e);}
  btn.classList.remove('loading');btn.textContent='Refresh Feeds';
}

document.getElementById('search').addEventListener('input',()=>renderGrid(allJobs));
document.getElementById('sortBy').addEventListener('change',()=>renderGrid(allJobs));
document.getElementById('minScore').addEventListener('change',()=>renderGrid(allJobs));

// Settings modal
async function openSettings(){
  const modal=document.getElementById('settingsModal');
  modal.style.display='flex';
  try{
    const r=await fetch('/api/settings');
    const s=await r.json();
    document.getElementById('setRole').value=s.job_role||'';
    document.getElementById('setLocation').value=s.location||'';
    document.getElementById('setResume').value=s.resume_summary||'';
  }catch(e){console.error(e);}
}
function closeSettings(){document.getElementById('settingsModal').style.display='none';}
async function saveSettings(){
  const btn=document.getElementById('saveSettingsBtn');
  const status=document.getElementById('settingsStatus');
  btn.classList.add('loading');btn.textContent='Saving...';
  try{
    const body={
      job_role:document.getElementById('setRole').value,
      location:document.getElementById('setLocation').value,
      resume_summary:document.getElementById('setResume').value
    };
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.ok){
      status.textContent='Saved! Refreshing jobs with new settings...';
      status.style.display='block';
      // Update footer
      document.querySelector('footer').innerHTML=
        'Job Search Dashboard &middot; Targeting: <strong>'+esc(d.job_role)+'</strong> in <strong>'+esc(d.location)+'</strong> &middot; Auto-refreshes every 5 min';
      setTimeout(async()=>{
        closeSettings();
        status.style.display='none';
        await refresh();
      },1500);
    }
  }catch(e){console.error(e);status.textContent='Error saving settings';status.style.color='var(--red)';status.style.display='block';}
  btn.classList.remove('loading');btn.textContent='Save & Re-score';
}
document.getElementById('settingsModal').addEventListener('click',function(e){if(e.target===this)closeSettings();});

// Initial load + auto-refresh
refresh();
setInterval(refresh, 300000);
</script>
</body>
</html>"""

    # ================================================================
    #  Routes
    # ================================================================
    @app.route("/")
    def index():
        return render_template_string(HTML, job_role=_live["job_role"], location=_live["location"])

    @app.route("/api/jobs")
    def api_jobs():
        all_jobs: List[ScoredJob] = []
        for feed in feeds:
            all_jobs.extend(_fetch_and_score(feed))
        all_jobs.sort(key=lambda j: j.score, reverse=True)
        return jsonify({
            "jobs": [asdict(j) for j in all_jobs],
            "feeds": len(feeds),
            "settings": {**_live},
        })

    @app.route("/api/feeds")
    def api_feeds():
        return jsonify({"feeds": feeds})

    @app.route("/api/add-feed", methods=["POST"])
    def api_add_feed():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        name = (data.get("name") or url).strip()
        if not url:
            return jsonify({"error": "url required"}), 400
        feeds.append({"name": name, "url": url, "category": "custom"})
        return jsonify({"ok": True, "feeds": len(feeds)})

    @app.route("/api/settings")
    def api_settings():
        return jsonify(_live)

    @app.route("/api/settings", methods=["POST"])
    def api_update_settings():
        import json as _json
        data = request.get_json(silent=True) or {}
        changed = False
        for key in ("job_role", "location", "resume_summary"):
            if key in data and isinstance(data[key], str) and data[key].strip():
                _live[key] = data[key].strip()
                changed = True
        if changed:
            # Clear cache so jobs are re-scored with new settings
            _cache.clear()
            _last_refresh.clear()
            # Persist to config.json
            try:
                cfg_path = os.path.abspath(config_path)
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = _json.load(f)
                cfg["job_role"] = _live["job_role"]
                cfg["location"] = _live["location"]
                cfg["resume_summary"] = _live["resume_summary"]
                with open(cfg_path, "w", encoding="utf-8") as f:
                    _json.dump(cfg, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                logger.info("Settings saved to %s", cfg_path)
            except Exception as e:
                logger.warning("Could not save settings to config.json: %s", e)
        return jsonify({"ok": True, **_live})

    return app


def main():
    import argparse

    from dotenv import load_dotenv
    load_dotenv()

    p = argparse.ArgumentParser(description="Job Search Web Dashboard")
    p.add_argument("--config", default="config.json", help="Path to config JSON")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)), help="Port (default 5000)")
    p.add_argument("--host", default="127.0.0.1", help="Host (default 127.0.0.1)")
    p.add_argument("--debug", action="store_true", help="Flask debug mode")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    app = _build_app(args.config)
    print(f"\n  Job Search Dashboard running at http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
