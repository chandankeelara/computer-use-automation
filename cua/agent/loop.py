"""Discovery driver.

Executes a stream of tool-calls (from the mock LLM or a live model)
against a WebSurface, records them via Recorder, and emits an Artifact.
"""
from __future__ import annotations

import logging
import uuid
from typing import Iterable, Optional

from ..artifact.schema import Artifact
from ..evidence.logger import EvidenceLogger
from ..safety.policy import Policy, Denied, RequiresApproval
from ..safety.redaction import redact_value
from ..surface.types import Action, Locator
from ..surface.web import WebSurface
from .mock_llm import script_open_sub_account
from .recorder import Recorder

log = logging.getLogger(__name__)

CAPABILITY_NAME = "open_sub_account"
VERSION = "1.0.0"
TARGET_NAME = "midwest_federal"
DISCOVERY_MODEL = "mock-llm@1"


def _default_input_spec(member_id: str) -> dict:
    return {
        "type": "object",
        "required": ["member_id"],
        "properties": {
            "member_id": {"type": "string", "example": member_id},
        },
    }


DEFAULT_OUTPUT_SPEC = {
    "type": "object",
    "properties": {
        "sub_account_id": {"type": "string", "pattern": r"^SA\d+$"},
    },
    "required": ["sub_account_id"],
}


def run_discovery(
    *,
    goal: str,
    target_url: str,
    member_id: str,
    evidence_root: str = "evidence",
    use_mock: bool = True,
    approve_risky: bool = False,
    policy: Optional[Policy] = None,
) -> Artifact:
    run_id = f"discovery_{member_id}"
    evidence = EvidenceLogger(evidence_root, run_id)
    evidence.start_manifest(
        "discovery", {"goal": goal, "target_url": target_url, "use_mock": use_mock}
    )
    input_spec = _default_input_spec(member_id)
    recorder = Recorder(
        goal=goal,
        target_url=target_url,
        target_name=TARGET_NAME,
        capability_name=CAPABILITY_NAME,
        version=VERSION,
        run_id=run_id,
        model=DISCOVERY_MODEL if use_mock else "claude-sonnet-4-6",
        input_spec=input_spec,
        output_spec=DEFAULT_OUTPUT_SPEC,
    )
    surface = WebSurface(headed=True, evidence_dir=evidence.dir)
    surface.start()
    stream: Iterable[dict]
    if use_mock:
        stream = script_open_sub_account(target_url, member_id=member_id)
    else:  # pragma: no cover
        from .llm import LiveLLM

        stream = LiveLLM(goal=goal, target_url=target_url).stream()

    inputs_for_redaction = {"member_id": member_id}
    try:
        for call in stream:
            tool = call["tool"]
            args = call.get("args", {})
            n = evidence.next_step()

            # Policy check for actionable calls
            if tool in ("navigate", "click", "type"):
                risk = args.get("risk", "safe")
                url = args.get("url")
                decision = (policy.check(tool, url=url, risk=risk) if policy else None)
                if isinstance(decision, Denied):
                    log.error("policy denied: %s", decision.reason)
                    raise RuntimeError(f"policy denied: {decision.reason}")
                if isinstance(decision, RequiresApproval) and not approve_risky:
                    log.warning("risky discovery step %s: %s", tool, decision.reason)
                    # In discovery we auto-record risky steps (they're what
                    # makes side_effects non-empty). Approval gating is a
                    # REPLAY concern; here we just note it.

            if tool == "navigate":
                url = args["url"]
                res = surface.execute(Action(kind="navigate", url=url))
                recorder.on_navigate(url)
            elif tool == "get_snapshot":
                snap = surface.snapshot(screenshot=True)
                evidence.dump_ax(n, snap.ax)
                evidence.log_step(
                    {
                        "step": n,
                        "tool": tool,
                        "url": snap.url,
                        "title": snap.title,
                        "screenshot": snap.screenshot_path,
                    }
                )
                continue
            elif tool == "click":
                primary = args["primary"]
                fallbacks = args.get("fallbacks", [])
                res = surface.execute(
                    Action(kind="click", target=Locator(primary=primary, fallbacks=fallbacks))
                )
                recorder.on_click(
                    primary,
                    fallbacks,
                    args.get("notes", ""),
                    args.get("risk", "safe"),
                )
            elif tool == "type":
                primary = args["primary"]
                fallbacks = args.get("fallbacks", [])
                value_template = args["value"]
                # {{member_id}} expansion for the live execution
                value = value_template.replace("{{member_id}}", member_id)
                res = surface.execute(
                    Action(
                        kind="type",
                        target=Locator(primary=primary, fallbacks=fallbacks),
                        value=value,
                    )
                )
                recorder.on_type(primary, fallbacks, value_template, args.get("notes", ""))
            elif tool == "mark_output":
                recorder.on_mark_output(args["name"], args["from"], args.get("regex"))
                evidence.log_step({"step": n, "tool": tool, "args": args})
                continue
            elif tool == "finish":
                recorder.on_finish(
                    args["outcome_code"], args.get("checkpoint_url_pattern")
                )
                evidence.log_step({"step": n, "tool": tool, "args": args})
                break
            else:
                log.warning("skipping unknown tool: %s", tool)
                continue

            # Log actionable outcome
            entry = {
                "step": n,
                "tool": tool,
                "ok": res.ok,
                "detail": redact_value(res.detail, inputs_for_redaction, input_spec),
                "resolved_tier": res.resolved_tier,
            }
            evidence.log_step(entry)
            if not res.ok:
                # Discovery-time failure — capture snapshot as evidence
                shot = evidence.screenshot_path(n)
                try:
                    surface.screenshot(shot)
                    entry["screenshot"] = shot
                except Exception:
                    pass
                raise RuntimeError(f"discovery step failed: {res.detail}")

        artifact = recorder.build()
        evidence.finalize({"ok": True, "steps": len(artifact.steps)})
        return artifact
    finally:
        surface.stop()
