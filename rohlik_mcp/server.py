"""MCP server exposing the Rohlík Group shop API as tools.

Rohlík.cz, Knuspr.de, Gurkerl.at, Kifli.hu and Sezamo.ro run the same API.
Credentials are supplied **per request** by the calling client, as the
``X-Rohlik-Username`` / ``X-Rohlik-Password`` headers, plus an optional
``X-Rohlik-Site`` header choosing the shop, so one server can serve many
accounts (see :mod:`rohlik_mcp.config`). A :class:`~rohlik_api.RohlikAPI`
client is created per account and shop and cached, keyed by a hash of the
credentials and the shop URL, so successive tool calls from one user reuse a
single logged-in session. Idle clients are logged out and closed by a
background reaper.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, tzinfo
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult as FastMCPToolResult
from rohlik_api import (
    Cart,
    DeliveryTracking,
    NextDelivery,
    RohlikAPI,
    RohlikAPIError,
    Site,
    UpcomingOrder,
)

from .config import (
    PASSWORD_HEADER,
    SITE_HEADER,
    USERNAME_HEADER,
    Credentials,
    InvalidSiteError,
    ServerConfig,
    resolve_shop,
)

if TYPE_CHECKING:
    from fastmcp.server.auth import StaticTokenVerifier

_LOGGER = logging.getLogger(__name__)

# A tool result is anything JSON-serialisable that FastMCP can return.
ToolResult = dict[str, Any] | list[Any] | str | None

_MISSING_CREDENTIALS_MESSAGE = (
    "No shop login credentials were supplied. This server takes them per request, "
    f"as the {USERNAME_HEADER} and {PASSWORD_HEADER} headers - set them in your "
    "MCP client's settings for this server."
)


# ``eq=False`` so ``==`` is identity. Entries are only ever compared to find a
# particular one (``entry not in leased``, ``_clients.get(key) is entry``), and
# a generated field-by-field ``__eq__`` would answer a question nobody is
# asking - one that a later field change could silently answer differently.
@dataclass(slots=True, eq=False)
class _AccountClient:
    """A cached, per-account API client and its bookkeeping."""

    client: RohlikAPI
    # Serialises the first login for this account. rohlik-api's own
    # ``ensure_logged_in`` is unlocked, so without this a tool that fans out
    # (``get_account_overview``, ``_gather_by_id``) would fire one login request
    # per in-flight coroutine on a cold client.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    # Tool calls currently holding this client. Eviction and reaping skip any
    # entry above zero: closing a client logs its session out, which would fail
    # the requests still using it.
    in_flight: int = 0


# Cached clients in least-recently-used order, keyed by ``Credentials.cache_key``.
_clients: OrderedDict[str, _AccountClient] = OrderedDict()
# Guards the registry's structure only; nothing awaiting I/O is done while held.
_registry_lock = asyncio.Lock()
_reaper_task: asyncio.Task[None] | None = None
# Strong references to in-flight background closes, so they are not garbage
# collected mid-await (asyncio only holds a weak reference to running tasks).
_pending_closes: set[asyncio.Task[None]] = set()

# Clients leased by the tool call being served, released when it finishes (see
# ``ClientLeaseMiddleware``). A list rather than the entry itself: child tasks
# inherit a copy of the context, so a write from inside ``asyncio.gather`` would
# not be visible here, but an append to the shared list is.
_leased: ContextVar[list[_AccountClient] | None] = ContextVar("_leased", default=None)


class ClientLeaseMiddleware(Middleware):
    """Release the clients a tool call leased once the call finishes.

    ``get_client`` marks the account it hands out as in use; without a matching
    release, eviction could never tell a busy client from an idle one.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, FastMCPToolResult],
    ) -> FastMCPToolResult:
        leased: list[_AccountClient] = []
        token = _leased.set(leased)
        try:
            return await call_next(context)
        finally:
            _leased.reset(token)
            for entry in leased:
                entry.in_flight -= 1


@lru_cache(maxsize=1)
def get_config() -> ServerConfig:
    """Return the process-wide server configuration, read once from the environment."""
    return ServerConfig.from_env()


