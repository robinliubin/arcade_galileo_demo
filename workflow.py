"""LangChain agent loop for the Arcade x Galileo demo.

Three tools, one trace:

* Discovers ``Gmail_ListEmailsByHeader``, ``GoogleDocs_CreateDocumentFromText``,
  and ``Gmail_SendEmail`` from Arcade (OpenAI-formatted schemas).
* Binds them to a ``ChatOpenAI`` LangChain runnable.
* Runs a multi-round loop: LLM picks tools → Arcade executes → repeat
  until the model produces a final answer (or ``MAX_WORKFLOW_ROUNDS`` is hit).

OpenTelemetry is configured by the ``instrumentation`` import below.
That import has side effects (sets up the OTLP exporter, registers the global
``TracerProvider``, installs the LangChain instrumentor) and **must** run
before the LangChain agent is constructed — otherwise the auto-instrumentation
attaches to nothing and no LLM spans reach Galileo.
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from arcadepy import Arcade, PermissionDeniedError
from galileo import otel
from galileo_core.schemas.logging.span import ToolSpan, WorkflowSpan
from langchain_openai import ChatOpenAI

# Imported for two reasons: (a) side effects — configures Galileo's OTLP processor +
# LangChain auto-instrumentation as a side effect of module load; (b) explicit access to
# the tracer provider so we can `force_flush()` + `shutdown()` cleanly on exit.
from instrumentation import tracer_provider as _tracer_provider


# Constants
MAX_WORKFLOW_ROUNDS = 5
DEFAULT_LLM_MODEL = "gpt-4o"
DEFAULT_LLM_TEMPERATURE = 0.7

# Required Arcade tools for this demo workflow
REQUIRED_ARCADE_TOOLS = [
    "Gmail_ListEmailsByHeader",
    "GoogleDocs_CreateDocumentFromText",
    "Gmail_SendEmail",
]


def validate_environment() -> None:
    """
    Validate that all required environment variables are set.

    Raises:
        SystemExit: If any required environment variable is missing.
    """
    required_vars = {
        "OPENAI_API_KEY": "OpenAI API key for LLM operations",
        "ARCADE_API_KEY": "Arcade API key for tool execution",
        "ARCADE_USER_ID": "Arcade user ID for tool authorization",
        "GALILEO_API_KEY": "Galileo API key for observability (set in instrumentation.py)",
        "GALILEO_PROJECT": "Galileo project name (set in instrumentation.py)",
    }

    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        print("Error: Missing required environment variables:", file=sys.stderr)
        for var in missing_vars:
            print(f"  - {var}: {required_vars[var]}", file=sys.stderr)
        sys.exit(1)


def load_arcade_tools() -> Tuple[List[Dict[str, Any]], Arcade, str]:
    """
    Load OpenAI-formatted schemas for the required Arcade tools.

    Returns:
        Tuple of (selected tool schemas, configured Arcade client, user_id).

    Raises:
        RuntimeError: If none of the required tools are discovered.
    """
    arcade = Arcade()
    user_id = os.environ["ARCADE_USER_ID"]

    needed_toolkits = sorted({name.split("_", 1)[0].lower() for name in REQUIRED_ARCADE_TOOLS})
    by_name: Dict[str, Dict[str, Any]] = {}
    for toolkit in needed_toolkits:
        for t in arcade.tools.formatted.list(format="openai", toolkit=toolkit):
            by_name[t["function"]["name"]] = t

    tools = [by_name[name] for name in REQUIRED_ARCADE_TOOLS if name in by_name]

    if not tools:
        raise RuntimeError(
            f"No required tools found. Expected tools containing: {REQUIRED_ARCADE_TOOLS}"
        )

    print(f"Loaded {len(tools)} tools: {', '.join(t['function']['name'] for t in tools)}")
    return tools, arcade, user_id


def create_agent(tools: List[Dict[str, Any]]) -> Any:
    """
    Create a LangChain agent with Arcade tools.

    Args:
        tools: List of tool definitions in OpenAI format.

    Returns:
        A LangChain runnable that can invoke the LLM with tools.

    Raises:
        ValueError: If OPENAI_API_KEY is not set.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY must be set")

    llm = ChatOpenAI(
        model=DEFAULT_LLM_MODEL,
        temperature=DEFAULT_LLM_TEMPERATURE,
        api_key=api_key,
    )

    return llm.bind_tools(tools)


