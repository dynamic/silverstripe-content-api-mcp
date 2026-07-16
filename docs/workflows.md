# Workflows

End-to-end recipes for using this server as an agent. Assumes a working `.mcp.json` wiring —
see [Installation](installation.md).

## Always start with schema discovery

```
content_schema_site()
```

Read `classes` for what's exposed and its verbs, `integrations` for what's installed
(elemental/linkfield/essentials palette), and `populationEnabled` for whether batch/composition
endpoints will even work in the current environment. Don't assume a class or integration is
available — check first.

For any class you're about to write to:

```
content_schema_class(classRef="ElementCard")
```

This returns each field's `writable` flag and each relation's `payload` kind
(`assetRef`/`link`/`recordRef`) — build your write payload from this, not from guessing at the
module's config.

## Compose a page, then publish

```
content_compose_page(page={
  "match": {"urlSegment": "about"},
  "createIfMissing": {"title": "About", "parentId": 0, "className": "BlockPage"}
}, publish="recursive", elements=[
  {"class": "ElementCard", "externalId": "about-card-1",
   "fields": {"Title": "Who We Are"}}
])
```

Re-running the identical call is safe — it upserts by `externalId`, never duplicates. Use
`publish: "recursive"` when you want the result live immediately; omit it (default `none`) to
leave everything on draft for review first.

## Batch upsert with self-correction

```
content_batch(operations=[
  {"op": "upsert", "class": "ElementContent", "externalId": "hero", "fields": {"Title": "..."}},
  {"op": "upsert", "class": "ElementContent", "externalId": "footer", "fields": {"Title": "..."}}
])
```

Inspect the response's `results[]` — each entry reports `status: created|updated|error`. On a
partial failure (non-atomic, the default), retry only the failed indices rather than resubmitting
the whole batch. Use `atomic: true` when partial application would leave inconsistent state —
then a single failure rolls everything back and you get one `VALIDATION_FAILED` with the partial
results attached.

## Asset upload via `filePath`

When running as a coding-agent MCP host with access to the local filesystem, prefer `filePath`
over `base64`:

```
content_asset_upload(filename="hero.jpg", folder="about", filePath="/Users/me/images/hero.jpg")
```

The MCP client reads and base64-encodes the file **locally** before sending the request — the
upstream SilverStripe API never sees `filePath` as a field, and you never have to inline a large
binary through your own context window. Use `base64` only when you already have the content
in-memory (e.g. downloaded from elsewhere in the same session) or are calling the underlying
HTTP API directly rather than through this MCP server.

`filePath` and `base64` are mutually exclusive — sending both raises `MCPError` before any HTTP
call is made. Two guardrails apply to `filePath` specifically:

- **25 MiB cap.** The file is read fully into memory then base64-encoded (~33% larger again)
  before sending — an oversized file fails fast with a clear error rather than a slow,
  memory-heavy request that likely times out anyway. Pass `base64` directly for anything larger.
- **Sensitive-path rejection.** A path matching a dotfile/dotdir component (`.ssh`, `.env`,
  `.aws`, `.git`, ...), an SSH private key filename, or a common key/cert extension
  (`.pem`/`.key`/`.pfx`/`.p12`/`.ppk`) is refused outright — `filePath` pointed at something
  sensitive (by mistake, or via a compromised/injected tool argument) shouldn't silently upload
  it as a site asset. This is defense-in-depth, not a security boundary by itself: an MCP host
  already has the same filesystem access as the agent invoking it, and the check is trivially
  bypassed by renaming a file. It exists to catch the easy, accidental case with a clear error,
  not to stop a deliberately malicious caller.

## Polymorphic has_one relations

Before writing a has_one relation, check whether it's polymorphic:

```
content_schema_class(classRef="EmailRecipient")
```

If `hasOne.Form.class` is the abstract `DataObject` type (no single concrete target), a bare id
is rejected as ambiguous. Write it with an explicit class hint instead:

```json
{ "class": "ElementForm", "id": 1431 }
```

or

```json
{ "class": "ElementForm", "externalId": "contact-form" }
```

`"class"` is the same short registry ref `content_schema_site` uses for `classes` keys. Reads of
a polymorphic relation come back in this same `{"id", "class"}` shape, so a `content_records_read`
response round-trips directly into a subsequent write.

## Discover-compose-publish, end to end

1. `content_schema_site()` — confirm `populationEnabled: true` and that the target classes are
   exposed with the verbs you need.
2. `content_schema_class(classRef=...)` for each class you'll write, to confirm field
   writability and relation shapes.
3. `content_compose_page(...)` (or `content_batch(...)` for non-page writes) with `publish:
   "none"` first if you want to review on draft.
4. `content_records_read(classRef=..., id="ext:...", _stage="draft")` to verify the result.
5. `content_records_stage(classRef=..., id=..., action="publish", recursive=true)` — or
   re-run step 3 with `publish: "recursive"` from the start once you trust the payload.
