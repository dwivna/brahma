"""`brahma run --approve` must use one event loop for discovery, approval and execution (0.12.1).

Found by the first real run: the session's cancel Event bound to the discovery loop and the later execute() raised
"bound to a different event loop", blocking the run before any worker started.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brahma.models import Plan, Result, Task
from brahma.session import Session
from test_core import init_repo
from tests.test_cli import run_cli


class RunLoop(unittest.TestCase):
    def test_discovery_and_execution_share_one_event_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            plan = Plan("G", [Task("T", "do", ["a.txt"], ["ok"])], checks=[["true"]])
            plan_file = Path(tmp) / "plan.json"
            plan_file.write_text(json.dumps(plan.to_dict()))

            async def touch_cancel(session):
                # Awaiting the Event is what binds it to the running loop.
                try:
                    await asyncio.wait_for(session.cancel.wait(), 0.01)
                except asyncio.TimeoutError:
                    pass

            async def discover(self):
                await touch_cancel(self)
                return None

            async def execute(self, **kwargs):
                await touch_cancel(self)
                return Result("brahma", "ready", "done")
            async def approve(self, expected_digest=None):
                await touch_cancel(self)
                return "d" * 64
            with patch.object(Session, "discover_model_profiles", discover), patch.object(Session, "execute", execute), \
                    patch.object(Session, "set_plan", lambda self, plan, **kw: None), patch.object(Session, "approve_async", approve), \
                    patch("brahma.cli.confirm_in_terminal", lambda session: "d" * 64):
                code, out, err = run_cli("run", "--repo", str(root), "--approve", str(plan_file), state=Path(tmp) / "state")
            self.assertEqual(code, 0, out + err)
            self.assertEqual(json.loads(out)["status"], "ready")


if __name__ == "__main__":
    unittest.main()
