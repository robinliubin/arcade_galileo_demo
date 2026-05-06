"""OpenTelemetry → Galileo setup via the supported ``galileo.otel`` integration.

Importing this module has side effects:

* loads ``.env``,
* validates Galileo credentials,
* initializes the Galileo project + log stream via ``galileo_context.init(...)``,
* registers a global ``TracerProvider`` with ``galileo.otel.GalileoSpanProcessor``
  (which handles the OTLP exporter, endpoint resolution, and routing headers
  internally — driven by ``GALILEO_API_KEY`` and ``GALILEO_CONSOLE_URL``),
* auto-instruments LangChain via ``openinference.instrumentation.langchain``.

After import, every LangChain LLM call is captured as an OpenInference-shaped
span and shipped to Galileo without further wiring. The exported ``tracer`` is
for manual spans (the workflow root and per-MCP-tool-call spans).

This module also exports ``ingest_passback_to_galileo(meta)`` — used by
``workflow.py`` to forward server-side spans received via SEP-2448
(``_meta.otel.traces.resourceSpans``) to the same Galileo OTLP endpoint that
``GalileoSpanProcessor`` writes to, so the joined client+server trace lands
in a single Galileo project / log stream.
"""

from __future__ import annotations

import base64
import copy
import logging
import os
import sys
from typing import Any

import httpx
from dotenv import load_dotenv
from galileo import galileo_context, otel
from openinference.instrumentation.langchain import LangChainInstrumentor
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

# Load environment variables from .env file
load_dotenv()

_log = logging.getLogger(__name__)

# Galileo configuration from environment
GALILEO_API_KEY: str | None = os.getenv("GALILEO_API_KEY")
GALILEO_PROJECT: str | None = os.getenv("GALILEO_PROJECT")
GALILEO_LOG_STREAM: str = os.getenv("GALILEO_LOG_STREAM", "default")

# Validate required configuration
if not GALILEO_API_KEY or not GALILEO_PROJECT:
    print(
        "Error: GALILEO_API_KEY and GALILEO_PROJECT must be set in .env file",
        file=sys.stderr,
    )
    print(
        "These are required for sending traces to Galileo.",
        file=sys.stderr,
    )
    sys.exit(1)

# Initialize Galileo project + log stream.
# Reads GALILEO_API_KEY (auth) and GALILEO_CONSOLE_URL (cluster routing) from env.
# This is what tells the GalileoSpanProcessor below where traces should land —
# both for default SaaS and non-default clusters (dev, staging, demo-v2, self-hosted).
galileo_context.init(project=GALILEO_PROJECT, log_stream=GALILEO_LOG_STREAM)

# Create OpenTelemetry Resource with service metadata.
# This metadata appears in Galileo to identify the source of traces.
resource: Resource = Resource.create({
    "service.name": "arcade-galileo-demo",
    "service.version": "1.0.0",
})

# Initialize tracer provider with service metadata
tracer_provider: TracerProvider = TracerProvider(resource=resource)

# Configure GalileoSpanProcessor — the supported Galileo OTel integration.
# It handles OTLP exporter setup, endpoint resolution from GALILEO_CONSOLE_URL,
# routing headers, and project/log_stream metadata internally.
span_processor: otel.GalileoSpanProcessor = otel.GalileoSpanProcessor(
    project=GALILEO_PROJECT,
    logstream=GALILEO_LOG_STREAM,
)
otel.add_galileo_span_processor(tracer_provider, span_processor)

# Set as global tracer provider for all OpenTelemetry instrumentation
trace.set_tracer_provider(tracer_provider)

# Auto-instrument LangChain so every ChatOpenAI invocation produces an
# OpenInference-shaped span (llm.input_messages, llm.output_messages,
# llm.token_count.*) that Galileo renders natively.
LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

# Export tracer for manual span creation in application code.
# Use this to create spans for operations not covered by auto-instrumentation
# (the workflow root and per-MCP-tool-call client spans).
tracer: Tracer = trace.get_tracer(__name__)


# ---------------------------------------------------------------------------
# SEP-2448 server-span passback → Galileo
# ---------------------------------------------------------------------------
#
# The local MCP server (``server.py``) returns its internal phase spans
# inline on every ``tools/call`` response, under
# ``response._meta.otel.traces.resourceSpans`` (OTLP JSON shape).
#
# To stitch those into the same Galileo trace as the agent's LLM + tool
# spans, we POST the OTLP protobuf form directly to Galileo's OTLP endpoint
# with the same routing headers ``GalileoSpanProcessor`` uses on the wire.
# Mirrors ``ingest_spans_protobuf`` in ``arcade-mcp/examples/mcp_servers/
# telemetry_passback/src/telemetry_passback/agent.py``.


