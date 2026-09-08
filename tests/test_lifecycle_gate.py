"""Tests for the draft -> approved lifecycle gate.

The gate refuses unattended replay of a `draft` artifact. This test
exercises the gate at the engine boundary without spinning up a real
browser — we construct a minimal `draft` artifact and confirm the engine
returns `DRAFT_ARTIFACT_REFUSED` before touching the Surface.

Rationale for locking this in with a test: this is the "no accidentally
promoting an unreviewed capability" guarantee. If a future refactor
short-circuits the check, unattended replays would silently execute a
capability nobody signed off on — the kind of thing that ends up in an
incident post-mortem. Structural, not documented.
"""
from __future__ import annotations

from cua.artifact.schema import (
    Artifact,
    ArtifactId,
    DetectedBy,
    Outcome,
    Provenance,
    Step,
)
from cua.replay.engine import replay_artifact
from cua.safety.policy import Policy


def _minimal_draft(tmp_evidence_root: str) -> Artifact:
    return Artifact(
        id=ArtifactId(name="cap", version="1.0.0", target="t"),
        title="t",
        description="d",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        outcomes=[
            Outcome(code="SUCCESS", detected_by=DetectedBy(kind="url_pattern", pattern="^.*$"))
        ],
        # No navigate step at all — the gate must fire before we open a browser.
        steps=[Step(id="s1", action="navigate", url="http://127.0.0.1:1/")],
        approval_state="draft",
        provenance=Provenance(
            goal="g", model="m", recorded_at="2026-01-01T00:00:00Z",
            run_id="r", target_url="http://x",
        ),
    )


def test_unattended_replay_of_draft_is_refused(tmp_path):
    art = _minimal_draft(str(tmp_path))
    policy = Policy(allowed_domains=["127.0.0.1:1"], allowed_actions=["navigate"], risky_gate=True)
    result = replay_artifact(
        art,
        {},
        policy=policy,
        evidence_root=str(tmp_path),
        unattended=True,
        allow_draft=False,
        headed=False,
    )
    assert result.outcome == "DRAFT_ARTIFACT_REFUSED"
    assert "approval_state" in result.error["expected"]


def test_allow_draft_flag_lets_it_through(tmp_path):
    """The escape hatch: --allow-draft. This IS audited — the run manifest
    still records that a draft was executed."""
    art = _minimal_draft(str(tmp_path))
    policy = Policy(allowed_domains=["127.0.0.1:1"], allowed_actions=["navigate"], risky_gate=True)
    # We can't actually run browser navigation in a unit test — the gate
    # under `allow_draft=True` should fall through to the normal engine
    # path. It'll then fail at browser startup, but NOT with
    # DRAFT_ARTIFACT_REFUSED. That's the property we're asserting.
    result = replay_artifact(
        art,
        {},
        policy=policy,
        evidence_root=str(tmp_path),
        unattended=True,
        allow_draft=True,
        headed=False,
    )
    assert result.outcome != "DRAFT_ARTIFACT_REFUSED"


def test_approved_artifact_bypasses_gate(tmp_path):
    art = _minimal_draft(str(tmp_path))
    art.approval_state = "approved"
    policy = Policy(allowed_domains=["127.0.0.1:1"], allowed_actions=["navigate"], risky_gate=True)
    result = replay_artifact(
        art,
        {},
        policy=policy,
        evidence_root=str(tmp_path),
        unattended=True,
        headed=False,
    )
    assert result.outcome != "DRAFT_ARTIFACT_REFUSED"
