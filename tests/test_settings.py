"""Token-resolution tests for ContentApiSettings.

Covers the CONTENT_API_TOKEN_FILE fallback (#2): a GUI-launched MCP host may
not inherit the shell environment CONTENT_API_TOKEN relies on, so the file
path must resolve to an equivalent token independent of process env. Also
covers current_token() (#23): re-reading the file per call rather than once
at construction, so a re-minted token doesn't need a host restart.
"""

import pytest
from pydantic import ValidationError

from content_api_mcp._base.errors import AuthenticationError
from content_api_mcp.settings import ContentApiSettings

BASE_URL = "https://example.test/content-api/v1"


@pytest.fixture(autouse=True)
def base_env(monkeypatch):
    monkeypatch.setenv("CONTENT_API_BASE_URL", BASE_URL)
    monkeypatch.delenv("CONTENT_API_TOKEN", raising=False)
    monkeypatch.delenv("CONTENT_API_TOKEN_FILE", raising=False)
    monkeypatch.delenv("CONTENT_API_CA_FILE", raising=False)


def test_token_env_var_used_directly(monkeypatch):
    monkeypatch.setenv("CONTENT_API_TOKEN", "tok_abc123")
    settings = ContentApiSettings()
    assert settings.token == "tok_abc123"


def test_token_file_used_when_token_env_absent(monkeypatch, tmp_path):
    token_file = tmp_path / "site.token"
    token_file.write_text("tok_from_file\n")
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(token_file))

    settings = ContentApiSettings()

    assert settings.token == "tok_from_file"


def test_token_env_var_takes_precedence_over_file(monkeypatch, tmp_path):
    token_file = tmp_path / "site.token"
    token_file.write_text("tok_from_file")
    monkeypatch.setenv("CONTENT_API_TOKEN", "tok_from_env")
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(token_file))

    settings = ContentApiSettings()

    assert settings.token == "tok_from_env"


def test_token_file_is_cleared_when_token_env_var_wins(monkeypatch, tmp_path):
    # #23 review follow-up (critical, caught before merge): construction
    # picking CONTENT_API_TOKEN must also clear token_file, not just skip
    # reading it — current_token() gates on token_file alone, so leaving it
    # populated meant construction resolved the env token but every actual
    # request re-read and authenticated with the file instead. This asserts
    # the fix at the field level; the client.py-level proof is
    # test_call_both_token_and_token_file_configured_uses_token_consistently
    # in tests/test_client.py.
    token_file = tmp_path / "site.token"
    token_file.write_text("tok_from_file")
    monkeypatch.setenv("CONTENT_API_TOKEN", "tok_from_env")
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(token_file))

    settings = ContentApiSettings()

    assert settings.token_file is None
    assert settings.current_token() == "tok_from_env"


def test_token_file_missing_raises_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(tmp_path / "does-not-exist.token"))

    with pytest.raises(ValidationError, match="CONTENT_API_TOKEN_FILE"):
        ContentApiSettings()


def test_neither_token_nor_token_file_raises_clear_error():
    with pytest.raises(ValidationError, match="CONTENT_API_TOKEN"):
        ContentApiSettings()


def test_token_file_invalid_encoding_raises_clear_error(monkeypatch, tmp_path):
    token_file = tmp_path / "site.token"
    token_file.write_bytes(b"\xff\xfe\x00\xff")
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(token_file))

    with pytest.raises(ValidationError, match="CONTENT_API_TOKEN_FILE"):
        ContentApiSettings()


# --- current_token() (#23) --------------------------------------------------


def test_current_token_returns_the_static_token_when_no_file_configured(monkeypatch):
    monkeypatch.setenv("CONTENT_API_TOKEN", "tok_abc123")
    settings = ContentApiSettings()

    assert settings.current_token() == "tok_abc123"


