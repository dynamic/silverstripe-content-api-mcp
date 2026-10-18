"""Shared core for content-api MCP servers.

Everything a server needs to expose dynamic/silverstripe-content-api as MCP
tools: the multi-site HTTP client, spec-driven tool generation, error
rendering, response shaping, the bootstrap and workflow tools, and the
prompts. Used by this package's own standalone stdio server
(content_api_mcp.server) and by DAISY's hosted daisy-content server, which
depends on this package. Depends only on public packages: never import
anything DAISY-specific here.
"""

from content_api_mcp.core.client import ContentClient, SiteConfig, parse_site_registry
from content_api_mcp.core.meta import register_all

__all__ = ["ContentClient", "SiteConfig", "parse_site_registry", "register_all"]
