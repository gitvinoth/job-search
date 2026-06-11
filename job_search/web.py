"""Flask web dashboard – config-driven multi-portal job aggregator."""
from __future__ import annotations

import json as _json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List
from urllib.parse import quote_plus

from flask import Flask, jsonify, render_template_string, request

from job_search.config import load_settings
from job_search.health_checker import check_portals, update_portal_health
from job_search.local_llm import LocalHeuristicLLM, _strip_html_to_text
from job_search.portals import fetch_portal_jobs
from job_search.source_registry import (
    add_source, deduplicate, load_registry, remove_source,
    save_registry, summary, toggle_source,
)

logger = logging.getLogger(__name__)


@dataclass
class ScoredJob:
    title: str
    link: str
    description: str
    summary: str
    score: int
    source: str
    fetched_at: str
    location: str
    published: str


def _load_portals(portals_path: str) -> list[dict]:
    """Load portal configs from JSON file."""
    p = Path(portals_path)
    if not p.is_file():
        logger.warning("portals.json not found at %s, using empty list", p)
        return []
    with p.open(encoding="utf-8") as f:
        data = _json.load(f)
    if not isinstance(data, list):
        logger.warning("portals.json must be a JSON array")
        return []
    return data


def _save_portals(portals_path: str, portals: list[dict]) -> None:
    """Persist portal configs back to JSON (excluding runtime-only entries)."""
    save_list = []
    for portal in portals:
        if portal.get("_runtime"):
            continue
        entry = dict(portal)
        if entry.get("type") == "talent500":
            entry["url"] = ""
        save_list.append(entry)
    p = Path(portals_path).resolve()
    with p.open("w", encoding="utf-8") as f:
        _json.dump(save_list, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _build_talent500_url(job_role: str) -> str:
    """Build a Talent500 search URL from the job role."""
    if not job_role:
        return ""
    first_role = job_role.split(",")[0].strip()
    return f"https://talent500.com/joblist/?search_term={quote_plus(first_role)}"


def _build_dynamic_url(template: str, job_role: str, location: str) -> str:
    """Resolve a dynamic URL template using the current job_role and location."""
    if not template:
        return ""
    return (
        template
        .replace("{job_role}", quote_plus(job_role))
        .replace("{location}", quote_plus(location))
    )


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

    # Mutable settings
    _live = {
        "job_role": settings.job_role,
        "location": settings.location,
        "resume_summary": settings.resume_summary,
    }

    # Load portals via normalized registry
    portals_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "portals.json")
    portals: list[dict] = load_registry(portals_path)
    portals = deduplicate(portals)

    # Wire dynamic URLs from settings
    for p in portals:
        if p.get("type") == "talent500" and not p.get("url"):
            p["url"] = _build_talent500_url(_live["job_role"])
        elif p.get("type") == "dynamic_rss" and not p.get("url"):
            p["url"] = _build_dynamic_url(
                p.get("url_template", ""), _live["job_role"], _live["location"]
            )

    # Add user RSS feed if configured (runtime-only, not persisted to portals.json)
    if settings.rss_feed_url and "your-feed-id" not in settings.rss_feed_url.lower():
        portals.insert(0, {
            "id": "_user_feed",
            "name": "User Feed",
            "type": "rss",
            "url": settings.rss_feed_url,
            "category": "general",
            "enabled": True,
            "_runtime": True,
        })

    # ---- Health state ----
    _health_status: dict[str, dict] = {}
    _health_running = {"active": False}

    def _run_health_checks() -> None:
        if _health_running["active"]:
            return
        _health_running["active"] = True
        try:
            active = [p for p in portals if not p.get("_runtime")]
            results = check_portals(active)
            for source_id, result in results.items():
                _health_status[source_id] = result.to_dict()
                # Update portal health fields in-place
                for p in portals:
                    if (p.get("id") or p.get("name")) == source_id:
                        update_portal_health(p, result)
                        break
            # Persist updated reliability scores
            try:
                save_registry(portals_path, portals)
            except Exception as save_err:
                logger.warning("Could not save health state: %s", save_err)
        finally:
            _health_running["active"] = False

    def _health_scheduler() -> None:
        """Background thread: run health checks every hour."""
        time.sleep(60)          # first check 60s after startup
        while True:
            try:
                _run_health_checks()
            except Exception as exc:
                logger.warning("Scheduled health check error: %s", exc)
            time.sleep(3600)    # then every hour

    _scheduler_thread = threading.Thread(target=_health_scheduler, daemon=True)
    _scheduler_thread.start()

    def _active_portals() -> list[dict]:
        return [p for p in portals if p.get("enabled", True) and p.get("url")]

    def _fetch_and_score(portal: dict) -> list[ScoredJob]:
        url = portal["url"]
        if not url:
            return []
        cache_key = f"{portal['name']}::{url}"
        now = time.time()
        if cache_key in _cache and (now - _last_refresh.get(cache_key, 0)) < CACHE_TTL:
            return _cache[cache_key]

        items = fetch_portal_jobs(portal)

        jobs: list[ScoredJob] = []
        for item in items:
            plain = _strip_html_to_text(item.description)
            summary = llm.summarize_job_html(
                job_role=_live["job_role"],
                location=_live["location"],
                html_or_text=item.description,
            )
            enriched_resume = (
                f"{_live['resume_summary']} {_live['job_role']} {_live['location']}"
            )
            score = llm.match_score(
                job_summary=summary,
                job_role=_live["job_role"],
                location=_live["location"],
                resume_summary=enriched_resume,
            )
            # Use item location, or try to extract from title/description
            location = item.location or ""
            if not location:
                # Try extracting from title
                title_lower = item.title.lower()
                if "remote" in title_lower:
                    location = "Remote"
                elif any(c in title_lower for c in ("india", "bangalore", "bengaluru", "pune", "hyderabad", "mumbai", "chennai", "delhi")):
                    for c in ("Bangalore", "Bengaluru", "Pune", "Hyderabad", "Mumbai", "Chennai", "Delhi", "India"):
                        if c.lower() in title_lower:
                            location = c
                            break

            jobs.append(ScoredJob(
                title=item.title,
                link=item.link,
                description=plain[:300],
                summary=summary,
                score=score,
                source=portal["name"],
                fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                location=location or "Not specified",
                published=item.published or datetime.now().strftime("%Y-%m-%d %H:%M"),
            ))
        jobs.sort(key=lambda j: j.score, reverse=True)
        _cache[cache_key] = jobs
        _last_refresh[cache_key] = now
        return jobs

    # ================================================================
    #  HTML template
    # ================================================================
    HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Search Dashboard</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--muted:#94a3b8;
--accent:#38bdf8;--green:#22c55e;--yellow:#eab308;--red:#ef4444;--orange:#f97316;
--purple:#a78bfa;--sidebar-w:260px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:var(--bg);color:var(--text);line-height:1.6}
.layout{display:flex;min-height:100vh}

