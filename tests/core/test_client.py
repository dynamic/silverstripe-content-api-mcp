"""Unit tests for ContentClient and its module-level helpers.

No network — patches requests.Session.request (ContentClient's shared
session, daisy-content#13) to verify URL construction, header
shaping, payload shaping, and error mapping without touching a real
SilverStripe site.
"""

import json
import logging
import socket
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from conftest import (
    _client,
    _mock_response,
    _non_json_response,
    _sites,
    start_sequenced_server,
)

from content_api_mcp._base.errors import (
    AuthenticationError,
    AuthorizationError,
    MCPError,
    NotFoundError,
    RateLimitError,
    ServiceError,
)
from content_api_mcp.core import client as client_module
from content_api_mcp.core import spec as spec_module
from content_api_mcp.core.client import (
    MAX_ERROR_BODY_LEN,
    MAX_FILE_PATH_BYTES,
    ContentClient,
    SiteConfig,
    _clean_path_segment,
    parse_site_registry,
)

# ---------------------------------------------------------------------------
# parse_site_registry — CONTENT_SITES JSON parsing.
# ---------------------------------------------------------------------------


def test_parse_site_registry_empty_string_returns_empty_dict():
    assert parse_site_registry("") == {}


def test_parse_site_registry_whitespace_only_returns_empty_dict():
    assert parse_site_registry("   \n  ") == {}


def test_parse_site_registry_parses_single_site():
    raw = json.dumps(
        {"acme": {"base_url": "https://acme.example.com/content-api/v1", "token": "t1"}}
    )
    sites = parse_site_registry(raw)
    assert set(sites) == {"acme"}
    assert sites["acme"].base_url == "https://acme.example.com/content-api/v1"
    assert sites["acme"].token == "t1"


def test_parse_site_registry_defaults_header():
    raw = json.dumps(
        {"acme": {"base_url": "https://acme.example.com/content-api/v1", "token": "t1"}}
    )
    sites = parse_site_registry(raw)
    assert sites["acme"].header == "X-Silverstripe-Apitoken"


def test_parse_site_registry_honours_custom_header():
    raw = json.dumps(
        {
            "acme": {
                "base_url": "https://x/content-api/v1",
                "token": "t1",
                "header": "X-Custom-Token",
            }
        }
    )
    sites = parse_site_registry(raw)
    assert sites["acme"].header == "X-Custom-Token"


def test_parse_site_registry_strips_trailing_slash_from_base_url():
    raw = json.dumps({"acme": {"base_url": "https://x/content-api/v1/", "token": "t1"}})
    sites = parse_site_registry(raw)
    assert sites["acme"].base_url == "https://x/content-api/v1"


def test_parse_site_registry_parses_multiple_sites():
    raw = json.dumps(
        {
            "acme": {
                "base_url": "https://acme.example.com/content-api/v1",
                "token": "t1",
            },
            "beta": {
                "base_url": "https://beta.example.com/content-api/v1",
                "token": "t2",
            },
        }
    )
    sites = parse_site_registry(raw)
    assert set(sites) == {"acme", "beta"}


def test_parse_site_registry_rejects_malformed_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_site_registry("{not json")


def test_parse_site_registry_rejects_non_object_top_level():
    with pytest.raises(ValueError, match="JSON object"):
        parse_site_registry("[1, 2, 3]")


def test_parse_site_registry_rejects_entry_missing_base_url():
    raw = json.dumps({"acme": {"token": "t1"}})
    with pytest.raises(ValueError, match="base_url and token"):
        parse_site_registry(raw)


def test_parse_site_registry_rejects_entry_missing_token():
    raw = json.dumps({"acme": {"base_url": "https://x/content-api/v1"}})
    with pytest.raises(ValueError, match="base_url and token"):
        parse_site_registry(raw)


def test_parse_site_registry_rejects_non_object_entry():
    raw = json.dumps({"acme": "not-an-object"})
    with pytest.raises(ValueError, match="must be an object"):
        parse_site_registry(raw)


def test_parse_site_registry_rejects_base_url_missing_route_entirely():
    # daisy-content#11 regression: a base_url with no /content-api/v1 suffix
    # used to boot clean and 404 every call instead of failing at parse time.
    raw = json.dumps({"acme": {"base_url": "https://acme.example.com", "token": "t1"}})
    with pytest.raises(
        ValueError, match=r"must end with the content-api base route '/content-api/v1'"
    ):
        parse_site_registry(raw)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://x/content-api/v11",
        "https://x/my-content-api/v1",
    ],
)
def test_parse_site_registry_rejects_near_miss_route_suffix(base_url):
    raw = json.dumps({"acme": {"base_url": base_url, "token": "t1"}})
    with pytest.raises(ValueError, match="must end with the content-api base route"):
        parse_site_registry(raw)


def test_parse_site_registry_rejects_route_present_but_not_terminal():
    raw = json.dumps(
        {"acme": {"base_url": "https://x/content-api/v1/records", "token": "t1"}}
    )
    with pytest.raises(ValueError, match="must end with the content-api base route"):
        parse_site_registry(raw)


# ---------------------------------------------------------------------------
# parse_site_registry — base_url scheme/host validation (daisy-content#2).
#
# Every value below deliberately ENDS with the valid base route, so the
# pre-existing suffix check cannot be what rejects it: pre-fix these entries
# booted clean and only failed on the first live tool call, with a `requests`
# connection error that named neither the slug nor the problem.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        # Right shape, wrong scheme: requests has no adapter for ftp.
        "ftp://acme.example.com/content-api/v1",
        # Missing scheme entirely — the classic "just paste the hostname" typo.
        "acme.example.com/content-api/v1",
        # Scheme present but no host, which urlsplit parses with an empty
        # netloc and requests turns into a hostname-resolution failure.
        "https:///content-api/v1",
        # Two host-less netlocs that are NOT empty: a bare port, and a bare
        # userinfo. Testing .netloc instead of .hostname lets both through,
        # and they fail on the first live call exactly like the three above.
        "https://:8080/content-api/v1",
        "https://user@/content-api/v1",
    ],
)
def test_parse_site_registry_rejects_base_url_that_is_not_an_http_url(base_url):
    raw = json.dumps({"acme": {"base_url": base_url, "token": "t1"}})
    with pytest.raises(
        ValueError,
        match=r"CONTENT_SITES\['acme'\]\.base_url must be an http:// or https:// URL",
    ) as excinfo:
        parse_site_registry(raw)
    # The offending value is quoted back, same as every other rejection
    # message here, so the operator can see which entry of a multi-site
    # registry is wrong without guessing.
    assert repr(base_url) in str(excinfo.value)


def test_parse_site_registry_rejects_unparseable_base_url_naming_the_slug():
    # urlsplit raises its own ValueError ("Invalid IPv6 URL") on an unbalanced
    # bracketed host. Framed here, not allowed to escape bare: the raw message
    # names no slug, which is the whole point of this function's error
    # contract.
    raw = json.dumps(
        {"acme": {"base_url": "http://[::1/content-api/v1", "token": "t1"}}
    )
    with pytest.raises(
        ValueError,
        match=r"CONTENT_SITES\['acme'\]\.base_url is not a parseable http:// or",
    ):
        parse_site_registry(raw)


@pytest.mark.parametrize(
    "base_url",
    [
        # daisy-content#52: neither urlsplit() nor .hostname parses a port —
        # only the lazily-evaluated .port property does, and pre-fix nothing
        # read it. A non-numeric or out-of-range port therefore booted clean
        # and failed on the first live call with an opaque
        # requests.exceptions.InvalidURL naming no slug.
        "http://acme.example.com:abc/content-api/v1",
        "http://acme.example.com:99999/content-api/v1",
        "http://acme.example.com:-1/content-api/v1",
    ],
)
def test_parse_site_registry_rejects_malformed_port(base_url):
    raw = json.dumps({"acme": {"base_url": base_url, "token": "t1"}})
    with pytest.raises(
        ValueError,
        match=r"CONTENT_SITES\['acme'\]\.base_url is not a parseable http:// or",
    ) as excinfo:
        parse_site_registry(raw)
    assert repr(base_url) in str(excinfo.value)


def test_parse_site_registry_accepts_empty_port():
    # The empty-port form parses to port None and is genuinely fine — it
    # must not be swept into the malformed-port rejection.
    raw = json.dumps(
        {"acme": {"base_url": "http://acme.example.com:/content-api/v1", "token": "t1"}}
    )
    sites = parse_site_registry(raw)
    assert sites["acme"].base_url == "http://acme.example.com:/content-api/v1"


def test_parse_site_registry_scheme_check_is_reported_before_the_route_check():
    # An entry that is wrong both ways must name the more fundamental problem:
    # there is no point telling the operator to add a base route to a URL that
    # cannot be fetched over any transport at all.
    raw = json.dumps({"acme": {"base_url": "ftp://acme.example.com", "token": "t1"}})
    with pytest.raises(
        ValueError, match=r"must be an http:// or https:// URL with a host"
    ):
        parse_site_registry(raw)


@pytest.mark.parametrize(
    "base_url",
    [
        # http is as valid as https — loopback/staging sites are a supported
        # configuration, not a downgrade to reject.
        "http://localhost:8080/content-api/v1",
        # Uppercase scheme must not be rejected: schemes are case-insensitive
        # per RFC 3986, so a startswith("http") style check would be wrong.
        "HTTPS://acme.example.com/content-api/v1",
    ],
)
def test_parse_site_registry_accepts_every_valid_http_scheme_form(base_url):
    raw = json.dumps({"acme": {"base_url": base_url, "token": "t1"}})
    sites = parse_site_registry(raw)
    assert sites["acme"].base_url == base_url


# ---------------------------------------------------------------------------
# content_api_mcp.core.spec.base_route — a missing/blank baseRoute must raise, not
# silently disable parse_site_registry's suffix check.
# ---------------------------------------------------------------------------


def test_base_route_raises_when_spec_has_no_base_route(tmp_path):
    stub_schema = tmp_path / "endpoints.json"
    stub_schema.write_text(json.dumps({"tools": []}))
    with pytest.raises(ValueError, match="no non-empty top-level 'baseRoute'"):
        spec_module.base_route(stub_schema)


def test_base_route_raises_when_base_route_is_blank(tmp_path):
    stub_schema = tmp_path / "endpoints.json"
    stub_schema.write_text(json.dumps({"baseRoute": "   ", "tools": []}))
    with pytest.raises(ValueError, match="no non-empty top-level 'baseRoute'"):
        spec_module.base_route(stub_schema)


def test_base_route_raises_when_base_route_is_only_slashes(tmp_path):
    # A baseRoute of "/" strips to "" too, but only *after* slash-stripping —
    # checking emptiness before that step would let it slip past as
    # "non-empty" and still return "", the exact silent-"" this function
    # exists to prevent.
    stub_schema = tmp_path / "endpoints.json"
    stub_schema.write_text(json.dumps({"baseRoute": "///", "tools": []}))
    with pytest.raises(ValueError, match="no non-empty top-level 'baseRoute'"):
        spec_module.base_route(stub_schema)


def test_base_route_raises_when_base_route_is_not_a_string(tmp_path):
    stub_schema = tmp_path / "endpoints.json"
    stub_schema.write_text(json.dumps({"baseRoute": 123, "tools": []}))
    with pytest.raises(ValueError, match="no non-empty top-level 'baseRoute'"):
        spec_module.base_route(stub_schema)


def test_base_route_raises_valueerror_not_attributeerror_when_spec_is_not_an_object(
    tmp_path,
):
    # A spec file whose top level is a JSON array (not an object) used to
    # raise a bare AttributeError from `.get("baseRoute")` — an unframed
    # crash instead of the same friendly ValueError every other malformed-
    # spec case gets.
    stub_schema = tmp_path / "endpoints.json"
    stub_schema.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(
        ValueError, match="does not contain a JSON object at the top level"
    ):
        spec_module.base_route(stub_schema)


def test_base_route_raises_filenotfounderror_for_missing_schema_file(tmp_path):
    # Not a ValueError — server.py's boot-time catch is required to widen to
    # (ValueError, OSError) to still frame this under the friendly
    # "Invalid pinned schema/endpoints.json" message (see
    # test_create_app_raises_when_pinned_spec_has_no_base_route in
    # tests/test_server_tools.py for that half of the proof).
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(FileNotFoundError):
        spec_module.base_route(missing)


