# Running the demo

A runbook you can follow top-to-bottom. If you're presenting live, keep this open on one screen and the two terminals + Galileo UI on another.

## 0. Prereqs (one-time)

**Install `uv`** (Python project manager — handles Python toolchain, venv, and deps):

```bash
# macOS
brew install uv

# Linux / WSL / alternative
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Verify: `uv --version` should print `uv 0.5+` or newer.

Nothing else to install — `uv` will fetch Python automatically on first run, and `uv sync` will pull `arcade-mcp-server` (and the other Arcade libraries) from PyPI.

## 1. Get the three API keys

You need a key from OpenAI, Arcade, and Galileo. Each dashboard shows the key **only once on creation** — paste it straight into `.env` as you go.

### OpenAI → `OPENAI_API_KEY`

1. Log in at https://platform.openai.com
2. **Add a payment method** at https://platform.openai.com/account/billing — new accounts without billing can't call `gpt-4o`. This is the #1 "why doesn't my key work" gotcha.
3. Create a key at https://platform.openai.com/api-keys → **Create new secret key** → copy the `sk-...` value.

### Arcade → `ARCADE_API_KEY`

1. Sign up at https://api.arcade.dev/dashboard/register
2. Go to https://api.arcade.dev/dashboard/api-keys → **Create API Key** → copy the `arc_...` value.
3. Note: this key is read by **`server.py`**, not `workflow.py`. The local server uses it to broker Google OAuth for `@tool(requires_auth=Google(...))`. The agent (`workflow.py`) never calls Arcade Cloud's tool-execution API directly — its only Arcade contact is **MCP OAuth 2.1** (PKCE flow against `cloud.arcade.dev/oauth2`) which the MCP SDK handles without an API key.

### Galileo → `GALILEO_API_KEY` (+ optional `GALILEO_CONSOLE_URL` for non-default clusters)

1. Sign up on the cluster you intend to use:
   - **Default SaaS**: https://app.galileo.ai/sign-up
   - **demo-v2**: https://console.demo-v2.galileocloud.io/
   - **Self-hosted / other**: whatever console URL your team uses
2. In the console, go to **Settings → API Keys** and create a key.
3. If you're not on the default SaaS cluster, also set `GALILEO_CONSOLE_URL` in `.env`. Both `GalileoSpanProcessor` (agent-side spans) and `ingest_passback_to_galileo` (server-passback spans) derive the OTLP endpoint from this — no separate endpoint override needed.

## 2. Clone and configure

This demo lives at https://github.com/robinliubin/arcade_galileo_demo. (Distinct from the upstream `ArcadeAI/arcade-mcp` cloned in §0 — that one is the *library* the demo depends on; this one is the demo itself.)

```bash
git clone https://github.com/robinliubin/arcade_galileo_demo.git
cd arcade_galileo_demo
cp .env.example .env
```

Edit `.env` and fill in:

```env
OPENAI_API_KEY=sk-...
ARCADE_API_KEY=arc_...
GALILEO_API_KEY=...

GALILEO_PROJECT=arcade-galileo-demo
# workflow.py suffixes this with -passback / -no-passback per CLI mode,
# so the two modes write to distinct streams in Galileo.
GALILEO_LOG_STREAM=arcade-galileo-demo

# Only if targeting a non-default cluster; omit otherwise.
# GALILEO_CONSOLE_URL=https://console.demo-v2.galileocloud.io/

ARCADE_USER_ID=you@example.com
```

**About `ARCADE_USER_ID`**: Arcade scopes Google OAuth tokens per `user_id`. Use your real Google email — the local server brokers Gmail OAuth per `ARCADE_USER_ID`, so reusing the same value lets the demo skip the consent step on subsequent runs. The default user query in `workflow.py` also templates this value in as the email recipient.

Then sync deps:

```bash
uv sync
```

This installs `arcade-mcp-server` (and the other Arcade libraries) from PyPI as ordinary packages — no editable installs, no sibling repo, no macOS `UF_HIDDEN` workarounds needed.

## 3. First run — two OAuth dances

The demo runs as **two processes**. You'll need two terminals open.

### Terminal 1: start the local MCP server

```bash
.venv/bin/python server.py
```

Expected output:

```
INFO     Starting MCPApp 'arcade_galileo_demo_server' v0.1.0 ...
INFO     Resource Server authentication is enabled. MCP routes are protected.
INFO     Accepted authorization server(s): https://cloud.arcade.dev/oauth2
INFO     MCP server started and ready for connections
INFO     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Leave it running.

