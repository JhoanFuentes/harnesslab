"""Offline native-SIGINT fixture, launched only in a parent's isolated child.

Windows requires a private new console containing exactly this PID. POSIX
requires a new session/process group. No console is attached/detached, no parent
is signalled, and there is no fallback to a simulated callback or CTRL_BREAK.
Importing this module neither installs a handler nor delivers an OS signal.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_concurrency import RetryCancelled, publish_with_retry
from scripts.release_interrupts import ctrl_c_cancellation
from scripts.release_persistence import SQLiteFakeGitHub
from scripts.release_simulation import ReleaseIdentity, REQUIRED_CHECKS, Verification


def inputs() -> tuple[ReleaseIdentity, bytes, Verification]:
    # Authored approval inputs, independent of the persisted fake-server state.
    package = b"HarnessLab native interrupt fixture; not an installable wheel.\n"
    identity = ReleaseIdentity(
        "v0.0.0-ctrl-c-test", "a" * 40, "fixture.whl", sha256(package).hexdigest(),
    )
    return identity, package, Verification(identity, {name: "success" for name in REQUIRED_CHECKS})


def summary(github: SQLiteFakeGitHub, identity: ReleaseIdentity) -> dict:
    release = github.get_release(identity.tag)
    return {
        "release_count": len(github.releases),
        "draft": release.draft if release else None,
        "assets": sorted(release.assets) if release else [],
        "asset_sha256": sha256(github.download_asset(identity.tag, identity.asset_name)).hexdigest()
        if release and identity.asset_name in release.assets else None,
        "upload_attempts": github.upload_attempts,
        "publish_attempts": github.publish_attempts,
        "mutations": [list(event) for event in github.mutations],
    }


class _NativeInterrupt:
    """Native calls stay inside the already protected, disposable child."""

    def __init__(self) -> None:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            self.ctypes = ctypes
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel32.GetConsoleProcessList.argtypes = [
                ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
            ]
            self.kernel32.GetConsoleProcessList.restype = wintypes.DWORD
            self.kernel32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
            self.kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
            self.kernel32.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
            self.kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
            self.pid_buffer = (wintypes.DWORD * 1)()
        elif os.name != "posix":
            raise RuntimeError("This native interrupt fixture requires Windows or POSIX")

    def isolation(self) -> dict:
        pid = os.getpid()
        if os.name == "nt":
            count = self.kernel32.GetConsoleProcessList(self.pid_buffer, 1)
            if count == 0:
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            # A larger required buffer already proves the console is not private.
            if count != 1 or self.pid_buffer[0] != pid:
                raise RuntimeError("Refusing Ctrl+C: console does not contain only the worker PID")
            return {"signal_transport": "windows-console-ctrl-c",
                    "console_pids": [pid], "isolated": True}
        session_id, group_id = os.getsid(0), os.getpgrp()
        if session_id != pid or group_id != pid:
            raise RuntimeError("Refusing SIGINT: worker is not in its own session/process group")
        return {"signal_transport": "posix-sigint", "console_pids": [],
                "session_id": session_id, "process_group_id": group_id, "isolated": True}

    def prepare(self) -> dict:
        info = self.isolation()
        if os.name == "nt" and not self.kernel32.SetConsoleCtrlHandler(None, False):
            # Remove a possibly inherited ignore-Ctrl+C attribute in this child
            # only. The Python handler is already installed by the context.
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return info

    def send(self) -> None:
        self.isolation()  # Recheck immediately before the real OS request.
        if os.name == "nt":
            # CTRL_C_EVENT cannot target a nonzero process-group ID. Group zero
            # is safe here only because our console contains exactly this PID.
            if not self.kernel32.GenerateConsoleCtrlEvent(0, 0):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
        else:
            os.kill(os.getpid(), signal.SIGINT)


def main() -> int:
    phase, state_path = sys.argv[1:]
    if phase not in ("backoff", "after-commit", "recover", "control"):
        raise ValueError("Expected backoff, after-commit, recover, or control")
    identity, package, verification = inputs()
    path = Path(state_path)
    previous = signal.getsignal(signal.SIGINT)
    publish_calls = 0
    ready = None
    before = summary(SQLiteFakeGitHub(path), identity) if phase in ("recover", "control") else None

    def emit(event: str, **fields) -> None:
        print(json.dumps({"event": event, "phase": phase, "pid": os.getpid(), **fields}), flush=True)

    def interrupt_at(site: str) -> None:
        nonlocal ready
        if ready is not None:
            raise AssertionError("The interrupt checkpoint must be reached only once")
        transport = _NativeInterrupt()
        ready = {"site": site, **transport.prepare()}
        emit("READY", **ready)
        # The parent's bounded watchdog owns this pipe wait. EOF/wrong commands
        # fail without delivering a signal; a readiness delay is never guessed.
        if sys.stdin.readline().strip() != "INTERRUPT":
            raise RuntimeError("Expected the parent's INTERRUPT command")
        transport.send()
        deadline = time.monotonic() + 10.0
        while not token.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Native SIGINT was sent but the cancellation flag was not observed")
            # This merely waits for native delivery, not readiness or a benchmark.
            time.sleep(min(0.01, remaining))

    def polling_sleep(seconds: float) -> None:
        if phase == "backoff" and ready is None:
            interrupt_at("backoff")
        else:
            time.sleep(seconds)

    class ObservedGitHub(SQLiteFakeGitHub):
        def publish(self, tag: str) -> None:
            nonlocal publish_calls
            publish_calls += 1
            if before is not None and before["draft"] is False:
                raise AssertionError("Recovery must confirm an existing publication without publishing again")
            super().publish(tag)
            if phase == "after-commit":
                # The fake server's effect is committed before SIGINT is sent.
                interrupt_at("after-commit")

    try:
        with ctrl_c_cancellation(sleep=polling_sleep) as token:
            outcome = publish_with_retry(
                path, identity, package, verification, client_factory=ObservedGitHub,
                cancel_event=token, uniform=lambda low, high: high,
                base_delay_seconds=0.5, max_delay_seconds=0.5,
            )
    except RetryCancelled:
        emit("CANCELLED", handler_restored=signal.getsignal(signal.SIGINT) is previous,
             signal_observed=token.is_set(), publish_calls=publish_calls, **(ready or {}))
        # No state read here: the parent still owns the backoff test's DB lock.
        return 130
    if phase in ("backoff", "after-commit"):
        raise AssertionError("Expected the real interrupt to cancel the publication wrapper")
    emit("CONFIRMED", before=before, after=summary(SQLiteFakeGitHub(path), identity),
         publish_calls=publish_calls, handler_restored=signal.getsignal(signal.SIGINT) is previous,
         signal_observed=token.is_set(), rounds=outcome.rounds)
    return 0


if __name__ == "__main__":
    # Parent tests may create workers; the worker cannot run subprocesses/network.
    with (
        patch("socket.socket.connect", side_effect=AssertionError("Offline worker")),
        patch("socket.socket.connect_ex", side_effect=AssertionError("Offline worker")),
        patch("subprocess.Popen", side_effect=AssertionError("Offline worker")),
    ):
        raise SystemExit(main())
