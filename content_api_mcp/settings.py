"""Settings for the content-api MCP server.

Extends mcp_base.BaseMCPSettings (shared server identity/logging fields) with
the connection details for a single SilverStripe site's /content-api/v1
surface. This server is per-site: one process, one base URL, one token.

Note: BaseMCPSettings sets env_prefix="MCP_" for its own fields (name,
version, ...). Our fields use an explicit validation_alias so they read from
CONTENT_API_* regardless of that prefix.

Token resolution: CONTENT_API_TOKEN is read directly like any other setting,
but a GUI-launched MCP host (Dock/Spotlight/desktop app) inherits macOS
launchd's environment, not the user's shell profile, so a token exported
only in ~/.zshrc silently resolves to nothing for those hosts, even though
the same config works fine from a terminal. CONTENT_API_TOKEN_FILE is an
env-independent alternative: a file path read at startup, so it works the
same regardless of how the process was launched. See README Troubleshooting.
"""

from pathlib import Path

from mcp_base import BaseMCPSettings
from pydantic import Field, model_validator


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
        description="Token minted via `sake tasks:MintContentApiToken`. "
        "Leave unset and use CONTENT_API_TOKEN_FILE instead if the host "
        "process may not inherit the shell environment.",
    )
    token_file: str | None = Field(
        default=None,
        validation_alias="CONTENT_API_TOKEN_FILE",
        description="Path to a file containing the token, read once at "
        "startup. Used when CONTENT_API_TOKEN is empty, the env-independent "
        "alternative for GUI-launched hosts that don't inherit shell env vars.",
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

    @model_validator(mode="before")
    @classmethod
    def _resolve_token_file(cls, data: object) -> object:
        """Fill CONTENT_API_TOKEN from CONTENT_API_TOKEN_FILE before field validation.

        Runs in "before" mode (on the raw settings-source dict, keyed by env
        var name) rather than "after" mode, so it always executes even when
        another required field (e.g. base_url) is also missing. An "after"
        validator is skipped whenever any field fails validation, which would
        have silently dropped the token-file resolution and narrowed a
        combined "missing config" error down to whichever field failed first.
        Token requiredness itself is left to pydantic's normal field
        validation below, so a still-missing token combines into the same
        ValidationError as any other missing required field, as before.
        """
        if not isinstance(data, dict):
            return data
        if data.get("CONTENT_API_TOKEN"):
            return data
        token_file = data.get("CONTENT_API_TOKEN_FILE")
        if not token_file:
            return data
        path = Path(token_file).expanduser()
        try:
            data["CONTENT_API_TOKEN"] = path.read_text().strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"CONTENT_API_TOKEN_FILE={token_file!r} could not be read: {exc}. "
                "See README Troubleshooting if a GUI-launched host isn't picking up "
                "your shell environment."
            ) from exc
        return data
