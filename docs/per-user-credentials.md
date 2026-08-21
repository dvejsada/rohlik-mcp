# Per-user credentials & session lifecycle — investigation

Status: **implemented in 0.5.0.** Kept as the rationale behind the design; see
the README for how to use it. Environment-based credentials were dropped
outright, with no backwards-compatible fallback.
Scope: (1) can the server accept per-user credentials instead of env vars, and be
driven from LibreChat user vars? (2) do successive tool calls from one user cause
a login/logout per call?

---

## 1. How it works today

### Credentials

* `Config.from_env()` (`rohlik_mcp/config.py`) requires `ROHLIK_USERNAME` /
  `ROHLIK_PASSWORD` and raises if either is missing.
* `__main__.main()` calls `Config.from_env()` *before* `mcp.run(...)`, so the
  process refuses to start without credentials.
* `server.get_client()` builds **one process-global** `RohlikAPI` lazily and caches
  it in the module-level `_client`. All 26 tools call `get_client()`.

Consequence: the server is single-account by construction. Every MCP client that
can reach the endpoint shops in the owner's cart.

### Session / login lifecycle

Tracing `rohlik-api` 0.2.0:

* `RohlikAPI` is constructed with `auto_login=False`, so nothing happens at build
  time.
* Every service method starts with `await self._ensure_logged_in()`
  (`services/base.py`), which delegates to `AuthManager.ensure_logged_in()`.
* `AuthManager.login()` **short-circuits when `self._is_logged_in` is True** and
  returns the cached login response without an HTTP call.
* The session cookie lives in the `aiohttp.ClientSession` cookie jar owned by
  `HttpClient`, which is created once and reused.
* `logout()` is only reached from `RohlikAPI.close()`, which the MCP server only
  calls from `_close_client()` in the `_lifespan` `finally` block — i.e. **at
  server shutdown**.
* On HTTP 401, `HttpClient._request` invokes the registered `on_unauthorized`
  handler (`AuthManager.relogin`) once, then retries the request. That path is
  serialised by `_reauth_lock` + `_reauth_generation`, so a burst of concurrent
  401s produces a single re-login.

### Answer to question 2

**No — there is no login/logout per tool call.** For the whole life of the
process there is exactly one login (plus a re-login whenever Rohlik expires the
session and returns 401) and one logout, at shutdown. Ten tool calls in a row
cost one login, not ten.

### One real bug this investigation surfaced

`AuthManager.ensure_logged_in()` has **no lock**. Two tools fan out concurrently
on a cold client:

* `get_account_overview` — `asyncio.gather` over 10 coroutines
* `_gather_by_id` — up to `_BULK_CONCURRENCY = 8` coroutines

On a not-yet-logged-in client all of them observe `_is_logged_in == False` and
each fires its own `POST /login`. Today this can happen at most once per process
lifetime, so it is nearly invisible. **Under per-user clients it happens on every
new user's first tool call**, and `get_account_overview` is exactly the kind of
call an assistant opens a conversation with. Any per-user work should fix this —
either upstream in `AuthManager` (an `asyncio.Lock` around the login), or in this
server by awaiting a single `client.login()` under a per-key lock before the
client is handed to a tool.

---

## 2. Per-user credentials

### Where credentials would come from

FastMCP 3.x exposes the live request headers to tool code:

```python
from fastmcp.server.dependencies import get_http_headers

headers = get_http_headers()   # lowercased dict; never raises
```

It returns `{}` when there is no HTTP request (stdio transport), and it strips
`authorization` unless you pass `include={"authorization"}`. Being a contextvar
read, it works inside `asyncio.gather`-ed child tasks too.

Three candidate transports for the credentials:

| Option | Shape | Verdict |
| --- | --- | --- |
| **A. Custom headers** | `X-Rohlik-Username`, `X-Rohlik-Password` | **Recommended.** Maps 1:1 onto LibreChat `customUserVars`; leaves `Authorization` free for the existing `ROHLIK_MCP_AUTH_TOKEN` gate. |
| B. HTTP Basic | `Authorization: Basic base64(u:p)` | Needs `include={"authorization"}` and collides with the bearer-token gate. |
| C. Token → credential registry | `StaticTokenVerifier(tokens={...})` with credentials in the claims | Keeps passwords off every request, but the operator must provision each user server-side — users cannot self-serve, which defeats the point of user vars. |