def test_parse_site_registry_suffix_check_generalizes_beyond_the_pinned_route():
    # The other rejection tests above all use the pinned schema's actual
    # "content-api/v1" value. This proves the endswith/prefix-boundary logic
    # in parse_site_registry itself is correct independent of that concrete
    # string, by swapping in an arbitrary base_route().
    with patch("content_api_mcp.core.client.base_route", return_value="foo/bar"):
        raw = json.dumps({"acme": {"base_url": "https://x/foo/bar", "token": "t1"}})
        sites = parse_site_registry(raw)
        assert sites["acme"].base_url == "https://x/foo/bar"

        raw_near_miss = json.dumps(
            {"acme": {"base_url": "https://x/other-foo/bar", "token": "t1"}}
        )
        with pytest.raises(
            ValueError, match=r"must end with the content-api base route '/foo/bar'"
        ):
            parse_site_registry(raw_near_miss)


# ---------------------------------------------------------------------------
# _clean_path_segment: one opaque, percent-encoded segment; `ext:` stays
# literal; empty and dot-only values are refused.
# ---------------------------------------------------------------------------


def test_clean_path_segment_keeps_safe_values_unchanged():
    for value in ("abc-123_XYZ", "ext:some-external-id", "App.Model.BlockPage", "~x"):
        assert _clean_path_segment(value) == value


@pytest.mark.parametrize(
    ("raw", "encoded"),
    [
        ("a/b", "a%2Fb"),
        ("a?b", "a%3Fb"),
        ("a#b", "a%23b"),
        ("a\\b", "a%5Cb"),
        ("bad id!", "bad%20id%21"),
        ("../x", "..%2Fx"),
        ("5/../6", "5%2F..%2F6"),
        ("{id}", "%7Bid%7D"),
        ("100%", "100%25"),
        # module #240: an ext: id with a space or slash round-trips.
        ("ext:spring open/house", "ext:spring%20open%2Fhouse"),
    ],
)
def test_clean_path_segment_encodes_anything_that_could_reshape_the_url(raw, encoded):
    assert _clean_path_segment(raw) == encoded


@pytest.mark.parametrize("bad", ["", "   ", ".", "..", "..."])
def test_clean_path_segment_rejects_empty_and_dot_only(bad):
    # Dot-only segments: requests' URL normalization collapses them and
    # could walk the request outside the base route with the site's token.
    with pytest.raises(ValueError):
        _clean_path_segment(bad)


# ---------------------------------------------------------------------------
# Site resolution — the multi-site departure from MediaClient.
# ---------------------------------------------------------------------------


def test_resolve_site_raises_not_found_for_unknown_slug():
    client = _client()
    with pytest.raises(NotFoundError, match="Unknown site"):
        client._resolve_site("does-not-exist")


def test_resolve_site_error_lists_configured_sites():
    client = ContentClient(
        sites=_sites(other=SiteConfig(base_url="https://y/content-api/v1", token="t2"))
    )
    with pytest.raises(NotFoundError, match="other, testbed"):
        client._resolve_site("nope")


def test_list_sites_returns_sorted_slugs_only_no_secrets():
    client = ContentClient(
        sites={
            "zeta": SiteConfig(base_url="https://z/content-api/v1", token="secret-z"),
            "alpha": SiteConfig(base_url="https://a/content-api/v1", token="secret-a"),
        }
    )
    assert client.list_sites() == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# _request — header shape (the "Auth header" departure from MediaClient),
# status-code mapping, and redirect refusal.
# ---------------------------------------------------------------------------


def test_request_sends_site_specific_header_not_authorization_bearer():
    with patch(
        "requests.Session.request", return_value=_mock_response(200, {"ok": True})
    ) as m:
        _client()._request("GET", "auth/session", site="testbed")
    headers = m.call_args.kwargs["headers"]
    assert headers["X-Silverstripe-Apitoken"] == "tok-123"
    assert "Authorization" not in headers


def test_request_uses_per_site_custom_header():
    client = ContentClient(
        sites={
            "acme": SiteConfig(
                base_url="https://x/content-api/v1", token="t1", header="X-Custom-Token"
            )
        }
    )
    with patch(
        "requests.Session.request", return_value=_mock_response(200, {"ok": True})
    ) as m:
        client._request("GET", "auth/session", site="acme")
    headers = m.call_args.kwargs["headers"]
    assert headers["X-Custom-Token"] == "t1"
    assert "X-Silverstripe-Apitoken" not in headers


# ---------------------------------------------------------------------------
# Shared HTTP session — daisy-content#13: User-Agent, connection reuse,
# retry/backoff on idempotent methods only.
# ---------------------------------------------------------------------------


def _expected_user_agent():
    from content_api_mcp.core.client import DEFAULT_USER_AGENT

    return DEFAULT_USER_AGENT


def test_session_carries_the_default_user_agent():
    client = _client()
    assert client._session.headers["User-Agent"] == _expected_user_agent()
    from content_api_mcp import __version__

    assert _expected_user_agent() == f"content-api-mcp/{__version__}"


def test_session_carries_a_server_supplied_user_agent():
    client = ContentClient(sites=_sites(), user_agent="daisy-content/0.4.0 (abc123)")
    assert client._session.headers["User-Agent"] == "daisy-content/0.4.0 (abc123)"


def test_session_created_once_and_reused_across_requests():
    with patch("content_api_mcp.core.client.create_http_session") as mock_create:
        mock_create.return_value.request.return_value = _mock_response(
            200, {"ok": True}
        )
        client = ContentClient(sites=_sites())
        client._request("GET", "auth/session", site="testbed")
        client._request("GET", "records/Page", site="testbed")
    # Session built exactly once at construction time, not per-request. This
    # is necessary but not sufficient for connection pooling/reuse: it pins
    # that create_http_session() (and thus the underlying HTTPAdapter/
    # PoolManager) isn't rebuilt per call — it does NOT prove a TCP
    # connection is actually reused on the wire, since create_http_session
    # is mocked out here entirely (no real socket exists). Real connection
    # reuse isn't exercised by any test today (review finding, daisy-content#13):
    # every real-transport fixture (start_sequenced_server) forces
    # `Connection: close` on every response, deliberately, so a
    # keep-alive-capable fixture would be needed to pin that claim.
    assert mock_create.call_count == 1
    assert client._session.request.call_count == 2


def test_close_closes_the_underlying_session():
    client = _client()
    with patch.object(client._session, "close") as mock_close:
        client.close()
    mock_close.assert_called_once()


def test_retry_policy_departures_apply_to_both_http_and_https_adapters():
    # Review finding: ContentClient.__init__ sets respect_retry_after_header
    # False and excludes 429 from status_forcelist on both the "http://" and
    # "https://" mount points explicitly, rather than relying on
    # create_http_session mounting one shared HTTPAdapter instance under
    # both prefixes (true today, but an implementation detail of a
    # separately-versioned package). This pins the actual end state
    # (both mounts must carry the departure), not the loop's mechanism —
    # `mcp_base.create_http_session` mounts the SAME HTTPAdapter instance
    # under both prefixes today, so mutating the loop to touch only
    # "https://" is still observably identical (get_adapter("http://") is
    # get_adapter("https://")) and this assertion alone can't distinguish
    # that from the real fix. It's still worth having: it fails the day
    # that aliasing ever changes (the exact scenario the loop's own comment
    # in client.py guards against) or if either departure is dropped
    # outright, neither of which was covered by anything before this test.
    client = _client()
    for scheme in ("http://", "https://"):
        retry = client._session.get_adapter(scheme).max_retries
        assert retry.respect_retry_after_header is False
        assert 429 not in retry.status_forcelist


def test_get_retried_through_two_502s_returns_the_eventual_200():
    base_url, handler_cls = start_sequenced_server([502, 502, 200])
    try:
        client = ContentClient(
            sites={"local": SiteConfig(base_url=base_url, token="t")}
        )
        result = client._request("GET", "records/Page", site="local", timeout=5)
    finally:
        handler_cls.server.shutdown()
        handler_cls.server.server_close()
    assert result == {"ok": True}
    assert handler_cls.call_count == 3
    # The UA header actually reaching the wire, not just set on the Session
    # object — real transport, real server-observed request.
    assert handler_cls.last_headers["User-Agent"] == _expected_user_agent()


def test_get_retried_through_two_502s_logs_a_warning_on_recovery(caplog):
    # Review finding: pre-change, every 502 hit self._logger.error — a site
    # failing most of its requests but always eventually succeeding on
    # retry now returns cleanly and (absent this) logs nothing, reading as
    # perfectly healthy. Assert on the real retry history urllib3 attaches
    # to the response (response.raw.retries.history), not a mock.
    base_url, handler_cls = start_sequenced_server([502, 502, 200])
    try:
        client = ContentClient(
            sites={"local": SiteConfig(base_url=base_url, token="t")}
        )
        # ContentClient._logger is BaseAPIClient's logging.getLogger(self.__class__.__name__)
        # ("ContentClient"), not the module-level `LOGGER = logging.getLogger(__name__)`
        # ("content_api_mcp.core.client") that client.py also declares but
        # doesn't use for this call.
        with caplog.at_level(logging.WARNING, logger="ContentClient"):
            client._request("GET", "records/Page", site="local", timeout=5)
    finally:
        handler_cls.server.shutdown()
        handler_cls.server.server_close()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "local" in m and "after 2 failed attempt(s)" in m for m in warnings
    ), warnings


def test_post_502_is_sent_exactly_once_and_raises_service_error():
    # POST is not in create_http_session's allowed_methods (idempotent-only
    # retry) — a 502 here must NOT be retried, since re-sending a
    # non-idempotent write could double-commit it.
    base_url, handler_cls = start_sequenced_server([502, 200])
    try:
        client = ContentClient(
            sites={"local": SiteConfig(base_url=base_url, token="t")}
        )
        with pytest.raises(ServiceError):
            client._request(
                "POST", "compositions/page", site="local", json_data={"a": 1}, timeout=5
            )
    finally:
        handler_cls.server.shutdown()
        handler_cls.server.server_close()
    assert handler_cls.call_count == 1


def test_get_retries_exhausted_still_raises_service_error():
    base_url, handler_cls = start_sequenced_server([502])
    try:
        client = ContentClient(
            sites={"local": SiteConfig(base_url=base_url, token="t")}
        )
        with pytest.raises(ServiceError) as exc_info:
            client._request("GET", "records/Page", site="local", timeout=5)
    finally:
        handler_cls.server.shutdown()
        handler_cls.server.server_close()
    assert exc_info.value.status_code == 502
    # 1 initial attempt + create_http_session's default max_retries=3.
    assert handler_cls.call_count == 4


def test_429_is_not_retried_at_transport_layer_even_with_retry_after():
    # urllib3's respect_retry_after_header (on by default) retries a 429
    # carrying a Retry-After header independent of status_forcelist
    # membership — excluding 429 from status_forcelist alone would not have
    # stopped this. Proof: the second response here is a 200, so if urllib3
    # retried once (honoring Retry-After: 2) this call would silently
    # SUCCEED with the 200 body instead of raising RateLimitError at all —
    # and would take >=2s doing it. It must instead fail fast, on the first
    # response.
    base_url, handler_cls = start_sequenced_server([429, 200], retry_after=2)
    try:
        client = ContentClient(
            sites={"local": SiteConfig(base_url=base_url, token="t")}
        )
        t0 = time.monotonic()
        with pytest.raises(RateLimitError) as exc_info:
            client._request("GET", "records/Page", site="local", timeout=5)
        elapsed = time.monotonic() - t0
    finally:
        handler_cls.server.shutdown()
        handler_cls.server.server_close()
    assert handler_cls.call_count == 1
    assert exc_info.value.details.get("retry_after") == "2"
    assert elapsed < 1.0


def test_session_never_accepts_cookies():
    # Auth here is header-based; a shared session that kept cookies would
    # leak a Set-Cookie from one call (any site) onto every later call this
    # process makes, to every site.
    base_url, handler_cls = start_sequenced_server(
        [200], set_cookie="PHPSESSID=abc123; Path=/"
    )
    try:
        client = ContentClient(
            sites={"local": SiteConfig(base_url=base_url, token="t")}
        )
        client._request("GET", "records/Page", site="local", timeout=5)
    finally:
        handler_cls.server.shutdown()
        handler_cls.server.server_close()
    assert list(client._session.cookies) == []


def test_503_is_still_retried_but_ignores_retry_after():
    # respect_retry_after_header=False is not 429-scoped: 503 stays in
    # status_forcelist, so it's retried either way — the flag only controls
    # the *wait* between attempts. Proof: retry_after=100 would make this
    # test take >=100s if honored; it must instead finish on urllib3's own
    # short backoff schedule (~0/0.6/1.2s between attempts).
    base_url, handler_cls = start_sequenced_server([503, 503, 200], retry_after=100)
    try:
        client = ContentClient(
            sites={"local": SiteConfig(base_url=base_url, token="t")}
        )
        t0 = time.monotonic()
        result = client._request("GET", "records/Page", site="local", timeout=5)
        elapsed = time.monotonic() - t0
    finally:
        handler_cls.server.shutdown()
        handler_cls.server.server_close()
    assert result == {"ok": True}
    assert handler_cls.call_count == 3
    assert elapsed < 5.0


