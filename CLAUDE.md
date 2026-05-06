# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Demo showcasing **MCP tool calls against a local Arcade MCP server, with SEP-2448 server-execution telemetry passback stitched into Galileo through standard OpenTelemetry/OTLP**. The four pieces a future session needs to hold in mind:

- **MCP (Model Context Protocol)** — the transport: streamable HTTP between the agent and a *local* MCP server. The agent uses `mcp.ClientSession`; the server uses `arcade-mcp-server`'s `MCPApp`.
- **SEP-2448 `serverExecutionTelemetry`** — the join: the server returns its internal phase spans inline on every `tools/call` response (under `_meta.otel.traces.resourceSpans`), and the agent forwards them to Galileo. Reference impl this demo is adapted from: `examples/mcp_servers/telemetry_passback/` in `ArcadeAI/arcade-mcp` ([PR #797](https://github.com/ArcadeAI/arcade-mcp/pull/797), merged).
- **Arcade** (arcade.dev) — appears in two distinct roles: (a) the **OAuth authorization server** for the local MCP server's resource-server auth (`cloud.arcade.dev/oauth2`, JWKS validation); (b) the **Google OAuth broker** for tool-level auth (`@tool(requires_auth=Google(...))`). The agent does NOT call Arcade Cloud's tool-execution API anymore — tools execute on the local `server.py`, which is the whole reason passback works.
- **Galileo** (rungalileo.io) — the observability layer. Traces flow through `galileo.otel.GalileoSpanProcessor` (the supported Galileo OTel integration). LangChain spans come from `openinference.instrumentation.langchain.LangChainInstrumentor`; manual workflow + `ToolSpan` spans are added on top via `galileo.otel.start_galileo_span(...)`. **Server-side spans** (received via passback) are forwarded to the same Galileo OTLP endpoint as protobuf via `instrumentation.ingest_passback_to_galileo(meta)` — same headers `GalileoSpanProcessor` uses, so they land in the same project / log stream / trace.

The point of the demo is the **stitch**: client-side LLM and tool spans + server-side phase spans + per-message HTTP child spans, all sharing one trace ID, all rendering as one tree in Galileo.

## Stack

- **Python 3.11+** (required by `arcade-mcp-server` and `mcp`).
- **`uv`** for Python toolchain, venv, and dependencies — never invoke `pip` or `python -m venv` directly. `uv.lock` is committed and authoritative.
- Deps live in `pyproject.toml` under `[project] dependencies`. Add deps with `uv add <pkg>`, remove with `uv remove <pkg>`. Never edit `uv.lock` by hand.
- `[tool.uv] package = false` — script project, not a library. Don't add a `[build-system]` section.
- All Arcade libraries (`arcade-mcp-server`, `arcade-serve`, `arcade-core`, `arcade-tdk`) install from PyPI as ordinary packages. **Historical note**: through 2026-05-05 this demo installed them editable from a sibling `../arcade-mcp/libs/` clone because `TelemetryPassbackMiddleware` (the SEP-2448 implementation) wasn't published. The middleware shipped in `arcade-mcp-server 1.21.3`, and on 2026-05-06 the demo migrated to PyPI installs — `[tool.uv.sources]` was removed and the sibling-repo + macOS `UF_HIDDEN` workarounds went with it. If you ever need to test an unreleased upstream change, temporarily re-add `[tool.uv.sources]` pointing at a sibling checkout; revert when you're done.

Key libraries:

- `arcade-mcp-server` — MCP server framework with `@app.tool`, `Google` auth provider, `ResourceServerAuth`, and `TelemetryPassbackMiddleware`.
- `mcp` — official MCP SDK on the agent side: `ClientSession`, `OAuthClientProvider`, `streamable_http_client`.
- `langchain-openai` — `ChatOpenAI` chat model + `.bind_tools(...)` for OpenAI-format tool schemas converted from MCP tool defs.
- `openinference-instrumentation-langchain` — auto-instruments every `ChatOpenAI` invocation with OpenInference attributes (`llm.input_messages`, `llm.output_messages`, token counts) that Galileo recognizes natively. (NOT `opentelemetry-instrumentation-langchain` from Traceloop — that's a different schema.)
- `galileo` — provides `galileo.otel.GalileoSpanProcessor`, `galileo_context.init(...)`, and `galileo.otel.start_galileo_span(...)` for typed `ToolSpan` / `WorkflowSpan`. The processor wraps the OTLP exporter and handles cluster routing via `GALILEO_CONSOLE_URL`.
- `opentelemetry-{api,sdk}` + `opentelemetry-exporter-otlp-proto-http` + `opentelemetry-proto` + `protobuf` — standard OTel plumbing; the proto packages are needed to encode passback `resourceSpans` as OTLP protobuf for the manual POST to Galileo.
- `opentelemetry-instrumentation-httpx` — used **only by `server.py`** to auto-emit HTTP child spans under each Gmail phase span. The agent's default passback request asks for full detail, so these HTTP spans always ride back inline (unless `--no-passback` is set, in which case the server returns no spans at all).

## Commands

- Refresh env after pulling changes: `uv sync`
- Add a dependency: `uv add <pkg>` (updates `pyproject.toml` and `uv.lock` in one step)
- Upgrade all deps: `uv lock --upgrade && uv sync`

The demo runs as **two processes**:

```bash
# Terminal 1
.venv/bin/python server.py

# Terminal 2 (default — passback enabled, full server tree incl. HTTPX child spans)
.venv/bin/python workflow.py

# Terminal 2 (no passback — server is a black box; only agent-side ToolSpans in Galileo)
.venv/bin/python workflow.py --no-passback
```

**Note**: `.venv/bin/python` invocations Just Work on all platforms now — no `chflags` dance needed. The previous macOS-specific `UF_HIDDEN` workaround was load-bearing only because of the editable-from-sibling install (uv wrote hidden `.pth` files that `site.py` silently skipped). With PyPI installs, there are no editable `.pth` files for the Arcade packages, so the workaround is gone.

No test suite yet.

## Architecture

Three files, ~700 LOC total:

1. **`server.py`** — Local Arcade MCP server.
   - `TracerProvider` + `TelemetryPassbackMiddleware(service_name="arcade-galileo-demo-server", ...)` registered as middleware on the `MCPApp`. The middleware reads `_meta.traceparent` and `_meta.otel.traces.{request, detailed}`, creates a SERVER span under the agent's trace, runs the tool, then attaches the captured spans to the response `_meta.otel.traces.resourceSpans` (OTLP JSON).
   - `HTTPXClientInstrumentor` auto-instruments every `httpx.AsyncClient` call so Gmail GET / POST become child spans under the phase spans. The agent's default passback request includes `detailed: True`, so these HTTP spans are returned inline; `--no-passback` skips the entire passback opt-in and no server spans are returned.
   - `ArcadeResourceServerAuth(canonical_url=..., authorization_servers=[cloud.arcade.dev/oauth2])` validates OAuth 2.1 Bearer tokens and swaps `user_id` to the JWT's `email` claim so Arcade's Google-OAuth broker matches the right user.
   - Two tools: `list_emails`, `send_email`. Each tool wraps logical phases (`auth.validate`, `gmail.list_messages`, `gmail.fetch_details`, `gmail.send_message`, `format_response`) in `tracer.start_as_current_span(...)` calls with `gen_ai.*` semantic conventions on every span.
   - Listens at `http://127.0.0.1:8000/mcp`. Does NOT export spans externally — they only ride back to the agent inline.

2. **`instrumentation.py`** — Side-effecting Galileo OTel boot.
   - On import: `load_dotenv()` → validate `GALILEO_API_KEY` + `GALILEO_PROJECT` → `galileo_context.init(project=..., log_stream=...)` → `TracerProvider(resource={service.name=arcade-galileo-demo})` → `otel.add_galileo_span_processor(provider, GalileoSpanProcessor(...))` → `LangChainInstrumentor().instrument(tracer_provider=...)`.
   - Exports `tracer` (manual spans) and `tracer_provider` (for `force_flush()` / `shutdown()` on exit).
   - Exports `ingest_passback_to_galileo(meta)` — pulls `meta.otel.traces.resourceSpans`, hex→base64 the IDs, builds an `ExportTraceServiceRequest` protobuf, POSTs to `<GALILEO_CONSOLE_URL>/api/galileo/otel/traces` with `Galileo-API-Key` / `project` / `logstream` headers. Mirrors `ingest_spans_protobuf` from the reference `agent.py`.

3. **`workflow.py`** — LangChain agent.
   - **CLI args are parsed at module load time** (not inside `main()`) so the per-mode Galileo log stream name can be set in `os.environ["GALILEO_LOG_STREAM"]` *before* the `instrumentation` import runs. The suffix is unconditional: passback mode → `<base>-passback`, `--no-passback` mode → `<base>-no-passback`. Base comes from `GALILEO_LOG_STREAM` in `.env` (default `arcade-galileo-demo`). This is what makes the two modes write to differentiated log streams in Galileo for side-by-side comparison.
   - First heavy import is `from instrumentation import tracer_provider, ingest_passback_to_galileo` — runs the Galileo OTel side effects *before* any LangChain class is constructed.
   - `OAuthClientProvider` + `FileTokenStorage` → MCP OAuth 2.1 (PKCE flow with browser callback on port 9905, tokens cached to `.oauth_tokens.json` / `.oauth_client.json`).
   - `streamable_http_client(url=..., http_client=httpx.AsyncClient(auth=oauth_auth))` opens the streamable HTTP transport. `ClientSession.initialize()` handshakes; `session.list_tools()` discovers the tools (named **`ArcadeGalileoDemoServer_ListEmails`** and **`ArcadeGalileoDemoServer_SendEmail`** — `arcade-mcp-server` prefixes each tool function with the CamelCased server name) and we convert their MCP JSON-Schema input shapes to OpenAI function-calling format via `_mcp_to_openai_tool`.
   - `ChatOpenAI(model="gpt-4o").bind_tools(openai_tools)` is the agent. Multi-round loop bound by `MAX_WORKFLOW_ROUNDS = 5`, wrapped in a Galileo `WorkflowSpan(name="arcade_galileo_workflow")`.
   - Each tool call: `ToolSpan(name=..., input=..., tool_call_id=...)` + `otel.start_galileo_span(...)` → `propagator.inject(carrier)` → in the default mode, `meta = {"traceparent": ..., "otel": {"traces": {"request": True, "detailed": True}}}` → `await session.call_tool(name, arguments=args, meta=meta)` → `ingest_passback_to_galileo(result.meta)`. With `--no-passback`, the `otel` field is omitted entirely and `ingest_passback_to_galileo` is skipped (the agent prints `Server-side spans: NONE (passback not requested)` instead).
   - First-time-per-scope Google OAuth: tool result text contains `authorization_url` instead of data → print URL, wait for Enter, retry the call. Same pattern as the reference agent.

Trace shape in Galileo (verified end-to-end against `console-bin-citizens.gcp-dev.galileo.ai`):

```
arcade_galileo_workflow                                  (WorkflowSpan, typed)
├── ChatOpenAI                                           (OpenInference, auto)
├── ArcadeGalileoDemoServer_ListEmails                   (ToolSpan, typed — agent-side)
│   └── tools/call ArcadeGalileoDemoServer_ListEmails    (SERVER, from passback)
│       ├── auth.validate
│       ├── gmail.list_messages
│       │   └── GET messages                             (HTTP child, default mode)
│       ├── gmail.fetch_details
│       │   └── GET messages/<id>                        (HTTP child × N, default mode)
│       └── format_response
├── ChatOpenAI
├── ArcadeGalileoDemoServer_SendEmail                    (ToolSpan)
│   └── tools/call ArcadeGalileoDemoServer_SendEmail     (SERVER)
│       ├── auth.validate
│       ├── gmail.send_message
│       │   └── POST messages/send                       (HTTP child, default mode)
│       └── format_response
└── ChatOpenAI                                           (final, no tool_calls)
```

**Span counts.** Default mode (passback enabled, `detailed: True`): `list_emails` produces 10 server spans (1 SERVER + 4 phase + 1 internal middleware span + 4 HTTPX children: 1 list + 3 detail-fetches per the default `max_results=3`); `send_email` produces 6 (1 SERVER + 3 phase + 1 internal middleware + 1 HTTPX POST). With `--no-passback`: 0 server spans — the response carries no `resourceSpans` and Galileo sees only the agent-side `ToolSpan`s. Historical note: the previous CLI had a `--detailed` flag that toggled HTTPX inclusion; that flag was removed and `detailed: True` is now hardcoded whenever passback is requested.

**MCP capability response (verified).** After `session.initialize()`, `init.capabilities.serverExecutionTelemetry` is `{'version': '2026-03-01', 'signals': {'traces': {'supported': True}}}`. The agent only checks truthiness — version negotiation is left to the SDK once SEP-2448 lands a stable version.

Critical: the workflow + agent-side tool spans use Galileo's typed schemas (`WorkflowSpan`, `ToolSpan` from `galileo_core.schemas.logging.span`) wrapped in `galileo.otel.start_galileo_span(...)`. Generic OTel spans from `tracer.start_as_current_span(...)` get rendered as Workflow spans regardless of name — only typed spans surface as Tool / Retriever spans in the UI. The server-side spans don't go through `start_galileo_span` (the server doesn't import Galileo's SDK), but Galileo's OTel ingest accepts the OpenTelemetry SDK `Span` shape and renders them as Workflow spans, parented under the agent's `ToolSpan` via the shared trace ID + the SERVER span's parent linkage from `_meta.traceparent`.

## Deliberate non-choices (preserve when extending)

- **Local MCP server, not Arcade Cloud.** Server-side passback requires server-side instrumentation under the agent's trace, which means we run the server. If a customer asks "can we keep using `arcadepy.tools.execute(...)` against Arcade Cloud?" — that's the previous version of this demo (no passback, opaque tool calls), preserved in git history.
- **`galileo.otel.GalileoSpanProcessor`, not a hand-rolled `OTLPSpanExporter`.** The processor is the supported integration surface — it handles cluster routing via `GALILEO_CONSOLE_URL`, header construction, and the underlying OTLP exporter. Hand-rolling those breaks on non-SaaS clusters and is what early versions of these demos got wrong. The manual passback POST in `instrumentation.py::ingest_passback_to_galileo` re-derives the same endpoint + headers so passback spans land at the identical destination.
- **`galileo_context.init(...)`, not the `galileo.openai` chat-client wrapper.** Both are in the `galileo` package; the OTel-native processor is the supported path here. Do not reintroduce `from galileo.openai import OpenAI`.
- **`OpenInferenceInstrumentor`, not Traceloop's `LangchainInstrumentor`.** The reference `agent.py` uses Traceloop's instrumentor because Jaeger doesn't care about schema. Galileo renders OpenInference attributes natively (`llm.input_messages`, `llm.output_messages`, `llm.token_count.*`) — keep OpenInference here. The reference's `_find_instrumentor_span` helper is Traceloop-specific and intentionally NOT ported; we rely on natural OTel context propagation through the workflow root span instead.
- **Manual workflow root span.** `LangChainInstrumentor` produces one span per `ChatOpenAI` invocation but no parent — the manual `WorkflowSpan` is what gives Galileo a single root to anchor the agent trajectory and lets the agent-side `ToolSpan`s land underneath.
- **Side-effecting `instrumentation` import.** Importing the module is what installs the OTLP exporter and the LangChain instrumentor. Do not refactor it into an `init()` function unless `workflow.py` calls it explicitly *before* any LangChain class is constructed.
- **`bind_tools` + manual loop, not `langchain.agents.create_agent`.** The reference uses LangChain v1's `create_agent`. We keep the explicit multi-round loop because: (a) it makes the per-round structure visible in code, mirroring what shows up in the Galileo trace; (b) it preserves continuity with the previous version of `workflow.py`; (c) it's framework-version-agnostic (works with langchain 0.x + 1.x).
- **MCP OAuth tokens cached to disk (`.oauth_tokens.json`, `.oauth_client.json`).** Listed in `.gitignore`. Delete those files to force re-auth on next run.
- **Per-mode Galileo log streams via early CLI parse + env-var mutation.** `workflow.py` parses CLI args *at module load time* (before importing `instrumentation`) and sets `os.environ["GALILEO_LOG_STREAM"]` to `<base>-passback` or `<base>-no-passback` based on `--no-passback`. The reason for this ordering: `instrumentation.py` is side-effecting on import — it calls `galileo_context.init(log_stream=GALILEO_LOG_STREAM)` and registers `GalileoSpanProcessor(logstream=GALILEO_LOG_STREAM)` at module-load time. By the time `main()` runs, the log stream is locked in. Do not refactor `parse_args()` back into `main()` unless you also refactor `instrumentation.py` to expose a lazy `init()` function — and even then, the suffixing must happen before `init()` is called. The unconditional suffixing (vs. only suffixing when the user hasn't set `GALILEO_LOG_STREAM`) is also deliberate: it preserves per-mode separation even when the user customizes the base name.
- **Paired OAuth workaround for the localhost-vs-cloud-OAuth mismatch.** Two coordinated changes that must move together:
  1. `workflow.py` monkey-patches `OAuthContext.should_include_resource_param` to return `False`, suppressing the RFC 8707 `resource=<server_url>` parameter on OAuth requests.
  2. `server.py` sets `expected_audiences=[CANONICAL_URL, "urn:arcade:mcp"]`.

  Why: When the agent sends `resource=http://127.0.0.1:8000/mcp` to Arcade Cloud, Arcade tries to back-channel-fetch the resource's PRM endpoint (`<resource>/.well-known/oauth-protected-resource`) for validation. That request goes from `cloud.arcade.dev` → localhost, which is unreachable. Result: `OAuth error: server_error | description: Could not retrieve protected resource metadata for the gateway`. With the parameter suppressed, Arcade defaults the issued token's `aud` to `"urn:arcade:mcp"` instead of the resource URL, so the server must accept that URN.

  These two changes are paired — removing one without the other breaks OAuth (the unpaired states fail on either Arcade-side validation or local-side audience check). Documented inline in both files. **Remove both** when migrating to a publicly-reachable server (e.g. ngrok tunnel, real deployment) — at that point Arcade can back-channel-fetch the public PRM, and proper RFC 8707 audience binding becomes available.

## Customer-requirement caveat

This demo gives a **stitched client+server view in Galileo**. It captures:

- The LLM's tool-selection decisions (via `LangChainInstrumentor` on every `ChatOpenAI` invocation),
- The agent-side view of each tool call (input args, output text — via `ToolSpan`),
- The local server's internal phases (auth, Gmail HTTP fan-out, formatting — via `TelemetryPassbackMiddleware` and `HTTPXClientInstrumentor`).

It does **not** capture anything that lives in **Arcade Cloud's** infrastructure (the OAuth authorization server's handling of MCP OAuth, the Google-OAuth broker's internal stages). Those happen behind the `cloud.arcade.dev/oauth2` boundary and would require Arcade-side instrumentation we don't control. For the demo's purposes, treat Arcade Cloud as the OAuth provider — its internals are not in the trace.

## Extending the demo

- **Add a tool**: define a new `@app.tool(requires_auth=...)` function in `server.py`, wrap each phase in `tracer.start_as_current_span(...)`. The agent's `session.list_tools()` discovers it automatically.
- **Different LLM**: swap `ChatOpenAI` in `workflow.py` for any other LangChain chat model that supports `bind_tools`. The OpenInference spans still flow to Galileo unchanged.
- **Different observability backend**: replace `otel.add_galileo_span_processor(...)` in `instrumentation.py` with a vanilla `BatchSpanProcessor(OTLPSpanExporter(endpoint=..., headers=...))` pointing at Jaeger / Honeycomb / Tempo / etc. Then ALSO update `ingest_passback_to_galileo` to point at the same destination (or replace it with a Jaeger ingest helper, like the reference's `ingest_spans_json`).
