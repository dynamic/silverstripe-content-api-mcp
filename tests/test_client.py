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

from content_api_mcp._base.errors import AuthenticationError, MCPError, ServiceError
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
    monkeypatch.delenv("CONTENT_API_CA_FILE", raising=False)
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


def upload_entry(spec):
    return entry(spec, "content_asset_upload")


def test_resolve_file_path_is_a_noop_for_a_tool_without_filepath_support(spec):
    # content_records_read's schema never declared "filePath" — nothing here
    # should be enforced or touched for it, even if the caller happens to
    # pass an (unknown-field) "filePath" key.
    arguments = {"classRef": "BlockPage", "id": "1", "filePath": "/tmp/whatever"}
    assert ContentApiClient._resolve_file_path(entry(spec, "content_records_read"), arguments) == (
        arguments
    )


def test_resolve_file_path_reads_and_encodes_the_file(spec, tmp_path):
    content = b"\xff\xd8\xff\xe0not-a-real-jpeg-but-bytes-are-bytes"
    file_ = tmp_path / "photo.jpg"
    file_.write_bytes(content)

    result = ContentApiClient._resolve_file_path(
        upload_entry(spec), {"filename": "photo.jpg", "filePath": str(file_)}
    )

    assert "filePath" not in result  # never forwarded upstream
    assert base64.b64decode(result["base64"]) == content


def test_resolve_file_path_expands_user_home(spec, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    file_ = tmp_path / "photo.jpg"
    file_.write_bytes(b"hello")

    result = ContentApiClient._resolve_file_path(
        upload_entry(spec), {"filename": "photo.jpg", "filePath": "~/photo.jpg"}
    )

    assert base64.b64decode(result["base64"]) == b"hello"


def test_resolve_file_path_rejects_both_base64_and_filepath(spec, tmp_path):
    file_ = tmp_path / "photo.jpg"
    file_.write_bytes(b"hello")

    with pytest.raises(MCPError, match="not both"):
        ContentApiClient._resolve_file_path(
            upload_entry(spec),
            {"filename": "photo.jpg", "filePath": str(file_), "base64": "abc"},
        )


def test_resolve_file_path_rejects_empty_string_base64_alongside_filepath(spec, tmp_path):
    # Presence, not truthiness: an explicit base64="" is a conflicting
    # payload, not "no base64 given" — must not silently let filePath win.
    file_ = tmp_path / "photo.jpg"
    file_.write_bytes(b"hello")

    with pytest.raises(MCPError, match="not both"):
        ContentApiClient._resolve_file_path(
            upload_entry(spec),
            {"filename": "photo.jpg", "filePath": str(file_), "base64": ""},
        )


def test_resolve_file_path_requires_one_of_base64_or_filepath(spec):
    with pytest.raises(MCPError, match='one of "filePath" or "base64"'):
        ContentApiClient._resolve_file_path(upload_entry(spec), {"filename": "photo.jpg"})


def test_resolve_file_path_missing_file_raises(spec, tmp_path):
    missing = tmp_path / "does-not-exist.jpg"

    with pytest.raises(MCPError, match="does not exist"):
        ContentApiClient._resolve_file_path(
            upload_entry(spec), {"filename": "photo.jpg", "filePath": str(missing)}
        )


def test_resolve_file_path_rejects_a_directory(spec, tmp_path):
    with pytest.raises(MCPError, match="does not exist"):
        ContentApiClient._resolve_file_path(
            upload_entry(spec), {"filename": "photo.jpg", "filePath": str(tmp_path)}
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        ".ssh/id_rsa",
        ".env",
        ".aws/credentials",
        "secrets/id_ed25519",
        "certs/server.pem",
        "certs/server.key",
    ],
)
def test_resolve_file_path_rejects_sensitive_looking_paths(spec, tmp_path, relative_path):
    file_ = tmp_path / relative_path
    file_.parent.mkdir(parents=True, exist_ok=True)
    file_.write_bytes(b"super secret")

    with pytest.raises(MCPError, match="credential/secret file"):
        ContentApiClient._resolve_file_path(
            upload_entry(spec), {"filename": "photo.jpg", "filePath": str(file_)}
        )


