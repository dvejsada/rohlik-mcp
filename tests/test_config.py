"""Tests for configuration loading."""

from rohlik_mcp.config import MIN_CLIENT_TTL, Credentials, ServerConfig

_OPTIONAL_VARS = (
    "ROHLIK_BASE_URL",
    "ROHLIK_TIMEOUT",
    "ROHLIK_MCP_HOST",
    "ROHLIK_MCP_PORT",
    "ROHLIK_MCP_PATH",
    "ROHLIK_CLIENT_TTL",
    "ROHLIK_MAX_CLIENTS",
)


def _clear_env(monkeypatch):
    for name in _OPTIONAL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_from_env_needs_no_credentials(monkeypatch):
    """Credentials arrive per request, so the server must start without them."""
    _clear_env(monkeypatch)
    monkeypatch.delenv("ROHLIK_USERNAME", raising=False)
    monkeypatch.delenv("ROHLIK_PASSWORD", raising=False)

    config = ServerConfig.from_env()

    assert config.base_url == "https://www.rohlik.cz"
    assert config.timeout == 30.0
    assert config.host == "0.0.0.0"
    assert config.port == 8000
    assert config.path == "/mcp/"
    assert config.client_ttl == 900.0
    assert config.max_clients == 50


def test_from_env_reads_optional_overrides(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ROHLIK_BASE_URL", "https://example.test")
    monkeypatch.setenv("ROHLIK_TIMEOUT", "10")
    monkeypatch.setenv("ROHLIK_MCP_PORT", "9001")
    monkeypatch.setenv("ROHLIK_CLIENT_TTL", "300")
    monkeypatch.setenv("ROHLIK_MAX_CLIENTS", "5")

    config = ServerConfig.from_env()

    assert config.base_url == "https://example.test"
    assert config.timeout == 10.0
    assert config.port == 9001
    assert config.client_ttl == 300.0
    assert config.max_clients == 5


def test_client_ttl_is_floored(monkeypatch):
    """A tiny TTL would log every account out between one tool call and the next."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("ROHLIK_CLIENT_TTL", "1")

    assert ServerConfig.from_env().client_ttl == MIN_CLIENT_TTL


def test_max_clients_is_at_least_one(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ROHLIK_MAX_CLIENTS", "0")

    assert ServerConfig.from_env().max_clients == 1


class TestCredentials:
    def test_cache_key_covers_password(self):
        """Keying on the username alone would let any password reuse a session."""
        base = "https://www.rohlik.cz"
        right = Credentials("user@example.com", "secret").cache_key(base)
        wrong = Credentials("user@example.com", "guess").cache_key(base)

        assert right != wrong

    def test_cache_key_covers_base_url(self):
        credentials = Credentials("user@example.com", "secret")

        assert credentials.cache_key("https://a.test") != credentials.cache_key("https://b.test")

    def test_cache_key_is_stable_and_opaque(self):
        credentials = Credentials("user@example.com", "secret")
        key = credentials.cache_key("https://www.rohlik.cz")

        assert key == credentials.cache_key("https://www.rohlik.cz")
        assert "secret" not in key
        assert "user@example.com" not in key

    def test_repr_redacts_the_password(self):
        assert "secret" not in repr(Credentials("user@example.com", "secret"))
