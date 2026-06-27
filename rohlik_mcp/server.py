"""MCP server exposing the Rohlik.cz API as tools.

Credentials are read from the environment (see :mod:`rohlik_mcp.config`). A
single :class:`~rohlik_api.RohlikAPI` client is created lazily on first use and
closed when the server shuts down. Service methods log in automatically on the
first authenticated call.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from typing import Any

from fastmcp import FastMCP
from rohlik_api import RohlikAPI, RohlikAPIError

from .config import Config

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


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Ensure the shared client is closed when the server stops."""
    try:
        yield
    finally:
        await _close_client()


mcp = FastMCP(
    "rohlik",
    instructions=(
        "Tools for the Rohlik.cz online grocery service: search products and "
        "recipes, inspect prices/composition, manage the shopping cart and read "
        "orders and delivery information for the authenticated account."
    ),
    lifespan=_lifespan,
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
async def get_product_price(product_id: int) -> ToolResult:
    """Get the current price and price-per-unit for a product."""
    return await _call(get_client().products.get_price(product_id))


@mcp.tool()
async def get_product_composition(product_id: int) -> ToolResult:
    """Get nutritional values, ingredients and allergens for a product."""
    return await _call(get_client().products.get_composition(product_id))


@mcp.tool()
async def get_product_ai_summary(product_id: int) -> ToolResult:
    """Get the AI-generated summary for a product."""
    return await _call(get_client().products.get_ai_summary(product_id))


@mcp.tool()
async def get_product_detail(product_id: int) -> ToolResult:
    """Get the full detail for a product (name, images, description, badges, etc.)."""
    return await _call(get_client().products.get_detail(product_id))


@mcp.tool()
async def get_product_categories(product_id: int) -> ToolResult:
    """Get the category breadcrumb a product belongs to."""
    return await _call(get_client().products.get_categories(product_id))


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_cart() -> ToolResult:
    """Get the current shopping cart contents and total price."""
    return await _call(get_client().cart.get_content())


@mcp.tool()
async def add_to_cart(product_id: int, quantity: int = 1) -> ToolResult:
    """Add a product to the shopping cart.

    Args:
        product_id: The ID of the product to add.
        quantity: How many units to add (default 1).
    """
    client = get_client()
    added = await _call(client.cart.add_items([{"product_id": product_id, "quantity": quantity}]))
    if isinstance(added, list) and product_id in added:
        return {"added": True, "product_id": product_id, "quantity": quantity}
    return {"added": False, "product_id": product_id}


@mcp.tool()
async def add_items_to_cart(items: list[dict[str, int]]) -> ToolResult:
    """Add several products to the cart in one call.

    Args:
        items: A list of ``{"product_id": <int>, "quantity": <int>}`` entries.
            ``quantity`` defaults to 1 when omitted.

    Returns a list of the product IDs that were successfully added.
    """
    payload = [
        {"product_id": int(item["product_id"]), "quantity": int(item.get("quantity", 1))}
        for item in items
    ]
    requested = [entry["product_id"] for entry in payload]
    added = await _call(get_client().cart.add_items(payload))
    added_ids = added if isinstance(added, list) else []
    return {
        "added": added_ids,
        "failed": [pid for pid in requested if pid not in added_ids],
    }


@mcp.tool()
async def remove_from_cart(cart_item_id: str) -> ToolResult:
    """Remove an item from the cart using its cart_item_id (orderFieldId).

    Use ``get_cart`` first to find the ``cart_item_id`` of the product.
    """
    try:
        await get_client().cart.delete_item(cart_item_id)
    except RohlikAPIError as err:
        return {"error": str(err)}
    return {"removed": True, "cart_item_id": cart_item_id}


# ---------------------------------------------------------------------------
# Recipes (Rohlík Chef)
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_recipes(query: str, limit: int = 10, offset: int = 0) -> ToolResult:
    """Search for recipes by name (Rohlík Chef)."""
    return await _call(get_client().recipes.search(query, limit=limit, offset=offset))


@mcp.tool()
async def get_recipe_detail(recipe_id: int) -> ToolResult:
    """Get full recipe details, including ingredients and directions."""
    return await _call(get_client().recipes.get_detail(recipe_id))


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
async def get_order_detail(order_id: int) -> ToolResult:
    """Get the full detail of a single order, including its line items.

    Use ``get_next_order``, ``get_last_order`` or ``get_delivered_orders`` to
    find the ``order_id``.
    """
    return await _call(get_client().orders.get_detail(order_id))


@mcp.tool()
async def get_delivery_info() -> ToolResult:
    """Get first-delivery information for the account."""
    return await _call(get_client().delivery.get_info())


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
async def get_shopping_list(shopping_list_id: str) -> ToolResult:
    """Get a saved shopping list by its ID."""
    return await _call(get_client().account.get_shopping_list(shopping_list_id))


@mcp.tool()
async def get_account_overview() -> ToolResult:
    """Get an aggregated snapshot of the account.

    Includes delivery info, upcoming and recent orders, cart contents, premium
    profile, announcements and delivery slots in a single call. Prefer the more
    specific tools when only one piece of information is needed.
    """
    return await _call(get_client().get_data())


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
