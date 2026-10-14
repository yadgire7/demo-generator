"""Tracing with OpenTelemetry.

Code opens spans through `span(...)`. That costs next to nothing until a process calls
`configure`, which turns tracing on only if the standard OpenTelemetry environment asks for it:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318    send traces to a collector (OTLP over HTTP)
    OTEL_TRACES_EXPORTER=console                         or print them, to see what is recorded
    OTEL_SERVICE_NAME=...                                override the name this process reports

What is recorded: one trace per advance of a run, with a span for each stage (and whether its
result came from cache), each model call (model and token counts, never the prompt or reply)
and each speech call. In the service, a trace follows a run across the queue into its workers.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind

from demogen.errors import DemogenError

TRACER = "demogen"
CLIENT, PRODUCER = SpanKind.CLIENT, SpanKind.PRODUCER


def span(name: str, kind: SpanKind = SpanKind.INTERNAL, **attributes: Any):
    """Open a span as the current one. An exception leaving it is recorded and marks it failed.

    Attribute names use dots where keyword arguments use double underscores:
    `span("stage x", demogen__run_id="r1")` sets `demogen.run_id`.
    """
    named = {key.replace("__", "."): value for key, value in attributes.items() if value is not None}
    return trace.get_tracer(TRACER).start_as_current_span(name, kind=kind, attributes=named)


def set_attributes(current: Span, **attributes: Any) -> None:
    for key, value in attributes.items():
        if value is not None:
            current.set_attribute(key.replace("__", "."), value)


def carrier() -> dict[str, str]:
    """The current trace position, in a form that can ride along with a queued job."""
    headers: dict[str, str] = {}
    propagate.inject(headers)
    return headers


@contextmanager
def continued(headers: Mapping[str, str] | None) -> Iterator[None]:
    """Carry on the trace a job was queued from, for the duration of the block."""
    token = otel_context.attach(propagate.extract(headers or {}))
    try:
        yield
    finally:
        otel_context.detach(token)


def build_provider(role: str, env: Mapping[str, str]) -> Any | None:
    """The tracer provider the environment asks for, or None if it asks for no tracing."""
    endpoint = env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    exporter = env.get("OTEL_TRACES_EXPORTER", "otlp" if endpoint else "none").strip().lower()
    if exporter == "none" or env.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return None
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError as error:
        raise DemogenError("tracing was asked for but OpenTelemetry's SDK is not installed. Install it with: pip install 'demogen[otel]'") from error

    # `Resource.create` also reads OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES, which win.
    provider = TracerProvider(resource=Resource.create({"service.name": f"demogen-{role}"}))
    if exporter == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    elif exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # reads its endpoint and headers from the environment
    else:
        raise DemogenError(f"OTEL_TRACES_EXPORTER={exporter!r} is not supported; use otlp, console or none")
    return provider


def configure(role: str) -> bool:
    """Turn tracing on for this process if the environment asks for it. Returns whether it did."""
    provider = build_provider(role, os.environ)
    if provider is None:
        return False
    trace.set_tracer_provider(provider)  # spans still open at exit are flushed by the provider itself
    return True
