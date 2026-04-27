# arcade_galileo_demo docs

Three documents, read in order:

| File | Who it's for | What it answers |
|---|---|---|
| [architecture.md](architecture.md) | Everyone | What the pieces are and how they fit together — MCP, Arcade, Galileo (as an OTLP destination), OpenAI via LangChain, and how `instrumentation.py` + `workflow.py` glue them. |
| [call-flow.md](call-flow.md) | Anyone customizing the demo | Step-by-step sequence of one `uv run python workflow.py` invocation — from prompt to Galileo trace. Includes the trace tree. |
| [running-the-demo.md](running-the-demo.md) | Anyone running the demo | Runbook: prereqs, API keys, OAuth one-time setup, expected output, troubleshooting, customization. |

If you're presenting this live, [running-the-demo.md](running-the-demo.md) is the primary script — the other two are the "what you're seeing" explainers you open alongside the terminal and the Galileo UI.
