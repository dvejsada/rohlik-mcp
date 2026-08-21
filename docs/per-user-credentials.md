# Per-user credentials & session lifecycle — investigation

Status: **investigation only, nothing implemented.**
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

* The password rides in a header on **every** request. TLS is no longer optional:
  document a reverse proxy / HTTPS URL as a requirement for per-user mode.
* Never log resolved headers. `rohlik_api.helpers.mask_data` exists for the login
  response; there is no equivalent guard on request headers.
* `ROHLIK_MCP_AUTH_TOKEN` stays useful as a "who may reach this endpoint at all"
  gate and composes fine — LibreChat can send a static bearer from `${ENV}` *and*
  per-user `X-Rohlik-*` from `customUserVars` in the same `headers:` block.
* Rohlik sees N logins from one IP instead of one. TTL-based eviction (rather
  than per-call logout) keeps that rate low.

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
    url: "https://rohlik-mcp.example.com/mcp/"
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

## 4. Recommendation

Feasible, and a fairly contained change. Suggested order:

1. Split `Config` into server config + per-request credentials; stop failing
   startup on missing credentials.
2. Replace the `_client` global with a credential-keyed registry (TTL reaper,
   LRU cap, per-key login lock), keeping `get_client()` no-arg so the tools are
   untouched.
3. Resolve credentials from `X-Rohlik-Username` / `X-Rohlik-Password`, with an
   explicitly opt-in env fallback.
4. Fix (or upstream a fix for) the unlocked `ensure_logged_in` login stampede.
5. Docs: LibreChat section, TLS requirement, updated variable table.
