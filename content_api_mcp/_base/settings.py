"""Vendored from dynamic/daisy-base at commit 612a4155d7696c692e82a3376ce94e119a60b141
(mcp_base/config/settings.py). See content_api_mcp/_base/__init__.py for why.

Bump this file deliberately when daisy-base changes BaseMCPSettings' fields
or behavior — it is not kept in sync automatically.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseMCPSettings(BaseSettings):
    """
    Base settings for all MCP servers.

    Extend this class to add server-specific settings:

        class MyServerSettings(BaseMCPSettings):
            name: str = Field(default="my-server")
            my_custom_setting: str = Field(default="value")

    Note: OAuth and authentication are handled at the gateway level.
    Child servers receive tokens via Authorization headers.
    """

    model_config = SettingsConfigDict(
        env_prefix="MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server identity
    name: str = Field(default="mcp-server", description="Server name for identification")
    version: str = Field(default="1.0.0", description="Server version")
    description: str = Field(default="MCP Server", description="Server description")

    # Transport configuration
    transport: str = Field(default="streamable-http", description="MCP transport type")
    host: str = Field(default="0.0.0.0", description="Server bind host")
    port: int = Field(default=3000, description="Server bind port")
    http_path: str = Field(default="/mcp", description="HTTP path for MCP endpoint")
    log_level: str = Field(default="INFO", description="Logging level")
