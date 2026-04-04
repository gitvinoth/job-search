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


def _strip_html(raw: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&\w+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---- Portal registry -------------------------------------------------------

def fetch_rss(portal: dict) -> list[JobFeedItem]:
    """Generic RSS/Atom feed fetcher."""
    return fetch_feed_items(portal["url"])


def fetch_talent500(portal: dict) -> list[JobFeedItem]:
    """Scrape Talent500 via their XML sitemap, filtering by keywords."""
    url = portal["url"]
    search_m = re.search(r'search_term=([^&]+)', url)
    keywords = unquote_plus(search_m.group(1)).lower().split() if search_m else []
    # Drop non-alphanumeric tokens like "&" that won't match slugs
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
        items.append(JobFeedItem(
            title=f"{title_part} @ {company}",
            link=job_url,
            description=f"{title_part} — {company} (Talent500)",
        ))

    logger.info("Talent500: %d jobs matched (keywords=%s, total=%d)", len(items), keywords, len(all_urls))
    return items


def fetch_remoteok(portal: dict) -> list[JobFeedItem]:
    """RemoteOK RSS feed."""
    return fetch_feed_items(portal["url"])


def fetch_weworkremotely(portal: dict) -> list[JobFeedItem]:
    """We Work Remotely RSS feeds."""
    return fetch_feed_items(portal["url"])


def fetch_sitemap_generic(portal: dict) -> list[JobFeedItem]:
    """Generic sitemap scraper — for portals that expose a sitemap with job URLs.

    Config keys:
      sitemap_url: URL of the XML sitemap
      url_pattern: regex to match job URLs in the sitemap
      keywords: list of keywords to filter slugs (optional)
    """
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
        items.append(JobFeedItem(title=title, link=job_url, description=title))

    logger.info("%s sitemap: %d jobs (total=%d)", portal["name"], len(items), len(all_urls))
    return items


def fetch_html_scraper(portal: dict) -> list[JobFeedItem]:
    """Generic HTML scraper — extracts job links from a page.

    Config keys:
      url: page URL to scrape
      link_pattern: regex with group(1)=URL, optional group(2)=title
      base_url: prefix for relative URLs (optional)
    """
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

        items.append(JobFeedItem(title=title, link=link, description=title))

    logger.info("%s scraper: %d jobs from %s", portal["name"], len(items), url)
    return items


def fetch_noop(portal: dict) -> list[JobFeedItem]:
    """Placeholder for portals that need RSS.app or manual config."""
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
    "noop": fetch_noop,
}


def fetch_portal_jobs(portal: dict) -> list[JobFeedItem]:
    """Dispatch to the correct fetcher based on portal type."""
    ptype = portal.get("type", "rss")
    fetcher = FETCHERS.get(ptype, fetch_rss)
    try:
        return fetcher(portal)
    except Exception as e:
        logger.warning("Portal %s (%s) failed: %s", portal.get("name", "?"), ptype, e)
        return []
