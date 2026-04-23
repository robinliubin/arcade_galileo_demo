# arcade_galileo_demo docs

Three documents, read in order:

| File | Who it's for | What it answers |
|---|---|---|
| [architecture.md](architecture.md) | Everyone | What the pieces are and how they fit together — MCP, Arcade, Galileo, OpenAI, and the 70-line `agent.py` in the middle. |
| [call-flow.md](call-flow.md) | Anyone customizing the demo | Step-by-step sequence of one `uv run python agent.py` invocation — from prompt to trace. Includes what the Galileo trace looks like. |
| [running-the-demo.md](running-the-demo.md) | Anyone running the demo | Runbook: prereqs, API keys, cluster targeting, expected output, troubleshooting, customization. |

If you're presenting this live, [running-the-demo.md](running-the-demo.md) is the primary script — the other two are the "what you're seeing" explainers you open alongside the terminal and the Galileo UI.
