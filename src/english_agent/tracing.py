"""OpenTelemetry tracing setup. One place that knows how spans leave the process.

Tracing is *opt-in*: it is wired up only when an OTLP endpoint is configured
(`OTEL_EXPORTER_OTLP_ENDPOINT`). When it is not - in CI, in tests, or in any
normal run without a backend - `configure_tracing` is a no-op and
`trace.get_tracer(...)` returns OpenTelemetry's built-in no-op tracer, so every
`with tracer.start_as_current_span(...)` in the codebase costs nothing and goes
nowhere. The instrumentation code elsewhere never has to check a flag.

Run a local backend with `docker compose up -d` (Jaeger) and point the endpoint
at `http://localhost:4318`; see the README's "Tracing" section.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from english_agent import __version__

if TYPE_CHECKING:
    from opentelemetry.instrumentation.httpx import RequestInfo
    from opentelemetry.trace import Span

    from english_agent.config import Settings

TRACER_NAME = "english_agent"

# The Telegram Bot API embeds the bot token in the URL path (/bot<token>/method).
# That must never reach a span attribute, so we scrub it from any captured URL.
_TELEGRAM_TOKEN = re.compile(r"/bot[^/]+/")


def get_tracer() -> trace.Tracer:
    """The project tracer. No-op until `configure_tracing` installs a provider."""
    return trace.get_tracer(TRACER_NAME)


def _redact(url: str) -> str:
    return _TELEGRAM_TOKEN.sub("/bot<redacted>/", url)


def _httpx_request_hook(span: Span, request: RequestInfo) -> None:
    """Overwrite the auto-captured URL so a Telegram bot token never lands on a span."""
    if not span.is_recording():
        return
    url = str(request.url)
    if "/bot" not in url:
        return
    safe = _redact(url)
    # Set both spellings: which one the instrumentation used depends on the
    # HTTP semantic-convention opt-in, and setting an unused one is harmless.
    span.set_attribute("url.full", safe)
    span.set_attribute("http.url", safe)


def configure_tracing(settings: Settings) -> TracerProvider | None:
    """Install a tracer provider that exports over OTLP, if an endpoint is set.

    Returns the provider (so the caller can `shutdown()` it to flush on exit) or
    `None` when tracing is disabled. Safe to call once per process.
    """
    endpoint = settings.otel_exporter_otlp_endpoint.strip()
    if not endpoint:
        return None

    resource = Resource.create({SERVICE_NAME: "english-agent", SERVICE_VERSION: __version__})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
    trace.set_tracer_provider(provider)

    HTTPXClientInstrumentor().instrument(request_hook=_httpx_request_hook)
    return provider