def test_resolve_file_path_rejects_oversized_files(spec, tmp_path, monkeypatch):
    from content_api_mcp import client as client_module

    monkeypatch.setattr(client_module, "MAX_FILE_PATH_BYTES", 10)
    file_ = tmp_path / "photo.jpg"
    file_.write_bytes(b"x" * 11)

    with pytest.raises(MCPError, match="over the 10-byte limit"):
        ContentApiClient._resolve_file_path(
            upload_entry(spec), {"filename": "photo.jpg", "filePath": str(file_)}
        )


def test_resolve_file_path_allows_files_within_the_size_cap(spec, tmp_path, monkeypatch):
    from content_api_mcp import client as client_module

    monkeypatch.setattr(client_module, "MAX_FILE_PATH_BYTES", 10)
    file_ = tmp_path / "photo.jpg"
    file_.write_bytes(b"x" * 10)

    result = ContentApiClient._resolve_file_path(
        upload_entry(spec), {"filename": "photo.jpg", "filePath": str(file_)}
    )
    assert base64.b64decode(result["base64"]) == b"x" * 10


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
def test_call_reads_a_rotated_token_file_without_recreating_the_client(spec, monkeypatch, tmp_path):
    # #23: the whole point is that a running client picks up a re-minted
    # token without a host restart — this is the end-to-end proof, one
    # ContentApiClient instance across two calls with the file changing
    # in between.
    token_file = tmp_path / "site.token"
    token_file.write_text("tok_original")
    monkeypatch.setenv("CONTENT_API_BASE_URL", BASE_URL)
    monkeypatch.delenv("CONTENT_API_TOKEN", raising=False)
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(token_file))
    settings = ContentApiSettings()
    client = ContentApiClient(settings)

    responses.add(responses.GET, f"{BASE_URL}/schema/site", json={}, status=200)
    responses.add(responses.GET, f"{BASE_URL}/schema/site", json={}, status=200)

    client.call(entry(spec, "content_schema_site"), {})
    assert responses.calls[0].request.headers["X-Silverstripe-Apitoken"] == "tok_original"

    token_file.write_text("tok_rotated")

    client.call(entry(spec, "content_schema_site"), {})
    assert responses.calls[1].request.headers["X-Silverstripe-Apitoken"] == "tok_rotated"


@responses.activate
def test_call_raises_authentication_error_and_makes_no_request_when_token_file_is_empty(
    spec, monkeypatch, tmp_path
):
    # #23 review follow-up: current_token()'s failure path was only tested
    # at the settings-unit level. This is the end-to-end proof, through the
    # same client.call() every tool invocation actually goes through — and
    # the security-relevant half specifically: a token resolution failure
    # must never let a request escape with a blank/missing auth header.
    token_file = tmp_path / "site.token"
    token_file.write_text("tok_original")
    monkeypatch.setenv("CONTENT_API_BASE_URL", BASE_URL)
    monkeypatch.delenv("CONTENT_API_TOKEN", raising=False)
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(token_file))
    settings = ContentApiSettings()
    client = ContentApiClient(settings)

    responses.add(responses.GET, f"{BASE_URL}/schema/site", json={}, status=200)

    token_file.write_text("")

    with pytest.raises(AuthenticationError, match="empty"):
        client.call(entry(spec, "content_schema_site"), {})

    assert len(responses.calls) == 0, "no request may reach the server when token resolution fails"


