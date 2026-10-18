"""HTTP client for dynamic/silverstripe-content-api's /content-api/v1 surface.

Unlike media_mcp.client.MediaClient (one shared credential, one upstream
process-wide), ContentClient is **multi-site**: this server is a DAISY
gateway child that serves every user and every client site
(daisy-workspace#9's "Multi-site" departure), so it holds a *registry* of
site configs (slug -> base_url/token/header) rather than a single
access_token/base_url pair, and every request names which site it targets.

Endpoint dispatch (path templating, GET-querystring vs POST-JSON-body
shaping, the filePath->base64 convenience) is a from-scratch reimplementation
of the technique dynamic/silverstripe-content-api-mcp's
content_api_mcp/client.py uses — NOT an import of that package. The two are
deliberately decoupled in code (daisy-workspace#9: "the alternative [...]
would have coupled a public repo's release cadence to a private DAISY
child"); this repo carries its own pinned copy of schema/endpoints.json and
its own drift check (scripts/check-spec-sync.sh) instead.
"""

from __future__ import annotations

import base64
import copy
import http.cookiejar
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from content_api_mcp import __version__
from content_api_mcp._base.errors import (
    AuthenticationError,
    AuthorizationError,
    MCPError,
    NotFoundError,
    RateLimitError,
    ServiceError,
    ValidationError,
)
from content_api_mcp._base.http import BaseAPIClient, create_http_session
from content_api_mcp.core.errors import ContentTransportError
from content_api_mcp.core.spec import base_route

LOGGER = logging.getLogger(__name__)


# A server built on this core passes its own name/version (and, for a
# deployed image, its revision) as `user_agent`; this is the fallback.
DEFAULT_USER_AGENT = f"content-api-mcp/{__version__}"

# Matches mcp_base's own MAX_ERROR_BODY_LEN — redeclared rather than imported,
# same call daisy-media/media_mcp/client.py makes (see that file's comment on
# daisy-base#45/PR daisy-content#46 for why). Bounds what an upstream error body can put in
# a caller-facing exception message; the full body still reaches the logger.
MAX_ERROR_BODY_LEN = 300

# content_records_update's per-environment switch (daisy-content#51), as
# RecordUpdateHandler names it in its ENV_FORBIDDEN details[].config. Used
# only when a PATCH 403 ENV_FORBIDDEN arrives without that details entry.
RECORD_UPDATE_GATE_CONFIG = (
    "Dynamic\\ContentApi\\Control\\Handlers\\RecordUpdateHandler.disabled_environments"
)

# The upstream TokenAuthenticator's configured header (colymba's
# tokenHeader config, checked via ContentApiController::checkAuth) — verified
# against dynamic/silverstripe-content-api-mcp's actual settings.py
# (ContentApiSettings.header) rather than trusted from the issue text alone,
# per daisy-workspace#9's "Auth header" departure. Per-site override is still
# supported (CONTENT_SITES[slug].header) in case a site customizes
# colymba's tokenHeader config, but every real site observed so far uses
# this default.
DEFAULT_TOKEN_HEADER = "X-Silverstripe-Apitoken"

# A filePath-resolved upload is read fully into memory, then base64-encoded
# (~33% larger again) before being sent — cap well under typical asset sizes
# so an oversized file fails fast and clearly instead of a slow, memory-heavy
# request that likely just times out anyway. Same limit and rationale as
# content_api_mcp/client.py's MAX_FILE_PATH_BYTES.
MAX_FILE_PATH_BYTES = 25 * 1024 * 1024  # 25 MiB

# Filename patterns that are essentially never a legitimate upload asset and
# very plausibly a credential/secret file — a defense-in-depth guard against
# "filePath" being pointed at something sensitive, not a security boundary by
# itself (an MCP host already runs with the same filesystem access as the
# agent invoking it). Ported from content_api_mcp/client.py verbatim.
_SENSITIVE_PATH_PATTERNS = (
    re.compile(
        r"(^|/)\."
    ),  # any dotfile/dotdir path component (.ssh, .env, .aws, .git, ...)
    re.compile(r"id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$", re.IGNORECASE),
    re.compile(r"\.(pem|key|pfx|p12|ppk)$", re.IGNORECASE),
)

# Short-TTL cache on the two schema reads (daisy-content#18): they rarely change between
# deploys, and an agent building one page re-reads the same class contract
# several times. Their environment flags (populationEnabled,
# recordUpdateEnabled) can lag an `.env` change by up to the TTL; the
# upstream gates themselves are enforced per request regardless. Keyed per
# (site, path); only parameter-less GETs of
# `schema/site` and `schema/{classRef}` are cached.
DEFAULT_SCHEMA_CACHE_TTL = 300


def schema_cache_ttl() -> float:
    """CONTENT_SCHEMA_CACHE_TTL in seconds, read per call; 0 disables the
    cache. A missing, non-numeric or negative value uses the default."""
    raw = os.getenv("CONTENT_SCHEMA_CACHE_TTL", "").strip()
    if not raw:
        return DEFAULT_SCHEMA_CACHE_TTL
    try:
        value = float(raw)
    except ValueError:
        value = -1
    if value < 0:
        LOGGER.warning(
            "CONTENT_SCHEMA_CACHE_TTL=%r is not a number >= 0; using %d",
            raw,
            DEFAULT_SCHEMA_CACHE_TTL,
        )
        return DEFAULT_SCHEMA_CACHE_TTL
    return value


def _is_schema_path(path: str) -> bool:
    return path == "schema/site" or (
        path.startswith("schema/") and path.count("/") == 1
    )


_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")



def _truncate_body(text: str, limit: int = MAX_ERROR_BODY_LEN) -> str:
    """Bound a body before it reaches a caller-facing message.

    Every call site logs the full, untruncated body first — this only caps
    what gets embedded in an exception message that could be relayed to an
    MCP caller (or through it, a user). `limit` defaults to the full
    caller-facing budget; a caller composing a message from several parts
    (daisy-content#21's UNKNOWN_FIELD/READONLY_FIELD branch) can pass a
    smaller sub-budget so one part can't consume the whole cap.
    """
    if len(text) > limit:
        return text[:limit] + "…(truncated)"
    return text


def _clean_path_segment(value: Any) -> str:
    """Encode a value as one opaque URL path segment.

    Percent-encoded with `quote(..., safe=":")`, so `/`, `?`, `#`, `%` and
    whitespace can't add segments or reinterpret the request, while the
    `ext:` prefix stays literal (the content-api matches it against the
    undecoded segment, then percent-decodes the id after it, module #240, so
    an external id with a space or `/` round-trips). Empty values and
    dot-only values (".", "..") are rejected: requests' URL normalization
    collapses dot segments and could walk the request outside the API's base
    route while still carrying the site's token.
    """
    val_str = str(value)
    if not val_str.strip():
        raise ValueError("Invalid path segment: empty")
    if set(val_str) <= {"."}:
        raise ValueError(
            f"Invalid path segment {val_str!r}: dot-only segments are not allowed"
        )
    return quote(val_str, safe=":")


def _looks_sensitive(path: Path) -> bool:
    text = str(path)
    return any(pattern.search(text) for pattern in _SENSITIVE_PATH_PATTERNS)


def _normalize_query_bool(value: Any) -> Any:
    """Normalize a scalar bool query value to 1/0 so it survives both server-side parses.

    `requests` serializes a bare Python `bool` on the wire as the literal
    string "True"/"False". Upstream's `FingerprintHandler` reads such params
    through `filter_var(..., FILTER_VALIDATE_BOOLEAN)`, which happens to be
    case-insensitive over true/false and tolerates that. But
    `RecordsHandler::applyFilters` feeds every other query var straight into
    `DataList::filter()` with no such parsing — "True"/"False" reaches MySQL
    as a raw string and gets coerced to 0 against a tinyint column, so a
    `filters={"ShowInMenus": True}` call silently matches the opposite rows
    (daisy-content#8). `1`/`0` round-trips correctly through both paths,
    unlike `"true"`/`"false"` (satisfies filter_var but still coerces to 0
    in the ORM filter path). `bool` is checked ahead of any would-be `int`
    handling since `isinstance(True, int)` is `True`.

    Scalar-only; `_query_value` handles lists (comma-joined) and objects.
    """
    if isinstance(value, bool):
        return int(value)
    return value


