"""Scoped Ctrl+C cancellation for the foreground, offline FakeGitHub publisher.

The Python SIGINT handler only sets a flag: no Event.set(), locks, I/O, printing,
or exceptions. Waiting polls outside the handler. This is not an I/O interrupt
or a hard cancellation-latency guarantee, and it never contacts real GitHub.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import signal
import threading
import time
from typing import Protocol

from scripts.release_concurrency import CancellationSignal, _finite_seconds


class SignalAPI(Protocol):
    """Registration boundary, replaceable without delivering OS signals in tests."""

    def getsignal(self, signalnum: int) -> object: ...

    def signal(self, signalnum: int, handler: object) -> object: ...


class _SigintCancellation:
    def __init__(
        self, clock: Callable[[], float], sleep: Callable[[float], None], interval: float,
    ) -> None:
        self._requested = False
        self._clock = clock
        self._sleep = sleep
        self._interval = interval

    def is_set(self) -> bool:
        return self._requested

    def wait(self, timeout: float) -> bool:
        duration = _finite_seconds(timeout, "Cancellation wait timeout")
        if duration < 0:
            raise ValueError("Cancellation wait timeout must be nonnegative")
        if self._requested or duration == 0:
            return self._requested
        started = _finite_seconds(self._clock(), "Monotonic clock reading")
        last_reading = started
        while not self._requested:
            now = _finite_seconds(self._clock(), "Monotonic clock reading")
            if now < last_reading:
                raise ValueError("Monotonic clock must not move backwards")
            last_reading = now
            remaining = duration - (now - started)
            if remaining <= 0:
                return self._requested
            self._sleep(min(self._interval, remaining))
        return True


@contextmanager
def ctrl_c_cancellation(
    *,
    signal_api: SignalAPI | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = 0.05,
) -> Iterator[CancellationSignal]:
    """Temporarily translate SIGINT into a sticky cancellation flag.

    Enter/exit on the main thread. Pass the yielded signal to publish_with_retry.
    For virtual-time tests, use the same clock in this adapter and the publisher,
    and inject a sleep that advances it. Repeated Ctrl+C requests never force-exit.
    Nested contexts are LIFO: only the innermost token receives SIGINT. The old
    handler is restored, not called/chained. A signal after the publisher's final
    checkpoint does not retroactively cancel its result. No handler is installed
    on import, and no cancellation flag is stored in SQLite.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Ctrl+C cancellation must be installed on the main thread")
    interval = _finite_seconds(poll_interval_seconds, "Polling interval")
    if interval <= 0:
        raise ValueError("Polling interval must be positive")
    api = signal if signal_api is None else signal_api
    clock = time.monotonic if monotonic is None else monotonic
    token = _SigintCancellation(clock, sleep, interval)
    previous = api.getsignal(signal.SIGINT)
    if previous is None:
        raise RuntimeError("Cannot safely restore a SIGINT handler not installed from Python")

    def handle_sigint(signum: int, frame: object) -> None:
        token._requested = True

    api.signal(signal.SIGINT, handle_sigint)
    try:
        yield token
    finally:
        api.signal(signal.SIGINT, previous)
