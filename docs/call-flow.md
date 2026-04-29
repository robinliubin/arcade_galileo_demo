# Call flow

What actually happens, in order, when you start `server.py` in one terminal and run `.venv/bin/python workflow.py` in another with the default user query (find 3 recent emails from `alex.salazar@arcade.dev`, then email a one-paragraph summary to `$ARCADE_USER_ID`).

## Sequence diagram

```mermaid
sequenceDiagram
    autonumber
    actor user as You
    participant wf as workflow.py
    participant ins as instrumentation.py
    participant srv as server.py<br/>(local MCP server)
    participant gmail as Gmail API
    participant arcade as Arcade Cloud<br/>(OAuth + Google broker)
    participant openai as OpenAI
    participant g as Galileo<br/>(OTLP HTTP)

    user->>srv: .venv/bin/python server.py
    srv->>srv: TracerProvider + TelemetryPassbackMiddleware
    srv->>srv: HTTPXClientInstrumentor().instrument()
    srv->>srv: app.run(transport="http", host=127.0.0.1, port=8000)

    user->>wf: .venv/bin/python workflow.py
    wf->>ins: import (side-effecting)
    ins->>ins: load_dotenv(); validate Galileo env
    ins->>ins: galileo_context.init(project, log_stream)
    ins->>ins: TracerProvider + add_galileo_span_processor(...)
    ins->>ins: LangChainInstrumentor().instrument(...)
    ins-->>wf: tracer + ingest_passback_to_galileo

    wf->>arcade: MCP OAuth 2.1 (PKCE, browser callback)
    arcade-->>wf: access_token (cached to .oauth_tokens.json)

    wf->>srv: streamable_http_client + ClientSession.initialize()
    srv-->>wf: serverInfo + capabilities (incl. serverExecutionTelemetry)
    wf->>srv: list_tools()
    srv-->>wf: [ArcadeGalileoDemoServer_ListEmails, ArcadeGalileoDemoServer_SendEmail]

    rect rgba(180,200,255,0.18)
    note over wf,g: WorkflowSpan("arcade_galileo_workflow")

    rect rgba(220,220,255,0.25)
    note over wf,openai: round 1 — agent picks ArcadeGalileoDemoServer_ListEmails
    wf->>openai: llm.ainvoke(messages)
    note right of ins: LangChainInstrumentor auto-emits ChatOpenAI span
    openai-->>wf: AIMessage(tool_calls=[ArcadeGalileoDemoServer_ListEmails(max_results=3, query="from:...")])
    end

    rect rgba(220,255,220,0.25)
    note over wf,srv: ToolSpan(name="ArcadeGalileoDemoServer_ListEmails")
    wf->>wf: propagator.inject(carrier) → traceparent
    wf->>srv: tools/call ArcadeGalileoDemoServer_ListEmails<br/>_meta = {traceparent, otel.traces.{request,detailed}}
    srv->>srv: TelemetryPassbackMiddleware opens SERVER span<br/>under agent's trace
    srv->>srv: auth.validate phase span
    srv->>gmail: GET /messages?q=from:... (HTTPX child span, --detailed only)
    gmail-->>srv: {messages: [...]}
    loop per message
        srv->>gmail: GET /messages/<id> (HTTPX child span, --detailed only)
        gmail-->>srv: metadata
    end
    srv->>srv: format_response phase span
    srv-->>wf: response<br/>_meta.otel.traces.resourceSpans = [...]<br/>content = {emails: [...]}
    wf->>g: ingest_passback_to_galileo(meta) → POST OTLP protobuf
    end

    rect rgba(220,220,255,0.25)
    note over wf,openai: round 2 — agent picks ArcadeGalileoDemoServer_SendEmail
    wf->>openai: llm.ainvoke(messages incl. email list)
    openai-->>wf: AIMessage(tool_calls=[ArcadeGalileoDemoServer_SendEmail(to=..., subject=..., body=...)])
    end

    rect rgba(220,255,220,0.25)
    note over wf,srv: ToolSpan(name="ArcadeGalileoDemoServer_SendEmail")
    wf->>srv: tools/call ArcadeGalileoDemoServer_SendEmail<br/>_meta = {traceparent, otel...}
    srv->>srv: auth.validate + gmail.send_message + format_response
    srv->>gmail: POST /messages/send (HTTPX child, --detailed only)
    gmail-->>srv: {id, status}
    srv-->>wf: response with passback _meta
    wf->>g: ingest_passback_to_galileo(meta)
    end

    rect rgba(220,220,255,0.25)
    note over wf,openai: round 3 — agent produces final answer
    wf->>openai: llm.ainvoke(messages incl. send confirmation)
    openai-->>wf: AIMessage(content="Done. Summary emailed.")
    end

    end

    wf->>wf: provider.force_flush() + shutdown()
    wf-)g: BatchSpanProcessor sends agent-side spans (OTLP HTTP/protobuf)
    wf-->>user: prints "View this trace at: ..."
```