def _query_value(key: str, value: Any) -> Any:
    """One query param value in the shape the module's filter parser reads.

    A list or tuple becomes a comma-separated string, the module's IN-list
    syntax (RecordsHandler splits on commas). `requests` would otherwise send
    repeated keys, and PHP keeps only the last one, so `{"ID": [1, 2, 3]}`
    silently became `ID = 3`. An element that itself contains a comma can't
    be expressed and is refused. A nested object has no query-string form
    the module reads (`{"Nested": {"a": 1}}` used to go out as `Nested=a`):
    refused, pointing at Field__Modifier keys.
    """
    if isinstance(value, dict):
        raise ValidationError(
            f"filters.{key}: a nested object isn't a filter value; use "
            'Field__Modifier keys instead (e.g. {"Title__PartialMatch": "home"}).'
        )
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, (dict, list, tuple)):
                raise ValidationError(f"filters.{key}: list items must be scalars.")
            text = str(_normalize_query_bool(item))
            if "," in text:
                raise ValidationError(
                    f"filters.{key}: {text!r} contains a comma, which the content "
                    "API's comma-separated IN list can't express."
                )
            parts.append(text)
        return ",".join(parts)
    return _normalize_query_bool(value)


@dataclass(frozen=True)
class SiteConfig:
    """One site's connection details.

    `token_file`, when set, is re-read on every request (`current_token`),
    so a re-minted token reaches a running server without a restart; `token`
    is then only the value read at construction. `ca_file` is a CA bundle
    path for a site on a private CA (e.g. mkcert), passed as `verify=` per
    request so it wins over any REQUESTS_CA_BUNDLE in the environment.
    """

    base_url: str
    token: str
    header: str = DEFAULT_TOKEN_HEADER
    token_file: str | None = None
    ca_file: str | None = None

    def current_token(self) -> str:
        if not self.token_file:
            return self.token
        try:
            value = Path(self.token_file).expanduser().read_text().strip()
        except (OSError, UnicodeDecodeError) as e:
            raise AuthenticationError(
                f"The token file {self.token_file!r} could not be read: {e}. It "
                "may have been removed, or become unreadable, since this server "
                "started."
            ) from e
        if not value:
            raise AuthenticationError(
                f"The token file {self.token_file!r} is empty. If a token-rotation "
                "script is running, this call landed mid-rewrite: retry."
            )
        return value


