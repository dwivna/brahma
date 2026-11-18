from pathlib import Path
import tempfile
import unittest

from brahma.discovery import discover, fingerprint, selected_instructions


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class Discovery(unittest.TestCase):
    def test_user_and_project_catalog_precedence_without_enabling(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, home = Path(tmp) / "project", Path(tmp) / "home"
            write(home / ".claude/skills/example/SKILL.md", "---\nname: example\ndescription: User version\n---\nprivate body")
            project_skill = write(repo / ".claude/skills/example/SKILL.md", "---\nname: example\ndescription: Project version\n---\nproject body")
            write(home / ".codex/agents/reviewer.toml", 'name="reviewer"\ndescription="Review code"\n')
            write(repo / ".agents/skills/checker/SKILL.md", "---\nname: checker\ndescription: Check\n---\n")
            write(repo / ".claude/agents/security.md", "---\nname: security\ndescription: Security review\n---\n")
            result = discover(repo, home)
            entries = {entry.id: entry for entry in result.entries}
            self.assertEqual(len(entries), 4)
            selected = entries["claude/skill/example"]
            self.assertEqual(selected.scope, "project")
            self.assertEqual(selected.description, "Project version")
            self.assertEqual(selected.path, str(project_skill.resolve()))
            self.assertEqual(fingerprint(selected), selected.sha256)
            self.assertTrue(all(entry.state == "discovered" and not entry.enabled for entry in entries.values()))
            self.assertNotIn("private body", repr(result))

    def test_exclusions_symlink_escape_unknown_format_and_credentials_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, home = Path(tmp) / "project", Path(tmp) / "home"
            secret = write(home / ".claude/credentials.json", "not-an-actual-secret")
            write(home / ".codex/auth.json", "not-an-actual-secret")
            write(repo / ".claude/agents/excluded.md", "---\nname: excluded\n---\n")
            write(repo / ".codex/agents/bad.toml", "broken [ TOML")
            bad = repo / ".claude/agents/link.md"
            bad.symlink_to(secret)
            write(repo / ".agents/skills/malformed/SKILL.md", "---\nname: ../../escape\n---\n")
            catalog = discover(repo, home, exclusions=[".claude/agents/excluded.md"])
            self.assertEqual(catalog.entries, [])
            self.assertEqual(len(catalog.errors), 3)
            self.assertNotIn("not-an-actual-secret", repr(catalog))

    def test_inventory_limits_and_symlink_roots_are_reported(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, home = Path(tmp) / "project", Path(tmp) / "home"
            for index in range(3):
                write(repo / f".claude/agents/role{index}.md", "role\n")
            with patch("brahma.discovery.MAX_DEFINITIONS", 2):
                catalog = discover(repo, home)
            self.assertEqual(len(catalog.entries), 2)
            self.assertIn("limit reached", catalog.errors[-1])
            alternate = Path(tmp) / "alternate"
            alternate.mkdir()
            (home / ".claude").parent.mkdir(parents=True, exist_ok=True)
            (home / ".claude").symlink_to(alternate)
            catalog = discover(repo, home)
            self.assertTrue(any("symlink inventory root" in error for error in catalog.errors))

    def test_selected_instruction_loading_checks_exact_ids_hashes_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, home = Path(tmp) / "project", Path(tmp) / "home"
            skill = write(repo / ".agents/skills/example/SKILL.md",
                          "---\nname: example\n---\nFollow referenced.md only when authorized.\n")
            write(repo / ".claude/agents/reviewer.md", "---\nname: reviewer\n---\nCLAUDE_ONLY\n")
            catalog = discover(repo, home)
            ids = ["codex/skill/example", "claude/agent/reviewer"]
            with self.assertRaisesRegex(ValueError, "cannot load"):
                selected_instructions(catalog.entries, ids, "codex")
            loaded = selected_instructions(catalog.entries, [ids[0]], "codex")
            self.assertIn("instruction-loaded", loaded)
            self.assertIn("referenced.md", loaded)
            self.assertNotIn("CLAUDE_ONLY", loaded)
            with self.assertRaises(ValueError):
                selected_instructions(catalog.entries, ["codex/skill/missing"], "codex")
            with self.assertRaises(ValueError):
                selected_instructions(catalog.entries, ids, "codex", max_chars=10)
            skill.write_text("Changed after approval\n")
            with self.assertRaises(ValueError):
                selected_instructions(catalog.entries, ids, "codex")

    def test_parent_redirection_after_discovery_cannot_read_outside_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, home = Path(tmp) / "project", Path(tmp) / "home"
            write(repo / ".agents/skills/example/SKILL.md", "Original\n")
            entry = discover(repo, home).entries[0]
            agents = repo / ".agents"
            agents.rename(repo / ".agents-original")
            outside = Path(tmp) / "outside"
            write(outside / "skills/example/SKILL.md", "Not authorized\n")
            agents.symlink_to(outside)
            with self.assertRaises((OSError, ValueError)):
                fingerprint(entry)


if __name__ == "__main__":
    unittest.main()
