from __future__ import annotations

from dataclasses import dataclass

import feedparser


@dataclass(frozen=True)
class JobFeedItem:
    title: str
    link: str
    description: str


def fetch_feed_items(feed_url: str) -> list[JobFeedItem]:
    parsed = feedparser.parse(feed_url)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise ValueError(
            f"Failed to parse RSS feed (check URL and network): {getattr(parsed, 'bozo_exception', 'unknown')}"
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
