"""Allowlisted telemetry without automatic payload or exception capture."""

import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import CollectorRegistry, Histogram

from scopecheck.version import PACKAGE_VERSION

ERRORS = frozenset(
    {
        "none",
        "timeout",
        "rate_limit",
        "server_error",
        "invalid_credentials",
        "connection_error",
        "provider_error",
        "malformed_output",
        "input_token_limit",
        "output_token_limit",
        "budget_exhausted",
        "database_unavailable",
        "execution_failed",
        "other",
    }
)
STATUSES = frozenset(
    {
        "ok",
        "error",
        "completed",
        "limited",
        "failed",
        "tools",
        "final_response",
        "refusal",
    }
)
ROUTES = frozenset(
    {
        "/health/live",
        "/health/ready",
        "/health/version",
        "/ui",
        "/",
        "/metrics",
        "/demo/runs",
        "/api/v1/datasets",
        "/api/v1/datasets/{dataset_id}",
        "/api/v1/configs",
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
        "/api/v1/runs/{run_id}/cases",
        "/api/v1/runs/{run_id}/report",
        "/api/v1/comparisons",
    }
)
NAMES = frozenset(
    {
        "http.request",
        "worker.run",
        "case.execute",
        "provider.request",
        "budget.reserve",
        "telemetry.retention",
    }
)
HEX_TRACE = re.compile(r"^[0-9a-f]{32}$")
PROPAGATOR = TraceContextTextMapPropagator()


def error_code(value: str | None) -> str:
    return value if value in ERRORS else "none" if value is None else "other"


def safe_attributes(attributes: dict[str, Any]) -> dict[str, str | int]:
    result: dict[str, str | int] = {}
    for key, value in attributes.items():
        if key == "route":
            result[key] = value if value in ROUTES else "unmatched"
        elif key == "error":
            result[key] = error_code(value)
        elif key == "status":
            result[key] = value if value in STATUSES else "error"
        elif key == "mode":
            result[key] = value if value in {"fake", "live"} else "fake"
        elif (
            key in {"trace_id", "span_id"}
            and isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{16}|[0-9a-f]{32}", value)
        ):
            result[key] = value
        elif key in {"run_id", "request_id"} and isinstance(value, str):
            if re.fullmatch(r"[0-9a-f-]{36}", value):
                result[key] = value
        elif key == "http_status" and type(value) is int and 100 <= value <= 599:
            result[key] = value
    return result


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Never interpolate log messages, exception text, SQL, URLs or SDK records.
        fields = safe_attributes(getattr(record, "telemetry", {}))
        return json.dumps(
            {
                "timestamp": record.created,
                "level": record.levelname,
                "event": record.msg
                if isinstance(record.msg, str) and record.msg in NAMES
                else "runtime",
                **fields,
            }
        )


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.getLogger().handlers = [handler]
    for name in ("scopecheck", "uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").disabled = True


class FileExporter(SpanExporter):
    """Private per-trace JSONL, append writes safe across API/worker processes."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            for span in spans:
                if span.context is None or span.name not in NAMES:
                    continue
                trace_id = f"{span.context.trace_id:032x}"
                record = {
                    "name": span.name,
                    "trace_id": trace_id,
                    "span_id": f"{span.context.span_id:016x}",
                    "parent_span_id": f"{span.parent.span_id:016x}"
                    if span.parent
                    else None,
                    "start_time": span.start_time,
                    "end_time": span.end_time,
                    "attributes": safe_attributes(dict(span.attributes or {})),
                }
                data = (json.dumps(record, separators=(",", ":")) + "\n").encode()
                fd = os.open(
                    self.directory / f"{trace_id}.jsonl",
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
        except OSError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


class Telemetry:
    def __init__(self) -> None:
        self.provider = TracerProvider(
            resource=Resource(
                {"service.name": "scopecheck", "service.version": PACKAGE_VERSION}
            )
        )
        self.tracer = self.provider.get_tracer("scopecheck", PACKAGE_VERSION)
        self.registry = CollectorRegistry()
        self.http_duration = Histogram(
            "scopecheck_http_seconds",
            "API wall-clock duration",
            ["route", "status"],
            registry=self.registry,
        )


runtime = Telemetry()
configuration: tuple[Path | None, str] | None = None


def configure(directory: Path | None, collector: str = "disabled") -> None:
    global runtime, configuration
    if collector != "disabled" and any(key.startswith("OTEL_") for key in os.environ):
        raise ValueError(
            "Ambient OTEL configuration is unsupported; use local settings"
        )
    configure_logging()
    if configuration == (directory, collector):
        return
    runtime.provider.shutdown()
    runtime = Telemetry()
    configuration = (directory, collector)
    if directory is not None:
        runtime.provider.add_span_processor(
            SimpleSpanProcessor(FileExporter(directory))
        )
    if collector != "disabled":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from requests import Session

        # Fixed local targets only; no ambient auth headers, proxy or arbitrary URL.
        session = Session()
        session.trust_env = False
        host = "127.0.0.1" if collector == "local" else "collector"
        exporter = OTLPSpanExporter(
            endpoint=f"http://{host}:4318/v1/traces",
            headers={},
            session=session,
            timeout=2,
        )
        runtime.provider.add_span_processor(BatchSpanProcessor(exporter))


@contextmanager
def span(
    name: str, *, parent: str | None = None, **attributes: Any
) -> Iterator[trace.Span]:
    if name not in NAMES:
        raise ValueError("Unknown telemetry operation")
    context: Context | None = None
    if parent is not None:
        context = PROPAGATOR.extract({"traceparent": parent})
    with runtime.tracer.start_as_current_span(
        name,
        context=context,
        attributes=safe_attributes(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException:
            current.set_attribute("status", "error")
            raise
        finally:
            fields = safe_attributes(dict(getattr(current, "attributes", {}) or {}))
            fields["trace_id"] = f"{current.get_span_context().trace_id:032x}"
            fields["span_id"] = f"{current.get_span_context().span_id:016x}"
            logging.getLogger("scopecheck").info(name, extra={"telemetry": fields})


def traceparent() -> str | None:
    carrier: dict[str, str] = {}
    PROPAGATOR.inject(carrier)
    return carrier.get("traceparent")


def observe_http(route: str, status: int, started: float) -> None:
    route = route if route in ROUTES else "unmatched"
    category = f"{status // 100}xx" if 100 <= status <= 599 else "other"
    runtime.http_duration.labels(route, category).observe(time.perf_counter() - started)
