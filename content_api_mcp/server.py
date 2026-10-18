"""The standalone stdio server: one site, configured from the environment.

Settings (settings.py) become a single core SiteConfig, and the shared core
(content_api_mcp.core) registers every tool and prompt. DAISY's hosted
daisy-content server builds on the same core with a multi-site registry, so
both expose the same tools.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastmcp import FastMCP

from content_api_mcp import __version__
from content_api_mcp._base import create_base_app
from content_api_mcp.core import ContentClient, SiteConfig, register_all
from content_api_mcp.core.spec import base_route
from content_api_mcp.settings import ContentApiSettings

LOGGER = logging.getLogger(__name__)

USER_AGENT = f"content-api-mcp/{__version__}"


def site_slug(base_url: str) -> str:
    """The site's slug: its host name (the only configured site, so `site`
    defaults to it and callers rarely need to name it)."""
    return urlsplit(base_url).hostname or "site"


def build_client(settings: ContentApiSettings) -> ContentClient:
    base_url = settings.base_url.rstrip("/")
    suffix = "/" + base_route()
    if not base_url.endswith(suffix):
        raise ValueError(
            f"CONTENT_API_BASE_URL must end with the content-api base route "
            f"{suffix!r}; got {settings.base_url!r}"
        )
    site = SiteConfig(
        base_url=base_url,
        token=settings.token,
        header=settings.header,
        token_file=settings.token_file,
        ca_file=settings.ca_file,
    )
    client = ContentClient({site_slug(base_url): site}, user_agent=USER_AGENT)
    client.DEFAULT_TIMEOUT = settings.timeout
    # Unset, the per-tool tiers in core.tools apply (10 s trivial reads,
    # 120 s bulk writes and the fingerprint) and CONTENT_API_TIMEOUT's
    # default covers the rest. Set explicitly, it applies to every call, as
    # it did before the tiers existed.
    if "timeout" in settings.model_fields_set:
        client.timeout_override = settings.timeout
    return client


def create_server(settings: ContentApiSettings | None = None) -> FastMCP:
    """Build the FastMCP server instance (used directly by tests; main() runs it)."""
    settings = settings or ContentApiSettings()
    # register_health=False: this server runs over stdio, not HTTP; there's
    # no listener for a /health endpoint to attach to.
    mcp = create_base_app(settings, register_health=False)
    client = build_client(settings)
    count = register_all(mcp, client, allow_file_path=True)
    LOGGER.info("content-api-mcp: %d tools for %s", count, settings.base_url)
    return mcp


def main() -> None:
    mcp = create_server()
    # show_banner=False: skip FastMCP's startup banner (it phones home to PyPI
    # for an update check), unwanted egress for a server launched per-session.
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
