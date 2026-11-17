"""Bounded retries for cooperating publishers of the local SQLite fake.

Stale snapshots reload immediately; SQLITE_BUSY uses capped full-jitter backoff.
Both share round and elapsed-time limits and accept caller-owned cancellation.
Deadlines/cancellation are cooperative: running I/O is not interrupted.
Verification errors and other storage errors propagate.
There is no real GitHub client, model call, background loop, or network request.
"""

from collections.abc import Callable
from dataclasses import dataclass
import math
from pathlib import Path
import random
import time
from typing import Protocol

from scripts.release_persistence import SQLiteFakeGitHub, StaleState, StorageBusy
from scripts.release_simulation import ReleaseIdentity, Verification, publish_verified


class RetryDeadlineExceeded(TimeoutError):
    """The caller's time budget expired; publication may already be committed."""


class RetryCancelled(RuntimeError):
    """The caller requested cancellation; publication may already be committed."""


class CancellationSignal(Protocol):
    """A threading.Event-compatible signal; keep it set once cancellation is requested."""

    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


def _finite_seconds(value: object, label: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(label + " must be a finite number")
    try:
        seconds = float(value)
    except OverflowError as error:
        raise ValueError(label + " must be a finite number") from error
    if not math.isfinite(seconds):
        raise ValueError(label + " must be a finite number")
    return seconds


@dataclass(frozen=True)
class PublicationOutcome:
    rounds: int
    conflicts: int
    busy_errors: int = 0
    requested_backoff_seconds: float = 0.0
    elapsed_seconds: float = 0.0


def publish_with_retry(
    path: str | Path,
    identity: ReleaseIdentity,
    package: bytes,
    verification: Verification,
    *,
    max_rounds: int = 4,
    client_factory: Callable[[str | Path], SQLiteFakeGitHub] = SQLiteFakeGitHub,
    base_delay_seconds: float = 0.05,
    max_delay_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    uniform: Callable[[float, float], float] | None = None,
    max_elapsed_seconds: float = 30.0,
    monotonic: Callable[[], float] | None = None,
    cancel_event: CancellationSignal | None = None,
) -> PublicationOutcome:
    """Reload and revalidate after a conflict, without replaying a raw write.

    rounds/conflicts describe this caller's workflow, not publish() calls or
    committed publication effects. The database must already exist. Backoff
    waits run after the failed connection has closed, never holding a transaction.
    Requested backoff is not elapsed time. The shared budget includes work and
    waits, measured with a monotonic clock, and is checked between workflow
    phases. It cannot interrupt in-flight I/O or steps inside publish_verified.
    Expiry does not imply rollback: reopen and verify before trying again.
    A new invocation gets a new budget; timestamps are not persisted in SQLite.
    Each retryable BUSY samples uniformly from zero to the current exponential
    ceiling. The ceiling grows independently of the sample. Inject uniform and
    sleep for deterministic tests; normal callers must not share a fixed seed.
    With cancel_event, its interruptible wait replaces sleep; virtual-clock
    tests must inject a signal whose wait advances that same clock. Cancellation
    is checked at the same phase boundaries as the deadline and takes priority
    when both are observed. Nonrecoverable errors retain their original type.
    The caller owns the signal: this function never clears or persists it.
    """
    if type(max_rounds) is not int or max_rounds < 1:
        raise ValueError("max_rounds must be a positive integer")
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        for value in (base_delay_seconds, max_delay_seconds)
    ) or base_delay_seconds > max_delay_seconds:
        raise ValueError("Backoff delays must be finite and positive, with base <= maximum")
    budget = _finite_seconds(max_elapsed_seconds, "max_elapsed_seconds")
    if budget < 0:
        raise ValueError("max_elapsed_seconds must be nonnegative")

    def check_cancelled(*, observed: bool = False) -> None:
        if observed or (cancel_event is not None and cancel_event.is_set()):
            raise RetryCancelled(
                "Publication cancelled; outcome may already be committed; "
                "reopen and verify before retrying with a new signal"
            )

    check_cancelled()
    clock = time.monotonic if monotonic is None else monotonic
    started = _finite_seconds(clock(), "Monotonic clock reading")
    last_reading = started

    def check_elapsed() -> float:
        nonlocal last_reading
        check_cancelled()
        now = _finite_seconds(clock(), "Monotonic clock reading")
        if now < last_reading:
            raise ValueError("Monotonic clock must not move backwards")
        last_reading = now
        elapsed = now - started
        if elapsed >= budget:
            raise RetryDeadlineExceeded(
                "Publication time budget exhausted; outcome may already be committed; "
                "reopen and verify before retrying"
            )
        return elapsed

    conflicts = 0
    busy_errors = 0
    requested_backoff = 0.0
    ceiling = float(base_delay_seconds)
    sample_uniform = random.uniform if uniform is None else uniform
    for round_number in range(1, max_rounds + 1):
        check_elapsed()
        try:
            # An exclusive lock can make opening/loading fail before publishing.
            github = client_factory(path)
            check_elapsed()
            publish_verified(github, identity, package, verification)
            check_elapsed()
            github.assert_current()
            elapsed = check_elapsed()
        except StaleState as error:
            conflicts += 1
            check_elapsed()
            if round_number == max_rounds:
                raise StaleState("Publication retry limit reached; inspect and retry later") from error
            continue
        except StorageBusy as error:
            busy_errors += 1
            check_elapsed()
            if round_number == max_rounds:
                raise StorageBusy("SQLite busy retry limit reached; inspect and retry later") from error
            delay = sample_uniform(0.0, ceiling)
            # A finite interval also rejects NaN/infinity; bool is not a delay.
            if type(delay) not in (int, float) or not 0.0 <= delay <= ceiling:
                raise ValueError(
                    "Jitter sample must be finite and between zero and the current ceiling"
                ) from error
            # Sampling can take time too. Never request more than remains;
            # after sleep, recheck the clock before opening another client.
            delay = min(float(delay), budget - check_elapsed())
            if cancel_event is None:
                sleep(delay)
            else:
                # Preserve a cancellation observed by wait even if another
                # caller incorrectly clears the flag before we inspect it.
                check_cancelled(observed=cancel_event.wait(delay))
            requested_backoff += delay
            check_elapsed()
            ceiling = min(float(max_delay_seconds), ceiling * 2)
            continue
        return PublicationOutcome(
            rounds=round_number, conflicts=conflicts, busy_errors=busy_errors,
            requested_backoff_seconds=requested_backoff,
            elapsed_seconds=elapsed,
        )
    raise AssertionError("Unreachable: every bounded round returns or raises")
