"""Smoke tests for server.py and the bundled spec.

Companion to test_client.py: that suite covers ContentApiClient's request
logic in isolation, this one covers the two things that only fail at server
startup otherwise — create_server() actually building, and every spec entry
having the keys build_tools() indexes. The spec is periodically re-synced
from another repo (scripts/sync-spec.sh), so a malformed or incompatible
sync would only surface in production without this test.
"""

from importlib.metadata import version as installed_version

import pytest

from content_api_mcp import __version__
from content_api_mcp.client import USER_AGENT
from content_api_mcp.server import create_server, load_spec
from content_api_mcp.settings import ContentApiSettings

BASE_URL = "https://example.test/content-api/v1"

REQUIRED_TOOL_KEYS = {"name", "description", "method", "path", "inputSchema"}


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("CONTENT_API_BASE_URL", BASE_URL)
    monkeypatch.setenv("CONTENT_API_TOKEN", "tok_abc123")
    return ContentApiSettings()


def test_create_server_builds_without_error(settings):
    mcp = create_server(settings)
    assert mcp is not None


def test_spec_tools_have_required_keys():
    spec = load_spec()
    assert spec["tools"], "spec has no tools"
    for entry in spec["tools"]:
        missing = REQUIRED_TOOL_KEYS - entry.keys()
        assert not missing, f"tool {entry.get('name', '<unnamed>')} missing keys: {missing}"


def _tool(spec, name):
    return next(t for t in spec["tools"] if t["name"] == name)


def test_v1_5_sync_landed_at_every_changed_location():
    # test_spec_tools_have_required_keys above only checks the five top-level
    # tool keys — it says nothing about inputSchema *content*, so a sync that
    # dropped or misspelled a field/enum value in one of the several places
    # v1.5 touched would pass every other test silently (the client forwards
    # arguments generically; FastMCP doesn't validate against inputSchema
    # locally — see docs/validation.md). Pin the actual spec content at each
    # location the v1.5 sync was supposed to change, straight from the
    # loaded file, not from a hardcoded expectation duplicated by hand.
    spec = load_spec()

    stage_props = _tool(spec, "content_records_stage")["inputSchema"]["properties"]
    assert stage_props["mode"]["enum"] == ["single", "recursive", "subtree"]
    assert stage_props["force"]["type"] == "boolean"

    batch_props = _tool(spec, "content_batch")["inputSchema"]["properties"]
    assert "subtree" in batch_props["defaultPublish"]["enum"]
    op_props = batch_props["operations"]["items"]["properties"]
    assert "subtree" in op_props["publish"]["enum"]
    assert op_props["force"]["type"] == "boolean"

    convert_props = _tool(spec, "content_page_convert")["inputSchema"]["properties"]
    assert "subtree" in convert_props["publish"]["enum"]

    # content_compose_page and content_page_apply_template deliberately did
    # NOT gain `subtree` in v1.5 — pin that too, so a future over-eager sync
    # doesn't silently add it where the module never did.
    compose_publish = _tool(spec, "content_compose_page")["inputSchema"]["properties"]["publish"]
    assert "subtree" not in compose_publish["enum"]
    template_publish = _tool(spec, "content_page_apply_template")["inputSchema"]["properties"][
        "publish"
    ]
    assert "subtree" not in template_publish["enum"]


def test_user_agent_matches_package_version():
    # Guards against USER_AGENT drifting from __version__ on a version bump
    # (content_api_mcp/client.py derives it from __version__).
    assert USER_AGENT == f"content-api-mcp/{__version__}"


def test_version_is_derived_from_installed_package_metadata():
    # Regression: __version__ used to be a hardcoded literal that could (and
    # did) drift from pyproject.toml and the git tag. It must now come from
    # importlib.metadata, so a version bump only has to happen in one place.
    assert __version__ == installed_version("content-api-mcp")
