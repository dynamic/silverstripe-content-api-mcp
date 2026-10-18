"""Spec-driven tool generation.

Loads the bundled, version-pinned copy of dynamic/silverstripe-content-api's
own module spec (schema/endpoints.json; re-sync with scripts/sync-spec.sh)
and generates one FastMCP tool per entry, instead of hand-writing ~15
near-identical handler functions. The tool contract is the spec's
inputSchema, plus the arguments this package adds at generation time: a
`site` property on every tool (required unless exactly one site is
configured), and the response-shaping arguments (shaping.py).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from functools import partial
from typing import Any

import jsonschema
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import Tool
from mcp.types import ToolAnnotations
from pydantic import PrivateAttr

from content_api_mcp._base.errors import MCPError, ValidationError
from content_api_mcp.core.client import ContentClient
from content_api_mcp.core.errors import ContentTransportError, render_tool_error
from content_api_mcp.core.shaping import inject_shaping, shape_response, split_shaping_args

# Re-exported, not used in this module: build_tools() takes the
# already-loaded spec as an argument. Tests and callers import `load_spec`
# from here; do not remove it as "unused".
from content_api_mcp.core.spec import load_spec  # noqa: F401

LOGGER = logging.getLogger(__name__)

_SITE_PROPERTY = {
    "type": "string",
    "description": (
        "Site slug identifying which configured site to target. Call "
        "content_list_sites first if unsure which slugs are configured: "
        "this server is configured with more than one site, so every "
        "content_* tool call must say which site it's for."
    ),
}


# Hand-authored per tool name, deliberately NOT inferred from `method` —
# daisy-content#19: a host UI needs readOnlyHint/destructiveHint/
# idempotentHint to decide "auto-approve reads, prompt on writes" without
# hardcoding a tool list, and `method` alone can't carry that. Six of these
# entries are POST — none is destructive-by-verb the way GET is
# read-only-by-verb, so each required its own judgment: content_batch's op
# list may contain a `delete`; content_compose_page deletes elements only
# via an opt-in `prune` object — the schema declares no default for
# `prune.enabled` itself, so "opt-in" means "the object is present", not
# "and set to some particular value" (see that entry's own comment);
# content_records_stage is destructive only for `action: unpublish`/
# `archive`, not every action; content_asset_upload's `conflict` defaults
# to `overwrite`, replacing an existing asset of the same name, and its
# `rename` value never hash-skips a repeat identical upload the way
# `overwrite` does (see that entry); content_page_convert and
# content_page_apply_template are each judged on their own entry below.
#
# Where a call's actual blast radius varies by argument, this table applies
# two rules (for destructiveHint specifically) rather than annotating
# optimistically:
#
# Rule 1 (conservative-by-default): if the destructive path is an ordinary
# value of a required argument, or that argument's OWN schema-declared
# default, annotate destructive. content_batch, content_records_stage, and
# content_asset_upload all rest on this — none of those arguments is
# something a caller has to specially opt into supplying, and
# content_asset_upload's `conflict` defaults to the destructive value.
#
# Rule 2 (opt-in carve-out): if the destructive path instead requires the
# caller to supply something the schema neither requires nor defaults into
# existence — a separate optional array/object (`prune`, `assets[]` below),
# or an ordinary field with no schema-declared default whose absence
# triggers no action at all (`content_compose_page.page.convertTo`) — it
# does NOT move the tool out of its additive common case. The dividing
# line: a call that omits the thing does nothing destructive, and nothing
# in the schema defaults it into firing anyway. This is why
# content_asset_upload's `conflict` does NOT qualify for rule 2 despite
# being "just one field" the same shape as `convertTo` — `conflict` has a
# real schema default (`"overwrite"`), so omitting it is not safe, which is
# the whole reason rule 1 catches it instead.
#
# content_compose_page is the deliberate rule-2 exception, with three
# destructive-adjacent paths rather than content_asset_upload's one —
# `prune`, `assets[].conflict`, and `page.convertTo`/`page.force` — see
# that entry's own comment below for why each one qualifies.
# content_page_convert is judged on different grounds entirely: it never
# deletes or overwrites a record — `newClassInstance()` converts a page's
# class in place — so it doesn't fall under either rule above; see its own
# entry for what `force`/`publish` do and don't gate.
#
# This matches how comparable servers annotate a mostly-additive write tool
# (`Automattic/wordpress-mcp`'s `wp_update_post` is `destructiveHint: false`
# despite an update being able to overwrite existing field values —
# verified against `Automattic/wordpress-mcp@a982583`'s
# `includes/Tools/McpPostsTools.php`, whose `wp_update_post` registration
# declares `readOnlyHint: false, destructiveHint: false, idempotentHint:
# true`, alongside an `openWorldHint: false` this table takes the opposite,
# unset position on for every content_* tool below — all 15 call out to an
# arbitrary configured site. content_list_sites (the 16th, hand-written
# tool — see meta.py) is the one exception: it reads only this server's
# own local registry, so it's annotated `openWorldHint: False` directly —
# see _ANNOTATION_FIELDS's own comment in tests/test_server_tools.py.
_TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "content_auth_session": ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    "content_schema_site": ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    "content_schema_class": ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    "content_records_list": ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    "content_records_read": ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    "content_records_parity": ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    "content_fingerprint": ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    "content_asset_read": ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    # `conflict` (schema/endpoints.json) defaults to "overwrite" — uploading
    # different content under an existing filename/folder replaces the
    # stored asset by default. "Identical content is hash-skipped" (the
    # spec's own description) only covers the identical-content case under
    # `overwrite`; it says nothing about same-name/different-content. The
    # destructive path here is both the tool's default AND an ordinary
    # value of an argument, so rule 2's carve-out doesn't apply —
    # destructiveHint=True per rule 1 above.
    #
    # NOT idempotent: `conflict: "rename"` never hash-skips, even on a
    # byte-identical repeat call. Verified against the pinned module's
    # `src/Assets/AssetService.php` (verified at `dynamic/
    # silverstripe-content-api@53e62fb`), `ingest()`: the hash-skip
    # (`$existing->getHash() === sha1($binary)`) only runs inside the
    # `$conflict === 'overwrite'` branch; `rename` falls straight through
    # to the "new record" branch every time, storing a fresh,
    # differently-named `File` (`foo.jpg` -> `foo-2.jpg` -> ...) on each
    # repeat call regardless of content. `skip` and `overwrite` are each
    # independently idempotent, but this annotation is per-tool, not
    # per-`conflict`-value, so the one non-idempotent value governs.
    "content_asset_upload": ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False
    ),
    # Three destructive-adjacent paths, none of which moves this tool out
    # of its additive common case — see the module comment above for the
    # two-rule policy this entry is judged against.
    #
    # 1. `prune` (rule 2: optional object) deletes elements missing from
    #    the payload. A call with no `prune` key sends no pruning
    #    instruction at all, regardless of what any field inside that
    #    object would default to. When `prune` IS sent with `enabled:
    #    true`, `scope` (schema-declared default `"managed"`) can be
    #    widened to `"all"`, deleting every element in the area regardless
    #    of whether this tool itself manages it — verified against the
    #    pinned module's `CompositionService::prune()` (verified at
    #    `dynamic/silverstripe-content-api@53e62fb`): `scope === 'all'`
    #    skips the `managed`-only filter entirely. That widening still
    #    sits behind the same opt-in `prune` object and explicit `enabled:
    #    true`, so it doesn't move this tool out of the "additive unless
    #    the caller constructs a prune request" common case. The schema
    #    declares no default for `enabled` itself (only for `scope`) —
    #    this comment does not claim omitting `enabled` inside a supplied
    #    `prune` object is safe; the classification rests on `prune` being
    #    absent by default, not on any field's default within it.
    # 2. `assets[].conflict` (rule 2: optional array) defaults to
    #    `"overwrite"` — the identical default-overwrite field that's the
    #    sole reason content_asset_upload above is annotated destructive.
    #    It doesn't move this tool's destructiveHint classification because
    #    `assets` is itself an optional array (not in the top-level
    #    `required` list, which is `["page"]` alone) that the caller must
    #    construct and populate per-item; a call with no `assets` entries
    #    has no `conflict` value to default at all, same "absent unless
    #    deliberately included" reasoning as `prune`. It DOES, however,
    #    carry the identical idempotency problem content_asset_upload's own
    #    entry describes: `CompositionService::processAsset()` hands each
    #    `assets[]` entry straight to the same `AssetService::ingest()`
    #    (verified at `dynamic/silverstripe-content-api@53e62fb`) —
    #    an `assets[]` entry with `conflict: "rename"` never hash-skips
    #    here either, so a byte-identical repeat call with that entry still
    #    present stores a fresh asset each time. See this entry's
    #    idempotentHint below.
    # 3. `page.convertTo`/`page.force` (rule 2: undefaulted field) change
    #    the page's class in place via the identical `newClassInstance`
    #    operation content_page_convert's own entry below performs, gated
    #    behind the identical home-page `force` guard — verified against
    #    `CompositionService::resolvePage()`/`convertPage()` (same
    #    citation as item 1). `page` is this tool's one required argument,
    #    so this path doesn't fit the "separate optional object" shape of
    #    1 and 2 above — but `convertTo` itself has no schema-declared
    #    default and gates its own conversion (upstream only converts when
    #    `!empty($pageSpec['convertTo'])`), so a call that omits it
    #    converts nothing, the same "absent unless deliberately included"
    #    dividing line rule 2 turns on. See content_page_convert's own
    #    entry for why the underlying operation isn't itself classified
    #    destructive independent of this carve-out.
    # NOT idempotent, despite the pinned spec's own description claiming
    # "Re-POSTing the same payload is idempotent (updates, never
    # duplicates)" — see item 2 above. Trusting that description outright
    # would be exactly the "overclaim from silence" this table refuses
    # elsewhere (content_asset_upload's own "Identical content is
    # hash-skipped" claim gets the same skepticism, for the same
    # underlying method). The page/elements/prune portion of a repeat call
    # genuinely is idempotent (elements upsert by `externalId`, per the
    # spec's own description) — only the optional `assets[]` path breaks
    # it, but this annotation is per-tool, not per-field, so the one
    # non-idempotent path governs, same policy as content_asset_upload.
    "content_compose_page": ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False
    ),
    # destructiveHint=False on different grounds than the rule-1/rule-2
    # policy above: `newClassInstance()` converts a page's class in place —
    # no record is deleted, and (unlike content_compose_page's `prune`)
    # there's no opt-in object gating a delete this table has to reason
    # about at all. `force` and `publish` don't gate WHETHER the class
    # changes — a call with a valid `id`/`className` always converts
    # (`PageHandler::convert()` no-ops only when the page is already that
    # class) — they gate two separate risk dimensions MCP's binary
    # destructiveHint has no room to express, both verified against this
    # endpoint's own handler, `PageHandler::convert()` (verified at
    # `dynamic/silverstripe-content-api@53e62fb`), not just inferred from
    # the schema's bare `default` values: `force` (schema default `false`) is
    # required only to target the site home page — `convert()` throws
    # unless `$body['force']` when the page's URLSegment/ParentID match the
    # site home page — and `publish` (default `"none"`, enum includes
    # `subtree`) controls what goes live afterward. `publish: "subtree"`
    # does NOT mean "the class change propagates to descendants" — the
    # same handler routes through the identical `PublishOrchestrator::
    # publish()` call content_records_stage's own `mode: "subtree"` uses:
    # it publishes the converted page plus every draft Hierarchy child
    # depth-first, i.e. every descendant's own pending edits, unrelated to
    # this call's class change. A home-page conversion or a wide `subtree`
    # publish is a real operational risk this annotation does not capture;
    # noted here rather than implied away by "the defaults are safe".
    "content_page_convert": ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True
    ),
    # NOT idempotent — one of four entries in this table annotated that
    # way (alongside content_asset_upload and content_compose_page above,
    # and content_batch below). Verified upstream behavior, not
    # conservatism, same basis as content_asset_upload/content_compose_page
    # above (unlike content_batch, whose non-idempotence is conservative —
    # its op list may or may not contain something non-idempotent) — but
    # unlike either of those two, this one isn't a single argument value
    # away from being idempotent: EVERY call duplicates, regardless of
    # arguments. Confirmed against upstream source: `dynamic/
    # silverstripe-elemental-templates@62dd326`'s `src/Service/
    # TemplateElementDuplicator.php`, `duplicateElements()`'s own docblock:
    # "Elements are appended to the end of any existing elements in the
    # area." Re-calling this with the same arguments duplicates the
    # template's elements again. This entry's spec description makes no
    # idempotency claim at all — annotating it True here would be exactly
    # the "overclaim from silence" the module comment above says this
    # table avoids.
    "content_page_apply_template": ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False
    ),
    # A field PATCH on one existing record (daisy-content#51, spec v1.18): no new target
    # record, no relation writes. destructiveHint=False on the same grounds
    # as the wp_update_post precedent above: overwriting field values is the
    # tool's purpose, not a delete. Two paths reach further — verified
    # against dynamic/silverstripe-content-api@e26fdfd
    # (RecordUpdateHandler::update() -> RecordWriter::update()):
    # 1. `publish: "single"` (opt-in; schema default "none") publishes the
    #    record's whole draft, including pending edits someone else made —
    #    a go-live risk this binary hint can't express, same shape as
    #    content_page_convert's `publish` note above.
    # 2. Owned Link records can be archived. Any write that leaves a
    #    has_one-to-Link no longer pointing at a link the record owns — a
    #    link payload with a different `type` (which writes a new link),
    #    `null`, or a repoint to another link id — makes the old link stale
    #    (OwnedLinks::stale()), and PublishOrchestrator::publish() archives
    #    it when the owner publishes: in this same call under
    #    `publish: "single"`, otherwise at the owner's next publish from any
    #    tool. Not marked destructive for consistency with
    #    content_compose_page above, whose element `fields` reach the same
    #    LinkTransformer/OwnedLinks path: the archived record is the
    #    replaced link itself, never a record the caller still references.
    # idempotentHint=True: LinkTransformer::transform() updates a
    # same-type link in place, so even a type-changing payload converges on
    # the second call (a repeat publish finds nothing stale left), and
    # RecordWriter::update() has no append-on-repeat path.
    "content_records_update": ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True
    ),
    # Destructive only for action: unpublish/archive, not every action —
    # annotated conservatively since this is a static per-tool
    # declaration and can't vary by the call's actual `action` argument.
    "content_records_stage": ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True
    ),
    # Op list may contain `delete` — annotated conservatively (destructive,
    # not idempotent) since the actual set of ops varies per call.
    "content_batch": ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False
    ),
}


# daisy-content#14: every tool shared mcp_base.BaseAPIClient.DEFAULT_TIMEOUT
# (30s) regardless of what it actually does — a 25 MiB upload sharing one
# deadline with `auth/session` (should fail in well under a second) meant
# the same number was simultaneously too generous for a hung trivial read
# and too tight for a large write that's doing real, expensive work on the
# far end. Two named tiers, not a per-entry number for all 15: the spread
# is genuinely bimodal (a handful of tools are trivial lookups, a handful
# do real bulk work), and manufacturing precise numbers for the tools in
# between would be guessing, not measuring — daisy-content#10's live smoke test will
# have real observed latencies to tune these against; until then these are
# deliberately round, conservative numbers, not the product of profiling.
# content_records_update (daisy-content#51) is deliberately unlisted: one record's field
# write, neither a trivial lookup nor bulk work, so the 30s default fits.
# Every tool NOT listed here keeps the unchanged 30s default — looked up
# via .get(name), which returns None, and None is exactly what `call()`'s
# own timeout default already means ("use DEFAULT_TIMEOUT").
_FAST_TIMEOUT = 10
_BULK_TIMEOUT = 120
_TOOL_TIMEOUTS: dict[str, int] = {
    # Trivial reads — a single lookup, tiny response. Should fail fast
    # rather than sit for the full 30s default if a site is unreachable.
    "content_auth_session": _FAST_TIMEOUT,
    "content_asset_read": _FAST_TIMEOUT,
    # Real bulk work on the far end, per the entries in daisy-content#14 itself:
    # content_asset_upload reads up to 25 MiB from disk, base64-expands it
    # ~33%, then triggers a SilverStripe image ingest with thumbnail
    # generation; content_compose_page can carry an arbitrary number of
    # inline base64 assets plus a full page composition inside one DB
    # transaction; content_batch's op list is unbounded and optionally
    # atomic; content_fingerprint walks a whole-site content snapshot
    # recursively. None of these should share a deadline with a lookup.
    "content_asset_upload": _BULK_TIMEOUT,
    "content_compose_page": _BULK_TIMEOUT,
    "content_batch": _BULK_TIMEOUT,
    "content_fingerprint": _BULK_TIMEOUT,
}


def _strip_file_path_for_gateway(schema: dict) -> dict:
    """Remove a "filePath" property from a tool's advertised inputSchema.

    daisy-content#17: "filePath" is documented as resolved "on the machine
    running the MCP host" — true for the stdio proxy this schema convention
    was ported from, false here, where the host is a shared gateway
    container that never holds the caller's file. Advertising it in the
    HTTP/gateway deployment makes it the one tool argument that lets an
    LLM-produced string trigger a local file read against THIS container's
    filesystem, not the caller's — a real vector with no legitimate use in
    this deployment shape.

    Stripping it here, rather than only rejecting it at call time, means an
    agent never sees the affordance advertised at all. Because every spec
    entry already declares additionalProperties: false, a caller that
    supplies "filePath" anyway is rejected by SpecTool.run's own schema
    validation before ContentClient.call() — and therefore before
    ContentClient._resolve_file_path, the method that would actually read
    the path — is ever reached. See SpecTool.run below for a
    filePath-specific message that fires ahead of the generic jsonschema
    one, so a caller finds out why and what to use instead.

    Never mutates the caller's dict, matching _inject_site's own contract
    (load_spec()'s result is shared across every tool built from one spec
    load). A no-op for any schema that doesn't declare "filePath" — every
    entry except content_asset_upload today.
    """
    if "filePath" not in schema.get("properties", {}):
        return schema
    schema = dict(schema)
    properties = dict(schema["properties"])
    del properties["filePath"]
    schema["properties"] = properties
    # filePath was never in "required" — it's mutually exclusive with
    # base64, so the spec's `required` (which only names "filename") could
    # never have named it either way. Nothing to remove there.
    return schema


def _inject_site(input_schema: dict, default_site: str | None = None) -> dict:
    """Return a copy of a spec entry's inputSchema with `site` added: as a
    required property, or, when `default_site` is given (exactly one site
    configured), as an optional one defaulting to it. Never mutates the
    caller's dict — load_spec()'s
    result is shared across every tool built from one spec load.
    """
    schema = dict(input_schema)
    properties = dict(schema.get("properties", {}))
    required = list(schema.get("required", []))
    if default_site is None:
        properties["site"] = _SITE_PROPERTY
        if "site" not in required:
            required.append("site")
    else:
        # Exactly one site configured: `site` is optional and defaults to it.
        properties["site"] = {
            **_SITE_PROPERTY,
            "description": (
                f"Site slug. Optional: defaults to {default_site!r}, the only "
                "configured site."
            ),
            "default": default_site,
        }
    schema["properties"] = properties
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)
    # additionalProperties is left exactly as the spec entry declared it
    # (every current entry sets it False) — `site` is now itself a declared
    # property, so a strict additionalProperties:false schema still accepts
    # it rather than rejecting the very argument this injection adds.
    return schema


# Same cap and reasoning as content_api_mcp.core.client.MAX_ERROR_BODY_LEN /
# _truncate_body: jsonschema's own `.message` embeds a repr() of the
# offending instance for validators like `type`/`enum` (NOT
# `additionalProperties`, whose message only names the extra property
# key(s), never the value under them), which for a large
# content_batch/content_compose_page payload could dump the whole (possibly
# huge) value into a message that reaches the calling agent's context
# unfiltered — the error text is the only channel that survives to the
# caller (the `content-error:` trailer, daisy-content#30, rides in that
# same text). Redeclared rather than imported from
# content_api_mcp.core.client, matching that module's own redeclare-don't-import
# convention for this constant.
_MAX_VALIDATION_MESSAGE_LEN = 300


def _describe_validation_error(error: jsonschema.exceptions.ValidationError) -> str:
    """Render a jsonschema ValidationError as a message naming the offending
    property and the constraint it violated (e.g. "limit: 501 is greater
    than the maximum of 500 (violates 'maximum' constraint)"). jsonschema's own
    `.message` already names the property for a `required` failure; for
    other validators (`enum`, `type`, ...) the property only appears via
    `absolute_path`, not in `.message` itself — this adds that path (absent
    for a top-level failure like `required`/`additionalProperties`, where
    the message alone already names the property) and the validator
    keyword, so a caller doesn't have to guess which schema constraint
    fired. Truncated to _MAX_VALIDATION_MESSAGE_LEN, since `.message` can
    otherwise embed an unbounded repr() of the offending value.
    """
    location = ".".join(str(part) for part in error.absolute_path)
    prefix = f"{location}: " if location else ""
    message = f"{prefix}{error.message} (violates {error.validator!r} constraint)"
    if len(message) > _MAX_VALIDATION_MESSAGE_LEN:
        message = message[:_MAX_VALIDATION_MESSAGE_LEN] + "…(truncated)"
    return message


class SpecTool(Tool):
    """A Tool whose schema AND behavior are both driven by one endpoints.json
    entry — mirrors the original proxy's SpecTool.

    `parameters` is the spec entry's (site-injected) inputSchema unchanged,
    not derived from a Python function signature, so the MCP tool contract
    exactly matches what dynamic/silverstripe-content-api documents (plus
    the one `site` property this repo adds).

    FastMCP 3.0.2's `Tool._run` (the server's actual entry point) calls
    `self.run(arguments)` directly, with no schema validation of its own —
    only `FunctionTool` (schema derived from a Python function signature)
    gets that for free, via a pydantic model FastMCP builds from the
    signature. A `Tool` subclass with a hand-supplied `parameters` and an
    overridden `run()`, like this one, owes its own validation or the
    pinned schema is pure documentation and a malformed call reaches the
    network unvalidated (daisy-content#9). `run()` below validates
    `arguments` against `self.parameters` before dispatch.
    """

    _call: Callable[[dict], Any] = PrivateAttr()
    _validator: jsonschema.protocols.Validator = PrivateAttr()
    # Set in build_tools() from the RAW spec entry, before stripping — True
    # only for content_asset_upload today. Distinct from "filePath" simply
    # being absent from self.parameters, which is ALSO true for every other
    # tool that never declared filePath at all. Without this distinction
    # (review finding, daisy-content#39), a caller who mistakenly passes a stray
    # "filePath" to some unrelated tool — content_records_read, say — would
    # get told to "pass the file content directly via base64", an option
    # that tool doesn't have either. Harmless, but wrong and confusing;
    # scoping the filePath-specific message to tools that actually had the
    # property lets every other tool fall through to jsonschema's ordinary
    # additionalProperties rejection instead.
    _had_file_path: bool = PrivateAttr(default=False)

    async def run(self, arguments: dict[str, Any]):
        """Run the call; re-raise any error as a ToolError whose text ends
        with the `content-error:` JSON trailer (daisy-content#30).

        Covers validation failures raised below as well as every error
        ContentClient raises: MCPError subclasses, and the
        ContentTransportError a transport failure or refused redirect
        produces. Any other exception, a RuntimeError subclass such as
        RecursionError included, is a bug and keeps FastMCP's own handling. FastMCP passes a
        ToolError's text through unchanged (no "Error calling tool" prefix,
        and mask_error_details doesn't apply to it), which is what keeps
        the trailer on its own final line.
        """
        try:
            return await self._run_checked(arguments)
        except (MCPError, ContentTransportError) as e:
            raise ToolError(render_tool_error(e)) from e

    async def _run_checked(self, arguments: dict[str, Any]):
        # daisy-content#17: in the gateway deployment, filePath is stripped
        # from self.parameters (_strip_file_path_for_gateway) but the raw
        # spec's properties still declare it, so a caller could construct a
        # call that includes it anyway. additionalProperties: false would
        # already reject that below via the generic jsonschema validator
        # ("Additional properties are not allowed"), but that message
        # doesn't say why or what to use instead. This check fires first,
        # specifically for this one property on a tool that actually had
        # it, so the caller gets a message pointing at the alternative
        # rather than a bare schema violation. No-op when build_tools(allow_file_path=True):
        # there filePath IS in self.parameters, so this condition is never
        # true and validation proceeds as before.
        if (
            self._had_file_path
            and "filePath" in arguments
            and "filePath" not in self.parameters.get("properties", {})
        ):
            raise ValidationError(
                "filePath is not available in this deployment — this server runs as a "
                "shared hosted process, not on your machine, so filePath could only "
                "ever resolve a file on the server's own filesystem, never yours. "
                'Pass the file content directly via "base64" instead.'
            )
        try:
            self._validator.validate(arguments)
        except jsonschema.exceptions.ValidationError as e:
            raise ValidationError(_describe_validation_error(e)) from e
        # ContentClient.call() is a blocking `requests` call — run it off
        # the event loop, same as the proxy's SpecTool.run(), so a
        # slow/retrying upstream request on one site doesn't stall every
        # other in-flight tool call across every other site.
        result = await asyncio.to_thread(self._call, arguments)
        return self.convert_result(result)


def _dispatch(
    client: ContentClient,
    entry: dict,
    allow_file_path: bool,
    timeout: int,
    default_site: str | None,
    arguments: dict,
) -> Any:
    """Pop the injected `site` argument and hand the rest to ContentClient.call().

    `site` is never part of the spec's own path/query/body shape — it's
    added by _inject_site() purely for this server's multi-site routing —
    so it must be popped here rather than passed through, or it would leak
    into the upstream querystring/JSON body as an unexpected field.

    With more than one site configured, a missing `site` key is caught
    earlier by SpecTool.run's schema validation (`site` is required). This
    check is not dead code: the injected `site` property only declares
    `"type": "string"`, so `site: ""` still passes schema validation and
    reaches here. With exactly one site configured, a missing or empty
    `site` falls back to `default_site` instead (daisy-content#18).

    allow_file_path (daisy-content#17 review) is threaded straight through
    to ContentClient.call() rather than re-derived here from `entry`'s raw
    inputSchema. Re-deriving it from `entry` would silently re-arm
    filePath's local-file-read capability regardless of deployment mode —
    `entry` is the RAW spec entry, which always declares "filePath" for
    content_asset_upload, independent of allow_file_path. build_tools() below
    computes this once per build, from the exact same check that decides
    whether "filePath" was stripped from the schema this tool advertises,
    so what a caller is told it can send and what this method will actually
    honor can't drift apart.

    timeout (daisy-content#14) is likewise threaded straight through rather
    than re-derived from `entry["name"]` here — build_tools() below looks it
    up once, in _TOOL_TIMEOUTS, from the same name every other per-tool
    table (_TOOL_ANNOTATIONS) is keyed on.
    """
    arguments = dict(arguments)
    site = arguments.pop("site", None) or default_site
    if not site:
        raise ValidationError('Missing required "site" argument.')
    # response_format/fields (daisy-content#20) are this server's own arguments, popped
    # here for the same reason as `site`; the result is reshaped and capped
    # on the way back (content_api_mcp.core.shaping).
    arguments, shaping_options = split_shaping_args(entry["name"], arguments)
    result = client.call(
        site, entry, arguments, allow_file_path=allow_file_path, timeout=timeout
    )
    return shape_response(entry["name"], result, shaping_options)


def _default_site(client: Any) -> str | None:
    """The only configured slug, when exactly one site is configured.

    Read from `client.sites` directly (not `ContentClient.default_site`) so a
    test double without a real registry gets no default.
    """
    sites = getattr(client, "sites", None)
    if isinstance(sites, dict) and len(sites) == 1:
        return next(iter(sites))
    return None


def build_tools(
    spec: dict[str, Any], client: ContentClient, *, allow_file_path: bool = False
) -> list[SpecTool]:
    """Generate one SpecTool per spec entry. No hand-written per-endpoint handlers.

    allow_file_path: advertise and honour content_asset_upload's "filePath",
    resolved on this process's filesystem. True only when the server runs
    on the caller's own machine (a local stdio process); a shared hosted
    server must leave it False, or a tool argument could read files on the
    host.
    """
    tools = []
    stdio_mode = allow_file_path
    default_site = _default_site(client)
    for entry in spec["tools"]:
        name = entry["name"]
        parameters = inject_shaping(
            name, _inject_site(entry["inputSchema"], default_site)
        )
        # Captured from the RAW schema, before any stripping below, so it
        # reflects what the spec actually declares rather than what's left
        # in self.parameters afterward — see the field's own docstring on
        # SpecTool for why that distinction matters (review finding daisy-content#7).
        had_file_path = "filePath" in parameters.get("properties", {})
        if not stdio_mode:
            parameters = _strip_file_path_for_gateway(parameters)
        # Compiled once here, not per call, in a validator class chosen for
        # the schema's own declared dialect (none of the spec's entries
        # declare one via "$schema", so this resolves to jsonschema's
        # default/latest validator) — a 15-tool server shouldn't re-parse
        # every tool's schema on every invocation.
        validator_cls = jsonschema.validators.validator_for(parameters)
        # check_schema validates the schema itself, not an instance against
        # it — catches a drifted/typo'd schema/endpoints.json (e.g. a bad
        # manual re-sync producing "type": "strin") at boot, the same
        # "fail loudly at boot, not on first call" posture the server's
        # startup already applies to base_route(): without this, a
        # broken entry would build_tools() cleanly and only raise on the
        # first real tool call, as an unframed jsonschema.exceptions.SchemaError
        # instead of the same RuntimeError("Invalid pinned schema/endpoints.json")
        # every other malformed-spec case here already gets.
        #
        # Deliberately checked BEFORE the _TOOL_ANNOTATIONS lookup below: a
        # future spec re-sync could introduce an entry that's both missing
        # its annotation row AND carries a malformed schema, and a broken
        # pinned schema/endpoints.json is the more "upstream" defect of the
        # two — surfacing it first avoids a fix-the-annotation/re-run/
        # discover-the-schema-is-also-broken two-step for whoever's doing
        # the re-sync.
        try:
            validator_cls.check_schema(parameters)
        except jsonschema.exceptions.SchemaError as e:
            raise RuntimeError(
                f"Invalid pinned schema/endpoints.json: entry {name!r}'s "
                f"inputSchema is not a valid JSON Schema: {e}"
            ) from e
        # Fail closed, at boot, the same posture as check_schema() above: a
        # spec entry with no row in _TOOL_ANNOTATIONS would otherwise build
        # an unannotated tool, which an annotation-aware host treats as
        # unsafe-unknown rather than failing loudly here where a future
        # spec re-sync (see CLAUDE.md's "When the module ships a schema
        # change" checklist) actually introduced the gap.
        try:
            annotations = _TOOL_ANNOTATIONS[name]
        except KeyError:
            # Distinct prefix from the check_schema() branch above on
            # purpose: schema/endpoints.json is not the broken artifact
            # here (it's valid — that's what just passed check_schema()).
            # The incomplete artifact is this module's own local
            # _TOOL_ANNOTATIONS side table, and that's where the operator's
            # fix goes — a shared "Invalid pinned schema/endpoints.json"
            # prefix would send them looking in the wrong file, and is also
            # why tests/test_server_tools.py's regression tests for these
            # two branches need distinguishable `match=` patterns.
            raise RuntimeError(
                f"Missing tool annotation: entry {name!r} has no row in "
                "content_api_mcp.core.tools._TOOL_ANNOTATIONS — add one before this "
                "tool can be built."
            ) from None
        tool = SpecTool(
            name=name,
            description=entry["description"],
            parameters=parameters,
            annotations=annotations,
        )
        # allow_file_path/timeout bound POSITIONALLY, not as keywords —
        # SpecTool.run calls self._call(arguments) with exactly one
        # trailing positional arg. functools.partial appends new positional
        # args after the ones already bound, so positional binds here
        # correctly land stdio_mode/timeout in _dispatch's 3rd/4th
        # (allow_file_path/timeout) slots with `arguments` filling the 5th.
        # Binding either as a keyword instead would collide with that same
        # trailing positional (TypeError: got multiple values for argument
        # '...') — verified by trying it for allow_file_path (daisy-content#17); the
        # same mechanics apply here.
        tool._call = partial(
            _dispatch,
            client,
            entry,
            stdio_mode,
            _TOOL_TIMEOUTS.get(name),
            default_site,
        )
        tool._validator = validator_cls(parameters)
        tool._had_file_path = had_file_path
        tools.append(tool)
    return tools
