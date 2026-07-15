"""MCP server for dynamic/silverstripe-content-api.

Generates one MCP tool per entry in the bundled schema/endpoints.json spec
and proxies calls onto the module's /content-api/v1 REST surface.
"""

__version__ = "1.0.0"
