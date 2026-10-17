# Workshop Parts MCP Server

MCP server for the spare parts store of a maintenance workshop, plus a small ReAct agent that uses it.

## Tools

| Tool | What it does |
| --- | --- |
| `search_inventory` | SQL lookup (sqlite3) in the parts table: stock, price, storage location. |
| `calculate_tiered_price` | Price for an order quantity with marginal volume discounts: units 1-10 full price, 11-50 5 %, 51-200 10 %, above 200 15 %. |
| `append_audit_event` | Appends a JSON line to `data/audit.log`. The file is also exposed as the resource `audit://log`. |

The database `data/inventory.db` is created with 12 sample parts on the first lookup.

## Setup

```bash
git clone https://github.com/Evocthinks/mci-ic-hw1-mcp-workshop-parts.git
uv sync
```

Create a file `.env` next to `agent.py` with the data of any OpenAI-compatible endpoint.

LiteLLM proxy:

```
LLM_BASE_URL=http://localhost:4000
LLM_API_KEY=sk-your-key
LLM_MODEL=gpt-4o-mini
```

Gemini free tier (Google AI Studio):

```
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=your-ai-studio-key
LLM_MODEL=gemini-3.8-flash
```

Without uv: `python -m venv .venv`, activate it, then `pip install mcp openai python-dotenv pytest`.

## Run

```bash
uv run python agent.py
```

This starts `server.py` over stdio, runs the three demo questions and prints Thought, Action and Observation for each step. At the end it reads the resource `audit://log`. Your own question: `uv run python agent.py "How many relays are in stock?"`.

Every ReAct step is one LLM request, so the three demo questions need about 12 requests. The Gemini free tier allows 5 requests per minute per model. When the endpoint answers with HTTP 429, the agent waits for the retry delay the endpoint names and tries again (at most 3 times), so a full run takes about 3 minutes on the free tier. If the delay is longer than 90 seconds, the daily quota is used up and the agent stops with the error.

## Tests

```bash
uv run pytest
```

The tests cover the SQL lookup, the discount tiers, the audit log and the agent loop against the real server with a scripted model (no API key needed).