### Terminal 2: run the agent

```bash
.venv/bin/python workflow.py
```

The first time you run the agent, **up to three OAuth flows happen**:

**Dance 1 — MCP OAuth 2.1 (agent → local server, validated by Arcade Cloud)**

The MCP SDK calls `webbrowser.open()` on `cloud.arcade.dev/oauth2/authorize` with PKCE params, you log in to Arcade, the redirect lands at `127.0.0.1:9905/callback`, the SDK exchanges the code for an access token, and writes `.oauth_tokens.json` + `.oauth_client.json` to disk. Subsequent runs are non-interactive.

```
  Mode:         passback (full server tree)
  Log stream:   arcade-galileo-demo-passback
  MCP server:   http://127.0.0.1:8000/mcp

  Opening browser for MCP OAuth authorization...
  URL: https://cloud.arcade.dev/oauth2/authorize?client_id=...

  [browser opens, you authorize]

  Server:                       arcade_galileo_demo_server v0.1.0
  serverExecutionTelemetry:     True
  Capability:                   {'version': '2026-03-01', 'signals': {'traces': {'supported': True}}}
  Tools:                        ['ArcadeGalileoDemoServer_ListEmails', 'ArcadeGalileoDemoServer_SendEmail']
```

> **Two modes.** The default invocation (`workflow.py`) requests SEP-2448 passback and writes traces to a log stream suffixed `-passback`. To compare against the "no observability" baseline, run `workflow.py --no-passback` — same agent, same query, but the server appears as a black box and traces land in a `-no-passback` log stream. See [§6](#6-passback-control---no-passback) for the side-by-side.

> **Browser didn't pop up?** `webbrowser.open()` can silently no-op when invoked from a sandboxed shell, over SSH, or under some terminal multiplexers. The agent still **prints the URL** before the call — copy-paste it into a browser yourself, complete consent, and the redirect to `127.0.0.1:9905/callback` will reach the agent. After that the run continues normally.

**Dances 2 & 3 — Google OAuth, one per Gmail scope (brokered by Arcade)**

Then the workflow runs. The first time `ArcadeGalileoDemoServer_ListEmails` executes for a fresh `ARCADE_USER_ID`, the server's `requires_auth=Google(scopes=["gmail.readonly"])` provider returns a JSON payload containing an `authorization_url` *instead* of email data:

```
Executing workflow...

  Google OAuth required for ArcadeGalileoDemoServer_ListEmails.
  Open this URL in your browser to authorize:

    https://accounts.google.com/o/oauth2/v2/auth?...

  Press Enter after authorizing...
```

Open the URL, complete Google's consent for `gmail.readonly`, press Enter — the agent retries the same call and the second attempt succeeds (Arcade has now cached the user's Google token for that scope). The same dance repeats for `ArcadeGalileoDemoServer_SendEmail` (`gmail.send` scope) on its first call.

If your `ARCADE_USER_ID` already has both Gmail scopes granted from a prior interaction (e.g. a previous demo, or via the Arcade dashboard), these two dances are skipped entirely — the workflow runs end-to-end after just the MCP OAuth in Dance 1. That's typical for the demo machine setup.

After both Google scopes are granted, Arcade caches the OAuth tokens per `ARCADE_USER_ID` and subsequent runs are non-interactive end to end.

## 4. Successful run — expected output

