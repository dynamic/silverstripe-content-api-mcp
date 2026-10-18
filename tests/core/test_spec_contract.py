"""End-to-end request-shaping contract tests over every pinned spec entry.

tests/test_client.py exercises ContentClient.call() end to end for a
handful of hand-written, synthetic entries. Nothing asserted that every
spec entry builds the right URL/method/body — a path template typo or a
flipped method in schema/endpoints.json would pass CI. This file closes
that gap by parametrizing over load_spec()["tools"] itself (not a
hand-copied list), so a spec change is exercised the moment it lands —
though not fully *covered* until its known-good (method, path) pair is
added to _EXPECTED_REQUEST_SHAPE below (see
test_expected_request_shape_covers_every_spec_entry for what happens
until then).

Reuses tests/conftest.py's _client()/_mock_response() helpers (same
rootless-import convention tests/test_server_tools.py already uses for
its own conftest.py imports) rather than duplicating them.
"""

import base64
import copy
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import _client, _mock_response

from content_api_mcp._base.errors import MCPError
from content_api_mcp.core.spec import load_spec

_TOOLS = load_spec()["tools"]
_TOOLS_BY_NAME = {entry["name"]: entry for entry in _TOOLS}
_IDS = [entry["name"] for entry in _TOOLS]

_BASE_URL = "https://example.com/content-api/v1"

_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")

# Hardcoded independently of schema/endpoints.json's own "path"/"method"
# fields — deliberately NOT read from `entry["path"]`/`entry["method"]`. A
# test that derived its expectation from the same field it's meant to be
# checking would be tautological: it would pass even if a path template (or
# a method) were typo'd, since both the "expected" and "actual" side would
# be built from the identical (broken) source value. Pinning the literal,
# known-good (method, path) pair per tool name is what actually catches
# spec drift and a path-template typo or flipped method introduced in a
# bad manual re-sync (see this file's docstring). A substitution-order
# regression (a value containing another arg's literal "{token}" text
# getting re-substituted) is a different failure mode this dict doesn't
# pin. The closest guard is
# test_client.py::test_resolve_path_one_arg_value_cannot_leak_into_another_placeholder,
# but per that test's own comment it pins something narrower: that a
# "{token}"-shaped value is *rejected* by _clean_path_segment's allowlist,
# not that substitution is single-pass — verified that a 3-pass
# (re-substitute-over-already-substituted) mutation of _resolve_path still
# passes the whole suite, since the allowlist makes a re-substitution leak
# unobservable either way.
# Pinning method here, not just path, matters: a method flipped from POST
# to GET in a bad re-sync is exactly the kind of drift this dict exists to
# catch, and would go undetected if `entry["method"]` were only ever
# compared against itself.
_EXPECTED_REQUEST_SHAPE = {
    "content_auth_session": ("GET", "auth/session"),
    "content_schema_site": ("GET", "schema/site"),
    "content_schema_class": ("GET", "schema/{classRef}"),
    "content_records_list": ("GET", "records/{classRef}"),
    "content_records_read": ("GET", "records/{classRef}/{id}"),
    "content_records_parity": ("GET", "records/{classRef}/{id}/parity"),
    "content_fingerprint": ("GET", "fingerprint"),
    "content_records_update": ("PATCH", "records/{classRef}/{id}"),
    "content_records_stage": ("POST", "records/{classRef}/{id}/{action}"),
    "content_batch": ("POST", "batch"),
    "content_compose_page": ("POST", "compositions/page"),
    "content_asset_upload": ("POST", "assets"),
    "content_asset_read": ("GET", "assets/{id}"),
    "content_page_convert": ("POST", "pages/{id}/convert"),
    "content_page_apply_template": ("POST", "pages/{id}/apply-template"),
}


