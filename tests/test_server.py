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


def test_user_agent_matches_package_version():
    # Guards against USER_AGENT drifting from __version__ on a version bump
    # (content_api_mcp/client.py derives it from __version__).
    assert USER_AGENT == f"content-api-mcp/{__version__}"


def test_version_is_derived_from_installed_package_metadata():
    # Regression: __version__ used to be a hardcoded literal that could (and
    # did) drift from pyproject.toml and the git tag. It must now come from
    # importlib.metadata, so a version bump only has to happen in one place.
    assert __version__ == installed_version("content-api-mcp")
