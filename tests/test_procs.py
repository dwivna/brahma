"""Review round 8, finding 17: workers left by a coordinator that died are stopped before new work starts."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from brahma import procs


def spawn_group() -> subprocess.Popen:
    # A leader with a child, like a provider CLI running a tool: the whole group must be stopped.
    return subprocess.Popen([sys.executable, "-c", "import subprocess,time; subprocess.Popen(['sleep','60']); time.sleep(60)"],
                            start_new_session=True)


def dead_pid() -> int:
    gone = subprocess.Popen(["true"])
    gone.wait()
    return gone.pid


class Reap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        procs.holding(self.home)
        self.addCleanup(procs.holding, None)
        self.groups = []

        def cleanup():
            for group in self.groups:
                try:
                    os.killpg(group.pid, 9)
                except ProcessLookupError:
                    pass
                group.wait()
        self.addCleanup(cleanup)

    def launch(self, coordinator: int | None = None) -> tuple[subprocess.Popen, Path]:
        group = spawn_group()
        self.groups.append(group)
        time.sleep(.3)
        record = procs.register(group.pid)
        if coordinator is not None:
            data = json.loads(record.read_text())
            data.update(coordinator=coordinator, coordinator_started=None)
            record.write_text(json.dumps(data))
        return group, record

    def test_an_orphaned_worker_group_is_stopped_with_its_children(self):
        group, record = self.launch(coordinator=dead_pid())
        stopped, survivors = procs.reap(self.home)
        group.wait(timeout=5)
        self.assertEqual((stopped, survivors), ([group.pid], []))
        self.assertFalse(procs._group_alive(group.pid))      # the child `sleep` went with it
        self.assertFalse(record.exists())

    def test_a_live_coordinators_workers_are_left_alone_unless_the_kill_switch_asks(self):
        group, record = self.launch()                         # coordinator is this test process: alive
        self.assertEqual(procs.reap(self.home), ([], []))
        self.assertIsNone(group.poll())
        self.assertEqual(procs.reap(self.home, everything=True)[0], [group.pid])
        group.wait(timeout=5)

    def test_a_reused_pid_is_never_signalled(self):
        group, record = self.launch(coordinator=dead_pid())
        data = json.loads(record.read_text())
        data["started"] = "Thu Jan  1 00:00:00 1970"          # the recorded process is not the one now using this PID
        record.write_text(json.dumps(data))
        self.assertEqual(procs.reap(self.home), ([], []))
        self.assertIsNone(group.poll())
        self.assertFalse(record.exists())

    def test_the_lock_refuses_to_start_while_an_orphan_cannot_be_stopped(self):
        from unittest.mock import patch
        from brahma.session import Session
        from brahma.store import Store
        store = Store(self.home / "state")
        self.addCleanup(store.close)
        fake = type("S", (), {"store": store})()
        with patch.object(procs, "reap", return_value=([], [4242])):
            with self.assertRaisesRegex(ValueError, "could not be stopped"):
                with Session.lock(fake):
                    self.fail("work must not start")


if __name__ == "__main__":
    unittest.main()
