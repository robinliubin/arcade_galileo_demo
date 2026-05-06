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

By default, every ``tools/call`` requests SEP-2448 passback with full detail —
the server returns its phase spans (``auth.validate``, ``gmail.list_messages``,
``gmail.fetch_details``, ``format_response``) plus the ``HTTPXClientInstrumentor``
HTTP child spans under each phase. Pass ``--no-passback`` to disable the opt-in
entirely; the server returns no ``resourceSpans`` and Galileo sees only the
agent-side ``ToolSpan`` for each call (server is a black box, useful for
showing what observability looks like *without* SEP-2448).
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

from dotenv import load_dotenv

MAX_WORKFLOW_ROUNDS = 5
DEFAULT_LLM_MODEL = "gpt-4o"
DEFAULT_LLM_TEMPERATURE = 0.7
DEFAULT_SERVER_URL = "http://127.0.0.1:8000/mcp"
DEFAULT_LOG_STREAM_BASE = "arcade-galileo-demo"

# Used when no positional `query` argument is supplied on the command line.
# The literal string `$ARCADE_USER_ID` is substituted at runtime with the
# value loaded from .env, so the default query references whichever email
# account you've authorized with Arcade. Override per-invocation by passing
# any string as the first positional argument:
#   .venv/bin/python workflow.py "Summarize my last 5 emails"
DEFAULT_QUERY = (
    "Find my 3 most recent emails from alex.salazar@arcade.dev. "
    "Then email a one-paragraph summary of them to me at $ARCADE_USER_ID."
)

