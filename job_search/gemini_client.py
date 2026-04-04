from __future__ import annotations

import re

from google import genai

from job_search.config import Settings

MAX_HTML_CHARS = 100_000


def _response_text(response: object) -> str:
    try:
        return (getattr(response, "text", None) or "").strip()
    except Exception:
        return ""


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[truncated]"


class GeminiClient:
    def __init__(self, settings: Settings) -> None:
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_model

    def _generate(self, prompt: str) -> str:
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
        )
        return _response_text(response)

    def summarize_job_html(
        self,
        *,
        job_role: str,
        location: str,
        html_or_text: str,
    ) -> str:
        body = _truncate(html_or_text, MAX_HTML_CHARS)
        prompt = (
            f"Target role context: {job_role} | {location}\n\n"
            f"Given the following HTML or text from a job posting:\n{body}\n\n"
            "Task: Output only a concise summary (max 215 characters) with core technologies "
            "and primary responsibilities. If the input is unusable, summarize any readable plain text."
        )
        text = self._generate(prompt)
        return text[:215] if len(text) > 215 else text

    def match_score(
        self,
        *,
        job_summary: str,
        job_role: str,
        location: str,
        resume_summary: str,
    ) -> int:
        prompt = (
            f'Job summary: {job_summary}\n'
            f'Advertised context: role "{job_role}", location "{location}".\n\n'
            f"Compare against this resume summary:\n{resume_summary}\n\n"
            "Task: Job Match Percentage 0-100 for how well the profile fits this role "
            "(skills, level, domain).\n"
            "Return ONLY the integer (e.g. 85). No other text."
        )
        raw = self._generate(prompt)
        m = re.search(r"\b(\d{1,3})\b", raw)
        if not m:
            return 0
        n = int(m.group(1))
        return max(0, min(100, n))
