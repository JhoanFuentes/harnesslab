"""Native interrupt integration in owned children, never the runner's console."""

import importlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.release_persistence import SQLiteFakeGitHub


ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("release_ctrl_c_worker.py")


def isolated_process_options() -> dict:
    if os.name != "nt":
        return {"start_new_session": True}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": startup}


class RealCtrlCTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = importlib.import_module("release_ctrl_c_worker")
        self.identity, self.package, self.verification = fixture.inputs()
        self.summary = fixture.summary
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "native-interrupt.sqlite"
        github = SQLiteFakeGitHub.create(self.path, tags={self.identity.tag: self.identity.source_commit})
        github.create_draft(self.identity)
        github.upload_asset(self.identity.tag, self.identity.asset_name, self.package)
        self.initial = self.summary(SQLiteFakeGitHub(self.path), self.identity)
        self.executable = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("OPENAI_", "GH_", "GITHUB_", "ANTHROPIC_", "PYTHON"))
        }
        # The parent may spawn owned test children, but it never needs networking.
        for target in ("socket.socket.connect", "socket.socket.connect_ex"):
            blocker = patch(target, side_effect=AssertionError("Offline native-signal test"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def run_completed(self, phase: str) -> dict:
        completed = subprocess.run(
            [self.executable, "-I", str(WORKER), phase, str(self.path)],
            cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", timeout=20, check=False,
            **isolated_process_options(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["event"], "CONFIRMED")
        self.assertEqual(result["phase"], phase)
        self.assertNotEqual(result["pid"], os.getpid())
        self.assertTrue(result["handler_restored"])
        return result

    def start_interruptible(self, phase: str):
        process = subprocess.Popen(
            [self.executable, "-I", str(WORKER), phase, str(self.path)],
            cwd=ROOT, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            **isolated_process_options(),
        )
        events = queue.Queue()

        def read_stdout() -> None:
            try:
                for line in process.stdout:
                    events.put(line)
            finally:
                events.put(None)

        reader = threading.Thread(target=read_stdout, daemon=True)

        def cleanup() -> None:
            # This is failure cleanup, never counted as successful cancellation.
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            reader.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass

        self.addCleanup(cleanup)
        reader.start()
        return process, events

    def read_event(self, process, events, expected: str) -> dict:
        try:
            line = events.get(timeout=15)
        except queue.Empty:
            self.fail("Timed out waiting for child event " + expected)
        if line is None:
            process.wait(timeout=2)
            self.fail("Child closed stdout before " + expected + ": " + process.stderr.read())
        event = json.loads(line)
        self.assertEqual(event["event"], expected)
        self.assertEqual(event["pid"], process.pid)
        self.assertNotEqual(event["pid"], os.getpid())
        return event

    def assert_isolated(self, event: dict) -> None:
        self.assertTrue(event["isolated"])
        if os.name == "nt":
            self.assertEqual(event["signal_transport"], "windows-console-ctrl-c")
            self.assertEqual(event["console_pids"], [event["pid"]])
        else:
            self.assertEqual(event["signal_transport"], "posix-sigint")
            self.assertEqual(event["session_id"], event["pid"])
            self.assertEqual(event["process_group_id"], event["pid"])

    def interrupt_and_finish(self, phase: str) -> dict:
        process, events = self.start_interruptible(phase)
        ready = self.read_event(process, events, "READY")
        self.assertEqual(ready["phase"], phase)
        self.assertEqual(ready["site"], phase)
        self.assert_isolated(ready)
        # Request native delivery inside the child's verified private console.
        # Never broadcast CTRL_C_EVENT from this parent's console or use BREAK.
        process.stdin.write("INTERRUPT\n")
        process.stdin.flush()
        cancelled = self.read_event(process, events, "CANCELLED")
        self.assertEqual(process.wait(timeout=10), 130, process.stderr.read())
        self.assertTrue(cancelled["signal_observed"])
        self.assertTrue(cancelled["handler_restored"])
        self.assertEqual(cancelled["phase"], phase)
        self.assert_isolated(cancelled)
        self.assertEqual(cancelled["publish_calls"], 1)
        return cancelled

    def assert_one_publication(self, state: dict) -> None:
        self.assertEqual(state["release_count"], 1)
        self.assertFalse(state["draft"])
        self.assertEqual(state["assets"], [self.identity.asset_name])
        self.assertEqual(state["asset_sha256"], self.identity.sha256)
        self.assertEqual(state["upload_attempts"], 1)
        self.assertEqual(state["publish_attempts"], 1)
        self.assertEqual(state["mutations"], [
            ["create_draft", self.identity.tag],
            ["upload_asset", self.identity.tag],
            ["publish", self.identity.tag],
        ])

    def test_uninterrupted_child_publishes_once(self) -> None:
        result = self.run_completed("control")
        self.assertEqual(result["publish_calls"], 1)
        self.assertEqual(result["before"], self.initial)
        self.assert_one_publication(result["after"])
        self.assertEqual(self.summary(SQLiteFakeGitHub(self.path), self.identity), result["after"])

    def test_native_ctrl_c_during_backoff_leaves_pending_work_for_a_fresh_process(self) -> None:
        holder = sqlite3.connect(self.path, timeout=0.0)
        try:
            holder.execute("BEGIN IMMEDIATE")
            self.interrupt_and_finish("backoff")
        finally:
            holder.rollback()
            holder.close()

        self.assertEqual(self.summary(SQLiteFakeGitHub(self.path), self.identity), self.initial)
        recovered = self.run_completed("recover")
        self.assertEqual(recovered["before"], self.initial)
        self.assertEqual(recovered["publish_calls"], 1)
        self.assert_one_publication(recovered["after"])
        self.assertEqual(self.summary(SQLiteFakeGitHub(self.path), self.identity), recovered["after"])

    def test_native_ctrl_c_after_commit_is_confirmed_without_republishing(self) -> None:
        self.interrupt_and_finish("after-commit")
        saved = self.summary(SQLiteFakeGitHub(self.path), self.identity)
        self.assert_one_publication(saved)
        recovered = self.run_completed("recover")
        self.assertEqual(recovered["publish_calls"], 0)
        self.assertEqual(recovered["before"], saved)
        self.assertEqual(recovered["after"], saved)
        self.assertEqual(self.summary(SQLiteFakeGitHub(self.path), self.identity), saved)


if __name__ == "__main__":
    unittest.main()