PROJECT_ROOT = Path(__file__).resolve().parent
OAUTH_TOKEN_FILE = PROJECT_ROOT / ".oauth_tokens.json"
OAUTH_CLIENT_FILE = PROJECT_ROOT / ".oauth_client.json"
OAUTH_CALLBACK_PORT = 9905
OAUTH_REDIRECT_URI = f"http://127.0.0.1:{OAUTH_CALLBACK_PORT}/callback"

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse CLI args.

    Defined and called at module load time so the per-mode log stream name
    (computed below from ``--no-passback``) can be set in ``os.environ``
    *before* the ``instrumentation`` import runs — that import is
    side-effecting and reads ``GALILEO_LOG_STREAM`` to call
    ``galileo_context.init(...)`` and configure ``GalileoSpanProcessor``.
    """
    parser = argparse.ArgumentParser(
        description="LangChain agent for the Arcade x Galileo demo with SEP-2448 passback",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=DEFAULT_QUERY,
        help=(
            "Natural-language task for the agent. If omitted, uses DEFAULT_QUERY "
            "from the top of this file (a built-in demo query). The literal "
            "`$ARCADE_USER_ID` in any query is substituted with your .env value "
            "at runtime. Example: "
            '.venv/bin/python workflow.py "Summarize my last 5 emails"'
        ),
    )
    parser.add_argument(
        "--no-passback",
        action="store_true",
        help=(
            "Disable SEP-2448 server-execution telemetry passback. "
            "Server appears as a black box in Galileo (agent-side ToolSpan only, "
            "no phase or HTTPX spans). Default: passback enabled with full detail. "
            "Each mode writes to a differently-suffixed Galileo log stream "
            "(``-passback`` vs ``-no-passback``) so the two shapes are easy to "
            "compare side-by-side in the UI."
        ),
    )
    parser.add_argument(
        "--server-url",
        default=DEFAULT_SERVER_URL,
        help=f"Local MCP server URL (default: {DEFAULT_SERVER_URL})",
    )
    return parser.parse_args()


# === Module-load-time side effects ===
# 1. Load .env so any user-set GALILEO_LOG_STREAM is visible below.
# 2. Parse CLI to learn passback mode.
# 3. Suffix the log stream name so passback / no-passback runs land in
#    different Galileo log streams (e.g. ``arcade-galileo-demo-passback`` vs
#    ``arcade-galileo-demo-no-passback``). The suffix is unconditional —
#    if the user customized GALILEO_LOG_STREAM in .env, they still get
#    differentiated streams (e.g. ``my-base-passback`` / ``my-base-no-passback``).
# 4. Then import ``instrumentation`` (side-effecting; reads GALILEO_LOG_STREAM).

load_dotenv(PROJECT_ROOT / ".env")
_cli_args = parse_args()
_passback = not _cli_args.no_passback

_log_stream_base = os.getenv("GALILEO_LOG_STREAM", DEFAULT_LOG_STREAM_BASE)
_mode_suffix = "passback" if _passback else "no-passback"
os.environ["GALILEO_LOG_STREAM"] = f"{_log_stream_base}-{_mode_suffix}"

# Heavy third-party imports below. ``instrumentation`` is the side-effecting
# Galileo OTel boot (calls galileo_context.init, registers the processor,
# installs LangChainInstrumentor). It reads GALILEO_LOG_STREAM from env, so
# the assignment above must happen before this import block.

import httpx  # noqa: E402
from galileo import otel  # noqa: E402
from galileo_core.schemas.logging.span import ToolSpan, WorkflowSpan  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.auth import OAuthClientProvider, TokenStorage  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken  # noqa: E402
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator  # noqa: E402

from instrumentation import (  # noqa: E402
    ingest_passback_to_galileo,
    tracer_provider as _tracer_provider,
)


# ---------------------------------------------------------------------------
# Workaround: suppress RFC 8707 `resource=<url>` on OAuth requests
# ---------------------------------------------------------------------------
#
# The MCP SDK (since ~1.25) sends `resource=<server_url>` on the authorize URL
# whenever the resource server publishes Protected Resource Metadata (RFC 9728).
# Our local server does publish PRM (it's how the MCP SDK discovers Arcade
# Cloud as the auth server in the first place), so the SDK always includes
# `resource=http://127.0.0.1:8000/mcp` in OAuth requests.
#
# Arcade Cloud's authorization server then performs a back-channel HTTP fetch
# of the resource's PRM endpoint to validate it. That request goes from
# `cloud.arcade.dev` to `http://127.0.0.1:8000` — i.e. from Arcade's cloud to
# *your* localhost — which is unreachable. Result on the OAuth callback:
#
#   OAuth error: server_error | description: Could not retrieve protected
#   resource metadata for the gateway. Verify that the gateway is reachable
#   and configured correctly.
#
# Suppressing the resource parameter (returning False from the decision
# helper) skips Arcade's back-channel validation, and the rest of the OAuth
# flow proceeds normally. Trade-off: tokens issued without resource binding
# could in principle be used against a different MCP server — but for a
# local demo where the only MCP server in question is the one on this laptop,
# that's not a meaningful attack surface.
#
# **Remove this patch** if you ever expose `server.py` via a publicly
# reachable URL (e.g. ngrok tunnel) and update CANONICAL_URL accordingly —
# at that point Arcade's back-channel validation will succeed and the
# parameter is the proper RFC 8707 audience binding.
from mcp.client.auth.oauth2 import OAuthContext  # noqa: E402

OAuthContext.should_include_resource_param = (  # type: ignore[method-assign]
    lambda self, protocol_version=None: False
)


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
                # Surface the full RFC 6749 error response (error +
                # error_description + error_uri) so the user can diagnose
                # auth-server-side failures. Without error_description the
                # only signal is the opaque error code (e.g. ``server_error``).
                error = qs.get("error", ["unknown"])[0]
                error_description = qs.get("error_description", [""])[0]
                error_uri = qs.get("error_uri", [""])[0]
                detail_parts = [f"OAuth error: {error}"]
                if error_description:
                    detail_parts.append(f"description: {error_description}")
                if error_uri:
                    detail_parts.append(f"more info: {error_uri}")
                detail = " | ".join(detail_parts)

                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                html = (
                    f"<h2>Authorization failed: {error}</h2>"
                    + (f"<p>{error_description}</p>" if error_description else "")
                    + (f'<p>More info: <a href="{error_uri}">{error_uri}</a></p>' if error_uri else "")
                )
                self.wfile.write(html.encode())
                loop.call_soon_threadsafe(
                    future.set_exception, RuntimeError(detail)
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


async def _call_mcp_tool(
    session: ClientSession,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_call_id: str,
    passback: bool,
    propagator: TraceContextTextMapPropagator,
) -> str:
    """Invoke an MCP tool, optionally requesting server span passback.

    Wraps the call in a Galileo ``ToolSpan`` so the UI renders it with the
    proper Tool icon and drill-down. Injects ``traceparent`` into ``_meta``
    so the server's spans (when passback is enabled) share our trace ID.

    With ``passback=True`` (default for this demo): adds
    ``_meta.otel.traces.{request: True, detailed: True}`` to opt into the
    full server span tree (phase spans + HTTPX child spans). The server's
    spans come back under ``response._meta.otel.traces.resourceSpans`` and
    we forward them to Galileo via ``ingest_passback_to_galileo``.

    With ``passback=False``: omits the ``otel`` field entirely. The server
    returns no ``resourceSpans`` and the agent-side ``ToolSpan`` is the
    only record of the call — the server is a black box. This matches the
    "Act 1" mode of the reference impl, useful for showing what
    observability looks like *without* SEP-2448.
    """
    tool = ToolSpan(
        name=tool_name,
        input=json.dumps(tool_args),
        tool_call_id=tool_call_id,
    )
    with otel.start_galileo_span(tool):
        carrier: dict[str, str] = {}
        propagator.inject(carrier)

        meta: dict[str, Any] = {"traceparent": carrier.get("traceparent", "")}
        if passback:
            meta["otel"] = {"traces": {"request": True, "detailed": True}}

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

        if passback:
            ingest_passback_to_galileo(result.meta)
        else:
            print("  Server-side spans: NONE (passback not requested)")
        return text


# ---------------------------------------------------------------------------
# Multi-round agent loop
# ---------------------------------------------------------------------------


async def execute_workflow(
    session: ClientSession,
    mcp_tools: list[Any],
    user_query: str,
    passback: bool,
) -> str | None:
    """Run the LangChain agent loop, with each tool call going over MCP.

    Wrapped in a Galileo ``WorkflowSpan`` so the whole trajectory anchors
    under one root in the UI. ``passback`` controls whether each tool call
    requests SEP-2448 server-execution telemetry (default) or runs as a
    black-box call (``--no-passback``).
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
                output = await _call_mcp_tool(
                    session=session,
                    tool_name=tc["name"],
                    tool_args=tc["args"],
                    tool_call_id=tc["id"],
                    passback=passback,
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
    # CLI was already parsed at module load time so the per-mode log stream
    # name could be set in os.environ before instrumentation imported.
    args = _cli_args
    passback = _passback

    validate_environment()
    user_id = os.environ["ARCADE_USER_ID"]
    user_query = args.query.replace("$ARCADE_USER_ID", user_id)

    print("=" * 60)
    print("Arcade + Galileo Integration Demo (server-span passback)")
    print("=" * 60)
    print(f"\n  Mode:         {'passback (full server tree)' if passback else 'no-passback (server is a black box)'}")
    print(f"  Log stream:   {os.environ['GALILEO_LOG_STREAM']}")
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
                passback=passback,
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
