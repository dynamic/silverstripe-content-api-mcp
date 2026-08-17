"""Vendored subset of dynamic/daisy-base's mcp_base package (#27).

Why: silverstripe-content-api-mcp is a public repo meant for any SilverStripe
developer to point at their own site, outside DAISY entirely. It previously
depended on private dynamic/daisy-base (`mcp-base @ git+...`), which meant
`pip install content-api-mcp` failed without a GITHUB_TOKEN/GH_TOKEN scoped
to a private repo the installer has no other reason to access — undercutting
the whole point of being a standalone public tool.

The actual surface used from mcp_base was always narrow — four symbols:
create_http_session, AuthenticationError/MCPError/ServiceError,
BaseMCPSettings, create_base_app — used shallowly (a requests session, a
small settings extension, and register_health=False on the app factory
since this server runs stdio, not HTTP). Vendored here at commit
612a4155d7696c692e82a3376ce94e119a60b141 rather than reimplemented from call
sites, per the source files' own docblocks pointing at daisy-base.

Each file's own header names its source commit so future drift is
traceable. This subpackage is NOT kept in sync with daisy-base
automatically — bump each file deliberately (and re-run the pinned-error-
hierarchy test in tests/test_base_errors.py) when daisy-base changes the
create_http_session/create_base_app signatures or the
AuthenticationError/ServiceError/MCPError hierarchy client.py depends on.

Not a general-purpose mcp_base replacement: only the symbols this repo
actually imports are re-exported below. dynamic/daisy-content (the DAISY
gateway child) carries its own separate, larger mcp_base surface and is
unaffected by this change — see that repo's own vendoring, if any.
"""

from content_api_mcp._base.errors import AuthenticationError, MCPError, ServiceError
from content_api_mcp._base.http import create_http_session
from content_api_mcp._base.server import create_base_app
from content_api_mcp._base.settings import BaseMCPSettings

__all__ = [
    "create_base_app",
    "create_http_session",
    "BaseMCPSettings",
    "MCPError",
    "AuthenticationError",
    "ServiceError",
]