```
============================================================
Arcade + Galileo Integration Demo (server-span passback)
============================================================

  Mode:         passback (full server tree)
  Log stream:   arcade-galileo-demo-passback
  MCP server:   http://127.0.0.1:8000/mcp
  Query:        Find my 3 most recent emails from alex.salazar@arcade.dev. ...

  Server:                       arcade_galileo_demo_server v0.1.0
  serverExecutionTelemetry:     True
  Capability:                   {'version': '2026-03-01', 'signals': {'traces': {'supported': True}}}
  Tools:                        ['ArcadeGalileoDemoServer_ListEmails', 'ArcadeGalileoDemoServer_SendEmail']

Executing workflow...

  Server-side spans: 10 received and forwarded to Galileo
  Server-side spans: 6 received and forwarded to Galileo

============================================================
Workflow completed successfully!
============================================================

Result:
I have summarized your 3 most recent emails from Alex Salazar and sent the
summary to your email at <ARCADE_USER_ID>. Let me know if there's anything
else you need!

✓ View this trace at: https://app.galileo.ai/project/<id>/log-streams/<id>
```

If you see two `Server-side spans: <N> received and forwarded to Galileo` lines (one per `tools/call`), server passback is working and those spans are on their way to Galileo. If you don't, see [troubleshooting](#troubleshooting) below.

> **Why 10 + 6?** Each `tools/call` produces one SERVER root + the tool's phase spans + the HTTPX child spans under each phase. `ArcadeGalileoDemoServer_ListEmails`: 1 SERVER + 4 phase spans (auth.validate, gmail.list_messages, gmail.fetch_details, format_response) + 1 internal middleware span + 4 HTTPX children (1 list + 3 detail-fetches per the default `max_results=3`) = 10 total. `ArcadeGalileoDemoServer_SendEmail` is one phase shorter (no fetch_details) and one HTTP child shorter, so 6. With `--no-passback`, both numbers go to 0 — the agent prints `Server-side spans: NONE (passback not requested)` instead.

### What `--no-passback` looks like

Run `.venv/bin/python workflow.py --no-passback` and the same successful end-to-end output is shaped slightly differently:

```
  Mode:         no-passback (server is a black box)
  Log stream:   arcade-galileo-demo-no-passback
  ...

Executing workflow...

  Server-side spans: NONE (passback not requested)
  Server-side spans: NONE (passback not requested)

============================================================
Workflow completed successfully!
============================================================
```

The agent still completes the same multi-round conversation and the LLM still picks the same tools — only the server-side passback bundle is gone. In Galileo, the trace lands in the `-no-passback` log stream and shows agent-side spans only (no `tools/call` SERVER subtree under each `ToolSpan`). This is the "Act 1: black box" baseline.

## 5. See the trace in Galileo

