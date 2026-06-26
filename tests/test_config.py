"""Tests for configuration loading."""

import pytest

from rohlik_mcp.config import Config


def test_from_env_requires_credentials(monkeypatch):
    monkeypatch.delenv("ROHLIK_USERNAME", raising=False)
    monkeypatch.delenv("ROHLIK_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="ROHLIK_USERNAME"):
        Config.from_env()


def test_from_env_reads_credentials(monkeypatch):
    monkeypatch.setenv("ROHLIK_USERNAME", "user@example.com")
    monkeypatch.setenv("ROHLIK_PASSWORD", "secret")
    monkeypatch.delenv("ROHLIK_BASE_URL", raising=False)
    monkeypatch.delenv("ROHLIK_TIMEOUT", raising=False)

    config = Config.from_env()

    assert config.username == "user@example.com"
    assert config.password == "secret"
    assert config.base_url == "https://www.rohlik.cz"
    assert config.timeout == 30.0


def test_from_env_reads_optional_overrides(monkeypatch):
    monkeypatch.setenv("ROHLIK_USERNAME", "user@example.com")
    monkeypatch.setenv("ROHLIK_PASSWORD", "secret")
    monkeypatch.setenv("ROHLIK_BASE_URL", "https://example.test")
    monkeypatch.setenv("ROHLIK_TIMEOUT", "10")

    config = Config.from_env()

    assert config.base_url == "https://example.test"
    assert config.timeout == 10.0
