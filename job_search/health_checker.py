"""Health checker — validates job source URLs and tracks per-source reliability."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (compatible; JobSearchHealthChecker/1.0; "
    "+https://github.com/gitvinoth/job-search)"
)
_HEADERS = {"User-Agent": _UA, "Accept": "*/*"}
_TIMEOUT = 15  # seconds per request


@dataclass
class HealthResult:
    source_id: str
    source_name: str
    url: str
    status: str          # "healthy" | "degraded" | "down" | "unknown"
    http_status: Optional[int]
    response_time_ms: Optional[float]
    error: Optional[str]
    checked_at: str

    @property
    def is_healthy(self) -> bool:
        return self.status in ("healthy", "degraded")

    def to_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_url(source_id: str, name: str, url: str) -> HealthResult:
    """Send a HEAD request (fallback GET) to validate a URL is reachable."""
    if not url:
        return HealthResult(
            source_id=source_id, source_name=name, url=url,
            status="unknown", http_status=None, response_time_ms=None,
            error="No URL configured", checked_at=_now_iso(),
        )

    start = time.monotonic()
    for method in ("HEAD", "GET"):
        try:
            r = requests.request(
                method, url, headers=_HEADERS,
                timeout=_TIMEOUT, allow_redirects=True,
            )
            elapsed = round((time.monotonic() - start) * 1000, 1)
            if r.status_code < 400:
                status = "healthy"
            elif r.status_code == 403:
                # 403 on HEAD is common for scrapers — try GET before marking down
                if method == "HEAD":
                    continue
                status = "degraded"
            elif r.status_code < 500:
                status = "degraded"
            else:
                status = "down"
            return HealthResult(
                source_id=source_id, source_name=name, url=url,
                status=status, http_status=r.status_code,
                response_time_ms=elapsed, error=None,
                checked_at=_now_iso(),
            )
        except requests.Timeout:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            return HealthResult(
                source_id=source_id, source_name=name, url=url,
                status="down", http_status=None,
                response_time_ms=elapsed, error="Timeout",
                checked_at=_now_iso(),
            )
        except requests.RequestException as exc:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            return HealthResult(
                source_id=source_id, source_name=name, url=url,
                status="down", http_status=None,
                response_time_ms=elapsed,
                error=str(exc)[:120],
                checked_at=_now_iso(),
            )
    # Fell through both HEAD and GET with 403
    elapsed = round((time.monotonic() - start) * 1000, 1)
    return HealthResult(
        source_id=source_id, source_name=name, url=url,
        status="degraded", http_status=403,
        response_time_ms=elapsed, error="403 Forbidden (scraping may be blocked)",
        checked_at=_now_iso(),
    )


def check_portals(portals: list[dict]) -> Dict[str, HealthResult]:
    """Check health for all portals and return results keyed by source id."""
    results: Dict[str, HealthResult] = {}
    for portal in portals:
        source_id = portal.get("id") or portal.get("name", "unknown")
        name = portal.get("name", source_id)
        url = portal.get("url", "")
        result = check_url(source_id, name, url)
        results[source_id] = result
        logger.info(
            "Health %-30s → %-10s %s ms",
            name, result.status,
            result.response_time_ms if result.response_time_ms is not None else "—",
        )
    return results


def update_portal_health(portal: dict, result: HealthResult) -> None:
    """Merge HealthResult into the portal dict's ``health`` sub-object (in-place)."""
    health = portal.setdefault("health", {
        "status": "unknown",
        "last_checked": None,
        "success_count": 0,
        "failure_count": 0,
        "last_response_time_ms": None,
    })
    health["status"] = result.status
    health["last_checked"] = result.checked_at
    health["last_response_time_ms"] = result.response_time_ms

    if result.is_healthy:
        health["success_count"] = health.get("success_count", 0) + 1
    else:
        health["failure_count"] = health.get("failure_count", 0) + 1

    total = health["success_count"] + health["failure_count"]
    if total > 0:
        portal["reliability_score"] = round(health["success_count"] / total, 2)
