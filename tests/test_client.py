"""Request-building tests for ContentApiClient.

Path/query construction is tested as pure functions (no HTTP involved) to
avoid any ambiguity around requests/urllib3 URL-encoding of special
characters (e.g. the `ext:` id prefix). Full call() round trips are tested
against `responses`-mocked HTTP for the cases that matter: query strings,
JSON bodies, auth header, and error envelope unwrapping.
"""

import json
from urllib.parse import parse_qs, urlparse

import pytest
import responses
from mcp_base.errors import AuthenticationError, MCPError, ServiceError

from content_api_mcp.client import ContentApiClient
from content_api_mcp.server import load_spec
from content_api_mcp.settings import ContentApiSettings

BASE_URL = "https://example.test/content-api/v1"


@pytest.fixture(scope="session")
def spec():
    return load_spec()


def entry(spec, name):
    return next(t for t in spec["tools"] if t["name"] == name)


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("CONTENT_API_BASE_URL", BASE_URL)
    monkeypatch.setenv("CONTENT_API_TOKEN", "tok_abc123")
    return ContentApiSettings()


@pytest.fixture
def client(settings):
    return ContentApiClient(settings)


# --- pure path/query construction -------------------------------------------------


def test_resolve_path_substitutes_and_pops_consumed_args(spec):
    entry_ = entry(spec, "content_records_read")
    path, remaining = ContentApiClient._resolve_path(
        entry_["path"], {"classRef": "BlockPage", "id": "ext:home", "_stage": "live"}
    )
    # ':' is kept literal — the server matches `ext:<id>` against the raw,
    # undecoded segment, so encoding it to '%3A' would break the lookup.
    assert path == "records/BlockPage/ext:home"
    assert remaining == {"_stage": "live"}


def test_resolve_path_splits_path_and_body_for_records_stage(spec):
    entry_ = entry(spec, "content_records_stage")
    path, remaining = ContentApiClient._resolve_path(
        entry_["path"],
        {"classRef": "BlockPage", "id": "42", "action": "publish", "recursive": True},
    )
    assert path == "records/BlockPage/42/publish"
    assert remaining == {"recursive": True}


def test_resolve_path_missing_argument_raises(spec):
    entry_ = entry(spec, "content_records_read")
    with pytest.raises(MCPError, match="missing required argument"):
        ContentApiClient._resolve_path(entry_["path"], {"classRef": "BlockPage"})


def test_resolve_path_percent_encodes_special_characters(spec):
    entry_ = entry(spec, "content_records_read")
    path, _ = ContentApiClient._resolve_path(
        entry_["path"], {"classRef": "BlockPage", "id": "ext:foo/bar?x=1#y"}
    )
    # '/', '?', '#' would otherwise be interpreted as path/query/fragment
    # syntax instead of literal id data — encoded. ':' stays literal (the
    # `ext:` convention needs it unescaped server-side).
    assert path == "records/BlockPage/ext:foo%2Fbar%3Fx%3D1%23y"
    assert path.count("/") == 2  # only the two real path separators remain


def test_resolve_path_value_cannot_inject_another_placeholder(spec):
    entry_ = entry(spec, "content_records_stage")
    # id's value is literally another param's {token} text — substitution is
    # a single regex pass over the pristine template, so this can't be
    # re-matched by the later `action` substitution (and it's percent-encoded
    # regardless, so the braces wouldn't match `{action}` even if re-scanned).
    path, remaining = ContentApiClient._resolve_path(
        entry_["path"],
        {"classRef": "BlockPage", "id": "{action}", "action": "publish"},
    )
    assert path == "records/BlockPage/%7Baction%7D/publish"
    assert remaining == {}


def test_flatten_query_flattens_filters_and_keeps_other_params():
    params = ContentApiClient._flatten_query(
        {
            "filters": {"Title__PartialMatch": "Foo"},
            "_stage": "draft",
            "sort": "-Created",
            "limit": None,
        }
    )
    assert params == {
        "Title__PartialMatch": "Foo",
        "_stage": "draft",
        "sort": "-Created",
    }
    assert "filters" not in params
    assert "limit" not in params  # None values are dropped, not sent as "None"


