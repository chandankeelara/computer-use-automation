"""SessionController — ownership state machine for the live session.

States:
  agent   — automation is driving. Actions permitted.
  paused  — handoff requested; no owner. Actions rejected.
  human   — operator is driving. Automation actions rejected.

Transitions are logged as `control_transitions[]` in the run manifest so
"who was in control when" is auditable after the fact. `GuardedSurface`
uses this object to make ownership *structural*: an act call on a Surface
raises OwnershipError unless owner == agent. That is stronger than "we
document the invariant."

Resume gate:
  Resuming from `human` back to `agent` requires either
    (a) at least one human action recorded on the page since takeover, OR
    (b) an explicit force override.
  Rationale: preventing a "resume with nothing done" collapse of the
  handoff into a no-op is exactly the class of bug the escalation flow is
  supposed to catch.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Optional


State = Literal["agent", "paused", "human"]


class OwnershipError(RuntimeError):
    """Raised when a non-owner attempts an action."""


class ResumeRefused(RuntimeError):
    """Raised when a resume request is not permitted."""


@dataclass
class Transition:
    ts: float
    from_state: State
    to_state: State
    reason: str
    actor: str  # "agent" | "operator" | "system"


@dataclass
class SessionController:
    state: State = "agent"
    transitions: list[Transition] = field(default_factory=list)
    # Count of human-originated actions observed on the page while state==human.
    human_actions_recorded: int = 0

    # --- transitions ----------------------------------------------------
    def request_handoff(self, reason: str) -> None:
        self._transition("paused", reason, actor="system")

    def take_control(self, actor: str = "operator") -> None:
        if self.state not in ("paused", "agent"):
            raise ResumeRefused(f"cannot take control from state={self.state}")
        self._transition("human", "operator took control", actor=actor)
        self.human_actions_recorded = 0

    def note_human_action(self, kind: str = "unknown") -> None:
        if self.state == "human":
            self.human_actions_recorded += 1

    def resume(self, *, force: bool = False, actor: str = "operator") -> None:
        if self.state != "human":
            raise ResumeRefused(f"can only resume from human, was {self.state}")
        if self.human_actions_recorded == 0 and not force:
            raise ResumeRefused(
                "no human action recorded since takeover — refusing to resume. "
                "Pass force=True to override (audited)."
            )
        reason = (
            f"resume after {self.human_actions_recorded} human action(s)"
            if not force
            else f"forced resume ({self.human_actions_recorded} recorded)"
        )
        self._transition("agent", reason, actor=actor)

    def abort(self, reason: str = "aborted") -> None:
        self._transition("paused", reason, actor="system")

    # --- guards ---------------------------------------------------------
    def assert_agent(self) -> None:
        if self.state != "agent":
            raise OwnershipError(
                f"automation attempted action while state={self.state}"
            )

    def is_agent(self) -> bool:
        return self.state == "agent"

    # --- serialization --------------------------------------------------
    def to_evidence(self) -> list[dict]:
        return [
            {
                "ts": t.ts,
                "from": t.from_state,
                "to": t.to_state,
                "reason": t.reason,
                "actor": t.actor,
            }
            for t in self.transitions
        ]

    # --- internals ------------------------------------------------------
    def _transition(self, to: State, reason: str, *, actor: str) -> None:
        self.transitions.append(
            Transition(ts=time.time(), from_state=self.state, to_state=to, reason=reason, actor=actor)
        )
        self.state = to