def test_one_sites_auth_header_never_persists_onto_another_sites_request():
    # Review finding: client.py's own comment names this hazard — the
    # session "is shared across every caller and every configured site" —
    # and the cookie half is tested (test_session_never_accepts_cookies
    # above) but the header half wasn't. `headers = {cfg.header: cfg.token}`
    # staying per-request (not moved onto self._session.headers) is what
    # prevents site A's credential from riding along on site B's request;
    # nothing pinned that until now.
    base_url, handler_cls = start_sequenced_server([200, 200])
    try:
        client = ContentClient(
            sites={
                "a": SiteConfig(
                    base_url=base_url, token="TOKEN-A", header="X-Site-A-Token"
                ),
                "b": SiteConfig(base_url=base_url, token="TOKEN-B"),
            }
        )
        client._request("GET", "records/Page", site="a", timeout=5)
        client._request("GET", "records/Page", site="b", timeout=5)
    finally:
        handler_cls.server.shutdown()
        handler_cls.server.server_close()
    assert handler_cls.last_headers["X-Silverstripe-Apitoken"] == "TOKEN-B"
    assert "X-Site-A-Token" not in handler_cls.last_headers
    # And the session's own persistent default headers never picked up a
    # per-site token either — the per-request dict is the only place either
    # site's credential should ever appear.
    assert not any(k.lower().endswith("token") for k in client._session.headers)


