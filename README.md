# arcade_galileo_demo

This demo showcases **MCP tool calls via [Arcade](https://arcade.dev), observed in [Galileo](https://rungalileo.io) through standard OpenTelemetry/OTLP** — using LangChain on the agent side and OpenInference auto-instrumentation, so no Galileo SDK imports are needed in the application code.

> **Presenting this demo?** Start with [`docs/running-the-demo.md`](docs/running-the-demo.md) — it's the full runbook with troubleshooting. [`docs/architecture.md`](docs/architecture.md) and [`docs/call-flow.md`](docs/call-flow.md) are the companion explainers.

## source:

  - https://github.com/ArcadeAI/arcade-mcp/pull/797
  - https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2448
  - https://www.loom.com/share/d5c79df7396a48668782b1eb5c415ec6

## What's happening

- The **LLM** decides which tool to call. It's `gpt-4o`, driven by `langchain_openai.ChatOpenAI(...).bind_tools(arcade_tools)`.
- **Arcade** is an MCP runtime + auth broker. Its Python SDK (`arcadepy`) is the MCP path — `arcade.tools.execute(...)` is an MCP tool call. This demo uses three OAuth-backed tools: `Gmail_ListEmailsByHeader`, `GoogleDocs_CreateDocumentFromText`, `Gmail_SendEmail`.
- **Galileo** is wired up via `galileo.otel.GalileoSpanProcessor` — the supported Galileo OTel integration. `instrumentation.py` calls `galileo_context.init(...)` to bootstrap the project and log stream, then attaches `GalileoSpanProcessor` to a standard OTel `TracerProvider`. The processor handles OTLP exporter setup, cluster routing via `GALILEO_CONSOLE_URL`, and routing headers internally — no manual endpoint or header munging in app code. LangChain LLM spans are auto-instrumented via `openinference.instrumentation.langchain`.

Two files (~250 LOC total):

| File | What it does |
|---|---|
| `instrumentation.py` | Side-effecting module: validates env, configures OTLP exporter, installs `LangChainInstrumentor`, exports `tracer` for manual spans. |
| `workflow.py` | Validates env, loads Arcade tools, creates the LangChain agent, runs the multi-round loop wrapped in an `arcade_galileo_workflow` span with per-Arcade-call sub-spans. |

## Prereqs

- [`uv`](https://docs.astral.sh/uv/) — Python project/package manager. Install: `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- API keys for OpenAI, Arcade, and Galileo (see below).
- A Google account willing to OAuth-authorize Gmail (read + send) and Google Docs (create) for the `ARCADE_USER_ID` you choose.

`uv` handles the Python toolchain, venv, and dependencies — you don't need to install Python or manage a venv by hand. The project pins Python 3.12 via `.python-version`; `uv` will fetch it automatically if missing.

### Getting the API keys

**OpenAI** → `OPENAI_API_KEY`

1. Sign up or log in at https://platform.openai.com
2. Add a payment method at https://platform.openai.com/account/billing — new accounts without billing can't call `gpt-4o`.
3. Go to https://platform.openai.com/api-keys → **Create new secret key** → copy the `sk-...` value.

**Arcade** → `ARCADE_API_KEY`

1. Sign up at https://api.arcade.dev/dashboard/register
2. Go to https://api.arcade.dev/dashboard/api-keys → **Create API Key** → copy the `arc_...` value.
3. Tip: project-scoped keys (prefix `arc_proj...`) are revocable without affecting other projects — prefer them.

**Galileo** → `GALILEO_API_KEY`

1. Sign up at https://app.galileo.ai/sign-up.
2. In the console, go to **Settings → API Keys** (https://app.galileo.ai/settings/api-keys).
3. Create a key, copy the value, paste into `.env`.
4. If you're on a non-default cluster (dev / staging / demo-v2 / self-hosted), set `GALILEO_CONSOLE_URL` in `.env` to that cluster's console URL (e.g. `https://console.demo-v2.galileocloud.io/`). The `galileo` SDK and `GalileoSpanProcessor` derive the OTLP endpoint from there — no separate endpoint override needed.

## Run it

```bash
cp .env.example .env
# ...then fill in OPENAI_API_KEY, ARCADE_API_KEY, ARCADE_USER_ID,
# GALILEO_API_KEY, GALILEO_PROJECT
# (and GALILEO_CONSOLE_URL if you're on a non-default cluster)

uv run python workflow.py
```

On first run, `uv` creates `.venv/` and installs from `uv.lock`. On the *very* first execute of a Gmail or Docs tool, Arcade returns an authorization URL — open it, complete Google's consent flow, then re-run. Subsequent runs reuse the cached token for that `ARCADE_USER_ID`.

Expected console output:

```
============================================================
Arcade + Galileo Integration Demo
============================================================

Loaded 3 tools: Gmail_ListEmailsByHeader, GoogleDocs_CreateDocumentFromText, Gmail_SendEmail
Executing workflow...

============================================================
Workflow completed successfully!
============================================================

Result:
I summarized your 3 most recent Arcade emails into a Google Doc...

✓ View traces at: https://app.galileo.ai
  Project:    arcade-galileo-demo
  Log stream: default
```

## See the trace in Galileo

Open Galileo UI → project `arcade-galileo-demo` → log stream `default`. You should see one trace shaped like:

- **`arcade_galileo_workflow`** — the manual workflow root (you set this in `workflow.py`).
- Multiple **`ChatOpenAI`** spans — auto-emitted by `LangChainInstrumentor`, with OpenInference attributes (`llm.input_messages`, `llm.output_messages`, `llm.token_count.prompt/completion`).
- **Tool spans** named after the Arcade tool (e.g. `Gmail_ListEmailsByHeader`) — one per Arcade call. Created via `ToolSpan(name=..., input=..., tool_call_id=...)` + `galileo.otel.start_galileo_span(...)` so Galileo's UI renders them with the proper Tool icon and drill-down (not as generic Workflow spans).

See [`docs/call-flow.md`](docs/call-flow.md) for the full sequence and trace tree.

## What about *server-side* Arcade telemetry?

This demo captures **client-side** spans (the agent's LLM calls + Arcade tool inputs/outputs as observed by the client). It does *not* capture Arcade's internal server-side stages (auth checks, middleware decisions, tool reduction, elicitation flows) — those live behind the MCP boundary.

If your audience needs that visibility, the MCP `serverExecutionTelemetry` capability ([SEP-2448](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2448)) addresses it: the MCP server passes its internal spans back inline via `_meta.otel`, and the client forwards them to Galileo. A reference implementation lives in `ArcadeAI/arcade-mcp` (`examples/mcp_servers/telemetry_passback/`, [PR #797 merged](https://github.com/ArcadeAI/arcade-mcp/pull/797)). That's a sibling demo; this one is the standard-OTLP-from-the-agent story.

## Swapping the toolkit

Edit `REQUIRED_ARCADE_TOOLS` in `workflow.py` and adjust the `user_query` in `execute_workflow`. No-auth toolkits (math, search) skip the OAuth phase. Other OAuth toolkits (Slack, GitHub-private) follow the same first-run-returns-URL pattern as Gmail/Docs.