@pytest.mark.parametrize(
    "arguments",
    [
        {"filters": {"_stage": "live"}, "_stage": "draft"},
        {"_stage": "draft", "filters": {"_stage": "live"}},
    ],
    ids=["filters-first", "top-level-first"],
)
def test_flatten_query_top_level_always_wins_over_filters_collision(arguments):
    # Precedence must be deterministic (top-level params win), not dependent
    # on argument insertion order.
    params = ContentApiClient._flatten_query(arguments)
    assert params["_stage"] == "draft"


# --- full call() round trips (responses-mocked HTTP) ------------------------------


@responses.activate
def test_call_get_sends_flattened_query_and_auth_header(spec, client):
    responses.add(responses.GET, f"{BASE_URL}/records/BlockPage", json={"items": []}, status=200)

    result = client.call(
        entry(spec, "content_records_list"),
        {"classRef": "BlockPage", "filters": {"Title__PartialMatch": "Foo"}, "_stage": "draft"},
    )

    assert result == {"items": []}
    req = responses.calls[0].request
    qs = parse_qs(urlparse(req.url).query)
    assert qs["Title__PartialMatch"] == ["Foo"]
    assert qs["_stage"] == ["draft"]
    assert req.headers["X-Silverstripe-Apitoken"] == "tok_abc123"


@responses.activate
def test_call_post_sends_json_body_intact(spec, client):
    responses.add(
        responses.POST, f"{BASE_URL}/compositions/page", json={"page": {"id": 5}}, status=200
    )

    payload = {
        "page": {"match": {"urlSegment": "about"}},
        "publish": "recursive",
        "elements": [
            {
                "class": "ElementContent",
                "externalId": "about-intro",
                "fields": {"HTML": "<p>Hi</p>"},
            }
        ],
    }
    result = client.call(entry(spec, "content_compose_page"), payload)

    assert result == {"page": {"id": 5}}
    req = responses.calls[0].request
    assert json.loads(req.body) == payload
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["X-Silverstripe-Apitoken"] == "tok_abc123"


@responses.activate
def test_call_records_stage_splits_path_and_body_end_to_end(spec, client):
    responses.add(
        responses.POST,
        f"{BASE_URL}/records/BlockPage/42/publish",
        json={"stage": "live"},
        status=200,
    )

    client.call(
        entry(spec, "content_records_stage"),
        {"classRef": "BlockPage", "id": "42", "action": "publish", "recursive": True},
    )

    req = responses.calls[0].request
    assert urlparse(req.url).path.endswith("/records/BlockPage/42/publish")
    assert json.loads(req.body) == {"recursive": True}


@responses.activate
def test_call_raises_authentication_error_on_401_envelope(spec, client):
    responses.add(
        responses.GET,
        f"{BASE_URL}/auth/session",
        json={
            "error": {
                "code": "UNAUTHENTICATED",
                "status": 401,
                "message": "No API token provided.",
            }
        },
        status=401,
    )

    with pytest.raises(AuthenticationError) as exc_info:
        client.call(entry(spec, "content_auth_session"), {})

    assert "UNAUTHENTICATED" in str(exc_info.value)
    assert "No API token provided." in str(exc_info.value)
    # Structured fields must round-trip onto the exception, not just the
    # human-readable message string.
    assert exc_info.value.error_code == "UNAUTHENTICATED"


@responses.activate
def test_call_raises_service_error_on_5xx(spec, client):
    responses.add(
        responses.GET,
        f"{BASE_URL}/schema/site",
        json={"error": {"code": "SERVER_ERROR", "status": 500, "message": "boom"}},
        status=500,
    )

    with pytest.raises(ServiceError, match="boom") as exc_info:
        client.call(entry(spec, "content_schema_site"), {})

    assert exc_info.value.error_code == "SERVER_ERROR"


@responses.activate
def test_call_raises_error_with_details_populated(spec, client):
    responses.add(
        responses.GET,
        f"{BASE_URL}/schema/site",
        json={
            "error": {
                "code": "VALIDATION_ERROR",
                "status": 400,
                "message": "bad field",
                "details": [{"field": "Title", "reason": "required"}],
            }
        },
        status=400,
    )

    with pytest.raises(MCPError) as exc_info:
        client.call(entry(spec, "content_schema_site"), {})

    assert exc_info.value.details == [{"field": "Title", "reason": "required"}]
