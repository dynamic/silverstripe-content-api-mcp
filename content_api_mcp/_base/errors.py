"""Vendored from dynamic/daisy-base at commit 612a4155d7696c692e82a3376ce94e119a60b141
(mcp_base/errors.py). See content_api_mcp/_base/__init__.py for why.

Only MCPError, AuthenticationError, and ServiceError are used by this repo's
client.py — the rest of the hierarchy (AuthorizationError, ValidationError,
NotFoundError, RateLimitError) and the Starlette-based error_response()/
ErrorHandlerMiddleware are kept for fidelity with the source file (issue #27
asked for the real implementations, not a reconstruction from call sites),
since fastmcp already pulls in starlette transitively and this file is small.

Bump this file deliberately when daisy-base changes the error hierarchy's
constructor signature or status/error code defaults — it is not kept in
sync automatically. client.py's except clauses depend on this shape.
"""

import logging
from typing import Any

from starlette.responses import JSONResponse

LOGGER = logging.getLogger(__name__)

__all__ = [
    "MCPError",
    "AuthenticationError",
    "AuthorizationError",
    "ValidationError",
    "NotFoundError",
    "RateLimitError",
    "ServiceError",
    "error_response",
    "ErrorHandlerMiddleware",
]


class MCPError(Exception):
    """
    Base exception for MCP server errors.

    Subclass this for specific error types.
    All MCPErrors are automatically converted to JSON responses.
    """

    status_code: int = 500
    error_code: str = "internal_error"

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        error_code: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if status_code:
            self.status_code = status_code
        if error_code:
            self.error_code = error_code

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON response."""
        result = {
            "error": self.error_code,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result


class AuthenticationError(MCPError):
    """Authentication failed (invalid/missing credentials)."""
    status_code = 401
    error_code = "authentication_failed"


class AuthorizationError(MCPError):
    """Authorization failed (valid credentials but insufficient permissions)."""
    status_code = 403
    error_code = "authorization_failed"


class ValidationError(MCPError):
    """Request validation failed."""
    status_code = 400
    error_code = "validation_error"


class NotFoundError(MCPError):
    """Requested resource not found."""
    status_code = 404
    error_code = "not_found"


class RateLimitError(MCPError):
    """Rate limit exceeded."""
    status_code = 429
    error_code = "rate_limit_exceeded"


class ServiceError(MCPError):
    """External service error."""
    status_code = 502
    error_code = "service_error"


def error_response(
    error_code: str,
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """
    Create a standard error response.

    Usage:
        return error_response("invalid_input", "Email is required", 400)
    """
    content = {
        "error": error_code,
        "message": message,
    }
    if details:
        content["details"] = details

    return JSONResponse(content, status_code=status_code)


class ErrorHandlerMiddleware:
    """
    Middleware that catches exceptions and returns JSON error responses.

    Automatically handles:
    - MCPError and subclasses (converted to appropriate JSON response)
    - Validation errors from Pydantic
    - Unhandled exceptions (logged and returned as 500)
    """

    def __init__(self, app, include_traceback: bool = False):
        """
        Initialize error handler.

        Args:
            app: ASGI application
            include_traceback: Include traceback in 500 responses (dev only!)
        """
        self.app = app
        self.include_traceback = include_traceback

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except MCPError as e:
            LOGGER.warning(
                "MCP error: %s (%s) - %s",
                e.error_code,
                e.status_code,
                e.message,
            )
            response = JSONResponse(
                e.to_dict(),
                status_code=e.status_code,
            )
            await response(scope, receive, send)
        except Exception:
            LOGGER.exception("Unhandled exception")

            content = {
                "error": "internal_error",
                "message": "An internal error occurred",
            }

            if self.include_traceback:
                import traceback
                content["traceback"] = traceback.format_exc()

            response = JSONResponse(content, status_code=500)
            await response(scope, receive, send)
