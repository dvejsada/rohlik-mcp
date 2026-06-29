"""MCP server exposing the Rohlik.cz API as tools.

Credentials are read from the environment (see :mod:`rohlik_mcp.config`). A
single :class:`~rohlik_api.RohlikAPI` client is created lazily on first use and
closed when the server shuts down. Service methods log in automatically on the
first authenticated call.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from rohlik_api import RohlikAPI, RohlikAPIError

from .config import Config

if TYPE_CHECKING:
    from fastmcp.server.auth import StaticTokenVerifier

_LOGGER = logging.getLogger(__name__)

# A tool result is anything JSON-serialisable that FastMCP can return.
ToolResult = dict[str, Any] | list[Any] | str | None

_client: RohlikAPI | None = None


def get_client() -> RohlikAPI:
    """Return the shared RohlikAPI client, creating it on first use."""
    global _client
    if _client is None:
        config = Config.from_env()
        _client = RohlikAPI(
            username=config.username,
            password=config.password,
            base_url=config.base_url,
            timeout=config.timeout,
            auto_login=False,
        )
    return _client


async def _close_client() -> None:
    """Close and clear the shared client (used on shutdown / in tests)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def _serialize(obj: Any) -> Any:
    """Recursively convert dataclass models into JSON-serialisable structures."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, list):
        return [_serialize(item) for item in obj]
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


async def _gather_by_id(ids: list[Any], fetch: Callable[[Any], Awaitable[Any]]) -> dict[str, Any]:
    """Fetch many items concurrently, returning an ``{id: result}`` map.

    ``fetch`` is an async callable taking a single ID. The lookups run
    concurrently — LLM round-trips are the expensive resource, individual API
    calls are not — so a batch costs one tool call regardless of size. Per-item
    Rohlik API errors become an ``{"error": ...}`` value for that ID rather than
    failing the whole batch; ``None`` results (not found / no data) are kept as
    ``null`` so callers can tell which IDs came back empty.

    Keys are always strings: JSON object keys are strings anyway, so numeric IDs
    are stringified up front to make the contract explicit and stable.
    """
    if not ids:
        return {}
    results = await asyncio.gather(*(fetch(item_id) for item_id in ids), return_exceptions=True)
    out: dict[str, Any] = {}
    for item_id, result in zip(ids, results, strict=True):
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
    """Ensure the shared client is closed when the server stops."""
    try:
        yield
    finally:
        await _close_client()


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
        "Tools for the Rohlik.cz online grocery service: search products and "
        "recipes, inspect prices/composition, manage the shopping cart and read "
        "orders and delivery information for the authenticated account."
    ),
    lifespan=_lifespan,
    auth=_build_auth(),
)


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
    return await _call(get_client().products.search(query, limit=limit, favourite=favourite))


@mcp.tool()
async def get_product_cards(product_ids: list[int]) -> ToolResult:
    """Get basic info for several products at once: name, brand, amount, unit, price
    (incl. any sale and the original price), price-per-unit and stock status.

    This is the primary tool for product pricing and availability — prefer it
    over the detailed lookups below when you only need names, prices or stock.

    Args:
        product_ids: The product IDs to look up.
    """
    return await _call(get_client().products.get_cards(product_ids))


@mcp.tool()
async def get_product_composition(product_ids: list[int]) -> ToolResult:
    """Get nutritional values, ingredients and allergens for one or more products.

    Args:
        product_ids: The product IDs to look up.

    Returns a map of product ID to its composition (``null`` when unavailable).
    """
    return await _gather_by_id(product_ids, get_client().products.get_composition)


@mcp.tool()
async def get_product_ai_summary(product_ids: list[int]) -> ToolResult:
    """Get the AI-generated summary for one or more products.

    Args:
        product_ids: The product IDs to look up.

    Returns a map of product ID to its AI summary (``null`` when unavailable).
    """
    return await _gather_by_id(product_ids, get_client().products.get_ai_summary)


@mcp.tool()
async def get_product_detail(product_ids: list[int]) -> ToolResult:
    """Get the detail (name, description, attributes, etc.) for one or more products.

    Bulky display-only fields (images, badges, marketing blocks) are stripped to
    keep the response compact. Use ``get_product_cards`` for price/stock only.

    Args:
        product_ids: The product IDs to look up.

    Returns a map of product ID to its (trimmed) detail (``null`` when unavailable).
    """
    products = get_client().products

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
    return await _gather_by_id(product_ids, get_client().products.get_categories)


@mcp.tool()
async def get_weekly_sales(size: int = 30) -> ToolResult:
    """Get this week's deals ("Akce týdne"), enriched with basic product data.

    Args:
        size: Maximum number of products to return (default 30).
    """
    return await _call(get_client().products.get_week_sales(size=size))


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_cart() -> ToolResult:
    """Get the current shopping cart contents and total price."""
    return await _call(get_client().cart.get_content())


@mcp.tool()
async def add_to_cart(items: list[dict[str, int]]) -> ToolResult:
    """Add one or more products to the cart in a single call.

    Args:
        items: A list of ``{"product_id": <int>, "quantity": <int>}`` entries.
            ``quantity`` defaults to 1 when omitted. Pass a single-entry list to
            add just one product.

    Returns the product IDs that were successfully added and any that failed.
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
    added = await _call(get_client().cart.add_items(payload))
    if not isinstance(added, list):
        # _call returned an error payload (or "no data") — surface it unchanged
        # instead of masking it as every item having failed.
        return added
    return {
        "added": added,
        "failed": [pid for pid in requested if pid not in added],
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
    client = get_client()
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
    return await _call(get_client().recipes.search(query, limit=limit, offset=offset))


@mcp.tool()
async def get_recipe_detail(recipe_ids: list[int]) -> ToolResult:
    """Get full details (ingredients and directions) for one or more recipes.

    Args:
        recipe_ids: The recipe IDs to look up.

    Returns a map of recipe ID to its detail (``null`` when unavailable).
    """
    return await _gather_by_id(recipe_ids, get_client().recipes.get_detail)


@mcp.tool()
async def get_ingredient_products(
    ingredient_ids: list[int], limit: int = 5, offset: int = 0
) -> ToolResult:
    """Get purchasable products for the given recipe ingredient IDs."""
    return await _call(
        get_client().recipes.get_ingredient_products(ingredient_ids, limit=limit, offset=offset)
    )


# ---------------------------------------------------------------------------
# Orders & delivery
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_next_order() -> ToolResult:
    """Get the next upcoming order."""
    return await _call(get_client().orders.get_next())


@mcp.tool()
async def get_last_order() -> ToolResult:
    """Get the most recently delivered order."""
    return await _call(get_client().orders.get_last())


@mcp.tool()
async def get_delivered_orders(limit: int = 10, offset: int = 0) -> ToolResult:
    """Get the history of delivered orders."""
    return await _call(get_client().orders.get_delivered(limit=limit, offset=offset))


@mcp.tool()
async def get_order_detail(order_ids: list[int]) -> ToolResult:
    """Get the full detail (including line items) of one or more orders.

    Use ``get_next_order``, ``get_last_order`` or ``get_delivered_orders`` to
    find the ``order_id``s.

    Args:
        order_ids: The order IDs to look up.

    Returns a map of order ID to its detail (``null`` when unavailable).
    """
    return await _gather_by_id(order_ids, get_client().orders.get_detail)


@mcp.tool()
async def get_delivery_info() -> ToolResult:
    """Get first-delivery information for the account."""
    return await _call(get_client().delivery.get_info())


@mcp.tool()
async def get_delivery_addresses() -> ToolResult:
    """Get the account's saved delivery addresses."""
    return await _call(get_client().delivery.get_addresses())


@mcp.tool()
async def get_next_delivery_slots() -> ToolResult:
    """Get the next available delivery slots."""
    return await _call(get_client().delivery.get_next_slots())


@mcp.tool()
async def get_timeslot_reservation() -> ToolResult:
    """Get the delivery timeslot currently reserved for the account, if any."""
    return await _call(get_client().delivery.get_timeslot_reservation())


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
    return await _gather_by_id(shopping_list_ids, get_client().account.get_shopping_list)


@mcp.tool()
async def get_account_overview() -> ToolResult:
    """Get a trimmed one-call snapshot of the account.

    Combines delivery info, the next and last orders, cart contents, premium
    profile, reusable-bags info, announcements, the reserved timeslot and the
    next available delivery slot in a single call. The full delivered-order
    history is excluded — use ``get_delivered_orders`` for that. Prefer the
    specific tools when only one piece of information is needed.
    """
    client = get_client()
    sections: list[tuple[str, Awaitable[Any]]] = [
        ("delivery", client.delivery.get_info()),
        ("next_order", client.orders.get_next()),
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
    out: dict[str, Any] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, RohlikAPIError):
            out[name] = {"error": str(result)}
        elif isinstance(result, BaseException):
            raise result
        else:
            out[name] = _serialize(result)
    return out


@mcp.tool()
async def get_premium_profile() -> ToolResult:
    """Get the premium (Rohlik membership) profile for the account."""
    return await _call(get_client().account.get_premium_profile())


@mcp.tool()
async def get_bags_info() -> ToolResult:
    """Get information about reusable shopping bags for the account."""
    return await _call(get_client().account.get_bags_info())


@mcp.tool()
async def get_announcements() -> ToolResult:
    """Get current account announcements (banners, notices)."""
    return await _call(get_client().account.get_announcements())