### Client registry

`get_client()` becomes a lookup in a registry keyed by the credentials rather
than a single global:

```python
key = sha256(f"{username}\0{password}\0{base_url}".encode()).hexdigest()
```

Design points, in rough order of importance:

1. **Key on username *and* password, never username alone.** Keying on username
   would hand a live authenticated session to anyone who supplies that username
   with *any* password — an authentication bypass. Hashing also keeps plaintext
   secrets out of dict keys, logs and reprs.
2. **Idle TTL + reaper.** A background task started in `_lifespan` closes entries
   untouched for longer than `ROHLIK_CLIENT_TTL` (suggest ~15 min, matching
   LibreChat's own idle disconnect). `RohlikAPI.close()` already logs out before
   closing the socket, so eviction releases the Rohlik-side session politely; it
   swallows logout errors, so a reaper call is safe.
3. **Bounded size.** Cap the registry (`ROHLIK_MAX_CLIENTS`) with LRU eviction.
   Each entry owns an `aiohttp.ClientSession`, i.e. a connection pool and a
   cookie jar.
4. **Per-key first-login lock**, per the bug above.
5. `_lifespan` shutdown closes every entry, not just one.

### Blast radius in this repo

Small, if `get_client()` keeps its current no-argument signature and reads the
headers itself: **none of the 26 tool bodies change.** The three sites that
capture the client once per invocation — `get_account_overview`,
`remove_from_cart`, `get_product_detail` — all resolve inside the request, so
headers are available there too.

What does change:

* `rohlik_mcp/config.py` — split into a `ServerConfig` (host/port/path/base URL/
  timeout/auth token) that never requires credentials, and a per-request
  `Credentials`. `Config.from_env()` currently raising on missing credentials is
  what stops the process from booting.
* `rohlik_mcp/__main__.py` — must stop treating missing credentials as fatal.
* `tests/test_config.py` — asserts the current raise-on-missing behaviour.
* `tests/test_server.py` — the `mock_client` fixture monkeypatches
  `server.get_client`; it keeps working as long as the signature stays no-arg.
* README / `.env.example` / `docker-compose.yml` — credentials become optional.
* Version bump to 0.5.0 (breaking configuration change).

### Env fallback — a deliberate decision, not a default

If header credentials are absent and `ROHLIK_USERNAME`/`ROHLIK_PASSWORD` are set,
falling back silently means a misconfigured LibreChat user shops in the **owner's**
account without any error. Recommendation: make the fallback opt-in
(`ROHLIK_ALLOW_ENV_CREDENTIALS=1`), and otherwise return a clear
`{"error": "No Rohlik credentials supplied — set them in the MCP server settings"}`.
Keeping the fallback available matters for stdio/Claude Desktop use, where
`get_http_headers()` returns `{}`.

### Security

Deployment context: **both containers run on one local Docker network** (LibreChat
→ `http://rohlik-mcp:8000/mcp/`), which changes the weighting here a lot.

* **TLS is not required.** The password rides in a header on every request, but
  that request never leaves the Docker bridge. Terminating TLS between two
  containers on the same host buys little. This *only* holds while the endpoint
  stays off the host and off the internet — see the next two points.
* **Stop publishing the port.** `docker-compose.yml` currently maps
  `"${ROHLIK_MCP_PORT:-8000}:${ROHLIK_MCP_PORT:-8000}"`. Containers on a shared
  network reach each other by service name, so the mapping is unnecessary and it
  is what turns "passwords on a private bridge" into "passwords on
  `localhost:8000`, reachable by anything on the host". Drop `ports:` (use
  `expose:` if you want it documented) and put both services on one named
  network.
* **Keep `ROHLIK_MCP_AUTH_TOKEN` on.** Every *other* container on that network
  can also reach the endpoint, and without the bearer gate any of them can call
  `add_to_cart` with credentials of their choosing. It composes fine with
  per-user headers — LibreChat sends a static bearer from `${ENV}` alongside the
  per-user `X-Rohlik-*` from `customUserVars`, in the same `headers:` block.
