"""Server-layer tests: tool registration and wiring to ContentClient.

Two tool families here, unlike daisy-media's single hand-written set:
- content_list_sites — the one hand-written meta tool (conftest.capture_meta_tools)
- every content_* spec-generated tool — built by content_api_mcp.core.tools
  from schema/endpoints.json (conftest.capture_spec_tools / capture_all_tools)

No `_headers` param on anything here (unlike the OAuth children) — this
server holds its site credentials itself, via the CONTENT_SITES registry.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from conftest import (
    capture_meta_tools,
    capture_spec_tools,
    raises_tool_error,
)

from content_api_mcp.core.tools import load_spec


def _spec_tool_names():
    return {entry["name"] for entry in load_spec()["tools"]}


# Hand-written tools registered by core.meta.register_meta_tools.
_META_TOOL_NAMES = {"content_list_sites", "content_get_started", "content_get_workflow"}


# ---------------------------------------------------------------------------
# create_app() wiring — every tool actually gets registered.
# ---------------------------------------------------------------------------


def test_list_sites_delegates_to_client():
    tools, client = capture_meta_tools()
    client.list_sites.return_value = ["acme", "beta"]

    result = tools["content_list_sites"]()

    client.list_sites.assert_called_once_with()
    assert result == {"sites": ["acme", "beta"]}


def test_list_sites_reports_empty_list_when_unconfigured():
    tools, client = capture_meta_tools()
    client.list_sites.return_value = []

    result = tools["content_list_sites"]()

    assert result == {"sites": []}


# ---------------------------------------------------------------------------
# Spec-generated tools — schema shape (the `site` injection) and dispatch.
# ---------------------------------------------------------------------------


def test_every_spec_tool_has_required_site_property():
    tools, _client = capture_spec_tools()
    for name, tool in tools.items():
        props = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])
        assert "site" in props, f"{name} is missing the injected site property"
        assert "site" in required, f"{name} does not require site"


def test_spec_tool_injection_does_not_mutate_shared_spec_dict():
    # _inject_site() must copy, not mutate, the entry's inputSchema — two
    # tools built from the same load_spec() result must not share state.
    spec = load_spec()
    tools, _client = capture_spec_tools(spec=spec)
    original_entry = next(
        e for e in spec["tools"] if e["name"] == "content_auth_session"
    )
    assert "site" not in original_entry["inputSchema"].get("properties", {})
    assert "site" in tools["content_auth_session"].parameters["properties"]


def test_build_tools_raises_runtime_error_for_an_invalid_schema_entry():
    # A drifted/typo'd schema/endpoints.json (e.g. a bad manual re-sync)
    # must fail loudly at boot with the same friendly framing every other
    # malformed-spec case here uses — not an unframed
    # jsonschema.exceptions.SchemaError surfacing later, on the first real
    # tool call. Uses a name NOT in _TOOL_ANNOTATIONS, on purpose, to
    # exercise the actual ordering build_tools() documents (generate_tools.py's
    # check_schema() call runs BEFORE the _TOOL_ANNOTATIONS lookup): if that
    # ordering ever flipped, this entry would instead fail with "Missing
    # tool annotation" (the KeyError branch's distinct message, see that
    # branch's own comment), not "is not a valid JSON Schema" — the match=
    # below would catch that regression. A name that IS in the table would
    # pass either way and leave the ordering itself unverified.
    from unittest.mock import MagicMock as _MagicMock

    from content_api_mcp.core.tools import build_tools

    broken_spec = {
        "tools": [
            {
                "name": "content_not_in_annotation_table",
                "description": "broken",
                "path": "broken",
                "method": "GET",
                "inputSchema": {"type": "not-a-real-type", "properties": {}},
            }
        ]
    }
    with pytest.raises(RuntimeError, match="is not a valid JSON Schema"):
        build_tools(broken_spec, _MagicMock())


def test_no_spec_tool_has_a_headers_param():
    # Defining property of a shared-credential child (see daisy-scrapey/CLAUDE.md
    # and daisy-media/CLAUDE.md's "Auth shape" section): no _headers, unlike
    # the OAuth children.
    tools, _client = capture_spec_tools()
    for name, tool in tools.items():
        assert "_headers" not in tool.parameters.get("properties", {}), name


# ---------------------------------------------------------------------------
# Tool annotations (readOnlyHint/destructiveHint/idempotentHint) —
# daisy-content#19. A host UI needs these to decide "auto-approve reads,
# prompt on writes" without hardcoding a tool list.
# ---------------------------------------------------------------------------

# The full expected annotation set for all 15 spec tools, mirroring
# generate_tools._TOOL_ANNOTATIONS exactly. A field omitted from an entry
# here must stay unset (None) on the built tool — asserted below — so a
# read-only tool can never also carry a stray destructiveHint=True. Pinning
# every tool (not just the "interesting" ones) matters: a narrower test
# checking only a handful of tools stays green if some OTHER tool's
# classification silently drifts wrong (verified — flipping any single
# tool's readOnlyHint in _TOOL_ANNOTATIONS makes this test fail).
_EXPECTED_SPEC_TOOL_ANNOTATIONS = {
    "content_auth_session": {"readOnlyHint": True, "idempotentHint": True},
    "content_schema_site": {"readOnlyHint": True, "idempotentHint": True},
    "content_schema_class": {"readOnlyHint": True, "idempotentHint": True},
    "content_records_list": {"readOnlyHint": True, "idempotentHint": True},
    "content_records_read": {"readOnlyHint": True, "idempotentHint": True},
    "content_records_parity": {"readOnlyHint": True, "idempotentHint": True},
    "content_fingerprint": {"readOnlyHint": True, "idempotentHint": True},
    "content_asset_read": {"readOnlyHint": True, "idempotentHint": True},
    # Not idempotent — `conflict: "rename"` never hash-skips even on a
    # byte-identical repeat call (see the explanatory comment on this
    # entry in generate_tools.py).
    "content_asset_upload": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
    },
    # Not idempotent — an `assets[]` entry with `conflict: "rename"` routes
    # through the identical AssetService.ingest() content_asset_upload
    # above does, and never hash-skips either (see the explanatory comment
    # on this entry in generate_tools.py).
    "content_compose_page": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
    "content_page_convert": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
    # Not idempotent — appends the template's elements again on every call
    # (see the explanatory comment on this entry in generate_tools.py).
    "content_page_apply_template": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
    # Field PATCH on one record (daisy-content#51): overwrites fields; can archive an
    # owned Link it stops pointing at, at publish. A same-type link payload
    # updates in place, so a repeat converges (see the explanatory comment
    # on this entry in generate_tools.py).
    "content_records_update": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
    "content_records_stage": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
    },
    "content_batch": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
    },
}

# openWorldHint is included even though no table entry above sets it (MCP
# defaults it *true* at the protocol level — the field itself stays
# `None` unless explicitly set, which is what `want`/`got` below actually
# compare — since all 15 spec tools call out to an arbitrary configured
# site) — without it in this tuple, a future stray `openWorldHint=False`
# on some entry would pass every assertion below. content_list_sites (the
# 16th, hand-written tool) is deliberately NOT covered by this table or
# this tuple's "defaults true" reasoning — it's checked on its own terms
# by test_content_list_sites_has_read_only_annotations below, since it
# reads no remote site at all.
_ANNOTATION_FIELDS = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)


def test_every_spec_tool_annotation_matches_the_expected_table():
    tools, _client = capture_spec_tools()
    assert set(tools.keys()) == set(_EXPECTED_SPEC_TOOL_ANNOTATIONS.keys())
    for name, expected in _EXPECTED_SPEC_TOOL_ANNOTATIONS.items():
        actual = tools[name].annotations
        assert actual is not None, f"{name} has no annotations"
        for field in _ANNOTATION_FIELDS:
            want = expected.get(field)
            got = getattr(actual, field)
            assert got == want, f"{name}.{field}: expected {want!r}, got {got!r}"


def test_tool_annotations_table_has_no_orphaned_entries():
    # test_every_spec_tool_annotation_matches_the_expected_table above
    # already catches a spec tool with no row (build_tools() raises before
    # that test's own capture_spec_tools() call even returns — see
    # test_build_tools_raises_runtime_error_for_a_spec_entry_missing_from_
    # annotation_table below) AND a tool removed from the spec (that test's
    # set-equality against *built* tools fails either direction). What
    # nothing catches is generate_tools._TOOL_ANNOTATIONS itself drifting
    # from the spec while _EXPECTED_SPEC_TOOL_ANNOTATIONS (a second,
    # independently hand-maintained dict in this file) drifts the same way
    # in lockstep — comparing the two tables against EACH OTHER, as an
    # earlier version of this test did, can't detect that, since it never
    # reads the spec at all. Comparing directly against load_spec() closes
    # that: an orphan row in _TOOL_ANNOTATIONS for a tool the pinned spec
    # no longer has fails here even if _EXPECTED_SPEC_TOOL_ANNOTATIONS was
    # (incorrectly) updated to match it.
    from content_api_mcp.core.tools import _TOOL_ANNOTATIONS

    assert set(_TOOL_ANNOTATIONS.keys()) == _spec_tool_names()


def test_content_list_sites_has_read_only_annotations():
    # The one hand-written meta tool — annotated directly in server.py's
    # @mcp.tool() decorator, not via generate_tools._TOOL_ANNOTATIONS
    # (which only covers spec-generated tools). openWorldHint=False here
    # (unlike every spec tool above) since this one reads only the local
    # CONTENT_SITES registry and never calls out to a configured site.
    tools, _client = capture_meta_tools()
    annotations = tools["content_list_sites"]._mcp_tool_kwargs.get("annotations")
    assert annotations == {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def test_build_tools_raises_runtime_error_for_a_spec_entry_missing_from_annotation_table():
    # daisy-content#19's fail-closed guard: a spec entry with no row in
    # generate_tools._TOOL_ANNOTATIONS must fail loudly at boot, not build
    # an unannotated (therefore host-treated-as-unsafe-unknown) tool.
    # match= checks the "Missing tool annotation" prefix, not just the tool
    # name — the name alone is interpolated into BOTH this branch's message
    # and the check_schema() branch's, so a name-only match would pass
    # whichever branch actually fired, leaving this test unable to tell
    # them apart (unmapped_entry here has a valid schema, so it can only
    # currently reach this branch, but the assertion should say so).
    from content_api_mcp.core.tools import build_tools

    spec = load_spec()
    entry = next(e for e in spec["tools"] if e["name"] == "content_auth_session")
    unmapped_entry = dict(entry, name="content_not_in_annotation_table")
    broken_spec = {"tools": [unmapped_entry]}

    with pytest.raises(
        RuntimeError,
        match="Missing tool annotation: entry 'content_not_in_annotation_table'",
    ):
        build_tools(broken_spec, MagicMock())


@pytest.mark.asyncio
async def test_spec_tool_run_pops_site_and_calls_client_call():
    client = MagicMock()
    client.call.return_value = {"member": {"id": 1}}
    tools, _client = capture_spec_tools(client=client)

    await tools["content_auth_session"].run({"site": "testbed"})

    client.call.assert_called_once()
    call_args = client.call.call_args.args
    assert call_args[0] == "testbed"
    assert call_args[1]["name"] == "content_auth_session"
    assert call_args[2] == {}  # site popped before reaching client.call


@pytest.mark.asyncio
async def test_spec_tool_run_passes_remaining_arguments_through():
    client = MagicMock()
    client.call.return_value = {"id": 5, "ClassName": "Page"}
    tools, _client = capture_spec_tools(client=client)

    await tools["content_records_read"].run(
        {"site": "testbed", "classRef": "Page", "id": "5"}
    )

    site, entry, remaining = client.call.call_args.args
    assert site == "testbed"
    assert entry["name"] == "content_records_read"
    assert remaining == {"classRef": "Page", "id": "5"}


@pytest.mark.asyncio
async def test_spec_tool_run_raises_mcperror_when_site_missing():

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="site"):
        await tools["content_auth_session"].run({})
    client.call.assert_not_called()


# ---------------------------------------------------------------------------
# daisy-content#9 — SpecTool.run must validate `arguments` against its own
# `parameters` schema before dispatch. FastMCP 3.0.2's Tool._run calls
# self.run(arguments) with no validation of its own for a Tool subclass with
# an overridden run() (only FunctionTool gets that, via a pydantic model
# derived from a function signature) — confirmed by reading
# fastmcp/tools/tool.py's Tool._run/Tool.run in the pinned 3.0.2 install.
# Every case here must reject *before* client.call is ever invoked: a
# malformed call must fail loudly at this boundary, never reach the network.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_missing_required_field_content_asset_upload():

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="filename"):
        await tools["content_asset_upload"].run({"site": "testbed"})
    client.call.assert_not_called()


# ---------------------------------------------------------------------------
# daisy-content#17 — filePath is stripped from the advertised gateway
# schema (generate_tools._strip_file_path_for_gateway) and, if a caller
# supplies it anyway, rejected by SpecTool.run before ContentClient.call —
# and therefore before ContentClient._resolve_file_path, the method that
# would actually touch the local filesystem — is ever reached. Every test
# below asserts client.call.assert_not_called(), the same "never reach
# dispatch" proof the missing-required-field tests above use, since that's
# the actual security property: not just that a nicer error is raised, but
# that no path in this deployment can trigger a local file read.
# ---------------------------------------------------------------------------


def test_file_path_is_stripped_from_gateway_schema(monkeypatch):
    # Review finding (daisy-content#39): without this delenv, a developer running the
    # suite with CONTENT_STDIO set in their own shell — the exact env var
    # CLAUDE.md's own documented local-dev command exports — gets a false
    # green here: build_tools() would see stdio_mode=True and never strip
    # filePath, so this assertion would pass by accident, not because the
    # gateway-mode behavior it's meant to pin is actually correct.
    tools, _client = capture_spec_tools()
    props = tools["content_asset_upload"].parameters.get("properties", {})
    assert "filePath" not in props


def test_file_path_stripping_does_not_affect_other_tools(monkeypatch):
    # A tool whose spec entry never declared filePath must be untouched —
    # _strip_file_path_for_gateway is a documented no-op for those, and
    # this proves it rather than trusting the docstring.
    spec = load_spec()
    entry = next(e for e in spec["tools"] if e["name"] == "content_records_read")
    original_props = set(entry["inputSchema"].get("properties", {}))
    assert "filePath" not in original_props
    tools, _client = capture_spec_tools(spec=spec)
    # The entry never had filePath to begin with; confirm the built tool's
    # property set is exactly the original set plus the injected "site" —
    # not shrunk, not otherwise touched by a strip pass that had nothing to
    # do here.
    built_props = set(tools["content_records_read"].parameters.get("properties", {}))
    assert built_props == original_props | {"site"}


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_file_path_with_a_message_pointing_at_base64(
    monkeypatch,
):

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="base64") as exc_info:
        await tools["content_asset_upload"].run(
            {"site": "testbed", "filename": "x.png", "filePath": "/etc/passwd"}
        )
    # Not just "some ValidationError" — the specific, filePath-aware
    # message this check exists to produce, not the generic
    # jsonschema "Additional properties are not allowed" one a caller
    # would otherwise get (and would have gotten pre-#17, since filePath
    # was still declared then).
    assert "not available in this deployment" in str(exc_info.value)
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_file_path_before_touching_the_filesystem(
    monkeypatch,
):
    # The actual security property, proven directly rather than inferred
    # from the message check above: even a filePath that DOES point at a
    # real, readable file on this machine must never reach
    # ContentClient._resolve_file_path, which is the method that would
    # open and base64-encode it. tmp_path (pytest's built-in fixture) is
    # deliberately not used here — a bare string is enough, since the
    # point is that _resolve_file_path is never called at all, so whether
    # the path resolves to something real is irrelevant.

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error"):
        await tools["content_asset_upload"].run(
            {"site": "testbed", "filename": "x.png", "filePath": __file__}
        )
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_stray_file_path_on_a_tool_that_never_had_it(
    monkeypatch,
):
    # Review finding (daisy-content#39, daisy-content#7): content_records_read
    # never declared filePath
    # at all, so a stray one in arguments must fall through to jsonschema's
    # ordinary "Additional properties are not allowed" rejection, not the
    # daisy-content#17 fix's base64-pointing message — that message only makes sense for
    # a tool (content_asset_upload) that actually had filePath stripped.
    # Pinned via _had_file_path, set in build_tools() from the RAW
    # pre-strip schema; mutating that default back to True for every tool
    # (its old, unscoped behavior) makes this test fail while every other
    # filePath test above still passes, since they all target
    # content_asset_upload, which is unaffected either way.

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error") as exc_info:
        await tools["content_records_read"].run(
            {
                "site": "testbed",
                "classRef": "Page",
                "id": "1",
                "filePath": "/etc/passwd",
            }
        )
    assert "not available in this deployment" not in str(exc_info.value)
    client.call.assert_not_called()


def test_file_path_stays_in_stdio_schema(monkeypatch):
    # Regression guard for the CONTENT_STDIO escape hatch itself: stripping
    # must never fire there, or local testing loses the convenience this
    # repo ported from the stdio proxy specifically to keep working.
    tools, _client = capture_spec_tools(allow_file_path=True)
    props = tools["content_asset_upload"].parameters.get("properties", {})
    assert "filePath" in props


@pytest.mark.asyncio
async def test_spec_tool_run_accepts_file_path_in_stdio_mode(monkeypatch, tmp_path):
    # Companion to test_asset_upload_resolves_file_path_through_call in
    # test_spec_contract.py, which exercises ContentClient.call() directly.
    # This one proves the same call succeeds through the FULL SpecTool.run
    # path (schema validation included) when CONTENT_STDIO is set — i.e.
    # that daisy-content#17's gateway-only stripping genuinely doesn't regress the
    # stdio case, not just that the client-layer method still works in
    # isolation.
    p = tmp_path / "asset.bin"
    p.write_bytes(b"stdio-mode-bytes")

    client = MagicMock()
    client.call.return_value = {"id": 1}
    tools, _client = capture_spec_tools(client=client, allow_file_path=True)

    # Not asserting on the return value itself: SpecTool.run wraps it via
    # self.convert_result (fastmcp's own ToolResult), which every other
    # SpecTool.run test in this file also leaves unchecked in favor of
    # asserting on client.call — the return path isn't what daisy-content#17 touches,
    # and completing without raising is itself part of the proof this
    # test exists for.
    await tools["content_asset_upload"].run(
        {"site": "testbed", "filename": "asset.bin", "filePath": str(p)}
    )

    client.call.assert_called_once()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_missing_required_field_content_batch():

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="operations"):
        await tools["content_batch"].run({"site": "testbed"})
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_missing_required_field_content_compose_page():

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="page"):
        await tools["content_compose_page"].run({"site": "testbed"})
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_missing_required_field_content_page_convert():

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="className"):
        await tools["content_page_convert"].run({"site": "testbed", "id": 5})
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_missing_required_field_content_page_apply_template():

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="templateId"):
        await tools["content_page_apply_template"].run({"site": "testbed", "id": 5})
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_undeclared_property():

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="bogus"):
        await tools["content_records_read"].run(
            {"site": "testbed", "classRef": "Page", "id": "5", "bogus": "x"}
        )
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_truncates_a_long_validation_message():
    # jsonschema's own `.message` embeds a repr() of the offending value for
    # validators like `type` — an oversized content_batch/content_compose_page
    # payload failing validation must not dump that whole value into the
    # caller-facing message. Same cap/convention as
    # content_api_mcp.core.client.MAX_ERROR_BODY_LEN/_truncate_body.

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error") as exc_info:
        await tools["content_batch"].run(
            {
                "site": "testbed",
                "operations": [{"op": "create", "class": "Page"}],
                "defaultPublish": "x" * 1000,  # not a valid enum value
            }
        )
    message = str(exc_info.value)
    assert len(message) <= 320
    assert message.endswith("…(truncated)")
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_limit_over_maximum():

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="500"):
        await tools["content_records_list"].run(
            {"site": "testbed", "classRef": "Page", "limit": 501}
        )
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_invalid_top_level_enum_value():
    # content_records_stage's `action` enum: publish/unpublish/archive.

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="action"):
        await tools["content_records_stage"].run(
            {
                "site": "testbed",
                "classRef": "Page",
                "id": "5",
                "action": "not-a-real-action",
            }
        )
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_invalid_nested_enum_value():
    # content_batch.operations[].op enum: create/upsert/update/delete — nested
    # inside an array item, not a top-level property, so this exercises that
    # the compiled validator actually recurses into "items", not just the
    # entry's own top-level properties.

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="not-a-real-op") as exc_info:
        await tools["content_batch"].run(
            {
                "site": "testbed",
                "operations": [{"op": "not-a-real-op", "class": "Page"}],
            }
        )
    assert "operations" in str(exc_info.value)
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_reaches_client_call_with_fully_valid_arguments():
    # The positive path: a call satisfying every constraint (required
    # fields, additionalProperties:false, enum, maximum) is not blocked by
    # validation and reaches client.call with the site popped, exactly as
    # the pre-existing pass-through tests above already pin.
    client = MagicMock()
    client.call.return_value = {"records": []}
    tools, _client = capture_spec_tools(client=client)

    result = await tools["content_records_list"].run(
        {"site": "testbed", "classRef": "Page", "limit": 10, "_stage": "live"}
    )

    client.call.assert_called_once()
    site, entry, remaining = client.call.call_args.args
    assert site == "testbed"
    assert entry["name"] == "content_records_list"
    assert remaining == {"classRef": "Page", "limit": 10, "_stage": "live"}
    assert result is not None


@pytest.mark.asyncio
async def test_spec_tool_run_injected_site_survives_additional_properties_false():
    # _inject_site's own comment claims `site` is added as a declared
    # property specifically so a strict additionalProperties:false schema
    # still accepts it — nothing exercised that claim through the validator
    # until now. content_auth_session has no properties of its own, so
    # `site` being accepted here is entirely down to the injection.
    client = MagicMock()
    client.call.return_value = {"member": {}}
    tools, _client = capture_spec_tools(client=client)

    await tools["content_auth_session"].run({"site": "testbed"})

    client.call.assert_called_once()


@pytest.mark.asyncio
async def test_spec_tool_run_falls_through_to_dispatch_guard_for_empty_site():
    # The injected `site` property only declares "type": "string" (no
    # minLength), so site="" passes schema validation and reaches
    # _dispatch's own `if not site` check — see that function's docstring.
    # Exercises the exact case that comment claims is still live, so a
    # future removal of _dispatch's guard (mistaking it for now-dead code)
    # would be caught here rather than only by inspection.

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="site"):
        await tools["content_auth_session"].run({"site": ""})
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_accepts_limit_at_the_maximum_boundary():
    # Only the over-the-limit case (501) was covered elsewhere — this pins
    # the boundary itself (500 is valid), so a future schema/validator
    # change to exclusiveMaximum would be caught as a regression here.
    client = MagicMock()
    client.call.return_value = {"records": []}
    tools, _client = capture_spec_tools(client=client)

    await tools["content_records_list"].run(
        {"site": "testbed", "classRef": "Page", "limit": 500}
    )

    client.call.assert_called_once()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_empty_operations_array():
    # content_batch.operations has minItems: 1 — satisfying `required`
    # alone (a present-but-empty array) must still be rejected.

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="operations"):
        await tools["content_batch"].run({"site": "testbed", "operations": []})
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_missing_required_field_nested_in_compose_page_assets():
    # content_compose_page.assets[] requires filename+base64 — a nested
    # required-field case distinct from content_batch.operations[].op's
    # nested *enum* case already covered above, and the more consequential
    # one: a missing base64/filename on an asset item silently reaching the
    # network was part of what daisy-content#9 was originally worried about.

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="base64") as exc_info:
        await tools["content_compose_page"].run(
            {
                "site": "testbed",
                "page": {"match": {"id": 1}},
                "assets": [{"filename": "photo.png"}],
            }
        )
    assert "assets" in str(exc_info.value)
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_spec_tool_run_rejects_wrong_type_for_integer_id():
    # content_page_convert declares `id` as "type": "integer" — a string id
    # (a plausible caller mistake, since content_records_read's `id` is
    # "type": "string" for the same conceptual field on a different tool)
    # must be rejected by validation before it can reach ContentClient.call().

    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("validation_error", match="id"):
        await tools["content_page_convert"].run(
            {"site": "testbed", "id": "5", "className": "BlockPage"}
        )
    client.call.assert_not_called()


# daisy-content#30: the structured part of an error (error_code, status,
# details) has to cross FastMCP's tools/call boundary in the error TEXT.
# FastMCP turns a raised exception into an isError result holding one text
# block, and the DAISY gateway's proxy client keeps only that block's text,
# so exception attributes never arrive. SpecTool.run re-raises every
# MCPError/RuntimeError as a ToolError whose final line is the
# `content-error:` JSON trailer (content_api_mcp.core.errors). These tests go through
# a real FastMCP().call_tool(), an in-memory fastmcp Client, and a fastmcp
# proxy in front of the server, which is the gateway's shape.


def _env_forbidden_error():
    from content_api_mcp._base.errors import AuthorizationError

    return AuthorizationError(
        "Site 'testbed' has population endpoints disabled (403 ENV_FORBIDDEN)",
        details={
            "error_code": "ENV_FORBIDDEN",
            "upstream_details_text": '[{"envVar": "SS_CONTENT_API_ALLOW_POPULATE"}]',
            "upstream_details": [{"envVar": "SS_CONTENT_API_ALLOW_POPULATE"}],
        },
    )


@pytest.mark.asyncio
async def test_error_trailer_survives_the_fastmcp_call_tool_boundary():
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    from content_api_mcp.core.errors import parse_tool_error

    client = MagicMock()
    client.call.side_effect = _env_forbidden_error()
    tools, _client = capture_spec_tools(client=client)

    mcp = FastMCP("test-boundary")
    mcp.add_tool(tools["content_auth_session"])

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("content_auth_session", {"site": "testbed"})

    text = str(exc_info.value)
    assert text.startswith("Site 'testbed' has population endpoints disabled")
    assert parse_tool_error(text) == {
        "kind": "authorization_failed",
        "status": 403,
        "error_code": "ENV_FORBIDDEN",
        "details": [{"envVar": "SS_CONTENT_API_ALLOW_POPULATE"}],
    }


@pytest.mark.asyncio
async def test_error_trailer_reaches_a_real_client_and_survives_a_proxy():
    from fastmcp import Client, FastMCP
    from fastmcp.server import create_proxy

    from content_api_mcp.core.errors import parse_tool_error

    client = MagicMock()
    client.call.side_effect = _env_forbidden_error()
    tools, _client = capture_spec_tools(client=client)

    backend = FastMCP("test-backend")
    backend.add_tool(tools["content_auth_session"])
    proxy = create_proxy(backend, name="test-gateway")

    for server in (backend, proxy):
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool_mcp(
                "content_auth_session", {"site": "testbed"}
            )
        assert result.isError is True
        trailer = parse_tool_error(result.content[0].text)
        assert trailer is not None, result.content[0].text
        assert trailer["error_code"] == "ENV_FORBIDDEN"
        assert trailer["kind"] == "authorization_failed"
        assert trailer["details"] == [{"envVar": "SS_CONTENT_API_ALLOW_POPULATE"}]


@pytest.mark.asyncio
async def test_transport_failure_trailer_has_request_failed_kind():
    from content_api_mcp.core.errors import ContentTransportError

    client = MagicMock()
    client.call.side_effect = ContentTransportError(
        "ContentClient request failed for site 'testbed': connection refused"
    )
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("request_failed", match="connection refused") as err:
        await tools["content_auth_session"].run({"site": "testbed"})
    assert err.trailer == {"kind": "request_failed", "status": None}


@pytest.mark.asyncio
async def test_refused_redirect_trailer_carries_the_3xx_status():
    from content_api_mcp.core.errors import ContentTransportError

    client = MagicMock()
    client.call.side_effect = ContentTransportError(
        "content-api returned an unexpected redirect (302 -> /Security/login)",
        status_code=302,
    )
    tools, _client = capture_spec_tools(client=client)

    with raises_tool_error("unexpected_redirect") as err:
        await tools["content_auth_session"].run({"site": "testbed"})
    assert err.trailer == {"kind": "unexpected_redirect", "status": 302}


@pytest.mark.asyncio
async def test_runtime_error_subclass_from_a_bug_is_not_converted():
    # A RecursionError is a RuntimeError, but not a transport failure: it
    # must not be relabelled request_failed ("retry") or skip masking.
    client = MagicMock()
    client.call.side_effect = RecursionError("maximum recursion depth exceeded")
    tools, _client = capture_spec_tools(client=client)

    with pytest.raises(RecursionError):
        await tools["content_auth_session"].run({"site": "testbed"})


def _real_client_tools(status, body, headers=None):
    from conftest import _client, _mock_response

    tools, _ = capture_spec_tools(client=_client())
    response = _mock_response(status, text=json.dumps(body), headers=headers)
    return tools, patch("requests.Session.request", return_value=response)


@pytest.mark.asyncio
async def test_real_envelope_details_reach_the_trailer_through_spec_tool():
    # End to end through the real ContentClient (only the HTTP call is
    # mocked): pins the `upstream_details` handoff between client.py and
    # errors.py, which the hand-built-details tests above can't see.
    body = {
        "error": {
            "code": "ENV_FORBIDDEN",
            "status": 403,
            "message": "Population endpoints are disabled in this environment.",
            "details": [{"envVar": "SS_CONTENT_API_ALLOW_POPULATE"}],
        }
    }
    tools, patched = _real_client_tools(403, body)
    with patched:
        with raises_tool_error("authorization_failed") as err:
            await tools["content_batch"].run(
                {
                    "site": "testbed",
                    "operations": [{"op": "create", "class": "Page", "fields": {}}],
                }
            )
    assert err.trailer == {
        "kind": "authorization_failed",
        "status": 403,
        "error_code": "ENV_FORBIDDEN",
        "details": [{"envVar": "SS_CONTENT_API_ALLOW_POPULATE"}],
    }


@pytest.mark.asyncio
async def test_upstream_422_and_409_get_validation_and_conflict_kinds():
    for status, code, kind in (
        (422, "UNKNOWN_FIELD", "validation_error"),
        (400, "PAYLOAD_INVALID", "validation_error"),
        (409, "CROSS_PAGE_REPARENT", "conflict"),
    ):
        body = {"error": {"code": code, "status": status, "message": "no"}}
        tools, patched = _real_client_tools(status, body)
        with patched:
            with raises_tool_error(kind) as err:
                await tools["content_records_read"].run(
                    {"site": "testbed", "classRef": "Page", "id": "1"}
                )
        assert err.trailer["status"] == status
        assert err.trailer["error_code"] == code


@pytest.mark.asyncio
async def test_429_retry_after_reaches_the_trailer_as_a_string():
    body = {"error": {"code": "RATE_LIMITED", "status": 429, "message": "slow"}}
    tools, patched = _real_client_tools(429, body, headers={"Retry-After": "30"})
    with patched:
        with raises_tool_error("rate_limit_exceeded") as err:
            await tools["content_auth_session"].run({"site": "testbed"})
    assert err.trailer["retry_after"] == "30"

    tools, patched = _real_client_tools(429, body)
    with patched:
        with raises_tool_error("rate_limit_exceeded") as err:
            await tools["content_auth_session"].run({"site": "testbed"})
    assert "retry_after" not in err.trailer


@pytest.mark.asyncio
async def test_local_input_error_gets_validation_error_kind():
    # ContentClient's own 400s (a bad path segment here) are bare MCPErrors.
    tools, _ = capture_spec_tools(client=__import__("conftest")._client())
    with raises_tool_error("validation_error", match="path argument"):
        await tools["content_records_read"].run(
            {"site": "testbed", "classRef": "Page", "id": ".."}
        )


@pytest.mark.asyncio
async def test_trailer_survives_mask_error_details():
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    from content_api_mcp.core.errors import parse_tool_error

    client = MagicMock()
    client.call.side_effect = _env_forbidden_error()
    tools, _client = capture_spec_tools(client=client)

    mcp = FastMCP("test-masked", mask_error_details=True)
    mcp.add_tool(tools["content_auth_session"])
    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool("content_auth_session", {"site": "testbed"})
    assert parse_tool_error(str(exc_info.value))["error_code"] == "ENV_FORBIDDEN"


@pytest.mark.asyncio
async def test_unexpected_exception_is_not_converted():
    # Only the errors this server raises on purpose get a trailer; anything
    # else (a bug) keeps FastMCP's own handling, including masking.
    client = MagicMock()
    client.call.side_effect = KeyError("boom")
    tools, _client = capture_spec_tools(client=client)

    with pytest.raises(KeyError):
        await tools["content_auth_session"].run({"site": "testbed"})


# Round-2 review finding (pre-#30): the boundary tests above use an
# AuthorizationError/ENV_FORBIDDEN example. This one exercises the exact
# same mechanism through the same
# call_tool path with a different message string (the ROLLBACK_UNVERIFIED-
# undetermined warning) — it does NOT re-verify that content_mcp/client.py
# actually produces that string (test_client.py's
# test_500_without_envelope_on_batch_warns_… owns that, against the real
# ContentClient); the exception here is a hand-built ServiceError on a
# MagicMock, same as the sibling test above. What this adds beyond that
# sibling test is confidence the specific literal text "cannot be ruled
# out" isn't itself mangled/truncated by FastMCP's ToolError wrapping (it
# is longer than the ENV_FORBIDDEN example and worth checking independently
# for that reason), not proof that any particular refactor would be caught.
@pytest.mark.asyncio
async def test_rollback_unverified_undetermined_warning_survives_the_fastmcp_call_tool_boundary():
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    from content_api_mcp._base.errors import ServiceError

    client = MagicMock()
    client.call.side_effect = ServiceError(
        "Site 'testbed' content-api returned a server error (500): server "
        "exploded; the upstream error code could not be determined from the "
        "response body, so a ROLLBACK_UNVERIFIED batch commit cannot be "
        "ruled out — verify the site's data before retrying or assuming it "
        "failed.",
        status_code=500,
    )
    tools, _client = capture_spec_tools(client=client)

    mcp = FastMCP("test-boundary")
    mcp.add_tool(tools["content_batch"])

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(
            "content_batch",
            {
                "site": "testbed",
                # A schema-valid op, so SpecTool.run's validation (daisy-content#9)
                # lets the call reach the mocked client.
                "operations": [
                    {"op": "update", "class": "Page", "id": 1, "fields": {}}
                ],
            },
        )
    client.call.assert_called_once()

    assert "cannot be ruled out" in str(exc_info.value)
    from content_api_mcp.core.errors import parse_tool_error

    assert parse_tool_error(str(exc_info.value))["kind"] == "service_error"


# ---------------------------------------------------------------------------
# daisy-content#14: per-endpoint timeouts. Every tool used to share
# mcp_base.BaseAPIClient.DEFAULT_TIMEOUT (30s) via ContentClient.call()
# never receiving a `timeout` kwarg at all. Pinned here by asserting the
# exact value _dispatch hands to client.call() per tool, not just "some
# int" — a tool silently drifting from FAST/BULK back to the unnamed
# default is exactly the kind of regression a looser assertion would miss.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trivial_tools_get_the_fast_timeout(monkeypatch):
    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    await tools["content_auth_session"].run({"site": "testbed"})
    assert client.call.call_args.kwargs["timeout"] == 10

    client.reset_mock()
    await tools["content_asset_read"].run({"site": "testbed", "id": "1"})
    assert client.call.call_args.kwargs["timeout"] == 10


@pytest.mark.asyncio
async def test_bulk_tools_get_the_bulk_timeout(monkeypatch):
    # Covers one GET bulk tool (content_fingerprint) and one POST bulk tool
    # (content_asset_upload, the 25 MiB case daisy-content#14 itself leads with) — a GET-
    # only assertion here would leave ContentClient.call()'s POST branch
    # (content_mcp/client.py's `return self._request(method, ...)`) unpinned
    # even though _dispatch threads the same `timeout` value through both.
    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    await tools["content_fingerprint"].run({"site": "testbed"})
    assert client.call.call_args.kwargs["timeout"] == 120

    client.reset_mock()
    await tools["content_asset_upload"].run(
        {"site": "testbed", "filename": "a.png", "base64": "eA=="}
    )
    assert client.call.call_args.kwargs["timeout"] == 120


@pytest.mark.asyncio
async def test_unlisted_tools_get_no_explicit_timeout(monkeypatch):
    # None here is not "forgot to set one" — it's the signal ContentClient
    # .call()/._request() already treat as "use DEFAULT_TIMEOUT", so an
    # unlisted tool's behavior is unchanged from before daisy-content#14 existed.
    client = MagicMock()
    tools, _client = capture_spec_tools(client=client)

    await tools["content_records_read"].run(
        {"site": "testbed", "classRef": "Page", "id": "1"}
    )
    assert client.call.call_args.kwargs["timeout"] is None


