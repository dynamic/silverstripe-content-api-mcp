"""Registration-only: load the bundled spec, generate one MCP tool per
entry, and run the server over stdio.

All HTTP/request-building logic lives in client.py; all connection settings
live in settings.py. This module just wires the two together and exposes
them as FastMCP tools whose schemas are the spec's inputSchema, verbatim.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from importlib import resources
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.tool import Tool
from pydantic import PrivateAttr

from content_api_mcp._base import create_base_app
from content_api_mcp.client import ContentApiClient
from content_api_mcp.settings import ContentApiSettings

LOGGER = logging.getLogger(__name__)


class SpecTool(Tool):
    """A Tool whose schema AND behavior are both driven by one endpoints.json entry.

    `parameters` is set to the spec entry's inputSchema unchanged (no
    derivation from a Python function signature), so the MCP tool contract
    exactly matches what dynamic/silverstripe-content-api documents.
    """

    _call: Any = PrivateAttr()

    async def run(self, arguments: dict[str, Any]):
        # _call is a blocking `requests` call; run it off the event loop so a
        # slow/retrying upstream request doesn't stall every other in-flight
        # tool call (the stdio transport dispatches concurrent tasks, it
        # doesn't serialize them).
        result = await asyncio.to_thread(self._call, arguments)
        return self.convert_result(result)


def load_spec() -> dict[str, Any]:
    """Load the bundled, version-pinned copy of the content-api module's spec.

    Re-sync with `scripts/sync-spec.sh` when the module ships new/changed
    endpoints.
    """
    spec_path = resources.files("content_api_mcp").joinpath("schema", "endpoints.json")
    return json.loads(spec_path.read_text())


def build_tools(spec: dict[str, Any], client: ContentApiClient) -> list[SpecTool]:
    """Generate one SpecTool per spec entry. No hand-written per-endpoint handlers."""
    tools = []
    for entry in spec["tools"]:
        tool = SpecTool(
            name=entry["name"],
            description=entry["description"],
            parameters=entry["inputSchema"],
        )
        tool._call = partial(client.call, entry)
        tools.append(tool)
    return tools


def create_server(settings: ContentApiSettings | None = None) -> FastMCP:
    """Build the FastMCP server instance (used directly by tests; main() runs it)."""
    settings = settings or ContentApiSettings()
    spec = load_spec()
    LOGGER.info(
        "content-api-mcp: loaded spec %s (%d tools) for %s",
        spec.get("version"),
        len(spec["tools"]),
        settings.base_url,
    )

    # register_health=False: this server runs over stdio, not HTTP — there's
    # no listener for a /health endpoint to attach to.
    mcp = create_base_app(settings, register_health=False)
    client = ContentApiClient(settings)
    for tool in build_tools(spec, client):
        mcp.add_tool(tool)
    return mcp


def main() -> None:
    mcp = create_server()
    # show_banner=False: skip FastMCP's startup banner (it phones home to PyPI
    # for an update check) — unwanted egress for a server launched per-session.
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
