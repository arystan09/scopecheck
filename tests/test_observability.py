import json
import logging
import os

import httpx
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from scopecheck import telemetry
from scopecheck.app import create_app
from scopecheck.retention import maintain
from scopecheck.settings import Settings

pytestmark = pytest.mark.unit


def test_span_and_logs_drop_payloads_exceptions_and_ambient_parent(tmp_path, capsys):
    telemetry.configure(tmp_path)
    secret = "PRIVATE-PROMPT-TOKEN-CANARY"
    with pytest.raises(ValueError):
        with telemetry.span(
            "provider.request", prompt=secret, token=secret, mode="fake"
        ):
            raise ValueError(secret)
    data = next(tmp_path.glob("*.jsonl")).read_text()
    logs = capsys.readouterr().err
    assert secret not in data + logs
    assert '"status":"error"' in data
    record = json.loads(logs.splitlines()[-1])
    assert record["event"] == "provider.request"
    assert len(record["trace_id"]) == 32
    formatter = telemetry.JsonFormatter()
    arbitrary = logging.LogRecord("third-party", logging.ERROR, "", 1, secret, (), None)
    assert secret not in formatter.format(arbitrary)
    assert telemetry.safe_attributes(
        {
            "error": secret,
            "route": "/api/v1/runs/" + secret,
            "status": secret,
            "run_id": secret,
        }
    ) == {"error": "other", "route": "unmatched", "status": "error"}


def test_retention_expires_only_unretained_diagnostic_artifacts(tmp_path):
    expired, retained, recent = [tmp_path / (c * 32 + ".jsonl") for c in "abc"]
    for path in (expired, retained, recent):
        path.write_text('{"synthetic":true}\n')
    now = 10_000_000
    for path in (expired, retained):
        os.utime(path, (now - 31 * 86400, now - 31 * 86400))
    os.utime(recent, (now, now))
    maintain(tmp_path, retain=retained.stem)
    other = tmp_path / "evaluation-report.json"
    other.write_text("published artifact")
    symlink = tmp_path / ("d" * 32 + ".jsonl")
    symlink.symlink_to(other)
    assert maintain(tmp_path, now=now) == 1
    assert not expired.exists()
    assert (
        retained.exists()
        and recent.exists()
        and other.exists()
        and symlink.is_symlink()
    )
    with pytest.raises(ValueError):
        maintain(tmp_path, retain="../outside")
    with pytest.raises(ValueError):
        maintain(tmp_path, days=0)


@pytest.mark.asyncio
async def test_metrics_disabled_without_dedicated_key_and_public_closed(monkeypatch):
    monkeypatch.delenv("SCOPECHECK_DATABASE_URL", raising=False)
    monkeypatch.delenv("SCOPECHECK_METRICS_TOKEN", raising=False)
    for public in (False, True):
        monkeypatch.setenv("SCOPECHECK_PUBLIC_DEMO", str(public))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            assert (await client.get("/metrics")).status_code == 404


def test_settings_secrets_and_invalid_retention(monkeypatch):
    secret = "synthetic-metrics-secret" * 2
    settings = Settings(metrics_token=secret)
    assert secret not in repr(settings) + settings.model_dump_json()
    with pytest.raises(ValidationError):
        Settings(trace_retention_days=0)
    with pytest.raises(ValidationError):
        Settings(metrics_token="short")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", secret)
    with pytest.raises(ValueError, match="Ambient"):
        telemetry.configure(None, "local")


def test_otel_interface_parent_linkage_and_no_exception_event():
    exporter = InMemorySpanExporter()
    telemetry.runtime.provider.add_span_processor(SimpleSpanProcessor(exporter))
    with telemetry.span("http.request"):
        parent = telemetry.traceparent()
    with telemetry.span("worker.run", parent=parent):
        with telemetry.span("provider.request", mode="fake") as current:
            current.set_attribute("error", "timeout")
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["worker.run"].parent.span_id == spans["http.request"].context.span_id
    assert (
        spans["provider.request"].parent.span_id == spans["worker.run"].context.span_id
    )
    assert len({span.context.trace_id for span in spans.values()}) == 1
    assert all(not span.events for span in spans.values())


@pytest.mark.asyncio
async def test_api_does_not_adopt_untrusted_trace_context(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPECHECK_TRACE_DIRECTORY", str(tmp_path))
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/health/live?prompt=PRIVATE",
            headers={
                "traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01",
                "baggage": "token=PRIVATE",
            },
        )
        assert response.status_code == 200
    text = "".join(path.read_text() for path in tmp_path.glob("*.jsonl"))
    assert "PRIVATE" not in text
    record = json.loads(text)
    assert record["trace_id"] != "1" * 32
    assert record["parent_span_id"] is None
