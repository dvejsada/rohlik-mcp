# 🛒 Rohlik MCP Server

**Do your Rohlik.cz grocery shopping by just talking to your AI assistant.**

[![Python](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-georgx22%2Frohlik--mcp-2496ED.svg?logo=docker&logoColor=white)](https://hub.docker.com/r/georgx22/rohlik-mcp)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

This is a [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server
that connects [Rohlik.cz](https://www.rohlik.cz) to AI assistants like Claude
Desktop, your IDE, and any other MCP-compatible app. Search products, build your
cart, plan meals from recipes, and check your orders and deliveries — all in
plain language.

Under the hood it's a thin wrapper around the
[`rohlik-api`](https://pypi.org/project/rohlik-api/) package, built with
[FastMCP](https://gofastmcp.com/) and served over the **Streamable HTTP**
transport.

---

## 💬 What you can ask

Once it's connected, talk to your assistant naturally:

> *"Search Rohlik for oat milk and add the cheapest one to my cart."*

> *"What's in my cart right now, and what's the total?"*

> *"Find a pasta recipe and add all its ingredients to my basket."*

> *"When's my next delivery, and what was in my last order?"*

> *"Show me the nutrition and allergens for this product."*

---

## 🚀 Quick start (Docker)

The fastest way to get running — just plug in your Rohlik.cz login:

```bash
docker run --rm -p 8000:8000 \
  -e ROHLIK_USERNAME="your_email@example.com" \
  -e ROHLIK_PASSWORD="your_password" \
  georgx22/rohlik-mcp
```

Your server is now live at **`http://localhost:8000/mcp/`**. That's it. 🎉

> 🔒 **Your credentials never leave your machine.** They're passed straight to
> Rohlik.cz from your own container.

---

## 🔌 Connect your MCP client

Point any client that speaks Streamable HTTP at the endpoint:

```json
{
  "mcpServers": {
    "rohlik": {
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

Using a stdio-only client (like some Claude Desktop setups)? Bridge to it with
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

### 🔑 Authentication (optional)

By default the endpoint is **unauthenticated** — anyone who can reach it can use
the tools (including the ones that change your real cart). If the server is
reachable beyond your own machine, set **`ROHLIK_MCP_AUTH_TOKEN`** to a long
random string; clients must then send it as a bearer token. Use this config
**instead of** the plain one above:

```json
{
  "mcpServers": {
    "rohlik": {
      "url": "http://localhost:8000/mcp/",
      "headers": { "Authorization": "Bearer your-token-here" }
    }
  }
}
```

> The `headers` field is supported by Streamable-HTTP-capable clients. On
> stdio-only clients (e.g. via `mcp-remote`), pass the header the way that tool
> documents it instead.

> The static token is a lightweight gate suitable for a personal, self-hosted
> server. For anything internet-exposed, also put it behind TLS / a reverse
> proxy.

---

## 🧰 What it can do

**28 tools**, grouped by what they touch:

### 🥑 Products
| Tool | What it does |
| ---- | ------------ |
| `search_products` | Search the catalogue by name |
| `get_product_price` | Current price and price-per-unit |
| `get_product_composition` | Nutrition, ingredients and allergens |
| `get_product_ai_summary` | AI-generated product summary |
| `get_product_detail` | Full product detail |
| `get_product_categories` | Category breadcrumb for a product |
| `get_product_cards` | Basic info for several products at once |
| `get_weekly_sales` | This week's deals ("Akce týdne"), enriched |

### 🛒 Cart
| Tool | What it does |
| ---- | ------------ |
| `get_cart` | Current cart contents and total |
| `add_to_cart` | Add a single product |
| `add_items_to_cart` | Add several products in one call |
| `remove_from_cart` | Remove an item |

### 👩‍🍳 Recipes (Rohlík Chef)
| Tool | What it does |
| ---- | ------------ |
| `search_recipes` | Search recipes by name |
| `get_recipe_detail` | Full recipe with ingredients and directions |
| `get_ingredient_products` | Shoppable products for a recipe's ingredients |

### 📦 Orders
| Tool | What it does |
| ---- | ------------ |
| `get_next_order` | Your next upcoming order |
| `get_last_order` | Your most recent delivered order |
| `get_delivered_orders` | Your order history |
| `get_order_detail` | Full detail of a single order by ID |

### 🚚 Delivery
| Tool | What it does |
| ---- | ------------ |
| `get_delivery_info` | First-delivery information |
| `get_delivery_addresses` | Saved delivery addresses |
| `get_next_delivery_slots` | Next available delivery slots |
| `get_timeslot_reservation` | Your currently reserved slot |

### 👤 Account
| Tool | What it does |
| ---- | ------------ |
| `get_shopping_list` | A saved shopping list by ID |
| `get_account_overview` | One-call snapshot: cart, orders, delivery & more |
| `get_premium_profile` | Premium (membership) profile |
| `get_bags_info` | Reusable shopping bags |
| `get_announcements` | Account announcements |

> ⚠️ **`add_to_cart`, `add_items_to_cart` and `remove_from_cart` change your
> real Rohlik cart.** Everything else is read-only.

---

## ⚙️ Configuration

Everything is configured through environment variables:

| Variable | Required | Default | Description |
| -------- | :------: | ------- | ----------- |
| `ROHLIK_USERNAME` | ✅ | — | Rohlik.cz account email |
| `ROHLIK_PASSWORD` | ✅ | — | Rohlik.cz account password |
| `ROHLIK_BASE_URL` | | `https://www.rohlik.cz` | API base URL |
| `ROHLIK_TIMEOUT` | | `30` | Request timeout (seconds) |
| `ROHLIK_MCP_HOST` | | `0.0.0.0` | HTTP bind host |
| `ROHLIK_MCP_PORT` | | `8000` | HTTP bind port |
| `ROHLIK_MCP_PATH` | | `/mcp/` | HTTP endpoint path |
| `ROHLIK_MCP_AUTH_TOKEN` | | — | Bearer token clients must present; auth is disabled when unset |

---

## 🛠️ Running from source

Prefer Python over Docker? You'll need **Python 3.13+** and a Rohlik.cz account.

```bash
git clone https://github.com/dvejsada/rohlik-mcp
cd rohlik-mcp
pip install -e ".[dev]"

export ROHLIK_USERNAME="your_email@example.com"
export ROHLIK_PASSWORD="your_password"
rohlik-mcp          # or: python -m rohlik_mcp
```

You can also build the Docker image yourself:

```bash
docker build -t rohlik-mcp .
```

---

## 🧪 Development

```bash
pip install -e ".[dev]"
pytest                 # run the test suite
ruff check .           # lint
black --check .        # formatting
mypy rohlik_mcp        # type checks
```

---

## 📄 License & disclaimer

Released under the [MIT License](LICENSE).

> **Unofficial project.** It uses the non-public Rohlik.cz API and is not
> affiliated with, authorised by, or endorsed by Rohlik.cz. Use at your own
> discretion.
