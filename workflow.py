"""LangChain agent for the Arcade x Galileo demo with SEP-2448 server-span passback.

Two tools, one stitched trace:

* Connects to the local Arcade MCP server (``server.py``) via streamable HTTP.
* Authenticates using MCP OAuth 2.1 (the MCP SDK opens the browser, runs PKCE,
  caches tokens to ``.oauth_*.json``).
* Discovers ``list_emails`` and ``send_email``, exposes them to a
  ``ChatOpenAI`` LangChain runnable in OpenAI function-calling shape.
* Runs a multi-round loop: LLM picks tools → MCP call with passback opt-in →
  server responds with its own phase spans inline in ``_meta.otel`` → we
  forward those to Galileo so they become children of the agent-side
  ``ToolSpan`` in the same trace.

OpenTelemetry is configured by the ``instrumentation`` import below. That
import has side effects (sets up the OTLP exporter via
``GalileoSpanProcessor``, registers the global ``TracerProvider``, installs
``LangChainInstrumentor``) and **must** run before the LangChain agent is
constructed — otherwise the auto-instrumentation attaches to nothing and no
LLM spans reach Galileo.

The ``--detailed`` flag controls how much of the server's internal tree is
returned. Without it the server returns only top-level phase spans
(``auth.validate``, ``gmail.list_messages``, ...). With it, you also get the
``HTTPXClientInstrumentor`` HTTP child spans under each phase.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from galileo import otel
from galileo_core.schemas.logging.span import ToolSpan, WorkflowSpan
from langchain_openai import ChatOpenAI
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# Side effects: configures GalileoSpanProcessor + LangChainInstrumentor.
# Also exports ``ingest_passback_to_galileo`` which we call after every
# tools/call to forward server-side spans into the same Galileo trace.
from instrumentation import (
    ingest_passback_to_galileo,
    tracer_provider as _tracer_provider,
)


MAX_WORKFLOW_ROUNDS = 5
DEFAULT_LLM_MODEL = "gpt-4o"
DEFAULT_LLM_TEMPERATURE = 0.7
DEFAULT_SERVER_URL = "http://127.0.0.1:8000/mcp"

PROJECT_ROOT = Path(__file__).resolve().parent
OAUTH_TOKEN_FILE = PROJECT_ROOT / ".oauth_tokens.json"
OAUTH_CLIENT_FILE = PROJECT_ROOT / ".oauth_client.json"
OAUTH_CALLBACK_PORT = 9905
OAUTH_REDIRECT_URI = f"http://127.0.0.1:{OAUTH_CALLBACK_PORT}/callback"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP OAuth 2.1 (handled by the MCP SDK)
# ---------------------------------------------------------------------------


class FileTokenStorage(TokenStorage):
    """Persist OAuth tokens and client registration to disk between runs."""

    async def get_tokens(self) -> OAuthToken | None:
        if OAUTH_TOKEN_FILE.exists():
            return OAuthToken.model_validate_json(OAUTH_TOKEN_FILE.read_text())
        return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        OAUTH_TOKEN_FILE.write_text(tokens.model_dump_json())

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        if OAUTH_CLIENT_FILE.exists():
            return OAuthClientInformationFull.model_validate_json(OAUTH_CLIENT_FILE.read_text())
        return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        OAUTH_CLIENT_FILE.write_text(client_info.model_dump_json())


async def _handle_oauth_redirect(authorization_url: str) -> None:
    """Open the browser for MCP OAuth consent (one-time per fresh ``.oauth_*.json``)."""
    print("\n  Opening browser for MCP OAuth authorization...")
    print(f"  URL: {authorization_url}\n")
    webbrowser.open(authorization_url)


async def _handle_oauth_callback() -> tuple[str, str | None]:
    """Start a local HTTP server, wait for the OAuth redirect, extract the code."""
    loop = asyncio.get_event_loop()
    future: asyncio.Future[tuple[str, str | None]] = loop.create_future()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            code = qs.get("code", [None])[0]
            state = qs.get("state", [None])[0]
            if code:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<h2>Authorization successful!</h2><p>You can close this tab.</p>"
                )
                loop.call_soon_threadsafe(future.set_result, (code, state))
            else:
                error = qs.get("error", ["unknown"])[0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"<h2>Authorization failed: {error}</h2>".encode())
                loop.call_soon_threadsafe(
                    future.set_exception, RuntimeError(f"OAuth error: {error}")
                )

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", OAUTH_CALLBACK_PORT), _Handler)

    def _serve() -> None:
        server.handle_request()
        server.server_close()

    await loop.run_in_executor(None, _serve)
    return await future


# ---------------------------------------------------------------------------
# Validation, schema conversion, Google-OAuth-on-first-call dance
# ---------------------------------------------------------------------------


def validate_environment() -> None:
    """Validate that all required environment variables are set.

    ``ARCADE_API_KEY`` is no longer needed by *this* process — we no longer
    call Arcade Cloud's tool-execution API. The local server still uses
    Arcade as the OAuth authorization server (token validation) and as the
    Google OAuth broker (tool ``requires_auth=Google(...)``), and reads
    those credentials from its own ``.env`` load. ``ARCADE_USER_ID`` is
    still useful here for printing / templating the user's email into the
    default query.
    """
    required_vars = {
        "OPENAI_API_KEY": "OpenAI API key for the LangChain agent",
        "ARCADE_USER_ID": "Your email — used by Arcade's Google OAuth broker",
        "GALILEO_API_KEY": "Galileo API key (set in instrumentation.py)",
        "GALILEO_PROJECT": "Galileo project name (set in instrumentation.py)",
    }
    missing = [var for var in required_vars if not os.getenv(var)]
    if missing:
        print("Error: Missing required environment variables:", file=sys.stderr)
        for var in missing:
            print(f"  - {var}: {required_vars[var]}", file=sys.stderr)
        sys.exit(1)


def _mcp_to_openai_tool(t: Any) -> dict[str, Any]:
    """Convert an MCP tool definition to OpenAI function-calling shape.

    The agent uses ``ChatOpenAI.bind_tools(...)`` which expects the OpenAI
    schema; the local server publishes JSON-Schema input shapes via the
    standard MCP ``tools/list`` response.
    """
    schema = t.inputSchema or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description or f"MCP tool: {t.name}",
            "parameters": schema,
        },
    }


def _extract_google_auth_url(result: Any) -> str | None:
    """If a tool result contains an Arcade Google-OAuth URL, return it.

    On the first ``list_emails`` / ``send_email`` for a fresh ``user_id``,
    the server returns a JSON payload with ``authorization_url`` instead of
    Gmail data — that's Arcade telling us the user needs to grant Google
    consent for the relevant scope. We surface the URL, wait for the user
    to complete consent, then retry the call.
    """
    for item in result.content:
        text = getattr(item, "text", None)
        if text and "authorization_url" in text:
            try:
                data = json.loads(text)
                return data.get("authorization_url")
            except (json.JSONDecodeError, TypeError):
                pass
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("authorization_url")
    return None


# ---------------------------------------------------------------------------
# The actual MCP tool call — with span passback
# ---------------------------------------------------------------------------


async def _call_mcp_tool_with_passback(
    session: ClientSession,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_call_id: str,
    detailed: bool,
    propagator: TraceContextTextMapPropagator,
) -> str:
    """Invoke an MCP tool, request server span passback, ingest into Galileo.

    Wraps the call in a Galileo ``ToolSpan`` so the UI renders it with the
    proper Tool icon and drill-down. Injects ``traceparent`` into
    ``_meta`` so the server's spans share our trace ID, and opts into
    passback via ``_meta.otel.traces.{request,detailed}``. On success the
    server's spans come back under ``response._meta.otel.traces.resourceSpans``
    and we forward them to Galileo via the helper from ``instrumentation``.
    """
    tool = ToolSpan(
        name=tool_name,
        input=json.dumps(tool_args),
        tool_call_id=tool_call_id,
    )
    with otel.start_galileo_span(tool):
        carrier: dict[str, str] = {}
        propagator.inject(carrier)

        meta: dict[str, Any] = {
            "traceparent": carrier.get("traceparent", ""),
            "otel": {"traces": {"request": True, "detailed": detailed}},
        }

        result = await session.call_tool(tool_name, arguments=tool_args, meta=meta)

        # Arcade's Google OAuth dance — first call per scope returns a URL
        # instead of data. Surface it, wait for the user, then retry.
        google_auth_url = _extract_google_auth_url(result)
        if google_auth_url:
            print(f"\n  Google OAuth required for {tool_name}.")
            print(f"  Open this URL in your browser to authorize:\n\n    {google_auth_url}\n")
            await asyncio.get_event_loop().run_in_executor(
                None, input, "  Press Enter after authorizing... "
            )
            print("  Retrying tool call...\n")
            result = await session.call_tool(tool_name, arguments=tool_args, meta=meta)

        text = result.content[0].text if result.content else ""
        # Cap stored tool output to keep span attributes manageable.
        tool.output = text[:5000]

        ingest_passback_to_galileo(result.meta)
        return text


# ---------------------------------------------------------------------------
# Multi-round agent loop
# ---------------------------------------------------------------------------


async def execute_workflow(
    session: ClientSession,
    mcp_tools: list[Any],
    user_query: str,
    detailed: bool,
) -> str | None:
    """Run the LangChain agent loop, with each tool call going over MCP.

    Wrapped in a Galileo ``WorkflowSpan`` so the whole trajectory anchors
    under one root in the UI.
    """
    propagator = TraceContextTextMapPropagator()
    openai_tools = [_mcp_to_openai_tool(t) for t in mcp_tools]

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY must be set")

    llm = ChatOpenAI(
        model=DEFAULT_LLM_MODEL,
        temperature=DEFAULT_LLM_TEMPERATURE,
        api_key=api_key,
    ).bind_tools(openai_tools)

    workflow = WorkflowSpan(name="arcade_galileo_workflow", input=user_query)
    with otel.start_galileo_span(workflow):
        messages: list[Any] = [{"role": "user", "content": user_query}]
        final: str | None = None

        for _round_num in range(1, MAX_WORKFLOW_ROUNDS + 1):
            ai_message = await llm.ainvoke(messages)
            messages.append(ai_message)

            tool_calls = getattr(ai_message, "tool_calls", None) or []
            if not tool_calls:
                final = ai_message.content if hasattr(ai_message, "content") else str(ai_message)
                break

            for tc in tool_calls:
                output = await _call_mcp_tool_with_passback(
                    session=session,
                    tool_name=tc["name"],
                    tool_args=tc["args"],
                    tool_call_id=tc["id"],
                    detailed=detailed,
                    propagator=propagator,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": output,
                })

        workflow.output = final if final is not None else "(workflow did not complete)"
        return final


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LangChain agent for the Arcade x Galileo demo with SEP-2448 passback",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=(
            "Find my 3 most recent emails from alex.salazar@arcade.dev. "
            "Then email a one-paragraph summary of them to me at $ARCADE_USER_ID."
        ),
        help="Natural-language task for the agent (use $ARCADE_USER_ID as a stand-in)",
    )
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="Request the full server span tree (incl. HTTPX child spans)",
    )
    parser.add_argument(
        "--server-url",
        default=DEFAULT_SERVER_URL,
        help=f"Local MCP server URL (default: {DEFAULT_SERVER_URL})",
    )
    return parser.parse_args()


def _print_galileo_trace_url() -> None:
    """Print a deep link to the Galileo trace, falling back to the project view."""
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


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    args = parse_args()

    validate_environment()
    user_id = os.environ["ARCADE_USER_ID"]
    user_query = args.query.replace("$ARCADE_USER_ID", user_id)

    print("=" * 60)
    print("Arcade + Galileo Integration Demo (server-span passback)")
    print("=" * 60)
    print(f"\n  Mode:         {'detailed (full tree)' if args.detailed else 'phases only'}")
    print(f"  MCP server:   {args.server_url}")
    print(f"  Query:        {user_query}\n")

    # MCP SDK handles OAuth 2.1 automatically:
    # On 401 it discovers the auth server (RFC 9728), runs PKCE, caches tokens.
    oauth_auth = OAuthClientProvider(
        server_url=args.server_url,
        client_metadata=OAuthClientMetadata(
            client_name="arcade-galileo-demo-agent",
            redirect_uris=[OAUTH_REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",  # OAuth 2.1 public client (no secret)
        ),
        storage=FileTokenStorage(),
        redirect_handler=_handle_oauth_redirect,
        callback_handler=_handle_oauth_callback,
    )
    http_client = httpx.AsyncClient(auth=oauth_auth)

    try:
        async with (
            streamable_http_client(url=args.server_url, http_client=http_client) as (
                read,
                write,
                _,
            ),
            ClientSession(read, write) as session,
        ):
            init = await session.initialize()
            telemetry_cap = getattr(init.capabilities, "serverExecutionTelemetry", None)
            print(f"  Server:                       {init.serverInfo.name} v{init.serverInfo.version}")
            print(f"  serverExecutionTelemetry:     {telemetry_cap is not None}")
            if telemetry_cap:
                print(f"  Capability:                   {telemetry_cap}")

            discovered = await session.list_tools()
            tool_names = [t.name for t in discovered.tools]
            print(f"  Tools:                        {tool_names}\n")

            print("Executing workflow...\n")
            result = await execute_workflow(
                session=session,
                mcp_tools=discovered.tools,
                user_query=user_query,
                detailed=args.detailed,
            )

            if result:
                print("\n" + "=" * 60)
                print("Workflow completed successfully!")
                print("=" * 60)
                print(f"\nResult:\n{result}")
            else:
                print("\n" + "=" * 60)
                print("Workflow did not complete")
                print("=" * 60)

            _print_galileo_trace_url()
    finally:
        # BatchSpanProcessor buffers spans; flush + shutdown so the trace
        # leaves the machine before the process exits.
        _tracer_provider.force_flush()
        _tracer_provider.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
