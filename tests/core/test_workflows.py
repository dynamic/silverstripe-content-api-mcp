"""daisy-content#22: workflow guides as MCP prompts and as the content_get_workflow tool."""

import json

import pytest
from conftest import capture_meta_tools, raises_tool_error

from content_api_mcp.core.tools import load_spec
from content_api_mcp.core.workflows import WORKFLOWS, get_workflow, list_workflows


def _spec_tool_names():
    return {entry["name"] for entry in load_spec()["tools"]}


def test_every_workflow_renders_without_and_with_arguments():
    for workflow in WORKFLOWS.values():
        plain = get_workflow(workflow.name)["text"]
        assert "content_list_sites" in plain
        filled = get_workflow(
            workflow.name, **{arg: "acme" for arg in workflow.arguments}
        )["text"]
        assert 'site="acme"' in filled


def test_every_tool_a_workflow_names_exists():
    import re

    known = _spec_tool_names() | {
        "content_list_sites",
        "content_get_started",
        "content_get_workflow",
    }
    for workflow in WORKFLOWS.values():
        named = set(
            re.findall(r"\bcontent_[a-z_]+\b", get_workflow(workflow.name)["text"])
        )
        assert named <= known, (workflow.name, named - known)


def test_workflow_stage_and_compose_values_match_the_spec():
    spec = {t["name"]: t["inputSchema"] for t in load_spec()["tools"]}
    stage = spec["content_records_stage"]["properties"]
    assert {"publish"} <= set(stage["action"]["enum"])
    assert "recursive" in stage["mode"]["enum"]
    compose_publish = spec["content_compose_page"]["properties"]["publish"]["enum"]
    assert {"none", "recursive"} == set(compose_publish)
    assert "dryRun" in spec["content_batch"]["properties"]


def test_get_workflow_rejects_unknown_names_and_arguments():
    with pytest.raises(KeyError):
        get_workflow("nope")
    with pytest.raises(TypeError, match="page"):
        get_workflow("build-landing-page", page="home")


def test_get_workflow_tool_lists_and_renders():
    tools, _ = capture_meta_tools()
    tool = tools["content_get_workflow"]
    assert tool._mcp_tool_kwargs["annotations"]["openWorldHint"] is False
    listed = tool()["workflows"]
    assert [w["name"] for w in listed] == list(WORKFLOWS)
    rendered = tool(name="add-section", page="about-us", site="acme")
    assert 'the page "about-us"' in rendered["text"]


@pytest.mark.asyncio
async def test_get_workflow_tool_errors_name_the_valid_options():
    tools, _ = capture_meta_tools()
    tool = tools["content_get_workflow"]

    async def call(**kwargs):
        return tool(**kwargs)

    with raises_tool_error("validation_error", match="Unknown workflow") as err:
        await call(name="nope")
    assert err.trailer["valid_options"] == list(WORKFLOWS)
    with raises_tool_error("validation_error", match="doesn't take: page") as err:
        await call(name="build-landing-page", page="home")
    assert err.trailer["valid_options"] == ["site", "brief"]


@pytest.mark.asyncio
async def test_prompts_are_registered_and_render_through_fastmcp():
    from fastmcp import Client, FastMCP

    from content_api_mcp.core.meta import register_prompts

    mcp = FastMCP("test-prompts")
    register_prompts(mcp)
    async with Client(mcp) as client:
        prompts = await client.list_prompts()
        names = {p.name for p in prompts}
        assert names == {f"content-{name}" for name in WORKFLOWS}
        add_section = next(p for p in prompts if p.name == "content-add-section")
        # FastMCP appends a JSON-schema hint to any argument not annotated
        # as bare `str`; the workflow's own description leads.
        described = {a.name: a.description for a in add_section.arguments}
        assert set(described) == set(WORKFLOWS["add-section"].arguments)
        for name, text in WORKFLOWS["add-section"].arguments.items():
            assert described[name].startswith(text)
        assert all(not a.required for a in add_section.arguments)
        result = await client.get_prompt(
            "content-add-section", {"site": "acme", "page": "about-us"}
        )
    text = result.messages[0].content.text
    assert 'the page "about-us"' in text
    assert 'site="acme"' in text


def test_list_workflows_is_small():
    assert len(json.dumps(list_workflows())) < 2000


def test_publish_steps_use_owns_never_recursive_stage_mode():
    # content_records_stage mode "recursive" (publishRecursive) leaves
    # elements in draft; "owns" walks the area and its elements.
    for name in ("build-landing-page", "add-section"):
        text = get_workflow(name)["text"]
        assert 'mode "owns"' in text
        assert 'and mode "recursive"' not in text


def test_add_section_covers_the_essentials_home_page_area():
    text = get_workflow("add-section")["text"]
    assert "ElementalHomePage" in text
    assert 'areaRelation "ElementalHomePage"' in text
