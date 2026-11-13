"""Retention for local diagnostic traces only, never evaluation/budget records."""

import os
import time
from pathlib import Path

from scopecheck.telemetry import HEX_TRACE


def maintain(
    directory: Path,
    days: int = 30,
    *,
    retain: str | None = None,
    now: float | None = None,
) -> int:
    # POSIX locking is required only when local retention is invoked.
    import fcntl

    if days < 1:
        raise ValueError("Retention must be positive")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(
        directory / ".retention.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if retain is not None:
            if (
                not HEX_TRACE.fullmatch(retain)
                or not (directory / f"{retain}.jsonl").is_file()
            ):
                raise ValueError("Existing trace id required")
            marker = os.open(
                directory / f"{retain}.retained",
                os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
            )
            os.close(marker)
            return 0
        cutoff = (time.time() if now is None else now) - days * 86400
        removed = 0
        for path in directory.glob("*.jsonl"):
            if (
                not HEX_TRACE.fullmatch(path.stem)
                or path.is_symlink()
                or path.with_suffix(".retained").exists()
            ):
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        return removed
    finally:
        os.close(lock)
