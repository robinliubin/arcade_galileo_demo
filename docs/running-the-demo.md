# Running the demo

A runbook you can follow top-to-bottom. If you're presenting live, keep this open on one screen and the terminal + Galileo UI on another.

## 0. Prereqs (one-time)

**Install `uv`** (Python project manager — handles Python toolchain, venv, and deps):

```bash
# macOS
brew install uv

# Linux / WSL / alternative
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Verify: `uv --version` should print `uv 0.11+` or newer.

Nothing else to install. `uv` will fetch Python 3.12 automatically on first run (pinned in `.python-version`).

## 1. Get the three API keys

You need an API key from OpenAI, Arcade, and Galileo. Each dashboard shows the key **only once on creation** — paste it straight into `.env` as you go.

### OpenAI → `OPENAI_API_KEY`

1. Log in at https://platform.openai.com
2. **Add a payment method** at https://platform.openai.com/account/billing — new accounts without billing can't call `gpt-4o`. This is the #1 "why doesn't my key work" gotcha.
3. Create a key at https://platform.openai.com/api-keys → **Create new secret key** → copy the `sk-...` value.

### Arcade → `ARCADE_API_KEY`

1. Sign up at https://api.arcade.dev/dashboard/register
2. Go to https://api.arcade.dev/dashboard/api-keys → **Create API Key** → copy the `arc_...` value.
3. Prefer project-scoped keys (prefix `arc_proj...`) — they're revocable without affecting other projects.

### Galileo → `GALILEO_API_KEY` (+ optional `GALILEO_CONSOLE_URL` for non-default clusters)

1. Sign up on the cluster you intend to use:
   - **Default SaaS**: https://app.galileo.ai/sign-up
   - **demo-v2**: https://console.demo-v2.galileocloud.io/
   - **Self-hosted / other**: whatever console URL your team uses
2. In the console, go to **Settings → API Keys** and create a key.
3. If you're not on the default SaaS cluster, also set `GALILEO_CONSOLE_URL` in `.env` to the cluster's console URL. The `galileo` SDK and `GalileoSpanProcessor` derive the OTLP endpoint from this — no separate endpoint override is needed.

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

**About `ARCADE_USER_ID`**: Arcade scopes Google OAuth tokens per `user_id`. Use your real Google email — Gmail and Google Docs tools cache OAuth tokens per `ARCADE_USER_ID`, so reusing the same value lets the demo skip the consent step on subsequent runs.

## 3. First run — the Google OAuth dance

```bash
uv run python workflow.py
```

On the *very* first invocation, `uv` creates `.venv/`, fetches Python 3.12 if needed, and installs from `uv.lock` — takes ~30 seconds.

Then the workflow runs. The first time `Gmail_ListEmailsByHeader` executes for a fresh `ARCADE_USER_ID`, Arcade returns an authorization URL instead of email data:

```
Loaded 3 tools: Gmail_ListEmailsByHeader, GoogleDocs_CreateDocumentFromText, Gmail_SendEmail
Executing workflow...

[arcade.execute.Gmail_ListEmailsByHeader] returned authorization_url:
  https://accounts.google.com/o/oauth2/v2/auth?... (long URL)
```

Open the URL in your browser, complete Google's consent for `gmail.readonly`, then re-run `uv run python workflow.py`. The same dance repeats for `GoogleDocs_CreateDocumentFromText` (`docs.documents` scope) and `Gmail_SendEmail` (`gmail.send` scope).

After all three scopes are granted, Arcade caches the OAuth tokens per `ARCADE_USER_ID` and subsequent runs are non-interactive.

## 4. Successful run — expected output

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
I summarized your 3 most recent emails from noreply@arcade.dev into a Google Doc
titled "Recent Arcade emails — summary" and sent the link to your inbox.

✓ View traces at: https://app.galileo.ai
  Project:    arcade-galileo-demo
  Log stream: default
```

If you see this, the LLM, Arcade OAuth, and Galileo OTLP ingest are all working. Next, verify the Galileo side.

## 5. See the trace in Galileo

