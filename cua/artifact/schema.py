"""Pydantic v2 models for the capability artifact.

Identity = (name, version, target). That triple lets the registry hold
multiple concrete implementations of the same *capability* across tenants —
Midwest Federal Servicing Console vs. Summit Credit Union's console can
both publish `open_sub_account.v1.0.0` and callers pick by target.

Overlays (see cua.artifact.overlay) let a second tenant piggyback on a
base artifact with a small patch (relabelled anchors, extra allowlist
entries) rather than re-recording the whole flow. Overlays *cannot* touch
the output_schema or the step count / action kinds — that's the guarantee
the registry surfaces to callers.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ArtifactId(BaseModel):
    name: str
    version: str
    target: str  # human-readable target system name


class LocatorStrategy(BaseModel):
    primary: dict[str, Any]
    fallbacks: list[dict[str, Any]] = Field(default_factory=list)


class DetectedBy(BaseModel):
    kind: Literal["url_pattern", "text_present", "element_visible"]
    pattern: Optional[str] = None
    text: Optional[str] = None
    locator: Optional[LocatorStrategy] = None


class ExtractSpec(BaseModel):
    name: str
    from_: Literal["text", "url"] = Field(alias="from")
    locator: Optional[LocatorStrategy] = None
    url_pattern: Optional[str] = None
    # regex to pull the value out of the text/url
    regex: Optional[str] = None

    model_config = {"populate_by_name": True}


class Outcome(BaseModel):
    code: str
    terminal: bool = True
    detected_by: DetectedBy
    extracts: list[ExtractSpec] = Field(default_factory=list)


class SideEffect(BaseModel):
    kind: str
    reversible: bool


class Expectation(BaseModel):
    kind: Literal["url_pattern", "text_present", "element_visible"]
    pattern: Optional[str] = None
    text: Optional[str] = None
    locator: Optional[LocatorStrategy] = None


class Step(BaseModel):
    id: str
    action: Literal["navigate", "click", "type", "press", "read", "assert"]
    target: Optional[LocatorStrategy] = None
    value: Optional[str] = None  # may reference {{param}}
    url: Optional[str] = None
    expect: Optional[Expectation] = None
    risk: Literal["safe", "risky"] = "safe"
    # human_required: this step is by design done by a human on every
    # run (canonical example: OTP / 2FA entry — only the human has the
    # code). Distinct from `risk: risky`, which is a policy gate that
    # `--auto-approve-risky` can bypass. `human_required` is NOT
    # bypassable — the engine will always escalate before this step,
    # and after the operator resumes it will NOT execute the step
    # itself (the human already did it in the paused tab). The resume
    # gate additionally requires human_actions_recorded > 0 unless
    # force=true (audited).
    human_required: bool = False
    notes: str = ""


class Checkpoint(BaseModel):
    kind: Literal["url_pattern", "text_present"]
    pattern: Optional[str] = None
    text: Optional[str] = None


class RecoveryTrigger(BaseModel):
    """When does this recovery fire?"""
    kind: Literal["text_present", "url_pattern", "element_visible"]
    pattern: Optional[str] = None
    text: Optional[str] = None
    locator: Optional[LocatorStrategy] = None


class Recovery(BaseModel):
    """Capability-level auto-recovery for known-transient conditions.

    Applied after any step whose result is `ok=False` or whose post-condition
    fails. Ordered — first match wins. If the recovery succeeds, the step is
    retried up to `max_attempts` times. If none apply, the engine escalates.

    action semantics:
      dismiss   click `dismiss_locator`; retry the current step
      wait      sleep `wait_ms`; retry the current step
      retry     do nothing extra; retry the current step
      reauth    click `reauth_locator` (a "Sign in" link); restart the
                WHOLE flow from step 0. This is the standard session-
                timeout pattern: after re-auth the session state is
                healthy but the previous position is lost, so re-running
                from the entry step is the only correct semantic. Guard
                with `max_attempts` — the counter is per-run, not per-step.

    This is deliberately data, not code: adding a new interstitial the app
    starts throwing (e.g. a marketing modal, a re-auth wall, a permission
    prompt) is a schema change, not a codebase change.
    """
    name: str
    trigger: RecoveryTrigger
    action: Literal["dismiss", "wait", "retry", "reauth"]
    dismiss_locator: Optional[LocatorStrategy] = None
    reauth_locator: Optional[LocatorStrategy] = None
    reauth_wait_ms: int = 500
    wait_ms: int = 500
    max_attempts: int = 2
    notes: str = ""


class Provenance(BaseModel):
    goal: str
    model: str
    recorded_at: str
    run_id: str
    target_url: str


ApprovalState = Literal["draft", "approved", "deprecated"]


class Artifact(BaseModel):
    id: ArtifactId
    title: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema; "sensitive": true marks secrets
    output_schema: dict[str, Any]
    outcomes: list[Outcome]
    side_effects: list[SideEffect] = Field(default_factory=list)
    steps: list[Step]
    checkpoint: Optional[Checkpoint] = None
    recoveries: list[Recovery] = Field(default_factory=list)
    # Lifecycle: `draft` = just recorded, not yet reviewed; `approved` = a
    # human has signed off; `deprecated` = superseded. Unattended replay
    # refuses `draft` — a caller wanting to run a not-yet-approved
    # capability has to pass an explicit escape hatch that lands in the
    # audit log. This mirrors the "promote to production" gate any real
    # ops system would have around irreversible side effects.
    approval_state: ApprovalState = "draft"
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    provenance: Provenance


# ---------------------------------------------------------------------------
# Overlay: a small patch layered on top of a base artifact for a different
# tenant running the same underlying vendor product.
# ---------------------------------------------------------------------------


class OverlayBaseRef(BaseModel):
    name: str
    version: str
    target: str


class StepPatch(BaseModel):
    """Patch a single step by id.

    Overlays may replace `target`, `value`, or `expect` on a step. They
    cannot change the step's `action` or `id` — that's a schema break, not
    a tenant relabel.
    """
    step_id: str
    target: Optional[LocatorStrategy] = None
    value: Optional[str] = None
    expect: Optional[Expectation] = None
    notes: Optional[str] = None


class Overlay(BaseModel):
    id: ArtifactId  # the overlay's own (name, version, tenant-target)
    based_on: OverlayBaseRef
    title: Optional[str] = None
    description: Optional[str] = None
    entry_url: Optional[str] = None  # replaces the first navigate step's url
    step_patches: list[StepPatch] = Field(default_factory=list)
    extra_outcomes: list[Outcome] = Field(default_factory=list)
    extra_recoveries: list[Recovery] = Field(default_factory=list)
    extra_allowed_domains: list[str] = Field(default_factory=list)
    notes: str = ""


def is_overlay(doc: dict) -> bool:
    """Cheap discriminator for load-time routing."""
    return "based_on" in doc
