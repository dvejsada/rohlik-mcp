"""Tests for per-account credential handling and client caching.

These exercise the registry in :mod:`rohlik_mcp.server` with a stub API client,
so no network access or real credentials are involved.
"""

import asyncio

import pytest
from fastmcp.exceptions import ToolError
from rohlik_api import InvalidCredentialsError

from rohlik_mcp import server
from rohlik_mcp.config import PASSWORD_HEADER, USERNAME_HEADER, ServerConfig


class FakeAPI:
    """Stand-in for RohlikAPI that records logins and closes."""

    # Credentials whose login should fail, shared across instances.
    invalid: set[tuple[str, str]] = set()

    def __init__(self, username, password, base_url, timeout, auto_login):
        self.username = username
        self.password = password
        self.base_url = base_url
        self.timeout = timeout
        self.auto_login = auto_login
        self.is_logged_in = False
        self.login_calls = 0
        self.closed = False

    async def login(self):
        self.login_calls += 1
        # Yield control, so an unsynchronised caller would interleave here.
        await asyncio.sleep(0.01)
        if (self.username, self.password) in FakeAPI.invalid:
            raise InvalidCredentialsError("Invalid credentials")
        self.is_logged_in = True
        return {}

    async def close(self):
        self.closed = True
        self.is_logged_in = False


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """Give each test an empty registry, a stub API client and no real headers."""
    monkeypatch.setattr(server, "RohlikAPI", FakeAPI)
    monkeypatch.setattr(server, "_clients", server.OrderedDict())
    monkeypatch.setattr(server, "_pending_closes", set())
    monkeypatch.setattr(FakeAPI, "invalid", set())
    server.get_config.cache_clear()
    yield server._clients
    server.get_config.cache_clear()


@pytest.fixture
def headers(monkeypatch):
    """Control what ``get_http_headers`` returns for the 'current request'."""
    current: dict[str, str] = {}
    monkeypatch.setattr(server, "get_http_headers", lambda: dict(current))

    def _set(username=None, password=None):
        current.clear()
        if username is not None:
            current[USERNAME_HEADER] = username
        if password is not None:
            current[PASSWORD_HEADER] = password

    return _set


async def _drain_background_closes():
    """Wait for the fire-and-forget close tasks eviction schedules."""
    while server._pending_closes:
        await asyncio.gather(*list(server._pending_closes), return_exceptions=True)


class TestReadCredentials:
    def test_reads_both_headers(self, headers):
        headers("user@example.com", "secret")

        credentials = server.read_credentials()

        assert credentials.username == "user@example.com"
        assert credentials.password == "secret"

    def test_strips_whitespace_around_the_username(self, headers):
        headers("  user@example.com  ", "  secret  ")

        credentials = server.read_credentials()

        assert credentials.username == "user@example.com"
        # The password is taken verbatim - whitespace may be part of it.
        assert credentials.password == "  secret  "

    @pytest.mark.parametrize(
        ("username", "password"),
        [(None, "secret"), ("user@example.com", None), (None, None), ("", "secret")],
    )
    def test_missing_credentials_raise_an_actionable_error(self, headers, username, password):
        headers(username, password)

        with pytest.raises(ToolError, match=USERNAME_HEADER):
            server.read_credentials()

    def test_no_http_request_reports_missing_credentials(self, monkeypatch):
        """Over stdio there are no headers, so there is nothing to fall back to."""
        monkeypatch.setattr(server, "get_http_headers", dict)

        with pytest.raises(ToolError, match="No Rohlik.cz credentials"):
            server.read_credentials()


