from __future__ import annotations

import re

from job_search.llm_common import MAX_HTML_CHARS, truncate

# Minimal English stopwords for keyword overlap (no external NLP deps).
_STOP = frozenset(
    "a an the and or but in on at to for of is are was were be been being "
    "it this that these those as by with from into through during before after "
    "above below between under again further then once here there when where "
    "why how all both each few more most other some such no nor not only own "
    "same so than too very can will just don should now".split()
)


def _strip_html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&\w+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _tokenize(text: str) -> set[str]:
    words: set[str] = set()
    for m in re.finditer(r"[a-z0-9][a-z0-9_+.#-]{1,}", text.lower()):
        w = m.group(0)
        if w not in _STOP and len(w) > 2:
            words.add(w)
    return words


class LocalHeuristicLLM:
    """
    Zero-cost provider: no API keys, no network for inference.
    Summary = truncated plain text from HTML/body; match = keyword overlap vs resume.
    Quality is lower than real LLMs but sufficient for triage.
    """

    def summarize_job_html(
        self,
        *,
        job_role: str,
        location: str,
        html_or_text: str,
    ) -> str:
        plain = _strip_html_to_text(truncate(html_or_text, MAX_HTML_CHARS))
        if not plain:
            plain = f"{job_role} | {location}"
        prefix = f"{job_role} @ {location}: "
        room = 215 - len(prefix)
        if room < 24:
            out = plain[:215]
        else:
            snippet = plain[:room].rsplit(" ", 1)[0] if len(plain) > room else plain
            out = prefix + snippet
        if len(out) > 215:
            out = out[:212].rstrip() + "…"
        return out

    def match_score(
        self,
        *,
        job_summary: str,
        job_role: str,
        location: str,
        resume_summary: str,
    ) -> int:
        job_text = f"{job_summary} {job_role} {location}"
        job_w = _tokenize(job_text)
        res_w = _tokenize(resume_summary)
        if not job_w or not res_w:
            return 0
        inter = job_w & res_w
        # F1-style balance so short lists don't always score 100
        p = len(inter) / len(job_w)
        r = len(inter) / len(res_w)
        if p + r == 0:
            return 0
        f1 = 2 * p * r / (p + r)
        return min(100, int(round(f1 * 100)))
