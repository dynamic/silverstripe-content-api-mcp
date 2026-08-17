"""Settings for the content-api MCP server.

Extends content_api_mcp._base.BaseMCPSettings (vendored from dynamic/daisy-base,
see that package's own docblock — #27) with the connection details for a
single SilverStripe site's /content-api/v1 surface. This server is per-site:
one process, one base URL, one token.

Note: BaseMCPSettings sets env_prefix="MCP_" for its own fields (name,
version, ...). Our fields use an explicit validation_alias so they read from
CONTENT_API_* regardless of that prefix.

Token resolution: CONTENT_API_TOKEN is read directly like any other setting,
but a GUI-launched MCP host (Dock/Spotlight/desktop app) inherits macOS
launchd's environment, not the user's shell profile, so a token exported
only in ~/.zshrc silently resolves to nothing for those hosts, even though
the same config works fine from a terminal. CONTENT_API_TOKEN_FILE is an
env-independent alternative: a file path read fresh on every call (#23; was
read once at startup before), so it works the same regardless of how the
process was launched, and a re-minted token is picked up without a host
restart. See docs/troubleshooting.md.
"""

from pathlib import Path

from pydantic import Field, model_validator

from content_api_mcp._base import BaseMCPSettings
from content_api_mcp._base.errors import AuthenticationError


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
        description="Path to a file containing the token. Re-read on every "
        "tool call via current_token() (#23), not just at startup, so a "
        "re-minted token is picked up without restarting the host. Used "
        "when CONTENT_API_TOKEN is empty, the env-independent alternative "
        "for GUI-launched hosts that don't inherit shell env vars.",
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

        This still only runs once, at construction — `token` (the field
        this populates) exists purely so pydantic has something to validate
        as "required" and so a `token_file`-only config still constructs
        successfully. It is NOT what a request authenticates with once
        `token_file` is set — see `current_token()` below, which re-reads
        the file per call (#23) instead of trusting this one-time value.
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
                "See docs/troubleshooting.md if a GUI-launched host isn't picking up "
                "your shell environment."
            ) from exc
        return data

    def current_token(self) -> str:
        """Resolve the token to use for the *next* request (#23).

        A content-api service-account token has a real expiry (7-day TTL by
        this project's own default) and a multi-session workflow — rehearse
        locally, re-provision after every DB sync, replay against a fresh
        environment — routinely needs to re-mint mid-project. Before this,
        `ContentApiSettings` resolved the token once at process construction
        with no way for a running MCP server to pick up a newly-minted token
        without a full restart, which pushed batch-write workflows onto a
        hand-rolled curl wrapper instead of this server (see #23's original
        report).

        `token_file` re-reads the file fresh on every call — cheap (a local
        file read), and correct even when the token was rotated after this
        process started. When `token_file` isn't set, `token` was resolved
        once from `CONTENT_API_TOKEN` at construction and there's nothing to
        re-read — an env var doesn't change out from under a running
        process either, so this matches the pre-#23 behavior exactly for
        that path; only the `token_file` path gains reconnect-without-
        restart.

        Raises `AuthenticationError` (not the "before"-validator's
        `ValueError`, since this runs mid-session, long after construction's
        own validation has passed) on a missing/unreadable/empty file, so a
        token file that was deleted or is mid-rewrite by a re-mint script
        surfaces as a clear auth failure on the next call rather than a
        stale/empty header silently reaching the server.
        """
        if not self.token_file:
            return self.token

        path = Path(self.token_file).expanduser()

        try:
            token = path.read_text().strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise AuthenticationError(
                f"CONTENT_API_TOKEN_FILE={self.token_file!r} could not be read: {exc}. "
                "The token file may have been removed, or become unreadable, since this "
                "server started."
            ) from exc

        if not token:
            raise AuthenticationError(
                f"CONTENT_API_TOKEN_FILE={self.token_file!r} is empty. If a token-rotation "
                "script is running, this call landed mid-rewrite — retry."
            )

        return token
