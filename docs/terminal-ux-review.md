# Brahma terminal UX review — 18 November 2026

## Plan

Review the approved three-pane terminal: A is shared conversation, A1 is Codex,
A2 is Claude, with one input and one active worker. Check readable plans and
approval, status and usage, stop feedback, keyboard focus, compact terminals,
international text, and untrusted display content. Retain the existing Textual
design; this is a bounded local review, with no provider calls or publication.

Scope: `src/brahma/ui.py`, `src/brahma/cli.py`, `tests/test_ui.py`, and the relevant
session interfaces. Read `AGENTS.md` and `docs/decisions.md`. The decisions require
explicit plan approval, existing subscription CLIs, no paid API fallback, and
sixty minutes / at most three repair cycles per task.

## Research and established pattern

Use the existing Textual layout and native keyboard controls. Local Textual 7.5
source was sufficient to identify rendering behavior; no new design system or
external visual research was needed. In particular, `RichLog` defaults to
`min_width=78`, even with `wrap=True`.

## Review findings

These are findings against the initial reviewed implementation. Source line
numbers identify that implementation and may move during repairs.

| Dimension | Severity | Finding and evidence |
| --- | --- | --- |
| Usability and approval | **Blocker** | `ui.py:65–68`: every log keeps the default 78-column render minimum. At 120×40 each worker has 55 content columns, so ordinary worker messages clip horizontally. At 80×24 shared help and plans also clip. Users cannot reliably read the proposed work or review feedback. |
| Truthful demo and guardrails | **Blocker** | `ui.py:184–190`: `/publish` has no demo guard. With a fixture session marked ready, submitting `/publish` in demo mode called a mocked `gittools.publish`. This contradicts the demo promise at `ui.py:91`. No publication actually occurred during the review. |
| Visual hierarchy and status | **Major** | `ui.py:36,114–117`: the fixed two-row status contains a complete repository path before state and usage. At 80 columns a long fixture path consumed both rows and hid the policy row plus the end of usage. Status must remain readable independently of path length. |
| Conversation identity and usage | **Major** | `ui.py:122–130,169–171,227–231`: starting a new session changes its ID but keeps prior worker messages and usage counters. A fixture usage event of 100 input / 20 output remained after `/new`, as did the old worker marker. Repeated resume also re-adds usage because replay calls `write`. |
| Feedback and stop | **Major** | `ui.py:222–225`: pressing Escape while idle reports that active groups “are terminated.” While active, the event only requests cancellation; completion has not been confirmed. This wording overstates the observed state. |
| Active inspection | **Major** | `ui.py:146–160`: the active-job guard rejects `/status` and clears its input. A mocked active task reproduced the rejection. Users need status/help without stopping work, and rejected task text should remain available to edit. |
| Responsive layout | **Major** below compact baseline | At 60×16, A1 has zero content rows. At 40×12 all three panes have zero content rows and worker/control regions overlap. Provide a minimum-size notice or usable fallback below the supported size. At 80×24 all three panes, input, and buttons remain on screen. |
| Keyboard accessibility | Pass within tested scope | Input receives initial focus. After a demo plan, Tab cycles Approve → Stop → A → A1 → A2 → input and returns; no focus trap was observed. Escape responds from a button. Approval is disabled before a plan and enables after the demo plan. |
| Color accessibility | Pass for explicit foregrounds | Computed sRGB contrast against the declared backgrounds: body 15.04:1, status 7.48:1, shared text 12.18:1, Codex 10.47:1, Claude 10.31:1, header 9.43:1. This does not certify terminal theme overrides, every button state, screen-reader support, or complete WCAG conformance. |
| Internationalization | Limited | Unicode text is used directly, but narrow log clipping affects all scripts. No locale switch or RTL policy exists in scope; bidirectional terminal behavior was not certified. Do not claim multilingual accessibility from this pass. |
| Design-layer code | Minor | Existing tests assert pane count, resize class, button enablement, fresh session ID, and control sanitization. They do not assert visible text, fresh-session logs/usage, demo publication rejection, or status visibility. The rendering blocker passed both existing tests. |

## Suggestions with examples

Required repairs retain the approved product structure.

1. **Render text to the available width, including after resize.** Set the minimum
   on all three logs; retain event content so it can be redrawn if the pane width
   changes. `RichLog` stores rendered strips and does not automatically reflow old
   lines just because `wrap=True`.

   ```python
   yield RichLog(id="manager", min_width=1, wrap=True, markup=False,
                 highlight=False, max_lines=1200)
   # Apply min_width=1 to #codex and #claude as well.
   # On width changes: clear each log, then rewrite its retained events.
   ```

   Acceptance: a long plan and worker message remain readable without horizontal
   scrolling at 120×40 and 80×24, including after narrowing an existing session.

