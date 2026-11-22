# Questions for Codex as a worker of Brahma (decision Round 21, item 4)

Ask Codex these questions read-only, after its weekly window resets (Wed 25 Nov 2026 21:40 IST). Record its answers
beside Claude's in `docs/decisions.md`. Give Codex the facts below as Brahma recorded them on 21 November 2026, and
ask it to answer from its own experience as a builder and reviewer in Brahma. Where it has no evidence, it should
say so rather than estimate.

## Facts recorded on 21 November 2026

- 20 sessions; none ran fully to ready (8 planning only, 8 never approved, 3 incomplete, 3 blocked, 1 interrupted).
- 6 tasks reached ready; 23 of 24 reviews requested changes.
- Repository memory for learned routing held 0 items; no benchmark existed.
- As builder, Codex runs in a `workspace-write` sandbox with a shell. Claude runs with Read, Glob, Grep, Edit and
  Write only, so it cannot run tests while it builds.
- Medium and low findings also force a repair; a passing review may carry only low or medium notes.

## Questions

1. What do you gain as a worker inside Brahma compared with working directly for the owner?
2. What does Brahma cost you, or prevent you from doing, as builder and as reviewer?
3. Does Brahma reduce or increase the owner's effort today? Use the facts above and your own runs; say what is
   unknown.
4. Why do you think 23 of 24 reviews requested changes? Is that the builders, the reviewers, the review rules, or
   something else? Point to evidence.
5. Which benchmarks would show whether Brahma is worth using? The owner chose "minutes of the owner's time per
   accepted change", measured A/B on a fixed suite (Round 21). What would you add or change?
6. How useful could Brahma become, and for which kinds of work is it the wrong tool?
7. Which features would you plan? The owner has chosen, in order: 1.2.0 benchmark and effort log, 1.3.0 the Claude
   builder runs approved checks, 1.4.0 calibrated review, then security skills (Round 22). Would you change the order
   or the design?
