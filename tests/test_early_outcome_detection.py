"""Regression test for the "flow terminated early" bug.

Bug (found by running `cua eval`): on ACME, member_id 'A-XXXX' returns
the Member-Not-Found page after step s3 (Look up). Step s4 (Open
Sub-Account) is declared risky. With no auto_approve_risky, the old
engine hit the risky-step policy gate on s4 BEFORE checking whether the
flow had already landed on a declared business outcome — and escalated
to a human instead of returning MEMBER_NOT_FOUND.

The fix: at the top of each step's loop iteration, check whether the
current page matches any declared non-SUCCESS outcome; if so, return the
typed outcome and stop. This makes flow termination first-class.

The test constructs an artifact whose second step is `risky` but points
at a page whose outcome is already MEMBER_NOT_FOUND, and asserts the
engine returns the typed outcome without escalating."""
from __future__ import annotations

from unittest.mock import patch

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


def test_early_outcome_short_circuits_risky_gate(tmp_path, monkeypatch):
    """We stub `find_matching_outcome` to say we're already on a
    MEMBER_NOT_FOUND page after step 1. The engine should NOT escalate on
    step 2's risky-policy check — it should return MEMBER_NOT_FOUND."""
    art = Artifact(
        id=ArtifactId(name="cap", version="1.0.0", target="t"),
        title="t",
        description="d",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        outcomes=[
            Outcome(code="SUCCESS", detected_by=DetectedBy(kind="url_pattern", pattern="^.*success.*$")),
            Outcome(code="MEMBER_NOT_FOUND", detected_by=DetectedBy(kind="text_present", text="not found")),
        ],
        steps=[
            Step(id="s1", action="navigate", url="http://127.0.0.1:9/", risk="safe"),
            Step(
                id="s2",
                action="click",
                target=LocatorStrategy(primary={"by": "role_name", "role": "button", "name": "Never"}),
                risk="risky",
            ),
        ],
        approval_state="approved",
        provenance=Provenance(
            goal="g", model="m", recorded_at="2026-01-01T00:00:00Z",
            run_id="r", target_url="http://x",
        ),
    )
    policy = Policy(
        allowed_domains=["127.0.0.1:9"],
        allowed_actions=["navigate", "click"],
        risky_gate=True,
    )

    # Stub WebSurface.start so we don't actually launch a browser.
    from cua.surface import web as web_mod

    started_page_state = {"n": 0}
    class _FakePage:
        def __init__(self):
            self.url = "http://127.0.0.1:9/not-found"
        def goto(self, url, wait_until=None):
            self.url = url
        def evaluate(self, *args, **kwargs):
            return {}
        def locator(self, *_a, **_k):
            class _L:
                def inner_text(_self): return "member not found"
                def count(_self): return 0
                @property
                def first(_self): return _self
            return _L()
        def screenshot(self, path, **_k):
            with open(path, "wb") as f: f.write(b"")

    def _fake_start(self):
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = _FakePage()
    def _fake_stop(self): pass
    def _fake_execute(self, action):
        from cua.surface.types import ExecutionResult
        started_page_state["n"] += 1
        return ExecutionResult(ok=True, detail="fake", resolved_tier=0)

    monkeypatch.setattr(web_mod.WebSurface, "start", _fake_start)
    monkeypatch.setattr(web_mod.WebSurface, "stop", _fake_stop)
    monkeypatch.setattr(web_mod.WebSurface, "execute", _fake_execute)

    # Stub find_matching_outcome to say we're at MEMBER_NOT_FOUND from step 1 onward.
    from cua.replay import engine as engine_mod
    def _fake_find(_surface, outcomes):
        for o in outcomes:
            if o.code == "MEMBER_NOT_FOUND":
                return o
        return None
    monkeypatch.setattr(engine_mod, "find_matching_outcome", _fake_find)

    result = replay_artifact(
        art, {}, policy=policy, evidence_root=str(tmp_path),
        unattended=True, headed=False,
    )
    assert result.outcome == "MEMBER_NOT_FOUND", (
        f"expected early-outcome short-circuit; got {result.outcome!r} err={result.error!r}"
    )
    assert result.error is None
