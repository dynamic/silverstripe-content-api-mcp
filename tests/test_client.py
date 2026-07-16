"""Request-building tests for ContentApiClient.

Path/query construction is tested as pure functions (no HTTP involved) to
avoid any ambiguity around requests/urllib3 URL-encoding of special
characters (e.g. the `ext:` id prefix). Full call() round trips are tested
against `responses`-mocked HTTP for the cases that matter: query strings,
JSON bodies, auth header, and error envelope unwrapping.
"""

import base64
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


# --- filePath -> base64 materialization -------------------------------------------


def test_resolve_file_path_is_a_noop_without_filepath():
    arguments = {"filename": "a.jpg", "base64": "abc123"}
    assert ContentApiClient._resolve_file_path(dict(arguments)) == arguments


def test_resolve_file_path_reads_and_encodes_the_file(tmp_path):
    content = b"\xff\xd8\xff\xe0not-a-real-jpeg-but-bytes-are-bytes"
    file_ = tmp_path / "photo.jpg"
    file_.write_bytes(content)

    result = ContentApiClient._resolve_file_path({"filename": "photo.jpg", "filePath": str(file_)})

    assert "filePath" not in result  # never forwarded upstream
    assert base64.b64decode(result["base64"]) == content


def test_resolve_file_path_expands_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    file_ = tmp_path / "photo.jpg"
    file_.write_bytes(b"hello")

    result = ContentApiClient._resolve_file_path(
        {"filename": "photo.jpg", "filePath": "~/photo.jpg"}
    )

    assert base64.b64decode(result["base64"]) == b"hello"


def test_resolve_file_path_rejects_both_base64_and_filepath(tmp_path):
    file_ = tmp_path / "photo.jpg"
    file_.write_bytes(b"hello")

    with pytest.raises(MCPError, match="not both"):
        ContentApiClient._resolve_file_path(
            {"filename": "photo.jpg", "filePath": str(file_), "base64": "abc"}
        )


def test_resolve_file_path_missing_file_raises(tmp_path):
    missing = tmp_path / "does-not-exist.jpg"

    with pytest.raises(MCPError, match="does not exist"):
        ContentApiClient._resolve_file_path({"filename": "photo.jpg", "filePath": str(missing)})


def test_resolve_file_path_rejects_a_directory(tmp_path):
    with pytest.raises(MCPError, match="does not exist"):
        ContentApiClient._resolve_file_path({"filename": "photo.jpg", "filePath": str(tmp_path)})


@responses.activate
def test_call_asset_upload_via_filepath_sends_base64_and_never_the_path(spec, client, tmp_path):
    content = b"some binary image content"
    file_ = tmp_path / "photo.jpg"
    file_.write_bytes(content)

    responses.add(
        responses.POST,
        f"{BASE_URL}/assets",
        json={"id": 1, "existed": False},
        status=201,
    )

    client.call(
        entry(spec, "content_asset_upload"),
        {"filename": "photo.jpg", "filePath": str(file_), "folder": "amd-home"},
    )

    sent_body = json.loads(responses.calls[0].request.body)
    assert "filePath" not in sent_body
    assert base64.b64decode(sent_body["base64"]) == content
    assert sent_body["folder"] == "amd-home"


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
def test_call_raises_on_3xx_and_does_not_follow_it(spec, client):
    # allow_redirects=False means `requests` never chases this — if it ever
    # did, `responses` would raise a ConnectionError for the unregistered
    # redirect target instead of the assertion below failing cleanly. A
    # custom header like X-Silverstripe-Apitoken survives a same-host
    # redirect (unlike Authorization/Cookie), so a misconfigured base URL or
    # an open redirect must never be followed with the token attached.
    responses.add(
        responses.GET,
        f"{BASE_URL}/schema/site",
        status=302,
        headers={"Location": "https://attacker.example/steal-token"},
    )

    with pytest.raises(ServiceError, match="redirect"):
        client.call(entry(spec, "content_schema_site"), {})

    assert len(responses.calls) == 1


@responses.activate
def test_call_raises_on_2xx_non_json_body(spec, client):
    # The realistic trigger: an auth/routing misconfiguration returns 200
    # with an HTML page instead of the expected JSON envelope. Must surface
    # as an error, not a silent empty-success {}.
    responses.add(
        responses.GET,
        f"{BASE_URL}/schema/site",
        status=200,
        body="<html><body>Please log in</body></html>",
        content_type="text/html",
    )

    with pytest.raises(ServiceError, match="non-JSON body"):
        client.call(entry(spec, "content_schema_site"), {})


@responses.activate
def test_call_treats_genuinely_empty_2xx_body_as_empty_success(spec, client):
    # A truly empty body on a 2xx (no content-api endpoint returns one today,
    # but nothing rules it out for a future action-style endpoint) is still
    # a legitimate empty success, distinct from a non-empty unparseable body.
    responses.add(
        responses.POST,
        f"{BASE_URL}/records/BlockPage/42/publish",
        status=200,
        body="",
    )

    result = client.call(
        entry(spec, "content_records_stage"),
        {"classRef": "BlockPage", "id": "42", "action": "publish"},
    )
    assert result == {}


@responses.activate
def test_call_treats_json_null_body_as_empty_success_not_parse_failure(spec, client):
    # response.json() parses the literal `null` body to None successfully —
    # that must stay a valid empty success, not be mistaken for the
    # can't-parse-as-JSON case (both look like `payload is None` unless the
    # parse-failure sentinel is tracked separately from the parsed value).
    responses.add(
        responses.POST,
        f"{BASE_URL}/records/BlockPage/42/publish",
        status=200,
        body="null",
        content_type="application/json",
    )

    result = client.call(
        entry(spec, "content_records_stage"),
        {"classRef": "BlockPage", "id": "42", "action": "publish"},
    )
    assert result == {}


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
