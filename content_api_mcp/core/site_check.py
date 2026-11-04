"""content_check_sites: is each configured site reachable, and is its token good (#44).

One `auth/session` call per site with the fast timeout, run in parallel, so a
caller (an agent on a schedule, an operator) can ask "whose token is about to
expire" without calling every site by hand. One bad site never fails the call:
each site reports its own status.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Any

from content_api_mcp.core.bootstrap import (
    _FAST_TIMEOUT,
    EXPIRY_WARN_DAYS,
    _iso,
    days_left,
    expires_in_days,
)
from content_api_mcp.core.client import ContentClient
from content_api_mcp.core.errors import error_payload

#: Per-site statuses. `needs_attention` lists everything except `ok`.
OK = "ok"
EXPIRING = "expiring"
EXPIRED = "expired"
INVALID_TOKEN = "invalid_token"
UNREACHABLE = "unreachable"
ERROR = "error"
MAX_WORKERS = 8
#: The whole call returns within about this many seconds. A site still
#: unanswered by then is reported `unreachable` (kind `timeout`): the client's
#: retries (3 attempts at 10 s) or an operator's CONTENT_API_TIMEOUT could
#: otherwise keep one hung host past a gateway's tool timeout.
CHECK_DEADLINE_SECONDS = 25


def check_site(
    client: ContentClient, site: str, warn_days: float, now: datetime | None = None
) -> dict[str, Any]:
    """Status of one site's token: never raises.

    Args:
        client: The multi-site client.
        site: A configured slug.
        warn_days: Days left below which a token is `expiring`.
        now: Reference time (tests); defaults to now (UTC).

    Returns:
        ``{"site", "status", "reachable", ...}`` with ``expiresAt`` and
        ``expiresInDays`` when the site answered, and ``detail`` (the error
        kind, HTTP status and error code) when it did not.
    """
    row: dict[str, Any] = {"site": site, "reachable": False}
    try:
        response = client.get(site, "auth/session", timeout=_FAST_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - one site's failure is that site's row
        payload = error_payload(e)
        row["detail"] = {
            k: payload[k] for k in ("kind", "status", "error_code") if k in payload
        }
        if "error_code" in row["detail"]:
            row["detail"]["error_code"] = str(row["detail"]["error_code"])[:64]
        status = payload.get("status")
        if str(e).startswith("The token file"):
            # Raised before any request: the site was not asked. An empty file
            # is also what a token rewrite looks like for a moment.
            row["status"] = ERROR
            row["detail"] = {"kind": "token_file_unreadable"}
        elif status in (401, 403):
            row["reachable"] = True
            code = str(payload.get("error_code") or "")
            row["status"] = EXPIRED if "EXPIRED" in code.upper() else INVALID_TOKEN
        elif isinstance(status, int) and 400 <= status < 600:
            # The site answered, just not usefully (4xx other than auth, 5xx).
            row["reachable"] = True
            row["status"] = ERROR
        else:
            row["status"] = UNREACHABLE
        return row

    data = response.get("data") if isinstance(response, dict) else None
    data = data if isinstance(data, dict) else {}
    row["reachable"] = True
    days = days_left(data.get("expires"), now)
    row["expiresAt"] = _iso(data.get("expires"))
    row["expiresInDays"] = expires_in_days(data.get("expires"), now)
    if days is None:
        row["status"] = ERROR
        row["detail"] = {"kind": "no_expiry", "message": "auth/session had no expires"}
    elif days <= 0:
        row["status"] = EXPIRED
    elif days < warn_days:
        row["status"] = EXPIRING
    else:
        row["status"] = OK
    return row


def check_sites(
    client: ContentClient,
    warn_days: float = EXPIRY_WARN_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check every configured site's token.

    Args:
        client: The multi-site client.
        warn_days: Days left below which a token is `expiring` (default 7).
        now: Reference time (tests).

    Returns:
        ``{"checked_at", "warn_days", "sites": [...], "needs_attention": [slug]}``
        in slug order (`list_sites()`).

    Raises:
        ValueError: ``warn_days`` is not a positive number.
    """
    if isinstance(warn_days, bool) or not isinstance(warn_days, (int, float)):
        raise ValueError("warn_days must be a number of days")
    if not math.isfinite(warn_days) or warn_days <= 0:
        raise ValueError("warn_days must be greater than 0")
    slugs = client.list_sites()
    now = now or datetime.now(UTC)
    rows = _check_all(client, slugs, warn_days, now) if slugs else []
    return {
        "checked_at": now.isoformat(),
        "warn_days": warn_days,
        "sites": rows,
        "needs_attention": [r["site"] for r in rows if r["status"] != OK],
    }


def _check_all(
    client: ContentClient, slugs: list[str], warn_days: float, now: datetime
) -> list[dict[str, Any]]:
    """Run `check_site` for every slug in parallel under one overall deadline."""
    pool = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(slugs)))
    try:
        futures = {
            slug: pool.submit(check_site, client, slug, warn_days, now)
            for slug in slugs
        }
        wait(futures.values(), timeout=CHECK_DEADLINE_SECONDS)
        rows = []
        for slug in slugs:
            future = futures[slug]
            if future.done():
                rows.append(future.result())
            else:
                rows.append(
                    {
                        "site": slug,
                        "reachable": False,
                        "status": UNREACHABLE,
                        "detail": {
                            "kind": "timeout",
                            "message": f"no answer within {CHECK_DEADLINE_SECONDS}s",
                        },
                    }
                )
        return rows
    finally:
        # Do not wait for a hung site: its thread ends with its own timeout.
        pool.shutdown(wait=False, cancel_futures=True)
