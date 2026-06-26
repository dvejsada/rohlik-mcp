"""Tests for the MCP server tools.

The tools are exercised directly with a mocked RohlikAPI client, so no network
access or credentials are required.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client
from rohlik_api import APIRequestFailedError, Cart, CartItem, ProductPrice

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
    async def test_get_product_price_serializes_model(self, mock_client):
        mock_client.products.get_price = AsyncMock(
            return_value=ProductPrice(
                product_id=123, price=40.9, currency="CZK", price_per_unit=340.83
            )
        )
        result = await server.get_product_price(123)
        assert result == {
            "product_id": 123,
            "price": 40.9,
            "currency": "CZK",
            "price_per_unit": 340.83,
            "sales": [],
        }

    async def test_none_result_returns_message(self, mock_client):
        mock_client.products.get_price = AsyncMock(return_value=None)
        result = await server.get_product_price(123)
        assert isinstance(result, str)
        assert "No data" in result


class TestCartTools:
    async def test_get_cart(self, mock_client):
        mock_client.cart.get_content = AsyncMock(
            return_value=Cart(total_price=99.9, total_items=1, can_make_order=True, products=[])
        )
        result = await server.get_cart()
        assert result["total_price"] == 99.9
        assert result["total_items"] == 1

    async def test_add_to_cart_success(self, mock_client):
        mock_client.cart.add_items = AsyncMock(return_value=[123])
        result = await server.add_to_cart(123, quantity=2)
        assert result == {"added": True, "product_id": 123, "quantity": 2}

    async def test_add_to_cart_failure(self, mock_client):
        mock_client.cart.add_items = AsyncMock(return_value=[])
        result = await server.add_to_cart(123)
        assert result == {"added": False, "product_id": 123}

    async def test_remove_from_cart(self, mock_client):
        mock_client.cart.delete_item = AsyncMock()
        result = await server.remove_from_cart("f1")
        assert result == {"removed": True, "cart_item_id": "f1"}
        mock_client.cart.delete_item.assert_awaited_once_with("f1")

    async def test_remove_from_cart_error(self, mock_client):
        mock_client.cart.delete_item = AsyncMock(side_effect=APIRequestFailedError("boom"))
        result = await server.remove_from_cart("f1")
        assert result == {"error": "boom"}


class TestErrorHandling:
    async def test_api_error_returns_error_dict(self, mock_client):
        mock_client.cart.get_content = AsyncMock(side_effect=APIRequestFailedError("nope"))
        result = await server.get_cart()
        assert result == {"error": "nope"}


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

    async def test_call_tool_in_memory(self, mock_client):
        """End-to-end: invoke a tool through the FastMCP client."""
        mock_client.cart.get_content = AsyncMock(
            return_value=Cart(total_price=12.5, total_items=0, can_make_order=False, products=[])
        )
        async with Client(server.mcp) as client:
            result = await client.call_tool("get_cart", {})
        assert result.data["total_price"] == 12.5
