"""One bounded retry policy; time, sleeping, and jitter are injectable."""

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from scopecheck.providers.base import Turn


def retry_after_seconds(value: str | None, now: float) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return math.inf
            seconds = max(0, date.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return math.inf
    return seconds if math.isfinite(seconds) and seconds >= 0 else math.inf


@dataclass(frozen=True)
class RetryPolicy:
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    random: Callable[[], float] = random.random

    def delay(self, turn: Turn, retries: int, remaining: float) -> float | None:
        if retries >= 2 or not turn.retryable:
            return None
        eligible = turn.outcome in {"connection_error", "timeout", "rate_limit"} or (
            turn.outcome == "server_error" and turn.status_code in {500, 502, 503, 504}
        )
        if not eligible:
            return None
        delay = 0.25 * 2**retries + 0.25 * self.random()
        if turn.retry_after is not None:
            # Never shorten a provider's requested wait to fit our bound.
            if not math.isfinite(turn.retry_after) or turn.retry_after > 5:
                return None
            delay = max(delay, turn.retry_after)
        return delay if 0 <= delay <= 5 and delay < remaining else None
