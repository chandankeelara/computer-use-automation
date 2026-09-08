"""Deterministic replay engine.

Contract:
  Result = {outcome: <code>|null, outputs: {..}|null, error: {..}|null,
            evidence_dir, resolved_via_handoff, tier_usage, recoveries_applied}
    - outcome == "SUCCESS": happy path.
    - outcome == other declared code (e.g. MEMBER_NOT_FOUND, ACCESS_DENIED,
      VALIDATION_ERROR): typed business outcome, NOT a crash.
    - outcome is null + error set: hard failure.
    - outcome == "ESCALATED_UNRESOLVED": handoff requested and either
      declined, unattended-failed, or timed out.

Ownership:
  The engine acts through GuardedSurface. Every execute() call passes
  through SessionController.assert_agent(); actions taken while the state
  is `paused` or `human` raise OwnershipError instead of silently reaching
  the browser. Control transitions are logged in evidence.

Escalation triggers:
  (a) locator all-miss on a step
  (b) unknown page: no outcome predicate matches AND expected post-
      condition does not hold
  (c) risky step without --auto-approve-risky

Recovery loop:
  Before escalating on (a) or (b), the engine tries the artifact's
  declared `recoveries` list. A recovery's trigger is a text/URL/element
  predicate; its action is dismiss / wait / retry. If any recovery fires,
  the step is retried (up to Recovery.max_attempts). This turns "a modal
  popped up during replay" into a data change, not a code change.

Resume gate:
  On resume from handoff we re-verify the step's post-condition (or the
  capability checkpoint if the step has no expect). If the invariant does
  not hold we re-escalate rather than proceed on faith.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..artifact.schema import Artifact, Recovery, Step
from ..escalation.console import OperatorConsole
from ..escalation.controller import OwnershipError, SessionController
from ..escalation.handoff import request_handoff
from ..escalation.notify import notify_all
from ..evidence.logger import EvidenceLogger
from ..safety.policy import Allowed, Denied, Policy, RequiresApproval
from ..safety.redaction import redact_inputs, redact_value
from ..surface.guarded import GuardedSurface
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
    tier_usage: dict[str, int] = field(default_factory=dict)
    recoveries_applied: list[dict] = field(default_factory=list)


def _substitute(value: Optional[str], inputs: dict[str, Any]) -> Optional[str]:
    if value is None:
        return None
    out = value
    for k, v in inputs.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _matches_predicate(page, kind: str, *, pattern: Optional[str] = None,
                       text: Optional[str] = None) -> bool:
    try:
        if kind == "url_pattern" and pattern:
            return re.match(pattern, page.url) is not None
        if kind == "text_present" and text:
            return text in page.locator("body").inner_text()
    except Exception:
        return False
    return False


def _check_expectation(surface: WebSurface, step: Step) -> bool:
    if step.expect is None:
        return True
    return _matches_predicate(
        surface.page,
        step.expect.kind,
        pattern=step.expect.pattern,
        text=step.expect.text,
    )


def _try_recoveries(
    surface: WebSurface,
    recoveries: list[Recovery],
    evidence: EvidenceLogger,
    step_id: str,
) -> Optional[dict]:
    """Try each recovery whose trigger matches. Return the applied record
    or None if none applied."""
    page = surface.page
    for rec in recoveries:
        trig = rec.trigger
        try:
            fired = False
            if trig.kind == "text_present" and trig.text:
                if trig.text in page.locator("body").inner_text():
                    fired = True
            elif trig.kind == "url_pattern" and trig.pattern:
                if re.match(trig.pattern, page.url):
                    fired = True
            elif trig.kind == "element_visible" and trig.locator is not None:
                spec = trig.locator.primary
                if spec.get("by") == "css":
                    if page.locator(spec["selector"]).count() > 0:
                        fired = True
        except Exception:
            fired = False
        if not fired:
            continue

        applied: dict[str, Any] = {"name": rec.name, "action": rec.action, "step_id": step_id}
        try:
            if rec.action == "wait":
                page.wait_for_timeout(rec.wait_ms)
            elif rec.action == "dismiss" and rec.dismiss_locator is not None:
                _click_locator(page, rec.dismiss_locator.primary)
                page.wait_for_load_state("domcontentloaded")
            elif rec.action == "reauth" and rec.reauth_locator is not None:
                _click_locator(page, rec.reauth_locator.primary)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(rec.reauth_wait_ms)
                applied["restart_from_step_0"] = True
            # 'retry' does no work here; the driver retries the step.
            applied["ok"] = True
        except Exception as e:
            applied["ok"] = False
            applied["detail"] = str(e)
        evidence.log_step({"recovery": applied})
        return applied
    return None


def _click_locator(page, spec: dict[str, Any]) -> None:
    """Click a locator described by a spec dict (role_name / css / text)."""
    by = spec.get("by")
    if by == "css":
        page.locator(spec["selector"]).first.click(timeout=3000)
    elif by == "role_name":
        page.get_by_role(spec["role"], name=spec.get("name")).first.click(timeout=3000)
    elif by == "text":
        page.get_by_text(spec["text"]).first.click(timeout=3000)
    else:
        raise ValueError(f"unsupported locator kind for click: {by}")


def replay_artifact(
    artifact: Artifact,
    inputs: dict[str, Any],
    *,
    policy: Policy,
    evidence_root: str = "evidence",
    auto_approve_risky: bool = False,
    unattended: bool = False,
    run_id: Optional[str] = None,
    operator_port: Optional[int] = None,
    headed: bool = True,
    allow_draft: bool = False,
) -> ReplayResult:
    run_id = run_id or f"replay_{inputs.get('member_id','x')}_{uuid.uuid4().hex[:6]}"
    evidence = EvidenceLogger(evidence_root, run_id)
    evidence.start_manifest(
        "replay",
        {
            "artifact": artifact.id.model_dump(),
            "inputs": redact_inputs(inputs, artifact.input_schema),
            "operator_port": operator_port,
        },
    )

    # Lifecycle gate. A `draft` artifact cannot be replayed unattended —
    # someone has to explicitly `cua approve` it first, or pass
    # `allow_draft=True` (which lands in the run manifest for audit).
    # This is the "no accidentally running an unreviewed capability
    # against prod" gate.
    if artifact.approval_state != "approved":
        if unattended and not allow_draft:
            evidence.finalize(
                {
                    "outcome": "DRAFT_ARTIFACT_REFUSED",
                    "error": {
                        "step": "<pre-flight>",
                        "expected": f"approval_state=approved (got {artifact.approval_state})",
                        "observed": "unattended replay of a draft artifact is not allowed",
                    },
                }
            )
            return ReplayResult(
                outcome="DRAFT_ARTIFACT_REFUSED",
                error={
                    "step": "<pre-flight>",
                    "expected": f"approval_state=approved (got {artifact.approval_state})",
                    "observed": "unattended replay of a draft artifact is not allowed; run `cua approve` or pass allow_draft=True",
                },
                evidence_dir=evidence.dir,
            )

    controller = SessionController()
    console: Optional[OperatorConsole] = None
    if operator_port is not None:
        console = OperatorConsole(port=operator_port)
        console.start(controller)

    inner = WebSurface(headed=headed, evidence_dir=evidence.dir)
    inner.start()
    surface = GuardedSurface(inner, controller)

    result_container = ReplayResult(evidence_dir=evidence.dir)
    tier_usage: dict[str, int] = {}
    recoveries_applied: list[dict] = []

    def _run_step(step: Step, attempt: int = 1) -> tuple[bool, Any]:
        """Execute one step; return (ok, ExecutionResult)."""
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
        res = surface.execute(action)
        ms = int((time.time() - t0) * 1000)
        entry: dict[str, Any] = {
            "step_id": step.id,
            "action": step.action,
            "attempt": attempt,
            "ok": res.ok,
            "detail": redact_value(res.detail, inputs, artifact.input_schema),
            "resolved_tier": res.resolved_tier,
            "tier_verdicts": res.tier_verdicts,
            "ms": ms,
        }
        if step.target is not None and res.ok:
            tier_usage[step.id] = res.resolved_tier
        evidence.log_step(entry)
        return res.ok, res

    def _finalize(res: ReplayResult) -> ReplayResult:
        res.tier_usage = dict(tier_usage)
        res.recoveries_applied = list(recoveries_applied)
        evidence.finalize(
            {
                "outcome": res.outcome,
                "outputs": res.outputs,
                "error": res.error,
                "tier_usage": res.tier_usage,
                "recoveries_applied": res.recoveries_applied,
                "resolved_via_handoff": res.resolved_via_handoff,
                "control_transitions": controller.to_evidence(),
            }
        )
        return res

    def _escalate(step_id: str, reason: str, obs: str) -> bool:
        """Trigger a handoff. Returns True if operator resumed, else False.
        On resume, re-verifies the step's expectation / capability checkpoint
        and re-escalates if the invariant does not hold."""
        # Attach page action listener so human actions get counted.
        _attach_human_action_listener(inner, controller)
        # Inject an on-page banner into the *automation tab itself* so the
        # operator has an unmistakable visual signal that the automation
        # is paused and they now hold control. The operator console may
        # be a monitor away; the banner is in the tab they're about to
        # interact with.
        _inject_pause_banner(inner, reason=reason)

        n = evidence.next_step()
        snap = surface.snapshot(screenshot=True)
        ax_path = evidence.dump_ax(n, snap.ax)
        shot = evidence.screenshot_path(n)
        try:
            surface.screenshot(shot)
        except Exception:
            shot = None
        payload = {
            "capability": artifact.id.model_dump(),
            "step_id": step_id,
            "reason": reason,
            "observed": obs,
            "current_url": snap.url,
            "screenshot_path": shot,
            "ax_snapshot_path": ax_path,
        }
        evidence.write_intervention_request(payload)
        # Fire notifications *before* blocking on handoff so the operator
        # actually hears about it. Best-effort — sinks that fail are logged
        # into the manifest but never cause the replay to fail.
        notify_verdicts = notify_all(
            "intervention_requested",
            payload,
            bell=policy.notify_bell,
            webhook_url=policy.notify_webhook_url,
            slack_url=policy.notify_slack_webhook_url,
        )
        evidence.log_step({"notify": notify_verdicts, "event": "intervention_requested"})
        outcome = request_handoff(
            payload,
            controller=controller,
            console=console,
            unattended=unattended,
        )
        return bool(outcome.get("resumed"))

    # Reauth recoveries can fire a "restart the whole flow" instruction.
    # We bound the total restarts by the recovery's own `max_attempts` —
    # otherwise a target that permanently returns "session expired" would
    # loop forever. The counter is per-run, not per-step.
    reauth_restarts_remaining = max(
        (r.max_attempts for r in artifact.recoveries if r.action == "reauth"),
        default=0,
    )

    try:
        step_index = 0
        while step_index < len(artifact.steps):
            step = artifact.steps[step_index]
            n = evidence.next_step()
            base_entry: dict[str, Any] = {
                "step": n,
                "step_id": step.id,
                "action": step.action,
                "risk": step.risk,
            }

            # Pre-step outcome check. If the flow already terminated at a
            # declared non-SUCCESS business outcome (e.g. we're on a
            # not-found page after step s3), return the typed outcome
            # instead of running remaining steps.
            early = find_matching_outcome(inner, artifact.outcomes)
            if early is not None and early.code != "SUCCESS":
                outputs = (
                    extract_outputs(inner, early, artifact.output_schema)
                    if early.extracts
                    else {}
                )
                evidence.log_step({**base_entry, "early_outcome": early.code})
                result_container.outcome = early.code
                result_container.outputs = outputs or None
                return _finalize(result_container)

            # Policy check
            decision = policy.check(
                step.action, url=_substitute(step.url, inputs), risk=step.risk
            )
            if isinstance(decision, Denied):
                evidence.log_step({**base_entry, "policy": "denied", "reason": decision.reason})
                result_container.error = {
                    "step": step.id,
                    "expected": "policy_allowed",
                    "observed": decision.reason,
                }
                return _finalize(result_container)
            if isinstance(decision, RequiresApproval) and not auto_approve_risky:
                # Trigger (c): risky step gate.
                resumed = _escalate(
                    step.id,
                    f"risky action requires approval: {decision.reason}",
                    obs="awaiting operator decision",
                )
                if not resumed:
                    result_container.outcome = "ESCALATED_UNRESOLVED"
                    result_container.error = {
                        "step": step.id,
                        "expected": "operator_approval",
                        "observed": "no_response_or_declined",
                    }
                    return _finalize(result_container)
                result_container.resolved_via_handoff = True
                # Operator may have executed the action themselves; verify.
                # If checkpoint / expectation holds, skip to next step.
                if _check_expectation(surface, step) or _capability_checkpoint_holds(surface, artifact):
                    step_index += 1
                    continue

            # Execute the step, with recovery loop.
            max_attempts = 1 + max(r.max_attempts for r in artifact.recoveries) if artifact.recoveries else 1
            ok, res = _run_step(step, attempt=1)
            attempt = 1
            reauth_fired = False
            while not ok and attempt < max_attempts:
                applied = _try_recoveries(inner, artifact.recoveries, evidence, step.id)
                if applied is None:
                    break
                recoveries_applied.append(applied)
                if applied.get("restart_from_step_0"):
                    reauth_fired = True
                    break
                attempt += 1
                ok, res = _run_step(step, attempt=attempt)

            # Reauth recovery? Restart the whole flow from step 0.
            if reauth_fired:
                if reauth_restarts_remaining > 0:
                    reauth_restarts_remaining -= 1
                    evidence.log_step({"reauth_restart": True, "remaining": reauth_restarts_remaining})
                    step_index = 0
                    continue
                result_container.error = {
                    "step": step.id,
                    "expected": "reauth succeeds within max_attempts",
                    "observed": "reauth recovery fired but restart budget exhausted",
                }
                return _finalize(result_container)

            if not ok:
                # Trigger (a): step failed after recoveries. Check whether
                # we already landed on a declared business outcome.
                matched = find_matching_outcome(inner, artifact.outcomes)
                if matched and matched.code != "SUCCESS":
                    outputs = extract_outputs(inner, matched, artifact.output_schema) if matched.extracts else {}
                    result_container.outcome = matched.code
                    result_container.outputs = outputs or None
                    return _finalize(result_container)

                resumed = _escalate(
                    step.id,
                    f"locator all-miss or action error: {res.detail}",
                    obs=res.detail,
                )
                if not resumed:
                    result_container.outcome = "ESCALATED_UNRESOLVED"
                    result_container.error = {
                        "step": step.id,
                        "expected": "step_succeeds",
                        "observed": res.detail,
                    }
                    return _finalize(result_container)
                result_container.resolved_via_handoff = True
                # Resume gate: re-verify invariant before proceeding.
                if not _check_expectation(surface, step) and not _capability_checkpoint_holds(surface, artifact):
                    matched = find_matching_outcome(inner, artifact.outcomes)
                    if matched and matched.code != "SUCCESS":
                        outputs = extract_outputs(inner, matched, artifact.output_schema) if matched.extracts else {}
                        result_container.outcome = matched.code
                        result_container.outputs = outputs or None
                        return _finalize(result_container)
                    # Nothing held — re-escalate. This is the "refuse to
                    # proceed on faith" guarantee.
                    result_container.outcome = "ESCALATED_UNRESOLVED"
                    result_container.error = {
                        "step": step.id,
                        "expected": "step invariant after resume",
                        "observed": f"url={inner.page.url} — invariant did not hold",
                    }
                    return _finalize(result_container)

            # Post-condition check + trigger (b)
            if not _check_expectation(surface, step):
                matched = find_matching_outcome(inner, artifact.outcomes)
                if matched and matched.code != "SUCCESS":
                    outputs = (
                        extract_outputs(inner, matched, artifact.output_schema)
                        if matched.extracts
                        else {}
                    )
                    result_container.outcome = matched.code
                    result_container.outputs = outputs or None
                    return _finalize(result_container)

            # Even if the step returned ok, the page may still match a
            # recovery trigger (e.g. a 401 session-expired interstitial
            # that renders normally as HTML — Playwright doesn't fail).
            # Give recoveries a second chance to fire here.
            post_step_outcome = find_matching_outcome(inner, artifact.outcomes)
            if post_step_outcome is None:
                applied = _try_recoveries(inner, artifact.recoveries, evidence, step.id)
                if applied is not None:
                    recoveries_applied.append(applied)
                    if applied.get("restart_from_step_0"):
                        if reauth_restarts_remaining > 0:
                            reauth_restarts_remaining -= 1
                            evidence.log_step({"reauth_restart": True, "remaining": reauth_restarts_remaining, "trigger": "post_step"})
                            step_index = 0
                            continue
                        result_container.error = {
                            "step": step.id,
                            "expected": "reauth succeeds within max_attempts",
                            "observed": "reauth fired post-step but restart budget exhausted",
                        }
                        return _finalize(result_container)

            step_index += 1

        # After all steps: match outcome
        matched = find_matching_outcome(inner, artifact.outcomes)
        if matched is None:
            result_container.error = {
                "step": "<terminal>",
                "expected": "any declared outcome",
                "observed": inner.page.url,
            }
            return _finalize(result_container)

        outputs = extract_outputs(inner, matched, artifact.output_schema) if matched.extracts else {}
        result_container.outcome = matched.code
        result_container.outputs = outputs or None
        return _finalize(result_container)
    except OwnershipError as e:
        # Should not happen in normal flow — represents a real bug (agent
        # code tried to act while state != agent). Surface it explicitly.
        result_container.error = {
            "step": "<ownership>",
            "expected": "controller.state == agent",
            "observed": str(e),
        }
        return _finalize(result_container)
    finally:
        inner.stop()


def _capability_checkpoint_holds(surface: WebSurface, artifact: Artifact) -> bool:
    cp = artifact.checkpoint
    if cp is None:
        return False
    return _matches_predicate(surface.page, cp.kind, pattern=cp.pattern, text=cp.text)


def _attach_human_action_listener(surface: WebSurface, controller: SessionController) -> None:
    """Wire a page-level listener that increments human_actions_recorded on
    click/input/keydown events. Safe to call multiple times — Playwright
    dedups the exposed binding after the first.

    Rationale: the resume gate in SessionController refuses to hand control
    back to the agent unless at least one human action was recorded, so we
    need to actually count them.
    """
    try:
        # expose_binding is per-context and raises if already exposed; guard.
        surface.page.expose_binding(
            "_cuaNoteHumanAction",
            lambda source, kind: controller.note_human_action(kind),
        )
    except Exception:
        # Already bound (or context error) — either way, don't fail.
        pass
    try:
        surface.page.add_init_script(_HUMAN_LISTENER_JS)
        # Re-inject on the live page (init script only fires on navigations).
        surface.page.evaluate(_HUMAN_LISTENER_JS)
    except Exception:
        pass


def _inject_pause_banner(surface: WebSurface, *, reason: str) -> None:
    """Attach an unmissable visual banner to the top of the automation
    tab so the operator sees state == paused inside the browser being
    driven. Idempotent — the banner element has a fixed id, so repeated
    calls replace rather than stack.

    Rationale: operators sometimes miss the console signal because their
    eyes are already on the tab. Putting the signal inside the tab
    closes that gap."""
    escaped = (reason or "").replace("\\", "\\\\").replace("`", "\\`")
    js = (
        "(() => {"
        "  const id = '__cua_pause_banner';"
        "  document.getElementById(id)?.remove();"
        "  const el = document.createElement('div');"
        "  el.id = id;"
        "  el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;"
        "padding:10px 16px;background:#a00;color:#fff;font:600 14px system-ui,sans-serif;"
        "text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3)';"
        f"  el.textContent = 'AGENT PAUSED — YOU HAVE CONTROL. Reason: `{escaped}`';"
        "  document.body.appendChild(el);"
        "})();"
    )
    try:
        surface.page.evaluate(js)
    except Exception:
        # Best-effort — never fail a replay because a banner injection failed.
        pass


_HUMAN_LISTENER_JS = r"""
(() => {
  if (window.__cuaHumanListener) return;
  window.__cuaHumanListener = true;
  const emit = (kind) => {
    try { window._cuaNoteHumanAction && window._cuaNoteHumanAction(kind); } catch(e){}
  };
  window.addEventListener('click',     () => emit('click'),   true);
  window.addEventListener('input',     () => emit('input'),   true);
  window.addEventListener('keydown',   () => emit('keydown'), true);
  window.addEventListener('submit',    () => emit('submit'),  true);
})();
"""