def read_credentials() -> Credentials:
    """Read the caller's shop credentials from the current request headers.

    Raises:
        ToolError: If either header is missing or empty. A ``ToolError`` message
            reaches the client verbatim, so the user is told what to fix.
    """
    headers = get_http_headers()
    # Only the username is stripped: surrounding whitespace there is a paste
    # artefact, whereas a password is taken exactly as given.
    username = headers.get(USERNAME_HEADER, "").strip()
    password = headers.get(PASSWORD_HEADER, "")
    if not username or not password:
        raise ToolError(_MISSING_CREDENTIALS_MESSAGE)
    return Credentials(username=username, password=password)


def read_shop(config: ServerConfig) -> Site:
    """Return the shop the current request is for (``X-Rohlik-Site``, else the default).

    Raises:
        ToolError: If the header names an unknown shop, or one this server
            cannot serve because ``ROHLIK_BASE_URL`` pins it to one URL.
    """
    try:
        return resolve_shop(config, get_http_headers().get(SITE_HEADER))
    except InvalidSiteError as err:
        raise ToolError(str(err)) from err


async def _close_account_client(entry: _AccountClient) -> None:
    """Log out and close one cached client, best effort."""
    try:
        await entry.client.close()
    except Exception as err:  # noqa: BLE001 - teardown must not raise
        _LOGGER.warning("Error closing Rohlik client: %s", err)


def _schedule_close(entry: _AccountClient) -> None:
    """Close a client in the background, so evicting never delays a request."""
    task = asyncio.create_task(_close_account_client(entry))
    _pending_closes.add(task)
    task.add_done_callback(_pending_closes.discard)


def _evict_for_room(config: ServerConfig) -> list[_AccountClient]:
    """Drop least-recently-used clients until there is room for one more.

    Only clients no tool call is holding are eligible; if every one is busy the
    cache is allowed to exceed its limit rather than close a session out from
    under a request in flight.

    Called with ``_registry_lock`` held; returns the evicted entries for the
    caller to close outside the lock.
    """
    evicted: list[_AccountClient] = []
    while len(_clients) >= config.max_clients:
        victim_key = next((key for key, e in _clients.items() if e.in_flight == 0), None)
        if victim_key is None:
            _LOGGER.warning(
                "Account client cache is full (%d) but every entry is in use; "
                "exceeding the limit for this request.",
                config.max_clients,
            )
            break
        evicted.append(_clients.pop(victim_key))
    return evicted


async def get_client() -> RohlikAPI:
    """Return a logged-in API client for the credentials on the current request.

    The client is cached per account, so a run of tool calls from one user costs
    a single login rather than one per call.

    Raises:
        ToolError: If credentials are missing from the request, the requested
            shop is unknown, or the shop rejects the credentials.
    """
    config = get_config()
    credentials = read_credentials()
    shop = read_shop(config)
    # The URL is part of the key, so one email on two shops gets two sessions.
    key = credentials.cache_key(shop.base_url)

    async with _registry_lock:
        entry = _clients.get(key)
        if entry is None:
            evicted = _evict_for_room(config)
            entry = _AccountClient(
                client=RohlikAPI(
                    username=credentials.username,
                    password=credentials.password,
                    base_url=shop.base_url,
                    timeout=config.timeout,
                    auto_login=False,
                )
            )
            _clients[key] = entry
        else:
            evicted = []
            _clients.move_to_end(key)
        # Taken under the lock, so the entry cannot be chosen as an eviction
        # victim between being handed out here and being marked as in use.
        leased = _leased.get()
        if leased is not None and entry not in leased:
            entry.in_flight += 1
            leased.append(entry)

    for victim in evicted:
        _schedule_close(victim)

    async with entry.lock:
        if not entry.client.is_logged_in:
            try:
                await entry.client.login()
            except RohlikAPIError as err:
                # Drop the entry so a corrected password is not shadowed by a
                # cached client that can never log in.
                async with _registry_lock:
                    if _clients.get(key) is entry:
                        del _clients[key]
                _schedule_close(entry)
                raise ToolError(f"{shop.name} login failed: {err}") from err

    entry.last_used = time.monotonic()
    return entry.client


async def _reap_once(config: ServerConfig) -> int:
    """Log out and close every client idle for longer than the TTL.

    Returns the number of clients closed.
    """
    now = time.monotonic()
    async with _registry_lock:
        expired = [
            key
            for key, e in _clients.items()
            if e.in_flight == 0 and now - e.last_used > config.client_ttl
        ]
        victims = [_clients.pop(key) for key in expired]
    for entry in victims:
        _LOGGER.debug("Closing idle Rohlik client")
        await _close_account_client(entry)
    return len(victims)


