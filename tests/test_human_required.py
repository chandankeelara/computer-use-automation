"""Tests for the human_required step field (2FA/OTP scenarios).

Contract:
  1. human_required=True on a step ALWAYS escalates, regardless of
     --auto-approve-risky. It's a functional requirement of the flow,
     not a policy gate.
  2. After operator resume, the step's own execute() is NOT called —
     the human is expected to have completed the step in the paused
     tab. The engine records it as `human_completed: true` in evidence
     and moves on.
  3. If the operator declines / times out / unattended, the run ends
     with ESCALATED_UNRESOLVED.
"""
from __future__ import annotations

from cua.artifact.schema import (
    Artifact,
    ArtifactId,
    DetectedBy,
    LocatorStrategy,
    Outcome,
    Provenance,
    Step,
)
from cua.replay.engine import replay_artifact
from cua.safety.policy import Policy


def _minimal_with_human_step(tmp_evidence_root: str, human_required: bool) -> Artifact:
    return Artifact(
        id=ArtifactId(name="cap", version="1.0.0", target="t"),
        title="t", description="d",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        outcomes=[
            Outcome(code="SUCCESS", detected_by=DetectedBy(kind="url_pattern", pattern="^.*$")),
        ],
        steps=[
            Step(id="s1", action="navigate", url="http://127.0.0.1:9/", risk="safe"),
            Step(
                id="s2",
                action="type",
                target=LocatorStrategy(primary={"by": "css", "selector": "#otp"}),
                value="ignored",
                risk="safe",
                human_required=human_required,
            ),
        ],
        approval_state="approved",
        provenance=Provenance(
            goal="g", model="m", recorded_at="2026-01-01T00:00:00Z",
            run_id="r", target_url="http://x",
        ),
    )


def _stub_browser(monkeypatch):
    """Replace WebSurface start/stop/execute so the test never touches
    a real browser. Returns a counter of execute() calls so the test
    can assert whether the human_required step was skipped."""
    from cua.surface import web as web_mod
    from cua.surface.types import ExecutionResult

    executed: list[str] = []

    class _FakePage:
        def __init__(self):
            self.url = "http://127.0.0.1:9/"
        def goto(self, url, wait_until=None): self.url = url
        def evaluate(self, *_a, **_k): return {}
        def title(self): return "fake"
        def locator(self, *_a, **_k):
            class _L:
                def inner_text(_s): return ""
                def count(_s): return 0
                @property
                def first(_s): return _s
            return _L()
        def screenshot(self, path, **_k):
            with open(path, "wb") as f: f.write(b"")
        def expose_binding(self, *a, **k): pass
        def add_init_script(self, *a, **k): pass

    def _fake_start(self):
        self._pw = None; self._browser = None; self._ctx = None
        self._page = _FakePage()
        self._step_counter = 0
    def _fake_stop(self): pass
    def _fake_execute(self, action):
        executed.append(action.kind)
        return ExecutionResult(ok=True, detail="fake", resolved_tier=0)

    monkeypatch.setattr(web_mod.WebSurface, "start", _fake_start)
    monkeypatch.setattr(web_mod.WebSurface, "stop", _fake_stop)
    monkeypatch.setattr(web_mod.WebSurface, "execute", _fake_execute)
    return executed


def test_human_required_step_escalates_even_with_auto_approve_risky(tmp_path, monkeypatch):
    """Prove that --auto-approve-risky does NOT bypass human_required."""
    executed = _stub_browser(monkeypatch)
    art = _minimal_with_human_step(str(tmp_path), human_required=True)
    policy = Policy(
        allowed_domains=["127.0.0.1:9"],
        allowed_actions=["navigate", "type"],
        risky_gate=True,
    )
    result = replay_artifact(
        art, {}, policy=policy, evidence_root=str(tmp_path),
        unattended=True, auto_approve_risky=True, headed=False,
    )
    # unattended + no console → escalation refused → ESCALATED_UNRESOLVED
    assert result.outcome == "ESCALATED_UNRESOLVED"
    assert result.error["expected"] == "human completes the step"
    # The human_required step's execute() must NOT have been called.
    # Only s1 (navigate) executed.
    assert executed == ["navigate"], f"expected navigate only, got {executed}"


def test_human_required_false_runs_normally(tmp_path, monkeypatch):
    """Sanity: without the flag, the step executes as usual."""
    executed = _stub_browser(monkeypatch)
    art = _minimal_with_human_step(str(tmp_path), human_required=False)
    policy = Policy(
        allowed_domains=["127.0.0.1:9"],
        allowed_actions=["navigate", "type"],
        risky_gate=True,
    )
    result = replay_artifact(
        art, {}, policy=policy, evidence_root=str(tmp_path),
        unattended=True, headed=False,
    )
    # Both steps executed; outcome depends on the stubbed page, but the
    # important assertion is that `type` fired.
    assert "type" in executed


def test_human_required_field_is_first_class_on_step_schema():
    """Locks in the schema surface so future refactors can't quietly
    drop the field."""
    fields = set(Step.model_fields.keys())
    assert "human_required" in fields
    # Default must be False so existing artifacts don't suddenly require
    # human intervention on every step.
    assert Step.model_fields["human_required"].default is False
