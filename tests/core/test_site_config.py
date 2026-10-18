"""SiteConfig's token_file / ca_file (from the standalone server's #23 / #32),
and the core's independence from DAISY."""

import ast
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import _mock_response

from content_api_mcp._base.errors import AuthenticationError
from content_api_mcp.core.client import ContentClient, SiteConfig, parse_site_registry

BASE = "https://example.com/content-api/v1"


def test_token_file_is_re_read_on_every_request(tmp_path):
    token_file = tmp_path / "site.token"
    token_file.write_text("first\n")
    site = SiteConfig(base_url=BASE, token="first", token_file=str(token_file))
    client = ContentClient({"s": site})
    with patch(
        "requests.Session.request", return_value=_mock_response(200, json_data={})
    ) as m:
        client._request("GET", "auth/session", site="s")
        token_file.write_text("rotated\n")
        client._request("GET", "auth/session", site="s")
    headers = [c.kwargs["headers"]["X-Silverstripe-Apitoken"] for c in m.call_args_list]
    assert headers == ["first", "rotated"]


def test_a_missing_or_empty_token_file_is_an_authentication_error(tmp_path):
    missing = SiteConfig(base_url=BASE, token="t", token_file=str(tmp_path / "gone"))
    with pytest.raises(AuthenticationError, match="could not be read"):
        missing.current_token()
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    with pytest.raises(AuthenticationError, match="is empty"):
        SiteConfig(base_url=BASE, token="t", token_file=str(empty)).current_token()


def test_ca_file_is_sent_as_verify_per_request():
    site = SiteConfig(base_url=BASE, token="t", ca_file="/etc/ssl/private-ca.pem")
    with patch(
        "requests.Session.request", return_value=_mock_response(200, json_data={})
    ) as m:
        ContentClient({"s": site})._request("GET", "auth/session", site="s")
    assert m.call_args.kwargs["verify"] == "/etc/ssl/private-ca.pem"


def test_no_ca_file_leaves_verify_to_requests():
    with patch(
        "requests.Session.request", return_value=_mock_response(200, json_data={})
    ) as m:
        ContentClient({"s": SiteConfig(base_url=BASE, token="t")})._request(
            "GET", "auth/session", site="s"
        )
    assert "verify" not in m.call_args.kwargs


def test_registry_accepts_token_file_and_ca_file(tmp_path):
    token_file = tmp_path / "acme.token"
    token_file.write_text("from-file\n")
    sites = parse_site_registry(
        json.dumps(
            {
                "acme": {
                    "base_url": BASE,
                    "token_file": str(token_file),
                    "ca_file": "~/ca.pem",
                }
            }
        )
    )
    assert sites["acme"].token == "from-file"
    assert sites["acme"].token_file == str(token_file)
    assert sites["acme"].ca_file == str(Path("~/ca.pem").expanduser())


def test_registry_reports_an_unreadable_token_file(tmp_path):
    raw = json.dumps({"acme": {"base_url": BASE, "token_file": str(tmp_path / "x")}})
    with pytest.raises(ValueError, match="token_file could not be read"):
        parse_site_registry(raw)


def test_no_module_imports_mcp_base_or_anything_daisy():
    # The package must install and run with no access to DAISY's private
    # repos: nothing under content_api_mcp may import mcp_base or a daisy
    # package.
    package = Path(__file__).resolve().parents[2] / "content_api_mcp"
    offenders = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                if root in ("mcp_base", "content_mcp") or root.startswith("daisy"):
                    offenders.append(f"{path.relative_to(package)}: {name}")
    assert offenders == []
