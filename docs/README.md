# arcade_galileo_demo docs

Four documents, read in order:

| File | Who it's for | What it answers |
|---|---|---|
| [architecture.md](architecture.md) | Everyone | What the pieces are and how they fit together — the local Arcade MCP server, SEP-2448 passback, the LangChain agent, OpenAI, Galileo, and how `server.py` + `instrumentation.py` + `workflow.py` glue them. |
| [call-flow.md](call-flow.md) | Anyone customizing the demo | Step-by-step sequence of one demo run — server boot, agent boot, MCP OAuth, Google OAuth, multi-round agent loop with passback ingest. Includes the trace tree. |
| [oauth-flows.md](oauth-flows.md) | Anyone debugging auth | Deep dive on the two OAuth flows — MCP OAuth 2.1 (PKCE) and Google OAuth (brokered by Arcade): what's sent on the wire, what's received, why each flow exists, where state is persisted, and how the JWT `email` claim chains the two flows together. |
| [running-the-demo.md](running-the-demo.md) | Anyone running the demo | Runbook: prereqs, API keys, two-terminal startup, OAuth flows, expected output, troubleshooting, customization. |

If you're presenting this live, [running-the-demo.md](running-the-demo.md) is the primary script — the other three are the "what you're seeing" explainers you open alongside the two terminals and the Galileo UI.
