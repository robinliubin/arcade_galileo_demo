# Call flow

What actually happens, in order, when you run `uv run python workflow.py` with the default user query (summarize 3 recent Arcade emails into a Google Doc, then email the link).

## Sequence diagram

```mermaid
sequenceDiagram
    autonumber
    actor user as You
    participant wf as workflow.py
    participant ins as instrumentation.py
    participant otlp as Galileo<br/>(OTLP HTTP)
    participant arcade as Arcade
    participant openai as OpenAI

    user->>wf: uv run python workflow.py
    wf->>ins: import (side-effecting)
    ins->>ins: load_dotenv(); validate Galileo env
    ins->>ins: galileo_context.init(project, log_stream)
    ins->>ins: TracerProvider + add_galileo_span_processor(GalileoSpanProcessor(...))
    ins->>ins: LangChainInstrumentor().instrument(...)
    ins-->>wf: tracer

    wf->>arcade: tools.formatted.list(format="openai", toolkit="gmail")
    arcade-->>wf: [Gmail_ListEmailsByHeader, Gmail_SendEmail, ...]
    wf->>arcade: tools.formatted.list(format="openai", toolkit="googledocs")
    arcade-->>wf: [GoogleDocs_CreateDocumentFromText, ...]
    wf->>wf: filter to REQUIRED_ARCADE_TOOLS (3 tools)

    wf->>wf: ChatOpenAI(model="gpt-4o").bind_tools(tools)

    rect rgba(180,200,255,0.18)
    note over wf,otlp: tracer.start_as_current_span("arcade_galileo_workflow")

    rect rgba(220,220,255,0.25)
    note over wf,openai: round 1 — agent picks Gmail_ListEmailsByHeader
    wf->>openai: agent.invoke(messages)
    note right of ins: LangChainInstrumentor auto-emits ChatOpenAI span
    openai-->>wf: AIMessage(tool_calls=[Gmail_ListEmailsByHeader(...)])
    end

    rect rgba(220,255,220,0.25)
    note over wf,arcade: arcade.execute.Gmail_ListEmailsByHeader span
    wf->>arcade: tools.execute("Gmail_ListEmailsByHeader", {sender, limit:3}, user_id)
    arcade-->>wf: [{subject, snippet, ...}, ...]
    end

    rect rgba(220,220,255,0.25)
    note over wf,openai: round 2 — agent picks GoogleDocs_CreateDocumentFromText
    wf->>openai: agent.invoke(messages incl. email list)
    openai-->>wf: AIMessage(tool_calls=[GoogleDocs_CreateDocumentFromText(...)])
    end

    rect rgba(220,255,220,0.25)
    wf->>arcade: tools.execute("GoogleDocs_CreateDocumentFromText", {title, text}, user_id)
    arcade-->>wf: {document_id, document_url}
    end

    rect rgba(220,220,255,0.25)
    note over wf,openai: round 3 — agent picks Gmail_SendEmail
    wf->>openai: agent.invoke(messages incl. doc URL)
    openai-->>wf: AIMessage(tool_calls=[Gmail_SendEmail(...)])
    end

    rect rgba(220,255,220,0.25)
    wf->>arcade: tools.execute("Gmail_SendEmail", {to, subject, body}, user_id)
    arcade-->>wf: {message_id, status:"sent"}
    end

    rect rgba(220,220,255,0.25)
    note over wf,openai: round 4 — agent produces final answer
    wf->>openai: agent.invoke(messages incl. send confirmation)
    openai-->>wf: AIMessage(content="Done. Doc created and emailed.")
    end

    end

    wf->>wf: provider.force_flush() + shutdown()
    wf-)otlp: BatchSpanProcessor sends OTLP HTTP/protobuf
    wf-->>user: prints "View traces at: https://app.galileo.ai"
```

## Step-by-step

**1. Module init (before `main()`)**

When Python imports `workflow.py`, the `from instrumentation import tracer` line at the top fires `instrumentation.py`'s side effects:

- `load_dotenv()` pulls `.env` into `os.environ`.
- `GALILEO_API_KEY` and `GALILEO_PROJECT` are validated; missing ones cause an immediate `sys.exit(1)`.
- `galileo_context.init(project=..., log_stream=...)` resolves the Galileo cluster from `GALILEO_CONSOLE_URL` (or default SaaS), authenticates, and bootstraps the project + log stream.
- A `TracerProvider` is constructed and `galileo.otel.GalileoSpanProcessor(project=..., logstream=...)` is attached via `otel.add_galileo_span_processor(provider, processor)`. The processor wraps the OTLP exporter and injects routing headers internally.
- `LangChainInstrumentor().instrument(tracer_provider=...)` patches LangChain so future `ChatOpenAI` constructions are auto-traced.

This ordering matters: the instrumentor must be active *before* `ChatOpenAI(...)` is constructed in `create_agent()`, otherwise the LLM spans never fire.

**2. Tool discovery**

`load_arcade_tools()` derives the unique toolkit names from `REQUIRED_ARCADE_TOOLS` (`gmail`, `googledocs`), fetches each toolkit's full tool list from Arcade in OpenAI function-calling shape, then filters down to exactly the three required tools:

