# Troubleshooting

## A freshly minted token is rejected (`Token invalid`) even though it works with `curl`

First isolate whether the problem is the token itself or the MCP host's environment:

```bash
curl -H "X-Silverstripe-Apitoken: <token>" https://<site>/content-api/v1/auth/session
```

If that returns `200`, the token is fine and the problem is that the MCP host process never saw
the updated `CONTENT_API_TOKEN` value. This is expected if the host app was launched from the
Dock, Spotlight, or Finder rather than a terminal: GUI-launched apps on macOS inherit
`launchd`'s environment, not your shell profile, so editing `~/.zshrc` (or any shell rc file)
and relaunching the app never picks up the new value, no matter how many times you restart it —
the app was never spawned from a shell that sources that profile in the first place.

**Fixes, in order of preference:**

1. **Use `CONTENT_API_TOKEN_FILE` instead of `CONTENT_API_TOKEN`.** Write the token to a file
   (e.g. `~/.config/content-api-mcp/<site>.token`, `chmod 600`) and point `CONTENT_API_TOKEN_FILE`
   at it in your MCP client config (see [Configuration](configuration.md#token-resolution)). A
   file read at process startup doesn't depend on environment inheritance at all — this works
   identically whether the host was launched from a terminal or the GUI, and survives token
   rotation without touching shell profiles.
2. **Fallback: inject the variable at the macOS user-session level** with
   `launchctl setenv CONTENT_API_TOKEN <value>` so GUI-launched apps see it too. Doesn't persist
   across reboots unless wrapped in a LaunchAgent, and needs re-running on every token rotation —
   `CONTENT_API_TOKEN_FILE` avoids both problems.

## A `filePath` install fails at the `mcp-base` line

`mcp-base` (`dynamic/daisy-base`) is a **private** repo installed via a pinned git commit SHA,
not PyPI. You need `GITHUB_TOKEN` (or `GH_TOKEN`) exported with read access to that repo before
`pip install`/`uvx` can resolve it. See [Installation](installation.md).

## Startup fails with a validation error naming both `CONTENT_API_TOKEN` and `CONTENT_API_TOKEN_FILE`

Neither resolved to a value — set exactly one. See
[Configuration](configuration.md#token-resolution).

## A write call fails with `500`/`ServiceError` and a redirect-related message

This server deliberately does **not** follow HTTP redirects (`allow_redirects=False` in
`ContentApiClient`) — a 3xx response raises `ServiceError` reporting the `Location` header
instead. This is intentional: the token header would otherwise survive a cross-host redirect
(unlike `Authorization`/`Cookie`, which `requests` strips on redirect), risking token egress to
an unintended host. If you're seeing this, check `CONTENT_API_BASE_URL` for a trailing redirect
(e.g. `http://` instead of `https://`, or a base URL that 301s) rather than assuming the server
is broken.

## A call fails with "non-JSON body" in the error message

The upstream response was 2xx but not valid JSON — usually an HTML page (a login screen, a 404
page from a misconfigured `CONTENT_API_BASE_URL`, a proxy error page) rather than the expected
API response. Verify `CONTENT_API_BASE_URL` points at `.../content-api/v1` exactly, and that the
site is actually reachable and running the module. See [Validation](validation.md#a-response-body-edge-case-worth-knowing).

## Nothing above matches

Check the upstream module's own
[error codes reference](https://github.com/dynamic/silverstripe-content-api/blob/1/docs/en/12_error-codes.md)
and [security model](https://github.com/dynamic/silverstripe-content-api/blob/1/docs/en/04_security-model.md)
docs — most failures at this layer originate server-side (permissions, environment gating,
write policy) and surface here verbatim via `error_code`.
