# Validation

## Arguments are validated before any request

Each tool's `inputSchema` is the module spec's declared contract (enums, `required`,
`additionalProperties: false`, `maximum`/`minItems`) plus the arguments this server adds
(`site`, `response_format`, `fields`). `SpecTool.run` validates every call against that schema
with `jsonschema` before anything is sent: a typo'd argument, a wrong type or a bogus enum value
fails with a `validation_error` naming the property and the constraint, and nothing reaches the
site. A path argument that is empty or dot-only (`.`, `..`) is refused the same way.

What passes local validation still meets the PHP content-api's own checks (writable fields,
class and record permissions, placement rules); its
[error codes reference](https://github.com/dynamic/silverstripe-content-api/blob/2/docs/en/12_error-codes.md)
lists those.

Declared defaults are not injected, with one exception: `content_records_list` sends `limit: 25`
when the call names none (the module's own default is 50), see
[Tools](tools.md#response-size).

## Error unwrapping

A failed call returns an MCP error result whose text is a readable message followed by one line
a program can parse:

```
Site 'example.test' has population endpoints disabled (403 ENV_FORBIDDEN): ...
content-error: {"kind": "authorization_failed", "status": 403, "error_code": "ENV_FORBIDDEN", "details": [...]}
```

Split the text on `\n`, take the last line starting with `content-error: `, and `json.loads` the
rest (ASCII-only, strict JSON). Branch on these keys, not on the message text:

| Key | Meaning |
|---|---|
| `kind` | `validation_error` (any rejected input, including an upstream 400/422), `conflict` (409), `not_found`, `authentication_failed` (401), `authorization_failed` (403), `rate_limit_exceeded` (429), `service_error` (≥500, or a non-JSON 2xx body), `request_failed` (no response came back), `unexpected_redirect` (a 3xx, never followed, see [Troubleshooting](troubleshooting.md)) |
| `status` | The HTTP status, or `null` when no response came back |
| `error_code` | The content-api's own `code` (`VALIDATION_FAILED`, `UNKNOWN_FIELD`, `ENV_FORBIDDEN`, ...), when it sent one |
| `retry_after` | The 429 `Retry-After` header, as a string |
| `details` | The content-api's `details` value; replaced by `details_truncated: true` and a capped `details_text` when large |
| `valid_options` | Values that would have worked, when the server knows them (after `UNKNOWN_CLASS`, the site's exposed class refs) |

Keys the error didn't carry are omitted.

Two codes worth calling out because their HTTP status doesn't match the usual intuition:
`ROLLBACK_UNVERIFIED` is a 500 that means "verify your data," not "the server broke." Don't
retry it blindly: re-check every `created`, `deleted` and `updated` result in `details` by id
first (a `deleted` op only needs re-checking when its mode could have reached the draft row:
`archive`, or any mode on an unversioned class). `content_batch`'s `dryRun: true` uses the same
verification on its own rollback, so a dry run can report `ROLLBACK_UNVERIFIED` too, with real
`created`/`updated`/`deleted` verbs instead of `would*`; treat it the same way.
`UNPUBLISH_STRANDS_DESCENDANTS` is a 409 (`kind: conflict`): an expected business-rule refusal
(see [Workflows](workflows.md#restructure-a-subtree-then-retire-the-old-wrapper)), not a fault.

## A response body edge case worth knowing

A literal JSON `null` body and a genuinely empty 2xx body both resolve to `{}` (empty success).
A non-empty, non-JSON 2xx body (e.g. an HTML page from a misconfigured base URL redirecting to a
login screen) raises `service_error` rather than being treated as success.
