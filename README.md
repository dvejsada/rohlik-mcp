# Rohlik MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server that
exposes the [Rohlik.cz](https://www.rohlik.cz) online grocery API as tools for
LLM clients such as Claude Desktop, IDEs and other MCP-compatible apps.

It is a thin wrapper around the [`rohlik-api`](https://github.com/dvejsada/rohlik_api_python)
package — all API logic lives there; this repository only maps the API onto MCP
tools. Built with [FastMCP](https://gofastmcp.com/) and served over the
**Streamable HTTP** transport.

> **Disclaimer:** Unofficial, uses the non-public Rohlik.cz API, and is not
> affiliated with or endorsed by Rohlik.cz.

## Requirements

- Python 3.13+ (or Docker)
- A Rohlik.cz account

## Configuration

Credentials and the HTTP bind settings are read from environment variables:

| Variable           | Required | Default                  | Description                  |
| ------------------ | -------- | ------------------------ | ---------------------------- |
| `ROHLIK_USERNAME`  | yes      | —                        | Rohlik.cz account email      |
| `ROHLIK_PASSWORD`  | yes      | —                        | Rohlik.cz account password   |
| `ROHLIK_BASE_URL`  | no       | `https://www.rohlik.cz`  | API base URL                 |
| `ROHLIK_TIMEOUT`   | no       | `30`                     | Request timeout (seconds)    |
| `ROHLIK_MCP_HOST`  | no       | `0.0.0.0`                | HTTP bind host               |
| `ROHLIK_MCP_PORT`  | no       | `8000`                   | HTTP bind port               |
| `ROHLIK_MCP_PATH`  | no       | `/mcp/`                  | HTTP endpoint path           |

## Running with Docker (recommended)

Pull the published image from Docker Hub:

```bash
docker run --rm -p 8000:8000 \
  -e ROHLIK_USERNAME="your_email@example.com" \
  -e ROHLIK_PASSWORD="your_password" \
  georgx22/rohlik-mcp
```

The Streamable HTTP endpoint is then available at `http://localhost:8000/mcp/`.

To build the image locally instead:

```bash
docker build -t rohlik-mcp .
```

## Running from source

```bash
git clone https://github.com/dvejsada/rohlik-mcp
cd rohlik-mcp
pip install -e ".[dev]"

export ROHLIK_USERNAME="your_email@example.com"
export ROHLIK_PASSWORD="your_password"
rohlik-mcp          # or: python -m rohlik_mcp
```

## Connecting a client

Point any MCP client that supports Streamable HTTP at the endpoint URL:

```json
{
  "mcpServers": {
    "rohlik": {
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

For stdio-only clients, bridge to the HTTP endpoint with
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "rohlik": {
      "command": "npx",
      "args": ["mcp-remote", "http://localhost:8000/mcp/"]
    }
  }
}
```

## Tools

| Tool                       | Description                                          |
| -------------------------- | ---------------------------------------------------- |
| `search_products`          | Search products by name                              |
| `get_product_price`        | Current price and price-per-unit for a product       |
| `get_product_composition`  | Nutritional values, ingredients and allergens        |
| `get_product_ai_summary`   | AI-generated product summary                          |
| `get_product_detail`       | Full product detail                                   |
| `get_product_categories`   | Category breadcrumb for a product                     |
| `get_cart`                 | Current cart contents and total                       |
| `add_to_cart`              | Add a product to the cart                             |
| `add_items_to_cart`        | Add several products to the cart in one call          |
| `remove_from_cart`         | Remove an item from the cart                          |
| `search_recipes`           | Search recipes (Rohlík Chef)                          |
| `get_recipe_detail`        | Full recipe with ingredients and directions           |
| `get_ingredient_products`  | Purchasable products for recipe ingredients           |
| `get_next_order`           | Next upcoming order                                   |
| `get_last_order`           | Most recent delivered order                           |
| `get_delivered_orders`     | History of delivered orders                           |
| `get_order_detail`         | Full detail of a single order by ID                   |
| `get_delivery_info`        | First-delivery information                            |
| `get_next_delivery_slots`  | Next available delivery slots                         |
| `get_timeslot_reservation` | Currently reserved delivery timeslot                  |
| `get_shopping_list`        | A saved shopping list by ID                           |
| `get_account_overview`     | Aggregated account snapshot (cart, orders, delivery)  |
| `get_premium_profile`      | Premium (membership) profile                          |
| `get_bags_info`            | Reusable shopping bags information                    |
| `get_announcements`        | Account announcements                                 |

`add_to_cart`, `add_items_to_cart` and `remove_from_cart` modify your real
Rohlik cart.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
black --check .
mypy rohlik_mcp
```

## License

MIT License - see LICENSE file for details.