def _closed_port():
    """Bind then immediately close an ephemeral port — a connection to it
    is refused (ECONNREFUSED), for testing the connect-phase retry path
    without needing a real unreachable host."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_connect_phase_failure_retries_for_every_method(method):
    # A connect-phase error (refused host) retries for every method
    # regardless of create_http_session's allowed_methods — unlike a
    # response-status retry, which is idempotent-methods-only (see
    # test_post_502_is_sent_exactly_once_and_raises_service_error above).
    # Proof: elapsed time reflects urllib3's multi-attempt backoff (several
    # hundred ms of sleep across the 4 attempts) for POST too, not just GET
    # — a single connect-refused attempt fails near-instantly, so this
    # threshold distinguishes "retried" from "gave up after one try".
    port = _closed_port()
    client = ContentClient(
        sites={
            "local": SiteConfig(
                base_url=f"http://127.0.0.1:{port}/content-api/v1", token="t"
            )
        }
    )
    kwargs = {"json_data": {"a": 1}} if method == "POST" else {}
    t0 = time.monotonic()
    with pytest.raises(RuntimeError):
        client._request(method, "records/Page", site="local", timeout=5, **kwargs)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.5


def test_request_builds_url_from_site_base_url():
    with patch("requests.Session.request", return_value=_mock_response(200, {})) as m:
        _client()._request("GET", "records/Page", site="testbed")
    assert (
        m.call_args.kwargs["url"] == "https://example.com/content-api/v1/records/Page"
    )


def test_request_never_follows_redirects():
    with patch("requests.Session.request", return_value=_mock_response(200, {})) as m:
        _client()._request("GET", "auth/session", site="testbed")
    assert m.call_args.kwargs["allow_redirects"] is False


def test_request_raises_on_3xx_instead_of_following():
    resp = _mock_response(302, headers={"Location": "https://elsewhere/"})
    with patch("requests.Session.request", return_value=resp):
        with pytest.raises(RuntimeError, match="redirect"):
            _client()._request("GET", "auth/session", site="testbed")


def test_401_raises_authentication_error():
    with patch(
        "requests.Session.request", return_value=_mock_response(401, text="bad token")
    ):
        with pytest.raises(AuthenticationError):
            _client()._request("GET", "auth/session", site="testbed")


def test_404_raises_not_found_error():
    with patch(
        "requests.Session.request",
        return_value=_mock_response(404, text="no such record"),
    ):
        with pytest.raises(NotFoundError):
            _client()._request("GET", "records/Page/999", site="testbed")


def test_429_raises_rate_limit_error_with_retry_after():
    with patch(
        "requests.Session.request",
        return_value=_mock_response(
            429, text="slow down", headers={"Retry-After": "30"}
        ),
    ):
        with pytest.raises(RateLimitError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert exc_info.value.details.get("retry_after") == "30"


def test_500_raises_service_error():
    with patch(
        "requests.Session.request",
        return_value=_mock_response(500, text="server exploded"),
    ):
        with pytest.raises(ServiceError):
            _client()._request("GET", "records/Page", site="testbed")


def test_unknown_site_never_reaches_the_network():
    with patch("requests.Session.request") as m:
        with pytest.raises(NotFoundError):
            _client()._request("GET", "records/Page", site="ghost")
    m.assert_not_called()


def test_204_returns_empty_dict():
    with patch("requests.Session.request", return_value=_mock_response(204)):
        result = _client()._request("DELETE", "records/Page/1", site="testbed")
    assert result == {}


# ---------------------------------------------------------------------------
# Non-JSON 2xx response (daisy-content#12) — a successful request whose body isn't JSON
# (e.g. a base_url landing on an HTML login page or a WAF interstitial) must
# not be swallowed by the generic RequestException handler and re-labeled a
# bare "request failed", which drops the status code, Content-Type, and body.
# ---------------------------------------------------------------------------


def test_non_json_2xx_names_content_type_and_body():
    body = "<html><body>Please log in</body></html>"
    with patch(
        "requests.Session.request",
        return_value=_non_json_response(
            body, status_code=200, content_type="text/html"
        ),
    ):
        with pytest.raises(ServiceError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    msg = str(exc_info.value)
    assert "text/html" in msg
    assert body in msg
    assert "request failed" not in msg
    assert exc_info.value.status_code == 200


def test_non_json_2xx_body_is_truncated():
    body = "b" * (MAX_ERROR_BODY_LEN + 500)
    with patch("requests.Session.request", return_value=_non_json_response(body)):
        with pytest.raises(ServiceError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    msg = str(exc_info.value)
    assert msg.endswith(body[:MAX_ERROR_BODY_LEN] + "…(truncated)")
    assert body not in msg


def test_non_json_2xx_full_body_reaches_logger_uncapped():
    body = "u" * (MAX_ERROR_BODY_LEN + 500)
    client = _client()
    with (
        patch("requests.Session.request", return_value=_non_json_response(body)),
        patch.object(client, "_logger") as mock_logger,
    ):
        with pytest.raises(ServiceError):
            client._request("GET", "records/Page", site="testbed")
    logged_body = mock_logger.error.call_args.args[-1]
    assert logged_body == body
    assert len(logged_body) > MAX_ERROR_BODY_LEN


def test_json_null_body_returns_empty_dict():
    # _mock_response can't represent this: it treats json_data=None as "no
    # body" (empty content, short-circuiting through the 204 branch before
    # response.json() is ever called) rather than "a real body whose parsed
    # JSON value is the literal null". Build the mock directly so .json()
    # actually returns None and the new normalization branch is exercised.
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"null"
    resp.text = "null"
    resp.headers = {"Content-Type": "application/json"}
    resp.raise_for_status.return_value = None
    resp.json.return_value = None
    client = _client()
    with (
        patch("requests.Session.request", return_value=resp),
        patch.object(client, "_logger") as mock_logger,
    ):
        result = client._request("GET", "records/Page/1", site="testbed")
    assert result == {}
    # review finding: a silently-normalized null body left no trace to
    # debug from later — must be logged, same discipline as the non-JSON
    # branch immediately above it in client.py.
    mock_logger.warning.assert_called_once()
    logged_args = mock_logger.warning.call_args.args
    assert "testbed" in logged_args
    assert "records/Page/1" in logged_args


# ---------------------------------------------------------------------------
# Error body capping + upstream error-envelope unwrapping.
# ---------------------------------------------------------------------------


def test_error_body_capped_when_not_json():
    # 500 on a non-batch path: the ROLLBACK_UNVERIFIED-risk warning
    # (test_500_without_envelope_on_batch_warns_rollback_unverified_cannot_be_ruled_out)
    # is scoped to path == "batch", so it cannot fire here and the generic
    # branch's `endswith` assertion below reflects the capped body cleanly.
    body = "x" * (MAX_ERROR_BODY_LEN + 500)
    with patch("requests.Session.request", return_value=_mock_response(500, text=body)):
        with pytest.raises(ServiceError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    msg = str(exc_info.value)
    assert msg.endswith(body[:MAX_ERROR_BODY_LEN] + "…(truncated)")


def test_error_envelope_message_extracted_when_present():
    body = json.dumps(
        {"error": {"code": "PAYLOAD_INVALID", "message": "classRef is required"}}
    )
    with patch("requests.Session.request", return_value=_mock_response(400, text=body)):
        with pytest.raises(
            MCPError, match="PAYLOAD_INVALID: classRef is required"
        ) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert exc_info.value.status_code == 400
    assert exc_info.value.details["error_code"] == "PAYLOAD_INVALID"


def test_error_envelope_details_travel_as_data_not_a_repr_in_the_message():
    # daisy-content#30: `details` reaches the caller as JSON in the
    # `content-error:` trailer (content_api_mcp.core.errors), built from the raw
    # `upstream_details` value. The message no longer carries a Python
    # repr of it.
    body = json.dumps(
        {
            "error": {
                "code": "FORBIDDEN_RECORD",
                "message": "not allowed",
                "details": {"id": 5},
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(403, text=body)):
        with pytest.raises(AuthorizationError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert str(exc_info.value).endswith("FORBIDDEN_RECORD: not allowed")
    assert "(details:" not in str(exc_info.value)
    assert exc_info.value.details["upstream_details"] == {"id": 5}


# ---------------------------------------------------------------------------
# 403 -> AuthorizationError — ENV_FORBIDDEN gets config-gate advice: the
# population gate's names SS_CONTENT_API_ALLOW_POPULATE, the record-update
# gate's (details[].config, or a PATCH with no details, daisy-content#51) names
# RecordUpdateHandler.disabled_environments. Every other 403 code (or an
# absent/unrecognised one) gets a generic permission-flavoured message and
# must NOT claim the env gate (daisy-content#6).
# ---------------------------------------------------------------------------


def test_403_env_forbidden_names_the_config_gate():
    body = json.dumps(
        {
            "error": {
                "code": "ENV_FORBIDDEN",
                "message": "population disabled on this environment",
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(403, text=body)):
        with pytest.raises(AuthorizationError) as exc_info:
            _client()._request("POST", "batch", site="testbed")
    assert "SS_CONTENT_API_ALLOW_POPULATE" in str(exc_info.value)
    # The upstream message is the only operator-facing text distinguishing
    # one ENV_FORBIDDEN from another — pin the pass-through, or the segment
    # can be deleted from the raise with the suite still green.
    assert (
        "Upstream message: ENV_FORBIDDEN: population disabled on this environment"
        in str(exc_info.value)
    )
    # The upstream code is the branchable signal, carried in `details` —
    # the exception's own `error_code` attribute stays at AuthorizationError's
    # stable class default, same as every sibling branch (401/404/429).
    assert exc_info.value.error_code == "authorization_failed"
    assert exc_info.value.details["error_code"] == "ENV_FORBIDDEN"


def test_403_forbidden_class_does_not_claim_env_gate():
    body = json.dumps(
        {"error": {"code": "FORBIDDEN_CLASS", "message": "not permitted on this class"}}
    )
    with patch("requests.Session.request", return_value=_mock_response(403, text=body)):
        with pytest.raises(AuthorizationError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert exc_info.value.details["error_code"] == "FORBIDDEN_CLASS"
    assert "SS_CONTENT_API_ALLOW_POPULATE" not in str(exc_info.value)


def test_403_env_forbidden_detected_even_without_upstream_message():
    # Regression guard: _extract_upstream_error used to gate its whole
    # envelope-unwrap on `message` being truthy, silently dropping `code`
    # (and with it, ENV_FORBIDDEN detection) for a well-formed envelope
    # that happens to omit `message`.
    body = json.dumps({"error": {"code": "ENV_FORBIDDEN"}})
    with patch("requests.Session.request", return_value=_mock_response(403, text=body)):
        with pytest.raises(AuthorizationError) as exc_info:
            _client()._request("POST", "batch", site="testbed")
    assert "SS_CONTENT_API_ALLOW_POPULATE" in str(exc_info.value)
    assert exc_info.value.details["error_code"] == "ENV_FORBIDDEN"


_RECORD_UPDATE_GATE_CONFIG = client_module.RECORD_UPDATE_GATE_CONFIG


def test_403_env_forbidden_from_record_update_gate_names_its_own_config():
    # daisy-content#51: content_records_update (spec v1.18) has its own per-environment
    # switch, not the population gate. RecordUpdateHandler names its config
    # key in details[].config; the population advice would send the caller
    # to a remedy that doesn't open this endpoint.
    body = json.dumps(
        {
            "error": {
                "code": "ENV_FORBIDDEN",
                "message": 'Single-record updates are disabled in the "live" environment.',
                "details": [
                    {
                        "code": "ENV_FORBIDDEN",
                        "environment": "live",
                        "config": _RECORD_UPDATE_GATE_CONFIG,
                    }
                ],
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(403, text=body)):
        with pytest.raises(AuthorizationError) as exc_info:
            _client()._request("PATCH", "records/Page/1", site="testbed")
    msg = str(exc_info.value)
    assert "single-record updates disabled" in msg
    assert _RECORD_UPDATE_GATE_CONFIG in msg
    assert "does not open it" in msg
    assert "population_enabled_environments" not in msg
    assert "Single-record updates are disabled" in msg
    assert exc_info.value.details["error_code"] == "ENV_FORBIDDEN"


def test_403_env_forbidden_from_population_gate_keeps_population_advice():
    # The population gate's details carry envVar, not config — it must keep
    # the population message after the daisy-content#51 split.
    body = json.dumps(
        {
            "error": {
                "code": "ENV_FORBIDDEN",
                "message": 'Population endpoints are disabled in the "live" environment.',
                "details": [
                    {
                        "code": "ENV_FORBIDDEN",
                        "environment": "live",
                        "envVar": "SS_CONTENT_API_ALLOW_POPULATE",
                        "populationEnabledEnvironments": ["dev", "test"],
                    }
                ],
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(403, text=body)):
        with pytest.raises(AuthorizationError) as exc_info:
            _client()._request("POST", "batch", site="testbed")
    msg = str(exc_info.value)
    assert "population endpoints disabled" in msg
    assert "population_enabled_environments" in msg
    assert "single-record updates" not in msg


def test_record_update_gate_constant_matches_upstream_details_key():
    # The PATCH fallback names this key without reading it off the wire;
    # pin it to RecordUpdateHandler's own spelling (e26fdfd).
    assert _RECORD_UPDATE_GATE_CONFIG == (
        "Dynamic\\ContentApi\\Control\\Handlers\\"
        "RecordUpdateHandler.disabled_environments"
    )


def test_403_env_forbidden_on_patch_without_details_falls_back_to_update_gate():
    # An envelope whose details didn't survive must not send a PATCH caller
    # to the population remedy: only content_records_update sends PATCH.
    body = json.dumps({"error": {"code": "ENV_FORBIDDEN"}})
    with patch("requests.Session.request", return_value=_mock_response(403, text=body)):
        with pytest.raises(AuthorizationError) as exc_info:
            _client()._request("PATCH", "records/Page/1", site="testbed")
    msg = str(exc_info.value)
    assert "single-record updates disabled" in msg
    assert _RECORD_UPDATE_GATE_CONFIG in msg
    assert "population_enabled_environments" not in msg


def test_403_non_env_forbidden_with_config_detail_stays_generic():
    # The update-gate branch is scoped to ENV_FORBIDDEN: a permissions 403
    # whose details happen to carry `config` must not be told to edit
    # disabled_environments.
    body = json.dumps(
        {
            "error": {
                "code": "FORBIDDEN_CLASS",
                "message": "not permitted on this class",
                "details": [{"config": _RECORD_UPDATE_GATE_CONFIG}],
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(403, text=body)):
        with pytest.raises(AuthorizationError) as exc_info:
            _client()._request("PATCH", "records/Page/1", site="testbed")
    msg = str(exc_info.value)
    assert "refused this request (403)" in msg
    assert "single-record updates disabled" not in msg


@pytest.mark.parametrize(
    "details",
    [
        None,
        "config",
        {"config": "x"},
        [{"config": ""}],
        [{"config": "   "}],
        [{"config": 3}],
        ["config"],
        [{"config": "x", "envVar": "SS_CONTENT_API_ALLOW_POPULATE"}],
    ],
)
def test_record_update_gate_config_ignores_other_shapes(details):
    assert ContentClient._record_update_gate_config(details) is None


def test_record_update_gate_config_reads_a_later_entry():
    details = [{"envVar": "SS_CONTENT_API_ALLOW_POPULATE"}, {"config": "k"}]
    assert ContentClient._record_update_gate_config(details) == "k"


def test_403_upstream_details_included_and_capped():
    big_details = {"fields": ["x" * MAX_ERROR_BODY_LEN]}
    body = json.dumps(
        {
            "error": {
                "code": "FORBIDDEN_RECORD",
                "message": "not allowed",
                "details": big_details,
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(403, text=body)):
        with pytest.raises(AuthorizationError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    upstream_details_text = exc_info.value.details["upstream_details_text"]
    assert upstream_details_text.endswith("…(truncated)")
    assert len(upstream_details_text) < len(json.dumps(big_details))


def test_403_without_envelope_omits_error_code_and_details_keys():
    # A 403 with no parseable content-api envelope (the HTML login-interstitial
    # case, ROADMAP.md §A item 5) carries no upstream code or details payload,
    # and the keys are *omitted* rather than padded with None — so a consumer
    # must branch with `details.get("error_code")`, never assume presence.
    with patch(
        "requests.Session.request", return_value=_mock_response(403, text="Forbidden")
    ):
        with pytest.raises(AuthorizationError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert "error_code" not in exc_info.value.details
    assert "upstream_details_text" not in exc_info.value.details
    assert exc_info.value.details == {}


def test_403_without_json_envelope_preserves_upstream_body_no_env_gate_advice():
    with patch(
        "requests.Session.request", return_value=_mock_response(403, text="Forbidden")
    ):
        with pytest.raises(AuthorizationError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert "Forbidden" in str(exc_info.value)
    assert "SS_CONTENT_API_ALLOW_POPULATE" not in str(exc_info.value)


def test_error_envelope_with_large_details_is_also_capped():
    # A well-formed {"error": {...}} envelope is the common path for a
    # batch/compose failure, not the degenerate one — MAX_ERROR_BODY_LEN
    # must apply here too, not just on the raw-body fallback branch.
    big_details = {"operations": ["x" * MAX_ERROR_BODY_LEN]}
    body = json.dumps(
        {
            "error": {
                "code": "BATCH_FAILED",
                "message": "one or more operations failed",
                "details": big_details,
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(400, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    msg = str(exc_info.value)
    # The message stays short: details are no longer folded into it (daisy-content#30),
    # and the copy kept for the trailer is capped by content_api_mcp.core.errors.
    assert "x" * 50 not in msg
    assert msg.endswith("BATCH_FAILED: one or more operations failed")
    assert exc_info.value.details["upstream_details"] == big_details


# ---------------------------------------------------------------------------
# Errors that teach (daisy-content#21) — the five validation codes the issue
# names. Envelope shapes below are lifted from dynamic/silverstripe-content-api
# @ 53e62fb (the pin when this was written), not inferred: ELEMENT_NOT_ALLOWED_ON_PAGE
# (Write/RecordWriter.php::assertElementPlacementAllowed), PAYLOAD_INVALID
# enum rejections (e.g. Composition/CompositionService.php's prune.scope
# check), UNKNOWN_CLASS (Registry/ClassRegistry.php::resolve), UNKNOWN_FIELD/
# READONLY_FIELD (Write/WriteApplicator.php::applyFields), and UNRESOLVED_REF
# (Composition/CompositionService.php::processElements/resolveRefs) each
# compose their own `message`/`details` differently upstream — verified by
# reading the raise sites directly, not assumed uniform across codes.
# ---------------------------------------------------------------------------


def test_422_element_not_allowed_on_page_already_names_allowed_types():
    # No client-side branch needed: RecordWriter::assertElementPlacementAllowed
    # composes the allowed-types list directly into `message` upstream (no
    # `details` array at all — verified against the pinned module), and that
    # message already reaches str(exc) unchanged via the generic 4xx path.
    body = json.dumps(
        {
            "error": {
                "code": "ELEMENT_NOT_ALLOWED_ON_PAGE",
                "status": 422,
                "message": (
                    '"App\\Elements\\ElementVideo" is not an allowed element type on '
                    "App\\PageTypes\\LandingPage (daisy-content#42) — allowed types: "
                    "ElementContent, ElementCta."
                ),
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(422, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("POST", "compositions/page", site="testbed")
    msg = str(exc_info.value)
    assert "allowed types: ElementContent, ElementCta" in msg
    assert exc_info.value.status_code == 422


def test_400_payload_invalid_bad_enum_already_names_valid_values():
    # Same pass-through as ELEMENT_NOT_ALLOWED_ON_PAGE above: most
    # PAYLOAD_INVALID raise sites (e.g. CompositionService's prune.scope
    # check) compose the valid enum values directly into `message`, no
    # `details` needed.
    body = json.dumps(
        {
            "error": {
                "code": "PAYLOAD_INVALID",
                "status": 400,
                "message": 'prune.scope must be "managed" or "all".',
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(400, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("POST", "compositions/page", site="testbed")
    assert 'must be "managed" or "all"' in str(exc_info.value)
    assert exc_info.value.status_code == 400


def test_404_unknown_class_names_the_bad_ref_no_exposed_class_list():
    # ClassRegistry::resolve() supplies no `details` and no enumeration of
    # exposed classes — only the bad ref name. Documents the known gap
    # (daisy-content#21's PR body/issue comment) rather than claiming this
    # client invents data upstream never sent.
    body = json.dumps(
        {
            "error": {
                "code": "UNKNOWN_CLASS",
                "status": 404,
                "message": 'Unknown class reference "Widget".',
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(404, text=body)):
        with pytest.raises(NotFoundError) as exc_info:
            _client()._request("GET", "records/Widget", site="testbed")
    assert 'Unknown class reference "Widget"' in str(exc_info.value)


def test_422_unresolved_ref_names_the_unresolved_alias_no_defined_set():
    # CompositionService::processElements/resolveRefs supply no `details` —
    # only the failing $ref name(s), not which aliases were defined.
    body = json.dumps(
        {
            "error": {
                "code": "UNRESOLVED_REF",
                "status": 422,
                "message": 'Unresolved $ref "hero-image".',
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(422, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("POST", "compositions/page", site="testbed")
    assert 'Unresolved $ref "hero-image"' in str(exc_info.value)


def test_422_unknown_field_and_readonly_field_render_field_problems_as_prose():
    # The actual fix: WriteApplicator::applyFields's per-field `details`
    # array used to only reach str(exc) as a raw Python list-of-dicts repr
    # (via the generic message composition's `(details: {details})`
    # suffix) — legible to a human, not to an agent expected to self-correct
    # in one turn. Assert both the readable rendering AND the absence of the
    # old repr shape, so a revert back to the raw-repr fallback fails this
    # test rather than passing on substring luck.
    details = [
        {
            "field": "Subtitle",
            "code": "UNKNOWN_FIELD",
            "message": 'Unknown field "Subtitle" on App\\PageTypes\\LandingPage.',
        },
        {
            "field": "URLSegment",
            "code": "READONLY_FIELD",
            "message": 'Field "URLSegment" is not writable via the content API.',
        },
    ]
    body = json.dumps(
        {
            "error": {
                "code": "READONLY_FIELD",
                "status": 422,
                "message": "2 field(s) rejected.",
                "details": details,
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(422, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("PATCH", "records/LandingPage/5", site="testbed")
    msg = str(exc_info.value)
    assert (
        '"Subtitle" (UNKNOWN_FIELD): Unknown field "Subtitle" on App\\PageTypes\\LandingPage.'
        in msg
    )
    assert (
        '"URLSegment" (READONLY_FIELD): Field "URLSegment" is not writable via the content API.'
        in msg
    )
    # Pinned to the adjacent `"{error_code}: {count}"` shape only
    # `count_clause` produces — round-5 review finding: a bare "2 field(s)
    # rejected" in msg also matches this fixture's own upstream `message`
    # ("2 field(s) rejected."), separately echoed into the `(upstream: ...)`
    # clause, so it would keep passing even with `count_clause` wrong. Same
    # gap class round 4 flagged in two sibling tests and this file's other
    # count-guarding tests were already tightened for.
    assert "READONLY_FIELD: 2 field(s) rejected" in msg
    # The old raw-repr shape this replaces would contain a Python dict
    # literal fragment like "{'field':" — assert it's gone, not just that
    # the new text is present alongside it.
    assert "{'field':" not in msg
    assert exc_info.value.details["error_code"] == "READONLY_FIELD"
    assert exc_info.value.status_code == 422


def test_422_unknown_field_partial_malformed_entries_are_not_silently_dropped():
    # Review finding: counting from the *filtered* field_problems list (not
    # the raw `details` array) silently dropped an entry that didn't match
    # the expected {field, code, message} shape AND undercounted — worse
    # than the old raw-repr rendering, which at least showed every entry.
    # One well-formed entry, one missing "message" (the WriteApplicator
    # shape guarantees all three keys, but this client must not assume that
    # holds for every future upstream release).
    details = [
        {
            "field": "Subtitle",
            "code": "UNKNOWN_FIELD",
            "message": 'Unknown field "Subtitle" on Page.',
        },
        {
            "field": "URLSegment",
            "code": "READONLY_FIELD",
        },  # missing "message" — filtered out
    ]
    body = json.dumps(
        {
            "error": {
                "code": "UNKNOWN_FIELD",
                "status": 422,
                "message": "2 field(s) rejected.",
                "details": details,
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(422, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("PATCH", "records/Page/5", site="testbed")
    msg = str(exc_info.value)
    assert '"Subtitle" (UNKNOWN_FIELD): Unknown field "Subtitle" on Page.' in msg
    # The count is derived from `len(details)` (the raw array), not the
    # post-filter list — it must state the true total (2) even though only
    # 1 entry could be rendered by name. Pinned to the adjacent
    # `"{error_code}: {count}"` shape only `count_clause` produces — a bare
    # `"2 field(s) rejected" in msg` also matches this fixture's own
    # upstream `message` (also "2 field(s) rejected."), separately echoed
    # into the `(upstream: ...)` clause, so it would keep passing even with
    # `count_clause` wrong (verified by mutation, same class of gap round-4
    # review found in the two truncation-stress tests below).
    assert "UNKNOWN_FIELD: 2 field(s) rejected" in msg
    assert "UNKNOWN_FIELD: 1 field(s) rejected" not in msg


def test_422_readonly_field_preserves_upstream_message_verbatim():
    # Review finding: the prose rendering used to build its own "N field(s)
    # rejected" summary from scratch, discarding whatever upstream's own
    # top-level `message` actually said — unlike the ENV_FORBIDDEN/
    # ROLLBACK_UNVERIFIED precedent branches, which both append "Upstream
    # message: ...". A custom upstream message (not just the generic
    # WriteApplicator default) must still surface verbatim.
    details = [
        {
            "field": "Slug",
            "code": "READONLY_FIELD",
            "message": 'Field "Slug" is not writable via the content API.',
        }
    ]
    body = json.dumps(
        {
            "error": {
                "code": "READONLY_FIELD",
                "status": 422,
                "message": (
                    "1 field(s) rejected. Retry without Slug or use "
                    "content_records_stage instead."
                ),
                "details": details,
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(422, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("PATCH", "records/Page/5", site="testbed")
    msg = str(exc_info.value)
    assert "Retry without Slug or use content_records_stage instead" in msg
    assert (
        '"Slug" (READONLY_FIELD): Field "Slug" is not writable via the content API.'
        in msg
    )


def test_422_unknown_field_many_rejected_fields_count_survives_truncation():
    # Review finding: the count used to be composed only from what survived
    # into the caller-facing text (either the shape-filtered list, or later
    # the per-field text after truncation) — both let a length budget
    # silently undercount. `count_clause` is now `len(details)`, placed in
    # its own untruncated clause, so it can't be affected by how much of
    # the field-by-field breakdown fits.
    well_formed = [
        {
            "field": f"Field{i}",
            "code": "UNKNOWN_FIELD",
            "message": f'Unknown field "Field{i}" on App\\PageTypes\\LandingPage.',
        }
        for i in range(5)
    ]
    details = well_formed + [
        {"field": "Bad", "code": "READONLY_FIELD"}
    ]  # missing "message" — malformed
    body = json.dumps(
        {
            "error": {
                "code": "UNKNOWN_FIELD",
                "status": 422,
                "message": "6 field(s) rejected.",
                "details": details,
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(422, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("PATCH", "records/LandingPage/5", site="testbed")
    msg = str(exc_info.value)
    # Bounded by `reserved`/`field_budget`'s accounting (`reserved +
    # field_budget == MAX_ERROR_BODY_LEN` by construction), plus
    # `_truncate_body`'s own "…(truncated)" marker — round-7 review
    # finding: a loose `< MAX_ERROR_BODY_LEN + 150` slack passed even with
    # that budget-reservation deleted entirely (`field_budget =
    # MAX_ERROR_BODY_LEN`, no `reserved` subtracted), which pushed the real
    # message to 430 chars — 43% over the 300-char cap this branch exists
    # to respect — while still sliding under the loose assertion.
    assert len(msg) <= MAX_ERROR_BODY_LEN + len("…(truncated)")
    # The adjacent `"{error_code}: {count}"` shape only `count_clause` (the
    # untruncated true-total clause) can produce — round-4 review finding:
    # a loose `"6 field(s) rejected" in msg` also matches the fixture's own
    # upstream `message`, which is separately echoed verbatim into the
    # `(upstream: ...)` clause, so it passed even with `count_clause`
    # deleted entirely (verified by mutation).
    assert "UNKNOWN_FIELD: 6 field(s) rejected" in msg
    # At least the first field's name must survive in the NEW prose format
    # even though the full list of 5 can't all fit alongside the count.
    # Asserting on this exact adjacent shape (not just `'"Field0"' in msg`)
    # matters: upstream's own per-field `message` text already contains
    # quoted field names (`Unknown field "Field0" on ...`), so a looser
    # substring check would also match the OLD raw-Python-repr rendering
    # this branch replaces — it would pass on unfixed code too, and prove
    # nothing (verified: it did, before this was tightened).
    assert (
        '"Field0" (UNKNOWN_FIELD): Unknown field "Field0" on App\\PageTypes\\LandingPage.'
        in msg
    )
    assert msg.endswith("…(truncated)")  # the field list genuinely didn't all fit


def test_422_unknown_field_long_message_and_many_fields_both_truncate_but_count_holds():
    # Combined stress case (review finding: the two prior tests each
    # exercised only one truncation mechanism — a long `raw_message` OR
    # many fields — and each individually passed even with the other
    # mechanism's fix reverted). Force both a long upstream message AND
    # more fields than fit, and confirm the true count still survives.
    long_message = "6 field(s) rejected. " + ("Extra upstream context. " * 20)
    assert len(long_message) > 250
    details = [
        {
            "field": f"Field{i}",
            "code": "UNKNOWN_FIELD",
            "message": f'Unknown field "Field{i}" on App\\PageTypes\\LandingPage.',
        }
        for i in range(6)
    ]
    body = json.dumps(
        {
            "error": {
                "code": "UNKNOWN_FIELD",
                "status": 422,
                "message": long_message,
                "details": details,
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(422, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("PATCH", "records/LandingPage/5", site="testbed")
    msg = str(exc_info.value)
    # Same tightened discriminator as the test above — see its comment for
    # why the loose form doesn't actually pin `count_clause`.
    assert "UNKNOWN_FIELD: 6 field(s) rejected" in msg
    # Same tight bound as the test above — see its comment for why the
    # looser form doesn't actually pin the `reserved`/`field_budget`
    # accounting.
    assert len(msg) <= MAX_ERROR_BODY_LEN + len("…(truncated)")
    # Same discriminator as the test above: the exact adjacent
    # `"Field0" (CODE): message` shape only the new prose rendering
    # produces — a bare `'"Field0"' in msg` substring check would also
    # match upstream's own per-field message text embedded in the OLD
    # raw-Python-repr rendering, proving nothing.
    assert (
        '"Field0" (UNKNOWN_FIELD): Unknown field "Field0" on App\\PageTypes\\LandingPage.'
        in msg
    )


def test_422_unknown_field_long_upstream_message_still_shows_field_names():
    # Review finding: leading with the untruncated `raw_message` gave it no
    # budget of its own — a long upstream sentence could consume the entire
    # 300-char cap and leave zero field names, producing strictly less
    # information than the generic fallback this branch replaces.
    # `raw_message` now gets a fixed 100-char cap (`_truncate_body(...,
    # limit=100)` in the `upstream_clause` composition) so field prose
    # always has room left. Assert the FULL field entry survives, not just
    # a loose substring — round-4 review finding: a `'"Slug" (READONLY_
    # FIELD)' in msg` check alone doesn't actually exercise the cap, since
    # the `max(field_budget, 40)` floor rescues that much even with the cap
    # removed entirely; only the full entry (with its own `message` text)
    # proves there was real room left for it.
    long_message = "1 field(s) rejected. " + ("Extra upstream context. " * 20)
    assert len(long_message) > 250
    details = [
        {
            "field": "Slug",
            "code": "READONLY_FIELD",
            "message": 'Field "Slug" is not writable via the content API.',
        }
    ]
    body = json.dumps(
        {
            "error": {
                "code": "READONLY_FIELD",
                "status": 422,
                "message": long_message,
                "details": details,
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(422, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("PATCH", "records/Page/5", site="testbed")
    msg = str(exc_info.value)
    assert (
        '"Slug" (READONLY_FIELD): Field "Slug" is not writable via the content API.'
        in msg
    )


def test_422_unknown_field_without_details_falls_back_to_plain_message():
    # RecordsHandler's filter/sort validation raises UNKNOWN_FIELD with NO
    # `details` array (just a message naming the bad field) — the new
    # field_problems branch must not fire (nothing to render) and must not
    # swallow this upstream message.
    body = json.dumps(
        {
            "error": {
                "code": "UNKNOWN_FIELD",
                "status": 422,
                "message": 'Cannot filter on unknown field "Bogus".',
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(422, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert 'Cannot filter on unknown field "Bogus"' in str(exc_info.value)


def test_422_unknown_field_with_malformed_details_falls_back_to_plain_message():
    # `details` present but not the expected list-of-{field,code,message}
    # shape (e.g. a future/unrelated envelope reusing the same error code
    # with different details semantics) — the original class/status/message
    # must still surface verbatim, never a swallowed or blank error.
    body = json.dumps(
        {
            "error": {
                "code": "UNKNOWN_FIELD",
                "status": 422,
                "message": "1 field(s) rejected.",
                "details": ["not-a-dict"],
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(422, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("PATCH", "records/Page/5", site="testbed")
    msg = str(exc_info.value)
    # Round-6 review finding: a bare "1 field(s) rejected" in msg also
    # matches this fixture's upstream `message` AND what `count_clause`
    # would produce if the prose branch mistakenly fired for this
    # malformed-shape `details` (`len(details) == 1` too). So pin the
    # fallback's exact ending (upstream's own sentence) and the absence of
    # the prose branch's `(upstream: ...)` marker.
    assert msg.endswith("UNKNOWN_FIELD: 1 field(s) rejected.")
    assert "(upstream:" not in msg
    assert exc_info.value.details["upstream_details"] == ["not-a-dict"]
    assert exc_info.value.status_code == 422


def test_full_body_still_reaches_logger_uncapped():
    body = "v" * (MAX_ERROR_BODY_LEN + 500)
    client = _client()
    with (
        patch("requests.Session.request", return_value=_mock_response(500, text=body)),
        patch.object(client, "_logger") as mock_logger,
    ):
        with pytest.raises(ServiceError):
            client._request("GET", "records/Page", site="testbed")
    logged_body = mock_logger.error.call_args.args[-1]
    assert logged_body == body
    assert len(logged_body) > MAX_ERROR_BODY_LEN


# ---------------------------------------------------------------------------
# _resolve_path — {param} substitution, missing-arg detection.
# ---------------------------------------------------------------------------


def test_resolve_path_substitutes_single_param():
    path, remaining = ContentClient._resolve_path(
        "schema/{classRef}", {"classRef": "Page"}
    )
    assert path == "schema/Page"
    assert remaining == {}


def test_resolve_path_substitutes_multiple_params_and_keeps_leftovers():
    path, remaining = ContentClient._resolve_path(
        "records/{classRef}/{id}/{action}",
        {"classRef": "Page", "id": "5", "action": "publish", "mode": "subtree"},
    )
    assert path == "records/Page/5/publish"
    assert remaining == {"mode": "subtree"}


def test_resolve_path_supports_ext_id():
    path, remaining = ContentClient._resolve_path(
        "records/{classRef}/{id}", {"classRef": "Page", "id": "ext:home"}
    )
    assert path == "records/Page/ext:home"


def test_resolve_path_raises_mcperror_on_missing_argument():
    with pytest.raises(MCPError, match="missing required argument"):
        ContentClient._resolve_path("records/{classRef}/{id}", {"classRef": "Page"})


def test_resolve_path_one_arg_value_cannot_leak_into_another_placeholder():
    # Substitution is a single pass over the pristine template, so a value
    # containing literal "{id}" text is never re-substituted; it is encoded
    # as an opaque segment instead.
    path, remaining = ContentClient._resolve_path(
        "records/{classRef}/{id}", {"classRef": "{id}", "id": "5"}
    )
    assert path == "records/%7Bid%7D/5"
    assert remaining == {}


def test_resolve_path_refuses_a_dot_only_segment_as_a_400():
    with pytest.raises(MCPError, match=r"invalid value for path argument") as exc:
        ContentClient._resolve_path(
            "records/{classRef}/{id}", {"classRef": "Page", "id": ".."}
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# _flatten_query — filters object flattening, precedence rules.
# ---------------------------------------------------------------------------


def test_flatten_query_merges_filters_object():
    params = ContentClient._flatten_query(
        {"filters": {"Title__PartialMatch": "home"}, "sort": "-Created"}
    )
    assert params == {"Title__PartialMatch": "home", "sort": "-Created"}


def test_flatten_query_top_level_arg_wins_over_filters_collision():
    params = ContentClient._flatten_query(
        {"filters": {"_stage": "live"}, "_stage": "draft"}
    )
    assert params["_stage"] == "draft"


def test_flatten_query_drops_none_values():
    params = ContentClient._flatten_query({"limit": None, "offset": 0})
    assert "limit" not in params
    assert params["offset"] == 0


def test_flatten_query_ignores_non_dict_filters():
    params = ContentClient._flatten_query({"filters": "not-a-dict", "sort": "-Created"})
    assert params == {"sort": "-Created"}


def test_flatten_query_normalizes_bool_in_filters_to_int():
    # RecordsHandler::applyFilters feeds raw query strings straight into
    # DataList::filter() — a bare "True"/"False" (requests' default bool
    # serialization) coerces to 0 against a tinyint column in MySQL, so an
    # unnormalized filters={"ShowInMenus": True} would silently match the
    # opposite rows (daisy-content#8).
    #
    # NB: `params == {"ShowInMenus": 1}` alone would NOT discriminate here —
    # in Python `True == 1`, so that assertion passes on both the buggy
    # (bool) and fixed (int) output. Assert the exact type instead.
    params = ContentClient._flatten_query({"filters": {"ShowInMenus": True}})
    assert params == {"ShowInMenus": 1}
    assert type(params["ShowInMenus"]) is int


def test_flatten_query_normalizes_top_level_bool_to_int():
    # includeIds=False happened to survive upstream (FingerprintHandler
    # parses it with filter_var(..., FILTER_VALIDATE_BOOLEAN), which is
    # case-insensitive over true/false) — but normalizing at the source
    # keeps every boolean GET param correct instead of relying on that one
    # handler's parsing being forgiving. See the type-check note above —
    # `== 0`/`== 1` alone doesn't discriminate bool from int.
    params = ContentClient._flatten_query({"includeIds": False})
    assert params == {"includeIds": 0}
    assert type(params["includeIds"]) is int

    params = ContentClient._flatten_query({"includeIds": True})
    assert params == {"includeIds": 1}
    assert type(params["includeIds"]) is int


# ---------------------------------------------------------------------------
# call() — GET uses querystring, POST uses JSON body.
# ---------------------------------------------------------------------------


def test_call_get_sends_params_not_json_body():
    entry = {
        "method": "GET",
        "path": "records/{classRef}",
        "inputSchema": {"properties": {}},
    }
    with patch(
        "requests.Session.request", return_value=_mock_response(200, {"records": []})
    ) as m:
        _client().call("testbed", entry, {"classRef": "Page", "limit": 10})
    assert m.call_args.kwargs["params"] == {"limit": 10}
    assert m.call_args.kwargs["json"] is None
    assert m.call_args.kwargs["url"].endswith("/records/Page")


def test_call_post_sends_json_body_not_params():
    entry = {
        "method": "POST",
        "path": "batch",
        "inputSchema": {"properties": {}},
    }
    with patch(
        "requests.Session.request", return_value=_mock_response(200, {"results": []})
    ) as m:
        _client().call(
            "testbed", entry, {"operations": [{"op": "create", "class": "Page"}]}
        )
    assert m.call_args.kwargs["json"] == {
        "operations": [{"op": "create", "class": "Page"}]
    }


def test_call_forwards_timeout_to_the_request_on_get():
    # daisy-content#14 review: the generate_tools-level tests (test_server_
    # tools.py) only prove _dispatch hands the right value to a MOCKED
    # ContentClient.call() — they never exercise call() itself actually
    # forwarding it into _request()/the real session.request() call.
    # Mutation-verified: deleting `timeout=timeout` from call()'s GET branch
    # (content_mcp/client.py) leaves every other test in this file and
    # test_server_tools.py green — nothing but this test would catch it.
    entry = {
        "method": "GET",
        "path": "records/{classRef}",
        "inputSchema": {"properties": {}},
    }
    with patch(
        "requests.Session.request", return_value=_mock_response(200, {"records": []})
    ) as m:
        _client().call("testbed", entry, {"classRef": "Page"}, timeout=10)
    assert m.call_args.kwargs["timeout"] == 10


def test_call_forwards_timeout_to_the_request_on_post():
    # Same property as the GET test above, for call()'s POST branch — a
    # separate code path (content_mcp/client.py's `return self._request(
    # method, ...)` line) that the GET assertion above does not cover.
    # Mutation-verified the same way: deleting `timeout=timeout` from this
    # branch specifically leaves every other test green.
    entry = {
        "method": "POST",
        "path": "batch",
        "inputSchema": {"properties": {}},
    }
    with patch(
        "requests.Session.request", return_value=_mock_response(200, {"results": []})
    ) as m:
        _client().call("testbed", entry, {"operations": []}, timeout=120)
    assert m.call_args.kwargs["timeout"] == 120


def test_call_get_normalizes_bool_filter_to_int_on_the_wire():
    entry = {
        "method": "GET",
        "path": "records/{classRef}",
        "inputSchema": {"properties": {}},
    }
    with patch(
        "requests.Session.request", return_value=_mock_response(200, {"records": []})
    ) as m:
        _client().call(
            "testbed", entry, {"classRef": "Page", "filters": {"ShowInMenus": True}}
        )
    params = m.call_args.kwargs["params"]
    assert params == {"ShowInMenus": 1}
    # `== 1` alone doesn't discriminate (True == 1 in Python) — the thing
    # that actually matters is what reaches the wire, same repro shape as
    # the issue itself (`requests.Request(...).prepare().url`).
    assert type(params["ShowInMenus"]) is int
    wire_url = (
        requests.Request("GET", "https://example.com/x", params=params).prepare().url
    )
    assert wire_url.endswith("ShowInMenus=1")


def test_call_unknown_site_raises_before_any_request():
    entry = {"method": "GET", "path": "auth/session", "inputSchema": {"properties": {}}}
    with patch("requests.Session.request") as m:
        with pytest.raises(NotFoundError):
            _client().call("ghost", entry, {})
    m.assert_not_called()


def test_call_defaults_to_disallowing_file_path():
    # daisy-content#17/daisy-content#39 review: this is the actual structural property
    # the fix rests on — ContentClient.call() must be secure BY DEFAULT for
    # any caller, not just the one production caller (generate_tools's
    # _dispatch) that happens to pass allow_file_path=False today. Calls
    # .call() itself with NO allow_file_path argument at all, exactly as a
    # future caller that doesn't know this kwarg exists would call it.
    # Verified by mutation: flipping ContentClient.call()'s own default
    # back to True (while leaving _dispatch's explicit False untouched)
    # left every OTHER test in this file and test_server_tools.py green —
    # nothing but this test would have caught that regression.
    with patch("requests.Session.request") as m:
        with pytest.raises(MCPError, match="not available in this deployment"):
            _client().call(
                "testbed",
                _UPLOAD_ENTRY,
                {"filename": "x.png", "filePath": "/etc/passwd"},
            )
    m.assert_not_called()


# ---------------------------------------------------------------------------
# _resolve_file_path — the filePath->base64 convenience, gated on the spec
# entry declaring a "filePath" property (content_asset_upload only).
# ---------------------------------------------------------------------------

_UPLOAD_ENTRY = {
    "name": "content_asset_upload",
    "method": "POST",
    "path": "assets",
    "inputSchema": {
        "properties": {
            "filename": {"type": "string"},
            "base64": {"type": "string"},
            "filePath": {"type": "string"},
        }
    },
}

_NO_FILE_PATH_ENTRY = {
    "name": "content_records_list",
    "method": "GET",
    "path": "records/{classRef}",
    "inputSchema": {"properties": {"classRef": {"type": "string"}}},
}


def test_resolve_file_path_noop_for_entry_without_file_path_property():
    args = ContentClient._resolve_file_path(_NO_FILE_PATH_ENTRY, {"classRef": "Page"})
    assert args == {"classRef": "Page"}


def test_resolve_file_path_requires_one_of_base64_or_file_path():
    with pytest.raises(MCPError, match="Provide one of"):
        ContentClient._resolve_file_path(_UPLOAD_ENTRY, {"filename": "x.png"})


def test_resolve_file_path_rejects_both_base64_and_file_path():
    with pytest.raises(MCPError, match="not both"):
        ContentClient._resolve_file_path(
            _UPLOAD_ENTRY,
            {"filename": "x.png", "base64": "AA==", "filePath": "/tmp/x.png"},
        )


def test_resolve_file_path_passes_through_when_only_base64_given():
    args = ContentClient._resolve_file_path(
        _UPLOAD_ENTRY, {"filename": "x.png", "base64": "AA=="}
    )
    assert args == {"filename": "x.png", "base64": "AA=="}


def test_resolve_file_path_rejects_nonexistent_file():
    with pytest.raises(MCPError, match="does not exist"):
        ContentClient._resolve_file_path(
            _UPLOAD_ENTRY, {"filename": "x.png", "filePath": "/no/such/file-xyz.png"}
        )


def test_resolve_file_path_reads_and_base64_encodes_real_file():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"fake-png-bytes")
        tmp_path = f.name
    try:
        args = ContentClient._resolve_file_path(
            _UPLOAD_ENTRY, {"filename": "x.png", "filePath": tmp_path}
        )
        assert "filePath" not in args
        assert "base64" in args
        import base64 as b64mod

        assert b64mod.b64decode(args["base64"]) == b"fake-png-bytes"
    finally:
        Path(tmp_path).unlink()


@pytest.mark.parametrize(
    "suffix",
    [".env", "id_rsa", "id_ed25519", "secret.pem", "creds.key"],
)
def test_resolve_file_path_rejects_sensitive_looking_paths(suffix):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / suffix
        p.write_bytes(b"whatever")
        with pytest.raises(MCPError, match="credential/secret"):
            ContentClient._resolve_file_path(
                _UPLOAD_ENTRY, {"filename": "x", "filePath": str(p)}
            )


def test_resolve_file_path_rejects_oversize_file():
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.seek(MAX_FILE_PATH_BYTES)
        f.write(b"\0")
        tmp_path = f.name
    try:
        with pytest.raises(MCPError, match="over the"):
            ContentClient._resolve_file_path(
                _UPLOAD_ENTRY, {"filename": "x.bin", "filePath": tmp_path}
            )
    finally:
        Path(tmp_path).unlink()


# ---------------------------------------------------------------------------
# daisy-content#7 — error_code/details attached to every raised error, not
# just 403. The `*_envelope_exposes_*` tests and every 500/4xx-type
# assertion fail on pre-fix code: pre-fix, 401/404/500 `details` were `{}`,
# 429 `details` had no `error_code` key at all (just `retry_after`), and 500
# stayed a bare RuntimeError so the ServiceError/MCPError type assertions
# fail outright. The `*_without_envelope_*` tests are regression guards for
# the "both keys optional" contract, not new-behavior proofs — 401/404
# already yielded `details == {}` pre-fix and 429 already yielded
# `{"retry_after": ...}`, so those three pass on both pre- and post-fix code.
# ---------------------------------------------------------------------------


def test_401_envelope_exposes_error_code_and_details():
    body = json.dumps(
        {
            "error": {
                "code": "TOKEN_EXPIRED",
                "message": "token expired",
                "details": {"expiredAt": "2026-01-01"},
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(401, text=body)):
        with pytest.raises(AuthenticationError) as exc_info:
            _client()._request("GET", "auth/session", site="testbed")
    assert exc_info.value.details["error_code"] == "TOKEN_EXPIRED"
    assert "expiredAt" in exc_info.value.details["upstream_details_text"]


def test_401_without_envelope_omits_error_code_and_details_keys():
    with patch(
        "requests.Session.request",
        return_value=_mock_response(401, text="Unauthorized"),
    ):
        with pytest.raises(AuthenticationError) as exc_info:
            _client()._request("GET", "auth/session", site="testbed")
    assert exc_info.value.details == {}


def test_404_envelope_exposes_error_code_and_details():
    body = json.dumps(
        {
            "error": {
                "code": "RECORD_NOT_FOUND",
                "message": "no such record",
                "details": {"id": 999},
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(404, text=body)):
        with pytest.raises(NotFoundError) as exc_info:
            _client()._request("GET", "records/Page/999", site="testbed")
    assert exc_info.value.details["error_code"] == "RECORD_NOT_FOUND"
    assert "999" in exc_info.value.details["upstream_details_text"]


def test_404_without_envelope_omits_error_code_and_details_keys():
    with patch(
        "requests.Session.request", return_value=_mock_response(404, text="not found")
    ):
        with pytest.raises(NotFoundError) as exc_info:
            _client()._request("GET", "records/Page/999", site="testbed")
    assert exc_info.value.details == {}


def test_429_keeps_retry_after_and_also_carries_upstream_error_code():
    body = json.dumps(
        {"error": {"code": "RATE_LIMITED", "message": "too many requests"}}
    )
    with patch(
        "requests.Session.request",
        return_value=_mock_response(429, text=body, headers={"Retry-After": "30"}),
    ):
        with pytest.raises(RateLimitError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert exc_info.value.details["retry_after"] == "30"
    assert exc_info.value.details["error_code"] == "RATE_LIMITED"


def test_429_without_envelope_keeps_retry_after_only():
    with patch(
        "requests.Session.request",
        return_value=_mock_response(
            429, text="slow down", headers={"Retry-After": "5"}
        ),
    ):
        with pytest.raises(RateLimitError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert exc_info.value.details == {"retry_after": "5"}


def test_500_rollback_unverified_raises_service_error_with_distinct_message():
    body = json.dumps(
        {
            "error": {
                "code": "ROLLBACK_UNVERIFIED",
                "message": "batch commit status unknown",
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(500, text=body)):
        with pytest.raises(ServiceError, match="cannot confirm whether") as exc_info:
            _client()._request("POST", "batch", site="testbed")
    assert exc_info.value.details["error_code"] == "ROLLBACK_UNVERIFIED"
    assert exc_info.value.status_code == 500


def test_generic_500_raises_service_error():
    body = json.dumps(
        {"error": {"code": "INTERNAL_ERROR", "message": "unexpected failure"}}
    )
    with patch("requests.Session.request", return_value=_mock_response(500, text=body)):
        with pytest.raises(ServiceError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert exc_info.value.details["error_code"] == "INTERNAL_ERROR"
    assert exc_info.value.status_code == 500
    assert "ROLLBACK_UNVERIFIED" not in str(exc_info.value)


def test_503_raises_service_error_with_real_status_code_not_502_default():
    # ServiceError's class default is 502 — a bare `raise ServiceError(...)`
    # with no `status_code=` would silently report 502 for a real upstream
    # 503, discarding the actual status exactly like the bug this issue
    # exists to close. 503 (not 500) is chosen specifically so this can't
    # pass by coincidentally matching ServiceError's own default.
    body = json.dumps(
        {"error": {"code": "SERVICE_UNAVAILABLE", "message": "maintenance"}}
    )
    with patch("requests.Session.request", return_value=_mock_response(503, text=body)):
        with pytest.raises(ServiceError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert exc_info.value.status_code == 503


def test_500_without_envelope_omits_error_code_and_details_keys():
    with patch(
        "requests.Session.request",
        return_value=_mock_response(500, text="server exploded"),
    ):
        with pytest.raises(ServiceError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert exc_info.value.details == {}


def test_500_without_envelope_on_batch_warns_rollback_unverified_cannot_be_ruled_out():
    # Review finding: when _extract_upstream_error can't recognize the body
    # (any shape other than {"error": {...}}), error_code comes back None and
    # this used to fall straight into the plain "server error" message with
    # nothing distinguishing it from an ordinary transient 500 — even though
    # an unrecognized body could just as easily BE a ROLLBACK_UNVERIFIED this
    # client failed to parse. The message must say extraction failed and name
    # the specific risk, not just report the status code. Scoped to `batch`
    # (see the sibling GET test below) — this is the one endpoint the spec
    # says can actually return ROLLBACK_UNVERIFIED.
    with patch(
        "requests.Session.request",
        return_value=_mock_response(500, text="server exploded"),
    ):
        with pytest.raises(ServiceError, match="cannot be ruled out") as exc_info:
            _client()._request("POST", "batch", site="testbed")
    assert exc_info.value.status_code == 500
    assert exc_info.value.details == {}
    # Positive pin for the message distinction (review finding): a None
    # code must get the "could not be determined" message, not the
    # determined-but-undocumented one — deleting the `if not error_code:`
    # block would route it to the latter and fail this assertion.
    assert "could not be determined" in str(exc_info.value)


def test_500_without_envelope_on_non_batch_endpoint_omits_rollback_warning():
    # Round-2 review finding: ROLLBACK_UNVERIFIED appears in
    # schema/endpoints.json only in the content_batch entry — the warning
    # added above must NOT fire on every unrelated >=500 (a plain GET
    # failing has nothing to roll back), or it trains an operator to stop
    # reading the one warning that matters.
    with patch(
        "requests.Session.request",
        return_value=_mock_response(500, text="server exploded"),
    ):
        with pytest.raises(ServiceError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    assert "cannot be ruled out" not in str(exc_info.value)
    assert "ROLLBACK_UNVERIFIED" not in str(exc_info.value)
    assert exc_info.value.status_code == 500
    assert exc_info.value.details == {}


def test_500_with_recognized_non_rollback_error_code_on_batch_warns():
    # daisy-content#42 inverted the pre-daisy-content#42 discriminator from allowlist to
    # denylist: the branch is now gated on `path == "batch"` alone, with the
    # ROLLBACK_UNVERIFIED branch above as the sole exclusion. A recognized
    # INTERNAL_ERROR on batch — a determined-but-undocumented code — carries
    # zero commit information, so it MUST warn, and the message must name
    # the code rather than claim it "could not be determined". Pre-daisy-content#42 this
    # exact test (as ..._omits_warning) asserted the inverse; the code
    # change is what flipped it, not a wording tweak. Mutation this catches:
    # reverting the gate to `not error_code and path == "batch"` would let a
    # truthy INTERNAL_ERROR skip the warning and fall to the generic message,
    # failing the `match="cannot be ruled out"` below.
    body = json.dumps(
        {"error": {"code": "INTERNAL_ERROR", "message": "unexpected failure"}}
    )
    with patch("requests.Session.request", return_value=_mock_response(500, text=body)):
        with pytest.raises(ServiceError, match="cannot be ruled out") as exc_info:
            _client()._request("POST", "batch", site="testbed")
    msg = str(exc_info.value)
    assert "INTERNAL_ERROR" in msg
    assert "could not be determined" not in msg
    assert exc_info.value.details["error_code"] == "INTERNAL_ERROR"
    assert exc_info.value.status_code == 500


def test_500_with_blank_error_code_on_batch_still_warns():
    # Silent-failure-hunter confirmation-pass finding, still mutation-
    # tested post-daisy-content#42: a well-formed envelope can legitimately supply
    # {"code": ""} (accepted by _extract_upstream_error's `message or code`
    # gate via a non-empty message alone) — an empty string is a DIFFERENT
    # value from None. Post-daisy-content#42 both sub-branches warn, so the `not
    # error_code` gate (not `error_code is None`) decides which message a
    # blank code gets: it must get "could not be determined" (exactly as
    # unusable for ruling out ROLLBACK_UNVERIFIED as no code at all), not
    # be misreported as a determined-but-undocumented code. Pre-daisy-content#42 the
    # warn-vs-no-warn distinction carried this test; post-inversion the
    # positive message assertion below does.
    body = json.dumps({"error": {"code": "", "message": "blank code from upstream"}})
    with patch("requests.Session.request", return_value=_mock_response(500, text=body)):
        with pytest.raises(ServiceError, match="cannot be ruled out") as exc_info:
            _client()._request("POST", "batch", site="testbed")
    assert exc_info.value.status_code == 500
    # Positive pin for the message distinction (review finding): pre-daisy-content#42
    # every test on this path matched only the shared "cannot be ruled
    # out" substring, so mutating `not error_code` to `error_code is None`
    # passed the suite — the blank-code message was never positively
    # asserted.
    assert "could not be determined" in str(exc_info.value)
    # Deliberate, pinned asymmetry (review finding): the message says the
    # code "could not be determined" while `details["error_code"]` still
    # faithfully carries the blank string upstream sent, rather than being
    # omitted like a truly absent code. The documented contract is
    # presence-safety (`details.get("error_code")`, never `details[...]`,
    # per CLAUDE.md), not truthiness — a consumer branching truthily is
    # unaffected here too, but that's incidental, not what's promised. This
    # assertion pins the choice as intentional, not an accident nothing was
    # pinning.
    assert exc_info.value.details["error_code"] == ""


def test_502_gateway_shaped_numeric_code_on_batch_warns_and_names_code():
    # daisy-content#42: a fronting gateway can emit a content-api-shaped
    # envelope with a NUMERIC, undocumented code that _extract_upstream_error
    # parses fine (code is truthy) but that the pinned spec documents nowhere
    # as ruling out a commit. Pre-daisy-content#42 a parsed code meant "determined", so
    # this suppressed the warning entirely — exactly the gap the allowlist
    # inversion closes. Now it must warn and name the code. The HTTP status
    # (500) is deliberately distinct from the named code (502) so the test
    # can't pass by matching the status where it should match the code. The
    # assertion pins the NAMED CLAUSE, not a bare substring: with a
    # message-carrying envelope the code also appears in the composed
    # "code: message" text, so a bare `in msg` would pass for the wrong
    # reason (the code-only shape, where the clause is the ONLY channel, is
    # pinned by the next test).
    body = json.dumps({"error": {"code": 502, "message": "upstream gateway error"}})
    with patch("requests.Session.request", return_value=_mock_response(500, text=body)):
        with pytest.raises(ServiceError, match="cannot be ruled out") as exc_info:
            _client()._request("POST", "batch", site="testbed")
    msg = str(exc_info.value)
    assert "the upstream error code (502) is not documented" in msg
    assert "could not be determined" not in msg
    assert exc_info.value.details["error_code"] == 502
    assert exc_info.value.status_code == 500


def test_500_code_only_envelope_names_code_truncated_from_raw_body():
    # daisy-content#42 (review finding): on _extract_upstream_error's
    # code-only path (envelope has `code` but no `message`) the caller-
    # facing message is the RAW truncated body, where the code appears
    # only incidentally — a large sibling field pushes it past the
    # truncation cut, and details["error_code"] does not cross the FastMCP
    # tools/call boundary, so the message text is an MCP caller's only
    # channel. The determined-but-undocumented branch must therefore name
    # the code itself. Pre-fix this exact body produced a message with no
    # trace of WEIRD_CODE at all (the operator was told a code existed and
    # was undocumented, with no way to see what it was).
    body = json.dumps({"error": {"details": "x" * 400, "code": "WEIRD_CODE"}})
    with patch("requests.Session.request", return_value=_mock_response(500, text=body)):
        with pytest.raises(ServiceError, match="cannot be ruled out") as exc_info:
            _client()._request("POST", "batch", site="testbed")
    msg = str(exc_info.value)
    # The raw body is ~450 chars against a 300-char cap, so the trailing
    # "code" field is truncated out of `message` — any WEIRD_CODE in the
    # exception text comes from the named clause, not the raw body.
    assert "the upstream error code (WEIRD_CODE) is not documented" in msg
    assert "could not be determined" not in msg
    assert exc_info.value.details["error_code"] == "WEIRD_CODE"
    assert exc_info.value.status_code == 500


def test_500_non_scalar_code_on_batch_names_situation_not_repr():
    # daisy-content#42 (review finding): a dict/list `code` is a valid JSON
    # shape, and the shape guard's purpose is to keep it out of the NAMED
    # CLAUSE as a raw Python repr — daisy-content#21's exact failure mode.
    # The envelope is deliberately code-only (no `message`): with a
    # message-carrying envelope, _extract_upstream_error's own composed
    # "code: message" text would str() the dict into `message` (a
    # pre-existing behavior of that shared function, outside this branch's
    # scope), so the code-only shape is the one that isolates the named
    # clause — the unit this guard protects. Its raw-body message is JSON
    # syntax, not a Python repr, so any `{'a': 1}` in the exception text
    # would come from the named clause.
    body = json.dumps({"error": {"code": {"a": 1}}})
    with patch("requests.Session.request", return_value=_mock_response(500, text=body)):
        with pytest.raises(ServiceError, match="cannot be ruled out") as exc_info:
            _client()._request("POST", "batch", site="testbed")
    msg = str(exc_info.value)
    assert "the upstream error code is not documented" in msg
    assert "{'a': 1}" not in msg
    assert "could not be determined" not in msg
    # The structured channel still carries the dict faithfully — the same
    # pinned asymmetry test_500_with_blank_error_code_on_batch_still_warns
    # documents for the blank string.
    assert exc_info.value.details["error_code"] == {"a": 1}
    assert exc_info.value.status_code == 500


def test_500_overlong_code_on_batch_is_capped_in_named_clause():
    # daisy-content#42 (review finding): a fronting gateway can put
    # arbitrary content in `code`, and _extract_upstream_error returns it
    # untouched (only `message` goes through _truncate_body), so the named
    # clause caps the value at 64 chars — a 5 KB code must not become a 5
    # KB exception message. Code-only envelope, same isolation reasoning as
    # the sibling test above: the raw body is ~425 chars against a 300-char
    # cap, so the trailing code is truncated out of `message` (280 of the
    # 400 Z's survive), and any 64-Z run with the truncation marker in the
    # exception text comes from the named clause.
    body = json.dumps({"error": {"code": "Z" * 400}})
    with patch("requests.Session.request", return_value=_mock_response(500, text=body)):
        with pytest.raises(ServiceError, match="cannot be ruled out") as exc_info:
            _client()._request("POST", "batch", site="testbed")
    msg = str(exc_info.value)
    assert f"the upstream error code ({'Z' * 64}…(truncated)) is not documented" in msg
    assert "Z" * 400 not in msg
    assert exc_info.value.details["error_code"] == "Z" * 400
    assert exc_info.value.status_code == 500


def test_transport_failure_on_write_batch_carries_commit_uncertainty_hazard():
    # daisy-content#42: a ReadTimeout (or any RequestException) on a
    # non-GET `batch` call means no usable response could be read — the
    # strongest commit-uncertainty signal this client can emit, since there
    # is nothing to read the commit status out of. The raised RuntimeError
    # must carry the hazard sentence, and `from e` must preserve the
    # underlying exception as __cause__ (pre-daisy-content#42 this branch raised a bare
    # RuntimeError with no cause and no hazard).
    cause = requests.exceptions.ReadTimeout("Read timed out.")
    with patch("requests.Session.request", side_effect=cause):
        with pytest.raises(RuntimeError) as exc_info:
            _client()._request("POST", "batch", site="testbed")
    msg = str(exc_info.value)
    assert "failed before a usable response could be read" in msg
    assert "cannot be determined whether the batch committed" in msg
    assert "verify the site's data before retrying" in msg
    # `from e` must preserve the ORIGINAL exception instance, not merely an
    # equivalent one — identity, not isinstance (a re-raised copy would pass
    # an isinstance check and still lose the original's traceback context).
    assert exc_info.value.__cause__ is cause


def test_transport_failure_on_get_omits_commit_uncertainty_hazard():
    # daisy-content#42: the hazard sentence is gated on non-GET method +
    # `batch` path. A GET can't have committed anything, so a transport
    # failure on a GET must NOT carry the hazard text — otherwise it would
    # train an operator to treat every timeout as a possible duplicate
    # commit. The base failure message and the preserved cause are asserted
    # positively, so this discriminates the gate's scope rather than merely
    # asserting an absence.
    with patch(
        "requests.Session.request",
        side_effect=requests.exceptions.ReadTimeout("Read timed out."),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            _client()._request("GET", "records/Page", site="testbed")
    msg = str(exc_info.value)
    assert "ContentClient request failed for site" in msg
    assert "Read timed out" in msg
    assert "failed before a usable response could be read" not in msg
    assert "cannot be determined whether the batch committed" not in msg
    assert isinstance(exc_info.value.__cause__, requests.exceptions.ReadTimeout)


def test_transport_failure_connection_error_on_write_batch_carries_hazard():
    # daisy-content#42: a ConnectionError on a non-GET `batch` call
    # MUST carry the hazard sentence. The requests hierarchy does not
    # expose a reliable reached-the-server distinction: in requests
    # 2.31-2.34 (the whole pin range, verified against both endpoints) a
    # read timeout while the BODY is streaming back surfaces as
    # ConnectionError — Response.iter_content wraps urllib3's
    # ReadTimeoutError as ConnectionError — so the server accepted the
    # request and started answering, the batch may have committed, and a
    # class-based exclusion would drop the hazard exactly there. The cost
    # of not excluding is a connection-refused batch getting one harmless
    # "verify" nudge on a retry that was safe; the cost of excluding is
    # silence on a stall where the batch may have committed. Safe-by-
    # default wins, same call as the 5xx branch. The base failure message
    # and the preserved cause are asserted positively, so this
    # discriminates the gate rather than merely asserting an absence.
    cause = requests.exceptions.ConnectionError("Connection refused.")
    with patch("requests.Session.request", side_effect=cause):
        with pytest.raises(RuntimeError) as exc_info:
            _client()._request("POST", "batch", site="testbed")
    msg = str(exc_info.value)
    assert "ContentClient request failed for site" in msg
    assert "Connection refused" in msg
    assert "failed before a usable response could be read" in msg
    assert "cannot be determined whether the batch committed" in msg
    assert "verify the site's data before retrying" in msg
    assert exc_info.value.__cause__ is cause


def test_transport_failure_on_get_batch_omits_commit_uncertainty_hazard():
    # daisy-content#42: the METHOD half of the gate, exercised on the
    # `batch` PATH itself (test_transport_failure_on_write_non_batch_
    # omits_commit_uncertainty_hazard exercises the path half on a write
    # method). A GET on `batch` can't have committed anything, so a
    # transport failure there must NOT carry the hazard text. The
    # spec's content_batch entry is POST-only, but _request is the shared
    # dispatch every tool maps onto, so the gate must hold for a GET that
    # reaches the batch path regardless.
    with patch(
        "requests.Session.request",
        side_effect=requests.exceptions.ReadTimeout("Read timed out."),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            _client()._request("GET", "batch", site="testbed")
    msg = str(exc_info.value)
    assert "ContentClient request failed for site" in msg
    assert "Read timed out" in msg
    assert "failed before a usable response could be read" not in msg
    assert "cannot be determined whether the batch committed" not in msg
    assert isinstance(exc_info.value.__cause__, requests.exceptions.ReadTimeout)


def test_transport_failure_on_write_non_batch_omits_commit_uncertainty_hazard():
    # daisy-content#42: the PATH half of the gate, exercised on a WRITE
    # method (the GET siblings above exercise the method half): a POST on
    # a non-`batch` path must NOT carry the hazard text — commit-
    # uncertainty is a `batch`-specific risk per the pinned spec, and a
    # verify-before-retry warning on every other write endpoint's timeout
    # is the alarm fatigue the 5xx branch's scoping comment exists to
    # prevent (the 5xx path's equivalent guard is
    # test_500_without_envelope_on_non_batch_endpoint_omits_rollback_
    # warning). Without this test the gate's `path == "batch"` half is
    # unpinned: mutating the gate to `method.upper() != "GET"` alone —
    # hazard on every non-GET transport failure — passes the rest of the
    # suite.
    with patch(
        "requests.Session.request",
        side_effect=requests.exceptions.ReadTimeout("Read timed out."),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            _client()._request("POST", "compositions/page", site="testbed")
    msg = str(exc_info.value)
    assert "ContentClient request failed for site" in msg
    assert "Read timed out" in msg
    assert "failed before a usable response could be read" not in msg
    assert "cannot be determined whether the batch committed" not in msg
    assert isinstance(exc_info.value.__cause__, requests.exceptions.ReadTimeout)


def test_batch_path_matches_spec():
    # Round-2-of-round-2 review finding: client.py's `path == "batch"` guard
    # (the two tests above) is a plain string literal, decoupled from the
    # pinned spec it's meant to track. A future schema/endpoints.json that
    # moves content_batch's path (e.g. to "batch/run") would silently stop
    # matching — the safety warning would vanish with nothing going red,
    # since the two tests above hardcode "batch" as their own request path
    # too rather than reading it from the spec. This test is the one place
    # that actually reads the spec, so a path rename fails HERE loudly
    # instead of being caught only by the unrelated check-spec-sync.sh drift
    # check (which wouldn't connect a path change to this specific branch).
    spec = spec_module.load_spec()
    batch_entries = [t for t in spec["tools"] if t["name"] == "content_batch"]
    assert len(batch_entries) == 1
    assert batch_entries[0]["path"] == "batch"
    # And the inverse: ROLLBACK_UNVERIFIED must still appear nowhere else in
    # the spec, or the batch-only scoping itself needs revisiting, not just
    # the path string.
    other_entries_mentioning_it = [
        t
        for t in spec["tools"]
        if t["name"] != "content_batch" and "ROLLBACK_UNVERIFIED" in json.dumps(t)
    ]
    assert other_entries_mentioning_it == []


def test_422_raises_mcperror_with_status_code_and_upstream_error_code():
    body = json.dumps(
        {"error": {"code": "UNKNOWN_FIELD", "message": "Foo is not a field on Page"}}
    )
    with patch("requests.Session.request", return_value=_mock_response(422, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("POST", "compositions/page", site="testbed")
    assert exc_info.value.status_code == 422
    assert exc_info.value.details["error_code"] == "UNKNOWN_FIELD"
    # type(...) is, not isinstance() — ServiceError IS an MCPError, so
    # isinstance(exc, MCPError) is satisfied by a >=500 branch too. Review
    # finding: without this, widening the `status >= 500` threshold to
    # `status >= 400` (an off-by-one, or a well-meaning "type everything"
    # edit) would reclassify every 4xx as a retryable ServiceError and every
    # test in this block would still pass, since `status_code=status` is
    # explicit on both branches.
    assert type(exc_info.value) is MCPError


def test_400_without_envelope_omits_error_code_and_details_keys():
    with patch(
        "requests.Session.request", return_value=_mock_response(400, text="Bad Request")
    ):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("POST", "compositions/page", site="testbed")
    assert exc_info.value.status_code == 400
    assert exc_info.value.details == {}
    assert type(exc_info.value) is MCPError


def test_409_raises_mcperror_with_status_code():
    body = json.dumps(
        {
            "error": {
                "code": "URLSEGMENT_COLLISION",
                "message": "URLSegment already in use",
            }
        }
    )
    with patch("requests.Session.request", return_value=_mock_response(409, text=body)):
        with pytest.raises(MCPError) as exc_info:
            _client()._request("POST", "compositions/page", site="testbed")
    assert exc_info.value.status_code == 409
    assert exc_info.value.details["error_code"] == "URLSEGMENT_COLLISION"
    assert type(exc_info.value) is MCPError


# ---------------------------------------------------------------------------
# #36: list filters become the module's comma-separated IN list; nested
# objects are refused instead of being mangled into `Key=firstkey`.
# ---------------------------------------------------------------------------


def test_flatten_query_joins_a_list_filter_into_an_in_list():
    params = ContentClient._flatten_query(
        {"filters": {"ID": [1, 2, 3], "Flag": [True]}}
    )
    assert params == {"ID": "1,2,3", "Flag": "1"}


def test_flatten_query_refuses_a_list_item_containing_a_comma():
    from content_api_mcp._base.errors import ValidationError

    with pytest.raises(ValidationError, match="contains a comma"):
        ContentClient._flatten_query({"filters": {"Title": ["a,b", "c"]}})


def test_flatten_query_refuses_a_nested_object_or_nested_list():
    from content_api_mcp._base.errors import ValidationError

    with pytest.raises(ValidationError, match="Field__Modifier"):
        ContentClient._flatten_query({"filters": {"Nested": {"a": 1}}})
    with pytest.raises(ValidationError, match="must be scalars"):
        ContentClient._flatten_query({"filters": {"ID": [[1, 2]]}})


def test_list_filter_reaches_the_wire_as_one_comma_joined_param():
    with patch(
        "requests.Session.request", return_value=_mock_response(200, json_data={})
    ) as m:
        _client()._request(
            "GET",
            "records/Page",
            site="testbed",
            params=ContentClient._flatten_query({"filters": {"ID": [1, 2]}}),
        )
    assert m.call_args.kwargs["params"] == {"ID": "1,2"}