class TestGetClient:
    async def test_logs_in_once_and_caches_the_client(self, headers, registry):
        headers("user@example.com", "secret")

        first = await server.get_client()
        second = await server.get_client()

        assert first is second
        assert first.login_calls == 1
        assert len(registry) == 1

    async def test_passes_credentials_and_config_to_the_client(self, headers, monkeypatch):
        monkeypatch.setenv("ROHLIK_BASE_URL", "https://example.test")
        monkeypatch.setenv("ROHLIK_TIMEOUT", "12")
        server.get_config.cache_clear()
        headers("user@example.com", "secret")

        client = await server.get_client()

        assert (client.username, client.password) == ("user@example.com", "secret")
        assert client.base_url == "https://example.test"
        assert client.timeout == 12.0
        assert client.auto_login is False

    async def test_concurrent_first_calls_share_one_login(self, headers, registry):
        """A fanned-out tool must not fire one login per in-flight coroutine."""
        headers("user@example.com", "secret")

        clients = await asyncio.gather(*(server.get_client() for _ in range(10)))

        assert len({id(c) for c in clients}) == 1
        assert clients[0].login_calls == 1

    async def test_different_users_get_separate_clients(self, headers, registry):
        headers("a@example.com", "secret")
        first = await server.get_client()
        headers("b@example.com", "secret")
        second = await server.get_client()

        assert first is not second
        assert len(registry) == 2

    async def test_a_wrong_password_does_not_reuse_the_session(self, headers, registry):
        """Keying on the username alone would hand out an authenticated session."""
        headers("user@example.com", "secret")
        legitimate = await server.get_client()

        headers("user@example.com", "guess")
        other = await server.get_client()

        assert other is not legitimate
        assert len(registry) == 2

    async def test_a_changed_password_starts_a_fresh_session(self, headers, registry):
        headers("user@example.com", "old")
        await server.get_client()

        headers("user@example.com", "new")
        client = await server.get_client()

        assert client.password == "new"
        assert client.login_calls == 1

    async def test_login_failure_raises_and_drops_the_entry(self, headers, registry):
        FakeAPI.invalid.add(("user@example.com", "wrong"))
        headers("user@example.com", "wrong")

        with pytest.raises(ToolError, match="Invalid credentials"):
            await server.get_client()

        # Nothing cached, so correcting the password is not shadowed by a
        # client that can never log in.
        assert not registry
        await _drain_background_closes()

    async def test_missing_credentials_create_no_client(self, headers, registry):
        headers(None, None)

        with pytest.raises(ToolError):
            await server.get_client()

        assert not registry


class TestEviction:
    async def test_lru_eviction_closes_the_oldest_idle_client(self, headers, monkeypatch, registry):
        monkeypatch.setenv("ROHLIK_MAX_CLIENTS", "2")
        monkeypatch.setenv("ROHLIK_TIMEOUT", "0")
        server.get_config.cache_clear()

        headers("a@example.com", "secret")
        oldest = await server.get_client()
        headers("b@example.com", "secret")
        await server.get_client()
        # Touching 'a' makes 'b' the least recently used.
        headers("a@example.com", "secret")
        await server.get_client()
        headers("c@example.com", "secret")
        await server.get_client()
        await _drain_background_closes()

        assert len(registry) == 2
        assert not oldest.closed
        assert [e.client.username for e in registry.values()] == ["a@example.com", "c@example.com"]

    async def test_in_use_clients_are_not_evicted(self, headers, monkeypatch, registry):
        """Closing a session mid-request would fail the call, so the cap yields."""
        monkeypatch.setenv("ROHLIK_MAX_CLIENTS", "1")
        monkeypatch.setenv("ROHLIK_TIMEOUT", "30")
        server.get_config.cache_clear()

        headers("a@example.com", "secret")
        busy = await server.get_client()
        headers("b@example.com", "secret")
        await server.get_client()
        await _drain_background_closes()

        assert len(registry) == 2
        assert not busy.closed

    async def test_reaper_closes_idle_clients_only(self, headers, registry):
        headers("idle@example.com", "secret")
        idle = await server.get_client()
        headers("fresh@example.com", "secret")
        fresh = await server.get_client()
        # Age the first entry past the TTL.
        next(iter(registry.values())).last_used -= 120

        closed = await server._reap_once(ServerConfig(client_ttl=60.0))

        assert closed == 1
        assert idle.closed
        assert not fresh.closed
        assert [e.client.username for e in registry.values()] == ["fresh@example.com"]

    async def test_close_all_clients_empties_the_registry(self, headers, registry):
        headers("a@example.com", "secret")
        first = await server.get_client()
        headers("b@example.com", "secret")
        second = await server.get_client()

        await server._close_all_clients()

        assert not registry
        assert first.closed and second.closed
