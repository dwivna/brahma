# Brahma development

Brahma is an external developer coordinator, not Anvi's mind. Its decisions live in
`docs/decisions.md`, with Brahma-specific rounds starting at 1. Read them before changes.
One worker runs at a time. Assign separate file ownership; do not revert others' changes.
Use existing signed-in Claude/Codex CLIs only; never export their credentials or fall back to paid APIs.
Keep actual usage separate from estimates. Unknown usage/cost is unknown, not zero or savings.
Plans need user approval; worker edits go to an isolated branch. Never automatically merge or release.
Respect each selected repository's instructions, privacy, examiner custody and stop controls.
Run `uv run python -m unittest discover -s tests -v` before claiming implementation passes.
Never commit session databases, raw worker transcripts, authentication material or local project data.