def _plausible_value(prop_name: str, prop_schema: dict):
    """A minimal, type-conformant value for one declared property — every
    declared property, required or optional, at every level (top-level or
    nested inside an object/array-of-objects). Nested completeness matters
    just as much as top-level: content_batch.operations[].fields/relations
    and content_compose_page.page/elements[].fields are the actual
    content-population payload this repo exists to send, and none of them
    is required, so a required-only recursion never built or asserted any
    of them (daisy-content#31).

    Not an attempt at semantic realism (content_page_convert doesn't care
    whether `className` names a real class) — only shape/type conformance,
    since what's under test is request construction, not upstream
    validation.
    Deliberately raises on an unrecognized/absent "type" for a scalar
    property, and separately on an array property whose "items" isn't
    itself type "object" (the branch below only knows how to build an
    object item), rather than falling back to a string or an empty list:
    either fallback would silently build a wrong-typed/empty payload and
    the test would still pass, since both sides of the comparison are
    built from the same args dict. The one declared property anywhere in
    schema/endpoints.json with no "type" at all —
    content_batch.operations[].id ("update/delete target: numeric or
    ext:<external-id>") — is handled explicitly by name below rather than
    by relaxing that raise; every other property this file ever builds a
    value for has an explicit "type", and every array property's "items"
    is type "object" (verified against schema/endpoints.json) — but
    silent wrong-shape defeats the point of this file.
    """
    if "enum" in prop_schema:
        return prop_schema["enum"][0]
    prop_type = prop_schema.get("type")
    if prop_type == "integer":
        return 5
    if prop_type == "number":
        return 1.5
    if prop_type == "boolean":
        return True
    if prop_type == "object":
        nested_props = prop_schema.get("properties", {})
        if not nested_props:
            # A handful of nested objects (content_batch.operations[].fields/
            # relations, content_compose_page.page.match/createIfMissing/
            # fields, elements[].fields/children/relations) are genuinely
            # free-form per schema/endpoints.json — no "properties" to
            # recurse into, by design (e.g. "fields" is "Sparse page field
            # updates", arbitrary keys). Building {} for these would make
            # every assertion on them vacuous (a dropped/corrupted value
            # inside would go uncaught, since both sides of the comparison
            # would already agree on {}), so seed one throwaway key instead
            # — safe because none of these nested schemas declares
            # "additionalProperties": false (only the entry's own top-level
            # schema does).
            return {"probe": "test-value"}
        return {
            name: _plausible_value(name, nested_schema)
            for name, nested_schema in nested_props.items()
        }
    if prop_type == "array":
        items = prop_schema.get("items", {})
        if items.get("type") == "object":
            nested_props = items.get("properties", {})
            item = {
                name: _plausible_value(name, nested_schema)
                for name, nested_schema in nested_props.items()
            }
            return [item]
        raise ValueError(
            f"_plausible_value: array property {prop_name!r} has non-object "
            f"items type {items.get('type')!r} — add explicit handling "
            "instead of defaulting to an empty list"
        )
    if prop_type is None and prop_name == "id":
        # content_batch.operations[].id: no declared "type" at all — only
        # reachable now that nested optional properties are built, not just
        # nested required ones (every operations[] entry's "id" is optional).
        return "5"
    if prop_type != "string":
        raise ValueError(
            f"_plausible_value: unrecognized/absent schema type {prop_type!r} "
            f"for {prop_name!r} — add explicit handling instead of defaulting "
            "to a string"
        )
    if prop_name == "id":
        return "5"
    return "test-value"


def _build_arguments(entry: dict) -> dict:
    """Satisfy every declared property (required and optional, including
    path params) with a plausible value, per the entry's own inputSchema —
    never hand-copied.

    Building every declared property, not just the required ones, is
    deliberate: a required-only build leaves test_spec_entry_request_shape's
    non-path-argument assertion vacuous for every entry whose non-path
    required set is empty, even when that entry has real optional
    properties that could carry a payload out. See the comment above
    expected_remaining in test_spec_entry_request_shape for exactly which
    entries that still applies to after this change.

    Two properties are skipped rather than built:
    - "filePath": ContentClient._resolve_file_path pops it and requires
      exactly one of filePath/base64 as a business rule JSON Schema's
      `required` can't express (the schema only requires `filename` on
      content_asset_upload), so building both would raise before a request
      is ever sent. The filePath path itself is covered end to end by
      test_asset_upload_resolves_file_path_through_call below; this generic
      pass only needs a minimal valid payload via the "base64" branch.
    - "filters": flattened into top-level Field/Field__Modifier query keys
      by ContentClient._flatten_query, so it never reaches the wire under
      its own "filters" key — building one here would assert on a key that
      can never appear in the actual request. Real filters coverage is
      test_records_list_filters_and_limit_land_in_params_not_body below.
    """
    props = entry["inputSchema"].get("properties", {})
    args = {
        name: _plausible_value(name, prop_schema)
        for name, prop_schema in props.items()
        if name not in ("filePath", "filters")
    }
    if entry["name"] == "content_asset_upload":
        args["base64"] = base64.b64encode(b"contract-test-bytes").decode("ascii")
    return args