def _execute_arcade_tool(
    arcade: Arcade,
    tool_name: str,
    tool_args: Dict[str, Any],
    user_id: str,
) -> Any:
    """
    Execute an Arcade tool, handling first-run OAuth interactively.

    When ``user_id`` has not yet granted the scope the tool requires, Arcade
    raises ``PermissionDeniedError`` with body ``tool_authorization_required``.
    We trigger ``arcade.tools.authorize(...)``, print the consent URL, block
    on ``arcade.auth.wait_for_completion(...)`` until the user finishes the
    Google flow, then retry the execute. After the first successful auth per
    scope, Arcade caches the token for that ``user_id`` and subsequent runs
    skip this dance.
    """
    try:
        return arcade.tools.execute(tool_name=tool_name, input=tool_args, user_id=user_id)
    except PermissionDeniedError as e:
        if "tool_authorization_required" not in str(e):
            raise
        auth = arcade.tools.authorize(tool_name=tool_name, user_id=user_id)
        if auth.status != "completed":
            print(
                f"\n  Authorization required for {tool_name}.\n"
                f"  Open this URL in your browser to authorize Arcade:\n\n"
                f"    {auth.url}\n"
            )
            arcade.auth.wait_for_completion(auth)
            print("  Authorization complete. Continuing...\n")
        return arcade.tools.execute(tool_name=tool_name, input=tool_args, user_id=user_id)


def execute_workflow(
    agent: Any,
    arcade: Arcade,
    user_id: str,
) -> Optional[str]:
    """
    Execute the email summary workflow with complete tracing.

    This workflow:
    1. Checks emails from today
    2. Creates a Google Doc with an email summary
    3. Sends an email with the doc link

    All operations are traced to Galileo via OpenTelemetry spans.

    Args:
        agent: LangChain agent with bound tools.
        arcade: Arcade client for tool execution.
        user_id: User ID for Arcade tool authorization.

    Returns:
        Final response from the agent, or None if workflow doesn't complete.

    Raises:
        Exception: If tool execution fails.
    """
    user_query = (
        "Find the 3 most recent emails I have received from "
        "alex.salazar@arcade.dev. Summarize them into a single short Google Doc, "
        "then email me the link to that doc. Use tools."
    )

    workflow = WorkflowSpan(name="arcade_galileo_workflow", input=user_query)
    with otel.start_galileo_span(workflow):
        messages: List[Any] = [{"role": "user", "content": user_query}]
        final: Optional[str] = None

        for _round_num in range(1, MAX_WORKFLOW_ROUNDS + 1):
            ai_message = agent.invoke(messages)
            messages.append(ai_message)

            tool_calls = getattr(ai_message, "tool_calls", None) or []
            if not tool_calls:
                final = ai_message.content if hasattr(ai_message, "content") else str(ai_message)
                break

            for tc in tool_calls:
                tool_name = tc["name"]
                tool_args = tc["args"]
                tool_call_id = tc["id"]

                # ToolSpan + start_galileo_span makes Galileo render this as a
                # proper Tool span (green icon, drill-down), not a Workflow span.
                tool = ToolSpan(
                    name=tool_name,
                    input=json.dumps(tool_args),
                    tool_call_id=tool_call_id,
                )
                with otel.start_galileo_span(tool):
                    result = _execute_arcade_tool(arcade, tool_name, tool_args, user_id)

                    if result.status == "failed":
                        output = f"ERROR: {result.output.error if result.output else 'unknown'}"
                    else:
                        output = json.dumps(result.output.value) if result.output else ""

                    tool.output = output

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": output,
                })

        workflow.output = final if final is not None else "(workflow did not complete)"
        return final


def main() -> None:
    """
    Main entry point for the Arcade + Galileo integration demo.

    Validates environment, loads tools, creates agent, and executes workflow.
    """
    print("=" * 60)
    print("Arcade + Galileo Integration Demo")
    print("=" * 60)
    print()

    validate_environment()

    try:
        tools, arcade, user_id = load_arcade_tools()
        agent = create_agent(tools)

        print("Executing workflow...\n")

        result = execute_workflow(agent, arcade, user_id)

        if result:
            print("\n" + "=" * 60)
            print("Workflow completed successfully!")
            print("=" * 60)
            print(f"\nResult:\n{result}")
        else:
            print("\n" + "=" * 60)
            print("Workflow did not complete")
            print("=" * 60)

        # Build the trace URL from Galileo's resolved cluster (reads
        # GALILEO_CONSOLE_URL via GalileoPythonConfig) and the project /
        # log-stream IDs that galileo_context.init(...) resolved at startup.
        # This is the same pattern as galileo-test/agents/10_otel_openinference.ipynb.
        from galileo import galileo_context
        from galileo.config import GalileoPythonConfig

        config = GalileoPythonConfig.get()
        logger = galileo_context.get_logger_instance()
        project_id = getattr(logger, "project_id", None)
        log_stream_id = getattr(logger, "log_stream_id", None)

        if project_id and log_stream_id:
            print(
                f"\n✓ View this trace at: "
                f"{config.console_url}project/{project_id}/log-streams/{log_stream_id}"
            )
        else:
            print(f"\n✓ View traces at: {config.console_url}")
            print(f"  Project:    {os.getenv('GALILEO_PROJECT')}")
            print(f"  Log stream: {os.getenv('GALILEO_LOG_STREAM', 'default')}")
    finally:
        # BatchSpanProcessor buffers spans; flush + shutdown so the trace
        # leaves the machine before the process exits.
        _tracer_provider.force_flush()
        _tracer_provider.shutdown()


if __name__ == "__main__":
    main()