def test_current_token_re_reads_the_file_on_every_call(monkeypatch, tmp_path):
    token_file = tmp_path / "site.token"
    token_file.write_text("tok_original")
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(token_file))
    settings = ContentApiSettings()

    assert settings.current_token() == "tok_original"

    # Simulate a re-mint mid-session, with no process restart in between —
    # the whole point of #23.
    token_file.write_text("tok_rotated")

    assert settings.current_token() == "tok_rotated"


def test_current_token_ignores_a_stale_startup_value_once_the_file_changes(monkeypatch, tmp_path):
    # Regression: the "before" validator's one-time CONTENT_API_TOKEN
    # resolution (self.token) must never be what current_token() actually
    # returns once token_file is set — if it silently fell back to the
    # cached construction-time value, this test wouldn't distinguish that
    # bug from the correct re-read behavior above.
    token_file = tmp_path / "site.token"
    token_file.write_text("tok_at_startup")
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(token_file))
    settings = ContentApiSettings()
    startup_token = settings.token

    token_file.write_text("tok_after_rotation")

    assert settings.current_token() == "tok_after_rotation"
    assert settings.current_token() != startup_token


def test_current_token_raises_authentication_error_when_file_removed_after_startup(
    monkeypatch, tmp_path
):
    token_file = tmp_path / "site.token"
    token_file.write_text("tok_original")
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(token_file))
    settings = ContentApiSettings()

    token_file.unlink()

    with pytest.raises(AuthenticationError, match="CONTENT_API_TOKEN_FILE"):
        settings.current_token()


def test_current_token_raises_authentication_error_on_empty_file(monkeypatch, tmp_path):
    # A token-rotation script can leave the file transiently empty between
    # truncating and rewriting it — this must fail loudly, not silently
    # send an empty header.
    token_file = tmp_path / "site.token"
    token_file.write_text("tok_original")
    monkeypatch.setenv("CONTENT_API_TOKEN_FILE", str(token_file))
    settings = ContentApiSettings()

    token_file.write_text("")

    with pytest.raises(AuthenticationError, match="empty"):
        settings.current_token()


def test_missing_base_url_and_token_reports_both_as_one_error(monkeypatch):
    # Regression: resolving the token via a "before" validator (rather than
    # an "after" one) must not narrow this combined error down to whichever
    # field happened to fail first — pydantic skips "after" validators
    # whenever any field already failed, which used to hide the missing
    # CONTENT_API_TOKEN error whenever CONTENT_API_BASE_URL was also unset.
    monkeypatch.delenv("CONTENT_API_BASE_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        ContentApiSettings()

    missing_fields = {error["loc"][0] for error in exc_info.value.errors()}
    assert missing_fields == {"CONTENT_API_BASE_URL", "CONTENT_API_TOKEN"}


def test_ca_file_defaults_to_none(monkeypatch):
    monkeypatch.setenv("CONTENT_API_TOKEN", "tok_abc123")

    assert ContentApiSettings().ca_file is None


def test_ca_file_expands_home_and_stores_the_resolved_path(monkeypatch, tmp_path):
    # The stored value is the expanded absolute path (#32) — client.py hands
    # it straight to requests' `verify`, which does no expansion of its own.
    (tmp_path / "rootCA.pem").write_text("dummy pem")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CONTENT_API_TOKEN", "tok_abc123")
    monkeypatch.setenv("CONTENT_API_CA_FILE", "~/rootCA.pem")

    settings = ContentApiSettings()

    assert settings.ca_file == str(tmp_path / "rootCA.pem")


def test_ca_file_missing_raises_clear_error_at_construction(monkeypatch, tmp_path):
    # Fail at startup naming the setting, not per-request deep inside
    # requests once the first call happens to run.
    monkeypatch.setenv("CONTENT_API_TOKEN", "tok_abc123")
    monkeypatch.setenv("CONTENT_API_CA_FILE", str(tmp_path / "does-not-exist.pem"))

    with pytest.raises(ValidationError, match="CONTENT_API_CA_FILE"):
        ContentApiSettings()