def _expected_path(entry: dict, args: dict) -> str:
    _, template = _EXPECTED_REQUEST_SHAPE[entry["name"]]
    return _PATH_PARAM_RE.sub(lambda m: str(args[m.group(1)]), template)


def _assert_matches_with_types(actual: dict, expected: dict) -> None:
    """Dict equality plus a same-type check, descending into nested dicts
    and lists so the check applies at every level, not just the top one.

    Plain `==` is insufficient here: `bool` is a subclass of `int` in
    Python, so `True == 1` and `{"k": True} == {"k": 1}` are both `True`,
    which a bare equality check can't discriminate between — and that
    applies just as much to a bool nested inside, e.g.,
    content_batch.operations[].force or
    content_compose_page.assets[].publish as to a top-level one (the
    reason this helper now descends at all — daisy-content#31). Whether
    this helper actually has anything to discriminate for a given call
    site (i.e. whether the "expected" side it's handed ever contains a
    real bool vs. an already-normalized int) is a per-call-site question —
    see the GET branch of test_spec_entry_request_shape for the current
    state of that.
    """
    assert (
        actual.keys() == expected.keys()
    ), f"key mismatch: {set(actual)} != {set(expected)}"
    for key, expected_value in expected.items():
        actual_value = actual[key]
        assert type(actual_value) is type(expected_value), (
            f"{key!r}: type {type(actual_value).__name__} != "
            f"{type(expected_value).__name__} ({actual_value!r} vs {expected_value!r})"
        )
        if isinstance(expected_value, dict):
            _assert_matches_with_types(actual_value, expected_value)
        elif isinstance(expected_value, list):
            assert len(actual_value) == len(
                expected_value
            ), f"{key!r}: length {len(actual_value)} != {len(expected_value)}"
            for index, (actual_item, expected_item) in enumerate(
                zip(actual_value, expected_value)
            ):
                assert type(actual_item) is type(expected_item), (
                    f"{key!r}[{index}]: type {type(actual_item).__name__} != "
                    f"{type(expected_item).__name__} ({actual_item!r} vs {expected_item!r})"
                )
                if isinstance(expected_item, dict):
                    _assert_matches_with_types(actual_item, expected_item)
                else:
                    assert (
                        actual_item == expected_item
                    ), f"{key!r}[{index}]: {actual_item!r} != {expected_item!r}"
        else:
            assert (
                actual_value == expected_value
            ), f"{key!r}: {actual_value!r} != {expected_value!r}"


