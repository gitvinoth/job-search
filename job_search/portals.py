"""Job portal scrapers — one function per source, common interface."""
from __future__ import annotations

import logging
import re
from typing import List
from urllib.parse import quote_plus, unquote_plus

import requests

from job_search.rss_client import JobFeedItem, fetch_feed_items

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

# Known location words for extraction from slugs/titles
_INDIAN_CITIES = {
    "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune", "chennai",
    "kolkata", "noida", "gurgaon", "gurugram", "ahmedabad", "jaipur", "kochi",
    "thiruvananthapuram", "coimbatore", "indore", "chandigarh", "lucknow",
    "nagpur", "visakhapatnam", "bhubaneswar", "mangalore", "mysore",
}
_EUROPEAN_CITIES = {
    "london", "manchester", "berlin", "munich", "amsterdam", "paris", "dublin",
    "stockholm", "copenhagen", "barcelona", "madrid", "warsaw", "prague", "vienna",
    "zurich", "helsinki", "oslo", "brussels", "lisbon", "rome", "milan",
    "frankfurt", "hamburg", "dusseldorf", "cologne", "rotterdam", "utrecht",
    "edinburgh", "bristol", "birmingham", "toronto", "vancouver", "montreal",
}
_GLOBAL_REGIONS = {
    "remote", "worldwide", "anywhere", "global",
    "india", "usa", "us", "uk", "europe", "canada", "australia",
    "singapore", "tokyo", "dubai", "sydney", "new york", "san francisco",
    "seattle", "austin", "chicago", "boston",
}
_LOCATION_WORDS = _INDIAN_CITIES | _EUROPEAN_CITIES | _GLOBAL_REGIONS


