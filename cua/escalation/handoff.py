"""Human handoff — two modes.

console mode: an OperatorConsole is started before replay and the driver
  publishes payloads to it, waits on a threading.Event set by POST
  /resume. Resume goes through SessionController.resume() which enforces
  the "at least one recorded human action or explicit force" invariant.

stdin mode : offline fallback. Same payload, same invariant enforcement
  (readline() counts as an implicit approve; force=True is not needed
  because the console isn't there to track human actions on the page).

Both write intervention_request.json into the evidence dir and leave the
headed Chromium window open so the operator really can drive it.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Optional

from .controller import ResumeRefused, SessionController
from .console import OperatorConsole

log = logging.getLogger(__name__)


def request_handoff(
    payload: dict[str, Any],
    *,
    controller: SessionController,
    console: Optional[OperatorConsole] = None,
    unattended: bool = False,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Return {resumed: bool, forced: bool, decision: str|None, aborted: bool}."""
    controller.request_handoff(payload.get("reason", "unspecified"))
    _print_banner(payload, console_port=(console.port if console else None))

    if console is not None:
        console.publish(payload)
        # In console mode we simulate "operator takes control" implicitly
        # via the /take-control button. The driver waits for /resume.
        result = console.wait_for_resume(timeout=timeout)
        if result["aborted"] or not result["signaled"]:
            return {"resumed": False, "forced": False, "decision": result.get("decision"), "aborted": True}
        return {
            "resumed": controller.state == "agent",
            "forced": bool(result.get("forced")),
            "decision": result.get("decision"),
            "aborted": False,
        }

    if unattended:
        print("(unattended mode) — auto-failing escalation.")
        return {"resumed": False, "forced": False, "decision": None, "aborted": True}

    try:
        sys.stdout.write("Press Enter to resume, or 'a' + Enter to abort... ")
        sys.stdout.flush()
        line = sys.stdin.readline().strip().lower()
    except KeyboardInterrupt:
        return {"resumed": False, "forced": False, "decision": None, "aborted": True}
    if line.startswith("a"):
        return {"resumed": False, "forced": False, "decision": "decline", "aborted": True}

    # stdin-mode: assume the operator did whatever was needed manually.
    # We simulate the state transitions (take_control + resume) because
    # the console isn't wired.
    try:
        controller.take_control(actor="stdin-operator")
        controller.note_human_action("stdin-implicit")
        controller.resume(force=False, actor="stdin-operator")
    except ResumeRefused as e:
        log.warning("resume refused (stdin path shouldn't hit this): %s", e)
        return {"resumed": False, "forced": False, "decision": "decline", "aborted": True}
    return {"resumed": True, "forced": False, "decision": "approve", "aborted": False}


def _print_banner(payload: dict[str, Any], *, console_port: Optional[int]) -> None:
    print("\n=== ESCALATION: HUMAN HANDOFF REQUESTED ===")
    print(f"  reason      : {payload.get('reason')}")
    print(f"  capability  : {payload.get('capability')}")
    print(f"  step        : {payload.get('step_id')}")
    print(f"  current url : {payload.get('current_url')}")
    print(f"  screenshot  : {payload.get('screenshot_path')}")
    print(f"  ax snapshot : {payload.get('ax_snapshot_path')}")
    if console_port:
        print(f"  operator UI : http://127.0.0.1:{console_port}/")
    print("  intervention_request.json written to evidence dir")
    print("The headed Chromium window is yours. Interact with it as needed.")