# ---------------------------------------------------------------------------
# 1. One parametrized contract test per spec entry — method, URL, and
#    params-vs-body placement, with no path key leaking into either.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", _TOOLS, ids=_IDS)
def test_spec_entry_request_shape(entry):
    # Pin the spec's own method/path against the hardcoded known-good pair
    # first — a mismatch here means schema/endpoints.json itself drifted
    # (e.g. a bad manual re-sync), distinct from a bug in how ContentClient
    # builds a request from a (correct) template.
    expected_method, expected_template = _EXPECTED_REQUEST_SHAPE[entry["name"]]
    assert entry["method"].upper() == expected_method
    assert entry["path"] == expected_template

    args = _build_arguments(entry)
    path_param_names = set(_PATH_PARAM_RE.findall(entry["path"]))
    # The non-path arguments, exactly as built — computed here in the test,
    # never via ContentClient._flatten_query/_resolve_path, so this doesn't
    # fall into the same trap as the method/path fix above: an "expected"
    # value built by calling the very code under test would pass even if
    # that code silently dropped or corrupted the payload — asserting only
    # that path-param keys are *absent* from params/body would never catch
    # a client change that dropped the whole body/query for every entry
    # whose properties (required or optional) aren't all path params.
    #
    # deepcopy, not a shallow dict/comprehension: args' values reach this
    # dict, ContentClient.call()'s own `dict(arguments or {})`, and
    # _resolve_path's `dict(arguments)` all as shallow copies of the same
    # underlying nested list/dict objects (content_batch.operations,
    # content_compose_page.page, ...). Without deepcopy, an in-place
    # corruption of a nested payload by the client (e.g. clearing a dict/list
    # value before sending it) mutates expected_remaining right along with
    # it, so the comparison below would still pass — verified by mutating
    # _request to clear every dict/list value in its json_data/params before
    # sending: 0 of this file's assertions caught it with a shallow copy,
    # vs. real failures on content_batch/content_compose_page with deepcopy.
    #
    # _build_arguments now builds every declared property, not just the
    # required ones (see its own docstring), so this positive-presence
    # assertion is vacuous only for the 4 of 15 entries with no non-path
    # properties at all: content_auth_session and content_schema_site (no
    # properties whatsoever), plus content_schema_class and
    # content_asset_read (their one property, classRef/id, is itself the
    # only path param). For those 4, expected_remaining is {} and the
    # assertion below compares {} to {} — still meaningful for
    # content_schema_class/content_asset_read, whose path template has a
    # {param}: it's what catches that param leaking *into* params/body
    # (mutating ContentClient._resolve_path to stop popping consumed keys
    # fails those 2, among other non-vacuous cases). It catches nothing at
    # all for content_auth_session/content_schema_site, which have no path
    # params to leak in the first place. Every other entry — including
    # content_records_stage, content_records_update and the four GETs with non-path properties
    # (content_records_list, content_records_read, content_records_parity,
    # content_fingerprint), whose optional properties used to leave this
    # assertion vacuous before _build_arguments covered them — now has a
    # real non-path value flowing through this comparison.
    #
    # That "4 of 15 vacuous" count is about top-level property presence
    # only — it doesn't mean the payload itself was covered. Before
    # daisy-content#31, _plausible_value recursed required-only into
    # nested objects/array items, so content_batch.operations[] and
    # content_compose_page's page/elements[] were built (and compared)
    # near-hollow: none of fields/relations/children — the actual
    # content-population payload this repo exists to send — ever appeared
    # on either side of this comparison (content_compose_page.assets[]
    # carries no fields/relations/children of its own; its own optional
    # properties, e.g. conflict/publish/title, had the same gap).
    # _plausible_value now builds every declared nested property the same
    # way it builds every declared top-level one, so those keys are
    # asserted here too.
    expected_remaining = copy.deepcopy(
        {k: v for k, v in args.items() if k not in path_param_names}
    )

    with patch(
        "requests.Session.request", return_value=_mock_response(200, {"ok": True})
    ) as m:
        result = _client().call("testbed", entry, dict(args))

    assert result == {"ok": True}
    assert m.call_args.kwargs["url"] == f"{_BASE_URL}/{_expected_path(entry, args)}"
    assert m.call_args.kwargs["method"] == expected_method

    if expected_method == "GET":
        assert m.call_args.kwargs["json"] is None
        # GET params go through ContentClient._flatten_query, which
        # normalizes a scalar bool to 0/1 before it hits the wire
        # (daisy-content#8: `True == 1` in Python, but a bare bool and an
        # int serialize differently through `requests` and upstream's
        # DataList::filter()). Normalized independently here, not via the
        # client's own _normalize_query_bool, and compared with an explicit
        # type check below — plain `==` alone wouldn't discriminate a
        # missing/broken normalization, since {"k": True} == {"k": 1} is
        # True in Python regardless of which one the client actually sent.
        # content_fingerprint.includeIds is the one live bool this branch
        # builds today (the schema's only GET boolean property), so the
        # int() conversion and the type check below are non-vacuous for
        # that entry — verified by making _normalize_query_bool a no-op:
        # test_spec_entry_request_shape[content_fingerprint] fails — and
        # type-only (both sides already ints/strings, nothing to convert)
        # for every other GET. Redundantly pinned by
        # test_fingerprint_include_ids_bool_normalized_on_the_wire below
        # (a real pinned spec entry, built inline rather than through this
        # branch) and
        # test_client.py::test_call_get_normalizes_bool_filter_to_int_on_the_wire
        # (a hand-written synthetic entry, same as this file's docstring
        # describes test_client.py using elsewhere).
        expected_params = {
            key: (int(value) if isinstance(value, bool) else value)
            for key, value in expected_remaining.items()
        }
        _assert_matches_with_types(m.call_args.kwargs["params"], expected_params)
    else:
        assert m.call_args.kwargs["params"] is None
        _assert_matches_with_types(m.call_args.kwargs["json"], expected_remaining)