2. **Keep demo publication impossible and its identity persistent.**

   ```python
   elif value == "/publish":
       if self.demo:
           raise ValueError("Demo mode cannot publish a branch or create a PR")
       # Existing ready/session validation and publication follow here.
   ```

   Include `DEMO` in the persistent status. Acceptance: mocked publication is
   never called from a ready demo session; the demo marker survives scrollback.

3. **Put state before the repository and constrain each status row.** Use plain
   `Text` to avoid interpreting repository names as Rich markup.

   ```python
   mode = "DEMO" if self.demo else "LIVE"
   selected = self.session.repo.name if self.session else "no repository"
   first = Text(f"{mode} | {status} | {usage}")
   second = Text(f"{selected} | API fallback OFF | 60m/task | 3 repairs")
   width = max(1, self.size.width - 2)
   first.truncate(width, overflow="ellipsis")
   second.truncate(width, overflow="ellipsis")
   self.query_one("#status", Static).update(Text.assemble(first, "\n", second))
   ```

   Acceptance: a long repository name never hides state or the demo marker.

4. **Reset the visible conversation and usage when selecting a session.** Validate
   the new session before clearing the current display. Reset before replay so
   reopening a checkpoint counts each saved usage event once.

   ```python
   for pane in ("manager", "codex", "claude"):
       self.query_one(f"#{pane}", RichLog).clear()
   self.inputs = self.outputs = 0
   self.have_usage = False
   self.demo_plan = None
   # Announce the selected session, then replay only its events.
   ```

   Acceptance: `/new`, Ctrl+N, `/repo`, and `/resume` display only the selected
   conversation and its correctly recomputed usage.

5. **Distinguish cancellation request from completion.**

   ```python
   active = self.job is not None and not self.job.done()
   if active and self.session:
       self.session.cancel.set()
       self.write(Event(text="Stop requested; waiting for active work to end."))
   else:
       self.write(Event(text="No active work to stop."))
   ```

   Report confirmed cancellation from the worker result; the headless UX pass
   does not prove process-group termination.

6. **Allow inspection during work and preserve rejected input.** Process safe
   inspection commands before assigning a new job; do not replace `self.job`
   with a status task.

   ```python
   if self.job and not self.job.done():
       if value == "/status":
           self.refresh_status()
           self.write(Event(text="Active work; checkpoint status shown above."))
       elif value == "/help":
           self.write(Event(text=HELP))
       else:
           event.input.value = value
           self.write(Event(text="Work is active. Use Escape to stop before another task."))
       return
   ```

   Acceptance: `/status` works during a mocked long-running task; a rejected new
   task remains in the input. Expand the status example with existing checkpoint
   data rather than implying a fresh checkpoint was saved.

7. **Make the compact-terminal boundary explicit.** At sizes below the supported
   layout, show a notice with a persistent Escape/Ctrl+Q route instead of empty
   panes. Example notice content:

   ```python
   warning = Text("Terminal too small. Resize to at least 80×24. Escape stops; Ctrl+Q quits.")
   ```

   Acceptance: the notice replaces the unusable pane arrangement below the
   selected minimum, and resizing back restores the three-pane interface.

## Security and guardrail check

Adversarial display check: Rich markup/link text remained literal, and an OSC 52
clipboard sequence was removed. `Text` plus `markup=False` is the appropriate
log boundary. No HTML, SVG, browser redirects, iframe, or web CSS exists in this
terminal layer, so those web attack categories are inapplicable here.

The demo publication route is a blocking authorization/expectation boundary
failure. Repository status should also use plain `Text`, as shown above. No
actual CLI sign-in, credentials, provider request, remote Git operation, private
user data, or raw worker transcript was accessed for this review.

## Measured checks and limits

- Both existing UI unit tests passed: 2 tests, 1.458 seconds.
- CLI main help and `run --help` exited successfully and clearly identify
  `--approve`, repository selection, and demo mode.
- Headless sizes inspected: 120×40, 80×24, 70×20, 60×16, and 40×12.
- Keyboard focus, demo plan enablement, fresh-session identity, retained logs and
  usage, active `/status`, idle Escape, plain markup, and OSC removal were probed.
- Publication was replaced by a mock, and the demo call-through was observed.
- Early screen-inspection attempts used unavailable internal compositor methods;
  the completed inspection used `render_strips()` and succeeded.

This bounded review is complete. It does not approve runtime quality, provider
integration, real cancellation, the orchestration engine, or release readiness.
The coordinator owns broader checks and any repair verification.

## Verdict

**CHANGES REQUIRED.** The initial bounded UX pass does **not pass**. Fix both
blockers and the major session/status/stop issues, then rerun the relevant
headless reproductions. Either repair the layout below 80×24 or provide the
explicit minimum-size notice. Optional multilingual certification can follow
separately; it is not claimed by this review.