```python
needed_toolkits = {name.split("_", 1)[0].lower() for name in REQUIRED_ARCADE_TOOLS}
by_name = {}
for toolkit in needed_toolkits:
    for t in arcade.tools.formatted.list(format="openai", toolkit=toolkit):
        by_name[t["function"]["name"]] = t
tools = [by_name[name] for name in REQUIRED_ARCADE_TOOLS if name in by_name]
```

Iterating the pager (rather than accessing `.items`) handles toolkits with >1 page of tools — Gmail in particular spans multiple pages.

**3. Agent construction**

```python
llm = ChatOpenAI(model="gpt-4o", temperature=0.7, api_key=...)
return llm.bind_tools(tools)
```

The returned object is a LangChain runnable. Because `LangChainInstrumentor` is already active, this construction is patched and every subsequent `agent.invoke(...)` will emit a `ChatOpenAI` OpenInference span.

**4. The agent loop**

Wrapped in `tracer.start_as_current_span("arcade_galileo_workflow")` so the whole agent trajectory has a single root in Galileo:

```python
for round_num in range(1, MAX_WORKFLOW_ROUNDS + 1):
    ai_message = agent.invoke(messages)
    messages.append(ai_message)
    if not ai_message.tool_calls:
        return ai_message.content     # done
    for tc in ai_message.tool_calls:
        with tracer.start_as_current_span(f"arcade.execute.{tc['name']}"):
            result = arcade.tools.execute(tool_name=tc["name"], input=tc["args"], user_id=...)
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": ...})
```

For the default user query, the loop converges in **4 rounds**:

| Round | LLM picks | Arcade returns |
|---|---|---|
| 1 | `Gmail_ListEmailsByHeader(sender="noreply@arcade.dev", limit=3)` | List of 3 email metadata records |
| 2 | `GoogleDocs_CreateDocumentFromText(title=..., text=summary)` | `{document_id, document_url}` |
| 3 | `Gmail_SendEmail(to=user_email, subject=..., body=...)` | `{message_id, status:"sent"}` |
| 4 | Final answer (no `tool_calls`) | — |

**5. OAuth on first run**

The very first time `arcade.tools.execute("Gmail_ListEmailsByHeader", ...)` runs for a fresh `ARCADE_USER_ID`, Arcade returns an authorization URL instead of email data — open it, complete Google's consent for `gmail.readonly`, then re-run. `GoogleDocs_CreateDocumentFromText` and `Gmail_SendEmail` each have their own scope (`docs.documents`, `gmail.send`) and will each return a one-time URL on their first call too.

After all three scopes are granted, Arcade caches the OAuth tokens per `ARCADE_USER_ID` and subsequent runs are non-interactive.

**6. Flush on exit**

```python
finally:
    provider.force_flush()
    provider.shutdown()
```

`BatchSpanProcessor` buffers spans locally and ships them in batches every few seconds. Without `force_flush`, a fast-exiting script can return before the spans leave your machine; the `finally` placement means even an exception still ships whatever was captured.

## What the Galileo trace looks like

In the Galileo UI, under project `arcade-galileo-demo` / log stream `default`, one invocation of `workflow.py` produces **one trace** shaped like:

```
arcade_galileo_workflow                        (WorkflowSpan — name, input=user_query, output=final_answer)
├── ChatOpenAI                                 (OpenInference, auto)
│   llm.input_messages: [{"role":"user","content":"Find the 3 most recent emails ..."}]
│   llm.output_messages: [{"role":"assistant","tool_calls":[...]}]
│   llm.token_count.prompt / completion captured
├── Gmail_ListEmailsByHeader                   (ToolSpan — name, input=tool_args JSON, output=result, tool_call_id)
├── ChatOpenAI                                 (round 2 — sees email list, picks doc creation)
├── GoogleDocs_CreateDocumentFromText          (ToolSpan)
├── ChatOpenAI                                 (round 3 — sees doc URL, picks send email)
├── Gmail_SendEmail                            (ToolSpan)
└── ChatOpenAI                                 (round 4 — produces final natural-language answer)
```

**What to point at during a live demo:**

- The **workflow root** shows end-to-end latency and the `workflow.user_query` attribute.
- Each **ChatOpenAI** span shows the exact prompt and response — including the `tool_calls` field that proves the LLM is choosing tools, not hallucinating.
- Each **arcade.execute.\*** span shows the input the LLM passed and the result Arcade returned. Comparing the input to the preceding ChatOpenAI's `tool_calls[0].function.arguments` makes the agent-trajectory link visible.

## Pitfalls the trace helps you catch

- **Tool hallucination**: LLM invents a tool name not in `REQUIRED_ARCADE_TOOLS` → Arcade returns `failed` → `arcade.execute.*` span's `arcade.tool.status` = `"failed"` → next ChatOpenAI span shows the model's recovery (or further failure).
- **Argument-shape drift**: LLM passes `{"limit":"3"}` (string) when Arcade expects an int → `arcade.tool.args` shows the bad type.
- **Silent OAuth stall**: first `Gmail_ListEmailsByHeader` returns an authorization URL instead of emails → `arcade.tool.result` is the URL string, and the next ChatOpenAI span shows the model "responding" to the URL instead of email content. Foreground this for live demos.
- **No spans appear in Galileo**: `force_flush()` was skipped (early crash before `finally`), or `LangChainInstrumentor().instrument(...)` ran *after* `ChatOpenAI(...)` was constructed — both produce the same symptom, both are guarded against by the current code structure.

All four failure modes are visually obvious in the Galileo trace before you even re-read the agent code.
