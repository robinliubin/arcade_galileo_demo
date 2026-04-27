# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Demo showcasing **MCP tool calls via Arcade, observed in Galileo through standard OpenTelemetry/OTLP**. The three pieces a future session needs to hold in mind:

- **MCP (Model Context Protocol)** — the transport: the demo's LLM/agent reaches tools through Arcade's MCP runtime.
- **Arcade** (arcade.dev) — the tool provider: supplies pre-authenticated integrations (Gmail + Google Docs in this demo) exposed as MCP servers, so the demo can call real third-party APIs without building its own auth/OAuth layer.
- **Galileo** (rungalileo.io) — the observability layer: traces flow through `galileo.otel.GalileoSpanProcessor` (the supported Galileo OTel integration), which handles OTLP exporter setup, cluster routing via `GALILEO_CONSOLE_URL`, and project/log-stream metadata internally. LangChain spans come from `openinference.instrumentation.langchain.LangChainInstrumentor`; manual workflow + Arcade tool spans are added on top via the OpenTelemetry SDK. **Do not** use the older `galileo.openai` chat-client wrapper — that's the previous demo's pattern.

The point of the demo is the *join* of these three using the canonical Galileo OTel processor (no manual `OTLPSpanExporter` construction, no manual header munging) and OpenInference semantic conventions so the trace renders natively in Galileo's UI.

## Stack

- **Python 3.12** (pinned via `.python-version`).
- **`uv`** for Python toolchain, venv, and dependencies — never invoke `pip` or `python -m venv` directly. `uv.lock` is committed and authoritative.
- Deps live in `pyproject.toml` under `[project] dependencies`. Add deps with `uv add <pkg>`, remove with `uv remove <pkg>`. Never edit `uv.lock` by hand.
- `[tool.uv] package = false` — script project, not a library. Don't add a `[build-system]` section.

Key libraries:
- `arcadepy` — Arcade Python SDK (the ergonomic surface over MCP).
- `langchain-openai` — `ChatOpenAI` chat model + `.bind_tools(...)` for OpenAI-format tool schemas from Arcade.
- `openinference-instrumentation-langchain` — auto-instruments every `ChatOpenAI` invocation with OpenInference span attributes (`llm.input_messages`, `llm.output_messages`, token counts) that Galileo recognizes natively.
- `galileo` — provides `galileo.otel.GalileoSpanProcessor` and `galileo_context.init(...)`, the supported Galileo OTel integration surface. The processor wraps the OTLP exporter and handles cluster routing via `GALILEO_CONSOLE_URL`. **Use this, not `galileo.openai`** — the chat-client wrapper is the older pattern.
- `opentelemetry-{api,sdk}` + `opentelemetry-exporter-otlp-proto-http` — standard OTel plumbing; the GalileoSpanProcessor uses the OTLP exporter under the hood.

## Commands

- Run the demo: `uv run python workflow.py`
- Refresh env after pulling changes: `uv sync`
- Add a dependency: `uv add <pkg>` (updates `pyproject.toml` and `uv.lock` in one step)
- Upgrade all deps: `uv lock --upgrade && uv sync`

No test suite yet.

## Architecture

Two files, ~250 LOC total:

1. **`instrumentation.py`** — Side-effecting module. On import it:
   - reads `GALILEO_API_KEY`, `GALILEO_PROJECT`, `GALILEO_LOG_STREAM` from env (and `GALILEO_CONSOLE_URL` if set, for non-default clusters);
   - calls `galileo_context.init(project=..., log_stream=...)` to bootstrap the project + log stream and authenticate against the cluster derived from `GALILEO_CONSOLE_URL`;
   - registers a global `TracerProvider`, attaches `galileo.otel.GalileoSpanProcessor(project=..., logstream=...)` via `otel.add_galileo_span_processor(provider, processor)` — the processor handles OTLP exporter setup, endpoint resolution, and routing headers internally;
   - calls `LangChainInstrumentor().instrument(tracer_provider=...)` so future `ChatOpenAI` invocations auto-emit OpenInference-shaped spans;
   - exports a `tracer` for manual spans (workflow root, Arcade tool spans).
2. **`workflow.py`** — The agent entry point. The very first import is `from instrumentation import tracer`, which runs the side effects above *before* any LangChain class is constructed. Then: `validate_environment()` → `load_arcade_tools()` (Gmail + Google Docs schemas in OpenAI format) → `create_agent(tools)` (returns `ChatOpenAI(...).bind_tools(tools)`) → `execute_workflow(...)` (multi-round loop bound by `MAX_WORKFLOW_ROUNDS = 5`, wrapped in an `arcade_galileo_workflow` span, with each Arcade call wrapped in `arcade.execute.<tool_name>`).

Trace shape in Galileo:

