"""Token-resolution tests for ContentApiSettings.

Covers the CONTENT_API_TOKEN_FILE fallback (#2): a GUI-launched MCP host may
not inherit the shell environment CONTENT_API_TOKEN relies on, so the file
path must resolve to an equivalent token independent of process env.
"""

import pytest
from pydantic import ValidationError

from content_api_mcp.settings import ContentApiSettings

BASE_URL = "https://example.test/content-api/v1"


@pytest.fixture(autouse=True)
def base_env(monkeypatch):
    monkeypatch.setenv("CONTENT_API_BASE_URL", BASE_URL)
    monkeypatch.delenv("CONTENT_API_TOKEN", raising=False)
    monkeypatch.delenv("CONTENT_API_TOKEN_FILE", raising=False)


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
