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
2. **Add a payment method** at https://platform.openai.com/account/billing — new accounts without billing can't call `gpt-4o-mini`. This is the #1 "why doesn't my key work" gotcha.
3. Create a key at https://platform.openai.com/api-keys → **Create new secret key** → copy the `sk-...` value.

### Arcade → `ARCADE_API_KEY`

1. Sign up at https://api.arcade.dev/dashboard/register
2. Go to https://api.arcade.dev/dashboard/api-keys → **Create API Key** → copy the `arc_...` value.
3. Prefer project-scoped keys (prefix `arc_proj...`) — they're revocable without affecting other projects.

### Galileo → `GALILEO_API_KEY` (+ `GALILEO_CONSOLE_URL` for non-SaaS clusters)

1. Sign up on your cluster's console URL:
   - **Default SaaS**: https://app.galileo.ai/sign-up
   - **demo-v2**: https://console.demo-v2.galileocloud.io/
   - **Self-hosted / other clusters**: whatever console URL your team uses
2. Once logged in, go to **Settings → API Keys** in the console and create a key.
3. If you're not on the default SaaS cluster, also note your console URL — you'll set `GALILEO_CONSOLE_URL` in `.env` below. The Python SDK reads it automatically; no code change needed.

## 2. Clone and configure

```bash
git clone <this repo>
cd arcade_galileo_demo
cp .env.example .env
```

Edit `.env` and fill in the four values:

```env
OPENAI_API_KEY=sk-...
ARCADE_API_KEY=arc_...
GALILEO_API_KEY=...

# Only if targeting a non-SaaS cluster; omit otherwise.
GALILEO_CONSOLE_URL=https://console.demo-v2.galileocloud.io/

GALILEO_PROJECT=arcade-galileo-demo
GALILEO_LOG_STREAM=dev

USER_ID=you@example.com
```

**About `USER_ID`**: Arcade requires it on every tool call so it can scope OAuth tokens per end-user. For the default `math` toolkit it's cosmetic — any stable string works. If you later swap to an OAuth toolkit (Gmail, Slack, GitHub-private), use your real email so Arcade can cache OAuth tokens between runs for that identity.

## 3. First run

```bash
uv run python agent.py
```

On first invocation, `uv` creates `.venv/`, fetches Python 3.12 if needed, and installs from `uv.lock` — takes ~30 seconds. Subsequent runs start instantly.

**Expected console output** (approximate — the LLM paraphrases):

```
17 × 23 = 391, and √391 ≈ 19.77.
```

If you see this, the LLM, Arcade, and your API keys are all working. Next, verify the Galileo side.

## 4. See the trace in Galileo

1. Open your Galileo console URL (the same one in `GALILEO_CONSOLE_URL`, or https://app.galileo.ai if default).
2. Navigate to project **`arcade-galileo-demo`** → log stream **`dev`**. Both are auto-created on first write if they didn't exist.
3. Open the most recent trace. You should see:
   - A **workflow span** named `main` at the root.
   - Three **LLM spans** (Chat Completions calls): the first two return `tool_calls`, the third is the final text answer.
   - Two **tool spans** named `run_arcade_tool` — one for the multiply, one for the sqrt. Each shows inputs and outputs.
4. Click an LLM span → **Messages** tab → you'll see the prompt and the `tool_calls[0].function.arguments`. Click the corresponding tool span → input/output match those arguments. That match is the `tool_call_id` link Galileo uses to thread the agent trajectory.

See [call-flow.md](call-flow.md) for a detailed explanation of the span tree and what each field means.

## 5. Customize the demo

### Change the prompt

Edit `PROMPT` in `agent.py`. For the `math` toolkit, any multi-step arithmetic prompt produces a multi-tool-call trace.

### Swap the Arcade toolkit

Change `TOOLKIT` in `agent.py`. Options:

| Toolkit | Auth needed? | What it shows |
|---|---|---|
| `math` | No | Default. Clean single-command demo, multi-step tool use. |
| `search` / `web` | No (uses Arcade's hosted credentials) | More impressive — real web search result in the trace. |
| `google` (Gmail/Drive/Calendar), `slack`, `github` | **Yes (OAuth)** | Showcases Arcade's managed-OAuth superpower. **First run** returns an authorization URL the user must visit; subsequent runs reuse the cached token for that `USER_ID`. |

For OAuth toolkits, plan the demo as a two-phase story: first run opens the auth URL (surface it to the audience — explain that Arcade is managing the OAuth flow), second run uses the real tool.

### Use a different LLM

The loop assumes OpenAI's Chat Completions shape. To swap:
- Different OpenAI model: change `MODEL` in `agent.py` (any function-calling-capable model works — `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`).
- Different provider (Anthropic, etc.): you'd swap both the client AND the message/tool_call handling. Galileo has wrappers for other providers in the same `galileo.<provider>` shape; check the Galileo SDK docs for the current list. The loop itself needs small changes since Anthropic's tool-use format differs from OpenAI's.

## Troubleshooting

**`KeyError: 'USER_ID'`** (or any env var)
You didn't create `.env`, or it's missing a value. Re-run `cp .env.example .env` and fill all five keys.

**`openai.AuthenticationError: Incorrect API key`**
The `OPENAI_API_KEY` is wrong, revoked, or from a different org. Check https://platform.openai.com/api-keys.

**`openai.PermissionDeniedError` or `insufficient_quota`**
OpenAI billing isn't set up on the account the key belongs to. Add a payment method at https://platform.openai.com/account/billing.

**`arcadepy.AuthenticationError` / `401`**
The `ARCADE_API_KEY` is wrong or revoked. Regenerate at https://api.arcade.dev/dashboard/api-keys (Arcade stores only a hash — you can't retrieve a lost key, only regenerate).

**`galileo` errors about `GALILEO_CONSOLE_URL`**
If you set `GALILEO_CONSOLE_URL`, the SDK tries to reach that host. Confirm it's reachable from your network (e.g., `curl https://console.demo-v2.galileocloud.io/`). For default SaaS, leave `GALILEO_CONSOLE_URL` unset.

**No trace appears in Galileo after the script finishes**
Three things to check in order:
1. Did the script reach the `finally: galileo_context.flush()` line? If it crashed early on imports, no spans were ever created.
2. Are you looking at the right project / log stream in the right cluster? `GALILEO_CONSOLE_URL` must match the console you're logged into.
3. `GALILEO_API_KEY` must belong to the same cluster as `GALILEO_CONSOLE_URL`. A key from app.galileo.ai won't work against demo-v2.

**Tool span says `"ERROR: ..."`**
Arcade returned `status=failed`. The error message is in the span output. Common causes: the LLM invented a tool name that doesn't exist in the toolkit, or passed arguments with the wrong shape (strings vs ints). This is exactly what Galileo is there to surface — no code change needed to see the failure.

**Galileo spans show the LLM response but no tool spans**
The LLM chose not to call a tool (answered directly from its own knowledge). Strengthen the prompt — e.g., add "Use tools. Do not compute from memory." The `math` toolkit prompt in `agent.py` already has this.