# ---------------------------------------------------------------------------
# 2. content_asset_upload through the real ContentClient.call() entry point
#    (not the bare static _resolve_file_path), proving a real temp file's
#    content round-trips to the request body correctly end to end. Note
#    this does NOT pin call()'s _resolve_file_path-then-_resolve_path
#    ordering: _resolve_path only pops keys a path template names via
#    "{param}", so for content_asset_upload specifically (path "assets",
#    no placeholders) the two calls operate on disjoint keys and commute.
#    No current spec entry combines a path param with a "filePath"
#    property, so nothing today exercises the one way order WOULD be
#    observable if one existed — which validation error fires first
#    (a missing filePath/base64 vs. an illegal path segment), not the
#    substituted value itself.
# ---------------------------------------------------------------------------


def test_asset_upload_resolves_file_path_through_call():
    # allow_file_path=True (daisy-content#17/#39 review): ContentClient.call()
    # defaults to False now, so this test's whole point — that the filePath
    # convenience works through the real call() entry point — needs to
    # explicitly opt in, the same way CONTENT_STDIO-mode dispatch does.
    entry = _TOOLS_BY_NAME["content_asset_upload"]
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"real-asset-bytes")
        tmp_path = f.name
    try:
        with patch(
            "requests.Session.request", return_value=_mock_response(200, {"id": 1})
        ) as m:
            result = _client().call(
                "testbed",
                entry,
                {"filename": "photo.png", "filePath": tmp_path},
                allow_file_path=True,
            )
    finally:
        Path(tmp_path).unlink()

    assert result == {"id": 1}
    assert m.call_args.kwargs["url"] == f"{_BASE_URL}/assets"
    assert m.call_args.kwargs["method"] == "POST"
    body = m.call_args.kwargs["json"]
    assert "filePath" not in body
    assert base64.b64decode(body["base64"]) == b"real-asset-bytes"
    assert body["filename"] == "photo.png"


# ---------------------------------------------------------------------------
# 3. Filters/query case — content_records_list's limit/filters land in
#    params, never the JSON body.
# ---------------------------------------------------------------------------


def test_records_list_filters_and_limit_land_in_params_not_body():
    # ShowInMenus: True exercises daisy-content#8's actual bug path, not
    # just its shape: RecordsHandler::applyFilters feeds a filters value
    # straight into DataList::filter(), with no filter_var()-style parsing
    # to tolerate an unnormalized bare bool — see
    # ContentClient._normalize_query_bool's docstring and
    # test_fingerprint_include_ids_bool_normalized_on_the_wire below for the
    # other GET boolean pin, which goes through content_fingerprint's
    # FingerprintHandler instead (a different, more tolerant upstream path).
    entry = _TOOLS_BY_NAME["content_records_list"]
    with patch(
        "requests.Session.request", return_value=_mock_response(200, {"records": []})
    ) as m:
        result = _client().call(
            "testbed",
            entry,
            {
                "classRef": "Page",
                "limit": 10,
                "filters": {"Title__PartialMatch": "home", "ShowInMenus": True},
            },
        )
    assert result == {"records": []}
    assert m.call_args.kwargs["json"] is None
    params = m.call_args.kwargs["params"]
    assert params == {"limit": 10, "Title__PartialMatch": "home", "ShowInMenus": 1}
    assert type(params["ShowInMenus"]) is int
    assert "classRef" not in params
    assert "filters" not in params