def parse_site_registry(raw: str) -> dict[str, SiteConfig]:
    """Parse the CONTENT_SITES env var into {slug: SiteConfig}.

    Shape (a JSON object, chosen over a SITE_<SLUG>_URL/_TOKEN env-var-pair
    convention because pydantic-settings has no clean way to enumerate
    dynamically-named env vars, and a single JSON blob is one secret to
    manage in the gateway's env rather than N pairs that grow every time a
    site is added):

        {"acme": {"base_url": "https://acme.example.com/content-api/v1",
                   "token": "...", "header": "X-Silverstripe-Apitoken"}, ...}

    `header` is optional per-entry and defaults to DEFAULT_TOKEN_HEADER.
    `token_file` (re-read per request, and the initial token when `token` is
    absent) and `ca_file` are optional too; see SiteConfig.

    Empty/unset CONTENT_SITES parses to {} rather than raising — same
    "warn, don't crash at boot" posture MediaSettings takes for a missing
    MEDIA_API_KEY (see the server's startup): content_list_sites still
    works and every other tool raises a clean NotFoundError naming the
    (empty) configured set, instead of the whole server failing to start.
    Malformed JSON or a malformed entry IS a hard failure, by contrast: unlike
    "not configured yet", that's a real misconfiguration a boot-time crash
    surfaces far more usefully than a warning that gets scrolled past. A
    malformed entry is a non-object, a missing base_url/token, a base_url with
    no http(s) scheme or no host in it (daisy-content#2 — a host-less netloc
    like "https://:8080" counts as host-less), or a base_url that doesn't end
    with the spec's base route (see content_api_mcp.core.spec.base_route,
    daisy-content#11). The port is NOT validated here.

    The scheme check is not an SSRF guard — `requests` ships no transport
    adapter for anything but http(s) anyway — it only moves the failure from
    the first opaque connection error on a live tool call to boot, where every
    other CONTENT_SITES misconfiguration already fails.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"CONTENT_SITES is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(
            "CONTENT_SITES must be a JSON object of {slug: {base_url, token, header?}}"
        )
    sites: dict[str, SiteConfig] = {}
    for slug, cfg in data.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"CONTENT_SITES[{slug!r}] must be an object")
        base_url = cfg.get("base_url")
        token = cfg.get("token")
        token_file = cfg.get("token_file")
        if not token and token_file:
            try:
                token = Path(str(token_file)).expanduser().read_text().strip()
            except OSError as e:
                raise ValueError(
                    f"CONTENT_SITES[{slug!r}].token_file could not be read: {e}"
                ) from e
        if not base_url or not token:
            raise ValueError(
                f"CONTENT_SITES[{slug!r}] must set base_url and token (or token_file)"
            )
        # daisy-content#2: a scheme-less, non-http(s), or host-less base_url
        # used to boot clean and fail on the first real tool call with an
        # opaque `requests` connection error naming no slug. Checked before
        # the base-route suffix below so the more fundamental problem is the
        # one reported for an entry that has both. urlsplit itself can raise
        # ValueError on a malformed bracketed host (e.g. "http://[::1/x"), so
        # that is framed into the same slug-naming ValueError as everything
        # else here rather than escaping as "Invalid IPv6 URL". Note urlsplit
        # strips surrounding ASCII whitespace and lowercases the scheme before
        # deciding, so " https://x/..." and "HTTPS://x/..." both pass: the
        # uppercase form is correct (schemes are case-insensitive per RFC
        # 3986), and the padded form is deliberately not this check's business
        # — parse_site_registry has never stripped padding out of base_url, so
        # rejecting it here would be a new failure mode rather than the
        # boot-vs-first-call timing change this issue asks for (the padded
        # string is still what SiteConfig stores and what _request builds URLs
        # from, unchanged from before this check existed).
        try:
            parsed_url = urlsplit(str(base_url))
            # hostname, not netloc: a netloc can be non-empty with no host in
            # it at all — "https://:8080/..." is netloc ":8080" with hostname
            # None, "https://user@/..." is netloc "user@" with hostname None —
            # and both then fail on the first live call exactly the way a bad
            # scheme does. Read inside the try purely defensively: on the
            # interpreter this was checked against, every malformed bracketed
            # host already raises from urlsplit() itself and .hostname raised
            # nothing for any input tried, so today the try only ever catches
            # urlsplit's own ValueError — the placement is what keeps a
            # hypothetical future .hostname raise framed the same way instead
            # of escaping bare. Note this does NOT validate the
            # port: neither urlsplit() nor .hostname parses it (only the .port
            # property does, and nothing here reads it), so
            # "http://x:abc/..." still boots — tracked separately.
            host = parsed_url.hostname
        except ValueError as e:
            raise ValueError(
                f"CONTENT_SITES[{slug!r}].base_url is not a parseable http:// or "
                f"https:// URL; got {base_url!r} ({e})"
            ) from e
        if parsed_url.scheme not in ("http", "https") or not host:
            raise ValueError(
                f"CONTENT_SITES[{slug!r}].base_url must be an http:// or "
                f"https:// URL with a host; got {base_url!r}"
            )
        normalized_url = str(base_url).rstrip("/")
        # daisy-content#11: a base_url missing the spec's base route booted
        # clean and 404'd every call, with an error naming the record, not
        # the misconfiguration. The leading "/" anchors this to a path
        # segment boundary, not a bare substring match — otherwise
        # "my-content-api/v1" would wrongly satisfy a "content-api/v1" check.
        expected_suffix = "/" + base_route()
        if not normalized_url.endswith(expected_suffix):
            raise ValueError(
                f"CONTENT_SITES[{slug!r}].base_url must end with the "
                f"content-api base route {expected_suffix!r} (from "
                f"schema/endpoints.json); got {base_url!r}"
            )
        header = cfg.get("header") or DEFAULT_TOKEN_HEADER
        ca_file = cfg.get("ca_file")
        sites[slug] = SiteConfig(
            base_url=normalized_url,
            token=str(token),
            header=str(header),
            token_file=str(token_file) if token_file else None,
            ca_file=str(Path(str(ca_file)).expanduser()) if ca_file else None,
        )
    return sites


class ContentClient(BaseAPIClient):
    """Multi-site client for dynamic/silverstripe-content-api's /content-api/v1 surface.

    Extends BaseAPIClient for the shared logging/error-body-capping
    conventions, but access_token/base_url on the base instance are unused
    placeholders — every real request resolves its own SiteConfig via the
    `site` argument threaded through call()/_request(), since (unlike
    MediaClient) there is no single upstream this process talks to.
    """

    def __init__(
        self, sites: dict[str, SiteConfig], user_agent: str | None = None
    ):
        super().__init__(access_token="", base_url="")
        self.sites = sites
        self._schema_cache: dict[tuple, tuple] = {}
        self._schema_cache_lock = threading.Lock()
        # One shared session for connection reuse + retry/backoff, per
        # mcp_base.create_http_session's contract — its allowed_methods
        # (HEAD/GET/OPTIONS only) keeps non-idempotent writes (POST/PATCH/
        # DELETE) un-retried on a response status or a read error
        # (daisy-content#13). A *connect*-phase error (refused/unreachable
        # host) is retried for every method regardless of allowed_methods —
        # urllib3's Retry.increment() checks that case before ever
        # consulting allowed_methods — but that's still safe here: by
        # definition the request never reached the server, so retrying it
        # can't double-commit a write. The per-site auth header can't live
        # on this session the same way (it varies by site), so it stays
        # per-request — see _request below.
        #
        # 429 is excluded from status_forcelist (unlike create_http_session's
        # default) and respect_retry_after_header is disabled below: urllib3's
        # Retry.is_retry() retries a 429/413/503 carrying a Retry-After header
        # via that flag *independent of* status_forcelist membership, so
        # excluding 429 from the forcelist alone would not have stopped it.
        # Left enabled, a GET rate-limited with a large Retry-After would
        # sleep inside urllib3 for that entire value — urllib3 2.6+ clamps
        # this via retry_after_max, but its default is 21600s/6h, not a
        # meaningful bound in practice, and DEFAULT_TIMEOUT bounds a single
        # socket operation, not a multi-attempt retry sequence — before
        # _request's own 429 handling, which raises RateLimitError carrying
        # Retry-After so the *caller* decides when to retry, ever got a
        # chance to run.
        self._session = create_http_session(
            user_agent=user_agent or DEFAULT_USER_AGENT,
            status_forcelist=(500, 502, 503, 504),
        )
        # Set on both mount points explicitly rather than relying on
        # create_http_session mounting the same HTTPAdapter instance under
        # both prefixes (true today, but an implementation detail of a
        # separately-versioned package — daisy-content#13 review) — an
        # http://-configured site must not silently regress to sleeping on
        # Retry-After if that aliasing ever changes.
        for _scheme in ("http://", "https://"):
            self._session.get_adapter(
                _scheme
            ).max_retries.respect_retry_after_header = False
        # Auth here is header-based (see DEFAULT_TOKEN_HEADER), so the
        # session has no legitimate use for cookies — without this, a
        # SilverStripe session cookie set on one call would ride along on
        # every later call this process makes, to any site, since the
        # session (and its cookie jar) is shared across every caller and
        # every configured site (daisy-content#13 review).
        self._session.cookies.set_policy(
            http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
        )

    def close(self) -> None:
        """Release the shared session's pooled connections.

        Correct for the single process-wide instance the server's
        startup builds (review finding: it previously had no way to be
        released short of process exit / GC). Not called automatically
        anywhere in this module — the caller (the server's startup, or a test that
        constructs its own ContentClient) owns the instance's lifetime and
        decides when it's done with it.

        NOT safe to call while any SpecTool.run() dispatch may still be
        in flight on this instance: this client is shared process-wide and
        every call is dispatched off the event loop via asyncio.to_thread
        (tools.py), so a concurrent in-flight `_session.request(...)`
        on another thread would race a `close()`-driven pool teardown. Only
        call this once no dispatch threads can still be using the instance
        (e.g. at process shutdown, after the server has stopped accepting
        new tool calls) — there is no wiring in this repo that calls it
        today.
        """
        self._session.close()

    def list_sites(self) -> list[str]:
        """Configured site slugs, sorted — never includes tokens/base URLs."""
        return sorted(self.sites)

    def default_site(self) -> str | None:
        """The only configured slug when exactly one site is configured."""
        return next(iter(self.sites)) if len(self.sites) == 1 else None

    def get(self, site: str, path: str, timeout: int = None) -> Any:
        """A parameter-less GET, served from the schema cache for
        `schema/site` and `schema/{classRef}` (daisy-content#18). Returns a deep copy, so
        a caller reshaping the result can't change the cached value."""
        ttl = schema_cache_ttl() if _is_schema_path(path) else 0
        key = (site, path)
        if ttl > 0:
            with self._schema_cache_lock:
                hit = self._schema_cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < ttl:
                return copy.deepcopy(hit[1])
        result = self._request("GET", path, site=site, params={}, timeout=timeout)
        if ttl > 0:
            with self._schema_cache_lock:
                self._schema_cache[key] = (time.monotonic(), copy.deepcopy(result))
        return result

    def exposed_class_refs(self, site: str) -> list[str] | None:
        """Class refs from the site's (cached) schema/site, or None when it
        can't be read. Used to teach after an UNKNOWN_CLASS (daisy-content#21)."""
        try:
            schema = self.get(site, "schema/site")
        except (MCPError, ContentTransportError) as e:
            self._logger.warning(
                "ContentClient: could not read schema/site for site %r to list "
                "valid class refs: %s",
                site,
                e,
            )
            return None
        data = schema.get("data") if isinstance(schema, dict) else None
        classes = data.get("classes") if isinstance(data, dict) else None
        return sorted(classes) if isinstance(classes, dict) else None

    def _resolve_site(self, site: str) -> SiteConfig:
        cfg = self.sites.get(site)
        if cfg is None:
            configured = ", ".join(self.list_sites()) or "(none configured)"
            raise NotFoundError(
                f"Unknown site {site!r}. Configured sites: {configured}"
            )
        return cfg

    def _request(
        self,
        method: str,
        path: str,
        site: str,
        params: dict = None,
        json_data: dict = None,
        timeout: int = None,
    ) -> Any:
        """Extends BaseAPIClient._request: resolves `site` to a SiteConfig,
        sends its token under its configured header (not
        `Authorization: Bearer` — the content-api's TokenAuthenticator
        expects a custom header; see DEFAULT_TOKEN_HEADER), and maps upstream
        status codes to typed mcp_base errors the same way MediaClient does
        (401 -> AuthenticationError, 404 -> NotFoundError, 429 ->
        RateLimitError with Retry-After), extended with 403 ->
        AuthorizationError (daisy-content#6) and >=500 -> ServiceError, with
        a distinct message for `ROLLBACK_UNVERIFIED` (a batch that may or may
        not have committed — verify, don't blindly retry) and, on the
        `batch` path, a commit-uncertainty warning for every OTHER 5xx —
        ROLLBACK_UNVERIFIED is the only code the pinned spec documents as
        carrying commit information, so any other code (or no code at all)
        can't rule out a ROLLBACK_UNVERIFIED batch commit, and the message
        says so rather than reading as an ordinary transient error
        (daisy-content#42); every other >=500 (any 5xx on a non-`batch`
        path) gets the plain generic message. Every remaining 4xx
        (400/409/422/...) raises a bare MCPError carrying `status_code`,
        with a distinct message for `UNKNOWN_FIELD`/`READONLY_FIELD`
        (daisy-content#21): upstream's per-field `details` array (each entry
        naming the offending field, its own code, and why) is rendered as
        prose in the message.
        A `ContentTransportError` (a `RuntimeError` subclass) means no HTTP
        *error* response was mapped: a
        genuine transport failure — on a non-GET `batch` call it
        appends a commit-uncertainty hazard sentence, since no usable
        response came back to read the commit status out of and the batch
        may have committed (daisy-content#42) — or an unexpected 3xx redirect this
        client refuses to follow (see the redirect check above, which fires
        with a real HTTP response in hand). A 2xx response whose body isn't parseable JSON
        (`response.json()`'s `JSONDecodeError` is itself a `RequestException`
        subclass, so without an inner guard it would land in that same branch
        despite a real 200 in hand — the HTML login-page case) raises
        `ServiceError` instead, carrying the real status code, the response's
        `Content-Type`, and a truncated body (ROADMAP.md §A item 5, resolved
        in daisy-content#12). A literal JSON `null` body normalizes to `{}`, matching the
        204/empty-body branch above.

        Every raised error's `details` carries the upstream content-api
        envelope's `error_code`/`details` when the body was a parseable
        envelope that supplied them (`_upstream_details`) — both keys are
        optional, so a consumer must use `details.get("error_code")`, never
        assume presence (daisy-content#7). 429 additionally always carries
        `retry_after` alongside those optional keys, and a non-empty
        envelope `details` also arrives parsed as `upstream_details`.
        Exception attributes don't cross FastMCP's `tools/call` boundary;
        SpecTool.run renders these fields into the error text's
        `content-error:` JSON trailer instead (content_api_mcp.core.errors,
        daisy-content#30), which is what an MCP caller branches on.
        """
        cfg = self._resolve_site(site)
        url = f"{cfg.base_url}/{path.lstrip('/')}"
        headers = {cfg.header: cfg.current_token()}
        if json_data is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self._session.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_data,
                timeout=timeout or self.DEFAULT_TIMEOUT,
                # The content-api surface never redirects in normal
                # operation, and `requests` only strips Authorization/Cookie
                # on a cross-host redirect — a custom header like
                # X-Silverstripe-Apitoken (set fresh per-request here, since
                # it varies by site) would be preserved and re-sent to
                # whatever a redirect points at. Disabling it means a
                # misconfigured base_url or an open redirect can't egress a
                # site's token; any 3xx surfaces as an error below instead of
                # being silently followed.
                allow_redirects=False,
                **({"verify": cfg.ca_file} if cfg.ca_file else {}),
            )
            if 300 <= response.status_code < 400:
                raise ContentTransportError(
                    f"content-api returned an unexpected redirect "
                    f"({response.status_code} -> {response.headers.get('Location', '?')}); "
                    "redirects are not followed",
                    status_code=response.status_code,
                )
            response.raise_for_status()
            # A transient 5xx that succeeds on retry would otherwise leave
            # no trace: the only log calls on this path are the non-JSON and
            # JSON-null branches below, and urllib3's own status-retry line
            # is log.debug (not enabled by default), so a site failing most
            # of its requests but always eventually recovering reads as
            # perfectly healthy (review finding, daisy-content#13). Guarded
            # with an isinstance check, not a bare truthiness check, since a
            # mocked response's `.raw.retries.history` in tests is itself a
            # truthy MagicMock that isn't real retry history.
            retry_history = getattr(getattr(response, "raw", None), "retries", None)
            history = getattr(retry_history, "history", None)
            if isinstance(history, tuple) and history:
                self._logger.warning(
                    "ContentClient: site %r request %s %s succeeded after "
                    "%d failed attempt(s) (final status %d)",
                    site,
                    method,
                    path,
                    len(history),
                    response.status_code,
                )
            if response.status_code == 204 or not response.content:
                return {}
            # Must be caught here, before the `except RequestException` below:
            # requests.exceptions.JSONDecodeError is itself a RequestException
            # subclass (as well as a ValueError), so without this inner guard
            # a non-JSON 2xx body falls into that generic handler and loses
            # its status code/Content-Type/body, re-labeled a bare "request
            # failed" (ROADMAP.md §A item 5, daisy-content#12) — the realistic
            # trigger being a base_url that resolves to an HTML login page, a
            # WAF interstitial, or a maintenance page. Mirrors
            # daisy-media/media_mcp/client.py's identical guard, but raises
            # ServiceError (not RuntimeError) with the real status_code: this
            # case has an actual HTTP response in hand, matching what
            # dynamic/silverstripe-content-api-mcp raises for it and this
            # file's own documented "RuntimeError means no HTTP response was
            # mapped" contract above.
            try:
                data = response.json()
            except ValueError as e:
                content_type = response.headers.get("Content-Type", "unknown")
                body_text = response.text
                self._logger.error(
                    "ContentClient non-JSON response %d for site %r "
                    "(Content-Type: %s): %s",
                    response.status_code,
                    site,
                    content_type,
                    body_text,
                )
                raise ServiceError(
                    f"Site {site!r} returned a non-JSON {response.status_code} "
                    f"response (Content-Type: {content_type}): "
                    f"{_truncate_body(body_text)}",
                    status_code=response.status_code,
                ) from e
            # A literal JSON `null` body parses to None — normalize to {}
            # for parity with the 204/empty-body branch two lines up and
            # with the stdio proxy, rather than handing callers a bare None.
            # Unlike that branch (a documented "no content" HTTP signal),
            # `null` on a 2xx with real content is unexpected enough
            # (review finding: an upstream serialization bug, or a
            # tombstoned record returned as `null` instead of a 404, would
            # look identical) that silently returning {} with no trace
            # would leave nothing to debug from later — log it, same
            # discipline as the non-JSON branch above.
            if data is None:
                self._logger.warning(
                    "ContentClient: site %r returned a JSON null body for "
                    "%s %s (2xx, non-empty content) — normalizing to {}",
                    site,
                    method,
                    path,
                )
                return {}
            return data
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code
            body_text = e.response.text
            message, error_code, details, raw_message = self._extract_upstream_error(
                body_text, status
            )
            self._logger.error(
                "ContentClient API error %d for site %r: %s", status, site, body_text
            )
            if status == 401:
                raise AuthenticationError(
                    f"Site {site!r} rejected its content-api token (401): {message}",
                    details=self._upstream_details(error_code, details),
                )
            if status == 404:
                raise NotFoundError(
                    f"content-api 404 on site {site!r}: {message}",
                    details=self._upstream_details(error_code, details),
                )
            if status == 429:
                retry_after = e.response.headers.get("Retry-After")
                raise RateLimitError(
                    f"Site {site!r} rate-limited this request (429)"
                    + (f", retry after {retry_after}s" if retry_after else "")
                    + f": {message}",
                    details={
                        "retry_after": retry_after,
                        **self._upstream_details(error_code, details),
                    },
                )
            if status == 403:
                # Only ENV_FORBIDDEN gets narrowed to a distinct message
                # today — every other upstream 403 code (or an
                # absent/unrecognised one) falls through to the generic
                # permission-flavoured message below.
                #
                # The upstream code/details ride in `details`, not the
                # exception's own `error_code` attribute — that attribute is
                # mcp_base's stable per-class wire identifier
                # ("authorization_failed", the same one every sibling branch
                # above leaves at its class default) rather than a slot for
                # an upstream-specific code. Both keys are optional: they
                # exist only when the upstream body was a parseable
                # content-api envelope that supplied them, so a consumer
                # branching on the upstream code must use
                # `details.get("error_code")`, never assume presence. A 403
                # served as an HTML login interstitial (ROADMAP.md §A item 5)
                # yields `details == {}`.
                details_payload = self._upstream_details(error_code, details)
                # ENV_FORBIDDEN (403 per dynamic/silverstripe-content-api's
                # src/Errors/ErrorCode.php::httpStatus()) and
                # SS_CONTENT_API_ALLOW_POPULATE (src/Security/EnvironmentGate.php)
                # verified directly against that public repo — including that
                # the gate is population-domain-only (EnvironmentGate's own
                # docblock: "batch, compositions, page actions, asset writes";
                # the message below names the concrete `compositions/page`
                # endpoint path from this repo's own pinned
                # schema/endpoints.json rather than repeating the docblock's
                # category word verbatim) and that its dev/test allowlist is
                # a configurable `population_enabled_environments`, not a
                # hardcoded rule — so the message names both remedies the
                # operator actually has.
                # A second ENV_FORBIDDEN source since spec v1.18 (daisy-content#51):
                # content_records_update's own per-environment switch,
                # RecordUpdateHandler.disabled_environments — NOT the
                # population gate, and SS_CONTENT_API_ALLOW_POPULATE does not
                # open it. Told apart by the upstream details entry: that
                # handler names its config key in details[].config, while
                # EnvironmentGate sends details[].envVar instead (both
                # verified against dynamic/silverstripe-content-api@e26fdfd).
                # Without this the population message below sent the caller
                # to the wrong remedy. PATCH is a fallback signal for an
                # envelope whose details didn't survive (only
                # content_records_update sends PATCH, and the population gate
                # never fires on it).
                update_gate_config = None
                if error_code == "ENV_FORBIDDEN":
                    update_gate_config = self._record_update_gate_config(details)
                    if not update_gate_config and method.upper() == "PATCH":
                        update_gate_config = RECORD_UPDATE_GATE_CONFIG
                if update_gate_config:
                    raise AuthorizationError(
                        f"Site {site!r} has single-record updates disabled "
                        "(403 ENV_FORBIDDEN): this is a config gate, not a "
                        "permissions problem, and not the population gate "
                        "(SS_CONTENT_API_ALLOW_POPULATE does not open it). "
                        "The site's "
                        f"{_truncate_body(update_gate_config, limit=120)} "
                        "config lists this environment; remove it there to "
                        "reopen content_records_update (an entry added by "
                        "another module's YAML can't be removed in YAML, "
                        "since config arrays merge additively: override it "
                        "in PHP with Config::modify()->set()). "
                        f"Upstream message: {message}",
                        details=details_payload,
                    )
                if error_code == "ENV_FORBIDDEN":
                    raise AuthorizationError(
                        f"Site {site!r} has population endpoints disabled "
                        "(403 ENV_FORBIDDEN): this is a config gate, not a "
                        "permissions problem — population endpoints "
                        "(batch, compositions/page, page actions, asset writes) "
                        "are restricted to the environments in the module's "
                        "EnvironmentGate.population_enabled_environments "
                        "config (dev, test by default). Either add this "
                        "site's environment to that allowlist, or set "
                        "SS_CONTENT_API_ALLOW_POPULATE on the site to a "
                        "truthy value (1, true, on, or yes). "
                        f"Upstream message: {message}",
                        details=details_payload,
                    )
                raise AuthorizationError(
                    f"Site {site!r} refused this request (403): {message}",
                    details=details_payload,
                )
            if status >= 500:
                # status_code=status is required on both raises below, not
                # just details= — ServiceError's class default (502) would
                # otherwise silently override the real upstream status
                # (500/503/504 all becoming "502" on the exception), which is
                # exactly the "computed then discarded" bug class this issue
                # exists to close, just recurring one branch down.
                service_details = self._upstream_details(error_code, details)
                if error_code == "ROLLBACK_UNVERIFIED":
                    raise ServiceError(
                        f"Site {site!r} content-api returned ROLLBACK_UNVERIFIED "
                        f"({status}): the server cannot confirm whether this "
                        "request's batch committed — verify the site's data "
                        "directly before retrying or assuming it failed; do not "
                        f"blindly retry. Upstream message: {message}",
                        status_code=status,
                        details=service_details,
                    )
                if path == "batch":
                    # daisy-content#42: denylist, not allowlist. The
                    # ROLLBACK_UNVERIFIED branch above is the only 5xx code
                    # the pinned spec documents anywhere as carrying commit
                    # information (a batch that may or may not have
                    # committed), so EVERY other 5xx on this path — a
                    # determined-but-undocumented code, a blank code, or no
                    # code at all — carries zero commit information and a
                    # ROLLBACK_UNVERIFIED batch commit cannot be ruled out.
                    # The pre-#42 gate was `not error_code and path ==
                    # "batch"` (allowlist: warn only when NO code could be
                    # extracted), which let a fronting gateway's
                    # content-api-shaped {"error": {"code": 502, ...}}
                    # suppress the warning despite saying nothing about
                    # whether the batch committed. Inverted: on `batch`,
                    # warn for every 5xx except ROLLBACK_UNVERIFIED. The
                    # cost of an extra warning on a genuinely transient 5xx
                    # is a false-positive log line; the cost of staying
                    # silent on a real commit-uncertain 5xx is a duplicated
                    # batch nobody notices until content review. Safe-by-
                    # default wins over quieter-by-default.
                    #
                    # Scoped to `path == "batch"` (round-2 review finding),
                    # not every >=500 — `ROLLBACK_UNVERIFIED` appears in
                    # schema/endpoints.json only in the content_batch entry
                    # (confirmed by grepping the whole pinned spec for it,
                    # not just reading that one entry's prose — an earlier
                    # comment here mis-cited a nearby sentence that's
                    # actually about `dryRun`, not this code), so surfacing
                    # this warning on e.g. a GET records/Page 500 would be a
                    # false alarm on every unrelated transient error,
                    # training an operator to stop reading the one warning
                    # that matters. `compositions/page`'s "any failure rolls
                    # the whole request back" carries a similar-sounding
                    # risk, but rolling back as a unit means a retry can't
                    # land in a *half-committed* state the way an
                    # interrupted batch can — that's the specific hazard
                    # this warning is about, not "could a retry ever have
                    # any duplicate-causing side effect at all" (the spec's
                    # own "re-POSTing is idempotent" claim for that endpoint
                    # is narrower than it sounds: daisy-content#19's
                    # content_api_mcp.core.tools._TOOL_ANNOTATIONS annotates
                    # content_compose_page idempotentHint=False, since an
                    # assets[] entry with conflict: "rename" does duplicate
                    # on repeat — asset duplication isn't a rollback/
                    # half-commit hazard, so it doesn't argue for widening
                    # this warning's scope either). Hence `batch`, and not a
                    # broader "any transactional endpoint" scoping.
                    #
                    # This is a plain string-equality check against the
                    # spec's own path, not this repo's tool name, so a
                    # `content_batch` rename wouldn't break it — but a
                    # future spec change to the *path* itself would, and
                    # test_batch_path_matches_spec (tests/test_client.py)
                    # exists specifically to fail loudly if that ever
                    # happens, since nothing else here would catch it.
                    #
                    # `not error_code`, not `error_code is None` (review
                    # finding, mutation-tested): a well-formed envelope can
                    # legitimately supply `{"code": ""}` — an accepted
                    # envelope shape per _extract_upstream_error's own
                    # `error.get("message") or error.get("code")` gate,
                    # which only requires ONE of the two to be truthy.
                    # Post-#42 both sub-branches warn, so the gate decides
                    # WHICH warning a blank code gets: it is exactly as
                    # unusable for ruling out ROLLBACK_UNVERIFIED as no code
                    # at all, so it must get the "could not be determined"
                    # message, not be misreported as a determined-but-
                    # undocumented code. Mutating to `error_code is None`
                    # routes a blank code to the determined branch, failing
                    # test_500_with_blank_error_code_on_batch_still_warns's
                    # positive "could not be determined" assertion.
                    if not error_code:
                        raise ServiceError(
                            f"Site {site!r} content-api returned a server error "
                            f"({status}): {message}; the upstream error code could "
                            "not be determined from the response body, so a "
                            "ROLLBACK_UNVERIFIED batch commit cannot be ruled out — "
                            "verify the site's data before retrying or assuming it "
                            "failed.",
                            status_code=status,
                            details=service_details,
                        )
                    # The determined code IS named here, shape-guarded and
                    # capped (review finding): `message` is NOT a reliable
                    # channel for it. On _extract_upstream_error's code-only
                    # path (envelope has `code` but no/empty `message` — an
                    # accepted shape per its own `message or code` gate) the
                    # caller-facing message is the RAW truncated body, where
                    # the code appears only incidentally and a large sibling
                    # field can push it past the truncation cut; and
                    # the message sentence is what a human reads first (the
                    # `content-error:` trailer, daisy-content#30, carries the code for a
                    # program).
                    # The guard keeps the two failure modes the old comment
                    # defended against: only scalar str/int codes are named
                    # (a dict/list code would surface as a raw Python repr —
                    # daisy-content#21's exact failure mode), and the value
                    # is capped (a 5 KB code must not become a 5 KB
                    # exception message). Non-scalar codes fall back to
                    # naming the situation only.
                    if isinstance(error_code, (str, int)):
                        named_code = _truncate_body(str(error_code), limit=64)
                        code_clause = f"the upstream error code ({named_code}) is "
                    else:
                        code_clause = "the upstream error code is "
                    raise ServiceError(
                        f"Site {site!r} content-api returned a server error "
                        f"({status}): {message}; {code_clause}not documented as "
                        "ruling out a commit, so a "
                        "ROLLBACK_UNVERIFIED batch commit cannot be ruled out "
                        "— verify the site's data before retrying or assuming "
                        "it failed.",
                        status_code=status,
                        details=service_details,
                    )
                raise ServiceError(
                    f"Site {site!r} content-api returned a server error ({status}): {message}",
                    status_code=status,
                    details=service_details,
                )
            if error_code in ("UNKNOWN_FIELD", "READONLY_FIELD") and isinstance(
                details, list
            ):
                # daisy-content#21: applyFields()'s per-field `details` array
                # (each entry {field, code, message}) otherwise reaches the
                # caller only as JSON in the `content-error:` trailer (daisy-content#30).
                # Render it as plain prose in the message too. This is the closest
                # thing upstream supplies to "why": it names each rejected
                # field's own reason, not the class's full writable-field
                # set (a schema re-fetch could add that, but daisy-content#18's
                # schema cache is roadmap-gated — see ROADMAP.md §C).
                field_problems = [
                    f'"{p["field"]}" ({p["code"]}): {p["message"]}'
                    for p in details
                    if isinstance(p, dict)
                    and p.get("field")
                    and p.get("code")
                    and p.get("message")
                ]
                if field_problems:
                    # The true rejected-field count is a short, bounded
                    # integer — state it in its own clause up front, never
                    # subject to truncation, so it can't be undercounted no
                    # matter what happens to the rest of the message. Two
                    # earlier review rounds each found a way for a
                    # length-budget interaction to make the count lie: first
                    # by counting the post-shape-filter list instead of the
                    # raw array (dropping entries in an unrecognized shape),
                    # then by appending the correction *after* the per-field
                    # text so a long field list truncated it away too.
                    # Counting from `details` (not `field_problems`) and
                    # placing it in its own untruncated clause closes both
                    # at once: it no longer depends on how many entries are
                    # shape-valid OR on how much room is left after the
                    # per-field breakdown.
                    count_clause = f"{len(details)} field(s) rejected"
                    # raw_message is upstream's own sentence (never `message`,
                    # which carries the `{code}: ` prefix this branch's own
                    # prefix already names) — worth keeping
                    # (same "never drop the upstream text" discipline the
                    # ENV_FORBIDDEN/ROLLBACK_UNVERIFIED precedent branches
                    # follow), but it's free-form and could be long, so it
                    # gets its own short cap rather than the count's.
                    upstream_clause = (
                        f" (upstream: {_truncate_body(raw_message, limit=100)})"
                        if raw_message
                        else ""
                    )
                    # Whatever's left after the two clauses above (both
                    # short and bounded) goes to the field-by-field
                    # breakdown — best-effort: `_truncate_body`'s own
                    # "…(truncated)" suffix is the signal that not every
                    # rejected field made it into this list, same as the
                    # count_clause is the signal for how many there truly
                    # were.
                    prefix = f"ContentClient API error {status} for site {site!r}: {error_code}: "
                    reserved = (
                        len(prefix)
                        + len(count_clause)
                        + len(upstream_clause)
                        + len(" — ")
                    )
                    field_budget = max(MAX_ERROR_BODY_LEN - reserved, 40)
                    field_text = _truncate_body(
                        "; ".join(field_problems), limit=field_budget
                    )
                    raise MCPError(
                        f"{prefix}{count_clause}{upstream_clause} — {field_text}",
                        status_code=status,
                        details=self._upstream_details(error_code, details),
                    )
            raise MCPError(
                f"ContentClient API error {status} for site {site!r}: {message}",
                status_code=status,
                details=self._upstream_details(error_code, details),
            )
        except requests.exceptions.RequestException as e:
            hazard = ""
            # daisy-content#42: a transport failure with no usable
            # response is the strongest commit-uncertainty signal this
            # client can emit — there is nothing to read the commit status
            # out of, and a blind retry of a non-GET `batch` risks a
            # duplicated batch, so the hazard sentence says verify before
            # retrying. Same path discriminator as the 5xx branch above:
            # the spec's own `batch` path, non-GET methods only (a GET
            # can't have committed anything). The sentence says "failed
            # before a usable response could be read", not "no response
            # was received" — the branch deliberately fires on partial-
            # response cases too (see below), where a response WAS
            # received but is unusable, and the false premise would send
            # an operator reasoning the wrong way about whether the write
            # reached the server.
            #
            # Deliberately NOT narrowed by exception class to "read-phase"
            # failures: the requests hierarchy doesn't expose a reliable
            # reached-the-server distinction. In requests 2.31-2.34 (the
            # whole `requests>=2.31.0` pin range, verified against both
            # endpoints) a read timeout while the BODY is streaming back
            # surfaces as requests.exceptions.ConnectionError —
            # Response.iter_content wraps urllib3's ReadTimeoutError as
            # ConnectionError — so the server accepted the request and
            # started answering, the batch may have committed, and a
            # ConnectionError exclusion would drop the hazard exactly
            # there. The cost of not excluding is a connection-refused
            # batch getting one harmless "verify" nudge on a retry that
            # was safe; the cost of excluding is silence on a stall where
            # the batch may have committed. Safe-by-default wins, same
            # call as the 5xx branch above.
            if path == "batch" and method.upper() != "GET":
                hazard = (
                    " The request failed before a usable response could be "
                    "read, so it cannot be determined whether the batch "
                    "committed — verify the site's data before retrying."
                )
            self._logger.error(
                "ContentClient request failed for site %r: %s%s", site, e, hazard
            )
            raise ContentTransportError(
                f"ContentClient request failed for site {site!r}: {e}{hazard}"
            ) from e

    @staticmethod
    def _extract_upstream_error(
        body_text: str, status_code: int
    ) -> tuple[str, Any, dict | list | None, str | None]:
        """Unwrap content-api's {"error": {code, status, message, details?}}
        envelope into a plain message when present, else fall back to the
        raw (truncated) body. content-api's errors are usually much more
        useful than a raw HTML/JSON dump — a class name, a missing field,
        a 409 conflict reason — so this is a light-touch quality improvement
        over MediaClient's "just truncate the raw body" approach, matched to
        what this particular upstream actually returns.

        The 2nd element (error_code) is whatever JSON type the envelope's
        `code` field carried — usually a str or int error code, but a
        dict/list is possible. It is annotated `Any`, not a narrower type,
        because a narrower annotation would be a false constraint; the 5xx
        `batch` branch shape-guards it before naming it in a message.

        The 4th element is the raw, untruncated, pre-composition `message`
        (None when the envelope supplied none) — the composed 1st element
        carries a `{code}: ` prefix (`details` is never folded into it; it
        travels as JSON in the `content-error:` trailer, daisy-content#30),
        so a caller that wants to build its OWN prose from `details`
        (daisy-content#21's UNKNOWN_FIELD/READONLY_FIELD branch) needs the
        plain upstream sentence back.
        """
        try:
            payload = json.loads(body_text) if body_text else None
        except ValueError:
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        # Gated on `code` OR `message` being present, not `message` alone —
        # a caller (e.g. _request's 403 branch) that needs `code` to branch
        # on must still get it even if upstream ever sends a well-formed
        # envelope with a `code` but an empty/missing `message`. Review
        # finding: gating on `message` alone silently dropped `code` for
        # that shape, defeating any code-based branch downstream.
        if isinstance(error, dict) and (error.get("message") or error.get("code")):
            code = error.get("code")
            details = error.get("details")
            raw_message = error.get("message")
            if not raw_message:
                # Code-only envelope: hand back `code`/`details` so a caller
                # that branches on the upstream code still gets it, but leave
                # the caller-facing message on the raw-body fallback exactly
                # as before. Composing a message out of `code` here would
                # change what every status surfaces — and would drop the
                # sibling fields (`recordId`, `hint`, ...) that live next to
                # `code` rather than inside `details`, which the raw body
                # still shows. Only `code`/`details` needed to stop being
                # dropped; the message did not.
                return (_truncate_body(body_text), code, details, None)
            plain_message = str(raw_message)
            # `details` is NOT folded into the message as a Python repr
            # (daisy-content#30): it travels as JSON in the
            # `content-error:` trailer content_api_mcp.core.errors appends at the
            # tools/call boundary, via `_upstream_details`'s
            # `upstream_details` key.
            composed = f"{code}: {plain_message}" if code else plain_message
            # _truncate_body applies here too, not just the raw-body fallback
            # below — a batch/compose failure's `details` can carry a large
            # per-operation error list, and this is the well-formed path,
            # not the degenerate one. Review finding: this was unbounded.
            return (_truncate_body(composed), code, details, plain_message)
        return (_truncate_body(body_text), None, None, None)

    @staticmethod
    def _upstream_details(
        error_code: Any, details: dict | list | None
    ) -> dict[str, Any]:
        """Shape `_extract_upstream_error`'s (error_code, details) pair into
        the payload every status branch in `_request` (above) attaches to
        its raised exception's `details=`. Both keys are omitted, not padded with
        `None`, when the upstream body wasn't a parseable content-api
        envelope that supplied them — so a consumer must branch with
        `details.get("error_code")`, never assume presence (daisy-content#7).
        `error_code` is `Any` for the same reason as in
        _extract_upstream_error: the envelope's `code` field can carry any
        JSON type.
        """
        payload: dict[str, Any] = {}
        if error_code is not None:
            payload["error_code"] = error_code
        if details:
            # `details` (unlike `message`) hasn't been through
            # _truncate_body yet — cap it here too, same reasoning as
            # _extract_upstream_error's own comment on unbounded
            # batch/compose `details` payloads. The "_text" suffix marks
            # it as a string that may not parse: a truncated value ends
            # mid-token with an "…(truncated)" suffix appended.
            payload["upstream_details_text"] = _truncate_body(
                json.dumps(details, default=str)
            )
            # The parsed value itself, for content_api_mcp.core.errors' trailer
            # (daisy-content#30), which applies its own size budget.
            payload["upstream_details"] = details
        return payload

    @staticmethod
    def _record_update_gate_config(details: dict | list | None) -> str | None:
        """Return the config key an ENV_FORBIDDEN envelope's details name, if any.

        Only RecordUpdateHandler's environment switch sends a `config` key
        in its details entry (daisy-content#51); the population gate sends `envVar`
        instead, so a non-empty string here means the single-record update
        gate fired. An entry carrying `envVar` is skipped even if it also
        names a `config`, so a future population-gate envelope that adds
        one can't be misread as the update gate. Returns None for any other
        shape.
        """
        if not isinstance(details, list):
            return None
        for entry in details:
            if isinstance(entry, dict) and "envVar" not in entry:
                config = entry.get("config")
                if isinstance(config, str) and config.strip():
                    return config
        return None

    # ------------------------------------------------------------------
    # Spec-driven dispatch — one entry point every generated tool calls
    # through (see tools.py). Everything below reimplements the
    # request-shaping technique content_api_mcp/client.py's
    # ContentApiClient.call() uses (path substitution, GET query vs POST
    # body, the filePath convenience) but routes through this client's own
    # _request() for multi-site + error-mapping, instead of calling the
    # session directly the way the proxy's call() does.
    # ------------------------------------------------------------------

    def call(
        self,
        site: str,
        tool_entry: dict,
        arguments: dict | None,
        *,
        allow_file_path: bool = False,
        timeout: int = None,
    ) -> Any:
        """Execute one spec-defined tool call against `site`.

        allow_file_path defaults to False (daisy-content#17 review): the raw
        spec entry's own inputSchema always declares "filePath" for
        content_asset_upload, regardless of deployment mode, so gating
        purely on that (the pre-#17 behavior) means any FUTURE caller of
        this method that doesn't route through tools.SpecTool.run
        — a batch executor, a debug/replay helper, a second hand-written
        tool — would silently re-arm the local-file-read vector daisy-content#17 exists
        to close, with no test anywhere catching it. Requiring an explicit
        opt-in here makes that structural rather than contingent on every
        future caller remembering to gate itself upstream. tools.py's
        _dispatch is the one production caller and passes this explicitly,
        keyed off the same CONTENT_STDIO check that controls whether
        "filePath" was stripped from the schema in the first place — so the
        schema a caller sees and the capability this method will actually
        honor can't drift apart.

        timeout (daisy-content#14) defaults to None, which _request resolves
        to BaseAPIClient.DEFAULT_TIMEOUT — the same fallback every call got
        before this parameter existed, so an unset value is unchanged
        behavior, not a new default. content_api_mcp.core.tools._dispatch is the one
        production caller and passes a per-tool value from
        content_api_mcp.core.tools._TOOL_TIMEOUTS when that tool has one.
        """
        arguments = self._resolve_file_path(
            tool_entry, dict(arguments or {}), allow_file_path=allow_file_path
        )
        path, remaining = self._resolve_path(tool_entry["path"], arguments)
        method = tool_entry["method"].upper()
        try:
            if method == "GET":
                params = self._flatten_query(remaining)
                if not params and _is_schema_path(path):
                    return self.get(site, path, timeout=timeout)
                return self._request(
                    "GET", path, site=site, params=params, timeout=timeout
                )
            return self._request(
                method, path, site=site, json_data=remaining, timeout=timeout
            )
        except NotFoundError as e:
            taught = self._teach_unknown_class(site, e)
            if taught is e:
                raise
            raise taught from e

    def _teach_unknown_class(self, site: str, error: NotFoundError) -> NotFoundError:
        """Name the site's exposed class refs on an UNKNOWN_CLASS 404 (daisy-content#21).

        The list comes from the cached schema/site, goes into the message and
        into `details["valid_options"]` (rendered as `valid_options` in the
        `content-error:` trailer). Any other 404, an UNKNOWN_CLASS whose
        upstream sub-code is CLASS_NOT_FOUND, or a schema read that fails
        returns the original error unchanged.
        """
        details = error.details if isinstance(error.details, dict) else {}
        if details.get("error_code") != "UNKNOWN_CLASS":
            return error
        upstream = details.get("upstream_details")
        first = upstream[0] if isinstance(upstream, list) and upstream else None
        sub_code = first.get("code") if isinstance(first, dict) else None
        # CLASS_NOT_FOUND: the ref is registered but its PHP class is missing
        # (a site misconfiguration); the caller's ref is right, and listing
        # the exposed refs (which leave it out) would suggest otherwise.
        if sub_code == "CLASS_NOT_FOUND":
            return error
        refs = self.exposed_class_refs(site)
        if not refs:
            return error
        listed = _truncate_body(", ".join(refs), limit=600)
        message = f"{error.message} Class refs exposed on site {site!r}: {listed}."
        if sub_code is None:
            # Plain unknown ref (no CLASS_ALREADY_MAPPED / CLASS_NOT_MAPPED
            # hint from upstream, whose own sentence then says what to do).
            message += (
                " Use one of these short refs, not a fully-qualified PHP class name."
            )
        return NotFoundError(message, details={**details, "valid_options": refs})

    @staticmethod
    def _resolve_file_path(
        tool_entry: dict, arguments: dict, *, allow_file_path: bool = True
    ) -> dict:
        """Materialize a spec-declared top-level "filePath" into "base64", read locally.

        Gated on the tool entry's own inputSchema declaring a TOP-LEVEL
        "filePath" property (only content_asset_upload's does today, per the
        pinned spec) so this is a no-op for every endpoint that doesn't opt
        in. Does not descend into nested structures — e.g.
        content_compose_page's "assets[]" array — so a hypothetical future
        spec change adding a per-item "filePath" there would not be resolved
        by this method and would need its own handling (review finding
        daisy-content#39/#6: not exploitable today, since no current spec entry declares
        filePath below the top level, but noted so this docstring doesn't
        overclaim coverage it doesn't have). Ported from
        content_api_mcp/client.py's ContentApiClient._resolve_file_path —
        the schema's own description promises this resolution happens
        client-side ("an MCP client resolves filePath locally [...] and
        never sends the path itself over HTTP"), so honoring it here is
        required to keep the pinned schema's documented contract true, not
        an optional add-on.

        allow_file_path (daisy-content#17 review) defaults to True so every
        existing direct caller of this static method — this file's own unit
        tests, the CONTENT_STDIO/local-testing path — keeps working
        unchanged. The actual production gate lives one layer up, in
        ContentClient.call()'s own allow_file_path default of False. When
        it's False here: a caller that still supplies "filePath" is
        REJECTED, never silently popped and ignored, with the same message
        SpecTool.run's own pre-check gives — so this method holds the same
        guarantee even if something one day calls it directly with
        allow_file_path=False and bypasses that pre-check. The
        base64-required message also stops naming "filePath" as an
        alternative in that case (review finding: it previously pointed a
        caller straight back at the argument this exact call had just
        disallowed).
        """
        schema_declares_file_path = "filePath" in tool_entry.get("inputSchema", {}).get(
            "properties", {}
        )
        if not schema_declares_file_path:
            return arguments

        if not allow_file_path:
            if "filePath" in arguments:
                raise MCPError(
                    "filePath is not available in this deployment — pass the file "
                    'content directly via "base64" instead.',
                    status_code=400,
                )
            if arguments.get("base64") is None:
                raise MCPError('Provide "base64".', status_code=400)
            return arguments

        file_path = arguments.pop("filePath", None)
        has_base64 = arguments.get("base64") is not None

        if file_path is not None and has_base64:
            raise MCPError(
                'Provide either "filePath" or "base64", not both.', status_code=400
            )
        if file_path is None:
            if not has_base64:
                raise MCPError(
                    'Provide one of "filePath" or "base64".', status_code=400
                )
            return arguments

        path = Path(file_path).expanduser()
        if not path.is_file():
            raise MCPError(
                f'filePath "{file_path}" does not exist or is not a file.',
                status_code=400,
            )
        if _looks_sensitive(path):
            raise MCPError(
                f'filePath "{file_path}" looks like a credential/secret file, not an '
                "upload asset — refusing to read it. If this is a legitimate asset, "
                'rename it or pass its content via "base64" instead.',
                status_code=400,
            )
        try:
            size = path.stat().st_size
        except OSError as e:
            raise MCPError(
                f'filePath "{file_path}" could not be read: {e}', status_code=400
            ) from e
        if size > MAX_FILE_PATH_BYTES:
            raise MCPError(
                f'filePath "{file_path}" is {size} bytes, over the '
                f"{MAX_FILE_PATH_BYTES}-byte limit for filePath uploads — pass "
                '"base64" directly if you need to upload something larger.',
                status_code=400,
            )
        try:
            raw = path.read_bytes()
        except OSError as e:
            raise MCPError(
                f'filePath "{file_path}" could not be read: {e}', status_code=400
            ) from e
        arguments["base64"] = base64.b64encode(raw).decode("ascii")
        return arguments

    @staticmethod
    def _resolve_path(path_template: str, arguments: dict) -> tuple[str, dict]:
        """Substitute {param} segments from arguments; return (path, leftover args).

        Consumed keys are popped so they never leak into the query string or
        JSON body. A single regex pass over the pristine template (never an
        already-substituted string) so one argument's value can't contain
        another argument's `{token}` text and get re-substituted. Unlike the
        proxy's `quote(..., safe=":")` percent-encoding, this validates each
        segment through the strict `_clean_path_segment` allowlist instead —
        same discipline as daisy-media's `_clean_id` on every value entering
        a URL path segment, per daisy-workspace#9.
        """
        remaining = dict(arguments)
        missing: list[str] = []

        def substitute(match: re.Match) -> str:
            key = match.group(1)
            if key not in remaining:
                missing.append(key)
                return match.group(0)
            value = remaining.pop(key)
            try:
                return _clean_path_segment(value)
            except ValueError as e:
                # Re-raised as MCPError(400), not left as a bare ValueError:
                # a caller-supplied classRef/id/action containing an illegal
                # character is a client input error, not an unhandled
                # exception — same "clean 400, not a 500" contract every
                # other validation failure in this method already gives.
                raise MCPError(
                    f"invalid value for path argument {key!r}: {e}", status_code=400
                ) from e

        path = _PATH_PARAM_RE.sub(substitute, path_template)
        if missing:
            raise MCPError(
                f"missing required argument(s) for path template: {', '.join(missing)}",
                status_code=400,
            )
        return path, remaining

    @staticmethod
    def _flatten_query(arguments: dict) -> dict:
        """Flatten the `filters` object into Field / Field__Modifier query params.

        Other GET args (sort, limit, offset, _stage) pass through as-is and
        always take precedence over a same-named key inside `filters`. Values
        go through `_query_value`: bools become 1/0, a list becomes the
        comma-separated IN list the module parses, and a nested object is
        refused.
        """
        filters = arguments.get("filters")
        params: dict = dict(filters) if isinstance(filters, dict) else {}
        for key, value in arguments.items():
            if key == "filters" or value is None:
                continue
            params[key] = value
        return {key: _query_value(key, value) for key, value in params.items()}
