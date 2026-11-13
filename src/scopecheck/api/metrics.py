"""Private process-local HTTP metrics plus database-wide durable worker counts."""

import secrets
from collections.abc import Iterable

from fastapi import APIRouter, Depends, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from sqlalchemy import text

from scopecheck import telemetry
from scopecheck.api.errors import ApiError
from scopecheck.api.routes import Session

router = APIRouter()


class Snapshot:
    def __init__(self, families: list[Metric]) -> None:
        self.families = families

    def collect(self) -> Iterable[Metric]:
        return self.families


async def private_metrics(request: Request) -> None:
    settings = request.app.state.settings
    credential = settings.metrics_token
    if settings.public_demo or credential is None:
        raise ApiError(404, "not_found", "Resource not found")
    if not secrets.compare_digest(
        request.headers.get("authorization", "").encode(),
        ("Bearer " + credential.get_secret_value()).encode(),
    ):
        raise ApiError(401, "unauthorized", "Valid bearer token required")


@router.get(
    "/metrics", include_in_schema=False, dependencies=[Depends(private_metrics)]
)
async def metrics(request: Request, session: Session) -> Response:
    families: list[Metric] = []
    queue = GaugeMetricFamily(
        "scopecheck_queue_depth", "Current durable queue depth", labels=["status"]
    )
    for status, count in await session.execute(
        text(
            "SELECT status, count(*) FROM run_jobs WHERE status IN "
            "('queued','running') GROUP BY status"
        )
    ):
        queue.add_metric([status], count)
    families.append(queue)
    cases = CounterMetricFamily(
        "scopecheck_cases", "Persisted final case results", labels=["status"]
    )
    for status, count in await session.execute(
        text("SELECT status, count(*) FROM case_results GROUP BY status")
    ):
        cases.add_metric(
            [status if status in {"completed", "limited", "failed"} else "failed"],
            count,
        )
    families.append(cases)
    failures = CounterMetricFamily(
        "scopecheck_provider_failures",
        "Persisted provider failure outcomes (including fake timeouts)",
        labels=["error"],
    )
    counts: dict[str, int] = {}
    for error, count in await session.execute(
        text(
            "SELECT outcome, count(*) FROM call_settlements WHERE "
            "outcome NOT IN ('tools','final_response','refusal') GROUP "
            "BY outcome UNION ALL SELECT error_code, count(*) FROM "
            "case_results WHERE (provenance IS NULL OR provenance = "
            "'null'::jsonb) AND error_code IS "
            "NOT NULL GROUP BY error_code"
        )
    ):
        label = telemetry.error_code(error)
        counts[label] = counts.get(label, 0) + count
    for label, count in counts.items():
        failures.add_metric([label], count)
    families.append(failures)
    takeovers = await session.scalar(
        text("SELECT coalesce(sum(greatest(lease_generation - 1, 0)),0) FROM run_jobs")
    )
    families.append(
        CounterMetricFamily(
            "scopecheck_lease_takeovers",
            "Persisted lease generations after first claim",
            value=int(takeovers or 0),
        )
    )
    reservations = GaugeMetricFamily(
        "scopecheck_reservations", "Durable call reservation states", labels=["status"]
    )
    for status, count in await session.execute(
        text(
            "SELECT CASE WHEN s.id IS NULL OR s.amount_microusd IS NULL"
            " THEN 'outstanding' ELSE 'settled' END, count(*) FROM "
            "call_reservations r LEFT JOIN call_settlements s ON "
            "s.reservation_id=r.id GROUP BY 1"
        )
    ):
        reservations.add_metric([status], count)
    families.append(reservations)
    duration = await session.scalar(
        text(
            "SELECT coalesce(sum(extract(epoch FROM finished_at - "
            "started_at)),0) FROM case_attempts WHERE finished_at IS "
            "NOT NULL"
        )
    )
    families.append(
        CounterMetricFamily(
            "scopecheck_case_seconds",
            "Sum of finished current-attempt durations",
            value=float(duration or 0),
        )
    )
    registry = CollectorRegistry()
    registry.register(telemetry.runtime.registry)
    registry.register(Snapshot(families))
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
