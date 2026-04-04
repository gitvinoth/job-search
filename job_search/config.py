from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    job_role: str
    location: str
    rss_feed_url: str
    resume_summary: str
    airtable_base_id: str
    airtable_table_name: str
    llm_provider: str  # gemini | anthropic | ollama | local
    gemini_model: str
    anthropic_model: str
    ollama_base_url: str
    ollama_model: str
    airtable_token: str
    gemini_api_key: str
    anthropic_api_key: str


def _validate_rss_feed_url(url: str) -> None:
    """Reject template URLs so users get a clear message instead of HTTP 404."""
    u = url.lower()
    if "your-feed-id" in u or "example.com" in u or u.endswith("/your-feed-id.xml"):
        raise ValueError(
            "config.json: rss_feed_url is still a placeholder. Replace it with your real RSS/XML "
            "feed URL (e.g. from rss.app after you create a feed for your job search). "
            "Open the feed URL in a browser — it should show XML, not a 404 page."
        )


def _require_str(data: dict[str, Any], key: str) -> str:
    v = data.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"config.json: missing or empty string field {key!r}")
    return v.strip()


def load_settings(config_path: str | Path) -> Settings:
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("config.json must be a JSON object")

    job_role = _require_str(data, "job_role")
    location = _require_str(data, "location")
    rss_feed_url = _require_str(data, "rss_feed_url")
    _validate_rss_feed_url(rss_feed_url)
    resume_summary = _require_str(data, "resume_summary")
    airtable_base_id = _require_str(data, "airtable_base_id")
    airtable_table_name = _require_str(data, "airtable_table_name")

    # Default must be a model ID that exists on generativelanguage.googleapis.com v1beta for AI Studio keys.
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip()
    anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022").strip()
    ollama_base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip().rstrip("/")
    ollama_model = os.environ.get("OLLAMA_MODEL", "llama3.2").strip()
    airtable_token = os.environ.get("AIRTABLE_TOKEN", "").strip()
    gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    raw_provider = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
    allowed = ("gemini", "anthropic", "ollama", "local")
    llm_provider = raw_provider if raw_provider in allowed else "gemini"

    return Settings(
        job_role=job_role,
        location=location,
        rss_feed_url=rss_feed_url,
        resume_summary=resume_summary,
        airtable_base_id=airtable_base_id,
        airtable_table_name=airtable_table_name,
        llm_provider=llm_provider,
        gemini_model=gemini_model,
        anthropic_model=anthropic_model,
        ollama_base_url=ollama_base_url,
        ollama_model=ollama_model,
        airtable_token=airtable_token,
        gemini_api_key=gemini_api_key,
        anthropic_api_key=anthropic_api_key,
    )