## Step-by-step

**1. Server boot (Terminal 1)**

`server.py` registers a `TracerProvider`, attaches a `TelemetryPassbackMiddleware` (with `service_name="arcade-galileo-demo-server"`), instruments `httpx`, configures `ArcadeResourceServerAuth` against `cloud.arcade.dev/oauth2`, and calls `app.run(transport="http", host="127.0.0.1", port=8000)`. The server stays up; subsequent requests open MCP sessions over streamable HTTP.

**2. Agent module init (Terminal 2, before `main()`)**

Importing `workflow.py` runs `from instrumentation import tracer_provider, ingest_passback_to_galileo` first, which executes `instrumentation.py`'s side effects:

- `load_dotenv()` pulls `.env` into `os.environ`.
- `GALILEO_API_KEY` and `GALILEO_PROJECT` are validated; missing ones cause `sys.exit(1)`.
- `galileo_context.init(project=..., log_stream=...)` resolves the Galileo cluster from `GALILEO_CONSOLE_URL` (or default SaaS), authenticates, and bootstraps the project + log stream.
- `TracerProvider` + `GalileoSpanProcessor(project=..., logstream=...)` is registered as the global tracer provider.
- `LangChainInstrumentor().instrument(tracer_provider=...)` patches LangChain so future `ChatOpenAI` constructions are auto-traced.

This ordering matters: the instrumentor must be active *before* `ChatOpenAI(...)` is constructed in `execute_workflow()`, otherwise the LLM spans never fire.

**3. MCP OAuth 2.1**

`OAuthClientProvider` + `FileTokenStorage` looks for cached tokens in `.oauth_tokens.json`. If absent, it:

1. Sends an unauthenticated request to the local server.
2. Receives 401 + `WWW-Authenticate` header pointing at `cloud.arcade.dev/oauth2` (RFC 9728 OAuth 2.1 protected resource discovery).
3. Performs PKCE flow: opens browser at the auth server's `/authorize` endpoint, waits on a local HTTP listener at `127.0.0.1:9905/callback` for the redirect, exchanges the code for an access token at `/token`.
4. Persists the access token + dynamic-client-registration response to `.oauth_*.json`.

Subsequent runs reuse the cached tokens — non-interactive.

**4. MCP session init + tool discovery**

```python
async with (
    streamable_http_client(url=server_url, http_client=http_client) as (read, write, _),
    ClientSession(read, write) as session,
):
    init = await session.initialize()              # capabilities incl. serverExecutionTelemetry
    discovered = await session.list_tools()        # [list_emails, send_email]
```

The MCP `initialize` handshake exposes `serverExecutionTelemetry` in `init.capabilities` — that's how the agent knows the server speaks SEP-2448 and passback is opt-in-able.

We then convert the MCP tool defs to OpenAI function-calling shape:

```python
openai_tools = [
    {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.inputSchema,
        },
    }
    for t in discovered.tools
]
```

**5. Agent loop — wrapped in `WorkflowSpan`**

For the default user query, the loop converges in **3 rounds**:

| Round | LLM picks | Server returns |
|---|---|---|
| 1 | `ArcadeGalileoDemoServer_ListEmails(max_results=3, query="from:alex.salazar@arcade.dev")` | List of 3 email metadata records + passback resourceSpans (6 spans default; +3 with `--detailed`) |
| 2 | `ArcadeGalileoDemoServer_SendEmail(to=user_email, subject=..., body=summary)` | `{message_id, status:"sent"}` + passback resourceSpans (5 spans default; +1 with `--detailed`) |
| 3 | Final answer (no `tool_calls`) | — |

Note the **server-name prefix**: `arcade-mcp-server` namespaces tool functions by prefixing the CamelCased server name (`MCPApp(name="arcade_galileo_demo_server")` → `ArcadeGalileoDemoServer_*`). This happens at `MCPApp` registration time, so `session.list_tools()` returns the prefixed names and the LLM sees them as plain function names. There's no transformation in `workflow.py`.

