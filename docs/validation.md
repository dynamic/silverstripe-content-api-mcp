# Validation

This server is a **thin proxy**. Each tool's `inputSchema` is the spec's declared contract
(enums, `required`, `additionalProperties: false`, `maximum`/`minItems`, defaults like
`_stage: draft`), but **nothing here enforces it before forwarding the call**.

## What that means in practice

- Arguments go to the upstream `/content-api/v1` API as-is.
- Declared defaults are **not** injected client-side — omitting `_stage` sends no `_stage` at
  all, not `draft`. The upstream API applies its own default in that case, so the observable
  behavior matches, but don't rely on this server having filled it in.
- An invalid enum value or a missing non-path-param `required` field is only caught
  **server-side, in the PHP content-api** — this server does not pre-validate beyond what
  FastMCP itself enforces from the schema (basic type/required checking) and the path-param
  substitution in `_resolve_path()` (missing path args raise a client-side `400` before any
  HTTP call is made).
- The PHP content-api is the single source of truth for validation. See its
  [error codes reference](https://github.com/dynamic/silverstripe-content-api/blob/1/docs/en/12_error-codes.md)
  for the full list this server's errors ultimately originate from.

So an invalid call still fails clearly — just one round trip later than local validation would
catch it, with the same structured error code the direct HTTP API would return.

## Error unwrapping

`ContentApiClient._parse_response()` unwraps the upstream error envelope
(`{"error": {code, status, message, details?}}`) into one of three typed exceptions:

| Exception | When |
|---|---|
| `AuthenticationError` | HTTP 401 or 403 |
| `ServiceError` | HTTP >= 500, a 3xx (redirects are never followed — see [Troubleshooting](troubleshooting.md)), or a non-JSON 2xx body |
| `MCPError` | Everything else (4xx validation/business errors) |

All three carry `status_code`, `error_code` (the upstream `code`, e.g. `VALIDATION_FAILED`), and
`details` when the upstream response included them. Branch on `error_code`, not on the message
text — see the module's [error codes reference](https://github.com/dynamic/silverstripe-content-api/blob/1/docs/en/12_error-codes.md).

## A response body edge case worth knowing

A literal JSON `null` body and a genuinely empty 2xx body both resolve to `{}` (empty success) —
they're treated identically, not as different states. A non-empty but non-JSON 2xx body (e.g. an
HTML page from a misconfigured base URL redirecting to a login screen) raises `ServiceError`
rather than being silently treated as success.
