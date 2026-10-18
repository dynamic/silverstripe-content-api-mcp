"""daisy-content#18 (content_get_started, schema cache, default site) and daisy-content#21's
UNKNOWN_CLASS teaching. Envelope shapes are from live daisy-content#10 testbed responses
(module 2.9.0): auth/session `{data: {memberId, email, permissions,
expires}}`, a 404 `{error: {code: "UNKNOWN_CLASS", status: 404, message:
'Unknown class reference "Nope".'}}`.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from conftest import (
    SiteConfig,
    _client,
    _mock_response,
    capture_meta_tools,
    capture_spec_tools,
    raises_tool_error,
)

from content_api_mcp._base.errors import NotFoundError, ValidationError
from content_api_mcp.core import bootstrap
from content_api_mcp.core.client import DEFAULT_SCHEMA_CACHE_TTL, schema_cache_ttl
from content_api_mcp.core.tools import load_spec

SCHEMA_SITE = {
    "data": {
        "environment": "dev",
        "populationEnabled": True,
        "recordUpdateEnabled": True,
        "crud": {"models": ["Page"]},
        "integrations": {"elemental": True, "linkfield": True},
        "classes": {
            "Page": {"page": True, "element": False},
            "BlockPage": {"page": True, "element": False},
            "ElementContent": {"page": False, "element": True},
            "Image": {"page": False, "element": False},
        },
    },
    "meta": {},
    "error": None,
}

SESSION = {
    "data": {
        "memberId": 6,
        "email": "svc@example.com",
        "permissions": [
            "CONTENT_API_ACCESS",
            "CONTENT_API_POPULATE",
            "VIEW_DRAFT_CONTENT",
        ],
        "expires": 1792882188,
    },
    "meta": {},
    "error": None,
}

UNKNOWN_CLASS = {
    "data": None,
    "meta": {},
    "error": {
        "code": "UNKNOWN_CLASS",
        "status": 404,
        "message": 'Unknown class reference "Nope".',
    },
}


def _ok(payload):
    return _mock_response(200, json_data=payload)


def _routed(routes):
    """A requests.Session.request stand-in answering by URL suffix."""

    def request(method, url, **kwargs):
        for suffix, response in routes.items():
            if url.endswith(suffix):
                return response() if callable(response) else response
        raise AssertionError(f"unexpected request {method} {url}")

    return MagicMock(side_effect=request)


def _entry(name):
    return next(t for t in load_spec()["tools"] if t["name"] == name)


# --- schema cache ------------------------------------------------------------


def test_schema_reads_are_cached_per_site_and_path():
    client = _client()
    fake = _routed({"/schema/site": lambda: _ok(SCHEMA_SITE)})
    with patch("requests.Session.request", fake):
        client.call("testbed", _entry("content_schema_site"), {})
        client.call("testbed", _entry("content_schema_site"), {})
    assert fake.call_count == 1


def test_schema_class_reads_are_cached_per_class_ref():
    client = _client()
    fake = _routed({"/schema/Page": lambda: _ok({"data": {"classRef": "Page"}})})
    fake2 = _routed({"/schema/Image": lambda: _ok({"data": {"classRef": "Image"}})})
    with patch("requests.Session.request", fake):
        client.call("testbed", _entry("content_schema_class"), {"classRef": "Page"})
        client.call("testbed", _entry("content_schema_class"), {"classRef": "Page"})
    with patch("requests.Session.request", fake2):
        client.call("testbed", _entry("content_schema_class"), {"classRef": "Image"})
    assert fake.call_count == 1
    assert fake2.call_count == 1


def test_cached_value_is_a_copy_the_caller_cannot_change():
    client = _client()
    fake = _routed({"/schema/site": lambda: _ok(json.loads(json.dumps(SCHEMA_SITE)))})
    with patch("requests.Session.request", fake):
        first = client.get("testbed", "schema/site")
        first["data"]["classes"].clear()
        second = client.get("testbed", "schema/site")
    assert "Page" in second["data"]["classes"]


def test_ttl_zero_disables_the_cache_and_other_paths_are_never_cached(monkeypatch):
    client = _client()
    monkeypatch.setenv("CONTENT_SCHEMA_CACHE_TTL", "0")
    fake = _routed({"/schema/site": lambda: _ok(SCHEMA_SITE)})
    with patch("requests.Session.request", fake):
        client.get("testbed", "schema/site")
        client.get("testbed", "schema/site")
    assert fake.call_count == 2

    monkeypatch.delenv("CONTENT_SCHEMA_CACHE_TTL")
    fake = _routed({"/auth/session": lambda: _ok(SESSION)})
    with patch("requests.Session.request", fake):
        client.get("testbed", "auth/session")
        client.get("testbed", "auth/session")
    assert fake.call_count == 2


def test_cache_expires_after_the_ttl(monkeypatch):
    client = _client()
    clock = [1000.0]
    monkeypatch.setattr("content_api_mcp.core.client.time.monotonic", lambda: clock[0])
    fake = _routed({"/schema/site": lambda: _ok(SCHEMA_SITE)})
    with patch("requests.Session.request", fake):
        client.get("testbed", "schema/site")
        clock[0] += DEFAULT_SCHEMA_CACHE_TTL - 1
        client.get("testbed", "schema/site")
        clock[0] += 2
        client.get("testbed", "schema/site")
    assert fake.call_count == 2


def test_a_failed_schema_read_is_not_cached():
    client = _client()
    responses = iter([_mock_response(500, text="boom"), _ok(SCHEMA_SITE)])
    fake = _routed({"/schema/site": lambda: next(responses)})
    with patch("requests.Session.request", fake):
        with pytest.raises(Exception):
            client.get("testbed", "schema/site")
        assert client.get("testbed", "schema/site")["data"]["environment"] == "dev"


def test_ttl_env_falls_back_on_bad_values(monkeypatch):
    for bad in ("abc", "-1"):
        monkeypatch.setenv("CONTENT_SCHEMA_CACHE_TTL", bad)
        assert schema_cache_ttl() == DEFAULT_SCHEMA_CACHE_TTL
    monkeypatch.setenv("CONTENT_SCHEMA_CACHE_TTL", "30")
    assert schema_cache_ttl() == 30


# --- UNKNOWN_CLASS teaching (daisy-content#21) --------------------------------------------


def test_unknown_class_lists_the_exposed_class_refs():
    client = _client()
    fake = _routed(
        {
            "/records/Nope": lambda: _mock_response(
                404, text=json.dumps(UNKNOWN_CLASS)
            ),
            "/schema/site": lambda: _ok(SCHEMA_SITE),
        }
    )
    with patch("requests.Session.request", fake):
        with pytest.raises(NotFoundError) as exc_info:
            client.call("testbed", _entry("content_records_list"), {"classRef": "Nope"})
    message = str(exc_info.value)
    assert 'Unknown class reference "Nope".' in message
    assert "BlockPage, ElementContent, Image, Page" in message
    assert exc_info.value.details["valid_options"] == [
        "BlockPage",
        "ElementContent",
        "Image",
        "Page",
    ]
    assert exc_info.value.details["error_code"] == "UNKNOWN_CLASS"


@pytest.mark.asyncio
async def test_unknown_class_valid_options_reach_the_trailer():
    tools, _ = capture_spec_tools(client=_client())
    fake = _routed(
        {
            "/schema/Nope": lambda: _mock_response(404, text=json.dumps(UNKNOWN_CLASS)),
            "/schema/site": lambda: _ok(SCHEMA_SITE),
        }
    )
    with patch("requests.Session.request", fake):
        with raises_tool_error("not_found") as err:
            await tools["content_schema_class"].run({"classRef": "Nope"})
    assert err.trailer["error_code"] == "UNKNOWN_CLASS"
    assert "Page" in err.trailer["valid_options"]


def test_unknown_class_keeps_the_original_error_when_the_schema_read_fails():
    client = _client()
    fake = _routed(
        {
            "/records/Nope": lambda: _mock_response(
                404, text=json.dumps(UNKNOWN_CLASS)
            ),
            "/schema/site": lambda: _mock_response(403, text="denied"),
        }
    )
    with patch("requests.Session.request", fake):
        with pytest.raises(NotFoundError) as exc_info:
            client.call("testbed", _entry("content_records_list"), {"classRef": "Nope"})
    assert "valid_options" not in exc_info.value.details
    assert "Class refs exposed" not in str(exc_info.value)


def test_other_404s_are_not_enriched():
    client = _client()
    body = {"error": {"code": "NOT_FOUND", "status": 404, "message": "No record"}}
    fake = _routed(
        {"/records/Page/99": lambda: _mock_response(404, text=json.dumps(body))}
    )
    with patch("requests.Session.request", fake):
        with pytest.raises(NotFoundError) as exc_info:
            client.call(
                "testbed",
                _entry("content_records_read"),
                {"classRef": "Page", "id": "99"},
            )
    assert fake.call_count == 1
    assert "valid_options" not in exc_info.value.details


# --- default site ------------------------------------------------------------


def test_one_configured_site_makes_site_optional_and_dispatch_uses_it():
    client = MagicMock()
    client.sites = {"only": object()}
    client.call.return_value = {"data": {}}
    tools, _ = capture_spec_tools(client=client)
    params = tools["content_auth_session"].parameters
    assert "site" not in params.get("required", [])
    assert params["properties"]["site"]["default"] == "only"
    assert "classRef" in tools["content_schema_class"].parameters["required"]


@pytest.mark.asyncio
async def test_default_site_is_sent_when_the_call_omits_it():
    client = MagicMock()
    client.sites = {"only": object()}
    client.call.return_value = {"data": {}}
    tools, _ = capture_spec_tools(client=client)
    await tools["content_auth_session"].run({})
    assert client.call.call_args.args[0] == "only"


def test_two_configured_sites_keep_site_required():
    client = MagicMock()
    client.sites = {"a": object(), "b": object()}
    tools, _ = capture_spec_tools(client=client)
    assert "site" in tools["content_auth_session"].parameters["required"]


# --- content_get_started -----------------------------------------------------


def _get_started_routes(session=SESSION, schema=SCHEMA_SITE):
    return _routed(
        {"/auth/session": lambda: _ok(session), "/schema/site": lambda: _ok(schema)}
    )


def test_get_started_summarizes_session_schema_and_steps():
    with patch("requests.Session.request", _get_started_routes()):
        result = bootstrap.get_started(_client(), None)
    assert result["site"] == "testbed"
    assert result["session"]["email"] == "svc@example.com"
    assert result["session"]["expiresAt"].startswith("2026-")
    assert result["environment"] == "dev"
    assert result["populationEnabled"] is True
    assert result["classes"] == {
        "pages": ["Page", "BlockPage"],
        "elements": ["ElementContent"],
        "other": ["Image"],
    }
    assert result["warnings"] == []
    assert any("content_schema_class" in step for step in result["nextSteps"])
    assert len(json.dumps(result)) < 4000


def test_get_started_warns_about_closed_gates_and_missing_permissions():
    session = json.loads(json.dumps(SESSION))
    session["data"]["permissions"] = ["CONTENT_API_ACCESS"]
    schema = json.loads(json.dumps(SCHEMA_SITE))
    schema["data"]["populationEnabled"] = False
    schema["data"]["recordUpdateEnabled"] = False
    with patch("requests.Session.request", _get_started_routes(session, schema)):
        warnings = bootstrap.get_started(_client(), "testbed")["warnings"]
    joined = " ".join(warnings)
    assert "populationEnabled is false" in joined
    assert "recordUpdateEnabled is false" in joined
    assert "CONTENT_API_POPULATE" in joined
    assert "VIEW_DRAFT_CONTENT" in joined
    # Both gates closed: the population warning must not claim
    # content_records_update still works.
    population = next(w for w in warnings if "populationEnabled" in w)
    assert "content_records_update" not in population
    assert "content_records_stage still works" in population


def test_get_started_population_warning_names_records_update_when_open():
    schema = json.loads(json.dumps(SCHEMA_SITE))
    schema["data"]["populationEnabled"] = False
    with patch("requests.Session.request", _get_started_routes(SESSION, schema)):
        warnings = bootstrap.get_started(_client(), "testbed")["warnings"]
    assert warnings == [
        "populationEnabled is false: content_batch, content_compose_page, page "
        "actions and asset writes return 403 ENV_FORBIDDEN here. "
        "content_records_update and content_records_stage still work."
    ]


def test_get_started_warns_when_the_token_lacks_access():
    session = json.loads(json.dumps(SESSION))
    session["data"]["permissions"] = ["CONTENT_API_SCHEMA"]
    with patch("requests.Session.request", _get_started_routes(session)):
        warnings = bootstrap.get_started(_client(), "testbed")["warnings"]
    assert any("CONTENT_API_ACCESS" in w for w in warnings)


def test_next_steps_publish_with_owns_and_require_external_ids():
    joined = " ".join(bootstrap.NEXT_STEPS)
    assert 'mode "owns"' in joined
    assert "Every element needs an externalId" in joined


def test_get_started_needs_a_site_when_several_are_configured():
    client = _client(
        other=SiteConfig(base_url="https://b.example/content-api/v1", token="t")
    )
    with pytest.raises(ValidationError, match="Configured sites: other, testbed"):
        bootstrap.get_started(client, None)


@pytest.mark.asyncio
async def test_get_started_tool_is_annotated_and_errors_carry_the_trailer():
    tools, client = capture_meta_tools()
    tool = tools["content_get_started"]
    assert tool._mcp_tool_kwargs["annotations"]["readOnlyHint"] is True

    client.default_site.return_value = None
    client.list_sites.return_value = ["a", "b"]
    with raises_tool_error("validation_error", match="Configured sites: a, b"):
        await tool()


@pytest.mark.asyncio
async def test_get_started_tool_returns_the_summary_through_fastmcp():
    from fastmcp import Client, FastMCP

    from content_api_mcp.core.meta import register_meta_tools

    real = _client()
    mcp = FastMCP("test-get-started")
    register_meta_tools(mcp, real)
    with patch("requests.Session.request", _get_started_routes()):
        async with Client(mcp) as mcp_client:
            result = await mcp_client.call_tool("content_get_started", {})
    assert result.structured_content["site"] == "testbed"
    assert result.structured_content["classes"]["elements"] == ["ElementContent"]


def test_every_meta_tool_is_annotated():
    tools, _ = capture_meta_tools()
    for name, fn in tools.items():
        annotations = fn._mcp_tool_kwargs.get("annotations") or {}
        assert "readOnlyHint" in annotations, name
        assert "idempotentHint" in annotations, name


def _unknown_class_with_sub(code, message):
    return {
        "data": None,
        "meta": {},
        "error": {
            "code": "UNKNOWN_CLASS",
            "status": 404,
            "message": message,
            "details": [{"field": None, "code": code, "message": "x"}],
        },
    }


def _call_records_list_unknown(body):
    client = _client()
    fake = _routed(
        {
            "/records/Nope": lambda: _mock_response(404, text=json.dumps(body)),
            "/schema/site": lambda: _ok(SCHEMA_SITE),
        }
    )
    with patch("requests.Session.request", fake):
        with pytest.raises(NotFoundError) as exc_info:
            client.call("testbed", _entry("content_records_list"), {"classRef": "Nope"})
    return exc_info.value, fake


def test_unknown_class_not_found_subcode_is_left_alone():
    # The ref is registered but its PHP class is missing: a site problem.
    body = _unknown_class_with_sub(
        "CLASS_NOT_FOUND",
        'Class reference "Nope" is registered to "App\\Nope", but that class does not exist.',
    )
    error, fake = _call_records_list_unknown(body)
    assert "valid_options" not in error.details
    assert fake.call_count == 1


def test_unknown_class_with_upstream_hint_keeps_it_and_adds_the_list():
    body = _unknown_class_with_sub(
        "CLASS_NOT_MAPPED",
        'Unknown class reference "Nope". A class named "Nope" exists but has no '
        '"models:" entry.',
    )
    error, _ = _call_records_list_unknown(body)
    assert error.details["valid_options"]
    assert "not a fully-qualified PHP class name" not in str(error)
    assert '"models:" entry' in str(error)


def test_valid_options_are_capped_in_the_trailer():
    from content_api_mcp.core.errors import MAX_TRAILER_DETAILS_LEN, error_payload

    refs = [f"VeryLongElementClassReference{i:04d}" for i in range(400)]
    payload = error_payload(
        NotFoundError(
            "x", details={"error_code": "UNKNOWN_CLASS", "valid_options": refs}
        )
    )
    assert payload["valid_options_truncated"] is True
    assert len(json.dumps(payload["valid_options"])) <= MAX_TRAILER_DETAILS_LEN
    assert payload["valid_options"] == refs[: len(payload["valid_options"])]