# ---------------------------------------------------------------------------
# 4. Empty case — content_auth_session takes no arguments and produces an
#    empty query with no leftover keys.
# ---------------------------------------------------------------------------


def test_auth_session_sends_empty_params():
    entry = _TOOLS_BY_NAME["content_auth_session"]
    with patch(
        "requests.Session.request", return_value=_mock_response(200, {"member": {}})
    ) as m:
        result = _client().call("testbed", entry, {})
    assert result == {"member": {}}
    assert m.call_args.kwargs["url"] == f"{_BASE_URL}/auth/session"
    assert m.call_args.kwargs["params"] == {}
    assert m.call_args.kwargs["json"] is None


# ---------------------------------------------------------------------------
# 5. Bad-input case — an illegal path segment raises a clean MCPError(400)
#    before any request is ever sent.
# ---------------------------------------------------------------------------


def test_illegal_path_segment_raises_before_any_request():
    entry = _TOOLS_BY_NAME["content_records_read"]
    with patch("requests.Session.request") as m:
        with pytest.raises(
            MCPError, match="invalid value for path argument"
        ) as exc_info:
            _client().call("testbed", entry, {"classRef": "Page", "id": ".."})
    assert exc_info.value.status_code == 400
    m.assert_not_called()


def test_slash_in_an_id_is_encoded_into_one_segment():
    entry = _TOOLS_BY_NAME["content_records_read"]
    with patch(
        "requests.Session.request", return_value=_mock_response(200, json_data={})
    ) as m:
        _client().call("testbed", entry, {"classRef": "Page", "id": "5/../6"})
    assert m.call_args.kwargs["url"].endswith("/records/Page/5%2F..%2F6")


# ---------------------------------------------------------------------------
# 5b. GET boolean normalization on a real, currently-optional spec entry.
#     content_fingerprint.includeIds is a real GET boolean on a pinned
#     entry, and is now also exercised generically by
#     test_spec_entry_request_shape, since _build_arguments builds every
#     declared property (required or optional — see its own docstring),
#     not just the required ones. Kept as its own test anyway: this one
#     builds its arguments and expectations inline rather than through
#     _build_arguments/_EXPECTED_REQUEST_SHAPE, so it keeps pinning this
#     property even if a future change to those loosened the generic
#     pass's coverage of it.
#
#     Provenance correction: an earlier review round described this
#     property as "precisely the daisy-content#8 bug class." It isn't,
#     per ContentClient._normalize_query_bool's own docstring and
#     tests/test_client.py: daisy-content#8's actual breakage is
#     RecordsHandler::applyFilters -> DataList::filter(), a path
#     content_fingerprint never reaches. FingerprintHandler instead reads
#     params through PHP's filter_var(..., FILTER_VALIDATE_BOOLEAN), which
#     already tolerates an unnormalized bare "True"/"False". This test
#     pins client-side bool normalization on a real spec entry — the same
#     *shape* as daisy-content#8, not the code path that actually bit. See
#     test_records_list_filters_and_limit_land_in_params_not_body above
#     for real coverage of the path that did.
# ---------------------------------------------------------------------------


def test_fingerprint_include_ids_bool_normalized_on_the_wire():
    entry = _TOOLS_BY_NAME["content_fingerprint"]
    with patch(
        "requests.Session.request", return_value=_mock_response(200, {"pages": {}})
    ) as m:
        result = _client().call(
            "testbed", entry, {"classes": "pages", "includeIds": True}
        )
    assert result == {"pages": {}}
    params = m.call_args.kwargs["params"]
    assert type(params["includeIds"]) is int and params["includeIds"] == 1


