from __future__ import annotations

import urllib.parse
from typing import Any

import requests

from job_search.config import Settings


def _airtable_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class AirtableClient:
    """Minimal Airtable REST client for duplicate search and record create."""

    JOB_LINK = "Job Link"
    TITLE = "Title"
    SEARCH_ROLE = "Search Role"
    SEARCH_LOCATION = "Search Location"
    SUMMARY = "Summary"
    MATCH_SCORE = "Match Score"

    def __init__(self, settings: Settings) -> None:
        self._base = f"https://api.airtable.com/v0/{settings.airtable_base_id}/{urllib.parse.quote(settings.airtable_table_name)}"
        self._headers = {
            "Authorization": f"Bearer {settings.airtable_token}",
            "Content-Type": "application/json",
        }

    def count_by_job_link(self, job_link: str) -> int:
        formula = f"{{{self.JOB_LINK}}} = {_airtable_string_literal(job_link)}"
        params = {"filterByFormula": formula, "maxRecords": 1}
        r = requests.get(self._base, headers=self._headers, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        records = data.get("records") or []
        return len(records)

    def create_job_record(
        self,
        *,
        job_link: str,
        title: str,
        search_role: str,
        search_location: str,
        summary: str,
        match_score: int,
    ) -> dict[str, Any]:
        fields = {
            self.JOB_LINK: job_link,
            self.TITLE: title,
            self.SEARCH_ROLE: search_role,
            self.SEARCH_LOCATION: search_location,
            self.SUMMARY: summary,
            self.MATCH_SCORE: match_score,
        }
        body = {"fields": fields}
        r = requests.post(self._base, headers=self._headers, json=body, timeout=60)
        r.raise_for_status()
        return r.json()