* **Never log resolved headers.** `rohlik_api.helpers.mask_data` masks the login
  response; nothing guards request headers. Bearing in mind `logging.basicConfig(
  level=logging.INFO)` in `__main__.py`, a stray debug log would put every user's
  password into `docker logs`.
* **Drop the credential env vars from the compose file** once per-user mode
  lands. They would otherwise sit in `.env` as a silent fallback into the owner's
  account (see above), and `docker inspect` exposes them to anyone on the host.
* Rohlik sees N logins from **one** egress IP — the host's — instead of one. TTL
  eviction rather than per-call logout keeps that rate low, which matters more
  here than it would with users spread across addresses.

---

## 3. LibreChat integration

LibreChat's `customUserVars` is exactly the mechanism this needs: the admin
declares the variables, each user fills them in from the MCP settings panel, and
LibreChat substitutes `{{VAR}}` into `headers`, `url` and `args` per user at
runtime, encrypted at rest.

```yaml
mcpServers:
  rohlik:
    type: streamable-http
    url: "http://rohlik-mcp:8000/mcp/"   # Docker service name, shared network
    headers:
      X-Rohlik-Username: "{{ROHLIK_USERNAME}}"
      X-Rohlik-Password: "{{ROHLIK_PASSWORD}}"
    customUserVars:
      ROHLIK_USERNAME:
        title: "Rohlik.cz e-mail"
        description: "The e-mail you sign in to rohlik.cz with."
      ROHLIK_PASSWORD:
        title: "Rohlik.cz password"
        description: "Your rohlik.cz password. LibreChat stores it encrypted."
    timeout: 60000
```

Behaviours that matter for the design:

* Servers declaring `customUserVars` are **automatically excluded from app-level
  connections**, so LibreChat will not try to connect at startup without
  credentials — no `startup: false` needed.
* LibreChat keeps a **per-user MCP connection** and **disconnects it after ~15
  minutes idle**. Because the registry above is keyed by credentials and not by
  MCP session id, the Rohlik login survives LibreChat reconnects — a reconnect
  does not cost a fresh login.
* LibreChat **re-resolves headers before each tool call without reconnecting**.
  So a user changing their password takes effect on the next call: new hash, new
  registry entry, one fresh login, and the stale entry ages out via TTL.
* FastMCP has no session-close hook (no `on_disconnect` middleware event, and the
  streamable-HTTP `terminate()` path is internal), so TTL eviction is the only
  way to reclaim clients — there is no way to react to LibreChat's disconnect.

---

## 4. What shipped (0.5.0)

Implemented as described above, with these decisions settled:

* **`ROHLIK_USERNAME` / `ROHLIK_PASSWORD` are gone.** No env fallback at all, so
  a client that forgets its headers gets an actionable error rather than silently
  shopping in the operator's account. `ServerConfig.from_env()` no longer raises,
  so the server starts with no credentials configured.
* `Config` split into `ServerConfig` (bind settings, base URL, timeout, cache
  limits) and a per-request `Credentials`, whose `cache_key()` hashes username,
  password *and* base URL.
* `get_client()` is now `async` and resolves the account per request. Failure
  modes surface as `ToolError`, whose message FastMCP passes to the client
  verbatim: missing headers explain which headers to set, a rejected login says
  so and the entry is dropped so a corrected password is not shadowed.
* The registry is an LRU `OrderedDict` with a background reaper. Both eviction
  paths refuse to close an entry touched within the request timeout, so a call
  in flight cannot have its session closed underneath it; `client_ttl` is floored
  at 60s *and* at twice `ROHLIK_TIMEOUT` for the same reason.
* The login stampede is fixed here rather than upstream: `get_client()` performs
  the first login itself under a per-account lock, so a fanned-out tool logs in
  once. Verified end to end — five tool calls across three accounts produce
  exactly three logins.
* `docker-compose.yml` no longer publishes a port (`expose` only, on a named
  network) and carries no credential variables.

Not done, and deliberately: no fix upstream in `rohlik-api` for the unlocked
`AuthManager.ensure_logged_in`. This server no longer relies on it, but any other
consumer that fans out on a cold client still has the bug.
