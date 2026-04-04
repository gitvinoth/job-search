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
    gemini_model: str
    airtable_token: str
    gemini_api_key: str


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
    resume_summary = _require_str(data, "resume_summary")
    airtable_base_id = _require_str(data, "airtable_base_id")
    airtable_table_name = _require_str(data, "airtable_table_name")

    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-1.5-pro").strip()
    airtable_token = os.environ.get("AIRTABLE_TOKEN", "").strip()
    gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    return Settings(
        job_role=job_role,
        location=location,
        rss_feed_url=rss_feed_url,
        resume_summary=resume_summary,
        airtable_base_id=airtable_base_id,
        airtable_table_name=airtable_table_name,
        gemini_model=gemini_model,
        airtable_token=airtable_token,
        gemini_api_key=gemini_api_key,
    )
