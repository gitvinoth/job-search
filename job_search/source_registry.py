"""Source registry — normalized schema management for job portals.

Every portal is stored in portals.json and conforms to the unified schema::

    {
      "id": "remoteok",
      "name": "RemoteOK",
      "source_name": "RemoteOK",
      "base_url": "https://remoteok.com",
      "type": "rss | json_api | dynamic_rss | html_scraper | talent500",
      "url": "https://remoteok.com/remote-jobs.rss",
      "category": "remote | startup | enterprise | freelance | general",
      "query_params_supported": false,
      "update_frequency": "hourly | daily | weekly",
      "reliability_score": 0.9,
      "enabled": true,
      "filter_keywords": [],
      "health": {
        "status": "unknown | healthy | degraded | down",
        "last_checked": null,
        "success_count": 0,
        "failure_count": 0,
        "last_response_time_ms": null
      }
    }

New portals can be added purely via config — no code change required.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_HEALTH_DEFAULTS: dict = {
    "status": "unknown",
    "last_checked": None,
    "success_count": 0,
    "failure_count": 0,
    "last_response_time_ms": None,
}

_SCHEMA_DEFAULTS: dict = {
    "category": "general",
    "query_params_supported": False,
    "update_frequency": "daily",
    "reliability_score": 0.8,
    "filter_keywords": [],
}


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------

def load_registry(path: str) -> list[dict]:
    """Load portals.json and fill in any missing normalized-schema fields."""
    p = Path(path)
    if not p.is_file():
        logger.warning("portals.json not found at %s", path)
        return []
    with p.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        logger.warning("portals.json must be a JSON array")
        return []
    return [_normalize(source) for source in data]


def save_registry(path: str, sources: list[dict]) -> None:
    """Persist registry to portals.json, stripping runtime-only entries."""
    save_list = []
    for src in sources:
        if src.get("_runtime"):
            continue
        entry = dict(src)
        if entry.get("type") == "talent500":
            entry["url"] = ""        # rebuilt at startup
        save_list.append(entry)
    p = Path(path).resolve()
    with p.open("w", encoding="utf-8") as fh:
        json.dump(save_list, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    logger.debug("Registry saved: %d sources → %s", len(save_list), p)


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def add_source(sources: list[dict], new_source: dict) -> tuple[bool, str]:
    """Validate and append a new source.  Returns (ok, reason)."""
    new_source = _normalize(new_source)
    new_id = new_source.get("id", "")
    new_url = new_source.get("url", "")

    if not new_source.get("name"):
        return False, "name is required"
    if not new_url and new_source.get("type") not in ("talent500",):
        return False, "url is required"

    for src in sources:
        if new_id and src.get("id") == new_id:
            return False, f"source id '{new_id}' already exists"
        if new_url and src.get("url") == new_url:
            return False, f"url '{new_url}' already registered"

    sources.append(new_source)
    logger.info("Source added: %s (%s)", new_source["name"], new_id)
    return True, "ok"


def remove_source(sources: list[dict], source_id: str) -> bool:
    for i, src in enumerate(sources):
        if src.get("id") == source_id:
            sources.pop(i)
            logger.info("Source removed: %s", source_id)
            return True
    return False


def toggle_source(sources: list[dict], source_id: str) -> bool | None:
    """Flip enabled flag; returns new state or None if not found."""
    for src in sources:
        if src.get("id") == source_id:
            src["enabled"] = not src.get("enabled", True)
            return src["enabled"]
    return None


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get_by_category(sources: list[dict], category: str) -> list[dict]:
    return [s for s in sources if s.get("category") == category]


def get_enabled(sources: list[dict]) -> list[dict]:
    return [s for s in sources if s.get("enabled", True) and s.get("url")]


def deduplicate(sources: list[dict]) -> list[dict]:
    """Remove sources with duplicate id or url (first occurrence wins)."""
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    result = []
    for src in sources:
        sid = src.get("id", "")
        url = src.get("url", "")
        if (sid and sid in seen_ids) or (url and url in seen_urls):
            logger.info("Dedup removed: %s", src.get("name"))
            continue
        if sid:
            seen_ids.add(sid)
        if url:
            seen_urls.add(url)
        result.append(src)
    return result


def summary(sources: list[dict]) -> dict:
    """Return a counts summary dict (for API responses)."""
    cats: dict[str, int] = {}
    enabled = 0
    for src in sources:
        if src.get("_runtime"):
            continue
        cat = src.get("category", "general")
        cats[cat] = cats.get(cat, 0) + 1
        if src.get("enabled", True):
            enabled += 1
    return {"total": len(sources), "enabled": enabled, "by_category": cats}


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _normalize(source: dict) -> dict:
    """Fill missing schema fields with sensible defaults (mutates in-place)."""
    # Auto-generate id from name if absent
    if not source.get("id"):
        raw = source.get("name", "unknown")
        source["id"] = (
            raw.lower()
            .replace(" ", "_")
            .replace("–", "")
            .replace("-", "_")
            .replace(".", "")
            .replace("/", "_")
        )

    # source_name mirrors name by default
    if not source.get("source_name"):
        source["source_name"] = source.get("name", "")

    # Derive base_url from url
    if not source.get("base_url"):
        url = source.get("url", "")
        if url and url.startswith("http"):
            parsed = urlparse(url)
            source["base_url"] = f"{parsed.scheme}://{parsed.netloc}"

    # Fill remaining schema defaults
    for key, default in _SCHEMA_DEFAULTS.items():
        if key not in source:
            source[key] = default

    # Health sub-object
    if "health" not in source:
        source["health"] = dict(_HEALTH_DEFAULTS)
    else:
        for k, v in _HEALTH_DEFAULTS.items():
            source["health"].setdefault(k, v)

    return source