1. Open your Galileo console URL (default https://app.galileo.ai, or your cluster's URL if you set `GALILEO_CONSOLE_URL`).
2. Navigate to project **`arcade-galileo-demo`** → log stream **`default`**.
3. Open the most recent trace. You should see:
   - A **Workflow span** named `arcade_galileo_workflow` at the root (typed `WorkflowSpan`, with `input=user_query` and `output=final_answer`).
   - Four **LLM spans** named `ChatOpenAI` — auto-emitted by `LangChainInstrumentor`, with OpenInference attributes (`llm.input_messages`, `llm.output_messages`, token counts).
   - Three **Tool spans** named after the Arcade tool (`Gmail_ListEmailsByHeader`, `GoogleDocs_CreateDocumentFromText`, `Gmail_SendEmail`) — typed `ToolSpan`, rendered with the green tool icon, with `input=tool_args`, `output=result`, and `tool_call_id` linking back to the LLM call that requested them.
4. Click a `ChatOpenAI` span → look at `llm.output_messages` → you'll see the model's `tool_calls` JSON. Click the matching Tool span → its `input` should match those `tool_call` arguments and its `tool_call_id` should equal the LLM's `tool_calls[*].id`. That link makes the agent trajectory visually obvious.

See [call-flow.md](call-flow.md) for the full trace tree breakdown.

## 6. Customize the demo

### Change the user query

Edit the `user_query` string in `execute_workflow()` in `workflow.py`. Any natural-language task that exercises Gmail + Docs will work.

### Swap the Arcade tools

Edit `REQUIRED_ARCADE_TOOLS` in `workflow.py`. Arcade tools are named `<Toolkit>_<ToolName>` (e.g. `Slack_SendMessage`, `Github_CreateIssue`). The demo's `load_arcade_tools()` derives the toolkit name automatically from the underscore split.

| Toolkit | Auth | Notes |
|---|---|---|
| `gmail`, `googledocs` (default) | Google OAuth, scoped per `ARCADE_USER_ID` | First-run consent flow per scope |
| `slack` | Slack OAuth | Same first-run pattern |
| `github` (private repos) | GitHub OAuth | Same |
| `math`, `web` | None | Skip the OAuth dance entirely |

### Use a different LLM

Swap `ChatOpenAI` in `create_agent()` for any other LangChain chat model that supports `bind_tools`:

```python
from langchain_anthropic import ChatAnthropic
llm = ChatAnthropic(model="claude-sonnet-4-6", ...)
```

`LangChainInstrumentor` is provider-agnostic, so the OpenInference spans still flow to Galileo unchanged. (You'd add `langchain-anthropic` via `uv add langchain-anthropic` and set the right API key in `.env`.)

### Send traces somewhere else

Because the demo uses standard OTLP transport, switching observability backends is a one-line change to env vars. Set `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and `OTEL_EXPORTER_OTLP_TRACES_HEADERS` to your destination's values, or modify `instrumentation.py` to point at a different endpoint. Common targets: Jaeger (`http://localhost:4318/v1/traces`), Honeycomb, Tempo, Langtrace.

## Troubleshooting

**`Error: GALILEO_API_KEY and GALILEO_PROJECT must be set`**
Missing env vars. Check `.env` exists and has both values filled in.

**`Error: Missing required environment variables`** (from `validate_environment()`)
The list of missing variables is printed. Most common: `ARCADE_USER_ID` not set (it was renamed from `USER_ID` — old `.env` files need updating).

**`openai.AuthenticationError: Incorrect API key`**
The `OPENAI_API_KEY` is wrong, revoked, or from a different org. Check https://platform.openai.com/api-keys.

**`openai.PermissionDeniedError` / `insufficient_quota`**
OpenAI billing isn't set up. Add a payment method at https://platform.openai.com/account/billing.

**`arcadepy.AuthenticationError` / `401`**
The `ARCADE_API_KEY` is wrong. Regenerate at https://api.arcade.dev/dashboard/api-keys.

**Tool result is an authorization URL instead of data**
First-run OAuth flow. Open the URL, complete consent, re-run. Each scope (`gmail.readonly`, `docs.documents`, `gmail.send`) needs its own one-time consent.

**`Failed to export span batch code: 401, reason: Unauthorized`** (printed mid-run by the span processor)
Galileo's OTLP collector rejected the request. The workflow keeps running — OTLP export is async and silent on failure — but no spans reach Galileo. With the `GalileoSpanProcessor` integration the surface area is small; check, in order:
1. `GALILEO_API_KEY` in `.env` is set, has no surrounding quotes or trailing whitespace, and isn't expired. Regenerate at **Settings → API Keys** if unsure.
2. **Cluster mismatch.** The key must be issued on the cluster `GALILEO_CONSOLE_URL` points at. A key from app.galileo.ai will not authenticate against demo-v2.galileocloud.io and vice-versa. If you're targeting a non-default cluster, confirm `GALILEO_CONSOLE_URL` in `.env` matches the URL where you created the key.
3. `galileo_context.init(...)` raised at startup but you ignored the error. Re-run and read the first few lines of stderr — auth failures usually surface there before the OTLP exporter even starts.

**No trace appears in Galileo after the script finishes** (no 401 either)
1. Check `force_flush()` was reached. If the script crashed during imports, no spans were created.
2. Check you're looking at the right project / log stream / cluster. `GALILEO_PROJECT` in `.env` must match the project name in the Galileo UI.
3. Check `GALILEO_API_KEY` belongs to the same cluster as `GALILEO_CONSOLE_URL`. A key from app.galileo.ai won't authenticate against a self-hosted cluster.

**Galileo trace shows `ChatOpenAI` spans but no `arcade.execute.*` or workflow root**
The manual `tracer.start_as_current_span(...)` calls aren't running. Likely cause: an exception in `load_arcade_tools()` or `create_agent()` before `execute_workflow()` is reached. Check the console for stack traces.

**Galileo trace shows `arcade.execute.*` spans but no `ChatOpenAI`**
`LangChainInstrumentor().instrument(...)` ran *after* the `ChatOpenAI` was constructed. Verify `from instrumentation import tracer` is at the top of `workflow.py`, before any `langchain_*` imports — and verify `instrumentation.py` imports `LangChainInstrumentor` and calls `.instrument(...)` at module load.

**`arcade.tool.status = "failed"` in a span**
Arcade returned `result.status == "failed"`. The error message is in `arcade.tool.result`. Common causes: LLM hallucinated a tool name, or passed arguments with wrong shape (strings vs ints).
