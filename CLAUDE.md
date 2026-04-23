# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Demo showcasing **MCP tool calls via Arcade, observed in the Galileo platform**. The three moving parts a future session needs to hold in mind:

- **MCP (Model Context Protocol)** — the transport: the demo's LLM/agent reaches tools through an MCP client.
- **Arcade** (arcade.dev) — the tool provider: supplies pre-authenticated integrations exposed as MCP servers, so the demo can call real third-party APIs without building its own auth/OAuth layer.
- **Galileo** (rungalileo.io) — the observability layer: the demo's traces, tool calls, and LLM spans are logged to Galileo so the user can inspect MCP-mediated tool behavior end-to-end.

The point of the demo is the *join* of these three — not any one of them in isolation. When adding code, preserve that story: an agent loop that calls Arcade-hosted MCP tools while emitting Galileo traces.

## Stack

- **Python 3.12** (pinned via `.python-version`).
- **`uv`** for Python toolchain, venv, and dependencies — never invoke `pip` or `python -m venv` directly. `uv.lock` is committed and authoritative.
- Deps live in `pyproject.toml` under `[project] dependencies`. Add deps with `uv add <pkg>`, remove with `uv remove <pkg>`. Never edit `uv.lock` by hand.
- `[tool.uv] package = false` — this repo is a script project, not a library. Don't add a `[build-system]` section.

## Commands

- Run the demo: `uv run python agent.py`
- Refresh env after pulling changes: `uv sync`
- Add a dependency: `uv add <pkg>` (updates `pyproject.toml` and `uv.lock` in one step)
- Upgrade all deps: `uv lock --upgrade && uv sync`

No test suite yet.

## Architecture

Single-file demo (`agent.py`, ~70 lines). Three integrations are visible with section banners:

1. **Galileo wrapping** — `from galileo.openai import OpenAI` replaces the stock OpenAI client so every `chat.completions.create(...)` is auto-traced. `galileo_context.init(...)` at module load; `galileo_context.flush()` in `finally` to ship spans before exit (common footgun: skipping flush drops recent traces).
2. **Arcade execution** — `arcade.tools.formatted.list(format="openai", toolkit=...)` fetches tool schemas already shaped for OpenAI function-calling; iterate the pager (not `.items`) to handle multi-page toolkits. `arcade.tools.execute(tool_name=..., input=..., user_id=...)` runs the tool. Errors come back via `result.status == "failed"`, not exceptions.
3. **Agent loop** — plain `while True` over `chat.completions.create(..., tools=tools)` + `msg.tool_calls`. No framework. The `@log(span_type="tool")` decorator on `run_arcade_tool` is what ties tool executions into the Galileo trace.

Deliberate non-choices (preserve these when extending):
- No LangChain / LangGraph / CrewAI / OpenAI Agents SDK / `openai-agents-arcade` wrapper — the loop must stay raw so adopters can port it to any framework.
- No raw MCP client — the `arcadepy` SDK *is* the MCP path; Arcade is the MCP runtime. If someone asks to "show MCP literally," that's a second sibling script, not a rewrite of `agent.py`.

## Extending the demo

- **Different toolkit**: edit `TOOLKIT` and `PROMPT` in `agent.py`. No-auth toolkits (math) run in one shot; OAuth toolkits (gmail, slack, github-private) return an authorization URL from the first `arcade.tools.execute(...)` call that the user must visit once per `USER_ID`.
- **Different LLM provider**: swap the Galileo-wrapped OpenAI client for Galileo's Anthropic wrapper if it exists in the installed `galileo` version; the loop's `chat.completions` shape is OpenAI-specific, so switching providers means changing the loop too.