@responses.activate
def test_call_both_token_and_token_file_configured_uses_token_consistently(
    spec, monkeypatch, tmp_path
):
    # #23 review follow-up (critical, caught before merge): construction
    # resolved CONTENT_API_TOKEN and ignored the file, per the documented
    # precedence — but current_token() gated on token_file alone, so every
    # actual request re-read and authenticated with the *file* instead.
    # Proves the two now agree: CONTENT_API_TOKEN wins consistently, and
    # the file can be deleted entirely without breaking a call.
    token_file = tmp_path / "site.token"
    token_file.write_text("tok_from_file")
    monkeypatch.setenv("CONTENT_API_BASE_URL", BASE_URL)
    monkeypatch.setenv("CONTENT_API_TOKEN", "tok_from_env")
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(token_file))
    settings = ContentApiSettings()
    client = ContentApiClient(settings)

    token_file.unlink()  # the file being gone must not matter — it's ignored

    responses.add(responses.GET, f"{BASE_URL}/schema/site", json={}, status=200)

    client.call(entry(spec, "content_schema_site"), {})

    assert responses.calls[0].request.headers["X-Silverstripe-Apitoken"] == "tok_from_env"


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
def test_call_records_stage_forwards_mode_to_body(spec, client):
    # v1.5 spec sync: `mode` (single/recursive/subtree) is a new non-path,
    # publish-only field on content_records_stage. Nothing in the client
    # special-cases it — this pins that it still reaches the JSON body
    # generically, same as the older `recursive` field above, and doesn't
    # get swallowed by path-param substitution.
    responses.add(
        responses.POST,
        f"{BASE_URL}/records/Page/7/publish",
        json={"stage": "live"},
        status=200,
    )

    client.call(
        entry(spec, "content_records_stage"),
        {"classRef": "Page", "id": "7", "action": "publish", "mode": "subtree"},
    )

    req = responses.calls[0].request
    assert urlparse(req.url).path.endswith("/records/Page/7/publish")
    assert json.loads(req.body) == {"mode": "subtree"}


@responses.activate
def test_call_records_stage_forwards_force_to_body(spec, client):
    # v1.5 spec sync: `force` (bypass the descendant-cascade guard) is a new
    # non-path, unpublish/archive-only field on content_records_stage. Same
    # generic-forwarding guarantee as the `mode` test above.
    responses.add(
        responses.POST,
        f"{BASE_URL}/records/Page/7/unpublish",
        json={"stage": "draft"},
        status=200,
    )

    client.call(
        entry(spec, "content_records_stage"),
        {"classRef": "Page", "id": "7", "action": "unpublish", "force": True},
    )

    req = responses.calls[0].request
    assert urlparse(req.url).path.endswith("/records/Page/7/unpublish")
    assert json.loads(req.body) == {"force": True}


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


class _CapturingResponse:
    """Just enough of requests.Response for _parse_response's success path.

    The verify tests below can't use `responses` — it intercepts at the
    adapter, underneath requests' merge of request kwargs with session and
    environment settings, so the `verify` value never reaches anything it
    records. Capturing the session.request call directly observes exactly
    what client.py passed, which is the thing under test.
    """

    status_code = 200
    ok = True
    text = "{}"
    headers: dict = {}

    @staticmethod
    def json():
        return {}


def test_call_passes_ca_file_as_request_level_verify(spec, monkeypatch, tmp_path):
    # Request-level deliberately (#32): requests consults
    # REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE only when the request-level verify
    # is unset, and a request-level value beats session.verify — this is the
    # one placement where an explicit CONTENT_API_CA_FILE can't be
    # overridden by whatever bundle vars the environment exports.
    ca = tmp_path / "rootCA.pem"
    ca.write_text("dummy pem")
    monkeypatch.setenv("CONTENT_API_BASE_URL", BASE_URL)
    monkeypatch.setenv("CONTENT_API_TOKEN", "tok_abc123")
    monkeypatch.setenv("CONTENT_API_CA_FILE", str(ca))
    client = ContentApiClient(ContentApiSettings())

    captured: dict = {}

    def capture(method, url, **kwargs):
        captured.update(kwargs)
        return _CapturingResponse()

    monkeypatch.setattr(client._session, "request", capture)
    client.call(entry(spec, "content_schema_site"), {})

    assert captured["verify"] == str(ca)


def test_call_omits_verify_entirely_when_no_ca_file_configured(spec, monkeypatch, client):
    # Absent, not verify=True: passing True would also work today (requests
    # still consults the env bundle vars for it), but omitting the kwarg
    # leaves the whole default chain untouched instead of re-implementing
    # one branch of it here.
    captured: dict = {}

    def capture(method, url, **kwargs):
        captured.update(kwargs)
        return _CapturingResponse()

    monkeypatch.setattr(client._session, "request", capture)
    client.call(entry(spec, "content_schema_site"), {})

    assert "verify" not in captured
