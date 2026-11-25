"""Tests for configuration loading."""

import pytest

from rohlik_mcp.config import (
    MIN_CLIENT_TTL,
    Credentials,
    InvalidSiteError,
    ServerConfig,
    resolve_shop,
)

_OPTIONAL_VARS = (
    "ROHLIK_SITE",
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

    assert config.site == "cz"
    assert config.base_url_override is None
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

    assert config.base_url_override == "https://example.test"
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


def test_from_env_reads_the_default_site(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ROHLIK_SITE", " DE ")

    assert ServerConfig.from_env().site == "de"


def test_from_env_rejects_an_unknown_site(monkeypatch):
    """A typo must stop the server at startup, not fail every request later."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("ROHLIK_SITE", "xx")

    with pytest.raises(InvalidSiteError, match="ROHLIK_SITE"):
        ServerConfig.from_env()


class TestResolveShop:
    def test_defaults_to_the_configured_site(self):
        shop = resolve_shop(ServerConfig(site="at"), None)

        assert (shop.code, shop.base_url, shop.currency) == ("at", "https://www.gurkerl.at", "EUR")

    @pytest.mark.parametrize("requested", ["", "   "])
    def test_blank_header_means_the_default(self, requested):
        assert resolve_shop(ServerConfig(), requested).code == "cz"

    def test_header_picks_another_shop(self):
        shop = resolve_shop(ServerConfig(), " HU ")

        assert (shop.name, shop.base_url, shop.currency) == (
            "Kifli.hu",
            "https://www.kifli.hu",
            "HUF",
        )

    def test_unknown_header_lists_the_valid_codes(self):
        with pytest.raises(InvalidSiteError, match="cz, de, at, hu, ro"):
            resolve_shop(ServerConfig(), "uk")

    def test_pinned_url_replaces_the_default_shops_url(self):
        config = ServerConfig(site="de", base_url_override="https://proxy.test")

        for requested in (None, "de"):
            shop = resolve_shop(config, requested)
            assert (shop.code, shop.base_url, shop.currency) == ("de", "https://proxy.test", "EUR")

    def test_pinned_url_refuses_another_shop(self):
        """The pin cannot be honoured for another shop, so say so rather than ignore it."""
        config = ServerConfig(base_url_override="https://proxy.test")

        with pytest.raises(InvalidSiteError, match="ROHLIK_BASE_URL"):
            resolve_shop(config, "de")


def test_empty_site_means_the_default(monkeypatch):
    """A blank ``ROHLIK_SITE=`` line in an env file must not stop the server."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("ROHLIK_SITE", "  ")

    assert ServerConfig.from_env().site == "cz"


@pytest.mark.parametrize("url", ["https://www.knuspr.de", "HTTPS://WWW.KNUSPR.DE/"])
def test_a_shops_own_url_selects_that_shop_instead_of_pinning(monkeypatch, url):
    """Before ROHLIK_SITE, ROHLIK_BASE_URL was how a server was set up for a sister shop."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("ROHLIK_BASE_URL", url)

    config = ServerConfig.from_env()

    assert (config.site, config.base_url_override) == ("de", None)
    assert resolve_shop(config, None).currency == "EUR"
    assert resolve_shop(config, "cz").base_url == "https://www.rohlik.cz"


def test_matching_site_and_shop_url_agree(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ROHLIK_SITE", "de")
    monkeypatch.setenv("ROHLIK_BASE_URL", "https://www.knuspr.de")

    assert ServerConfig.from_env().site == "de"


def test_conflicting_site_and_shop_url_fail_at_startup(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ROHLIK_SITE", "cz")
    monkeypatch.setenv("ROHLIK_BASE_URL", "https://www.knuspr.de")

    with pytest.raises(InvalidSiteError, match="Knuspr.de"):
        ServerConfig.from_env()


def test_other_url_is_a_pin(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ROHLIK_SITE", "at")
    monkeypatch.setenv("ROHLIK_BASE_URL", " https://proxy.test ")

    config = ServerConfig.from_env()

    assert (config.site, config.base_url_override) == ("at", "https://proxy.test")