def _galileo_otlp_endpoint() -> str:
    """Derive the OTLP traces endpoint from ``GALILEO_CONSOLE_URL``.

    The ``GalileoSpanProcessor`` derives this same URL internally from the
    resolved console URL — we re-derive it here so the manually-POSTed
    passback spans land at the same destination as the spans this process
    emits via the processor.
    """
    override = os.getenv("GALILEO_OTLP_ENDPOINT")
    if override:
        return override
    console = os.getenv("GALILEO_CONSOLE_URL", "https://app.galileo.ai/")
    if not console.endswith("/"):
        console += "/"
    return f"{console}api/galileo/otel/traces"


_ID_FIELDS = ("traceId", "spanId", "parentSpanId")


def _hex_ids_to_base64(resource_spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert hex trace/span IDs to base64 for protobuf ``ParseDict``.

    OTLP JSON uses hex strings; the protobuf wire format uses raw bytes,
    which ``ParseDict`` decodes from base64. The MCP passback travels as
    JSON, so we re-encode here just before serializing to protobuf.
    """
    converted = copy.deepcopy(resource_spans)
    for rs in converted:
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                for fld in _ID_FIELDS:
                    if span.get(fld):
                        span[fld] = base64.b64encode(bytes.fromhex(span[fld])).decode()
    return converted


def _count_spans(resource_spans: list[dict[str, Any]]) -> int:
    return sum(
        len(ss.get("spans", []))
        for rs in resource_spans
        for ss in rs.get("scopeSpans", [])
    )


def ingest_passback_to_galileo(meta: Any) -> None:
    """Forward server spans received via SEP-2448 ``_meta.otel`` to Galileo.

    Reads ``meta.otel.traces.resourceSpans`` (OTLP JSON), converts IDs,
    serializes to OTLP protobuf, and POSTs to the OTLP endpoint
    ``GalileoSpanProcessor`` is using — same ``Galileo-API-Key`` /
    ``project`` / ``logstream`` headers, same cluster.

    No-ops silently if the response carries no passback (server doesn't
    advertise the capability, agent didn't request it, or there were no
    spans to return).
    """
    if not meta:
        return

    otel_data = meta.get("otel") if isinstance(meta, dict) else getattr(meta, "otel", None)
    if not otel_data:
        return

    traces = otel_data.get("traces", {}) if isinstance(otel_data, dict) else {}
    resource_spans = traces.get("resourceSpans")
    if not resource_spans:
        return

    span_count = _count_spans(resource_spans)
    truncated = bool(traces.get("truncated", False))
    dropped = int(traces.get("droppedSpanCount", 0))

    print(f"  Server-side spans: {span_count} received and forwarded to Galileo")
    if truncated:
        # Unreachable from this demo (we always send detailed=True), but kept
        # for any future client that opts into phase-only passback.
        print(f"  ({dropped} additional spans were filtered server-side)")

    try:
        from google.protobuf.json_format import ParseDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
    except ImportError:
        _log.warning(
            "Skipping Galileo passback export — install protobuf + opentelemetry-proto",
        )
        return

    body = ParseDict(
        {"resourceSpans": _hex_ids_to_base64(resource_spans)},
        ExportTraceServiceRequest(),
    ).SerializeToString()

    endpoint = _galileo_otlp_endpoint()
    headers = {
        "Content-Type": "application/x-protobuf",
        "Galileo-API-Key": GALILEO_API_KEY or "",
        "project": GALILEO_PROJECT or "",
        "logstream": GALILEO_LOG_STREAM,
    }
    try:
        resp = httpx.post(endpoint, content=body, headers=headers, timeout=10.0)
        if resp.status_code >= 400:
            _log.warning(
                "Galileo passback ingest returned HTTP %d: %s",
                resp.status_code,
                resp.text[:200],
            )
    except httpx.ConnectError:
        _log.warning("Could not connect to Galileo OTLP at %s", endpoint)
    except httpx.HTTPError as exc:
        _log.warning("Galileo passback ingest failed: %s", exc)