For each tool call:

```python
tool = ToolSpan(name=tc["name"], input=json.dumps(tc["args"]), tool_call_id=tc["id"])
with otel.start_galileo_span(tool):
    propagator.inject(carrier)
    meta = {
        "traceparent": carrier["traceparent"],
        "otel": {"traces": {"request": True, "detailed": detailed}},
    }
    result = await session.call_tool(tc["name"], arguments=tc["args"], meta=meta)
    tool.output = result.content[0].text[:5000]
    ingest_passback_to_galileo(result.meta)
```

The active span when `propagator.inject(carrier)` runs is the `ToolSpan`, so `traceparent` carries that span's trace ID + span ID. The server creates its `tools/call <toolname>` SERVER span as a child of the `ToolSpan` — the parent linkage is the entire stitch.

**6. Server-side execution per tool call**

`TelemetryPassbackMiddleware`'s flow on a single `tools/call`:

1. Read `_meta.traceparent` → restore that as the OTel context.
2. Read `_meta.otel.traces.{request, detailed}` to know whether to capture spans (and how much).
3. Open a SERVER-kind span named `tools/call <toolname>` — child of the `ToolSpan` via the trace context.
4. Run the tool function. Inside the tool, `tracer.start_as_current_span(...)` calls produce the phase spans (`auth.validate`, `gmail.list_messages`, ...) as children of the SERVER span. `HTTPXClientInstrumentor` adds HTTP child spans under each phase.
5. After the tool returns, the middleware pulls all spans associated with this request out of its in-memory buffer.
6. If `detailed=True`, include all spans. If `detailed=False`, include only top-level phase spans + the SERVER span; set `truncated=True` and `droppedSpanCount=N` to advertise that more was filtered.
7. Serialize to OTLP JSON, attach to `response._meta.otel.traces.resourceSpans`.

**7. First-time-per-scope Google OAuth dance**

The Arcade `Google` auth provider injects a check before the tool body runs. If the user_id (resolved to the OAuth email claim) hasn't yet granted the requested scope (`gmail.readonly` for `list_emails`, `gmail.send` for `send_email`):

- The tool returns a result whose content text is `{"authorization_url": "https://accounts.google.com/...", ...}` instead of email data.
- The agent's `_extract_google_auth_url` notices the URL, prints it to the terminal, and waits for `<Enter>`.
- After the user completes consent, the agent retries the *same* `session.call_tool(...)` — Arcade has now cached the token for that user_id + scope, so the call succeeds.

This is the exact same flow as the previous `arcadepy`-based demo, just with the OAuth URL coming back inside an MCP response instead of an `arcadepy.PermissionDeniedError`.

**8. Passback ingest into Galileo**

`ingest_passback_to_galileo(result.meta)` runs immediately after each `session.call_tool`:

- Pulls `meta.otel.traces.resourceSpans` (OTLP JSON).
- Converts hex `traceId` / `spanId` / `parentSpanId` to base64 (protobuf wire format).
- `ParseDict` → `ExportTraceServiceRequest` protobuf → `SerializeToString()`.
- POSTs synchronously to `<GALILEO_CONSOLE_URL>/api/galileo/otel/traces` with `Galileo-API-Key` / `project` / `logstream` headers.

Synchronous so spans for an in-progress trace don't get lost if the process exits before a background flush — the demo accepts the per-call latency.

**9. Flush on exit**

```python
finally:
    _tracer_provider.force_flush()
    _tracer_provider.shutdown()
```

`BatchSpanProcessor` (inside `GalileoSpanProcessor`) buffers spans locally and ships them in batches every few seconds. Without `force_flush`, a fast-exiting script can return before the spans leave your machine. The passback POSTs are synchronous so they don't need this, but the agent-side spans (workflow root, ToolSpan, ChatOpenAI) do.

## What the Galileo trace looks like

In Galileo UI → project `arcade-galileo-demo` → log stream `default`, one invocation produces **one trace**.

> The shape below was verified end-to-end against `console-bin-citizens.gcp-dev.galileo.ai` on 2026-04-29. Default-mode passback returned 6 server spans on round 1 (`ArcadeGalileoDemoServer_ListEmails`) and 5 on round 2 (`ArcadeGalileoDemoServer_SendEmail`), with `(3 additional)` and `(1 additional)` HTTPX spans available via `--detailed` respectively. All passback POSTs to `<console>/api/galileo/otel/traces` returned HTTP 200.

