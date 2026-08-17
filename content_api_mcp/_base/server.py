"""Vendored from dynamic/daisy-base at commit 612a4155d7696c692e82a3376ce94e119a60b141
(mcp_base/server.py). See content_api_mcp/_base/__init__.py for why.

This repo only calls create_base_app(settings, register_health=False) —
the stdio proxy has no HTTP listener for a /health endpoint to attach to,
and never calls run_server() (fastmcp's own stdio entry point is used
instead, see content_api_mcp/server.py:main()). Both are kept for fidelity
with the source file.

Bump this file deliberately when daisy-base changes create_base_app's
setup/logging behavior — it is not kept in sync automatically.
"""

import logging
from datetime import UTC, datetime

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from content_api_mcp._base.settings import BaseMCPSettings

LOGGER = logging.getLogger(__name__)


def create_base_app(
    settings: BaseMCPSettings,
    register_health: bool = True,
) -> FastMCP:
    """
    Create a pre-configured FastMCP server with common infrastructure.

    Args:
        settings: Server configuration (extend BaseMCPSettings for your server)
        register_health: Include /health endpoint (recommended)

    Returns:
        Configured FastMCP instance. Add your tools, then call run_server().

    Example:
        settings = MyServerSettings()
        mcp = create_base_app(settings)

        @mcp.tool()
        def my_tool(param: str) -> dict:
            return {"result": param}

        run_server(mcp, settings)
    """
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    mcp = FastMCP(
        name=settings.name,
        version=settings.version,
    )

    # Health endpoint (always recommended)
    if register_health:
        _register_health_endpoint(mcp, settings)

    LOGGER.info(f"🏗️ Base MCP server '{settings.name}' v{settings.version} initialized")
    return mcp


def run_server(mcp: FastMCP, settings: BaseMCPSettings) -> None:
    """
    Run the MCP server.

    Args:
        mcp: FastMCP instance from create_base_app()
        settings: Server settings
    """
    import uvicorn

    LOGGER.info(
        f"🚀 Starting {settings.name} on {settings.host}:{settings.port}{settings.http_path} "
        f"({settings.transport})"
    )

    # Create the Starlette application
    app = mcp.http_app(
        transport=settings.transport,
        path=settings.http_path,
        stateless_http=(settings.transport == "streamable-http"),
    )

    # Run using uvicorn
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


def _register_health_endpoint(mcp: FastMCP, settings: BaseMCPSettings) -> None:
    """Register health check endpoint."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        """Health check endpoint for container orchestration."""
        return JSONResponse({
            "status": "healthy",
            "service": settings.name,
            "version": settings.version,
            "timestamp": datetime.now(UTC).isoformat(),
        })

    LOGGER.debug("✅ Health endpoint registered at /health")
