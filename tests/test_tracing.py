"""Tracing is wired correctly: real spans in the right tree, no-op when disabled.

The provider is global and can be set only once per process, so a session-scoped
fixture installs an in-memory exporter and every test reads/clears it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from english_agent.config import Settings
from english_agent.tracing import _redact, configure_tracing
from test_research_agent import (
    ENGLEX_URL,
    GOOD_NOTES,
    TOPIC,
    all_sites_tools,
    fetch,
    finish,
    make_agent,
    search,
)


@pytest.fixture(scope="session", autouse=True)
def span_exporter() -> InMemorySpanExporter:
    # The global provider can be set only once per process, so claim it here (autouse,
    # session scope) before configure_tracing in any test can win the single slot.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def spans(span_exporter: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    span_exporter.clear()
    yield span_exporter
    span_exporter.clear()


def _by_name(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {span.name: span for span in exporter.get_finished_spans()}


# -- disabled by default -------------------------------------------------------------


def test_configure_tracing_disabled_without_endpoint() -> None:
    assert configure_tracing(Settings(otel_exporter_otlp_endpoint="")) is None


def test_configure_tracing_enabled_with_endpoint() -> None:
    provider = configure_tracing(Settings(otel_exporter_otlp_endpoint="http://localhost:4318"))
    try:
        assert provider is not None
    finally:
        # Undo the global side effects: flush/stop the exporter thread and remove the
        # httpx patch so it cannot leak into other tests (the global tracer provider
        # is already owned by the session fixture, so set_tracer_provider here was a
        # no-op and the in-memory exporter stays in charge).
        if provider is not None:
            provider.shutdown()
        HTTPXClientInstrumentor().uninstrument()


# -- spans from the research loop ----------------------------------------------------


def test_research_run_produces_nested_spans(spans: InMemorySpanExporter) -> None:
    tools = all_sites_tools()
    agent, _ = make_agent(
        [
            search("englex", "present", "perfect"),
            fetch(ENGLEX_URL),
            finish(True, GOOD_NOTES),
        ],
        tools,
    )
    agent.run(TOPIC)

    finished = _by_name(spans)
    assert "research.run" in finished
    assert "tool.search_site" in finished
    assert "tool.fetch_page" in finished
    assert "tool.finish" in finished

    root = finished["research.run"]
    assert root.attributes is not None
    assert root.attributes["research.status"] == "found"
    assert root.attributes["topic.id"] == TOPIC.id

    # The tool spans are children of research.run.
    assert root.context is not None
    for name in ("tool.search_site", "tool.fetch_page", "tool.finish"):
        child = finished[name]
        assert child.parent is not None
        assert child.parent.span_id == root.context.span_id

    search_span = finished["tool.search_site"]
    assert search_span.attributes is not None
    assert search_span.attributes["search.site"] == "englex"
    assert search_span.attributes["search.hits"] == 1


def test_tool_error_is_recorded_on_span(spans: InMemorySpanExporter) -> None:
    tools = all_sites_tools()
    # Searching skysmart first violates site order -> ToolError on tool.search_site.
    agent, _ = make_agent(
        [
            search("skysmart", "present"),
            search("englex", "present", "perfect"),
            fetch(ENGLEX_URL),
            finish(True, GOOD_NOTES),
        ],
        tools,
    )
    agent.run(TOPIC)

    search_spans = [s for s in spans.get_finished_spans() if s.name == "tool.search_site"]
    errored = [s for s in search_spans if s.status.status_code is StatusCode.ERROR]
    assert errored, "the out-of-order search should have produced an ERROR span"
    assert any(event.name == "exception" for event in errored[0].events)


# -- token redaction -----------------------------------------------------------------


def test_redact_strips_telegram_token() -> None:
    url = "https://api.telegram.org/bot8426858305:AAEfn5aq/sendMessage"
    assert _redact(url) == "https://api.telegram.org/bot<redacted>/sendMessage"
    assert "8426858305" not in _redact(url)


def test_redact_leaves_other_urls_untouched() -> None:
    url = "https://englex.ru/present-perfect/"
    assert _redact(url) == url
