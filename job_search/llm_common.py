from __future__ import annotations

MAX_HTML_CHARS = 100_000


def truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[truncated]"


def summarize_prompt(*, job_role: str, location: str, body: str) -> str:
    return (
        f"Target role context: {job_role} | {location}\n\n"
        f"Given the following HTML or text from a job posting:\n{body}\n\n"
        "Task: Output only a concise summary (max 215 characters) with core technologies "
        "and primary responsibilities. If the input is unusable, summarize any readable plain text."
    )


def match_prompt(
    *,
    job_summary: str,
    job_role: str,
    location: str,
    resume_summary: str,
) -> str:
    return (
        f'Job summary: {job_summary}\n'
        f'Advertised context: role "{job_role}", location "{location}".\n\n'
        f"Compare against this resume summary:\n{resume_summary}\n\n"
        "Task: Job Match Percentage 0-100 for how well the profile fits this role "
        "(skills, level, domain).\n"
        "Return ONLY the integer (e.g. 85). No other text."
    )
