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

The server holds **no credentials of its own** — each client sends its own
Rohlik.cz login with every request, so one server can serve several people:

```bash
docker run --rm -p 8000:8000 georgx22/rohlik-mcp
```

Your server is now live at **`http://localhost:8000/mcp/`**. That's it. 🎉

> 🔒 **Your credentials never leave your machine.** They travel from your MCP
> client to your own container, and from there straight to Rohlik.cz. Nothing is
> stored on disk.

---

## 🔌 Connect your MCP client

Point any client that speaks Streamable HTTP at the endpoint, and send your
Rohlik.cz login as two headers:

```json
{
  "mcpServers": {
    "rohlik": {
      "url": "http://localhost:8000/mcp/",
      "headers": {
        "X-Rohlik-Username": "your_email@example.com",
        "X-Rohlik-Password": "your_password"
      }
    }
  }
}
```

Using a stdio-only client (like some Claude Desktop setups)? Bridge to it with
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote), passing the headers the
way that tool documents:

```json
{
  "mcpServers": {
    "rohlik": {
      "command": "npx",
      "args": [
        "mcp-remote", "http://localhost:8000/mcp/",
        "--header", "X-Rohlik-Username:your_email@example.com",
        "--header", "X-Rohlik-Password:your_password"
      ]
    }
  }
}
```

The server logs in once per account and reuses that session for every following
tool call, so a conversation costs one login rather than one per call. A session
left unused for 15 minutes is logged out and discarded; the next call simply logs
in again.

### 🧑‍🤝‍🧑 LibreChat (per-user credentials)

