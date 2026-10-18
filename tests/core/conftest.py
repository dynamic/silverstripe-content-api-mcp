"""Shared test helpers for daisy-content's test suite."""

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import requests

from content_api_mcp.core.client import ContentClient, SiteConfig


def _sites(**overrides):
    base = {
        "testbed": SiteConfig(
            base_url="https://example.com/content-api/v1", token="tok-123"
        )
    }
    base.update(overrides)
    return base


def _client(**overrides):
    return ContentClient(sites=_sites(**overrides))


def _mock_response(status_code=200, json_data=None, text="", headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"x" if json_data is not None else b""
    resp.text = text
    resp.headers = headers or {}
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        err = requests.exceptions.HTTPError(response=resp)
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    return resp


def _non_json_response(text, status_code=200, content_type="text/html"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"x"
    resp.text = text
    resp.headers = {"Content-Type": content_type}
    resp.raise_for_status.return_value = None
    resp.json.side_effect = requests.exceptions.JSONDecodeError(
        "Expecting value", text, 0
    )
    return resp


def capture_meta_tools():
    """Register server._register_meta_tools against a mock MCP + mock
    ContentClient and return ({name: fn}, mock_client) — the hand-written
    (non-spec-driven) tools, e.g. content_list_sites.

    Each captured fn carries its `@mcp.tool(...)` decorator kwargs (e.g.
    `annotations=`) as a `_mcp_tool_kwargs` attribute, since the real
    FastMCP `Tool` object those kwargs would otherwise land on is never
    built here — this stays a plain function, not a Tool.
    """
    from content_api_mcp.core.meta import register_meta_tools

    captured = {}

    def tool_decorator(*dargs, **dkwargs):
        def wrapper(fn):
            fn._mcp_tool_kwargs = dkwargs
            captured[fn.__name__] = fn
            return fn

        return wrapper

    mcp = MagicMock()
    mcp.tool.side_effect = tool_decorator
    client = MagicMock()
    register_meta_tools(mcp, client)
    return captured, client


def capture_spec_tools(spec=None, client=None, allow_file_path=False):
    """Build SpecTool instances via content_api_mcp.core.tools.build_tools()
    directly (bypassing server.py's mcp.add_tool wiring) and return
    ({name: SpecTool}, client). Pass a MagicMock client (the default) to
    inspect how a tool's arguments get dispatched to ContentClient.call()
    without any real HTTP.
    """
    from content_api_mcp.core.tools import build_tools, load_spec

    spec = spec if spec is not None else load_spec()
    client = client if client is not None else MagicMock()
    tools = build_tools(spec, client, allow_file_path=allow_file_path)
    return {t.name: t for t in tools}, client


class _FastBindThreadingHTTPServer(ThreadingHTTPServer):
    # HTTPServer.server_bind() calls socket.getfqdn(host) — a reverse DNS
    # lookup that isn't needed here (nothing reads self.server_name) but
    # can stall tens of seconds in a sandboxed/offline dev environment with
    # no working reverse-DNS path. Skip it; this is otherwise
    # socketserver.TCPServer.server_bind's own body.
    def server_bind(self):
        import socketserver

        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    # Per-connection handler threads default to non-daemon — pytest's own
    # process exit would otherwise block until they wind down naturally.
    daemon_threads = True


def start_sequenced_server(status_codes, retry_after=None, set_cookie=None):
    """Bind an ephemeral local HTTP server (127.0.0.1:0) that replies with
    `status_codes[min(call_count, len(status_codes) - 1)]` on every request
    — a real transport for exercising ContentClient's actual retry/backoff
    behavior (mcp_base.create_http_session) without touching a real external
    host. Most tests in test_client.py/test_spec_contract.py instead patch
    requests.Session.request, but that bypasses urllib3's retry machinery
    entirely and proves nothing about retries actually engaging — this real
    local server is what a test needs instead whenever the thing under test
    is the retry/backoff behavior itself, not just the request shape.

    `retry_after`, if given, adds a `Retry-After` header to every response
    with status >= 400 — for proving 429 isn't retried at the transport
    layer even when the upstream invites it. `set_cookie`, if given, adds a
    `Set-Cookie` header to every response — for proving the session's cookie
    jar stays empty.

    Returns (base_url, handler_cls). `handler_cls.call_count` is the number
    of requests actually received; `handler_cls.last_headers` is the most
    recently received request's headers (an `email.message.Message`), for
    asserting what the client actually sent (e.g. `User-Agent`). Caller must
    call handler_cls.server.shutdown() + .server_close().
    """

    class _Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 (needed for a Content-Length-bearing response requests
        # can parse cleanly) but every response explicitly closes the
        # connection: this server is a ThreadingHTTPServer (one thread per
        # connection) specifically so a kept-alive connection blocked on its
        # own next-request read can't stall any *other* connection — but a
        # single retry attempt's own thread waiting on a request that
        # instead arrives on a fresh connection (urllib3 retry doesn't
        # guarantee same-connection reuse here) would still tie up that
        # thread until DEFAULT_TIMEOUT. Each attempt opening its own
        # connection avoids that entirely, matching ordinary real-world
        # 502-retry behavior anyway.
        protocol_version = "HTTP/1.1"
        codes = list(status_codes)
        call_count = 0
        last_headers = None

        def _handle(self):
            idx = min(type(self).call_count, len(type(self).codes) - 1)
            status = type(self).codes[idx]
            type(self).call_count += 1
            type(self).last_headers = self.headers
            body = b'{"ok": true}' if status < 400 else b"{}"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if retry_after is not None and status >= 400:
                self.send_header("Retry-After", str(retry_after))
            if set_cookie is not None:
                self.send_header("Set-Cookie", set_cookie)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def log_message(self, format, *args):
            pass

    server = _FastBindThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.server = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}/content-api/v1", _Handler


@contextmanager
def raises_tool_error(kind, match=None):
    """Assert a SpecTool.run call raised the ToolError daisy-content#30 produces.

    Checks that the raised error is a fastmcp ToolError whose text is
    exactly render_tool_error() of its cause, and that the trailer's `kind`
    equals `kind`. Yields a holder whose `.value` is the ORIGINAL exception
    (the ToolError's __cause__), so a test's existing assertions on the
    message keep reading the message alone, without the trailer line;
    `.tool_error` is the ToolError itself and `.trailer` the parsed JSON.
    """
    import re
    from types import SimpleNamespace

    import pytest
    from fastmcp.exceptions import ToolError

    from content_api_mcp.core.errors import parse_tool_error, render_tool_error

    holder = SimpleNamespace(value=None, tool_error=None, trailer=None)
    with pytest.raises(ToolError) as exc_info:
        yield holder
    holder.tool_error = exc_info.value
    holder.value = exc_info.value.__cause__
    assert holder.value is not None, "ToolError raised without a cause"
    assert str(holder.tool_error) == render_tool_error(holder.value)
    holder.trailer = parse_tool_error(str(holder.tool_error))
    assert holder.trailer is not None, str(holder.tool_error)
    assert holder.trailer["kind"] == kind, holder.trailer
    if match is not None:
        assert re.search(match, str(holder.value)), str(holder.value)
