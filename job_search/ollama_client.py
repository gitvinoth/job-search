from __future__ import annotations

import re

import requests

from job_search.config import Settings
from job_search.llm_common import MAX_HTML_CHARS, match_prompt, summarize_prompt, truncate


def _message_text(data: dict) -> str:
    msg = data.get("message") or {}
    c = msg.get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        parts: list[str] = []
        for block in c:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts).strip()
    return ""


class OllamaLLMClient:
    """
    Local Ollama HTTP API — free aside from your own machine / electricity.
    Install: https://ollama.com — then e.g. `ollama pull llama3.2`
    """

    def __init__(self, settings: Settings) -> None:
        self._url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        self._model = settings.ollama_model

    def _generate(self, prompt: str) -> str:
        r = requests.post(
            self._url,
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            timeout=300,
        )
        r.raise_for_status()
        return _message_text(r.json())

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
