"""The approval prompt against a real (pseudo-)terminal, not a stand-in: a terminal cannot seek."""
from __future__ import annotations

import os
import pty
import threading
import unittest
from unittest.mock import patch

from brahma import cli


class RealTerminalPrompt(unittest.TestCase):
    def test_the_digest_prompt_writes_and_reads_through_a_terminal(self):
        controller, terminal = pty.openpty()
        self.addCleanup(os.close, controller)
        self.addCleanup(os.close, terminal)
        shown = bytearray()

        def person():
            while b"approve it" not in shown:
                shown.extend(os.read(controller, 1024))
            os.write(controller, b"f7e3a4d4\n")
        typing = threading.Thread(target=person, daemon=True)
        typing.start()
        with patch.object(cli, "_TTY", os.ttyname(terminal)):
            typed = cli._read_typed("Type the first 8 characters of the contract digest to approve it (f7e3…): ")
        typing.join(5)
        self.assertEqual(typed, "f7e3a4d4")
        self.assertIn(b"(f7e3", bytes(shown))


if __name__ == "__main__":
    unittest.main()
