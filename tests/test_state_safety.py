"""Review round 8, findings 14 and 18: the state database is migrated atomically and kept private."""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from brahma.store import SCHEMA_VERSION, Store


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class Migration(unittest.TestCase):
    def test_a_newer_database_is_refused_without_being_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "state"
            home.mkdir(mode=0o700)
            db = sqlite3.connect(home / "state.sqlite3")
            db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, repo TEXT, created TEXT, status TEXT, plan TEXT, worktree TEXT)")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            db.commit()
            before = [row[1] for row in db.execute("PRAGMA table_info(sessions)")]
            tables = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            db.close()
            with self.assertRaises(ValueError):
                Store(home)
            db = sqlite3.connect(home / "state.sqlite3")
            self.assertEqual([row[1] for row in db.execute("PRAGMA table_info(sessions)")], before)
            self.assertEqual(sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")), tables)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 1)
            db.close()

    def test_an_old_database_is_migrated_and_reopened_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "state"
            home.mkdir(mode=0o700)
            db = sqlite3.connect(home / "state.sqlite3")
            db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, repo TEXT, created TEXT, status TEXT, plan TEXT, worktree TEXT)")
            db.execute("INSERT INTO sessions VALUES ('k','/r','t','chat',NULL,NULL)")
            db.commit()
            db.close()
            store = Store(home)
            self.assertEqual(store.session("k")["paused"], 0)
            store.close()
            Store(home).close()


class Permissions(unittest.TestCase):
    def test_an_existing_open_directory_and_database_are_tightened(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "state"
            home.mkdir()
            os.chmod(home, 0o755)
            (home / "state.sqlite3").touch()
            os.chmod(home / "state.sqlite3", 0o644)
            store = Store(home)
            store.create(Path(tmp))
            store.close()
            self.assertEqual(mode(home), 0o700)
            self.assertEqual(mode(home / "state.sqlite3"), 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = home / ("state.sqlite3" + suffix)
                if sidecar.exists():
                    self.assertEqual(mode(sidecar) & 0o077, 0)

    def test_a_new_database_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.umask(0o022)
            try:
                Store(Path(tmp) / "fresh").close()
            finally:
                os.umask(old)
            self.assertEqual(mode(Path(tmp) / "fresh" / "state.sqlite3"), 0o600)

    def test_a_symlinked_state_directory_or_database_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir(mode=0o700)
            link = Path(tmp) / "link"
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                Store(link)
            home = Path(tmp) / "home"
            home.mkdir(mode=0o700)
            (home / "state.sqlite3").symlink_to(Path(tmp) / "elsewhere.sqlite3")
            with self.assertRaises(ValueError):
                Store(home)
            self.assertFalse((Path(tmp) / "elsewhere.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
