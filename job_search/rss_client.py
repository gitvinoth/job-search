from __future__ import annotations

from dataclasses import dataclass

import certifi
import feedparser
import requests


@dataclass(frozen=True)
class JobFeedItem:
    title: str
    link: str
    description: str


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
        items.append(JobFeedItem(title=title, link=link, description=desc))
    return items
