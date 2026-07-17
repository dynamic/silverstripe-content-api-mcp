"""MCP server for dynamic/silverstripe-content-api.

Generates one MCP tool per entry in the bundled schema/endpoints.json spec
and proxies calls onto the module's /content-api/v1 REST surface.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("content-api-mcp")
except PackageNotFoundError:
    # Not installed (e.g. running straight from a source checkout) — this is
    # a dev-only fallback, never a release the USER_AGENT header should claim.
    __version__ = "0.0.0-dev"