def _strip_html(raw: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&\w+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_location_from_text(text: str) -> str:
    """Extract a location from free text by matching known city/country names."""
    t = text.lower()
    # Check for remote first
    if "remote" in t or "work from home" in t or "wfh" in t:
        return "Remote"
    found = []
    for word in _LOCATION_WORDS:
        if word in t:
            found.append(word.title())
    return ", ".join(found[:2]) if found else ""


def _extract_location_from_slug(slug: str) -> str:
    """Try to extract a location from a URL slug."""
    return _extract_location_from_text(slug.replace("-", " ").replace("_", " "))


def _filter_by_keywords(items: list[JobFeedItem], keywords: list[str]) -> list[JobFeedItem]:
    """Keep only items whose title or description contains at least one keyword."""
    if not keywords:
        return items
    kws = [k.lower() for k in keywords]
    return [
        item for item in items
        if any(kw in (item.title + " " + item.description).lower() for kw in kws)
    ]


# ---- Portal registry -------------------------------------------------------

def fetch_rss(portal: dict) -> list[JobFeedItem]:
    """Generic RSS/Atom feed fetcher."""
    return fetch_feed_items(portal["url"])


def fetch_talent500(portal: dict) -> list[JobFeedItem]:
    """Scrape Talent500 via their XML sitemap, filtering by keywords."""
    url = portal["url"]
    search_m = re.search(r'search_term=([^&]+)', url)
    keywords = unquote_plus(search_m.group(1)).lower().split() if search_m else []
    # Drop non-alphanumeric tokens like "&"
    keywords = [kw for kw in keywords if kw.isalnum()]
    if not keywords:
        logger.info("Talent500: no search_term in URL, skipping")
        return []

    r = requests.get(
        "https://talent500.com/jobs-sitemap.xml",
        headers={"User-Agent": _UA}, timeout=30,
    )
    r.raise_for_status()
    all_urls = re.findall(r'<loc>(https://talent500\.com/jobs/[^<]+)</loc>', r.text)

    items: list[JobFeedItem] = []
    for job_url in all_urls:
        slug = job_url.rstrip("/").split("/")[-1].lower()
        matched = sum(1 for kw in keywords if kw in slug)
        if matched < min(2, len(keywords)):
            continue
        clean = re.sub(r'-t500[_-][a-z0-9_]+$', '', slug, flags=re.IGNORECASE)
        title_part = clean.replace("-", " ").title()
        company = job_url.rstrip("/").split("/")[-2].replace("-", " ").title()
        location = _extract_location_from_slug(clean)
        items.append(JobFeedItem(
            title=f"{title_part} @ {company}",
            link=job_url,
            description=f"{title_part} — {company} (Talent500)",
            location=location or "India",  # Talent500 is predominantly India
        ))

    logger.info("Talent500: %d jobs matched (keywords=%s, total=%d)", len(items), keywords, len(all_urls))
    return items


def fetch_remoteok(portal: dict) -> list[JobFeedItem]:
    return fetch_feed_items(portal["url"])


def fetch_weworkremotely(portal: dict) -> list[JobFeedItem]:
    return fetch_feed_items(portal["url"])


def fetch_sitemap_generic(portal: dict) -> list[JobFeedItem]:
    """Generic sitemap scraper."""
    sitemap_url = portal.get("sitemap_url", "")
    url_pattern = portal.get("url_pattern", r'<loc>([^<]+)</loc>')
    keywords = [k.lower() for k in portal.get("keywords", [])]

    if not sitemap_url:
        return []

    r = requests.get(sitemap_url, headers={"User-Agent": _UA}, timeout=30)
    r.raise_for_status()
    all_urls = re.findall(url_pattern, r.text)

    items: list[JobFeedItem] = []
    for job_url in all_urls:
        slug = job_url.rstrip("/").split("/")[-1]
        if keywords and not all(kw in slug.lower() for kw in keywords):
            continue
        title = slug.replace("-", " ").replace("_", " ").title()
        location = _extract_location_from_slug(slug)
        items.append(JobFeedItem(title=title, link=job_url, description=title, location=location))

    logger.info("%s sitemap: %d jobs (total=%d)", portal["name"], len(items), len(all_urls))
    return items


def fetch_html_scraper(portal: dict) -> list[JobFeedItem]:
    """Generic HTML scraper — extracts job links from a page."""
    url = portal["url"]
    link_pattern = portal.get("link_pattern", r'href="(/job[^"]+)"')
    base_url = portal.get("base_url", "")

    r = requests.get(url, headers=_HEADERS, timeout=30)
    r.raise_for_status()

    matches = re.findall(link_pattern, r.text)
    items: list[JobFeedItem] = []
    seen: set[str] = set()

    for match in matches:
        if isinstance(match, tuple):
            link, title = match[0], match[1] if len(match) > 1 else ""
        else:
            link, title = match, ""

        if not link.startswith("http"):
            link = base_url + link

        if link in seen:
            continue
        seen.add(link)

        if not title:
            slug = link.rstrip("/").split("/")[-1]
            title = slug.replace("-", " ").replace("_", " ").title()
        else:
            title = _strip_html(title).strip()

        location = _extract_location_from_text(title + " " + link.rstrip("/").split("/")[-1])
        items.append(JobFeedItem(
            title=title, link=link, description=title,
            location=location or "Remote",  # HTML scraped portals are mostly remote
        ))

    logger.info("%s scraper: %d jobs from %s", portal["name"], len(items), url)
    return items


def fetch_json_api(portal: dict) -> list[JobFeedItem]:
    """Fetch jobs from a JSON API endpoint.

    Portal config fields:
    - url: API endpoint (required)
    - json_path: dot-separated path to the list, e.g. "jobs" or "data.results"
    - title_field: key for job title (default: "title")
    - link_field: key for job URL (default: "url")
    - desc_field: key for description (default: "description")
    - location_field: key for location (default: "location")
    - date_field: key for publication date (default: "" = omitted)
    """
    url = portal.get("url", "")
    if not url:
        return []

    json_path = portal.get("json_path", "")
    title_field = portal.get("title_field", "title")
    link_field = portal.get("link_field", "url")
    desc_field = portal.get("desc_field", "description")
    location_field = portal.get("location_field", "location")
    date_field = portal.get("date_field", "")

    r = requests.get(url, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()

    # Navigate nested path: e.g. "jobs" → data["jobs"]
    if json_path:
        for key in json_path.split("."):
            if isinstance(data, dict):
                data = data.get(key, [])

    if not isinstance(data, list):
        logger.warning("%s JSON API: expected list at path '%s', got %s", portal["name"], json_path, type(data).__name__)
        return []

    items: list[JobFeedItem] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        link = str(entry.get(link_field, "")).strip()
        if not link:
            continue
        title = str(entry.get(title_field, "")).strip() or "(no title)"
        # Strip HTML from description field (Remotive API returns HTML)
        raw_desc = str(entry.get(desc_field, "")).strip()
        desc = _strip_html(raw_desc) if "<" in raw_desc else raw_desc
        location = str(entry.get(location_field, "")).strip()
        published = str(entry.get(date_field, "")).strip() if date_field else ""
        items.append(JobFeedItem(
            title=title, link=link, description=desc[:500],
            location=location, published=published,
        ))

    logger.info("%s JSON API: %d jobs from %s", portal["name"], len(items), url)
    return items


def fetch_dynamic_rss(portal: dict) -> list[JobFeedItem]:
    """Dynamic RSS feed whose URL was pre-built from config parameters.

    The URL is expected to already be resolved (set by web.py/_build_app from
    the portal's ``url_template`` field). Falls back gracefully if empty.
    """
    url = portal.get("url", "")
    if not url:
        logger.debug("%s dynamic_rss: no URL resolved yet, skipping", portal.get("name"))
        return []
    return fetch_feed_items(url)


def fetch_noop(portal: dict) -> list[JobFeedItem]:
    logger.debug("%s: no URL configured, skipping", portal["name"])
    return []


# ---- Dispatcher -------------------------------------------------------------

FETCHERS = {
    "rss": fetch_rss,
    "talent500": fetch_talent500,
    "remoteok": fetch_remoteok,
    "weworkremotely": fetch_weworkremotely,
    "sitemap": fetch_sitemap_generic,
    "html_scraper": fetch_html_scraper,
    "json_api": fetch_json_api,
    "dynamic_rss": fetch_dynamic_rss,
    "noop": fetch_noop,
}


def fetch_portal_jobs(portal: dict) -> list[JobFeedItem]:
    """Dispatch to the correct fetcher, then apply keyword filter if configured."""
    ptype = portal.get("type", "rss")
    fetcher = FETCHERS.get(ptype, fetch_rss)
    try:
        items = fetcher(portal)
        # Apply keyword filter defined per-portal (for general job boards)
        filter_keywords = portal.get("filter_keywords", [])
        if filter_keywords:
            before = len(items)
            items = _filter_by_keywords(items, filter_keywords)
            logger.info("%s: keyword filter kept %d/%d jobs", portal.get("name", "?"), len(items), before)
        return items
    except Exception as e:
        logger.warning("Portal %s (%s) failed: %s", portal.get("name", "?"), ptype, e)
        return []
