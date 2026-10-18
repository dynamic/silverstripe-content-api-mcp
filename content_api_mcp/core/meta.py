"""Registering the core's tools and prompts on a FastMCP server.

`register_all(mcp, client, allow_file_path=...)` is the one call a server
makes: the spec-generated content_* tools (tools.py), the hand-written meta
tools below, and the workflow prompts. The standalone stdio server
(content_api_mcp.server) and DAISY's hosted daisy-content both use it, so
the two expose the same tools.
"""

from __future__ import annotations

import asyncio
import logging

from fastmcp.exceptions import ToolError

from content_api_mcp._base.errors import MCPError, ValidationError
from content_api_mcp.core.bootstrap import get_started
from content_api_mcp.core.client import ContentClient
from content_api_mcp.core.errors import ContentTransportError, render_tool_error
from content_api_mcp.core.spec import load_spec
from content_api_mcp.core.tools import build_tools
from content_api_mcp.core.workflows import WORKFLOWS, get_workflow, list_workflows

LOGGER = logging.getLogger(__name__)


def register_all(mcp, client: ContentClient, *, allow_file_path: bool = False) -> int:
    """Register every tool and prompt; return the number of tools added."""
    spec = load_spec()
    LOGGER.info(
        "content-api: loaded spec %s (%d tools)",
        spec.get("version"),
        len(spec["tools"]),
    )
    tools = build_tools(spec, client, allow_file_path=allow_file_path)
    for tool in tools:
        mcp.add_tool(tool)
    register_meta_tools(mcp, client)
    register_prompts(mcp)
    return len(tools) + len(META_TOOL_NAMES)


META_TOOL_NAMES = ("content_list_sites", "content_get_started", "content_get_workflow")


def register_meta_tools(mcp, client: ContentClient) -> None:
    """The hand-written tools: no spec entry corresponds to them."""

    # openWorldHint False: reads only this server's own site registry.
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def content_list_sites() -> dict:
        """List configured site slugs. Every other content_* tool takes a
        `site` argument naming one of these; it is optional only when
        exactly one site is configured. Never returns tokens or base URLs,
        only slugs.
        """
        return {"sites": client.list_sites()}

    # Composes two upstream calls. Errors get the same `content-error:`
    # trailer as the spec tools. Async, with the blocking requests calls off
    # the event loop, like SpecTool.run. openWorldHint left at its default
    # (true): it calls the site.
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def content_get_started(site: str | None = None) -> dict:
        """Start here on a site you haven't worked on in this conversation.
        One call returns: the token's member and permissions, the site's
        environment and whether population (batch, compositions, page
        actions, asset writes) and single-record updates are enabled, the
        detected integrations, the class refs grouped into pages / elements
        / other, warnings about anything that will block a write, and the
        order of calls that builds a page. `site` is optional when only one
        site is configured. Follow with content_schema_class for each class
        you will write.
        """
        try:
            return await asyncio.to_thread(get_started, client, site)
        except (MCPError, ContentTransportError) as e:
            raise ToolError(render_tool_error(e)) from e

    # The same guides are registered as MCP prompts by register_prompts;
    # this tool exists for hosts that relay tools but not prompts (the DAISY
    # gateway proxies tools only).
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def content_get_workflow(
        name: str | None = None,
        site: str | None = None,
        page: str | None = None,
        brief: str | None = None,
    ) -> dict:
        """Step-by-step guides for common content jobs: build a landing
        page, add a section to an existing page, populate a page from a
        brief, check what changed. Call with no `name` to list them; with a
        `name` to get its steps, filled in with any of `site`, `page` and
        `brief` that workflow takes. Reads nothing from any site.
        """
        if not name:
            return {"workflows": list_workflows()}
        arguments = {
            key: value
            for key, value in (("site", site), ("page", page), ("brief", brief))
            if value
        }
        try:
            return get_workflow(name, **arguments)
        except KeyError:
            error = ValidationError(
                f"Unknown workflow {name!r}. Workflows: " + ", ".join(WORKFLOWS),
                details={"valid_options": list(WORKFLOWS)},
            )
        except TypeError as e:
            accepted = ", ".join(WORKFLOWS[name].arguments)
            error = ValidationError(
                f"Workflow {name!r} doesn't take: {e}. It takes: {accepted}.",
                details={"valid_options": list(WORKFLOWS[name].arguments)},
            )
        raise ToolError(render_tool_error(error)) from error


def register_prompts(mcp) -> None:
    """Each workflow as an MCP prompt named content-<workflow>, for clients
    that list prompts; see content_get_workflow for hosts that don't."""
    for workflow in WORKFLOWS.values():
        mcp.prompt(
            name=f"content-{workflow.name}",
            title=workflow.title,
            description=workflow.description,
        )(workflow.render)
