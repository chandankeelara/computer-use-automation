"""Deterministic replay engine.

Contract:
  Result = {outcome: <code>|null, outputs: {..}|null, error: {..}|null, evidence_dir}
    - outcome == "SUCCESS": happy path.
    - outcome == other declared code (e.g. MEMBER_NOT_FOUND): typed business
      outcome, NOT a crash.
    - outcome is null + error set: hard failure.

Escalation triggers:
  (a) locator all-miss on a step
  (b) unknown page: no outcome predicate matches AND expected post-condition
      does not hold
  (c) risky step without --auto-approve-risky
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..artifact.schema import Artifact, Step
from ..escalation.handoff import request_handoff
from ..evidence.logger import EvidenceLogger
from ..safety.policy import Allowed, Denied, Policy, RequiresApproval
from ..safety.redaction import redact_inputs, redact_value
from ..surface.types import Action, Locator
from ..surface.web import WebSurface
from .extractor import extract_outputs
from .outcome_matcher import find_matching_outcome

log = logging.getLogger(__name__)


@dataclass
class ReplayResult:
    outcome: Optional[str] = None
    outputs: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    evidence_dir: str = ""
    resolved_via_handoff: bool = False


def _substitute(value: Optional[str], inputs: dict[str, Any]) -> Optional[str]:
    if value is None:
        return None
    out = value
    for k, v in inputs.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _check_expectation(surface: WebSurface, step: Step) -> bool:
    if step.expect is None:
        return True
    page = surface.page
    if step.expect.kind == "url_pattern" and step.expect.pattern:
        return re.match(step.expect.pattern, page.url) is not None
    if step.expect.kind == "text_present" and step.expect.text:
        try:
            return step.expect.text in page.locator("body").inner_text()
        except Exception:
            return False
    return True


def replay_artifact(
    artifact: Artifact,
    inputs: dict[str, Any],
    *,
    policy: Policy,
    evidence_root: str = "evidence",
    auto_approve_risky: bool = False,
    unattended: bool = False,
    run_id: Optional[str] = None,
) -> ReplayResult:
    run_id = run_id or f"replay_{inputs.get('member_id','x')}_{uuid.uuid4().hex[:6]}"
    evidence = EvidenceLogger(evidence_root, run_id)
    evidence.start_manifest(
        "replay",
        {
            "artifact": artifact.id.model_dump(),
            "inputs": redact_inputs(inputs, artifact.input_schema),
        },
    )
    surface = WebSurface(headed=True, evidence_dir=evidence.dir)
    surface.start()
    resolved_via_handoff = False
    try:
        # Ensure at least one navigate step exists (mock scripts always include one).
        for step in artifact.steps:
            n = evidence.next_step()
            entry: dict[str, Any] = {
                "step": n,
                "step_id": step.id,
                "action": step.action,
                "risk": step.risk,
            }

            # Policy check
            decision = policy.check(
                step.action, url=_substitute(step.url, inputs), risk=step.risk
            )
            if isinstance(decision, Denied):
                entry["policy"] = "denied"
                evidence.log_step(entry)
                res = ReplayResult(
                    error={
                        "step": step.id,
                        "expected": "policy_allowed",
                        "observed": decision.reason,
                    },
                    evidence_dir=evidence.dir,
                )
                evidence.finalize({"outcome": None, "error": res.error})
                return res
            if isinstance(decision, RequiresApproval) and not auto_approve_risky:
                # Escalate — trigger (c)
                snap = surface.snapshot(screenshot=True)
                ax_path = evidence.dump_ax(n, snap.ax)
                shot = evidence.screenshot_path(n)
                surface.screenshot(shot)
                payload = {
                    "capability": artifact.id.model_dump(),
                    "step_id": step.id,
                    "reason": f"risky action requires approval: {decision.reason}",
                    "current_url": snap.url,
                    "screenshot_path": shot,
                    "ax_snapshot_path": ax_path,
                }
                evidence.write_intervention_request(payload)
                resumed = request_handoff(payload, unattended=unattended)
                if not resumed:
                    res = ReplayResult(
                        outcome="ESCALATED_UNRESOLVED",
                        error={
                            "step": step.id,
                            "expected": "operator_approval",
                            "observed": "no_response",
                        },
                        evidence_dir=evidence.dir,
                    )
                    evidence.log_step({**entry, "escalated": True, "resolved": False})
                    evidence.finalize({"outcome": res.outcome})
                    return res
                resolved_via_handoff = True
                # After resume, re-check the current URL — the operator may
                # have already completed the action.
                evidence.log_step({**entry, "escalated": True, "resolved": True})
                # Fall through to still-try the step; if it fails softly we
                # proceed to outcome match.

            # Execute action
            value = _substitute(step.value, inputs)
            url = _substitute(step.url, inputs)
            action = Action(
                kind=step.action,
                target=(
                    Locator(primary=step.target.primary, fallbacks=step.target.fallbacks)
                    if step.target
                    else None
                ),
                value=value,
                url=url,
            )
            t0 = time.time()
            result = surface.execute(action)
            entry["ok"] = result.ok
            entry["detail"] = redact_value(result.detail, inputs, artifact.input_schema)
            entry["resolved_tier"] = result.resolved_tier
            entry["ms"] = int((time.time() - t0) * 1000)

            # Screenshot on failure
            if not result.ok:
                shot = evidence.screenshot_path(n)
                try:
                    surface.screenshot(shot)
                    entry["screenshot_on_fail"] = shot
                except Exception:
                    pass

            evidence.log_step(entry)

            if not result.ok:
                # Trigger (a): locator all-miss → check outcome first; maybe
                # we already landed on a business outcome page.
                matched = find_matching_outcome(surface, artifact.outcomes)
                if matched and matched.code != "SUCCESS":
                    outputs = extract_outputs(surface, matched, artifact.output_schema) if matched.extracts else {}
                    res = ReplayResult(
                        outcome=matched.code,
                        outputs=outputs or None,
                        evidence_dir=evidence.dir,
                        resolved_via_handoff=resolved_via_handoff,
                    )
                    evidence.finalize({"outcome": matched.code, "outputs": outputs})
                    return res
                # Otherwise escalate.
                snap = surface.snapshot(screenshot=True)
                ax_path = evidence.dump_ax(n, snap.ax)
                payload = {
                    "capability": artifact.id.model_dump(),
                    "step_id": step.id,
                    "reason": f"locator all-miss or action error: {result.detail}",
                    "current_url": snap.url,
                    "screenshot_path": entry.get("screenshot_on_fail"),
                    "ax_snapshot_path": ax_path,
                }
                evidence.write_intervention_request(payload)
                resumed = request_handoff(payload, unattended=unattended)
                if not resumed:
                    res = ReplayResult(
                        error={
                            "step": step.id,
                            "expected": "step_succeeds",
                            "observed": result.detail,
                        },
                        evidence_dir=evidence.dir,
                    )
                    evidence.finalize({"outcome": None, "error": res.error})
                    return res
                resolved_via_handoff = True

            # Check post-condition
            if not _check_expectation(surface, step):
                # Trigger (b) — unknown page.
                matched = find_matching_outcome(surface, artifact.outcomes)
                if matched and matched.code != "SUCCESS":
                    outputs = (
                        extract_outputs(surface, matched, artifact.output_schema)
                        if matched.extracts
                        else {}
                    )
                    res = ReplayResult(
                        outcome=matched.code,
                        outputs=outputs or None,
                        evidence_dir=evidence.dir,
                    )
                    evidence.finalize({"outcome": matched.code, "outputs": outputs})
                    return res

        # After all steps: match outcome
        matched = find_matching_outcome(surface, artifact.outcomes)
        if matched is None:
            res = ReplayResult(
                error={
                    "step": "<terminal>",
                    "expected": "any declared outcome",
                    "observed": surface.page.url,
                },
                evidence_dir=evidence.dir,
            )
            evidence.finalize({"outcome": None, "error": res.error})
            return res

        outputs = extract_outputs(surface, matched, artifact.output_schema) if matched.extracts else {}
        res = ReplayResult(
            outcome=matched.code,
            outputs=outputs or None,
            evidence_dir=evidence.dir,
            resolved_via_handoff=resolved_via_handoff,
        )
        evidence.finalize({"outcome": matched.code, "outputs": outputs})
        return res
    finally:
        surface.stop()
