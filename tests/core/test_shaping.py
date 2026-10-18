"""Response-size control (daisy-content#20): content_api_mcp.core.shaping and
its wiring in SpecTool.

Payload shapes are taken from live responses of the daisy-content#10 testbed (module
2.9.0): records_list `{data: [{id, classRef, className, externalId, fields,
relations, stage}], meta: {total, limit, offset, stage}}`, fingerprint
`{data: {pages, related: {ref: {records, unresolved}}, totals, violations},
meta: {skipped}}`, schema/site `{data: {..., crud, classes: {ref: {page,
element, ...}}}}`.
"""

import json
from unittest.mock import MagicMock

import pytest
from conftest import capture_spec_tools, raises_tool_error

from content_api_mcp.core import shaping


def _record(i, class_ref="Page", **fields):
    return {
        "id": i,
        "classRef": class_ref,
        "className": class_ref,
        "externalId": None,
        "fields": {"Title": f"Page {i}", "URLSegment": f"page-{i}", **fields},
        "relations": {"Parent": None, "BackLinks": []},
        "stage": {"draft": True, "live": False, "modifiedOnDraft": False},
    }


def _records_result(n, total=None, offset=0, **fields):
    return {
        "data": [_record(i, **fields) for i in range(1, n + 1)],
        "meta": {"total": total or n, "limit": n, "offset": offset, "stage": "draft"},
        "error": None,
    }


def _fingerprint_result(pages=3, related_rows=2):
    return {
        "data": {
            "pages": [
                {"path": f"/p{i}", "className": "Page", "live": True}
                for i in range(pages)
            ],
            "related": {
                "HeroImage": {
                    "records": [{"ownerPath": f"/p{i}"} for i in range(related_rows)],
                    "unresolved": 0,
                }
            },
            "totals": {"pages": {"draft": pages, "live": pages}},
            "violations": [{"path": "/p0", "blockedBy": "/"}],
        },
        "meta": {"skipped": []},
        "error": None,
    }


def _schema_site_result():
    return {
        "data": {
            "environment": "dev",
            "populationEnabled": True,
            "crud": {"models": ["Page"]},
            "integrations": {"elemental": True},
            "classes": {
                "Page": {"page": True, "element": False, "access": ["read"]},
                "ElementContent": {"page": False, "element": True},
                "Image": {"page": False, "element": False},
            },
        },
        "meta": {},
        "error": None,
    }


# --- schema injection -------------------------------------------------------


def test_only_the_three_tools_get_response_format():
    tools, _ = capture_spec_tools()
    with_format = {
        name
        for name, tool in tools.items()
        if "response_format" in tool.parameters.get("properties", {})
    }
    assert with_format == {
        "content_records_list",
        "content_fingerprint",
        "content_schema_site",
    }
    assert "fields" in tools["content_records_list"].parameters["properties"]
    limit = tools["content_records_list"].parameters["properties"]["limit"]
    assert limit["default"] == shaping.DEFAULT_RECORDS_LIMIT
    assert limit["maximum"] == 500


def test_injection_does_not_mutate_the_spec_entry():
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    shaping.inject_shaping("content_records_list", schema)
    assert schema == {"type": "object", "properties": {"limit": {"type": "integer"}}}


@pytest.mark.asyncio
async def test_bogus_response_format_is_rejected_before_any_call():
    client = MagicMock()
    tools, _ = capture_spec_tools(client=client)
    with raises_tool_error("validation_error", match="response_format"):
        await tools["content_records_list"].run(
            {"site": "testbed", "classRef": "Page", "response_format": "tiny"}
        )
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_fields_with_detailed_is_rejected_before_any_call():
    client = MagicMock()
    tools, _ = capture_spec_tools(client=client)
    with raises_tool_error("validation_error", match="fields"):
        await tools["content_records_list"].run(
            {
                "site": "testbed",
                "classRef": "Page",
                "response_format": "detailed",
                "fields": ["URLSegment"],
            }
        )
    client.call.assert_not_called()


# --- records_list -----------------------------------------------------------


@pytest.mark.asyncio
async def test_records_list_defaults_to_concise_and_limit_25_upstream():
    client = MagicMock()
    client.call.return_value = _records_result(2, total=2)
    tools, _ = capture_spec_tools(client=client)

    result = await tools["content_records_list"].run(
        {"site": "testbed", "classRef": "Page"}
    )

    sent = client.call.call_args.args[2]
    assert sent == {"classRef": "Page", "limit": 25}
    data = result.structured_content["data"]
    assert data[0] == {
        "id": 1,
        "classRef": "Page",
        "externalId": None,
        "title": "Page 1",
        "stage": {"draft": True, "live": False, "modifiedOnDraft": False},
    }
    assert "nextOffset" not in result.structured_content["meta"]


