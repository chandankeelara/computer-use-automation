"""Human handoff.

Design: the *same* headed browser stays open. We write an intervention
request payload with all context a human operator would need, print an
approval prompt, and wait on stdin. Reading a line = resume signal.

Mock/production seam:
  - In production this would post the intervention_request.json to an
    operator console (a co-browsing UI), and resume via a webhook or
    signalling channel.
  - Here we resolve via stdin so the demo works without extra services.
"""
from __future__ import annotations

import sys
from typing import Any


def request_handoff(payload: dict[str, Any], *, unattended: bool = False) -> bool:
    """Return True if operator resumed, False if unattended-fail."""
    print("\n=== ESCALATION: HUMAN HANDOFF REQUESTED ===")
    print(f"  reason      : {payload.get('reason')}")
    print(f"  capability  : {payload.get('capability')}")
    print(f"  step        : {payload.get('step_id')}")
    print(f"  current url : {payload.get('current_url')}")
    print(f"  screenshot  : {payload.get('screenshot_path')}")
    print(f"  ax snapshot : {payload.get('ax_snapshot_path')}")
    print(f"  intervention_request.json written")
    print("The headed Chromium window is yours. Interact with it as needed.")
    if unattended:
        print("(unattended mode) — auto-failing escalation.")
        return False
    try:
        sys.stdout.write("Press Enter here to resume replay (Ctrl-C to abort)... ")
        sys.stdout.flush()
        sys.stdin.readline()
        return True
    except KeyboardInterrupt:
        return False
