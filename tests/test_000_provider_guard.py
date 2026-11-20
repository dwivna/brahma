"""Loaded first by discovery: installs the provider guard for the whole run (see tests/provider_guard.py)."""
import os
import shutil
import unittest

from tests import provider_guard  # noqa: F401


class ProviderGuard(unittest.TestCase):
    def test_provider_commands_resolve_to_the_stand_ins(self):
        for name in ("claude", "codex"):
            self.assertIn("brahma-provider-guard-", shutil.which(name) or "")
        self.assertTrue(os.environ["BRAHMA_TEST_PROVIDER_GUARD"])
