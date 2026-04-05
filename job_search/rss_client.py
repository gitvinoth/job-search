from __future__ import annotations

from dataclasses import dataclass, field

import certifi
import feedparser
import requests


@dataclass(frozen=True)
class JobFeedItem:
    title: str
    link: str
    description: str
    location: str = ""
    published: str = ""  # ISO-ish string, e.g. "2026-04-05 12:00"


def fetch_feed_items(feed_url: str) -> list[JobFeedItem]:
    """Fetch RSS over HTTPS using certifi's CA bundle (fixes many macOS verify failures)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; JobSearchCollector/0.1; +https://github.com/gitvinoth/job-search)"
        ),
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    }
    try:
        r = requests.get(
            feed_url,
            timeout=60,
            headers=headers,
            verify=certifi.where(),
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise ValueError(f"RSS HTTP request failed (check URL and network): {e}") from e

    parsed = feedparser.parse(r.content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise ValueError(
            f"Failed to parse RSS feed (check URL and body): {getattr(parsed, 'bozo_exception', 'unknown')}"
        )

    items: list[JobFeedItem] = []
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        title = (entry.get("title") or "").strip() or "(no title)"
        desc = ""
        if entry.get("summary"):
            desc = str(entry.summary).strip()
        elif entry.get("description"):
            desc = str(entry.description).strip()

        # Extract location from common RSS fields
        location = ""
        if entry.get("location"):
            location = str(entry.location).strip()
        elif entry.get("region"):
            location = str(entry.region).strip()

        # Extract published date
        published = ""
        if entry.get("published"):
            published = str(entry.published).strip()
        elif entry.get("updated"):
            published = str(entry.updated).strip()

        items.append(JobFeedItem(
            title=title, link=link, description=desc,
            location=location, published=published,
        ))
    return items
