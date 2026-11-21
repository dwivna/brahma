"""No test may launch a real provider CLI (it would spend the owner's subscription usage and depend on the network).

Imported first by discovery (tests/test_000_provider_guard.py) and by the shared helpers, so a single test file run alone
is covered too. `claude` and `codex` on PATH are replaced with stand-ins that record the call and fail; if any test reached
one, the run ends with a failing exit status and names the call.
"""
from __future__ import annotations

import atexit
import os
import sys
import tempfile
from pathlib import Path

if not os.environ.get("BRAHMA_TEST_PROVIDER_GUARD"):
    _folder = Path(tempfile.mkdtemp(prefix="brahma-provider-guard-"))
    _log = _folder / "calls.log"
    for _name in ("claude", "codex"):
        _stub = _folder / _name
        _stub.write_text(f'#!/bin/sh\necho "{_name} $*" >> "{_log}"\necho "a test launched the real {_name} CLI" >&2\nexit 1\n')
        _stub.chmod(0o755)
    os.environ["PATH"] = str(_folder) + os.pathsep + os.environ.get("PATH", "")
    os.environ["BRAHMA_TEST_PROVIDER_GUARD"] = str(_log)
    # A test that forgets its own state directory must never touch the owner's real one (kill switch, locks, sessions).
    os.environ["BRAHMA_STATE_DIR"] = str(_folder / "state")

    def _report() -> None:
        if _log.exists() and _log.read_text().strip():
            sys.stderr.write("\nPROVIDER GUARD: a test tried to launch a real provider CLI:\n" + _log.read_text()[:2000])
            sys.stderr.flush()
            os._exit(1)
    atexit.register(_report)
