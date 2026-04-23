# arcade_galileo_demo

This demo showcases **MCP tool calls via [Arcade](https://arcade.dev), observed in [Galileo](https://rungalileo.io)** — using plain Python and no agent framework, so the integration pattern is easy to lift into your own codebase.

> **Presenting this demo?** Start with [`docs/running-the-demo.md`](docs/running-the-demo.md) — it's the full runbook with troubleshooting. [`docs/architecture.md`](docs/architecture.md) and [`docs/call-flow.md`](docs/call-flow.md) are the companion explainers.

## What's happening

- The **LLM** decides when to call a tool (standard OpenAI function-calling).
- **Arcade** is an MCP runtime + auth broker. Its Python SDK (`arcadepy`) is the ergonomic surface over MCP — `arcade.tools.execute(...)` is the MCP tool call. You don't need a raw MCP client to demo MCP-via-Arcade; the SDK **is** the MCP path.
- **Galileo** wraps the OpenAI client (`from galileo.openai import OpenAI`) so LLM spans log automatically, and a single `@log(span_type="tool")` decorator attaches Arcade executions to the same trace.

Everything lives in `agent.py` (~70 lines). The loop is vanilla OpenAI function-calling — drop-in portable to LangChain, LangGraph, OpenAI Agents SDK, or your own framework.

## Prereqs

- [`uv`](https://docs.astral.sh/uv/) — Python project/package manager. Install: `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- API keys for OpenAI, Arcade, and Galileo (see below).

`uv` handles the Python toolchain, venv, and dependencies — you don't need to install Python or manage a venv by hand. The project pins Python 3.12 via `.python-version`; `uv` will fetch it automatically if missing.

### Getting the API keys

**OpenAI** → `OPENAI_API_KEY`

1. Sign up or log in at https://platform.openai.com
2. Add a payment method at https://platform.openai.com/account/billing — new accounts without billing can't call `gpt-4o-mini`
3. Go to https://platform.openai.com/api-keys → **Create new secret key** → copy the `sk-...` value
4. The key is shown only once — paste it straight into `.env`

**Arcade** → `ARCADE_API_KEY`

1. Sign up at https://api.arcade.dev/dashboard/register
2. Go to https://api.arcade.dev/dashboard/api-keys → **Create API Key** → name it, copy the `arc_...` value
3. Shown only once — if lost, regenerate (Arcade stores only a hash)
4. Tip: project-scoped keys (prefix `arc_proj...`) are revocable without affecting other projects — prefer them over account-wide keys

**Galileo** → `GALILEO_API_KEY` (+ optional `GALILEO_CONSOLE_URL` for non-default clusters)

1. Sign up at https://app.galileo.ai/sign-up (or on your cluster's console URL, e.g. `https://console.demo-v2.galileocloud.io/`)
2. In the console, go to **Settings → API Keys** (direct link on the default SaaS cluster: https://app.galileo.ai/settings/api-keys)
3. Create a key, copy the value, paste into `.env`
4. If you're on a non-default cluster (dev / staging / demo-v2 / self-hosted), also set `GALILEO_CONSOLE_URL` to that cluster's console URL — the Python SDK reads it from env automatically. Omit for the default SaaS cluster

## Run it

```bash
cp .env.example .env
# ...then fill in the three API keys in .env

uv run python agent.py
```

On first run, `uv` creates `.venv/` and installs from `uv.lock`. Subsequent runs are instant.

Expected console output: a natural-language answer that required the Arcade Math tool (e.g., arithmetic result).

## See the trace in Galileo

Open Galileo UI → project `arcade-galileo-demo` → log stream `dev`. You should see one trace containing:

- an **LLM span** with the prompt, the model's tool_call decision, and the final answer,
- one or more **tool spans** (one per Arcade call), with input/output captured, linked to the LLM call via `tool_call_id`.

## Swapping the toolkit

The demo uses Arcade's `math` toolkit because it's no-auth — `uv run python agent.py` just works. To showcase Arcade's managed-OAuth superpower (Gmail, Slack, GitHub private repos, etc.), change `TOOLKIT` in `agent.py` and the prompt. On first run, Arcade will return an authorization URL the user must visit once; subsequent runs reuse the cached token for that `USER_ID`.