@pytest.mark.asyncio
async def test_records_list_explicit_limit_wins_and_extra_args_never_reach_upstream():
    client = MagicMock()
    client.call.return_value = _records_result(1)
    tools, _ = capture_spec_tools(client=client)

    await tools["content_records_list"].run(
        {
            "site": "testbed",
            "classRef": "Page",
            "limit": 5,
            "response_format": "concise",
            "fields": ["URLSegment"],
        }
    )
    assert client.call.call_args.args[2] == {"classRef": "Page", "limit": 5}


@pytest.mark.asyncio
async def test_records_list_concise_includes_requested_fields():
    client = MagicMock()
    client.call.return_value = _records_result(1)
    tools, _ = capture_spec_tools(client=client)

    result = await tools["content_records_list"].run(
        {
            "site": "testbed",
            "classRef": "Page",
            "fields": ["URLSegment", "Parent", "Missing"],
        }
    )
    record = result.structured_content["data"][0]
    # Parent is a has_one: upstream puts it under relations, not fields.
    assert record["fields"] == {"URLSegment": "page-1", "Parent": None}
    assert result.structured_content["meta"]["unknownFields"] == ["Missing"]


@pytest.mark.asyncio
async def test_records_list_detailed_returns_upstream_records_unchanged():
    upstream = _records_result(2)
    client = MagicMock()
    client.call.return_value = upstream
    tools, _ = capture_spec_tools(client=client)

    result = await tools["content_records_list"].run(
        {"site": "testbed", "classRef": "Page", "response_format": "detailed"}
    )
    assert result.structured_content["data"] == upstream["data"]


def test_records_list_title_falls_back_to_name_then_none():
    image = {"id": 9, "classRef": "Image", "fields": {"Name": "a.jpg"}}
    bare = {"id": 10, "classRef": "Thing", "fields": {}}
    assert shaping._concise_record(image, [])["title"] == "a.jpg"
    assert shaping._concise_record(bare, [])["title"] is None


def test_records_list_next_offset_when_more_remain():
    result = shaping.shape_response(
        "content_records_list",
        _records_result(5, total=12, offset=5),
        {"response_format": "concise", "fields": [], "offset": 5},
    )
    assert result["meta"]["nextOffset"] == 10


def test_records_list_over_cap_drops_rows_and_says_how_to_narrow(monkeypatch):
    monkeypatch.setenv("CONTENT_MAX_RESPONSE_CHARS", "3000")
    big = _records_result(20, total=40, Content="x" * 400)
    result = shaping.shape_response(
        "content_records_list",
        big,
        {"response_format": "detailed", "fields": [], "offset": 0},
    )
    assert len(json.dumps(result)) <= 3000
    kept = len(result["data"])
    assert 0 < kept < 20
    assert result["data"] == big["data"][:kept]
    truncated = result["meta"]["truncated"]
    assert truncated["returned"] == kept
    assert truncated["dropped"] == 20 - kept
    assert f"offset={kept}" in truncated["narrowWith"]
    assert result["meta"]["nextOffset"] == kept


def test_cap_env_falls_back_to_default_on_bad_values(monkeypatch):
    for bad in ("abc", "0", "999", "-5"):
        monkeypatch.setenv("CONTENT_MAX_RESPONSE_CHARS", bad)
        assert shaping.max_response_chars() == shaping.DEFAULT_MAX_RESPONSE_CHARS
    monkeypatch.setenv("CONTENT_MAX_RESPONSE_CHARS", "5000")
    assert shaping.max_response_chars() == 5000


# --- fingerprint ------------------------------------------------------------


def test_fingerprint_detailed_under_cap_is_unchanged():
    upstream = _fingerprint_result()
    assert (
        shaping.shape_response(
            "content_fingerprint", upstream, {"response_format": "detailed"}
        )
        is upstream
    )


def test_fingerprint_concise_counts_rows_and_keeps_totals_and_violations():
    upstream = _fingerprint_result(pages=4, related_rows=3)
    result = shaping.shape_response(
        "content_fingerprint", upstream, {"response_format": "concise"}
    )
    assert result["data"] == {
        "sections": {"pages": {"rows": 4}, "HeroImage": {"rows": 3, "unresolved": 0}},
        "totals": upstream["data"]["totals"],
        "violations": upstream["data"]["violations"],
    }