```
arcade_galileo_workflow                                  (WorkflowSpan)
    workflow.input  = "Find my 3 most recent emails from alex.salazar..."
    workflow.output = "I have summarized your 3 most recent emails ..."

├── ChatOpenAI                                           (OpenInference, auto)
│   llm.input_messages: [{role:"user", content:"Find my 3 most recent emails ..."}]
│   llm.output_messages: [{role:"assistant",
│                          tool_calls:[ArcadeGalileoDemoServer_ListEmails(...)]}]
│   llm.token_count.prompt / completion captured

├── ArcadeGalileoDemoServer_ListEmails                   (ToolSpan — agent-side)
│   tool.input    = {"max_results":3, "query":"from:alex.salazar@arcade.dev"}
│   tool.output   = "{\"emails\":[...]}"  (capped at 5000 chars)
│   tool_call_id  = call_abc123             (matches the LLM's tool_calls[0].id)
│   │
│   └── tools/call ArcadeGalileoDemoServer_ListEmails    (SERVER, from passback)
│       ├── auth.validate                                (gen_ai.tool.name=auth.validate)
│       ├── gmail.list_messages                          (gmail.message_count=3)
│       │   └── GET messages                             (HTTP child, --detailed only)
│       ├── gmail.fetch_details                          (gmail.fetch_count=3)
│       │   ├── GET messages/<id1>                       (--detailed only)
│       │   ├── GET messages/<id2>                       (--detailed only)
│       │   └── GET messages/<id3>                       (--detailed only)
│       └── format_response                              (email.count=3)

├── ChatOpenAI                                           (round 2 — sees email list)
├── ArcadeGalileoDemoServer_SendEmail                    (ToolSpan — agent-side)
│   └── tools/call ArcadeGalileoDemoServer_SendEmail     (SERVER)
│       ├── auth.validate
│       ├── gmail.send_message                           (gmail.recipient=..., gmail.subject=...)
│       │   └── POST messages/send                       (--detailed only)
│       └── format_response
└── ChatOpenAI                                           (round 3 — final answer)
```

**What to point at during a live demo:**

- The **workflow root** anchors the whole agent trajectory.
- Each **ChatOpenAI** span shows the exact prompt and response — including `tool_calls` proving the LLM is choosing tools, not hallucinating.
- The **agent-side `ToolSpan`** (e.g. `ArcadeGalileoDemoServer_ListEmails`) shows what the agent saw: the args it sent and the result text it received.
- The **SERVER span** (`tools/call list_emails`) and its children show what *actually happened* on the server. Without passback this entire subtree is invisible — the tool call is opaque.
- With `--detailed`, the **HTTP child spans** under `gmail.fetch_details` reveal the per-message sequential-fetch waterfall that makes that phase the slowest part of the request. That's the point of the SEP: the agent author can diagnose server-side performance without server-side access.

## Pitfalls the trace helps you catch

- **Server passback didn't arrive**: `ToolSpan` is present in Galileo but no `tools/call` SERVER child. Causes: server isn't running, server doesn't advertise `serverExecutionTelemetry`, agent didn't set `_meta.otel.traces.request=true`, or `ingest_passback_to_galileo` failed (check console for HTTP error from the manual POST).
- **Stitch broke** (server spans land as a separate trace, not under the agent's `ToolSpan`): `propagator.inject` ran outside the `ToolSpan`'s context, or `meta.traceparent` is missing/empty. Verify by checking the trace IDs — they should match between agent-side and server-side spans.
- **Tool hallucination**: LLM invents a tool name → server returns "tool not found" error → `ToolSpan.output` shows the error → next `ChatOpenAI` span shows the model's recovery.
- **Argument-shape drift**: LLM passes `{"max_results":"3"}` (string) when the server expects an int → server returns a validation error in the tool result.
- **Silent OAuth stall**: first `ArcadeGalileoDemoServer_ListEmails` returns an authorization URL → `ToolSpan.output` is the URL JSON → next ChatOpenAI span shows the model "responding" to the URL instead of email data. Foreground this behavior for live demos.
- **No spans appear in Galileo**: `force_flush()` was skipped (early crash before `finally`), or `LangChainInstrumentor` ran *after* `ChatOpenAI` was constructed.
