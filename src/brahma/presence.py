"""Proof that a person, not a program, is taking a human-only action (Round 9 item 3, Round 14; review round 8, finding 1).

Approving a contract, deciding a disputed review, publishing and lifting the kill switch are Abhinav's. An AI caller runs as
the same OS user, so a flag, a file or a terminal check is no barrier. These actions therefore require macOS device-owner
authentication (Touch ID, or the login password) through LocalAuthentication, run by the system's own `/usr/bin/osascript`.
Nothing is compiled or stored. There is no environment variable, flag or configuration that skips it.

Limits, stated plainly: this stops an agent from approving by running a command; it does not defend against a process that
can already rewrite Brahma's own source code or drive the screen with accessibility permission. A caller can still make
the system dialog appear, so the dialog names the contract digest and the CLI first shows the whole contract in the
person's terminal and asks for the digest prefix (cli.confirm_in_terminal). Other systems have no device-owner prompt
here, so human-only actions are refused there.
"""
from __future__ import annotations

import subprocess
import sys

OSASCRIPT = "/usr/bin/osascript"
TIMEOUT_SECONDS = 120

# LAPolicyDeviceOwnerAuthentication (2): Touch ID, Apple Watch or the account password. The reply arrives on another queue;
# the run loop is spun until it does or the deadline passes.
_SCRIPT = r"""
ObjC.import('LocalAuthentication');
ObjC.import('Foundation');
function run(argv) {
  var context = $.LAContext.alloc.init;
  var state = {done: false, ok: false, message: ''};
  context.evaluatePolicyLocalizedReasonReply(2, argv[0], function (success, error) {
    state.ok = success;
    if (!success && error && !error.isNil()) { state.message = ObjC.unwrap(error.localizedDescription); }
    state.done = true;
  });
  var limit = $.NSDate.dateWithTimeIntervalSinceNow(Number(argv[1]));
  while (!state.done && $.NSDate.date.compare(limit) < 0) {
    $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.1));
  }
  if (!state.done) { return 'timeout'; }
  return state.ok ? 'approved' : 'denied: ' + state.message;
}
"""


class NotConfirmed(ValueError):
    """The person did not confirm (cancelled, failed, timed out) or confirmation is unavailable here."""


def _authenticate(reason: str) -> tuple[bool, str]:
    """Show the system prompt and wait for the person. (True, "") when they confirmed."""
    if sys.platform != "darwin":
        return False, "human-only actions need macOS device-owner authentication, which this system does not provide"
    try:
        done = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", _SCRIPT, reason, str(TIMEOUT_SECONDS)],
                              capture_output=True, text=True, timeout=TIMEOUT_SECONDS + 15)
    except (OSError, subprocess.SubprocessError) as error:
        return False, "the authentication prompt could not be shown: " + str(error)
    answer = done.stdout.strip()
    if done.returncode == 0 and answer == "approved":
        return True, ""
    return False, (answer or done.stderr.strip() or "no answer")[:300]


def require(action: str) -> None:
    """Raise NotConfirmed unless the device owner confirms `action` now. Called immediately before the action happens."""
    reason = "Brahma: " + " ".join(action.split())[:200]
    ok, why = _authenticate(reason)
    if not ok:
        raise NotConfirmed(f"Not confirmed by the device owner ({why}); nothing was done. "
                           "Only a person at this Mac can " + action + ".")
