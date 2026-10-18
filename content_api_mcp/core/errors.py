"""Render a raised error as tool-result text an agent can branch on (daisy-content#30).

FastMCP turns any exception a tool raises into a `CallToolResult` with
`isError: true` and a single text block holding `str(exception)`; a
`ToolError` passes through with its own text unchanged. Exception
attributes (`.details`, `.error_code`, `.status_code`) never cross the
`tools/call` boundary, and the DAISY gateway's proxy client keeps only that
first text block of an error result too. So the structured part of an error
has to live in the text itself.

`render_tool_error` keeps the human-readable message first and appends one
trailer line:

    content-error: {"kind": "authorization_failed", "status": 403,
                    "error_code": "ENV_FORBIDDEN", "details": [...]}

(on a single line). The line always starts with ERROR_TRAILER_PREFIX and the
rest of it is a JSON object, so a caller finds the last line with that prefix
and `json.loads` what follows. The JSON is ASCII-only (every non-ASCII
character is a `\\uXXXX` escape), so no character inside it can act as a
line break, and it never contains NaN or Infinity. Keys:

- `kind`: this server's error class: `authentication_failed`,
  `authorization_failed`, `validation_error`, `not_found`, `conflict`,
  `rate_limit_exceeded`, `service_error`; `request_failed` for a transport
  failure with no usable response, `unexpected_redirect` for a refused 3xx.
  An upstream 4xx without a more specific class (400, 422, ...) is
  `validation_error`; a 409 is `conflict`.
- `status`: the HTTP status the error maps to, or null when no response
  came back.
- `error_code`: the content-api envelope's own code (`ENV_FORBIDDEN`,
  `UNKNOWN_FIELD`, `ROLLBACK_UNVERIFIED`, ...), omitted when the upstream
  body supplied none.
- `retry_after`: the 429 `Retry-After` header value, as a string, when there
  was one.
- `valid_options`: the values that would have worked, when this server
  knows them (the site's exposed class refs after an `UNKNOWN_CLASS`, daisy-content#21),
  capped at MAX_TRAILER_DETAILS_LEN characters; `valid_options_truncated:
  true` when the list was cut.
- `details`: the envelope's `details` value as JSON, omitted when absent.
  When its serialized form is longer than MAX_TRAILER_DETAILS_LEN, or holds
  a value JSON can't represent, `details` is replaced by
  `details_truncated: true` plus `details_text`, a capped string.
"""

from __future__ import annotations

import json
from typing import Any

from content_api_mcp._base.errors import MCPError

ERROR_TRAILER_PREFIX = "content-error: "

# Budget for the serialized `details` value. A field-validation envelope
# for a large composition can list one entry per rejected field; past this
# the trailer carries a capped string instead, so one error can't fill a
# chat context on its own.
MAX_TRAILER_DETAILS_LEN = 2000


class ContentTransportError(RuntimeError):
    """No usable HTTP response: a transport failure (`status_code` None) or a
    3xx redirect this client refuses to follow (`status_code` the 3xx).

    A RuntimeError subclass so code that caught the plain RuntimeError
    ContentClient used to raise keeps working.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _kind(error: Exception) -> str:
    if isinstance(error, ContentTransportError):
        return "unexpected_redirect" if error.status_code else "request_failed"
    if not isinstance(error, MCPError):
        return "request_failed"
    status = error.status_code
    # A bare MCPError keeps mcp_base's class default "internal_error"
    # whatever status it carries; ContentClient raises one for every upstream
    # 4xx without its own class and for its own input checks.
    if type(error) is MCPError and isinstance(status, int) and 400 <= status < 500:
        return "conflict" if status == 409 else "validation_error"
    return error.error_code


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, allow_nan=False)


def error_payload(error: Exception) -> dict[str, Any]:
    """The structured fields for one raised error, as a JSON-safe dict."""
    status = getattr(error, "status_code", None)
    payload: dict[str, Any] = {
        "kind": _kind(error),
        "status": status if isinstance(status, int) else None,
    }
    if not isinstance(error, MCPError):
        return payload

    details = error.details if isinstance(error.details, dict) else {}
    if details.get("error_code") is not None:
        payload["error_code"] = details["error_code"]
    if details.get("retry_after") is not None:
        payload["retry_after"] = str(details["retry_after"])
    if isinstance(details.get("valid_options"), list):
        options = details["valid_options"]
        kept = []
        for option in options:
            if len(_dumps(kept + [option])) > MAX_TRAILER_DETAILS_LEN:
                break
            kept.append(option)
        payload["valid_options"] = kept
        if len(kept) < len(options):
            payload["valid_options_truncated"] = True
    if "upstream_details" in details:
        upstream = details["upstream_details"]
        try:
            serialized = _dumps(upstream)
        except ValueError:
            # NaN/Infinity: not representable in strict JSON.
            serialized = None
        if serialized is not None and len(serialized) <= MAX_TRAILER_DETAILS_LEN:
            payload["details"] = json.loads(serialized)
        else:
            text = json.dumps(upstream, default=str)
            payload["details_truncated"] = True
            payload["details_text"] = (
                text[:MAX_TRAILER_DETAILS_LEN] + "…(truncated)"
                if len(text) > MAX_TRAILER_DETAILS_LEN
                else text
            )
    return payload


def _safe_text(text: str) -> str:
    # A lone surrogate (e.g. decoded from an upstream "\ud800" escape) can't
    # be encoded as UTF-8, so the whole tool result would fail to serialize
    # and the caller would get no error text at all.
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def render_tool_error(error: Exception) -> str:
    """The message text, then the `content-error:` trailer line."""
    message = error.message if isinstance(error, MCPError) else str(error)
    trailer = _dumps(error_payload(error))
    return f"{_safe_text(message)}\n{ERROR_TRAILER_PREFIX}{trailer}"


def parse_tool_error(text: str) -> dict[str, Any] | None:
    """Inverse of render_tool_error's trailer, for tests and callers.

    Returns the trailer's JSON object from the LAST line (split on "\\n"
    only) starting with ERROR_TRAILER_PREFIX, or None when there is no such
    line or it isn't a JSON object. Tolerates a prefix FastMCP or a proxy
    adds ahead of the message, since only the final lines are read.
    """
    for line in reversed(text.split("\n")):
        if line.startswith(ERROR_TRAILER_PREFIX):
            try:
                parsed = json.loads(line[len(ERROR_TRAILER_PREFIX) :])
            except ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None