/* Sidebar */
.sidebar{width:var(--sidebar-w);background:var(--card);border-right:1px solid var(--border);
padding:16px;position:fixed;top:0;left:0;bottom:0;overflow-y:auto;z-index:50}
.sidebar h3{font-size:.85rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;
margin:16px 0 8px;padding-bottom:4px;border-bottom:1px solid var(--border)}
.sidebar h3:first-child{margin-top:0}
.sidebar-logo{font-size:1.1rem;font-weight:700;margin-bottom:16px;padding-bottom:12px;
border-bottom:1px solid var(--border)}
.sidebar-logo span{color:var(--accent)}
.filter-group{margin-bottom:4px}
.filter-item{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:6px;
cursor:pointer;font-size:.82rem;color:var(--muted);transition:all .15s}
.filter-item:hover{background:rgba(56,189,248,.08);color:var(--text)}
.filter-item.active{background:rgba(56,189,248,.15);color:var(--accent);font-weight:600}
.filter-item .count{margin-left:auto;font-size:.72rem;opacity:.7}
.filter-item input[type="radio"]{display:none}
.filter-dot{width:8px;height:8px;border-radius:50%;border:1.5px solid var(--muted);flex-shrink:0}
.filter-item.active .filter-dot{background:var(--accent);border-color:var(--accent)}
/* Location multi-select dropdown */
.loc-dropdown{position:relative;margin-bottom:4px}
.loc-btn{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);
padding:6px 10px;border-radius:6px;font-size:.82rem;cursor:pointer;display:flex;
justify-content:space-between;align-items:center;text-align:left;transition:border-color .15s}
.loc-btn:hover{border-color:var(--accent)}
.loc-btn .arrow{font-size:.6rem;color:var(--muted)}
.loc-panel{display:none;position:absolute;top:calc(100% + 4px);left:0;right:0;
background:var(--card);border:1px solid var(--border);border-radius:8px;
box-shadow:0 8px 24px rgba(0,0,0,.4);z-index:200;max-height:280px;overflow-y:auto}
.loc-panel.open{display:block}
.loc-search{padding:8px;border-bottom:1px solid var(--border)}
.loc-search input{width:100%;background:var(--bg);border:1px solid var(--border);
color:var(--text);padding:5px 8px;border-radius:4px;font-size:.8rem}
.loc-option{display:flex;align-items:center;gap:8px;padding:6px 10px;cursor:pointer;
font-size:.8rem;color:var(--muted);transition:background .1s}
.loc-option:hover{background:rgba(56,189,248,.08);color:var(--text)}
.loc-option input[type="checkbox"]{accent-color:var(--accent);width:13px;height:13px;cursor:pointer}
.loc-option label{flex:1;cursor:pointer;color:var(--text)}
.loc-option .loc-count{font-size:.7rem;color:var(--muted);margin-left:auto}
.loc-clear{padding:6px 10px;border-top:1px solid var(--border);font-size:.75rem;
color:var(--accent);cursor:pointer;text-align:center}
.loc-clear:hover{background:rgba(56,189,248,.08)}
.loc-tag{display:inline-block;background:rgba(56,189,248,.15);color:var(--accent);
padding:1px 6px;border-radius:10px;font-size:.7rem;margin:1px}