Because credentials arrive per request, LibreChat's
[`customUserVars`](https://www.librechat.ai/docs/features/mcp) let **each user
supply their own Rohlik.cz login** from the MCP settings panel — nothing is baked
into the config. In `librechat.yaml`:

```yaml
mcpServers:
  rohlik:
    type: streamable-http
    url: "http://rohlik-mcp:8000/mcp/"   # Docker service name, shared network
    headers:
      X-Rohlik-Username: "{{ROHLIK_USERNAME}}"
      X-Rohlik-Password: "{{ROHLIK_PASSWORD}}"
      Authorization: "Bearer ${ROHLIK_MCP_AUTH_TOKEN}"
    customUserVars:
      ROHLIK_USERNAME:
        title: "Rohlik.cz e-mail"
        description: "The e-mail you sign in to rohlik.cz with."
      ROHLIK_PASSWORD:
        title: "Rohlik.cz password"
        description: "Your rohlik.cz password. LibreChat stores it encrypted."
    timeout: 60000
```

Servers declaring `customUserVars` are excluded from LibreChat's app-level
connections automatically, so it won't try to connect before a user has filled
theirs in. LibreChat re-resolves the headers before each tool call, so changing
your password takes effect on the next message.

### 🔑 Authentication (optional)

The credential headers say *which account* to act on; they don't say who may
reach the server at all. By default the endpoint is **open** — anything that can
reach it can drive the tools with any credentials it likes. Set
**`ROHLIK_MCP_AUTH_TOKEN`** to a long random string and clients must also present
it as `Authorization: Bearer <token>`, as in the LibreChat example above.

> Worth setting whenever the server is reachable by anything other than your own
> MCP client — including other containers on a shared Docker network.

### 🐳 Alongside LibreChat on one Docker network

`docker-compose.yml` in this repo attaches the server to an external network and
**does not publish a port**: LibreChat reaches it as `http://rohlik-mcp:8000/`
by service name, and passwords stay on the Docker bridge rather than on
`localhost:8000` where anything on the host could reach them. Publish the port
only if you need a client outside Docker, and set `ROHLIK_MCP_AUTH_TOKEN` if you
do.

---

## 🧰 What it can do

**28 tools**, grouped by what they touch. Lookups that take an ID accept a
**list** of IDs and run in one call — fewer round-trips, same result. Most by-ID
lookups return a map of **ID → result** (the IDs are JSON object keys, so always
strings, e.g. `"123"`; an ID is `null` when nothing was found for it). The
exception is `get_product_cards`, which returns a **list** of product cards.

### 🥑 Products
| Tool | What it does |
| ---- | ------------ |
| `search_products` | Search the catalogue by name (optionally favourites only) |
| `get_product_cards` | Name, price, sale and stock for several products — the go-to for pricing |
| `get_product_composition` | Nutrition, ingredients and allergens (one or more products) |
| `get_product_ai_summary` | AI-generated product summary (one or more products) |
| `get_product_detail` | Trimmed product detail (one or more products) |
| `get_product_categories` | Category breadcrumb (one or more products) |
| `get_weekly_sales` | This week's deals ("Akce týdne"), enriched |

### 🛒 Cart
| Tool | What it does |
| ---- | ------------ |
| `get_cart` | Current cart contents and total |
| `add_to_cart` | Add one or more products in one call |
| `remove_from_cart` | Remove one or more items in one call |

### 👩‍🍳 Recipes (Rohlík Chef)
| Tool | What it does |
| ---- | ------------ |
| `search_recipes` | Search recipes by name |
| `get_recipe_detail` | Full recipe(s) with ingredients and directions |
| `get_ingredient_products` | Shoppable products for a recipe's ingredients |

### 📦 Orders
| Tool | What it does |
| ---- | ------------ |
| `get_next_order` | Your next upcoming order |
| `get_last_order` | Your most recent delivered order |
| `get_delivered_orders` | Your order history |
| `get_order_detail` | Full detail of one or more orders by ID |

### 🚚 Delivery
| Tool | What it does |
| ---- | ------------ |
| `get_delivery_info` | First-delivery information |
| `get_delivery_addresses` | Saved delivery addresses |
| `get_next_delivery_slots` | Next available delivery slots |
| `get_timeslot_reservation` | Your currently reserved slot |
| `track_delivery` | Live tracking: status, courier ETA, stops before yours, courier distance |
| `get_delivery_announcements` | Delivery announcements incl. the live delivery-time ETA |

### 👤 Account
| Tool | What it does |
| ---- | ------------ |
| `get_shopping_list` | One or more saved shopping lists by ID |
| `get_account_overview` | Trimmed one-call snapshot: cart, orders, delivery & more |
| `get_premium_profile` | Premium (membership) profile |
| `get_bags_info` | Reusable shopping bags |
| `get_announcements` | Account announcements |

> ⚠️ **`add_to_cart` and `remove_from_cart` change your real Rohlik cart.**
> Everything else is read-only.

---

## ⚙️ Configuration

Rohlik.cz credentials are **not** configured here — they come from each request's
`X-Rohlik-Username` / `X-Rohlik-Password` headers. Everything else is environment
variables:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `ROHLIK_MCP_AUTH_TOKEN` | — | Bearer token clients must present; auth is disabled when unset |
| `ROHLIK_BASE_URL` | `https://www.rohlik.cz` | API base URL |
| `ROHLIK_TIMEOUT` | `30` | Request timeout (seconds) |
| `ROHLIK_MCP_HOST` | `0.0.0.0` | HTTP bind host |
| `ROHLIK_MCP_PORT` | `8000` | HTTP bind port |
| `ROHLIK_MCP_PATH` | `/mcp/` | HTTP endpoint path |
| `ROHLIK_CLIENT_TTL` | `900` | Seconds an unused account session is kept before logging out (min 60) |
| `ROHLIK_MAX_CLIENTS` | `50` | Most account sessions held at once; the least recently used is dropped first |

---

## 🛠️ Running from source

Prefer Python over Docker? You'll need **Python 3.13+** and a Rohlik.cz account.

```bash
git clone https://github.com/dvejsada/rohlik-mcp
cd rohlik-mcp
pip install -e ".[dev]"

rohlik-mcp          # or: python -m rohlik_mcp
```

No credentials to export — the server takes them from each request.

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
