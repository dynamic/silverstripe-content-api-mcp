"""HTTP client for a single site's /content-api/v1 surface.

All request-building lives here: given a spec tool entry (from
schema/endpoints.json) and the arguments an MCP client passed, build the
right HTTP call — {path} substitution, GET querystring vs POST JSON body —
and inject the site's token header. server.py stays registration-only.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from mcp_base import create_http_session
from mcp_base.errors import AuthenticationError, MCPError, ServiceError

from content_api_mcp import __version__
from content_api_mcp.settings import ContentApiSettings

LOGGER = logging.getLogger(__name__)

# Derived from __version__ (not hardcoded) so a version bump can't leave this
# silently stale — see content_api_mcp/__init__.py and pyproject.toml.
USER_AGENT = f"content-api-mcp/{__version__}"

_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")


class ContentApiClient:
    """Thin HTTP proxy: one spec entry + arguments in, parsed JSON out."""

    def __init__(self, settings: ContentApiSettings):
        self._settings = settings
        self._session = create_http_session(user_agent=USER_AGENT)
        # Custom header, raw token value — NOT "Authorization: Bearer ..."
        # (colymba's TokenAuthenticator.tokenHeader; see ContentApiController::checkAuth).
        self._session.headers[settings.header] = settings.token

    def call(self, tool_entry: dict[str, Any], arguments: dict[str, Any] | None) -> Any:
        """Execute one spec-defined tool call against the site."""
        arguments = dict(arguments or {})
        path, remaining = self._resolve_path(tool_entry["path"], arguments)
        url = f"{self._settings.base_url.rstrip('/')}/{path.lstrip('/')}"
        method = tool_entry["method"].upper()

        request_kwargs: dict[str, Any] = {
            "timeout": self._settings.timeout,
            # The content-api surface never redirects in normal operation.
            # `requests` only strips Authorization/Cookie on a cross-host
            # redirect — a custom header like X-Silverstripe-Apitoken (set
            # once on the session in __init__) would be preserved and
            # re-sent to the redirect target. Disable following so a
            # misconfigured base URL or an open redirect can't egress the
            # token, and any 3xx surfaces as an error in _parse_response
            # instead of silently chasing it.
            "allow_redirects": False,
        }
        if method == "GET":
            request_kwargs["params"] = self._flatten_query(remaining)
        else:
            request_kwargs["json"] = remaining
            request_kwargs["headers"] = {"Content-Type": "application/json"}

        LOGGER.debug("content-api %s %s", method, url)
        response = self._session.request(method, url, **request_kwargs)
        return self._parse_response(response)

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
