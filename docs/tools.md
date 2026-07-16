# Tools

All 12 tools are generated at startup from the bundled `content_api_mcp/schema/endpoints.json`
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

One class's payload contract: fields with types/writability/enum values/token hints, has_one
payload kinds (`assetRef`/`link`/`recordRef`), has_many/many_many writability. **This is where
you learn which relations are polymorphic** before building a write — see
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

### `content_records_stage`

Publish (optionally recursive), unpublish, or archive a record.

`classRef`, `id`, `action` (`publish`|`unpublish`|`archive`, required); `recursive` (default
`false`, only meaningful with `action: publish`).

### `content_batch`

Run ordered write operations with per-op results and a summary — the agent self-correction
contract: inspect which operations failed and retry only those. `atomic: true` wraps the whole
batch in a transaction and rolls everything back on the first failure.

`operations` (required, min 1 item) — each: `op` (`create`|`upsert`|`update`|`delete`, required),
`class` (required), `id`, `externalId`, `fields`, `relations`, `publish`, `mode` (delete mode:
`archive`|`unpublish`|`hard`). `atomic` (default `false`); `defaultPublish` (default `none`).

### `content_compose_page`

Compose a full page atomically: match/create/convert the page, upsert ordered elements (array
order sets `Sort`) with children/relations/link payloads/color tokens, ingest assets with
`$ref` aliases, optionally prune managed elements missing from the payload, publish everything
explicitly. Re-POSTing the same payload is idempotent. Any failure rolls the whole request back.

`page` (required) — `match` (required: `{id}`/`{urlSegment}`/`{externalId}`),
`createIfMissing`, `convertTo`, `force`, `areaRelation` (default `ElementalArea`), `fields`.
`publish` (`none`|`recursive`, default `none`); `prune` (`{enabled, scope}`); `assets[]`
(`filename`+`base64` required per entry); `elements[]` (`class`+`externalId` required per
entry). See [Workflows](workflows.md) for a full worked example.

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

`id`, `className` (required); `publish` (`none`|`single`|`recursive`, default `none`); `force`
(default `false`).

### `content_page_apply_template`

Apply an `elemental-templates` Template's element composition to a page. Requires
`dynamic/silverstripe-elemental-templates` on the target site.

`id`, `templateId` (required); `publish` (`none`|`recursive`, default `none`).

## What's not wrapped

The module's generic colymba `/api` CRUD surface (stage-unaware, same token) is **not**
wrapped as a tool here — it's a direct HTTP surface for server-to-server callers, not agent
tooling. `content_schema_site`'s response includes a `crud` pointer (`genericCrud` in the spec)
describing where it lives if you need it directly.
