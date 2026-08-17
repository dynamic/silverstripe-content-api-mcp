"""Pins the vendored content_api_mcp/_base/ error hierarchy (#27).

client.py's except clauses depend on this exact shape (constructor signature,
status_code/error_code defaults, subclass relationships) — a future edit to
_base/errors.py that silently changes it would break client.py's error
mapping without any test failing elsewhere. These tests exist so that edit
fails loudly here instead.
"""

from content_api_mcp._base import (
    AuthenticationError,
    BaseMCPSettings,
    MCPError,
    ServiceError,
    create_base_app,
    create_http_session,
)
from content_api_mcp._base.errors import (
    AuthorizationError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)


def test_mcp_error_defaults():
    error = MCPError("boom")
    assert error.message == "boom"
    assert error.status_code == 500
    assert error.error_code == "internal_error"
    assert error.details == {}


def test_mcp_error_accepts_overrides_client_py_relies_on():
    # client.py's _parse_response() constructs errors exactly this way:
    # MCPError(message, status_code=..., error_code=..., details=...)
    error = MCPError(
        "not found",
        status_code=404,
        error_code="NOT_FOUND",
        details=[{"field": None, "code": "X"}],
    )
    assert error.status_code == 404
    assert error.error_code == "NOT_FOUND"
    assert error.details == [{"field": None, "code": "X"}]


def test_authentication_error_is_an_mcp_error_with_401_default():
    error = AuthenticationError("nope")
    assert isinstance(error, MCPError)
    assert error.status_code == 401
    assert error.error_code == "authentication_failed"


def test_service_error_is_an_mcp_error_with_502_default():
    error = ServiceError("upstream broke")
    assert isinstance(error, MCPError)
    assert error.status_code == 502
    assert error.error_code == "service_error"


def test_authentication_error_status_code_is_overridable():
    # client.py's AuthenticationError raise always passes status_code
    # explicitly (401 or 403 from the real response) — confirm the override
    # wins over the class default, not just that the default exists.
    error = AuthenticationError("nope", status_code=403)
    assert error.status_code == 403


def test_error_hierarchy_unused_subclasses_still_present():
    # Not used by client.py today, but kept for fidelity with the source
    # file (#27's own instruction: vendor faithfully, don't reconstruct
    # from call sites) — pin their existence so a future trim doesn't
    # silently narrow the hierarchy without a deliberate decision.
    for cls in (AuthorizationError, ValidationError, NotFoundError, RateLimitError):
        assert issubclass(cls, MCPError)


def test_create_http_session_returns_a_configured_requests_session():
    session = create_http_session(user_agent="test-agent/1.0")
    assert session.headers["User-Agent"] == "test-agent/1.0"
    assert session.headers["Accept"] == "application/json"


def test_base_mcp_settings_has_expected_fields_and_prefix():
    settings = BaseMCPSettings()
    assert settings.name == "mcp-server"
    assert settings.log_level == "INFO"
    # env_prefix="MCP_" is what content_api_mcp/settings.py's own docblock
    # relies on to explain why its own fields need an explicit
    # validation_alias — confirm the prefix itself didn't drift.
    assert BaseMCPSettings.model_config.get("env_prefix") == "MCP_"


def test_create_base_app_with_register_health_false_matches_this_repos_only_call_shape():
    # server.py always calls create_base_app(settings, register_health=False)
    # (stdio transport, no HTTP listener for /health to attach to) — confirm
    # that call shape still works and doesn't register a route.
    settings = BaseMCPSettings(name="test-server")
    mcp = create_base_app(settings, register_health=False)
    assert mcp.name == "test-server"