def test_fingerprint_over_cap_drops_rows_but_never_violations(monkeypatch):
    monkeypatch.setenv("CONTENT_MAX_RESPONSE_CHARS", "2000")
    upstream = _fingerprint_result(pages=200, related_rows=200)
    result = shaping.shape_response(
        "content_fingerprint", upstream, {"response_format": "detailed"}
    )
    assert len(json.dumps(result)) <= 2000
    assert result["data"]["violations"] == upstream["data"]["violations"]
    assert result["data"]["totals"] == upstream["data"]["totals"]
    sections = result["meta"]["truncated"]["sections"]
    assert sections
    for name, counts in sections.items():
        assert counts["returned"] + counts["dropped"] == 200
    assert "classes" in result["meta"]["truncated"]["narrowWith"]
    assert result["meta"]["skipped"] == []
    # The upstream dict itself is left alone.
    assert len(upstream["data"]["pages"]) == 200


def test_fingerprint_related_empty_list_shape_is_tolerated():
    # PHP encodes an empty $related array as [], not {}.
    upstream = _fingerprint_result()
    upstream["data"]["related"] = []
    result = shaping.shape_response(
        "content_fingerprint", upstream, {"response_format": "concise"}
    )
    assert result["data"]["sections"] == {"pages": {"rows": 3}}


# --- schema_site ------------------------------------------------------------


def test_schema_site_concise_groups_classes_and_drops_crud():
    result = shaping.shape_response(
        "content_schema_site", _schema_site_result(), {"response_format": "concise"}
    )
    assert result["data"]["classes"] == {
        "pages": ["Page"],
        "elements": ["ElementContent"],
        "other": ["Image"],
    }
    assert "crud" not in result["data"]
    assert result["data"]["populationEnabled"] is True


def test_schema_site_detailed_over_cap_falls_back_to_concise(monkeypatch):
    monkeypatch.setenv("CONTENT_MAX_RESPONSE_CHARS", "1000")
    upstream = _schema_site_result()
    for i in range(60):
        upstream["data"]["classes"][f"Element{i}"] = {
            "page": False,
            "element": True,
            "access": ["read", "create", "update", "action"],
            "className": f"Vendor\\Module\\Elements\\Element{i}",
        }
    result = shaping.shape_response(
        "content_schema_site", upstream, {"response_format": "detailed"}
    )
    assert result["meta"]["truncated"]["replacedWith"] == "concise"
    assert "Element59" in result["data"]["classes"]["elements"]


def test_unshaped_tools_pass_through():
    payload = {"data": {"anything": [1] * 10_000}}
    assert shaping.shape_response("content_schema_class", payload, {}) is payload
    args = {"classRef": "Page"}
    assert shaping.split_shaping_args("content_schema_class", args) == (args, {})


def test_a_single_row_over_the_cap_still_advances_the_page(monkeypatch):
    monkeypatch.setenv("CONTENT_MAX_RESPONSE_CHARS", "1000")
    big = _records_result(3, total=30, offset=20, Content="x" * 5000)
    result = shaping.shape_response(
        "content_records_list",
        big,
        {"response_format": "detailed", "fields": [], "offset": 20},
    )
    assert result["data"] == [
        {
            "id": 1,
            "classRef": "Page",
            "externalId": None,
            "title": "Page 1",
            "stage": {"draft": True, "live": False, "modifiedOnDraft": False},
        }
    ]
    assert result["meta"]["nextOffset"] == 21
    assert "content_records_read" in result["meta"]["truncated"]["narrowWith"]


def test_fingerprint_with_many_truncated_sections_stays_under_the_cap(monkeypatch):
    monkeypatch.setenv("CONTENT_MAX_RESPONSE_CHARS", "5000")
    upstream = _fingerprint_result(pages=50, related_rows=0)
    upstream["data"]["related"] = {
        f"RelatedSection{i:02d}": {
            "records": [{"ownerPath": f"/p{j}"} for j in range(40)],
            "unresolved": 0,
        }
        for i in range(20)
    }
    result = shaping.shape_response(
        "content_fingerprint", upstream, {"response_format": "detailed"}
    )
    assert len(json.dumps(result)) <= 5000
    assert result["data"]["violations"] == upstream["data"]["violations"]


def test_fingerprint_whose_violations_alone_exceed_the_cap_says_so(monkeypatch):
    monkeypatch.setenv("CONTENT_MAX_RESPONSE_CHARS", "1000")
    upstream = _fingerprint_result(pages=5)
    upstream["data"]["violations"] = [
        {"path": f"/very/long/path/{i}", "blockedBy": "/very"} for i in range(80)
    ]
    result = shaping.shape_response(
        "content_fingerprint", upstream, {"response_format": "detailed"}
    )
    assert result["data"]["violations"] == upstream["data"]["violations"]
    assert result["data"]["pages"] == []
    assert "never dropped" in result["meta"]["truncated"]["narrowWith"]
