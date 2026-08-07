# Tools

All 14 tools are generated at startup from the bundled `content_api_mcp/schema/endpoints.json`
(check its `version` field for the current spec version — kept byte-identical to the module's own
copy, see [Development](development.md#keeping-the-spec-in-sync)). Each tool's name, description,
and `inputSchema` come directly from that file — this page summarizes them; the spec is the exact
source of truth.

**Call `content_schema_site` first.** It reports which classes are exposed, which optional
integrations are installed, and whether population endpoints are currently enabled — build every
other call from that, not from assumptions about the site's config.

## Reference

| Tool | Method | Path | Required args |
|---|---|---|---|
| `content_auth_session` | GET | `auth/session` | — |
| `content_schema_site` | GET | `schema/site` | — |
| `content_schema_class` | GET | `schema/{classRef}` | `classRef` |
| `content_records_list` | GET | `records/{classRef}` | `classRef` |
| `content_records_read` | GET | `records/{classRef}/{id}` | `classRef`, `id` |
| `content_records_parity` | GET | `records/{classRef}/{id}/parity` | `classRef`, `id` |
| `content_fingerprint` | GET | `fingerprint` | — |
| `content_records_stage` | POST | `records/{classRef}/{id}/{action}` | `classRef`, `id`, `action` |
| `content_batch` | POST | `batch` | `operations` |
| `content_compose_page` | POST | `compositions/page` | `page` |
| `content_asset_upload` | POST | `assets` | `filename` (+ one of `base64`/`filePath`) |
| `content_asset_read` | GET | `assets/{id}` | `id` |
| `content_page_convert` | POST | `pages/{id}/convert` | `id`, `className` |
| `content_page_apply_template` | POST | `pages/{id}/apply-template` | `id`, `templateId` |

## Details

### `content_auth_session`

Verify the current token: member, held permission codes, expiry. No args.

### `content_schema_site`

Discover the site's exposed classes (with verbs/capabilities), detected integrations
(elemental, linkfield, Essentials palette + button labels), environment, and whether
population endpoints are enabled. No args.

### `content_schema_class`

One class's payload contract: fields with types/writability/enum values/token hints, honesty
flags (`computed`: recomputed by the model on save; `importOwned`: owned by an external feed —
both advisory, a write lands but is silently overwritten) with an optional `note`, has_one
payload kinds (`assetRef`/`link`/`recordRef`), has_many/many_many writability and, when the
relation carries extra join data (a classic `many_many_extraFields` map or a many_many
`through` relation backed by a join class), an `extraFields` array naming those fields before
you round-trip `{"id", "extraFields"}` items. **This is where you learn which relations are
polymorphic** before building a write — see
[Workflows](workflows.md#polymorphic-has_one-relations).

`classRef` (required) — short class reference from `content_schema_site`.

### `content_records_list`

List records with filtering (`Field=value`, `Field__PartialMatch` etc.), sorting (`-Field` for
DESC), pagination, and stage selection.

`classRef` (required); `filters` (object, `Field`/`Field__Modifier` keys — modifiers:
`ExactMatch, PartialMatch, StartsWith, EndsWith, GreaterThan(OrEqual), LessThan(OrEqual), not,
nocase, case`); `sort`; `limit` (max `500`); `offset`; `_stage` (`draft`|`live`, default
`draft`).

### `content_records_read`

Read one record by numeric id or `ext:<external-id>`. Returns PascalCase fields that round-trip
into writes, relation ids, and draft/live stage state.

`classRef`, `id` (required); `_stage` (default `draft`).

### `content_records_parity`

Does this record, and everything it `$owns`, match between draft and live, and where do they
differ. Compares a configurable set of the root's own fields
(`Title`/`ParentID`/`ClassName`/`ShowInMenus`/`URLSegment`/`Sort` by default, filtered to
whichever the class actually declares) — the record not existing on live at all is reported as
`liveExists: false`, a legitimate state, not a failure. Also walks the `$owns` tree recursively,
reporting each owned descendant's live/draft status and depth (not a field-level diff — only the
root gets that); an owned descendant disagreeing with the root's own live status either direction
is a mismatch (`ok: false`). Response carries both a machine-readable structure
(`fields`/`owned`/`liveExists`/`ok`) and a flat `report: [{label, ok, message}]` list. `400
PAYLOAD_INVALID` for a non-Versioned class.

`classRef`, `id` (required); `include` (`owned`|`none`, default `owned` — `none` skips the `$owns`
walk); `depth` (caps how far the walk recurses; unset uses the module's configured default).

### `content_fingerprint`

A deterministic, path-keyed snapshot of the site's content for diffing across gates (before/after
a batch) or environments (a local rehearsal vs. production ahead of a replay). Pages are keyed by
URL path, not id — ids churn across a rebuild/re-seed/environment boundary, paths don't. Also
asserts a reachability invariant: `violations` lists every live page (or live `related` record)
whose path runs through a non-live ancestor, since a non-live path segment 404s regardless of the
target row's own live status. `related` sections are project-configured (e.g. a hero-image
relation keyed by its owning page's FK column); an owner id that doesn't resolve to a known page
is counted in `unresolved` rather than leaked as a raw id (`includeIds=true` surfaces a separate
`unresolvedIds` list instead). Applies the same per-row class/record ACL as every other read
endpoint — a class not exposed at all is reported in `meta.skipped`; a specific row this token
can't view is simply absent from the response.

`classes` (comma-separated section refs — `pages` plus any configured `related` ref; omit for
every section); `includeIds` (default `false` — off by default since the whole point is being
diffable across environments where ids aren't stable).

### `content_records_stage`

Publish, unpublish, or archive a record. `unpublish` refuses with `409
UNPUBLISH_STRANDS_DESCENDANTS` if the record has any live `Hierarchy` descendants; `archive`
refuses if it has any in either stage (`SiteTree.enforce_strict_hierarchy` cascades a delete to
every current child in the stage(s) being deleted from) — move/publish them elsewhere first, or
pass `force` to proceed anyway and accept the loss. See
[Workflows](workflows.md#restructure-a-subtree-then-retire-the-old-wrapper).

`classRef`, `id`, `action` (`publish`|`unpublish`|`archive`, required); `mode`
(`single`|`recursive`|`subtree`, publish only — takes precedence over the legacy `recursive`
boolean below; `subtree` publishes the record then every draft `Hierarchy` child depth-first, the
way to publish a moved subtree before unpublishing its old wrapper); `recursive` (default `false`,
legacy shorthand for `mode: recursive`, ignored when `mode` is present); `force` (default `false`,
unpublish/archive only — bypass the descendant-cascade guard); `liveOnly` (`mode: subtree` only,
`400 PAYLOAD_INVALID` otherwise — skip a descendant branch entirely, no publish and no recursing
into it, when it isn't already live, so restructuring a live ancestor can't accidentally resurrect
a page deliberately taken offline); `dryRun` (`mode: subtree` only, same restriction — runs the
full authorization-checked walk and returns the would-publish set in `meta.published` without
writing anything).

### `content_batch`

Run ordered write operations with per-op results and a summary — the agent self-correction
contract: inspect which operations failed and retry only those. `atomic: true` wraps the whole
batch in a transaction and rolls everything back on the first failure. On atomic failure, a
`rolledBack: true` result is independently re-verified before it's reported — every `created` op
is re-checked by id, every `deleted` op whose mode could actually have reached the draft row
(`archive`, or any mode on an unversioned class; `unpublish` on a versioned class only touches
live, so it's correctly skipped) is re-checked, and every `updated` op has its declared fields
re-checked against a pre-write snapshot — an unverified rollback reports `500
ROLLBACK_UNVERIFIED` instead, carrying the same `results` array so every entry can be checked by
hand. Attaching an element to a page whose Elemental config doesn't permit that type fails that op
with `422 ELEMENT_NOT_ALLOWED_ON_PAGE`.

`operations` (required, min 1 item) — each: `op` (`create`|`upsert`|`update`|`delete`, required),
`class` (required), `id`, `externalId`, `fields`, `relations`, `publish`
(`none`|`single`|`recursive`|`subtree`), `mode` (delete mode: `archive`|`unpublish`|`hard`),
`force` (default `false` — `delete` with `mode: unpublish`/`archive` only, bypass the
descendant-cascade guard). `atomic` (default `false`); `defaultPublish`
(`none`|`single`|`recursive`|`subtree`, default `none`); `dryRun` (default `false` — runs the
batch exactly as a real request would, same authorization/resolution/validation, inside a
transaction that's unconditionally rolled back regardless of `atomic` or outcome; on success every
response status is prefixed `would` — `wouldCreate`/`wouldUpdate`/`wouldDelete` — so it can never
be mistaken for a confirmed write; a dry run that itself can't be verified as zero-persistence
reports `500 ROLLBACK_UNVERIFIED` with real, unmapped verbs instead, since at that point the
caller genuinely can't tell whether it committed).

### `content_compose_page`

Compose a full page atomically: match/create/convert the page, upsert ordered elements (array
order sets `Sort`) with children/relations/link payloads/color tokens, ingest assets with
`$ref` aliases, optionally prune managed elements missing from the payload, publish everything
explicitly. Re-POSTing the same payload is idempotent. Any failure rolls the whole request back.

`page` (required) — `match` (required: `{id}`/`{urlSegment}`/`{externalId}`),
`createIfMissing`, `convertTo`, `force`, `areaRelation` (default `ElementalArea`), `fields`.
`publish` (`none`|`recursive`, default `none`); `prune` (`{enabled, scope}`); `assets[]`
(`filename`+`base64` required per entry); `elements[]` (`class`+`externalId` required per
entry). An element type the target page's Elemental config (`allowed_elements`/
`disallowed_elements`) doesn't permit is rejected with `422 ELEMENT_NOT_ALLOWED_ON_PAGE`, listing
the page's actual allowed types — same check the CMS's own "add element" picker uses. See
[Workflows](workflows.md) for a full worked example.

### `content_asset_upload`

Upload a file into the asset store. Identical content is hash-skipped but **always** returns the
full record (`existed: true`) so relations can be wired on every run.

`filename` (required); exactly one of `base64` or `filePath` (see
[Workflows](workflows.md#asset-upload-via-filepath) — `filePath` is resolved client-side, never
sent to the upstream API, capped at 25 MiB, and rejected outright for filenames that look like
credential/secret files); `folder`; `title`; `externalId`; `conflict`
(`overwrite`|`skip`|`rename`, default `overwrite`); `publish` (default `true`).

### `content_asset_read`

Read one asset (numeric id or `ext:`) with `url`, `hash`, and `filename`.

`id` (required).

### `content_page_convert`

Change a page's class via `newClassInstance` (e.g. `Page` → `BlockPage`). Refuses the site home
page without `force: true`.

`id`, `className` (required); `publish` (`none`|`single`|`recursive`|`subtree`, default `none`);
`force` (default `false`).

### `content_page_apply_template`

Apply an `elemental-templates` Template's element composition to a page. Requires
`dynamic/silverstripe-elemental-templates` on the target site.

`id`, `templateId` (required); `publish` (`none`|`recursive`, default `none`).

## What's not wrapped

The module's generic colymba `/api` CRUD surface (stage-unaware, same token) is **not**
wrapped as a tool here — it's a direct HTTP surface for server-to-server callers, not agent
tooling. `content_schema_site`'s response includes a `crud` pointer (`genericCrud` in the spec)
describing where it lives if you need it directly.
