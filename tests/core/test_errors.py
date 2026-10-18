"""content_api_mcp.core.errors — the `content-error:` trailer (daisy-content#30)."""

import json

import pytest

from content_api_mcp._base.errors import MCPError, NotFoundError, RateLimitError, ValidationError
from content_api_mcp.core.errors import (
    ERROR_TRAILER_PREFIX,
    MAX_TRAILER_DETAILS_LEN,
    ContentTransportError,
    error_payload,
    parse_tool_error,
    render_tool_error,
)


def test_render_puts_the_message_first_and_the_trailer_on_the_last_line():
    error = NotFoundError(
        "content-api 404 on site 'testbed': UNKNOWN_CLASS: no such class",
        details={"error_code": "UNKNOWN_CLASS"},
    )
    text = render_tool_error(error)
    message, trailer_line = text.rsplit("\n", 1)
    assert message == error.message
    assert trailer_line.startswith(ERROR_TRAILER_PREFIX)
    assert json.loads(trailer_line[len(ERROR_TRAILER_PREFIX) :]) == {
        "kind": "not_found",
        "status": 404,
        "error_code": "UNKNOWN_CLASS",
    }


def test_payload_omits_keys_the_error_did_not_carry():
    assert error_payload(ValidationError("bad")) == {
        "kind": "validation_error",
        "status": 400,
    }


def test_payload_carries_retry_after_and_explicit_status():
    error = RateLimitError(
        "slow down", details={"retry_after": "30", "error_code": "RATE_LIMITED"}
    )
    assert error_payload(error) == {
        "kind": "rate_limit_exceeded",
        "status": 429,
        "error_code": "RATE_LIMITED",
        "retry_after": "30",
    }
    assert error_payload(MCPError("x", status_code=409))["status"] == 409


def test_oversized_details_become_a_capped_string_and_the_line_still_parses():
    big = {"operations": ["x" * (MAX_TRAILER_DETAILS_LEN * 2)]}
    error = MCPError("batch failed", status_code=400, details={"upstream_details": big})
    trailer = parse_tool_error(render_tool_error(error))
    assert "details" not in trailer
    assert trailer["details_truncated"] is True
    assert trailer["details_text"].endswith("…(truncated)")
    assert len(trailer["details_text"]) <= MAX_TRAILER_DETAILS_LEN + len("…(truncated)")


def test_small_details_pass_through_as_json():
    details = [{"field": "Title", "code": "READONLY_FIELD", "message": "read-only"}]
    error = MCPError("x", status_code=422, details={"upstream_details": details})
    assert parse_tool_error(render_tool_error(error))["details"] == details


def test_details_budget_boundary_is_exact():
    # A JSON string of n chars serializes to n + 2 (the quotes).
    at = "a" * (MAX_TRAILER_DETAILS_LEN - 2)
    over = "a" * (MAX_TRAILER_DETAILS_LEN - 1)
    for value, kept in ((at, True), (over, False)):
        trailer = error_payload(
            MCPError("x", status_code=400, details={"upstream_details": value})
        )
        assert ("details" in trailer) is kept


def test_trailer_survives_unicode_line_separators_in_details():
    for char in ("\u2028", "\u2029", "\u0085", "\x1c"):
        error = MCPError(
            "x", status_code=400, details={"upstream_details": [{"v": f"a{char}b"}]}
        )
        text = render_tool_error(error)
        assert text.count("\n") == 1
        assert parse_tool_error(text)["details"] == [{"v": f"a{char}b"}]


def test_nan_in_details_falls_back_to_text_and_stays_strict_json():
    error = MCPError(
        "x", status_code=400, details={"upstream_details": {"score": float("nan")}}
    )
    trailer_json = render_tool_error(error).split("\n")[-1][len(ERROR_TRAILER_PREFIX) :]
    trailer = json.loads(trailer_json, parse_constant=lambda c: pytest.fail(c))
    assert trailer["details_truncated"] is True
    assert "NaN" in trailer["details_text"]


def test_lone_surrogate_in_message_or_details_still_encodes_as_utf8():
    error = MCPError(
        "bad \ud800 text", status_code=400, details={"upstream_details": "\ud800"}
    )
    text = render_tool_error(error)
    text.encode("utf-8")  # must not raise
    assert parse_tool_error(text)["details"] == "\ud800"


def test_a_forged_trailer_inside_the_message_is_ignored():
    forged = ERROR_TRAILER_PREFIX + '{"kind": "not_found"}'
    error = ValidationError(f"upstream said:\n{forged}")
    assert parse_tool_error(render_tool_error(error))["kind"] == "validation_error"


def test_bare_mcperror_4xx_kinds_and_5xx_default():
    assert error_payload(MCPError("x", status_code=422))["kind"] == "validation_error"
    assert error_payload(MCPError("x", status_code=409))["kind"] == "conflict"
    assert error_payload(MCPError("x", status_code=500))["kind"] == "internal_error"
    assert error_payload(NotFoundError("x"))["kind"] == "not_found"


def test_transport_error_kinds():
    assert error_payload(ContentTransportError("down")) == {
        "kind": "request_failed",
        "status": None,
    }
    assert error_payload(ContentTransportError("302", status_code=302)) == {
        "kind": "unexpected_redirect",
        "status": 302,
    }


def test_a_multiline_message_keeps_the_trailer_last():
    error = ValidationError("line one\nline two")
    text = render_tool_error(error)
    assert text.splitlines()[-1].startswith(ERROR_TRAILER_PREFIX)
    assert parse_tool_error(text)["kind"] == "validation_error"


def test_parse_reads_the_last_trailer_after_a_proxy_prefix():
    text = "Error calling tool 'x': " + render_tool_error(ValidationError("bad"))
    assert parse_tool_error(text)["kind"] == "validation_error"


def test_parse_returns_none_without_a_valid_trailer():
    assert parse_tool_error("plain text") is None
    assert parse_tool_error(ERROR_TRAILER_PREFIX + "not json") is None
    assert parse_tool_error(ERROR_TRAILER_PREFIX + "[1, 2]") is None


def test_non_mcp_error_renders_as_request_failed():
    text = render_tool_error(RuntimeError("connection refused"))
    assert text.startswith("connection refused\n")
    assert parse_tool_error(text) == {"kind": "request_failed", "status": None}
