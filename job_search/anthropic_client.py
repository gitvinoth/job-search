from __future__ import annotations

import re

import anthropic

from job_search.config import Settings
from job_search.llm_common import MAX_HTML_CHARS, match_prompt, summarize_prompt, truncate


class AnthropicLLMClient:
    """Anthropic Claude API — use when Gemini quota is exhausted (set LLM_PROVIDER=anthropic)."""

    def __init__(self, settings: Settings) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model

    def _generate(self, prompt: str) -> str:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        if not msg.content:
            return ""
        block = msg.content[0]
        return (getattr(block, "text", None) or "").strip()

    def summarize_job_html(
        self,
        *,
        job_role: str,
        location: str,
        html_or_text: str,
    ) -> str:
        body = truncate(html_or_text, MAX_HTML_CHARS)
        prompt = summarize_prompt(job_role=job_role, location=location, body=body)
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
        prompt = match_prompt(
            job_summary=job_summary,
            job_role=job_role,
            location=location,
            resume_summary=resume_summary,
        )
        raw = self._generate(prompt)
        m = re.search(r"\b(\d{1,3})\b", raw)
        if not m:
            return 0
        n = int(m.group(1))
        return max(0, min(100, n))
