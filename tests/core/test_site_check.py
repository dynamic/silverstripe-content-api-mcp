"""content_check_sites: per-site token status that never fails on one bad site (#44)."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import requests
from conftest import SiteConfig, _mock_response, capture_meta_tools

from content_api_mcp.core import site_check

EXPIRES = 1792882188  # 2026-10-24T22:49:48Z


def _at(days_before_expiry):
    return datetime.fromtimestamp(EXPIRES - days_before_expiry * 86400, UTC)


def _session(expires=EXPIRES):
    data = {"memberId": 1, "permissions": ["CONTENT_API_ACCESS"]}
    if expires is not None:
        data["expires"] = expires
    return {"data": data, "meta": {}, "error": None}


def _unauth(code="TOKEN_EXPIRED"):
    return {
        "data": None,
        "error": {"code": code, "status": 401, "message": "Token invalid."},
    }


def _sites(*slugs):
    return {
        slug: SiteConfig(base_url=f"https://{slug}.test/content-api/v1", token="t")
        for slug in slugs
    }


def _routes(by_host):
    """requests.Session.request stub: reply per site host, or raise."""

    def request(method, url, **kwargs):
        host = url.split("//")[1].split("/")[0].split(".")[0]
        reply = by_host[host]
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        if status >= 400:
            return _mock_response(status, text=json.dumps(body))
        return _mock_response(status, body)

    return MagicMock(side_effect=request)


def _client_for(*slugs):
    from content_api_mcp.core.client import ContentClient

    return ContentClient(sites=_sites(*slugs))


def test_ok_expiring_expired_in_slug_order():
    client = _client_for("good", "soon", "gone")
    routes = {
        "good": (200, _session(EXPIRES)),
        "soon": (200, _session(EXPIRES)),
        "gone": (200, _session(EXPIRES)),
    }
    # One shared clock cannot make three sites differ, so give each its own
    # expiry relative to a fixed "now" 10 days before EXPIRES.
    now = _at(10)
    routes["soon"] = (200, _session(EXPIRES - 7 * 86400))  # 3 days left
    routes["gone"] = (200, _session(EXPIRES - 12 * 86400))  # 2 days past
    with patch("requests.Session.request", _routes(routes)):
        out = site_check.check_sites(client, now=now)
    # list_sites() is slug-sorted
    assert [r["site"] for r in out["sites"]] == ["gone", "good", "soon"]
    assert [r["status"] for r in out["sites"]] == ["expired", "ok", "expiring"]
    assert out["needs_attention"] == ["gone", "soon"]
    assert out["sites"][0]["expiresInDays"] == -2.0
    assert out["sites"][1]["expiresInDays"] == 10.0
    assert out["sites"][2]["expiresInDays"] == 3.0
    assert out["warn_days"] == 7 and out["checked_at"] == now.isoformat()


def test_boundary_is_strictly_under_warn_days():
    client = _client_for("a")
    with patch("requests.Session.request", _routes({"a": (200, _session())})):
        assert site_check.check_sites(client, 7, now=_at(7))["sites"][0]["status"] == "ok"
        assert (
            site_check.check_sites(client, 7, now=_at(6.9))["sites"][0]["status"]
            == "expiring"
        )
        assert (
            site_check.check_sites(client, 14, now=_at(10))["sites"][0]["status"]
            == "expiring"
        )


def test_a_401_token_expired_is_expired_and_reachable():
    client = _client_for("a")
    with patch("requests.Session.request", _routes({"a": (401, _unauth())})):
        row = site_check.check_sites(client, now=_at(1))["sites"][0]
    assert row["status"] == "expired" and row["reachable"] is True
    assert row["detail"]["status"] == 401


def test_a_401_other_code_is_an_invalid_token():
    client = _client_for("a")
    with patch(
        "requests.Session.request", _routes({"a": (401, _unauth("UNAUTHENTICATED"))})
    ):
        row = site_check.check_sites(client, now=_at(1))["sites"][0]
    assert row["status"] == "invalid_token" and row["reachable"] is True


def test_transport_failure_and_5xx_are_unreachable():
    client = _client_for("down", "boom")
    routes = {
        "down": requests.exceptions.ConnectionError("refused"),
        "boom": (503, {"error": {"code": "X", "status": 503}}),
    }
    with patch("requests.Session.request", _routes(routes)):
        rows = site_check.check_sites(client, now=_at(1))["sites"]
    assert [r["status"] for r in rows] == ["unreachable", "unreachable"]
    assert all(r["reachable"] is False for r in rows)


def test_one_bad_site_does_not_fail_the_others():
    client = _client_for("good", "down")
    routes = {
        "good": (200, _session()),
        "down": requests.exceptions.ConnectionError("refused"),
    }
    with patch("requests.Session.request", _routes(routes)):
        out = site_check.check_sites(client, now=_at(30))
    assert [r["site"] for r in out["sites"]] == ["down", "good"]
    assert [r["status"] for r in out["sites"]] == ["unreachable", "ok"]
    assert out["needs_attention"] == ["down"]


def test_a_reply_without_an_expiry_is_an_error_not_ok():
    client = _client_for("a")
    with patch("requests.Session.request", _routes({"a": (200, _session(None))})):
        row = site_check.check_sites(client, now=_at(1))["sites"][0]
    assert row["status"] == "error" and row["detail"]["kind"] == "no_expiry"


def test_no_configured_sites_is_an_empty_clean_result():
    out = site_check.check_sites(_client_for(), now=_at(1))
    assert out["sites"] == [] and out["needs_attention"] == []


@pytest.mark.parametrize("bad", [0, -1, "7", None, True])
def test_bad_warn_days_is_refused(bad):
    with pytest.raises(ValueError, match="warn_days"):
        site_check.check_sites(_client_for("a"), bad)


def test_no_token_or_url_appears_in_the_result():
    client = _client_for("a")
    with patch("requests.Session.request", _routes({"a": (200, _session())})):
        out = site_check.check_sites(client, now=_at(30))
    text = json.dumps(out)
    assert "a.test" not in text and '"t"' not in text


def test_the_tool_is_registered_read_only_and_runs():
    tools, client = capture_meta_tools()
    tool = tools["content_check_sites"]
    assert tool._mcp_tool_kwargs["annotations"]["readOnlyHint"] is True
    client.list_sites.return_value = []
    import asyncio

    out = asyncio.run(tool())
    assert out["sites"] == [] and out["warn_days"] == 7


def test_a_token_expiring_this_instant_is_expired():
    client = _client_for("a")
    with patch("requests.Session.request", _routes({"a": (200, _session())})):
        row = site_check.check_sites(client, now=_at(0))["sites"][0]
    assert row["status"] == "expired" and row["expiresInDays"] == 0.0
