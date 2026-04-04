from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Protocol

import requests

from job_search.airtable_client import AirtableClient
from job_search.config import Settings, load_settings
from job_search.rss_client import fetch_feed_items

logger = logging.getLogger(__name__)


class JobLLM(Protocol):
    def summarize_job_html(
        self, *, job_role: str, location: str, html_or_text: str
    ) -> str: ...

    def match_score(
        self,
        *,
        job_summary: str,
        job_role: str,
        location: str,
        resume_summary: str,
    ) -> int: ...


def _has_llm_credentials(settings: Settings) -> bool:
    if settings.llm_provider == "anthropic":
        return bool(settings.anthropic_api_key)
    return bool(settings.gemini_api_key)


def _make_llm(settings: Settings) -> JobLLM:
    if settings.llm_provider == "anthropic":
        from job_search.anthropic_client import AnthropicLLMClient

        return AnthropicLLMClient(settings)
    from job_search.gemini_client import GeminiClient

    return GeminiClient(settings)


@dataclass
class RunOptions:
    config_path: str
    dry_run: bool
    no_ai: bool
    max_jobs: int
    sleep_seconds: float
    skip_scrape: bool


def _fetch_page_text(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; JobSearchCollector/0.1; +https://github.com/gitvinoth/job-search)"
        ),
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=45)
    r.raise_for_status()
    return r.text or ""


def run(options: RunOptions) -> int:
    settings = load_settings(options.config_path)

    if not options.dry_run and not settings.airtable_token:
        logger.error("AIRTABLE_TOKEN is not set (required for live runs)")
        return 1

    if not options.dry_run and not options.no_ai:
        if settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
            logger.error(
                "ANTHROPIC_API_KEY is not set (or set LLM_PROVIDER=gemini with GEMINI_API_KEY, or use --no-ai)"
            )
            return 1
        if settings.llm_provider == "gemini" and not settings.gemini_api_key:
            logger.error(
                "GEMINI_API_KEY is not set (or set LLM_PROVIDER=anthropic with ANTHROPIC_API_KEY, or use --no-ai)"
            )
            return 1

    if options.dry_run and not options.no_ai and not _has_llm_credentials(settings):
        logger.warning("dry-run: no LLM API key for provider %s; using RSS text only", settings.llm_provider)
        options = replace(options, no_ai=True)

    try:
        items = fetch_feed_items(settings.rss_feed_url)
    except Exception as e:
        logger.error("RSS fetch failed: %s", e)
        return 1

    ai: JobLLM | None = None
    if not options.no_ai and _has_llm_credentials(settings):
        ai = _make_llm(settings)
        logger.info("LLM provider: %s", settings.llm_provider)

    at: AirtableClient | None = None
    if settings.airtable_token:
        at = AirtableClient(settings)
    elif not options.dry_run:
        logger.error("AIRTABLE_TOKEN is not set")
        return 1
    else:
        logger.warning("dry-run: AIRTABLE_TOKEN missing; duplicate check skipped")

    processed = 0
    for item in items:
        if processed >= options.max_jobs:
            break

        if at is not None:
            try:
                n = at.count_by_job_link(item.link)
            except Exception as e:
                logger.warning("Airtable lookup failed for %s: %s", item.link, e)
                continue
            if n > 0:
                logger.debug("Skip duplicate: %s", item.link)
                continue

        if options.sleep_seconds > 0 and processed > 0:
            time.sleep(options.sleep_seconds)

        html_or_text = item.description
        if not options.skip_scrape:
            try:
                html_or_text = _fetch_page_text(item.link)
            except Exception as e:
                logger.warning("HTTP fetch failed, falling back to RSS description: %s — %s", item.link, e)
                html_or_text = item.description

        summary = ""
        score = 0
        if options.no_ai or ai is None:
            summary = (item.description or "")[:215]
            score = 0
        else:
            try:
                summary = ai.summarize_job_html(
                    job_role=settings.job_role,
                    location=settings.location,
                    html_or_text=html_or_text,
                )
                score = ai.match_score(
                    job_summary=summary,
                    job_role=settings.job_role,
                    location=settings.location,
                    resume_summary=settings.resume_summary,
                )
            except Exception as e:
                logger.error("LLM failed for %s: %s", item.link, e)
                continue

        if options.dry_run:
            logger.info(
                "[dry-run] new job | %s | score=%s | summary=%r",
                item.link,
                score,
                summary[:80] + ("…" if len(summary) > 80 else ""),
            )
        elif at is not None:
            try:
                at.create_job_record(
                    job_link=item.link,
                    title=item.title,
                    search_role=settings.job_role,
                    search_location=settings.location,
                    summary=summary,
                    match_score=score,
                )
                logger.info("Stored: %s (match=%s)", item.title[:60], score)
            except Exception as e:
                logger.error("Airtable create failed for %s: %s", item.link, e)
                continue
        else:
            logger.error("Internal error: live run without Airtable client")
            return 1

        processed += 1

    logger.info("Done. Processed %s new job(s) (cap %s).", processed, options.max_jobs)
    return 0


def run_from_argv(argv: list[str] | None = None) -> int:
    import argparse

    from dotenv import load_dotenv

    load_dotenv()

    p = argparse.ArgumentParser(description="Job collector: RSS → scrape → LLM → Airtable")
    p.add_argument("--config", default="config.json", help="Path to config JSON")
    p.add_argument("--dry-run", action="store_true", help="Do not write to Airtable")
    p.add_argument("--no-ai", action="store_true", help="Skip LLM; summary from RSS only")
    p.add_argument("--max", type=int, default=25, dest="max_jobs", help="Max new jobs per run")
    p.add_argument(
        "--sleep",
        type=float,
        default=30.0,
        help="Seconds to sleep between successive job scrapes (rate limiting)",
    )
    p.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Do not HTTP-fetch job URL; use RSS description only",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    return run(
        RunOptions(
            config_path=args.config,
            dry_run=args.dry_run,
            no_ai=args.no_ai,
            max_jobs=args.max_jobs,
            sleep_seconds=args.sleep,
            skip_scrape=args.skip_scrape,
        )
    )