1. Open the Galileo console URL printed at the end (or your cluster's URL if you set `GALILEO_CONSOLE_URL`).
2. Navigate to project **`arcade-galileo-demo`** → log stream **`arcade-galileo-demo-passback`** (or **`arcade-galileo-demo-no-passback`** if you ran with `--no-passback`). The agent prints the resolved log stream name in its startup banner — match what you see there. After running both modes, the project view shows both log streams with their respective trace counts; click each to A/B compare.
3. Open the most recent trace. You should see:
   - A **WorkflowSpan** named `arcade_galileo_workflow` at the root.
   - Three **LLM spans** named `ChatOpenAI` — auto-emitted by `LangChainInstrumentor`, with OpenInference attributes (`llm.input_messages`, `llm.output_messages`, token counts).
   - Two **agent-side ToolSpans** (`ArcadeGalileoDemoServer_ListEmails`, `ArcadeGalileoDemoServer_SendEmail`) — typed `ToolSpan`, rendered with the green tool icon, with input args, output text, and `tool_call_id` linking back to the LLM call.
   - Under each ToolSpan, a **`tools/call <toolname>` SERVER span** — this is the passback. Underneath it: `auth.validate`, the Gmail phase spans, and `format_response`.
4. Click a ChatOpenAI span → look at `llm.output_messages` → you'll see the model's `tool_calls` JSON. Click the matching ToolSpan → its `input` should match those `tool_call` arguments and its `tool_call_id` should equal the LLM's `tool_calls[*].id`. That link makes the agent trajectory visually obvious.
5. Click the SERVER span (`tools/call ArcadeGalileoDemoServer_ListEmails`) → drill into `gmail.fetch_details` → in default mode this phase is already a tree with HTTP child spans, one per message — the N+1 sequential-fetch pattern is visible immediately. Re-run with `--no-passback` and the entire SERVER subtree disappears, leaving only the agent-side `ToolSpan` with its 2-second total duration and no internal breakdown — the "before" picture.

See [call-flow.md](call-flow.md) for the full trace tree breakdown.

## 6. Passback control: `--no-passback`

By default, every `tools/call` requests the **full** server span tree — phase spans plus the HTTPX child spans under each phase. Pass `--no-passback` to skip the SEP-2448 opt-in entirely:

```bash
.venv/bin/python workflow.py --no-passback
```

Expected difference in Galileo:

| Mode | `ArcadeGalileoDemoServer_ListEmails` SERVER subtree | `ArcadeGalileoDemoServer_SendEmail` SERVER subtree |
|------|-----------------------------------------------------|----------------------------------------------------|
| default (passback enabled) | **10 spans**: SERVER root + auth.validate + gmail.list_messages + gmail.fetch_details + format_response + 1 internal middleware span + `GET messages` + `GET messages/<id>` × 3 (with `max_results=3`) | **6 spans**: SERVER root + auth.validate + gmail.send_message + format_response + 1 internal middleware span + `POST messages/send` |
| `--no-passback` | **0 spans** (no SERVER subtree at all — the agent-side `ToolSpan` is the only record) | **0 spans** |

This is the SEP-2448 opt-in: server vendors expose the spans they're willing to share; clients decide whether to ask. The agent's stdout reports per-call: `Server-side spans: 10 received and forwarded to Galileo` (default) vs. `Server-side spans: NONE (passback not requested)` (`--no-passback`).

### Per-mode log streams

Each mode writes to a **differently-suffixed Galileo log stream** so the two trace shapes are easy to compare side-by-side in the UI without filtering:

| CLI | Log stream name |
|---|---|
| `python workflow.py` | `<GALILEO_LOG_STREAM>-passback` (default: `arcade-galileo-demo-passback`) |
| `python workflow.py --no-passback` | `<GALILEO_LOG_STREAM>-no-passback` (default: `arcade-galileo-demo-no-passback`) |

The suffixing happens in `workflow.py` at module-load time, before the `instrumentation` import runs (which is what calls `galileo_context.init` with the resolved log stream name). The base name comes from `GALILEO_LOG_STREAM` in `.env` (or the demo default `arcade-galileo-demo` if you didn't set it). Customizing the base — e.g. `GALILEO_LOG_STREAM=my-stream` — gives you `my-stream-passback` and `my-stream-no-passback`; the suffixing is unconditional. The agent prints the resolved log stream in the startup banner (`Log stream: ...`) so you know exactly which UI link to open.

For a live A/B demo: run once in default mode, then again with `--no-passback` — Galileo's project view shows both log streams, the trace counts side by side; click each to see the same two LLM rounds with vs. without server visibility.

> Historical note: a previous version of this demo had a `--detailed` flag that toggled HTTPX inclusion (default returned phase spans only; `--detailed` returned the full tree). That flag was removed — `detailed: True` is now hardcoded whenever passback is requested, since "phase spans without HTTP children" turned out not to be a useful intermediate point.

## 7. Customize the demo

### Change the user query

Pass it on the command line:

```bash
.venv/bin/python workflow.py "List my 5 most recent unread emails"
```

Or edit the `DEFAULT_QUERY` constant near the top of `workflow.py` to change what runs when no positional argument is given. Use the literal string `$ARCADE_USER_ID` anywhere in your query — the agent substitutes your `.env` value at runtime.

### Add a new tool

Add it to `server.py`:

```python
@app.tool(requires_auth=Google(scopes=["..."]))
async def my_tool(context: Context, ...) -> dict:
    with tracer.start_as_current_span("phase_a") as s:
        s.set_attribute("gen_ai.tool.name", "phase_a")
        ...
    return {...}
```

No changes to `workflow.py` — it discovers tools dynamically via `session.list_tools()`. The new tool will be exposed as `ArcadeGalileoDemoServer_MyTool` (CamelCased server name + CamelCased function name).

### Use a different LLM

Swap `ChatOpenAI` in `execute_workflow()` for any other LangChain chat model that supports `bind_tools`:

```python
from langchain_anthropic import ChatAnthropic
llm = ChatAnthropic(model="claude-sonnet-4-6", ...).bind_tools(openai_tools)
```

`LangChainInstrumentor` is provider-agnostic, so the OpenInference spans still flow to Galileo unchanged. (You'd add `langchain-anthropic` via `uv add langchain-anthropic` and set the right API key in `.env`.)

### Send traces somewhere else

Replace `otel.add_galileo_span_processor(...)` in `instrumentation.py` with a vanilla `BatchSpanProcessor(OTLPSpanExporter(endpoint=..., headers=...))` for the agent-side spans, and update `ingest_passback_to_galileo` (or replace it) to point the server-passback POST at the same destination. Common targets: Jaeger (`http://localhost:4318/v1/traces`), Honeycomb, Tempo, Langtrace.

## Troubleshooting

**`ConnectError: All connection attempts failed` (when running `workflow.py`)**
The local server isn't running, or it's listening on a different port. Confirm Terminal 1 shows `Listening on http://127.0.0.1:8000/mcp`. Check `lsof -i :8000` to see what's listening.

**MCP OAuth browser flow fails / hangs**
Port 9905 must be free for the OAuth callback. If something else is using it, edit `OAUTH_CALLBACK_PORT` in `workflow.py`. To force fresh MCP OAuth, delete `.oauth_tokens.json` and `.oauth_client.json`.

**`OAuth error: server_error | description: Could not retrieve protected resource metadata for the gateway`**
The MCP SDK is sending RFC 8707 `resource=http://127.0.0.1:8000/mcp` to Arcade Cloud, and Arcade's authorization server is trying to back-channel-fetch the resource's PRM endpoint to validate it — but `127.0.0.1` isn't reachable from `cloud.arcade.dev`. `workflow.py` should already monkey-patch `OAuthContext.should_include_resource_param` to suppress the parameter; if you see this error, the patch isn't taking effect. Confirm the `from mcp.client.auth.oauth2 import OAuthContext` block near the top of `workflow.py` runs *before* `OAuthClientProvider(...)` is instantiated. If you removed that patch deliberately to use RFC 8707 audience binding, you'll need to expose `127.0.0.1:8000` via a public tunnel (ngrok, cloudflared) and update `CANONICAL_URL` in `server.py` to the tunnel URL.

**Local server returns 401 after Arcade issued a token successfully**
The token Arcade issued has `aud` that doesn't match what `server.py` expects. With the `should_include_resource_param` monkey-patch in `workflow.py`, Arcade falls back to `aud="urn:arcade:mcp"` (their generic URN). Confirm `server.py`'s `expected_audiences` list includes both `CANONICAL_URL` and `"urn:arcade:mcp"`. If you removed the URN audience deliberately, you must also remove the monkey-patch — the two are paired (client suppresses `resource=` ↔ server accepts URN audience).

**`Error: GALILEO_API_KEY and GALILEO_PROJECT must be set`**
Missing env vars. Check `.env` exists and has both values filled in.

**`Error: Missing required environment variables`** (from `validate_environment()`)
The list of missing variables is printed. Most common: `ARCADE_USER_ID` not set, or `OPENAI_API_KEY` not set.

**`openai.AuthenticationError: Incorrect API key`**
The `OPENAI_API_KEY` is wrong, revoked, or from a different org. Check https://platform.openai.com/api-keys.

**`openai.PermissionDeniedError` / `insufficient_quota`**
OpenAI billing isn't set up. Add a payment method at https://platform.openai.com/account/billing.

**Tool result is an authorization URL instead of data**
First-time-per-scope Google OAuth. Open the URL, complete consent, press Enter. Each scope (`gmail.readonly`, `gmail.send`) needs its own one-time consent.

**`serverExecutionTelemetry: False` printed at startup**
The server isn't advertising the SEP-2448 capability. Likely cause: `arcade-mcp-server` resolved to a version older than 1.21.3 (the first PyPI release shipping `TelemetryPassbackMiddleware`). Check the resolved version with `grep -A 2 '^name = "arcade-mcp-server"' uv.lock` — if it's <1.21.3, run `uv lock --upgrade-package arcade-mcp-server && uv sync` to bump it. A working install prints:

```
serverExecutionTelemetry:     True
Capability:                   {'version': '2026-03-01', 'signals': {'traces': {'supported': True}}}
```

(The `version` field will change as SEP-2448 stabilizes; the agent only checks truthiness.)

**No `Server-side spans: <N>` lines in console output**
The agent didn't get passback in `result.meta`. Causes (in order of likelihood):
1. Server doesn't advertise the capability — see above.
2. Agent didn't request passback — confirm `meta = {"otel": {"traces": {"request": True, ...}}}` is being sent (check it's not being stripped by an older MCP SDK version).
3. The tool returned an error before the middleware could capture spans. Look at the agent's `ToolSpan.output` — if it contains an error message, fix the upstream issue.

**`Server-side spans: <N> received and forwarded to Galileo` in console, but no SERVER child span in Galileo**
Passback arrived at the agent but didn't reach Galileo. Check:
1. The `ingest_passback_to_galileo` POST returned non-2xx — look for `Galileo passback ingest returned HTTP <code>` warnings in the terminal.
2. Cluster mismatch — `GALILEO_API_KEY` must be issued on the cluster `GALILEO_CONSOLE_URL` points at. A key from app.galileo.ai will not authenticate against demo-v2.galileocloud.io.
3. Trace ID mismatch — verify the SERVER span's traceId matches the `ToolSpan`'s. Mismatch means `traceparent` injection happened outside the `ToolSpan`'s context.

**`Failed to export span batch code: 401, reason: Unauthorized`** (printed mid-run by the span processor)
Galileo's OTLP collector rejected the agent-side spans (separate from passback). Same checklist as the previous version of this demo:
1. `GALILEO_API_KEY` in `.env` is set, has no surrounding quotes or trailing whitespace, and isn't expired.
2. **Cluster mismatch.** Same as above.
3. `galileo_context.init(...)` raised at startup but you ignored the error. Re-run and read the first few lines of stderr.

**Galileo trace shows `ChatOpenAI` spans but no `arcade_galileo_workflow` root or `ToolSpan`s**
The manual `start_galileo_span(...)` calls aren't running. Likely cause: an exception in the agent loop before the `WorkflowSpan` opened. Check the console for stack traces.

**Galileo trace shows `ToolSpan` and `tools/call` SERVER child but they're in *different* traces**
The traceparent stitch failed. Almost always means `propagator.inject(carrier)` ran outside the `ToolSpan`'s context. Verify `_call_mcp_tool` does `propagator.inject(carrier)` *inside* the `with otel.start_galileo_span(tool):` block.

**Trace went to the wrong log stream** (or you can't find it)
`workflow.py` always suffixes `GALILEO_LOG_STREAM` with `-passback` or `-no-passback` based on the CLI mode. The startup banner prints the resolved name — match that exactly in the Galileo UI. If the agent's `Log stream:` line shows something unexpected (e.g. you set `GALILEO_LOG_STREAM=foo` in `.env` and got `foo-passback` instead of `foo`), that's intentional — the suffixing is unconditional. To opt out of suffixing entirely, you'd need to remove the env-var assignment in `workflow.py` (lines around `os.environ["GALILEO_LOG_STREAM"] = ...`). To force a fresh log stream, just change `GALILEO_LOG_STREAM` in `.env` — the suffix is appended to whatever you set.
