"""Tests for the MCP server tools.

The tools are exercised directly with a mocked RohlikAPI client, so no network
access or credentials are required.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client
from rohlik_api import (
    APIRequestFailedError,
    Cart,
    CartItem,
    ProductCard,
    ProductComposition,
)

from rohlik_mcp import server


@pytest.fixture
def mock_client(monkeypatch):
    """Patch server.get_client to return a fully mocked RohlikAPI client."""
    client = MagicMock()
    client.products = MagicMock()
    client.cart = MagicMock()
    client.recipes = MagicMock()
    client.orders = MagicMock()
    client.delivery = MagicMock()
    client.account = MagicMock()
    monkeypatch.setattr(server, "get_client", lambda: client)
    return client


class TestSerialize:
    def test_dataclass_to_dict(self):
        item = CartItem(id="1", cart_item_id="f1", name="Milk", quantity=2, price=20.0)
        assert server._serialize(item) == {
            "id": "1",
            "cart_item_id": "f1",
            "name": "Milk",
            "quantity": 2,
            "price": 20.0,
            "category_name": "",
            "brand": "",
        }

    def test_list_of_dataclasses(self):
        items = [CartItem("1", "f1", "Milk", 1, 10.0)]
        result = server._serialize(items)
        assert isinstance(result, list)
        assert result[0]["name"] == "Milk"

    def test_passthrough(self):
        assert server._serialize({"a": 1}) == {"a": 1}
        assert server._serialize(None) is None


class TestProductTools:
    async def test_get_product_composition_bulk(self, mock_client):
        comp = ProductComposition(product_id=1, ingredients="milk")
        mock_client.products.get_composition = AsyncMock(side_effect=[comp, None])
        result = await server.get_product_composition([1, 2])
        assert result["1"]["product_id"] == 1
        assert result["1"]["ingredients"] == "milk"
        assert result["2"] is None

    async def test_get_product_detail_bulk_trims_noise(self, mock_client):
        mock_client.products.get_detail = AsyncMock(
            return_value={"id": 123, "name": "Milk", "images": ["a", "b"], "badges": ["x"]}
        )
        result = await server.get_product_detail([123])
        assert result == {"123": {"id": 123, "name": "Milk"}}
        mock_client.products.get_detail.assert_awaited_once_with(123)

    async def test_get_product_detail_missing_is_null(self, mock_client):
        mock_client.products.get_detail = AsyncMock(return_value=None)
        result = await server.get_product_detail([999])
        assert result == {"999": None}

    async def test_get_product_categories_bulk(self, mock_client):
        mock_client.products.get_categories = AsyncMock(return_value=[{"id": 1, "name": "Dairy"}])
        result = await server.get_product_categories([123])
        assert result == {"123": [{"id": 1, "name": "Dairy"}]}
        mock_client.products.get_categories.assert_awaited_once_with(123)

    async def test_bulk_empty_ids_returns_empty_map(self, mock_client):
        mock_client.products.get_categories = AsyncMock()
        result = await server.get_product_categories([])
        assert result == {}
        mock_client.products.get_categories.assert_not_awaited()

    async def test_bulk_deduplicates_ids(self, mock_client):
        mock_client.products.get_categories = AsyncMock(return_value=[{"id": 1}])
        result = await server.get_product_categories([7, 7, 7])
        assert result == {"7": [{"id": 1}]}
        mock_client.products.get_categories.assert_awaited_once_with(7)

    async def test_get_product_cards_empty_skips_api(self, mock_client):
        mock_client.products.get_cards = AsyncMock()
        result = await server.get_product_cards([])
        assert result == []
        mock_client.products.get_cards.assert_not_awaited()

    async def test_bulk_surfaces_per_item_error(self, mock_client):
        mock_client.account.get_shopping_list = AsyncMock(side_effect=APIRequestFailedError("boom"))
        result = await server.get_shopping_list(["abc"])
        assert result == {"abc": {"error": "boom"}}

    async def test_get_product_cards_serializes_models(self, mock_client):
        card = ProductCard(
            id=1,
            name="Milk",
            brand="Rohlik",
            amount="1 l",
            unit="l",
            price=27.9,
            original_price=34.9,
            unit_price=27.9,
            currency="CZK",
            on_sale=True,
        )
        mock_client.products.get_cards = AsyncMock(return_value=[card])
        result = await server.get_product_cards([1, 2])
        assert result[0]["id"] == 1
        assert result[0]["on_sale"] is True
        assert result[0]["price"] == 27.9
        mock_client.products.get_cards.assert_awaited_once_with([1, 2])

    async def test_get_weekly_sales(self, mock_client):
        card = ProductCard(
            id=5,
            name="Cheese",
            brand=None,
            amount="200 g",
            unit="ks",
            price=49.9,
            original_price=59.9,
            unit_price=249.5,
            currency="CZK",
            on_sale=True,
        )
        mock_client.products.get_week_sales = AsyncMock(return_value=[card])
        result = await server.get_weekly_sales(size=10)
        assert result[0]["name"] == "Cheese"
        mock_client.products.get_week_sales.assert_awaited_once_with(size=10)


class TestCartTools:
    async def test_get_cart(self, mock_client):
        mock_client.cart.get_content = AsyncMock(
            return_value=Cart(total_price=99.9, total_items=1, can_make_order=True, products=[])
        )
        result = await server.get_cart()
        assert result["total_price"] == 99.9
        assert result["total_items"] == 1

    async def test_add_to_cart_single(self, mock_client):
        mock_client.cart.add_items = AsyncMock(return_value=[123])
        result = await server.add_to_cart([{"product_id": 123, "quantity": 2}])
        assert result == {"added": [123], "failed": []}
        mock_client.cart.add_items.assert_awaited_once_with([{"product_id": 123, "quantity": 2}])

    async def test_add_to_cart_partial(self, mock_client):
        mock_client.cart.add_items = AsyncMock(return_value=[123])
        result = await server.add_to_cart([{"product_id": 123, "quantity": 2}, {"product_id": 456}])
        assert result == {"added": [123], "failed": [456]}
        mock_client.cart.add_items.assert_awaited_once_with(
            [{"product_id": 123, "quantity": 2}, {"product_id": 456, "quantity": 1}]
        )

    async def test_add_to_cart_surfaces_api_error(self, mock_client):
        mock_client.cart.add_items = AsyncMock(side_effect=APIRequestFailedError("auth failed"))
        result = await server.add_to_cart([{"product_id": 123}])
        assert result == {"error": "auth failed"}

    async def test_add_to_cart_malformed_input(self, mock_client):
        mock_client.cart.add_items = AsyncMock()
        result = await server.add_to_cart([{"quantity": 2}])
        assert isinstance(result, dict)
        assert "error" in result
        mock_client.cart.add_items.assert_not_awaited()

    async def test_add_to_cart_empty_skips_api(self, mock_client):
        mock_client.cart.add_items = AsyncMock()
        result = await server.add_to_cart([])
        assert result == {"added": [], "failed": []}
        mock_client.cart.add_items.assert_not_awaited()

    async def test_remove_from_cart_partial(self, mock_client):
        mock_client.cart.delete_item = AsyncMock(side_effect=[None, APIRequestFailedError("boom")])
        result = await server.remove_from_cart(["f1", "f2"])
        assert result == {"removed": ["f1"], "failed": ["f2"]}
        assert mock_client.cart.delete_item.await_count == 2


class TestOrderTools:
    async def test_get_order_detail_bulk(self, mock_client):
        mock_client.orders.get_detail = AsyncMock(return_value={"id": 555, "state": "DELIVERED"})
        result = await server.get_order_detail([555])
        assert result == {"555": {"id": 555, "state": "DELIVERED"}}
        mock_client.orders.get_detail.assert_awaited_once_with(555)


class TestAccountAndDeliveryTools:
    async def test_none_result_returns_message(self, mock_client):
        mock_client.account.get_premium_profile = AsyncMock(return_value=None)
        result = await server.get_premium_profile()
        assert isinstance(result, str)
        assert "No data" in result

    async def test_get_premium_profile(self, mock_client):
        mock_client.account.get_premium_profile = AsyncMock(return_value={"active": True})
        assert await server.get_premium_profile() == {"active": True}

    async def test_get_bags_info(self, mock_client):
        mock_client.account.get_bags_info = AsyncMock(return_value={"count": 3})
        assert await server.get_bags_info() == {"count": 3}

    async def test_get_announcements(self, mock_client):
        mock_client.account.get_announcements = AsyncMock(return_value=[])
        assert await server.get_announcements() == []

    async def test_get_timeslot_reservation(self, mock_client):
        mock_client.delivery.get_timeslot_reservation = AsyncMock(return_value={"slot": "today"})
        assert await server.get_timeslot_reservation() == {"slot": "today"}

    async def test_get_account_overview_composes_and_omits_login(self, mock_client):
        mock_client.delivery.get_info = AsyncMock(return_value={"d": 1})
        mock_client.orders.get_next = AsyncMock(return_value={"n": 1})
        mock_client.orders.get_last = AsyncMock(return_value=None)
        mock_client.cart.get_content = AsyncMock(
            return_value=Cart(total_price=5.0, total_items=1, can_make_order=True, products=[])
        )
        mock_client.account.get_premium_profile = AsyncMock(return_value={"active": True})
        mock_client.account.get_bags_info = AsyncMock(return_value={"count": 0})
        mock_client.account.get_announcements = AsyncMock(return_value=[])
        mock_client.delivery.get_timeslot_reservation = AsyncMock(return_value=None)
        mock_client.delivery.get_next_slots = AsyncMock(return_value={"slot": "today"})

        result = await server.get_account_overview()

        assert "login" not in result
        assert "delivered_orders" not in result
        assert result["cart"]["total_price"] == 5.0
        assert result["premium_profile"] == {"active": True}
        assert result["last_order"] is None

    async def test_get_account_overview_surfaces_section_error(self, mock_client):
        mock_client.delivery.get_info = AsyncMock(return_value={"d": 1})
        mock_client.orders.get_next = AsyncMock(return_value=None)
        mock_client.orders.get_last = AsyncMock(return_value=None)
        mock_client.cart.get_content = AsyncMock(side_effect=APIRequestFailedError("nope"))
        mock_client.account.get_premium_profile = AsyncMock(return_value=None)
        mock_client.account.get_bags_info = AsyncMock(return_value=None)
        mock_client.account.get_announcements = AsyncMock(return_value=None)
        mock_client.delivery.get_timeslot_reservation = AsyncMock(return_value=None)
        mock_client.delivery.get_next_slots = AsyncMock(return_value=None)

        result = await server.get_account_overview()
        assert result["cart"] == {"error": "nope"}

    async def test_get_delivery_addresses(self, mock_client):
        mock_client.delivery.get_addresses = AsyncMock(
            return_value={"data": [{"address": {"id": 11723996}}]}
        )
        result = await server.get_delivery_addresses()
        assert result["data"][0]["address"]["id"] == 11723996


class TestErrorHandling:
    async def test_api_error_returns_error_dict(self, mock_client):
        mock_client.cart.get_content = AsyncMock(side_effect=APIRequestFailedError("nope"))
        result = await server.get_cart()
        assert result == {"error": "nope"}


class TestAuth:
    def test_no_token_means_no_auth(self, monkeypatch):
        monkeypatch.delenv("ROHLIK_MCP_AUTH_TOKEN", raising=False)
        assert server._build_auth() is None

    def test_empty_token_means_no_auth(self, monkeypatch):
        monkeypatch.setenv("ROHLIK_MCP_AUTH_TOKEN", "")
        assert server._build_auth() is None

    def test_token_builds_verifier(self, monkeypatch):
        from fastmcp.server.auth import StaticTokenVerifier

        monkeypatch.setenv("ROHLIK_MCP_AUTH_TOKEN", "secret-xyz")
        verifier = server._build_auth()
        assert isinstance(verifier, StaticTokenVerifier)
        assert "secret-xyz" in verifier.tokens


class TestServerMetadata:
    async def test_tools_are_registered(self):
        async with Client(server.mcp) as client:
            tools = await client.list_tools()
        names = {tool.name for tool in tools}
        assert "search_products" in names
        assert "get_cart" in names
        assert "add_to_cart" in names
        assert "search_recipes" in names
        assert "get_account_overview" in names
        # newly added tools
        assert "get_product_detail" in names
        assert "get_product_categories" in names
        assert "get_order_detail" in names
        # consolidated/removed tools
        assert "add_items_to_cart" not in names
        assert "get_product_price" not in names
        assert "get_timeslot_reservation" in names
        assert "get_premium_profile" in names
        assert "get_bags_info" in names
        assert "get_announcements" in names
        assert "get_product_cards" in names
        assert "get_weekly_sales" in names
        assert "get_delivery_addresses" in names

    async def test_call_tool_in_memory(self, mock_client):
        """End-to-end: invoke a tool through the FastMCP client."""
        mock_client.cart.get_content = AsyncMock(
            return_value=Cart(total_price=12.5, total_items=0, can_make_order=False, products=[])
        )
        async with Client(server.mcp) as client:
            result = await client.call_tool("get_cart", {})
        assert result.data["total_price"] == 12.5