# ---------------------------------------------------------------------------
# 5c. Direct coverage for the nested-payload gap this file used to leave
#     open (daisy-content#31): _plausible_value recursed required-only
#     into nested objects/array items, so content_batch.operations[] and
#     content_compose_page's page/elements[] never carried fields/
#     relations on either side of test_spec_entry_request_shape's
#     comparison, and a nested bool-vs-int mismatch inside either payload
#     was invisible to _assert_matches_with_types. Both tests below are
#     falsifiable on pre-fix code — see daisy-content#31 for the
#     before/after proof.
# ---------------------------------------------------------------------------


def test_build_arguments_builds_nested_optional_properties():
    batch_args = _build_arguments(_TOOLS_BY_NAME["content_batch"])
    operation = batch_args["operations"][0]
    assert "id" in operation
    # fields/relations are free-form maps in the schema (no declared nested
    # "properties" to build from) — asserting non-empty, not just "in", so
    # a regression back to seeding {} (vacuous on both sides of the
    # request-shape comparison) would fail here too.
    assert operation["fields"]
    assert operation["relations"]

    page_args = _build_arguments(_TOOLS_BY_NAME["content_compose_page"])
    assert page_args["page"]["fields"]
    element = page_args["elements"][0]
    assert element["fields"]
    assert element["relations"]


def test_assert_matches_with_types_raises_on_nested_bool_int_mismatch():
    # List-of-dicts case: content_batch.operations[].force is a real
    # nested bool per schema/endpoints.json.
    with pytest.raises(AssertionError):
        _assert_matches_with_types(
            {"operations": [{"force": 1}]},
            {"operations": [{"force": True}]},
        )
    # Plain dict-nesting case (one level, no list in between):
    # content_compose_page.page.force is a real nested bool too.
    with pytest.raises(AssertionError):
        _assert_matches_with_types(
            {"page": {"force": 1}},
            {"page": {"force": True}},
        )


# ---------------------------------------------------------------------------
# 6. Schema sanity — every spec entry is well-formed and internally
#    consistent, independent of any one entry's request-shaping behavior.
# ---------------------------------------------------------------------------


def test_expected_request_shape_covers_every_spec_entry():
    # Catches all three ways _EXPECTED_REQUEST_SHAPE can drift from the live
    # spec, one of which the parametrized test_spec_entry_request_shape
    # above doesn't: a spec entry added without a pin (KeyError at test
    # *run* time — the failing test's id, from _IDS, names the missing
    # entry, so not silent, but this is the one dedicated assertion whose
    # failure message says so directly), a spec entry renamed (same
    # KeyError), or a spec entry *removed* while its stale pin lingers in
    # the dict (no error anywhere else — nothing else re-checks
    # _EXPECTED_REQUEST_SHAPE's keys against the current spec). Together
    # with test_spec_entry_names_are_unique below (spec names have no
    # duplicates), this set-equality assertion also implies
    # len(_TOOLS) == 15, subsuming the old fixed-count check without a
    # second magic number to keep in sync by hand.
    assert set(_EXPECTED_REQUEST_SHAPE) == set(_TOOLS_BY_NAME)


@pytest.mark.parametrize("entry", _TOOLS, ids=_IDS)
def test_spec_entry_has_required_top_level_keys(entry):
    for key in ("name", "method", "path", "description", "inputSchema"):
        assert key in entry, f"entry missing {key!r}: {entry}"


def test_spec_entry_names_are_unique():
    names = [entry["name"] for entry in _TOOLS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("entry", _TOOLS, ids=_IDS)
def test_every_path_param_is_declared_and_required(entry):
    path_params = set(_PATH_PARAM_RE.findall(entry["path"]))
    props = entry["inputSchema"].get("properties", {})
    required = set(entry["inputSchema"].get("required", []))
    for param in path_params:
        assert (
            param in props
        ), f"{entry['name']}: path param {param!r} not a declared property"
        assert (
            param in required
        ), f"{entry['name']}: path param {param!r} not marked required"
