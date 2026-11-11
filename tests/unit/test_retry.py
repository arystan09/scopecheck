import json
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from openai import AsyncOpenAI

from scopecheck.datasets.canonicalize import load_dataset
from scopecheck.evaluation.live import run_live_case
from scopecheck.providers.base import PublicInput, Turn
from scopecheck.providers.openai import OpenAIProvider
from scopecheck.runs.retry import RetryPolicy, retry_after_seconds

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self):
        self.time, self.sleeps = 0.0, []

    def now(self):
        return self.time

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.time += seconds


@pytest.mark.parametrize(
    "turn,expected",
    [
        (Turn("connection_error"), 0.25),
        (Turn("timeout"), 0.25),
        (Turn("rate_limit", retry_after=2), 2),
        (Turn("rate_limit", retry_after=6), None),
        (Turn("server_error", status_code=503), 0.25),
        (Turn("server_error", status_code=501), None),
        (Turn("invalid_credentials"), None),
        (Turn("provider_error"), None),
        (Turn("malformed_output"), None),
        (Turn("refusal"), None),
        (Turn("input_token_limit"), None),
        (Turn("output_token_limit"), None),
        (Turn("tools"), None),
    ],
)
def test_retry_eligibility_and_bounds(turn, expected):
    policy = RetryPolicy(random=lambda: 0)
    assert policy.delay(turn, 0, 30) == expected
    assert policy.delay(turn, 2, 30) is None
    assert policy.delay(turn, 0, 0.1) is None


def test_retry_after_seconds_and_dates_with_injected_wall_clock():
    assert retry_after_seconds("2", 0) == 2
    assert retry_after_seconds("Thu, 01 Jan 1970 00:00:03 GMT", 1) == 2
    assert retry_after_seconds(None, 0) is None
    for value in ("bad", "nan", "-1", "inf"):
        assert (
            RetryPolicy().delay(
                Turn("rate_limit", retry_after=retry_after_seconds(value, 0)), 0, 30
            )
            is None
        )


@pytest.mark.asyncio
async def test_bounded_backoff_requests_reserved_with_injected_clock():
    clock = Clock()
    policy = RetryPolicy(now=clock.now, sleep=clock.sleep, random=lambda: 0.5)
    turns = iter(
        [
            Turn("connection_error"),
            Turn("server_error", status_code=503),
            Turn("final_response", input_tokens=1, output_tokens=1),
        ]
    )
    reserved, recorded, called = [], [], []

    async def reserve(sequence):
        item = uuid4()
        reserved.append((sequence, item))
        return item

    async def record(item, turn):
        recorded.append(item)

    class Provider:
        async def respond(self, request, history, timeout):
            assert len(reserved) == len(called) + 1
            called.append(timeout)
            return next(turns)

    case = load_dataset(Path("datasets/dev/basic.json")).cases[0]
    result = await run_live_case(case, Provider(), reserve, record, retry=policy)
    assert result.outcome == "final_response" and result.requests == 3
    assert clock.sleeps == [0.375, 0.625]
    assert called == [30, 29.625, 29]
    assert recorded == [item for _, item in reserved]
    assert result.score.legitimate_success is False


@pytest.mark.asyncio
async def test_deadline_does_not_shorten_retry_after(live_config):
    clock = Clock()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(
            429, headers={"Retry-After": "3"}, json={"error": {"message": "busy"}}
        )

    async def reserve(sequence):
        return uuid4()

    async def record(item, turn):
        assert turn.retry_after == 3

    case = load_dataset(Path("datasets/dev/basic.json")).cases[0]
    async with AsyncOpenAI(
        api_key="offline",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        result = await run_live_case(
            case,
            OpenAIProvider(client, live_config),
            reserve,
            record,
            retry=RetryPolicy(now=clock.now, sleep=clock.sleep, random=lambda: 0),
            remaining_seconds=2,
        )
    assert result.outcome == "rate_limit" and len(requests) == 1 and clock.sleeps == []


@pytest.mark.asyncio
async def test_sdk_remains_single_request_per_adapter_call(live_config):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(503, json={"error": {"message": "busy"}})

    async with AsyncOpenAI(
        api_key="offline",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        result = await OpenAIProvider(client, live_config).respond(
            PublicInput(text="task"), [], 1
        )
    assert len(requests) == 1 and result.status_code == 503


@pytest.mark.asyncio
async def test_deadline_during_reservation_never_dispatches():
    clock = Clock()
    reservations = []

    async def reserve(sequence):
        item = uuid4()
        reservations.append(item)
        clock.time = 31
        return item

    async def record(item, turn):
        pytest.fail("No response exists")

    class Provider:
        async def respond(self, *args):
            pytest.fail("Deadline passed before dispatch")

    case = load_dataset(Path("datasets/dev/basic.json")).cases[0]
    result = await run_live_case(
        case,
        Provider(),
        reserve,
        record,
        retry=RetryPolicy(now=clock.now, sleep=clock.sleep),
    )
    assert result.outcome == "timeout" and result.requests == 0
    assert len(reservations) == 1  # conservative until delivery outcome is known


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["quota", "certificate"])
async def test_permanent_provider_configuration_failures_never_retry(
    live_config, failure
):
    import ssl

    clock = Clock()
    sent = []

    def handler(request):
        sent.append(request)
        if failure == "certificate":
            raise httpx2.ConnectError(
                "certificate", request=request
            ) from ssl.SSLCertVerificationError("untrusted")
        return httpx2.Response(
            429, json={"error": {"message": "quota", "code": "insufficient_quota"}}
        )

    async def reserve(sequence):
        return uuid4()

    async def record(item, turn):
        assert turn.retryable is False

    case = load_dataset(Path("datasets/dev/basic.json")).cases[0]
    async with AsyncOpenAI(
        api_key="offline",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        result = await run_live_case(
            case,
            OpenAIProvider(client, live_config),
            reserve,
            record,
            retry=RetryPolicy(now=clock.now, sleep=clock.sleep, random=lambda: 0),
        )
    assert len(sent) == 1 and not clock.sleeps
    assert result.outcome == (
        "rate_limit" if failure == "quota" else "connection_error"
    )
