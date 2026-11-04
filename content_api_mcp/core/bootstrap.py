"""content_get_started: one call that orients an agent on a site (daisy-content#18).

Composes `auth/session` and the (cached) `schema/site` into one short
summary: who the token is and what it may do, the environment and its write
gates, the detected integrations, which class refs are pages and which are
elements, and the order of calls that builds a page. It summarizes and
points at `content_schema_class` for per-class detail rather than returning
every contract, so the response stays a few KB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from content_api_mcp._base.errors import ValidationError
from content_api_mcp.core.client import ContentClient
from content_api_mcp.core.shaping import concise_schema_site

_FAST_TIMEOUT = 10

#: A token with fewer days than this left is reported as expiring
#: (`content_get_started` warning, `content_check_sites` status).
EXPIRY_WARN_DAYS = 7

ACCESS_PERMISSION = "CONTENT_API_ACCESS"
POPULATE_PERMISSION = "CONTENT_API_POPULATE"
DRAFT_PERMISSION = "VIEW_DRAFT_CONTENT"

NEXT_STEPS = [
    "content_schema_class(classRef) for each page and element class you will "
    "write: it lists the writable fields; an unknown or read-only field is "
    "rejected with 422 UNKNOWN_FIELD / READONLY_FIELD.",
    "Build a page in one call with content_compose_page (page match or "
    "createIfMissing, elements, inline assets). Every element needs an "
    "externalId: it is the idempotency and prune key, so a repeat call "
    "updates the element instead of adding a copy.",
    "Use content_batch with dryRun: true to check a multi-record change before "
    "running it for real.",
    'Publish with content_compose_page\'s publish: "recursive", or '
    'content_records_stage on the page with action "publish" and mode "owns" '
    '(the default mode, "single", publishes the page row only, not its '
    "elements); confirm with content_records_parity.",
    "To change one field on an existing record (also on a live site), use "
    "content_records_update.",
]


def resolve_site(client: ContentClient, site: str | None) -> str:
    """The requested slug, or the only configured one when none is given."""
    if site:
        return site
    default = client.default_site()
    if default is None:
        configured = ", ".join(client.list_sites()) or "(none configured)"
        raise ValidationError(
            f'Missing required "site" argument. Configured sites: {configured}'
        )
    return default


def _iso(timestamp: Any) -> str | None:
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
    return None


def days_left(timestamp: Any, now: datetime | None = None) -> float | None:
    """Unrounded days from `now` to an `auth/session` `expires` epoch.

    Negative once past; None when the site reported no usable expiry. Decide
    expired / expiring on this, not on the rounded display value: a token with
    an hour left must not read as expired.
    """
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        return None
    now = now or datetime.now(UTC)
    return (timestamp - now.timestamp()) / 86400


def expires_in_days(timestamp: Any, now: datetime | None = None) -> float | None:
    """`days_left` rounded to one decimal, for display."""
    days = days_left(timestamp, now)
    return None if days is None else round(days, 1)


def _expiry_warning(days: float | None, expires_at: str | None) -> str | None:
    if days is None:
        return None
    if days <= 0:
        return (
            f"The token expired {expires_at}: every call for this site fails "
            "401 until a new token is minted and installed."
        )
    if days < EXPIRY_WARN_DAYS:
        return (
            f"The token expires in {round(days, 1):g} days ({expires_at}): mint a new one "
            "before then, or every call for this site starts failing 401."
        )
    return None


def _warnings(
    session: dict, schema: dict, now: datetime | None = None
) -> list[str]:
    permissions = session.get("permissions")
    permissions = permissions if isinstance(permissions, list) else []
    warnings = []
    expiry = _expiry_warning(
        days_left(session.get("expires"), now), _iso(session.get("expires"))
    )
    if expiry:
        warnings.append(expiry)
    if ACCESS_PERMISSION not in permissions:
        warnings.append(
            f"The token's member lacks {ACCESS_PERMISSION}: the schema reads "
            "answer, but every records, batch, composition and asset call "
            "returns 403."
        )
    if schema.get("populationEnabled") is False:
        still = (
            "content_records_update and content_records_stage still work."
            if schema.get("recordUpdateEnabled") is not False
            else "content_records_stage still works."
        )
        warnings.append(
            "populationEnabled is false: content_batch, content_compose_page, "
            f"page actions and asset writes return 403 ENV_FORBIDDEN here. {still}"
        )
    if schema.get("recordUpdateEnabled") is False:
        warnings.append(
            "recordUpdateEnabled is false: content_records_update returns 403 "
            "ENV_FORBIDDEN here."
        )
    if POPULATE_PERMISSION not in permissions:
        warnings.append(
            f"The token's member lacks {POPULATE_PERMISSION}, which batch, "
            "compositions, asset writes and page actions require."
        )
    if DRAFT_PERMISSION not in permissions:
        warnings.append(
            f"The token's member lacks {DRAFT_PERMISSION}, so draft-only records "
            "are invisible to it."
        )
    return warnings


def get_started(
    client: ContentClient, site: str | None, now: datetime | None = None
) -> dict:
    """Session + site schema summary + recipe for one site.

    `now` is the reference time for the expiry fields (tests); default now (UTC).
    """
    site = resolve_site(client, site)
    session_response = client.get(site, "auth/session", timeout=_FAST_TIMEOUT)
    schema_response = client.get(site, "schema/site")

    session = (
        session_response.get("data") if isinstance(session_response, dict) else None
    )
    session = session if isinstance(session, dict) else {}
    schema = schema_response.get("data") if isinstance(schema_response, dict) else None
    schema = schema if isinstance(schema, dict) else {}
    summary = concise_schema_site(schema)

    return {
        "site": site,
        "session": {
            "memberId": session.get("memberId"),
            "email": session.get("email"),
            "permissions": session.get("permissions"),
            "expires": session.get("expires"),
            "expiresAt": _iso(session.get("expires")),
            "expiresInDays": expires_in_days(session.get("expires"), now),
        },
        "environment": schema.get("environment"),
        "populationEnabled": schema.get("populationEnabled"),
        "recordUpdateEnabled": schema.get("recordUpdateEnabled"),
        "integrations": schema.get("integrations"),
        "classes": summary.get("classes"),
        "warnings": _warnings(session, schema, now),
        "nextSteps": NEXT_STEPS,
    }
