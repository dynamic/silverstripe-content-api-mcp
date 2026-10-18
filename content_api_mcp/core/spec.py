"""Pinned schema/endpoints.json access — spec loading and the base route.

Its own module so client.py can validate a site's base_url against the
spec's baseRoute without a circular import: tools.py imports client.py (for
ContentClient), so client.py importing tools.py would cycle. This module
imports nothing from the package, so both can import it freely.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

# content_api_mcp/schema/, shipped as package data (pyproject.toml), so the
# path resolves the same in a checkout and in an installed package. It holds
# endpoints.json and module-ref, the module commit it was copied from.
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "endpoints.json"


def load_spec(schema_path: Path = SCHEMA_PATH) -> dict[str, Any]:
    """Load the pinned schema/endpoints.json copy.

    Re-sync with scripts/sync-spec.sh whenever dynamic/silverstripe-content-api
    ships a schema change; scripts/check-spec-sync.sh and
    scripts/check-module-ref.sh (local-ci) catch drift and a stale
    module-ref.
    """
    return json.loads(schema_path.read_text())


@cache
def base_route(schema_path: Path = SCHEMA_PATH) -> str:
    """The spec's top-level baseRoute, normalized to no leading/trailing slash.

    Every configured site's base_url is required to end with this (see
    content_api_mcp.core.client.parse_site_registry) — a base_url that omits it boots
    clean and 404s every call (daisy-content#11). Raises rather than
    defaulting to "" on a missing/blank baseRoute, so a malformed spec fails
    loudly instead of silently disabling that check.

    Cached per schema_path (this repo's pinned schema/endpoints.json isn't
    expected to change without a redeploy) — parse_site_registry calls this
    once per configured site, so re-parsing the same JSON file per site
    would be wasted work.
    """
    spec = load_spec(schema_path)
    if not isinstance(spec, dict):
        raise ValueError(
            f"{schema_path} does not contain a JSON object at the top level"
        )
    route = spec.get("baseRoute")
    # Normalize before the emptiness check, not after — a baseRoute of "/"
    # or "///" strips to "" too, and checking pre-strip would let that slip
    # past as "non-empty" while still returning "" (the exact silent-"" this
    # function exists to prevent).
    normalized = route.strip().strip("/") if isinstance(route, str) else route
    if not isinstance(normalized, str) or not normalized:
        raise ValueError(
            f"{schema_path} has no non-empty top-level 'baseRoute' — cannot "
            "validate configured sites' base_url against it"
        )
    return normalized