/* Main content */
.main{margin-left:var(--sidebar-w);flex:1;padding:16px 24px}
header{display:flex;justify-content:space-between;align-items:center;padding:12px 0;
border-bottom:1px solid var(--border);margin-bottom:16px;flex-wrap:wrap;gap:12px}
h1{font-size:1.4rem;font-weight:700}
h1 span{color:var(--accent)}
.stats{display:flex;gap:10px;flex-wrap:wrap}
.stat{background:var(--card);padding:5px 12px;border-radius:8px;text-align:center;
border:1px solid var(--border)}
.stat-val{font-size:1.2rem;font-weight:700;color:var(--accent)}
.stat-label{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
.controls input,.controls select{background:var(--card);color:var(--text);border:1px solid var(--border);
padding:7px 12px;border-radius:6px;font-size:.84rem}
.controls input{flex:1;min-width:200px}
.controls select{min-width:110px}
.btn{background:var(--accent);color:#0f172a;border:none;padding:6px 14px;border-radius:6px;
cursor:pointer;font-weight:600;font-size:.84rem;transition:all .2s}
.btn:hover{opacity:.85}
.btn.loading{opacity:.5;pointer-events:none}
.btn-secondary{background:#475569;color:var(--text)}
.btn-apply{background:var(--green);color:#fff;padding:4px 12px;border-radius:5px;
font-size:.78rem;font-weight:600;text-decoration:none;display:inline-flex;align-items:center;gap:4px;
transition:all .2s;border:none;cursor:pointer}
.btn-apply:hover{opacity:.85;transform:scale(1.02)}
.btn-apply.visited{background:#475569;color:var(--muted)}
.feed-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.feed-tab{padding:4px 11px;border-radius:20px;font-size:.76rem;cursor:pointer;
background:var(--card);border:1px solid var(--border);color:var(--muted);transition:all .2s}
.feed-tab.active{background:var(--accent);color:#0f172a;border-color:var(--accent);font-weight:600}
.feed-tab .count{margin-left:3px;opacity:.7}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:14px;transition:transform .15s,border-color .15s;position:relative;overflow:hidden;
display:flex;flex-direction:column}
.card:hover{transform:translateY(-2px);border-color:var(--accent)}
.card.visited-card{border-left:3px solid var(--purple);opacity:.85}
.card-header{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:5px}
.card-title{font-size:.92rem;font-weight:600;flex:1}
.card-title a{color:var(--text);text-decoration:none}
.card-title a:hover{color:var(--accent)}
.score-badge{padding:3px 8px;border-radius:20px;font-size:.76rem;font-weight:700;white-space:nowrap}
.score-high{background:rgba(34,197,94,.15);color:var(--green)}
.score-med{background:rgba(234,179,8,.15);color:var(--yellow)}
.score-low{background:rgba(249,115,22,.15);color:var(--orange)}
.score-none{background:rgba(239,68,68,.1);color:var(--red)}
.card-desc{font-size:.8rem;color:var(--muted);margin-bottom:8px;flex:1;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card-meta{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;font-size:.72rem}
.meta-tag{padding:2px 7px;border-radius:4px;display:inline-flex;align-items:center;gap:3px}
.meta-location{background:rgba(34,197,94,.1);color:var(--green)}
.meta-date{background:rgba(167,139,250,.1);color:var(--purple)}
.card-footer{display:flex;justify-content:space-between;align-items:center;font-size:.73rem;
color:var(--muted);margin-top:auto;padding-top:7px;border-top:1px solid var(--border)}
.source-tag{background:rgba(56,189,248,.1);color:var(--accent);padding:2px 7px;border-radius:4px;
font-size:.7rem}
.visited-tag{background:rgba(167,139,250,.15);color:var(--purple);padding:2px 7px;border-radius:4px;
font-size:.7rem;margin-left:5px}
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty h2{font-size:1.1rem;margin-bottom:8px;color:var(--text)}
.bar{position:absolute;top:0;left:0;height:3px;background:var(--accent);transition:width .3s}
.modal-overlay{display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.6);
align-items:center;justify-content:center}
.modal-overlay.open{display:flex}
/* Sources panel */
.cat-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.cat-tab{padding:3px 10px;border-radius:14px;font-size:.75rem;cursor:pointer;
background:var(--bg);border:1px solid var(--border);color:var(--muted);transition:all .2s}
.cat-tab.active{background:var(--accent);color:#0f172a;border-color:var(--accent);font-weight:600}
.src-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;
max-height:55vh;overflow-y:auto;padding-right:4px}
.src-card{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px;
position:relative;transition:border-color .15s}
.src-card:hover{border-color:var(--accent)}
.src-card.disabled{opacity:.5}
.src-header{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.health-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.h-healthy{background:var(--green)}.h-degraded{background:var(--yellow)}
.h-down{background:var(--red)}.h-unknown{background:var(--muted)}
.src-name{font-size:.88rem;font-weight:600;flex:1}
.src-badges{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px}
.src-badge{padding:1px 6px;border-radius:4px;font-size:.68rem}
.badge-type{background:rgba(56,189,248,.1);color:var(--accent)}
.badge-cat{background:rgba(167,139,250,.1);color:var(--purple)}
.badge-freq{background:rgba(34,197,94,.1);color:var(--green)}
.src-meta{font-size:.73rem;color:var(--muted);margin-bottom:8px}
.src-meta a{color:var(--accent);text-decoration:none;word-break:break-all}
.src-footer{display:flex;align-items:center;justify-content:space-between;margin-top:auto}
.reliability-bar{flex:1;height:4px;background:var(--border);border-radius:2px;margin:0 8px}
.reliability-fill{height:100%;border-radius:2px;background:var(--green);transition:width .3s}
.toggle-btn{font-size:.72rem;padding:2px 9px;border-radius:4px;cursor:pointer;border:1px solid;
transition:all .2s}
.toggle-on{background:rgba(34,197,94,.1);color:var(--green);border-color:var(--green)}
.toggle-off{background:rgba(239,68,68,.1);color:var(--red);border-color:var(--red)}
.src-setup{font-size:.72rem;color:var(--yellow);margin-top:4px;
background:rgba(234,179,8,.05);border-radius:4px;padding:4px 6px}
/* Add-source form */
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.form-row.full{grid-template-columns:1fr}
.form-group{margin-bottom:8px}
.form-group label{display:block;font-size:.8rem;color:var(--muted);margin-bottom:3px}
.form-group input,.form-group select,.form-group textarea{
width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border);
padding:7px 10px;border-radius:6px;font-size:.84rem}
.form-group textarea{resize:vertical}
.modal{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;
width:90%;max-width:600px;max-height:90vh;overflow-y:auto}
.modal h2{font-size:1.1rem;margin-bottom:14px}
.modal label{display:block;margin-bottom:4px;color:var(--muted);font-size:.82rem}
.modal input,.modal textarea{width:100%;background:var(--bg);color:var(--text);
border:1px solid var(--border);padding:8px 12px;border-radius:6px;margin-bottom:10px;font-size:.86rem}
.modal textarea{resize:vertical}
footer{text-align:center;padding:16px 0;color:var(--muted);font-size:.76rem;
border-top:1px solid var(--border);margin-top:20px}
@media(max-width:900px){
  .sidebar{position:relative;width:100%;border-right:none;border-bottom:1px solid var(--border)}
  .main{margin-left:0}
  .layout{flex-direction:column}
}
@media(max-width:600px){
  .grid{grid-template-columns:1fr}
  .stats{gap:6px}
  .stat{padding:4px 8px}
}
</style>
</head>
<body>
<div class="layout">

<!-- Left Sidebar -->
<aside class="sidebar">
  <div class="sidebar-logo">Job <span>Search</span></div>

  <h3>Location</h3>
  <div class="loc-dropdown" id="locDropdown">
    <button class="loc-btn" id="locBtn" onclick="toggleLocPanel(event)">
      <span id="locBtnLabel">All Locations</span>
      <span class="arrow">&#9660;</span>
    </button>
    <div class="loc-panel" id="locPanel">
      <div class="loc-search"><input type="text" id="locSearch" placeholder="Search location..." oninput="filterLocOptions()"></div>
      <div id="locOptions"></div>
      <div class="loc-clear" onclick="clearLocations()">Clear all selections</div>
    </div>
  </div>

  <h3>Date Posted</h3>
  <div class="filter-group" id="dateFilters">
    <div class="filter-item active" onclick="setDateFilter('all')">
      <span class="filter-dot"></span> All Time
    </div>
    <div class="filter-item" onclick="setDateFilter('1min')">
      <span class="filter-dot"></span> Last 1 minute
    </div>
    <div class="filter-item" onclick="setDateFilter('1hour')">
      <span class="filter-dot"></span> Last 1 hour
    </div>
    <div class="filter-item" onclick="setDateFilter('1day')">
      <span class="filter-dot"></span> Last 24 hours
    </div>
    <div class="filter-item" onclick="setDateFilter('1week')">
      <span class="filter-dot"></span> Last 1 week
    </div>
    <div class="filter-item" onclick="setDateFilter('2weeks')">
      <span class="filter-dot"></span> Last 2 weeks
    </div>
    <div class="filter-item" onclick="setDateFilter('1month')">
      <span class="filter-dot"></span> Last 1 month
    </div>
  </div>

  <h3>Visited</h3>
  <div class="filter-group" id="visitedFilters">
    <div class="filter-item active" onclick="setVisitedFilter('all')">
      <span class="filter-dot"></span> All Jobs
    </div>
    <div class="filter-item" onclick="setVisitedFilter('new')">
      <span class="filter-dot"></span> New Only
    </div>
    <div class="filter-item" onclick="setVisitedFilter('visited')">
      <span class="filter-dot"></span> Visited Only
    </div>
  </div>

  <h3>Score</h3>
  <div class="filter-group" id="scoreFilters">
    <div class="filter-item active" onclick="setScoreFilter(0)">
      <span class="filter-dot"></span> All Scores
    </div>
    <div class="filter-item" onclick="setScoreFilter(20)">
      <span class="filter-dot"></span> 20+
    </div>
    <div class="filter-item" onclick="setScoreFilter(40)">
      <span class="filter-dot"></span> 40+
    </div>
    <div class="filter-item" onclick="setScoreFilter(60)">
      <span class="filter-dot"></span> 60+
    </div>
  </div>
</aside>

<!-- Main Content -->
<div class="main">
<header>
  <h1>Job <span>Search</span> Dashboard</h1>
  <div class="stats">
    <div class="stat"><div class="stat-val" id="totalJobs">-</div><div class="stat-label">Total Jobs</div></div>
    <div class="stat"><div class="stat-val" id="avgScore">-</div><div class="stat-label">Avg Score</div></div>
    <div class="stat"><div class="stat-val" id="topMatch">-</div><div class="stat-label">Top Match</div></div>
    <div class="stat"><div class="stat-val" id="feedCount">-</div><div class="stat-label">Portals</div></div>
    <div class="stat"><div class="stat-val" id="visitedCount">0</div><div class="stat-label">Visited</div></div>
  </div>
</header>

<div class="controls">
  <input type="text" id="search" placeholder="Search jobs by title, description, source, location...">
  <select id="sortBy">
    <option value="score">Sort: Score</option>
    <option value="title">Sort: Title</option>
    <option value="source">Sort: Source</option>
    <option value="newest">Sort: Newest</option>
  </select>
  <button class="btn" id="refreshBtn" onclick="refresh()">Refresh</button>
  <button class="btn btn-secondary" onclick="openSources()">Sources</button>
  <button class="btn btn-secondary" onclick="openSettings()">Settings</button>
</div>

<div class="feed-tabs" id="feedTabs"></div>
<div class="grid" id="grid"></div>
<div class="empty" id="empty" style="display:none">
  <h2>No jobs found</h2>
  <p>Try adjusting your filters, add more portals, or configure your job role in Settings.</p>
</div>

<!-- Sources Modal -->
<div id="sourcesModal" class="modal-overlay" onclick="if(event.target===this)closeSources()">
  <div class="modal" style="max-width:860px;width:95%">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h2>Job Sources <span id="srcCountBadge" style="font-size:.8rem;color:var(--muted);font-weight:400"></span></h2>
      <button onclick="closeSources()" style="background:none;border:none;color:var(--muted);font-size:1.5rem;cursor:pointer">&times;</button>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center">
      <button class="btn btn-secondary" id="healthCheckBtn" onclick="runHealthCheck()" style="font-size:.8rem;padding:5px 12px">&#9679; Check Health</button>
      <button class="btn" onclick="openAddSource()" style="font-size:.8rem;padding:5px 12px">+ Add Source</button>
      <span id="healthCheckStatus" style="font-size:.78rem;color:var(--muted)"></span>
    </div>
    <div class="cat-tabs" id="srcCategoryTabs">
      <span class="cat-tab active" onclick="filterSrcCat('all')">All</span>
      <span class="cat-tab" onclick="filterSrcCat('remote')">Remote</span>
      <span class="cat-tab" onclick="filterSrcCat('startup')">Startup</span>
      <span class="cat-tab" onclick="filterSrcCat('enterprise')">Enterprise</span>
      <span class="cat-tab" onclick="filterSrcCat('freelance')">Freelance</span>
      <span class="cat-tab" onclick="filterSrcCat('general')">General</span>
    </div>
    <div class="src-grid" id="srcGrid"></div>
  </div>
</div>

<!-- Add Source Modal -->
<div id="addSourceModal" class="modal-overlay" onclick="if(event.target===this)closeAddSource()">
  <div class="modal" style="max-width:540px;width:95%">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <h2>Add Job Source</h2>
      <button onclick="closeAddSource()" style="background:none;border:none;color:var(--muted);font-size:1.5rem;cursor:pointer">&times;</button>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Source Name *</label>
        <input type="text" id="addName" placeholder="e.g. My Company Jobs">
      </div>
      <div class="form-group">
        <label>Category</label>
        <select id="addCategory">
          <option value="remote">Remote</option>
          <option value="startup">Startup</option>
          <option value="enterprise">Enterprise</option>
          <option value="freelance">Freelance</option>
          <option value="general" selected>General</option>
        </select>
      </div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Type</label>
        <select id="addType" onchange="onAddTypeChange()">
          <option value="rss" selected>RSS Feed</option>
          <option value="json_api">JSON API</option>
          <option value="html_scraper">HTML Scraper</option>
          <option value="dynamic_rss">Dynamic RSS (uses job role &amp; location)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Update Frequency</label>
        <select id="addFreq">
          <option value="hourly">Hourly</option>
          <option value="daily" selected>Daily</option>
          <option value="weekly">Weekly</option>
        </select>
      </div>
    </div>
    <div class="form-group form-row full" id="addUrlRow">
      <div class="form-group" style="margin-bottom:0">
        <label id="addUrlLabel">Feed URL *</label>
        <input type="text" id="addUrl" placeholder="https://example.com/jobs/feed.rss">
      </div>
    </div>
    <div class="form-group" id="addTemplateRow" style="display:none">
      <label>URL Template (use <code>{job_role}</code> and <code>{location}</code>)</label>
      <input type="text" id="addTemplate" placeholder="https://example.com/rss?q={job_role}&l={location}">
    </div>
    <div class="form-group" id="addJsonPathRow" style="display:none">
      <label>JSON Path to jobs array (e.g. <code>jobs</code> or <code>data.results</code>)</label>
      <input type="text" id="addJsonPath" placeholder="jobs">
    </div>
    <div class="form-group">
      <label>Filter Keywords (comma-separated, leave blank to include all jobs)</label>
      <input type="text" id="addKeywords" placeholder="engineer, developer, data, backend">
    </div>
    <p id="addSourceStatus" style="font-size:.83rem;margin:8px 0;display:none"></p>
    <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:4px">
      <button class="btn btn-secondary" onclick="closeAddSource()">Cancel</button>
      <button class="btn" onclick="submitAddSource()">Add Source</button>
    </div>
  </div>
</div>

<!-- Settings Modal -->
<div id="settingsModal" class="modal-overlay" onclick="if(event.target===this)closeSettings()">
  <div class="modal">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <h2>Job Search Settings</h2>
      <button onclick="closeSettings()" style="background:none;border:none;color:var(--muted);font-size:1.5rem;cursor:pointer">&times;</button>
    </div>
    <label>Job Titles</label>
    <input type="text" id="setRole" placeholder="e.g. Data Engineer, Senior Data Engineer">
    <label>Location</label>
    <input type="text" id="setLocation" placeholder="e.g. Remote India, Remote United States">
    <label>Resume Summary / Job Description</label>
    <textarea id="setResume" rows="5" placeholder="Describe your skills, experience, and target role..."></textarea>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn btn-secondary" onclick="closeSettings()">Cancel</button>
      <button class="btn" id="saveSettingsBtn" onclick="saveSettings()">Save &amp; Re-score</button>
    </div>
    <p id="settingsStatus" style="margin-top:10px;font-size:.85rem;color:var(--green);display:none"></p>
  </div>
</div>

<footer id="footerBar">
  Job Search Dashboard &middot; Targeting: <strong>{{ job_role }}</strong> in <strong>{{ location }}</strong>
</footer>
</div>
</div>

<script>
let allJobs=[], activeFeed='all';
let activeLocations=new Set(); // empty = all locations
let activeDateFilter='all', activeVisitedFilter='all', activeScoreFilter=0;
let _locCounts={};  // bucket -> count (from all jobs, for dropdown)

// ---- Visited/Applied tracking (localStorage) ----
function getVisited(){try{return JSON.parse(localStorage.getItem('jsd_visited')||'{}');}catch(e){return {};}}
function setVisited(link){const v=getVisited();v[link]=Date.now();localStorage.setItem('jsd_visited',JSON.stringify(v));}
function isVisited(link){return !!getVisited()[link];}
function visitedCount(){return Object.keys(getVisited()).length;}

async function fetchJobs(){const r=await fetch('/api/jobs');return r.json();}

function scoreClass(s){
  if(s>=60) return 'score-high';if(s>=35) return 'score-med';
  if(s>=15) return 'score-low';return 'score-none';
}

// ---- Date filter logic ----
function parseDate(s){
  if(!s) return null;
  const d=new Date(s);
  if(!isNaN(d.getTime())) return d;
  return null;
}
function matchesDateFilter(published, filter){
  if(filter==='all') return true;
  const d=parseDate(published);
  if(!d) return true;
  const now=Date.now();
  const ms={'1min':60000,'1hour':3600000,'1day':86400000,'1week':604800000,'2weeks':1209600000,'1month':2592000000};
  const cutoff=ms[filter];
  if(!cutoff) return true;
  return (now - d.getTime()) <= cutoff;
}

// ---- Location normalize ----
function normalizeLocation(loc){
  const l=(loc||'').toLowerCase().trim();
  if(!l || l==='not specified') return 'Not specified';
  if(l.includes('remote') || l.includes('anywhere') || l.includes('worldwide') || l.includes('global')) return 'Remote';
  // India
  if(l.includes('bangalore') || l.includes('bengaluru')) return 'Bangalore';
  if(l.includes('hyderabad')) return 'Hyderabad';
  if(l.includes('pune')) return 'Pune';
  if(l.includes('mumbai')) return 'Mumbai';
  if(l.includes('chennai')) return 'Chennai';
  if(l.includes('delhi') || l.includes('noida') || l.includes('gurgaon') || l.includes('gurugram')) return 'Delhi NCR';
  if(l.includes('kolkata')) return 'Kolkata';
  if(l.includes('ahmedabad')) return 'Ahmedabad';
  if(l.includes('kochi') || l.includes('cochin')) return 'Kochi';
  if(l.includes('coimbatore')) return 'Coimbatore';
  if(l.includes('jaipur')) return 'Jaipur';
  if(l.includes('india')) return 'India';
  // Europe
  if(l.includes('london') || l.includes('manchester') || l.includes('edinburgh') || l.includes('bristol') || l.includes('birmingham')) return 'United Kingdom';
  if(l.includes('berlin') || l.includes('munich') || l.includes('hamburg') || l.includes('frankfurt') || l.includes('cologne')) return 'Germany';
  if(l.includes('amsterdam') || l.includes('rotterdam') || l.includes('utrecht')) return 'Netherlands';
  if(l.includes('paris') || l.includes('lyon') || l.includes('marseille')) return 'France';
  if(l.includes('dublin')) return 'Ireland';
  if(l.includes('stockholm') || l.includes('gothenburg')) return 'Sweden';
  if(l.includes('copenhagen')) return 'Denmark';
  if(l.includes('barcelona') || l.includes('madrid')) return 'Spain';
  if(l.includes('lisbon') || l.includes('porto')) return 'Portugal';
  if(l.includes('warsaw') || l.includes('krakow') || l.includes('wroclaw')) return 'Poland';
  if(l.includes('prague')) return 'Czech Republic';
  if(l.includes('vienna')) return 'Austria';
  if(l.includes('zurich') || l.includes('geneva') || l.includes('bern')) return 'Switzerland';
  if(l.includes('helsinki')) return 'Finland';
  if(l.includes('oslo')) return 'Norway';
  if(l.includes('brussels')) return 'Belgium';
  if(l.includes('milan') || l.includes('rome')) return 'Italy';
  if(l.includes('europe')) return 'Europe';
  // North America
  if(l.includes('toronto') || l.includes('vancouver') || l.includes('montreal') || l.includes('ottawa')) return 'Canada';
  if(l.includes('new york') || l.includes('nyc')) return 'New York';
  if(l.includes('san francisco') || l.includes('bay area') || l.includes('sf')) return 'San Francisco';
  if(l.includes('seattle')) return 'Seattle';
  if(l.includes('austin')) return 'Austin';
  if(l.includes('chicago')) return 'Chicago';
  if(l.includes('boston')) return 'Boston';
  if(l.includes('los angeles') || l.includes(' la,') || l.includes('la ')) return 'Los Angeles';
  if(l.includes('usa') || l.includes('united states') || l.includes('us ') || l==='us') return 'USA';
  // Asia-Pacific
  if(l.includes('singapore')) return 'Singapore';
  if(l.includes('tokyo') || l.includes('osaka')) return 'Japan';
  if(l.includes('sydney') || l.includes('melbourne') || l.includes('australia')) return 'Australia';
  if(l.includes('dubai') || l.includes('abu dhabi')) return 'UAE';
  // Return cleaned first segment
  return loc.split(/[,|/]/)[0].trim() || 'Not specified';
}

function matchesLocationFilter(job){
  if(activeLocations.size===0) return true;
  const bucket=normalizeLocation(job.location||'');
  return activeLocations.has(bucket);
}

// ---- Location dropdown (multi-select) ----
function buildLocCounts(jobs){
  _locCounts={};
  jobs.forEach(j=>{
    const b=normalizeLocation(j.location||'');
    _locCounts[b]=(_locCounts[b]||0)+1;
  });
}

function renderLocOptions(searchVal=''){
  const q=searchVal.toLowerCase();
  const sorted=Object.entries(_locCounts).sort((a,b)=>b[1]-a[1]);
  const html=sorted.filter(([loc])=>!q||loc.toLowerCase().includes(q)).map(([loc,c])=>`
    <div class="loc-option">
      <input type="checkbox" id="lc_${esc(loc)}" ${activeLocations.has(loc)?'checked':''}
        onchange="toggleLocation('${loc.replace(/'/g,"\\'")}',this.checked)">
      <label for="lc_${esc(loc)}">${esc(loc)}</label>
      <span class="loc-count">${c}</span>
    </div>`).join('');
  document.getElementById('locOptions').innerHTML=html||'<div class="loc-option" style="color:var(--muted)">No matches</div>';
}

function updateLocBtn(){
  const label=document.getElementById('locBtnLabel');
  if(activeLocations.size===0){
    label.innerHTML='All Locations';
  } else {
    const tags=[...activeLocations].map(l=>`<span class="loc-tag">${esc(l)}</span>`).join(' ');
    label.innerHTML=tags;
  }
}

function toggleLocPanel(e){
  e.stopPropagation();
  document.getElementById('locPanel').classList.toggle('open');
  document.getElementById('locSearch').value='';
  renderLocOptions();
}
function filterLocOptions(){
  renderLocOptions(document.getElementById('locSearch').value);
}
function toggleLocation(loc, checked){
  if(checked) activeLocations.add(loc);
  else activeLocations.delete(loc);
  updateLocBtn();
  renderAll();
}
function clearLocations(){
  activeLocations.clear();
  updateLocBtn();
  renderLocOptions(document.getElementById('locSearch').value);
  renderAll();
}
// Close dropdown when clicking outside
document.addEventListener('click', e=>{
  const panel=document.getElementById('locPanel');
  if(panel && !document.getElementById('locDropdown').contains(e.target)){
    panel.classList.remove('open');
  }
});

// ---- Sidebar filter setters ----
function setDateFilter(f){
  activeDateFilter=f;
  updateFilterUI('dateFilters',f);
  renderAll();
}
function setVisitedFilter(f){
  activeVisitedFilter=f;
  updateFilterUI('visitedFilters',f);
  renderAll();
}
function setScoreFilter(s){
  activeScoreFilter=s;
  updateFilterUI('scoreFilters',String(s));
  renderAll();
}
function updateFilterUI(containerId, activeValue){
  const items=document.getElementById(containerId).querySelectorAll('.filter-item');
  items.forEach(el=>{
    const onclick=el.getAttribute('onclick')||'';
    const match=onclick.match(/'([^']*)'/) || onclick.match(/\((\d+)\)/);
    if(match) el.classList.toggle('active', match[1]===String(activeValue));
  });
}

function renderAll(){
  buildLocCounts(allJobs);
  renderTabs(allJobs);
  renderGrid(allJobs);
}

function renderTabs(jobs){
  // Count per source applying ALL non-source filters so tab counts stay in sync
  const srcCounts={};
  jobs.forEach(j=>{
    if(j.score<activeScoreFilter) return;
    if(activeVisitedFilter==='new' && isVisited(j.link)) return;
    if(activeVisitedFilter==='visited' && !isVisited(j.link)) return;
    if(!matchesLocationFilter(j)) return;
    if(!matchesDateFilter(j.published, activeDateFilter)) return;
    srcCounts[j.source]=(srcCounts[j.source]||0)+1;
  });
  const totalFiltered=Object.values(srcCounts).reduce((s,c)=>s+c,0);
  let html=`<div class="feed-tab ${activeFeed==='all'?'active':''}" onclick="setFeed('all')">All<span class="count">(${totalFiltered})</span></div>`;
  Object.entries(srcCounts).sort((a,b)=>b[1]-a[1]).forEach(([s,c])=>{
    html+=`<div class="feed-tab ${activeFeed===s?'active':''}" onclick="setFeed('${s.replace(/'/g,"\\'")}')">
      ${s}<span class="count">(${c})</span></div>`;
  });
  document.getElementById('feedTabs').innerHTML=html;
}

function renderGrid(jobs){
  const q=document.getElementById('search').value.toLowerCase();
  const sort=document.getElementById('sortBy').value;

  let filtered=jobs.filter(j=>{
    if(activeFeed!=='all' && j.source!==activeFeed) return false;
    if(j.score<activeScoreFilter) return false;
    if(activeVisitedFilter==='new' && isVisited(j.link)) return false;
    if(activeVisitedFilter==='visited' && !isVisited(j.link)) return false;
    if(!matchesLocationFilter(j)) return false;
    if(!matchesDateFilter(j.published, activeDateFilter)) return false;
    if(q && !j.title.toLowerCase().includes(q) && !j.description.toLowerCase().includes(q)
       && !j.source.toLowerCase().includes(q) && !(j.location||'').toLowerCase().includes(q)) return false;
    return true;
  });

  if(sort==='title') filtered.sort((a,b)=>a.title.localeCompare(b.title));
  else if(sort==='source') filtered.sort((a,b)=>a.source.localeCompare(b.source)||b.score-a.score);
  else if(sort==='newest') filtered.sort((a,b)=>(b.published||'').localeCompare(a.published||''));
  else filtered.sort((a,b)=>b.score-a.score);

  // Update header stats with filtered counts
  document.getElementById('totalJobs').textContent=filtered.length;
  const avg=filtered.length?Math.round(filtered.reduce((s,j)=>s+j.score,0)/filtered.length):0;
  document.getElementById('avgScore').textContent=avg;
  document.getElementById('topMatch').textContent=filtered.length?filtered.reduce((m,j)=>Math.max(m,j.score),0):'-';
  document.getElementById('feedCount').textContent=new Set(filtered.map(j=>j.source)).size;
  document.getElementById('visitedCount').textContent=visitedCount();

  if(!filtered.length){
    document.getElementById('grid').innerHTML='';
    document.getElementById('empty').style.display='block';return;
  }
  document.getElementById('empty').style.display='none';

  document.getElementById('grid').innerHTML=filtered.map(j=>{
    const v=isVisited(j.link);
    const pubDate=formatDate(j.published);
    return `<div class="card${v?' visited-card':''}">
      <div class="bar" style="width:${j.score}%"></div>
      <div class="card-header">
        <div class="card-title"><a href="${j.link}" target="_blank" rel="noopener">${esc(j.title)}</a></div>
        <span class="score-badge ${scoreClass(j.score)}">${j.score}%</span>
      </div>
      <div class="card-desc">${esc(j.description)}</div>
      <div class="card-meta">
        ${j.location?`<span class="meta-tag meta-location">&#128205; ${esc(normalizeLocation(j.location))}</span>`:''}
        ${pubDate?`<span class="meta-tag meta-date">&#128197; ${pubDate}</span>`:''}
      </div>
      <div class="card-footer">
        <div>
          <span class="source-tag">${esc(j.source)}</span>
          ${v?'<span class="visited-tag">Visited</span>':''}
        </div>
        <a class="btn-apply${v?' visited':''}" href="${j.link}" target="_blank" rel="noopener"
           onclick="markVisited('${j.link.replace(/'/g,"\\'")}')">
          ${v?'Revisit':'Apply'} &#8599;
        </a>
      </div>
    </div>`}).join('');
}

function formatDate(s){
  if(!s) return '';
  const d=parseDate(s);
  if(!d) return s;
  const now=Date.now();
  const diff=now-d.getTime();
  if(diff<0) return 'Just now';
  if(diff<60000) return Math.floor(diff/1000)+'s ago';
  if(diff<3600000) return Math.floor(diff/60000)+'m ago';
  if(diff<86400000) return Math.floor(diff/3600000)+'h ago';
  if(diff<604800000) return Math.floor(diff/86400000)+'d ago';
  return d.toLocaleDateString();
}

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function setFeed(f){activeFeed=f;renderAll();}
function markVisited(link){setVisited(link);renderGrid(allJobs);}

async function refresh(){
  const btn=document.getElementById('refreshBtn');
  btn.classList.add('loading');btn.textContent='Loading...';
  try{
    const data=await fetchJobs();
    allJobs=data.jobs||[];
    renderAll();
    updateLocBtn();
    if(data.settings){
      document.getElementById('footerBar').innerHTML=
        'Job Search Dashboard &middot; Targeting: <strong>'+esc(data.settings.job_role)+'</strong> in <strong>'+esc(data.settings.location)+'</strong>';
    }
  }catch(e){console.error(e);}
  btn.classList.remove('loading');btn.textContent='Refresh';
}

// ---- Settings modal ----
async function openSettings(){
  document.getElementById('settingsModal').classList.add('open');
  try{const r=await fetch('/api/settings');const s=await r.json();
    document.getElementById('setRole').value=s.job_role||'';
    document.getElementById('setLocation').value=s.location||'';
    document.getElementById('setResume').value=s.resume_summary||'';
  }catch(e){console.error(e);}
}
function closeSettings(){document.getElementById('settingsModal').classList.remove('open');}
async function saveSettings(){
  const btn=document.getElementById('saveSettingsBtn');
  const status=document.getElementById('settingsStatus');
  btn.classList.add('loading');btn.textContent='Saving...';
  try{
    const body={job_role:document.getElementById('setRole').value,
      location:document.getElementById('setLocation').value,
      resume_summary:document.getElementById('setResume').value};
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.ok){status.textContent='Saved! Refreshing...';status.style.color='var(--green)';status.style.display='block';
      setTimeout(async()=>{closeSettings();status.style.display='none';await refresh();},1200);}
  }catch(e){status.textContent='Error';status.style.color='var(--red)';status.style.display='block';}
  btn.classList.remove('loading');btn.textContent='Save & Re-score';
}

document.getElementById('search').addEventListener('input',()=>renderGrid(allJobs));
document.getElementById('sortBy').addEventListener('change',()=>renderGrid(allJobs));

// ============================================================
// Sources Panel
// ============================================================
let allSources=[], srcCatFilter='all';

async function openSources(){
  document.getElementById('sourcesModal').classList.add('open');
  await loadSources();
}
function closeSources(){document.getElementById('sourcesModal').classList.remove('open');}

async function loadSources(){
  try{
    const r=await fetch('/api/sources');
    const d=await r.json();
    allSources=d.sources||[];
    const s=d.summary||{};
    document.getElementById('srcCountBadge').textContent=
      `(${s.enabled||0} enabled / ${s.total||0} total)`;
    renderSources();
  }catch(e){console.error('loadSources',e);}
}

function filterSrcCat(cat){
  srcCatFilter=cat;
  document.querySelectorAll('#srcCategoryTabs .cat-tab').forEach(t=>{
    t.classList.toggle('active',t.textContent.toLowerCase()===cat||
      (cat==='all'&&t.textContent==='All'));
  });
  renderSources();
}

function healthDotClass(status){
  return {'healthy':'h-healthy','degraded':'h-degraded','down':'h-down'}[status]||'h-unknown';
}

function renderSources(){
  const filtered=srcCatFilter==='all'?allSources:allSources.filter(s=>s.category===srcCatFilter);
  const grid=document.getElementById('srcGrid');
  if(!filtered.length){grid.innerHTML='<p style="color:var(--muted);padding:20px">No sources in this category.</p>';return;}
  grid.innerHTML=filtered.map(s=>{
    const h=s.health||{};
    const dotCls=healthDotClass(h.status||'unknown');
    const rel=Math.round((s.reliability_score||0)*100);
    const relColor=rel>=80?'var(--green)':rel>=50?'var(--yellow)':'var(--red)';
    const hasUrl=!!(s.url||s.url_template);
    const setup=s._setup||'';
    return `<div class="src-card${s.enabled?'':' disabled'}">
      <div class="src-header">
        <span class="health-dot ${dotCls}" title="Health: ${h.status||'unknown'}"></span>
        <span class="src-name">${esc(s.name)}</span>
      </div>
      <div class="src-badges">
        <span class="src-badge badge-type">${esc(s.type)}</span>
        <span class="src-badge badge-cat">${esc(s.category)}</span>
        <span class="src-badge badge-freq">${esc(s.update_frequency||'daily')}</span>
      </div>
      <div class="src-meta">
        ${hasUrl?`<a href="${esc(s.url||s.url_template)}" target="_blank" rel="noopener">${esc((s.url||s.url_template).slice(0,55))}${(s.url||s.url_template).length>55?'…':''}</a>`:'<em>No URL configured</em>'}
      </div>
      ${setup?`<div class="src-setup">&#9432; ${esc(setup.slice(0,120))}${setup.length>120?'…':''}</div>`:''}
      <div style="display:flex;align-items:center;margin-top:8px;gap:6px">
        <span style="font-size:.7rem;color:var(--muted)">Reliability</span>
        <div class="reliability-bar"><div class="reliability-fill" style="width:${rel}%;background:${relColor}"></div></div>
        <span style="font-size:.7rem;color:${relColor}">${rel}%</span>
        ${h.last_checked?`<span style="font-size:.65rem;color:var(--muted);margin-left:auto">${formatDate(h.last_checked)}</span>`:''}
      </div>
      <div class="src-footer" style="margin-top:8px">
        <span style="font-size:.7rem;color:var(--muted)">
          &#10003;${h.success_count||0} &nbsp; &#10007;${h.failure_count||0}
          ${h.last_response_time_ms?` &nbsp; ${h.last_response_time_ms}ms`:''}
        </span>
        <button class="toggle-btn ${s.enabled?'toggle-on':'toggle-off'}"
          onclick="toggleSource('${s.id}')">
          ${s.enabled?'Enabled':'Disabled'}
        </button>
      </div>
    </div>`;
  }).join('');
}

async function toggleSource(sourceId){
  try{
    const r=await fetch(`/api/sources/${encodeURIComponent(sourceId)}/toggle`,{method:'POST'});
    const d=await r.json();
    if(d.ok){await loadSources();}
    else{alert(d.message);}
  }catch(e){console.error('toggleSource',e);}
}

async function runHealthCheck(){
  const btn=document.getElementById('healthCheckBtn');
  const status=document.getElementById('healthCheckStatus');
  btn.disabled=true;btn.textContent='Checking…';
  status.textContent='Health check in progress…';
  try{
    const r=await fetch('/api/sources/health-check',{method:'POST'});
    const d=await r.json();
    if(d.ok){
      status.textContent='Running in background. Results appear when done.';
      // Poll for completion
      const poll=setInterval(async()=>{
        const hr=await fetch('/api/sources/health');
        const hd=await hr.json();
        if(!hd.running){
          clearInterval(poll);
          btn.disabled=false;btn.textContent='&#9679; Check Health';
          status.textContent='Health check complete.';
          await loadSources();
        }
      },2000);
    }else{
      status.textContent=d.message||'Error';
      btn.disabled=false;btn.textContent='&#9679; Check Health';
    }
  }catch(e){
    status.textContent='Error running health check';
    btn.disabled=false;btn.textContent='&#9679; Check Health';
  }
}

// ---- Add Source ----
function openAddSource(){document.getElementById('addSourceModal').classList.add('open');}
function closeAddSource(){
  document.getElementById('addSourceModal').classList.remove('open');
  document.getElementById('addSourceStatus').style.display='none';
}
function onAddTypeChange(){
  const t=document.getElementById('addType').value;
  document.getElementById('addUrlRow').style.display=(t==='dynamic_rss')?'none':'block';
  document.getElementById('addTemplateRow').style.display=(t==='dynamic_rss')?'block':'none';
  document.getElementById('addJsonPathRow').style.display=(t==='json_api')?'block':'none';
  document.getElementById('addUrlLabel').textContent=
    t==='html_scraper'?'Page URL *':t==='json_api'?'API Endpoint URL *':'Feed URL *';
}
async function submitAddSource(){
  const status=document.getElementById('addSourceStatus');
  const type=document.getElementById('addType').value;
  const name=document.getElementById('addName').value.trim();
  const url=document.getElementById('addUrl').value.trim();
  const template=document.getElementById('addTemplate').value.trim();
  const keywords=document.getElementById('addKeywords').value
    .split(',').map(k=>k.trim()).filter(Boolean);

  const payload={
    name, type,
    category:document.getElementById('addCategory').value,
    update_frequency:document.getElementById('addFreq').value,
    url: type==='dynamic_rss'?'':url,
    url_template: type==='dynamic_rss'?template:'',
    filter_keywords: keywords,
    enabled: true,
  };
  if(type==='json_api'){
    const jp=document.getElementById('addJsonPath').value.trim();
    if(jp) payload.json_path=jp;
  }

  try{
    const r=await fetch('/api/sources/add',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();
    status.style.display='block';
    if(d.ok){
      status.style.color='var(--green)';status.textContent='Source added!';
      setTimeout(()=>{closeAddSource();loadSources();},900);
    }else{
      status.style.color='var(--red)';status.textContent=d.message||'Error';
    }
  }catch(e){
    status.style.display='block';status.style.color='var(--red)';
    status.textContent='Request failed';
  }
}

refresh();
setInterval(refresh,300000);
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
        for portal in _active_portals():
            all_jobs.extend(_fetch_and_score(portal))
        all_jobs.sort(key=lambda j: j.score, reverse=True)
        return jsonify({
            "jobs": [asdict(j) for j in all_jobs],
            "feeds": len(_active_portals()),
            "settings": {**_live},
        })

    @app.route("/api/settings")
    def api_settings():
        return jsonify(_live)

    @app.route("/api/settings", methods=["POST"])
    def api_update_settings():
        data = request.get_json(silent=True) or {}
        changed = False
        for key in ("job_role", "location", "resume_summary"):
            if key in data and isinstance(data[key], str):
                _live[key] = data[key].strip()
                changed = True
        if changed:
            _cache.clear()
            _last_refresh.clear()
            # Re-wire dynamic URLs
            for p in portals:
                if p.get("type") == "talent500":
                    p["url"] = _build_talent500_url(_live["job_role"])
                elif p.get("type") == "dynamic_rss":
                    p["url"] = _build_dynamic_url(
                        p.get("url_template", ""), _live["job_role"], _live["location"]
                    )
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
                logger.warning("Could not save config.json: %s", e)
        return jsonify({"ok": True, **_live})

    # ------------------------------------------------------------------ #
    #  Sources & Health API                                               #
    # ------------------------------------------------------------------ #

    @app.route("/api/sources")
    def api_sources():
        """Return all portals with their health and registry metadata."""
        result = []
        for p in portals:
            if p.get("_runtime"):
                continue
            entry = {
                "id": p.get("id", p.get("name")),
                "name": p.get("name"),
                "source_name": p.get("source_name", p.get("name")),
                "base_url": p.get("base_url", ""),
                "type": p.get("type", "rss"),
                "url": p.get("url", ""),
                "url_template": p.get("url_template", ""),
                "category": p.get("category", "general"),
                "query_params_supported": p.get("query_params_supported", False),
                "update_frequency": p.get("update_frequency", "daily"),
                "reliability_score": p.get("reliability_score", 0.0),
                "enabled": p.get("enabled", True),
                "filter_keywords": p.get("filter_keywords", []),
                "health": p.get("health", {}),
                "_setup": p.get("_setup", ""),
            }
            # Merge live health_status if available
            sid = entry["id"]
            if sid in _health_status:
                entry["health"] = {**entry["health"], **_health_status[sid]}
            result.append(entry)
        return jsonify({
            "sources": result,
            "summary": summary(portals),
            "health_running": _health_running["active"],
        })

    @app.route("/api/sources/health-check", methods=["POST"])
    def api_health_check():
        """Start an async health check for all configured sources."""
        if _health_running["active"]:
            return jsonify({"ok": False, "message": "Health check already in progress"}), 409
        threading.Thread(target=_run_health_checks, daemon=True).start()
        return jsonify({"ok": True, "message": "Health check started"})

    @app.route("/api/sources/health")
    def api_sources_health():
        """Return the latest health results for all sources."""
        return jsonify({
            "health": _health_status,
            "running": _health_running["active"],
            "checked_at": max(
                (v.get("checked_at", "") for v in _health_status.values()),
                default=None,
            ),
        })

    @app.route("/api/sources/<source_id>/toggle", methods=["POST"])
    def api_toggle_source(source_id: str):
        """Enable or disable a source by id."""
        new_state = toggle_source(portals, source_id)
        if new_state is None:
            return jsonify({"ok": False, "message": f"Source '{source_id}' not found"}), 404
        _cache.clear()
        _last_refresh.clear()
        try:
            save_registry(portals_path, portals)
        except Exception as exc:
            logger.warning("Could not persist toggle: %s", exc)
        return jsonify({"ok": True, "source_id": source_id, "enabled": new_state})

    @app.route("/api/sources/add", methods=["POST"])
    def api_add_source():
        """Add a new source from a JSON payload conforming to the registry schema."""
        data = request.get_json(silent=True) or {}
        if not data.get("name"):
            return jsonify({"ok": False, "message": "name is required"}), 400
        if not data.get("url") and data.get("type") not in ("talent500", "dynamic_rss"):
            return jsonify({"ok": False, "message": "url is required"}), 400
        ok, reason = add_source(portals, data)
        if not ok:
            return jsonify({"ok": False, "message": reason}), 409
        _cache.clear()
        try:
            save_registry(portals_path, portals)
        except Exception as exc:
            logger.warning("Could not persist new source: %s", exc)
        return jsonify({"ok": True, "message": "Source added", "id": data.get("id")})

    @app.route("/api/sources/<source_id>", methods=["DELETE"])
    def api_delete_source(source_id: str):
        """Remove a source by id."""
        removed = remove_source(portals, source_id)
        if not removed:
            return jsonify({"ok": False, "message": f"Source '{source_id}' not found"}), 404
        _cache.clear()
        try:
            save_registry(portals_path, portals)
        except Exception as exc:
            logger.warning("Could not persist removal: %s", exc)
        return jsonify({"ok": True, "message": f"Source '{source_id}' removed"})

    return app


def main():
    import argparse

    from dotenv import load_dotenv
    load_dotenv()

    p = argparse.ArgumentParser(description="Job Search Web Dashboard")
    p.add_argument("--config", default="config.json", help="Path to config JSON")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)), help="Port")
    p.add_argument("--host", default="127.0.0.1", help="Host")
    p.add_argument("--debug", action="store_true", help="Flask debug mode")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    app = _build_app(args.config)
    print(f"\n  Job Search Dashboard: http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
