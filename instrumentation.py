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
for manual spans (workflow root, Arcade tool spans).

Pattern follows ``galileo-test/agents/10_otel_openinference.ipynb`` — using the
canonical ``GalileoSpanProcessor`` instead of hand-rolling an ``OTLPSpanExporter``
means non-default clusters (dev / staging / demo-v2 / self-hosted) work via
``GALILEO_CONSOLE_URL`` without endpoint or header munging in this file.
"""

import os
import sys

from dotenv import load_dotenv
from galileo import galileo_context, otel
from openinference.instrumentation.langchain import LangChainInstrumentor
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

# Load environment variables from .env file
load_dotenv()

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

# Auto-instrument LangChain to capture:
# - LLM calls (prompts, responses, tokens)
# - Chain executions
# - Agent tool selections
LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

# Export tracer for manual span creation in application code.
# Use this to create spans for operations not covered by auto-instrumentation.
tracer: Tracer = trace.get_tracer(__name__)
