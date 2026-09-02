"""HTTP client for a single site's /content-api/v1 surface.

All request-building lives here: given a spec tool entry (from
schema/endpoints.json) and the arguments an MCP client passed, build the
right HTTP call — {path} substitution, GET querystring vs POST JSON body —
and inject the site's token header. server.py stays registration-only.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from content_api_mcp import __version__
from content_api_mcp._base import create_http_session
from content_api_mcp._base.errors import AuthenticationError, MCPError, ServiceError
from content_api_mcp.settings import ContentApiSettings

LOGGER = logging.getLogger(__name__)

# Derived from __version__ (not hardcoded) so a version bump can't leave this
# silently stale — see content_api_mcp/__init__.py and pyproject.toml.
USER_AGENT = f"content-api-mcp/{__version__}"

_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")

# A filePath-resolved upload is read fully into memory, then base64-encoded
# (~33% larger again) before being sent — cap well under typical asset sizes
# so an oversized file fails fast and clearly instead of a slow, memory-heavy
# request that likely just times out anyway.
MAX_FILE_PATH_BYTES = 25 * 1024 * 1024  # 25 MiB

# Filename patterns that are essentially never a legitimate upload asset and
# very plausibly a credential/secret file — rejected as a defense-in-depth
# guard against "filePath" being pointed at something sensitive (by mistake,
# or via a compromised/injected tool argument). This is NOT a security
# boundary by itself — an MCP host already runs with the same filesystem
# access as the agent invoking it, and this list is trivially bypassed by
# renaming a file. The actual boundary is not letting untrusted input control
# tool arguments in the first place; this just closes the easy, accidental
# case and gives a caller a clear error instead of a silent exfiltration.
_SENSITIVE_PATH_PATTERNS = (
    re.compile(r"(^|/)\."),  # any dotfile/dotdir path component (.ssh, .env, .aws, .git, ...)
    re.compile(r"id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$", re.IGNORECASE),
    re.compile(r"\.(pem|key|pfx|p12|ppk)$", re.IGNORECASE),
)


def _looks_sensitive(path: Path) -> bool:
    text = str(path)
    return any(pattern.search(text) for pattern in _SENSITIVE_PATH_PATTERNS)


class ContentApiClient:
    """Thin HTTP proxy: one spec entry + arguments in, parsed JSON out."""

    def __init__(self, settings: ContentApiSettings):
        self._settings = settings
        self._session = create_http_session(user_agent=USER_AGENT)
        # No auth header set here (#23) — settings.token was resolved once
        # at process construction and never changes, which is exactly the
        # problem: a re-minted token (a real, routine event — see
        # ContentApiSettings.current_token()'s docblock) had no way to reach
        # a running session. The header is built fresh per request in
        # call() instead, via current_token(), which re-reads
        # CONTENT_API_TOKEN_FILE when configured.

    def call(self, tool_entry: dict[str, Any], arguments: dict[str, Any] | None) -> Any:
        """Execute one spec-defined tool call against the site."""
        arguments = self._resolve_file_path(tool_entry, dict(arguments or {}))
        path, remaining = self._resolve_path(tool_entry["path"], arguments)
        url = f"{self._settings.base_url.rstrip('/')}/{path.lstrip('/')}"
        method = tool_entry["method"].upper()

        # Custom header, raw token value — NOT "Authorization: Bearer ..."
        # (colymba's TokenAuthenticator.tokenHeader; see ContentApiController::checkAuth).
        # Resolved fresh per call (#23), not cached on the session — see
        # ContentApiSettings.current_token().
        headers: dict[str, str] = {self._settings.header: self._settings.current_token()}

        request_kwargs: dict[str, Any] = {
            "timeout": self._settings.timeout,
            # The content-api surface never redirects in normal operation.
            # `requests` only strips Authorization/Cookie on a cross-host
            # redirect — a custom header like X-Silverstripe-Apitoken (set
            # per-request above) would be preserved and re-sent to the
            # redirect target regardless of which level set it. Disable
            # following so a misconfigured base URL or an open redirect
            # can't egress the token, and any 3xx surfaces as an error in
            # _parse_response instead of silently chasing it.
            "allow_redirects": False,
        }
        if self._settings.ca_file:
            # Per-request rather than session.verify, deliberately (#32):
            # requests only consults REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE when
            # the request-level verify is unset, and a request-level value
            # beats session.verify in merge_environment_settings — so this
            # is the one placement where an explicit CONTENT_API_CA_FILE
            # always wins over whatever bundle vars the environment happens
            # to export. Path validated/expanded at construction, see
            # ContentApiSettings._resolve_ca_file.
            request_kwargs["verify"] = self._settings.ca_file
        if method == "GET":
            request_kwargs["params"] = self._flatten_query(remaining)
        else:
            request_kwargs["json"] = remaining
            headers["Content-Type"] = "application/json"
        request_kwargs["headers"] = headers

        LOGGER.debug("content-api %s %s", method, url)
        response = self._session.request(method, url, **request_kwargs)
        return self._parse_response(response)

    @staticmethod
    def _resolve_file_path(
        tool_entry: dict[str, Any], arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Materialize a spec-declared "filePath" into "base64", read locally.

        Gated on the tool entry's own inputSchema declaring "filePath" (only
        content_asset_upload's does today) rather than hardcoding a tool
        name, so it's a no-op — and enforces nothing — for every endpoint
        that doesn't opt into this. For an endpoint that does, exactly one of
        "base64"/"filePath" is required; see module issue #39 on
        dynamic/silverstripe-content-api for why "filePath" exists at all —
        a chunked reassembly of a large base64 string into an agent's own
        output can silently corrupt the file while still "succeeding" (an
        image's dimensions are read from its header, which can parse fine
        even when the body itself is truncated/garbled).

        Resolution happens entirely on this process's filesystem (the
        machine running the MCP host) and "filePath" is popped before the
        request is built — the upstream API never receives it, only the
        "base64" it already expected.
        """
        supports_file_path = "filePath" in tool_entry.get("inputSchema", {}).get("properties", {})

        if not supports_file_path:
            return arguments

        file_path = arguments.pop("filePath", None)
        # Presence, not truthiness: a caller who explicitly sends base64=""
        # alongside filePath has supplied a conflicting payload regardless of
        # whether that value happens to be falsy, and should get the
        # mutual-exclusion error below, not have it silently overwritten.
        has_base64 = arguments.get("base64") is not None

        if file_path is not None and has_base64:
            raise MCPError(
                'Provide either "filePath" or "base64", not both.',
                status_code=400,
            )

        if file_path is None:
            if not has_base64:
                raise MCPError(
                    'Provide one of "filePath" or "base64".',
                    status_code=400,
                )
            return arguments

        path = Path(file_path).expanduser()

        if not path.is_file():
            raise MCPError(
                f'filePath "{file_path}" does not exist or is not a file.',
                status_code=400,
            )

        if _looks_sensitive(path):
            raise MCPError(
                f'filePath "{file_path}" looks like a credential/secret file, not an '
                'upload asset — refusing to read it. If this is a legitimate asset, '
                'rename it or pass its content via "base64" instead.',
                status_code=400,
            )

        size = path.stat().st_size
        if size > MAX_FILE_PATH_BYTES:
            raise MCPError(
                f'filePath "{file_path}" is {size} bytes, over the '
                f"{MAX_FILE_PATH_BYTES}-byte limit for filePath uploads — pass "
                '"base64" directly if you need to upload something larger.',
                status_code=400,
            )

        arguments["base64"] = base64.b64encode(path.read_bytes()).decode("ascii")

        return arguments

    @staticmethod
    def _resolve_path(
        path_template: str, arguments: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Substitute {param} segments from arguments; return (path, leftover args).

        Consumed keys are popped so they never leak into the query string or
        JSON body (e.g. content_records_stage's classRef/id/action belong to
        the path, while `recursive` belongs to the body). Substitution is a
        single regex pass over the pristine template — never over an
        already-substituted string — so one argument's value can't contain
        another argument's `{token}` text and get re-substituted. Each value
        is percent-encoded as an opaque path segment so it can't introduce
        extra path segments or a stray `?`/`#` that would change the
        request's shape. `:` is kept safe/unescaped — the API's `ext:<id>`
        convention (see MintApiTokenTask/schema `id` docs) is matched
        server-side against the literal, undecoded segment, so encoding it
        to `%3A` would break every `ext:` lookup.
        """
        remaining = dict(arguments)
        missing: list[str] = []

        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in remaining:
                missing.append(key)
                return match.group(0)
            return quote(str(remaining.pop(key)), safe=":")

        path = _PATH_PARAM_RE.sub(substitute, path_template)
        if missing:
            raise MCPError(
                f"missing required argument(s) for path template: {', '.join(missing)}",
                status_code=400,
            )
        return path, remaining

    @staticmethod
    def _flatten_query(arguments: dict[str, Any]) -> dict[str, Any]:
        """Flatten the `filters` object into Field / Field__Modifier query params.

        Other GET args (sort, limit, offset, _stage) pass through as-is and
        always take precedence over a same-named key inside `filters` —
        applied after filters regardless of argument order, so a filters
        key that happens to collide with a reserved param name (e.g. a
        `_stage` field) can never silently override it.
        """
        filters = arguments.get("filters")
        params: dict[str, Any] = dict(filters) if isinstance(filters, dict) else {}
        for key, value in arguments.items():
            if key == "filters" or value is None:
                continue
            params[key] = value
        return params

    @staticmethod
    def _parse_response(response) -> Any:
        # allow_redirects=False means a 3xx reaches here as the final
        # response, not something `requests` already chased — surface it
        # rather than treating it as any kind of success (see call()).
        if 300 <= response.status_code < 400:
            raise ServiceError(
                f"content-api returned an unexpected redirect "
                f"({response.status_code} -> {response.headers.get('Location', '?')}); "
                "redirects are not followed",
                status_code=response.status_code,
            )

        text = response.text
        try:
            # `payload is None` can't be used as the "parse failed" sentinel
            # below — a body of the literal JSON `null` parses successfully
            # to None too, and must stay a valid (empty) success rather than
            # being mistaken for an unparseable body.
            payload = response.json()
            parse_failed = False
        except ValueError:
            payload = None
            parse_failed = True

        if response.ok:
            # A 2xx whose body didn't parse as JSON is not a valid empty
            # success — the realistic trigger is a misconfigured base URL or
            # an auth/routing redirect landing on an HTML page (requests.json()
            # raises ValueError, and this used to fall through to {}). Only a
            # genuinely empty body counts as an empty success; any non-empty
            # body that fails to parse is an error, not silent success.
            if parse_failed and text.strip():
                content_type = response.headers.get("Content-Type", "(none)")
                raise ServiceError(
                    f"content-api returned a {response.status_code} response with a "
                    f"non-JSON body (Content-Type: {content_type}): {text[:300]}",
                    status_code=response.status_code,
                )
            return payload if payload is not None else {}

        message, code, details = ContentApiClient._extract_error(payload, response)
        if response.status_code in (401, 403):
            raise AuthenticationError(
                message, status_code=response.status_code, error_code=code, details=details
            )
        if response.status_code >= 500:
            raise ServiceError(
                message, status_code=response.status_code, error_code=code, details=details
            )
        raise MCPError(
            message, status_code=response.status_code, error_code=code, details=details
        )

    @staticmethod
    def _extract_error(
        payload: Any, response
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        """Unwrap the content-api error envelope: {"error": {code, status, message, details?}}."""
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get("message"):
            code = error.get("code")
            message = str(error["message"])
            details = error.get("details")
            display_message = f"{message} (details: {details})" if details else message
            return (
                (f"{code}: {display_message}" if code else display_message),
                code,
                details,
            )
        return (
            f"content-api request failed ({response.status_code}): {response.text[:300]}",
            None,
            None,
        )
