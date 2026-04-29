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

**Sibling repo check.** This demo's `pyproject.toml` installs `arcade-mcp-server` editable from `../arcade-mcp/libs/arcade-mcp-server` (the unreleased `TelemetryPassbackMiddleware` lives there). Confirm the sibling exists:

```bash
ls ../arcade-mcp/libs/arcade-mcp-server/pyproject.toml
```

If absent, clone it: `git clone https://github.com/ArcadeAI/arcade-mcp.git ../arcade-mcp` (the `main` branch already includes PR #797).

Nothing else to install — `uv` will fetch Python automatically on first run.

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

```bash
git clone <this repo>
cd arcade_galileo_demo
cp .env.example .env
```

Edit `.env` and fill in:

```env
OPENAI_API_KEY=sk-...
ARCADE_API_KEY=arc_...
GALILEO_API_KEY=...

GALILEO_PROJECT=arcade-galileo-demo
GALILEO_LOG_STREAM=default

# Only if targeting a non-default cluster; omit otherwise.
# GALILEO_CONSOLE_URL=https://console.demo-v2.galileocloud.io/

ARCADE_USER_ID=you@example.com
```

**About `ARCADE_USER_ID`**: Arcade scopes Google OAuth tokens per `user_id`. Use your real Google email — the local server brokers Gmail OAuth per `ARCADE_USER_ID`, so reusing the same value lets the demo skip the consent step on subsequent runs. The default user query in `workflow.py` also templates this value in as the email recipient.

Then sync deps:

```bash
uv sync
```

### macOS-only: clear the `UF_HIDDEN` flag on editable `.pth` files (one-time)

The `arcade-mcp-server` install is editable from `../arcade-mcp/libs/`. On macOS, uv writes those editable `.pth` files with `UF_HIDDEN` set, which causes Python's `site.py` to silently skip them — and `import arcade_mcp_server` fails with `ModuleNotFoundError`. Clear the flag once after `uv sync`:

```bash
chflags nohidden \
  .venv/lib/python*/site-packages/_editable_impl_*.pth \
  .venv/lib/python*/site-packages/_virtualenv.pth
```

**Don't use `uv run` after this** — `uv run` re-applies the hidden flag on every invocation, and you'll be back to square one. Use `.venv/bin/python` directly (or `source .venv/bin/activate && python ...`). Linux / WSL is unaffected.

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
  Mode:         phases only
  MCP server:   http://127.0.0.1:8000/mcp

  Opening browser for MCP OAuth authorization...
  URL: https://cloud.arcade.dev/oauth2/authorize?client_id=...

  [browser opens, you authorize]

  Server:                       arcade_galileo_demo_server v0.1.0
  serverExecutionTelemetry:     True
  Capability:                   {'version': '2026-03-01', 'signals': {'traces': {'supported': True}}}
  Tools:                        ['ArcadeGalileoDemoServer_ListEmails', 'ArcadeGalileoDemoServer_SendEmail']
```

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

  Mode:         phases only
  MCP server:   http://127.0.0.1:8000/mcp
  Query:        Find my 3 most recent emails from alex.salazar@arcade.dev. ...

  Server:                       arcade_galileo_demo_server v0.1.0
  serverExecutionTelemetry:     True
  Capability:                   {'version': '2026-03-01', 'signals': {'traces': {'supported': True}}}
  Tools:                        ['ArcadeGalileoDemoServer_ListEmails', 'ArcadeGalileoDemoServer_SendEmail']

Executing workflow...

  Server-side spans: 6 received and forwarded to Galileo
  (3 additional spans available with --detailed)
  Server-side spans: 5 received and forwarded to Galileo
  (1 additional spans available with --detailed)

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

> **Why 6 + 5?** Each `tools/call` produces one SERVER root + the tool's phase spans. `ArcadeGalileoDemoServer_ListEmails` adds 5 phase spans (auth.validate, gmail.list_messages, gmail.fetch_details, format_response, plus an internal middleware span); 1 SERVER + 5 = 6 total. `ArcadeGalileoDemoServer_SendEmail` is one phase shorter (no fetch_details), so 5. The `(N additional spans available with --detailed)` lines count the HTTPX child spans the server filtered out — `--detailed` returns the full tree.

## 5. See the trace in Galileo

1. Open the Galileo console URL printed at the end (or your cluster's URL if you set `GALILEO_CONSOLE_URL`).
2. Navigate to project **`arcade-galileo-demo`** → log stream **`default`**.
3. Open the most recent trace. You should see:
   - A **WorkflowSpan** named `arcade_galileo_workflow` at the root.
   - Three **LLM spans** named `ChatOpenAI` — auto-emitted by `LangChainInstrumentor`, with OpenInference attributes (`llm.input_messages`, `llm.output_messages`, token counts).
   - Two **agent-side ToolSpans** (`ArcadeGalileoDemoServer_ListEmails`, `ArcadeGalileoDemoServer_SendEmail`) — typed `ToolSpan`, rendered with the green tool icon, with input args, output text, and `tool_call_id` linking back to the LLM call.
   - Under each ToolSpan, a **`tools/call <toolname>` SERVER span** — this is the passback. Underneath it: `auth.validate`, the Gmail phase spans, and `format_response`.
4. Click a ChatOpenAI span → look at `llm.output_messages` → you'll see the model's `tool_calls` JSON. Click the matching ToolSpan → its `input` should match those `tool_call` arguments and its `tool_call_id` should equal the LLM's `tool_calls[*].id`. That link makes the agent trajectory visually obvious.
5. Click the SERVER span (`tools/call ArcadeGalileoDemoServer_ListEmails`) → drill into `gmail.fetch_details` → that's the bottleneck phase even with only 3 emails. Re-run with `--detailed` and that phase becomes a tree with HTTP child spans, one per message — the N+1 pattern made visible.

See [call-flow.md](call-flow.md) for the full trace tree breakdown.

## 6. Granularity control: `--detailed`

The default mode returns only top-level phase spans. Pass `--detailed` to ask the server's middleware for the full tree, including HTTPX child spans:

```bash
.venv/bin/python workflow.py --detailed
```

Expected difference in Galileo:

| Mode | `ArcadeGalileoDemoServer_ListEmails` SERVER subtree | `ArcadeGalileoDemoServer_SendEmail` SERVER subtree |
|------|-----------------------------------------------------|----------------------------------------------------|
| default (`phases only`) | **6 spans**: SERVER root + auth.validate + gmail.list_messages + gmail.fetch_details + format_response + 1 internal middleware span | **5 spans**: SERVER root + auth.validate + gmail.send_message + format_response + 1 internal middleware span |
| `--detailed` | **9 spans**: above + `GET messages` + `GET messages/<id>` × 3 (with `max_results=3`) | **6 spans**: above + `POST messages/send` |

This is the SEP-2448 filtering knob: server vendors decide what to expose, agents opt into how much detail they want. The number of dropped spans is reported back inline: `(3 additional spans available with --detailed)` after `ArcadeGalileoDemoServer_ListEmails`, `(1 additional spans available with --detailed)` after `ArcadeGalileoDemoServer_SendEmail`.

## 7. Customize the demo

### Change the user query

Pass it on the command line:

```bash
.venv/bin/python workflow.py "List my 5 most recent unread emails"
```

Or edit the default in `workflow.py`'s `parse_args()`. Use the literal string `$ARCADE_USER_ID` and the agent will substitute your `.env` value at runtime.

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

**`ModuleNotFoundError: No module named 'arcade_core'`** (or `arcade_tdk`, `arcade_mcp_server`, `arcade_serve`) — macOS only
`uv` writes the editable-install `.pth` files with the `UF_HIDDEN` flag, which causes Python's `site.py` to skip them silently. Clear the flag:

```bash
chflags nohidden .venv/lib/python*/site-packages/_editable_impl_*.pth \
                 .venv/lib/python*/site-packages/_virtualenv.pth
```

After this, **always invoke the venv's python directly** — `uv run python ...` re-applies the hidden flag on every invocation and the error returns. Use `.venv/bin/python ...` (or `source .venv/bin/activate && python ...`).

Confirm the fix worked: `stat -f "%Sf" .venv/lib/python*/site-packages/_editable_impl_arcade_core.pth` should print `-` (no flags), not `hidden`. You can also set `PYTHONVERBOSE=1` and look for `Skipping hidden .pth file:` lines if the flag is back.

**`ConnectError: All connection attempts failed` (when running `workflow.py`)**
The local server isn't running, or it's listening on a different port. Confirm Terminal 1 shows `Listening on http://127.0.0.1:8000/mcp`. Check `lsof -i :8000` to see what's listening.

**MCP OAuth browser flow fails / hangs**
Port 9905 must be free for the OAuth callback. If something else is using it, edit `OAUTH_CALLBACK_PORT` in `workflow.py`. To force fresh MCP OAuth, delete `.oauth_tokens.json` and `.oauth_client.json`.

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
The server isn't advertising the SEP-2448 capability. Likely cause: stale `arcade-mcp-server` install. Run `uv sync` again to refresh the editable install from `../arcade-mcp/libs/arcade-mcp-server/`. If still false, confirm the sibling checkout is on a branch that includes PR #797. A working install prints:

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
The traceparent stitch failed. Almost always means `propagator.inject(carrier)` ran outside the `ToolSpan`'s context. Verify `_call_mcp_tool_with_passback` does `propagator.inject(carrier)` *inside* the `with otel.start_galileo_span(tool):` block.