async def _reap_idle_clients(config: ServerConfig) -> None:
    """Periodically log out and close clients that have gone idle."""
    interval = max(config.client_ttl / 4, 15.0)
    while True:
        await asyncio.sleep(interval)
        await _reap_once(config)


async def _close_all_clients() -> None:
    """Close every cached client (used on shutdown / in tests)."""
    async with _registry_lock:
        entries = list(_clients.values())
        _clients.clear()
    for entry in entries:
        await _close_account_client(entry)
    if _pending_closes:
        await asyncio.gather(*list(_pending_closes), return_exceptions=True)


def _serialize(obj: Any, tz: tzinfo | None = None) -> Any:
    """Recursively convert API results into JSON-serialisable structures.

    Dataclass models become dicts, and every ``datetime`` at any depth becomes
    an ISO 8601 string with its UTC offset (the typed models' timestamps are
    timezone-aware). With ``tz``, each is first converted to that timezone, so
    one result never mixes UTC and local times. Timestamps inside raw API
    payloads are strings already and pass through in the shop's own format.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        obj = asdict(obj)
    if isinstance(obj, datetime):
        return (obj.astimezone(tz) if tz is not None else obj).isoformat()
    if isinstance(obj, dict):
        return {key: _serialize(value, tz) for key, value in obj.items()}
    if isinstance(obj, list | tuple):
        return [_serialize(item, tz) for item in obj]
    return obj


async def _call(coro: Awaitable[Any]) -> ToolResult:
    """Await a service coroutine and normalise its result for MCP.

    Converts dataclass models to dicts, turns ``None`` into a friendly message
    and reports Rohlik API errors as a structured ``{"error": ...}`` payload
    instead of raising.
    """
    try:
        result: ToolResult = _serialize(await coro)
    except RohlikAPIError as err:
        return {"error": str(err)}

    if result is None:
        return "No data available (the request returned nothing or failed)."
    return result


def _serialize_fields(fields: dict[str, Any], tz: tzinfo | None = None) -> dict[str, Any]:
    """Serialise each value of a dict built by a tool (see :func:`_serialize`)."""
    return {key: _serialize(value, tz) for key, value in fields.items()}


# Cap on simultaneous in-flight requests per bulk lookup, so a large ID list
# can't open an unbounded number of connections at once.
_BULK_CONCURRENCY = 8


async def _gather_by_id(ids: list[Any], fetch: Callable[[Any], Awaitable[Any]]) -> dict[str, Any]:
    """Fetch many items concurrently, returning an ``{id: result}`` map.

    ``fetch`` is an async callable taking a single ID. The lookups run
    concurrently — LLM round-trips are the expensive resource, individual API
    calls are not — so a batch costs one tool call regardless of size. Per-item
    Rohlik API errors become an ``{"error": ...}`` value for that ID rather than
    failing the whole batch; ``None`` results (not found / no data) are kept as
    ``null`` so callers can tell which IDs came back empty.

    Keys are always strings: JSON object keys are strings anyway, so numeric IDs
    are stringified up front to make the contract explicit and stable. Duplicate
    IDs are collapsed (a JSON map can't hold duplicates), which also avoids
    redundant requests, and concurrency is bounded by ``_BULK_CONCURRENCY``.
    """
    if not ids:
        return {}
    # De-duplicate while preserving order.
    unique_ids = list(dict.fromkeys(ids))
    semaphore = asyncio.Semaphore(_BULK_CONCURRENCY)

    async def _bounded(item_id: Any) -> Any:
        async with semaphore:
            return await fetch(item_id)

    results = await asyncio.gather(
        *(_bounded(item_id) for item_id in unique_ids), return_exceptions=True
    )
    out: dict[str, Any] = {}
    for item_id, result in zip(unique_ids, results, strict=True):
        key = str(item_id)
        if isinstance(result, RohlikAPIError):
            out[key] = {"error": str(result)}
        elif isinstance(result, BaseException):
            raise result
        else:
            out[key] = _serialize(result)
    return out


# Best-effort denylist of bulky, low-signal keys in the raw product-detail
# payload. Trimming by removal (rather than whitelisting) keeps every useful
# field even if the API shape changes; tune this list against a live payload.
_DETAIL_DROP_KEYS = frozenset(
    {
        "images",
        "image",
        "imgPath",
        "imagePath",
        "gallery",
        "badges",
        "htmlDescription",
        "descriptionHtml",
        "marketingText",
        "banner",
        "banners",
        "relatedProducts",
        "similarProducts",
        "alternatives",
        "recommendations",
        "breadcrumbs",
    }
)


def _trim_product_detail(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop bulky display-only keys from a raw product-detail payload."""
    return {key: value for key, value in raw.items() if key not in _DETAIL_DROP_KEYS}


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Run the idle-client reaper, and close every client when the server stops."""
    global _reaper_task
    _reaper_task = asyncio.create_task(_reap_idle_clients(get_config()))
    try:
        yield
    finally:
        _reaper_task.cancel()
        try:
            await _reaper_task
        except asyncio.CancelledError:
            pass
        _reaper_task = None
        await _close_all_clients()


def _build_auth() -> StaticTokenVerifier | None:
    """Build bearer-token auth from ``ROHLIK_MCP_AUTH_TOKEN``, if set.

    When the variable is unset the server runs without authentication
    (unchanged behaviour). When set, clients must present the token as
    ``Authorization: Bearer <token>``.
    """
    token = os.environ.get("ROHLIK_MCP_AUTH_TOKEN")
    if not token:
        return None
    # Imported lazily so a missing/renamed symbol only breaks startup when auth
    # is actually configured, not for the default unauthenticated server.
    from fastmcp.server.auth import StaticTokenVerifier

    return StaticTokenVerifier(tokens={token: {"client_id": "rohlik-mcp"}})


mcp = FastMCP(
    "rohlik",
    instructions=(
        "Tools for the Rohlík Group online grocery shops (Rohlík.cz, Knuspr.de, "
        "Gurkerl.at, Kifli.hu, Sezamo.ro): search products and recipes, inspect "
        "prices/composition, manage the shopping cart and read orders and delivery "
        "information for the authenticated account. Each user's account is on one "
        "shop; prices are in that shop's currency (see get_account_overview)."
    ),
    lifespan=_lifespan,
    auth=_build_auth(),
)
mcp.add_middleware(ClientLeaseMiddleware())


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_products(query: str, limit: int = 10, favourite: bool = False) -> ToolResult:
    """Search for products by name.

    Args:
        query: Search term, e.g. "mleko".
        limit: Maximum number of products to return.
        favourite: If true, return only products marked as favourite.
    """
    client = await get_client()
    return await _call(client.products.search(query, limit=limit, favourite=favourite))


@mcp.tool()
async def get_product_cards(product_ids: list[int]) -> ToolResult:
    """Get basic info for several products at once: name, brand, amount, unit, price
    (incl. any sale and the original price), price-per-unit and stock status.

    This is the primary tool for product pricing and availability — prefer it
    over the detailed lookups below when you only need names, prices or stock.

    Args:
        product_ids: The product IDs to look up.
    """
    if not product_ids:
        return []
    return await _call((await get_client()).products.get_cards(product_ids))


@mcp.tool()
async def get_product_composition(product_ids: list[int]) -> ToolResult:
    """Get nutritional values, ingredients and allergens for one or more products.

    Args:
        product_ids: The product IDs to look up.

    Returns a map of product ID to its composition (``null`` when unavailable).
    """
    return await _gather_by_id(product_ids, (await get_client()).products.get_composition)


@mcp.tool()
async def get_product_ai_summary(product_ids: list[int]) -> ToolResult:
    """Get the AI-generated summary for one or more products.

    Args:
        product_ids: The product IDs to look up.

    Returns a map of product ID to its AI summary (``null`` when unavailable).
    """
    return await _gather_by_id(product_ids, (await get_client()).products.get_ai_summary)


@mcp.tool()
async def get_product_detail(product_ids: list[int]) -> ToolResult:
    """Get the detail (name, description, attributes, etc.) for one or more products.

    Bulky display-only fields (images, badges, marketing blocks) are stripped to
    keep the response compact. Use ``get_product_cards`` for price/stock only.

    Args:
        product_ids: The product IDs to look up.

    Returns a map of product ID to its (trimmed) detail (``null`` when unavailable).
    """
    products = (await get_client()).products

    async def _fetch(product_id: int) -> Any:
        raw = await products.get_detail(product_id)
        return _trim_product_detail(raw) if isinstance(raw, dict) else raw

    return await _gather_by_id(product_ids, _fetch)


@mcp.tool()
async def get_product_categories(product_ids: list[int]) -> ToolResult:
    """Get the category breadcrumb for one or more products.

    Args:
        product_ids: The product IDs to look up.

    Returns a map of product ID to its category hierarchy (``null`` when unavailable).
    """
    return await _gather_by_id(product_ids, (await get_client()).products.get_categories)


@mcp.tool()
async def get_weekly_sales(size: int = 30) -> ToolResult:
    """Get this week's deals ("Akce týdne"), enriched with basic product data.

    Args:
        size: Maximum number of products to return (default 30).
    """
    return await _call((await get_client()).products.get_week_sales(size=size))


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------


def _shop_tz(shop: Site) -> ZoneInfo:
    """Return the shop's timezone, for rendering the typed models' times locally."""
    return ZoneInfo(shop.timezone)


def _cart_fields(cart: Cart, shop: Site) -> dict[str, Any]:
    """Serialise a cart and add how far it is from the shop's minimum order.

    ``remaining_to_minimum`` is ``0`` once the total reaches the minimum, and
    ``null`` when the shop reports no minimum. ``currency`` falls back to the
    shop's when the cart has no items to take it from.
    """
    fields: dict[str, Any] = _serialize(cart)
    fields["currency"] = cart.currency or shop.currency
    minimum = cart.minimum_order_price
    fields["remaining_to_minimum"] = (
        round(max(0.0, minimum - cart.total_price), 2) if minimum is not None else None
    )
    return fields


@mcp.tool()
async def get_cart() -> ToolResult:
    """Get the current shopping cart: items, total price and currency.

    Also returns the shop's ``minimum_order_price`` and ``remaining_to_minimum``,
    the amount still needed before an order can be placed (``0`` once reached).
    ``can_make_order`` covers more than the minimum (e.g. a chosen delivery
    slot), so use ``remaining_to_minimum`` to answer "is my cart big enough?".
    """
    client = await get_client()
    shop = read_shop(get_config())
    try:
        cart = await client.cart.get_content()
    except RohlikAPIError as err:
        return {"error": str(err)}
    return _cart_fields(cart, shop)


@mcp.tool()
async def add_to_cart(items: list[dict[str, int]]) -> ToolResult:
    """Add one or more products to the cart in a single call.

    Args:
        items: A list of ``{"product_id": <int>, "quantity": <int>}`` entries.
            ``quantity`` defaults to 1 when omitted. Pass a single-entry list to
            add just one product.

    Returns the product IDs that were successfully added and any that failed,
    plus a ``cart`` summary afterwards: ``total_price``, ``currency`` and
    ``remaining_to_minimum`` (the amount still needed to reach the shop's
    minimum order; ``0`` once reached). The summary is an ``{"error": ...}``
    if the cart could not be read back; the items were still added.
    """
    if not items:
        return {"added": [], "failed": []}
    try:
        payload = [
            {"product_id": int(item["product_id"]), "quantity": int(item.get("quantity", 1))}
            for item in items
        ]
    except (KeyError, TypeError, ValueError) as err:
        return {
            "error": f"Each item needs an integer 'product_id' (and optional 'quantity'): {err}"
        }

    requested = [entry["product_id"] for entry in payload]
    client = await get_client()
    added = await _call(client.cart.add_items(payload))
    if not isinstance(added, list):
        # _call returned an error payload (or "no data") — surface it unchanged
        # instead of masking it as every item having failed.
        return added

    # One extra request, so the model can say how far the cart is from the
    # minimum order without a separate get_cart call.
    summary: dict[str, Any]
    try:
        cart = _cart_fields(await client.cart.get_content(), read_shop(get_config()))
    except RohlikAPIError as err:
        summary = {"error": str(err)}
    else:
        summary = {
            key: cart[key]
            for key in ("total_price", "currency", "minimum_order_price", "remaining_to_minimum")
        }
    return {
        "added": added,
        "failed": [pid for pid in requested if pid not in added],
        "cart": summary,
    }


@mcp.tool()
async def remove_from_cart(cart_item_ids: list[str]) -> ToolResult:
    """Remove one or more items from the cart using their cart_item_id (orderFieldId).

    Use ``get_cart`` first to find each ``cart_item_id``. Pass a single-entry
    list to remove just one item.

    Args:
        cart_item_ids: The cart-line IDs (``orderFieldId``) to remove.

    Returns the IDs that were removed and any that failed.
    """
    client = await get_client()
    removed: list[str] = []
    failed: list[str] = []
    # Removals mutate the cart, so run them sequentially for predictable results.
    for cart_item_id in cart_item_ids:
        try:
            await client.cart.delete_item(cart_item_id)
            removed.append(cart_item_id)
        except RohlikAPIError:
            failed.append(cart_item_id)
    return {"removed": removed, "failed": failed}


# ---------------------------------------------------------------------------
# Recipes (Rohlík Chef)
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_recipes(query: str, limit: int = 10, offset: int = 0) -> ToolResult:
    """Search for recipes by name (Rohlík Chef)."""
    return await _call((await get_client()).recipes.search(query, limit=limit, offset=offset))


@mcp.tool()
async def get_recipe_detail(recipe_ids: list[int]) -> ToolResult:
    """Get full details (ingredients and directions) for one or more recipes.

    Args:
        recipe_ids: The recipe IDs to look up.

    Returns a map of recipe ID to its detail (``null`` when unavailable).
    """
    return await _gather_by_id(recipe_ids, (await get_client()).recipes.get_detail)


@mcp.tool()
async def get_ingredient_products(
    ingredient_ids: list[int], limit: int = 5, offset: int = 0
) -> ToolResult:
    """Get purchasable products for the given recipe ingredient IDs."""
    client = await get_client()
    return await _call(
        client.recipes.get_ingredient_products(ingredient_ids, limit=limit, offset=offset)
    )


# ---------------------------------------------------------------------------
# Orders & delivery
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_next_order() -> ToolResult:
    """Get the upcoming (not yet delivered) orders, soonest delivery first.

    Each has its ``id``, delivery slot, order time, item count, total price and
    currency; times are in the shop's timezone. An empty list means there is no
    upcoming order. For an order's line items use ``get_order_detail``; for
    when it will arrive use ``track_delivery``.
    """
    client = await get_client()
    tz = _shop_tz(read_shop(get_config()))
    orders = await client.orders.get_upcoming()
    if orders is None:
        return {"error": "Cannot fetch upcoming orders."}
    serialized: list[Any] = _serialize(orders, tz)
    return serialized


@mcp.tool()
async def get_last_order() -> ToolResult:
    """Get the most recently delivered order."""
    return await _call((await get_client()).orders.get_last())


@mcp.tool()
async def get_delivered_orders(limit: int = 10, offset: int = 0) -> ToolResult:
    """Get the history of delivered orders."""
    return await _call((await get_client()).orders.get_delivered(limit=limit, offset=offset))


@mcp.tool()
async def get_order_detail(order_ids: list[int]) -> ToolResult:
    """Get the full detail (including line items) of one or more orders.

    Use ``get_next_order``, ``get_last_order`` or ``get_delivered_orders`` to
    find the ``order_id``s.

    Args:
        order_ids: The order IDs to look up.

    Returns a map of order ID to its detail (``null`` when unavailable).
    """
    return await _gather_by_id(order_ids, (await get_client()).orders.get_detail)


def _format_tracking(tracking: DeliveryTracking, tz: tzinfo) -> dict[str, Any]:
    """Flatten a ``DeliveryTracking`` model into a compact, JSON-ready dict.

    Times are converted to ``tz``, the shop's timezone. The raw coordinates of
    the courier and of the delivery address are left out: a straight-line
    distance answers "how far away is it" without putting location data into
    the conversation. The courier's first name is kept, as the shop's own app
    shows it to the customer.
    """
    progress = tracking.progress
    distance = tracking.courier_distance
    return _serialize_fields(
        {
            "order_id": tracking.order_id,
            "status": tracking.status,
            "is_active": tracking.is_active,
            "has_arrived": tracking.has_arrived,
            "is_delivered": tracking.is_delivered,
            "estimated_arrival": tracking.estimated_arrival,
            "stops_before": tracking.stops_before,
            "courier_name": tracking.courier_name,
            "courier_distance_m": round(distance) if distance is not None else None,
            "courier_location_stale": progress.courier_location_stale if progress else None,
            "generated_at": progress.generated_at if progress else None,
            "slot_since": tracking.slot_since,
            "slot_till": tracking.slot_till,
            "slot_type": tracking.slot_type,
            "early_delivery_available": tracking.early_delivery_available,
            "early_delivery_requested": tracking.early_delivery_requested,
            "early_delivery_allowed_from": tracking.early_delivery_allowed_from,
        },
        tz,
    )


@mcp.tool()
async def track_delivery(order_id: int | None = None) -> ToolResult:
    """Track a delivery live: status, courier ETA, stops before yours and courier distance.

    With no ``order_id`` this tracks the soonest upcoming order and also returns
    a resolved ``eta`` (the courier estimate, never earlier than the slot start
    while the order is still queued; the slot start when there is no estimate).
    Pass an ``order_id`` to keep following an order after it drops off the
    upcoming list, e.g. to see it reach ``DELIVERED``.

    Statuses run ``IN_QUEUE`` -> ``ON_THE_WAY`` -> ``ARRIVED`` -> ``HANDING_OVER``
    -> ``DELIVERED``; ``status`` is ``null`` until the order is queued for a
    courier. The data changes minute by minute, so fetch fresh when asked.

    All times are in the shop's local time (``timezone`` in the result).
    ``courier_distance_m`` is a straight-line distance from the courier's last
    reported position; when ``courier_location_stale`` is true that position is
    outdated, so do not present the distance as current.

    Args:
        order_id: The order to track. Omit to track the next upcoming order.
    """
    if order_id is not None and order_id < 0:
        return {"error": "Invalid order_id: it must not be negative."}
    orders = (await get_client()).orders
    shop = read_shop(get_config())
    timezone_name = shop.timezone
    tz = _shop_tz(shop)
    try:
        if order_id is not None:
            tracking = await orders.get_delivery_tracking(order_id, raise_on_error=True)
            if tracking is None:
                return f"The shop has no delivery tracking for order {order_id}."
            return {"timezone": timezone_name, **_format_tracking(tracking, tz)}

        nxt = await orders.get_next_delivery()
    except RohlikAPIError as err:
        return {"error": str(err)}

    if nxt is None:
        return "There is no upcoming order to track."
    return _format_next_delivery(nxt, tz, timezone_name)


def _format_next_delivery(nxt: NextDelivery, tz: tzinfo, timezone_name: str) -> dict[str, Any]:
    """Flatten a ``NextDelivery`` into a compact dict, times in the shop's ``tz``."""
    order = nxt.order
    return _serialize_fields(
        {
            "timezone": timezone_name,
            "order_id": order.id,
            "eta": nxt.eta,
            "eta_source": nxt.eta_source,
            "eta_capped": nxt.eta_capped,
            "status": nxt.status,
            "order": {
                "slot_since": order.slot_since,
                "slot_till": order.slot_till,
                "slot_type": order.slot_type,
                "order_time": order.order_time,
                "items_count": order.items_count,
                "total_price": order.total_price,
                "currency": order.currency,
            },
            "tracking": _format_tracking(nxt.tracking, tz) if nxt.tracking is not None else None,
        },
        tz,
    )


@mcp.tool()
async def get_delivery_info() -> ToolResult:
    """Get first-delivery information for the account."""
    return await _call((await get_client()).delivery.get_info())


@mcp.tool()
async def get_delivery_addresses() -> ToolResult:
    """Get the account's saved delivery addresses."""
    return await _call((await get_client()).delivery.get_addresses())


@mcp.tool()
async def get_next_delivery_slots() -> ToolResult:
    """Get the next available delivery slots."""
    return await _call((await get_client()).delivery.get_next_slots())


@mcp.tool()
async def get_timeslot_reservation() -> ToolResult:
    """Get the delivery timeslot currently reserved for the account, if any."""
    return await _call((await get_client()).delivery.get_timeslot_reservation())


@mcp.tool()
async def get_delivery_announcements() -> ToolResult:
    """Get delivery announcements, including the current delivery-time ETA
    for an upcoming order (the ETA shifts often, so fetch fresh when asked
    about when an order will arrive)."""
    return await _call((await get_client()).delivery.get_announcements())


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_shopping_list(shopping_list_ids: list[str]) -> ToolResult:
    """Get one or more saved shopping lists by their IDs.

    Args:
        shopping_list_ids: The shopping-list IDs to look up.

    Returns a map of shopping-list ID to its contents (``null`` when unavailable).
    """
    return await _gather_by_id(shopping_list_ids, (await get_client()).account.get_shopping_list)


@mcp.tool()
async def get_account_overview() -> ToolResult:
    """Get a trimmed one-call snapshot of the account.

    Starts with the ``shop`` the account is on (name, currency, timezone), then
    combines delivery info, the upcoming orders, the ``next_delivery`` (the
    soonest order's live status and resolved ETA, as ``track_delivery`` gives
    it; ``null`` with no upcoming order), the last order, the cart (with
    ``remaining_to_minimum``), premium profile, reusable-bags info, account
    announcements, the reserved timeslot and the next available delivery slot
    in a single call. The full delivered-order history is excluded — use
    ``get_delivered_orders`` for that. Prefer the specific tools when only one
    piece of information is needed.
    """
    client = await get_client()
    shop = read_shop(get_config())
    tz = _shop_tz(shop)
    sections: list[tuple[str, Awaitable[Any]]] = [
        ("delivery", client.delivery.get_info()),
        ("upcoming_orders", client.orders.get_upcoming()),
        ("last_order", client.orders.get_last()),
        ("cart", client.cart.get_content()),
        ("premium_profile", client.account.get_premium_profile()),
        ("bags", client.account.get_bags_info()),
        ("announcements", client.account.get_announcements()),
        ("timeslot", client.delivery.get_timeslot_reservation()),
        ("next_delivery_slot", client.delivery.get_next_slots()),
    ]
    names = [name for name, _ in sections]
    results = await asyncio.gather(*(coro for _, coro in sections), return_exceptions=True)
    out: dict[str, Any] = {
        "shop": {
            "code": shop.code,
            "name": shop.name,
            "currency": shop.currency,
            "timezone": shop.timezone,
        }
    }
    upcoming: list[UpcomingOrder] | None = None
    for name, result in zip(names, results, strict=True):
        if isinstance(result, RohlikAPIError):
            out[name] = {"error": str(result)}
        elif isinstance(result, BaseException):
            raise result
        elif name == "upcoming_orders" and result is None:
            out[name] = {"error": "Cannot fetch upcoming orders."}
        elif name == "upcoming_orders":
            upcoming = result
            out[name] = _serialize(result, tz)
        elif name == "cart":
            out[name] = _cart_fields(result, shop)
        else:
            out[name] = _serialize(result, tz)

    if upcoming is None:
        out["next_delivery"] = {"error": "Upcoming orders unavailable, so no delivery to track."}
    else:
        out["next_delivery"] = await _next_delivery_for(client, upcoming, tz, shop.timezone)
    return out


async def _next_delivery_for(
    client: RohlikAPI, upcoming: list[UpcomingOrder], tz: tzinfo, timezone_name: str
) -> dict[str, Any] | None:
    """Track the soonest of ``upcoming`` (already fetched) and resolve its ETA.

    This is ``orders.get_next_delivery`` without its second fetch of the
    upcoming orders: one request, and none when there is no upcoming order.
    """
    if not upcoming:
        return None
    order = upcoming[0]
    try:
        tracking = await client.orders.get_delivery_tracking(order.id, raise_on_error=True)
    except RohlikAPIError as err:
        return {"error": str(err)}
    try:
        nxt = NextDelivery.resolve(order, tracking)
    except ValueError as err:
        return {"error": f"Inconsistent delivery tracking: {err}"}
    return _format_next_delivery(nxt, tz, timezone_name)


@mcp.tool()
async def get_premium_profile() -> ToolResult:
    """Get the premium (membership) profile for the account."""
    return await _call((await get_client()).account.get_premium_profile())


@mcp.tool()
async def get_bags_info() -> ToolResult:
    """Get information about reusable shopping bags for the account."""
    return await _call((await get_client()).account.get_bags_info())


@mcp.tool()
async def get_announcements() -> ToolResult:
    """Get current account announcements (banners, notices)."""
    return await _call((await get_client()).account.get_announcements())
