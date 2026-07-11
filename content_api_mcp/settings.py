"""Settings for the content-api MCP server.

Extends mcp_base.BaseMCPSettings (shared server identity/logging fields) with
the connection details for a single SilverStripe site's /content-api/v1
surface. This server is per-site: one process, one base URL, one token.

Note: BaseMCPSettings sets env_prefix="MCP_" for its own fields (name,
version, ...). Our fields use an explicit validation_alias so they read from
CONTENT_API_* regardless of that prefix.
"""

from mcp_base import BaseMCPSettings
from pydantic import Field


class ContentApiSettings(BaseMCPSettings):
    """Connection settings for one content-api site."""

    name: str = Field(default="content-api-mcp", validation_alias="CONTENT_API_NAME")
    description: str = Field(
        default="MCP proxy for a SilverStripe dynamic/silverstripe-content-api site",
        validation_alias="CONTENT_API_DESCRIPTION",
    )

    base_url: str = Field(
        validation_alias="CONTENT_API_BASE_URL",
        description="Site's content-api/v1 base URL, e.g. https://example.com/content-api/v1",
    )
    token: str = Field(
        validation_alias="CONTENT_API_TOKEN",
        description="Token minted via `sake tasks:MintContentApiToken`",
    )
    header: str = Field(
        default="X-Silverstripe-Apitoken",
        validation_alias="CONTENT_API_HEADER",
        description="Header the site's TokenAuthenticator expects (colymba tokenHeader config)",
    )
    timeout: int = Field(
        default=30,
        validation_alias="CONTENT_API_TIMEOUT",
        description="Request timeout in seconds",
    )