```
arcade_galileo_workflow                       (WorkflowSpan, typed)
├── ChatOpenAI                                (OpenInference, auto)
├── Gmail_ListEmailsByHeader                  (ToolSpan, typed)
├── ChatOpenAI
├── GoogleDocs_CreateDocumentFromText         (ToolSpan)
├── ChatOpenAI
├── Gmail_SendEmail                           (ToolSpan)
└── ChatOpenAI                                (final, no tool_calls)
```

Critical: the workflow + tool spans use Galileo's typed schemas (`WorkflowSpan`, `ToolSpan` from `galileo_core.schemas.logging.span`) wrapped in `galileo.otel.start_galileo_span(...)`. Generic OTel spans created via `tracer.start_as_current_span(...)` get rendered as Workflow spans regardless of name — only typed spans surface as Tool / Retriever spans in the UI.

Deliberate non-choices (preserve when extending):

- **`galileo.otel.GalileoSpanProcessor`, not a hand-rolled `OTLPSpanExporter`.** The processor is the supported integration surface — it handles cluster routing via `GALILEO_CONSOLE_URL`, header construction, and the underlying OTLP exporter. Hand-rolling those (with `OTEL_EXPORTER_OTLP_TRACES_HEADERS` and a hardcoded endpoint) breaks on non-SaaS clusters and is what the early version of this demo got wrong.
- **`galileo_context.init(...)`, not the `galileo.openai` chat-client wrapper.** Both are in the `galileo` package; the OTel-native processor is the supported path here. Do not reintroduce `from galileo.openai import OpenAI` — that's the older "wrap the chat client" pattern this demo replaces.
- **`arcadepy` SDK, not raw MCP.** Arcade is the MCP runtime; the SDK is the MCP path. If a customer asks for SEP-2448 server-side span passback (`_meta.otel.traces.resourceSpans`), that's a sibling demo (`ArcadeAI/arcade-mcp` PR #797 reference impl), not a rewrite of `workflow.py`.
- **LangChain `ChatOpenAI` + OpenInference instrumentor.** This is the framework the demo bakes in. Other LangChain chat models (`ChatAnthropic`, `ChatVertexAI`) work transparently — the OpenInference instrumentor is provider-agnostic — but swapping out LangChain itself means swapping out the OpenInference instrumentor too.
- **Side-effecting `instrumentation` import.** Importing the module is what installs the OTLP exporter and the LangChain instrumentor. Do not refactor it into an `init()` function unless `workflow.py` calls it explicitly *before* any LangChain class is constructed; otherwise the patch is too late and no LLM spans fire.
- **Manual workflow root span.** `LangChainInstrumentor` produces one span per `ChatOpenAI` invocation but no parent — the manual `arcade_galileo_workflow` span is what gives Galileo a single root to anchor the agent trajectory.

## Customer-requirement caveat

This demo is **client-side OpenTelemetry instrumentation exporting via OTLP to Galileo**. It captures the LLM's tool-selection decisions (because `LangChainInstrumentor` traces every model invocation) and Arcade tool execution input/output (via the manual `arcade.execute.*` spans). It does **not** capture Arcade's *internal server-side* execution stages (auth checks, middleware decisions, elicitation flows, tool reduction). Those would require either:

- SEP-2448 (`serverExecutionTelemetry`) span passback — the MCP server returns its internal spans inline via `_meta.otel`, the client forwards them to Galileo. Reference impl: `examples/mcp_servers/telemetry_passback/` in `ArcadeAI/arcade-mcp` (PR #797, merged).
- Direct OTLP egress from Arcade Cloud to a customer's Galileo collector — not currently exposed in the Arcade dashboard as of writing.

When customers ask for "server-side Arcade telemetry in Galileo", clarify which of the above they actually need before pointing them at this demo.

## Extending the demo

- **Different toolkit**: edit `REQUIRED_ARCADE_TOOLS` in `workflow.py` and adjust the `user_query` in `execute_workflow`. `load_arcade_tools()` derives the toolkit name from the underscore split of the tool name (`Slack_SendMessage` → toolkit `slack`), so adding new tools is one line per tool. Gmail + Google Docs require Google OAuth — first execute returns an authorization URL the user visits once per `ARCADE_USER_ID`. No-auth toolkits (math, search) skip that.
- **Different LLM**: swap `ChatOpenAI` for any other LangChain chat model that supports `bind_tools`. The OpenInference spans still flow to Galileo unchanged.
- **Different observability backend**: replace `otel.add_galileo_span_processor(...)` in `instrumentation.py` with a vanilla `BatchSpanProcessor(OTLPSpanExporter(endpoint=..., headers=...))` pointing at Jaeger / Honeycomb / Tempo / etc. The OpenInference instrumentation and the manual `arcade.execute.*` spans flow unchanged — only the processor swaps.
