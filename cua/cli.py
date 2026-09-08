"""CLI entry point: `python -m cua.cli {discover,replay,stability,catalog}`.

argparse-based so we don't pull an extra dep just for the CLI. Keeps the
demo self-contained.

Notable flags:
  replay --operator-port 8765  spawns the FastAPI operator console; the
                               replay pauses on escalation and waits for
                               POST /resume (which is refused unless a
                               human action was recorded, or force=true).
  replay --tenant summit_credit_union
                               loads the overlay artifact whose
                               `based_on.target` == midwest_federal, and
                               replays against `?tenant=summit`.
  stability --n 10             replays N times and reports pass rate +
                               resolved-tier distribution — a cheap
                               flakiness signal.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
from collections import Counter

from .agent.loop import run_discovery
from .artifact.registry import list_artifacts
from .artifact.store import load_artifact, save_artifact
from .replay.engine import replay_artifact
from .safety.policy import Policy


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_discover(args: argparse.Namespace) -> int:
    policy = Policy.from_yaml(args.policy)
    member_id = args.member_id or _extract_member_id_from_goal(args.goal) or "12345"
    artifact = run_discovery(
        goal=args.goal,
        target_url=args.target,
        member_id=member_id,
        evidence_root=args.evidence_root,
        use_mock=args.mock_llm,
        policy=policy,
    )
    save_artifact(artifact, args.out)
    print(f"[discover] artifact written to {args.out}")
    print(
        f"[discover] steps: {len(artifact.steps)}, "
        f"outcomes: {[o.code for o in artifact.outcomes]}, "
        f"recoveries: {[r.name for r in artifact.recoveries]}"
    )
    return 0


def _extract_member_id_from_goal(goal: str) -> str | None:
    import re
    m = re.search(r"\b(\d{4,})\b", goal)
    return m.group(1) if m else None


def _resolve_artifact_path(args: argparse.Namespace) -> str:
    """Support --tenant shorthand: pick the matching overlay/base.

    Now that multiple capabilities live in one artifacts dir, `--tenant`
    alone is ambiguous — a bare `.midwest_federal.json` suffix would
    resolve to whichever capability sorts first alphabetically (real bug
    caught by the eval suite: `lookup_member_balance` sorts before
    `open_sub_account`, so `--tenant midwest_federal` was silently
    replaying the wrong capability). If more than one artifact matches
    the tenant, require `--capability` to disambiguate.
    """
    if args.tenant is None:
        return args.artifact
    dir_ = args.artifacts_dir
    capability = getattr(args, "capability", None)
    hits: list[str] = []
    for suffix in (f".{args.tenant}.overlay.json", f".{args.tenant}.json"):
        for path in sorted(os.listdir(dir_)):
            if path.endswith(suffix):
                if capability and not path.startswith(capability + "."):
                    continue
                hits.append(os.path.join(dir_, path))
    if not hits:
        raise SystemExit(
            f"no artifact for tenant='{args.tenant}'"
            + (f" capability='{capability}'" if capability else "")
            + f" under {dir_}"
        )
    if len(hits) > 1:
        raise SystemExit(
            f"ambiguous --tenant='{args.tenant}': matches {len(hits)} artifacts "
            f"({[os.path.basename(h) for h in hits]}). Pass --capability=NAME to disambiguate."
        )
    return hits[0]


def cmd_replay(args: argparse.Namespace) -> int:
    policy = Policy.from_yaml(args.policy)
    path = _resolve_artifact_path(args)
    artifact = load_artifact(path)
    try:
        inputs = json.loads(args.inputs) if args.inputs else {}
    except json.JSONDecodeError as e:
        print(f"invalid --inputs JSON: {e}", file=sys.stderr)
        return 2
    result = replay_artifact(
        artifact,
        inputs,
        policy=policy,
        evidence_root=args.evidence_root,
        auto_approve_risky=args.auto_approve_risky,
        unattended=args.unattended,
        operator_port=args.operator_port,
        headed=not args.headless,
        allow_draft=args.allow_draft,
        run_id=args.run_id,
    )
    print(json.dumps(
        {
            "artifact": path,
            "outcome": result.outcome,
            "outputs": result.outputs,
            "error": result.error,
            "evidence_dir": result.evidence_dir,
            "resolved_via_handoff": result.resolved_via_handoff,
            "tier_usage": result.tier_usage,
            "recoveries_applied": result.recoveries_applied,
        },
        indent=2,
    ))
    if result.outcome is None:
        return 1
    return 0


def cmd_stability(args: argparse.Namespace) -> int:
    """Replay the same artifact + inputs N times and report a flakiness
    signal. Reports: pass rate, outcome distribution, mean/p95 duration,
    tier-usage distribution per step."""
    policy = Policy.from_yaml(args.policy)
    path = _resolve_artifact_path(args)
    artifact = load_artifact(path)
    try:
        inputs = json.loads(args.inputs) if args.inputs else {}
    except json.JSONDecodeError as e:
        print(f"invalid --inputs JSON: {e}", file=sys.stderr)
        return 2

    outcomes: Counter[str] = Counter()
    tier_dist: dict[str, Counter] = {}
    durations: list[float] = []
    per_run = []
    for i in range(args.n):
        import time as _t
        t0 = _t.time()
        result = replay_artifact(
            artifact,
            inputs,
            policy=policy,
            evidence_root=args.evidence_root,
            auto_approve_risky=args.auto_approve_risky,
            unattended=True,  # stability = non-interactive
            headed=not args.headless,
            allow_draft=args.allow_draft,
            run_id=f"stability_{i+1}_of_{args.n}",
        )
        dur = _t.time() - t0
        durations.append(dur)
        code = result.outcome or f"HARD_FAILURE:{(result.error or {}).get('expected','?')}"
        outcomes[code] += 1
        for step_id, tier in (result.tier_usage or {}).items():
            tier_dist.setdefault(step_id, Counter())[tier] += 1
        per_run.append({"run": i + 1, "outcome": result.outcome, "duration_s": round(dur, 2)})
        print(f"[stability] run {i+1}/{args.n}: outcome={result.outcome} dur={dur:.2f}s")

    success = outcomes.get("SUCCESS", 0)
    report = {
        "artifact": path,
        "n": args.n,
        "success_rate": round(success / args.n, 3) if args.n else 0.0,
        "outcomes": dict(outcomes),
        "duration_s": {
            "mean": round(statistics.mean(durations), 2) if durations else 0.0,
            "p95": round(sorted(durations)[max(int(0.95 * len(durations)) - 1, 0)], 2) if durations else 0.0,
        },
        "tier_usage_per_step": {k: dict(v) for k, v in tier_dist.items()},
        "runs": per_run,
    }
    print(json.dumps(report, indent=2))
    if args.report_out:
        with open(args.report_out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"[stability] wrote report to {args.report_out}")
    return 0 if outcomes.get("SUCCESS", 0) == args.n else 1


def cmd_eval(args: argparse.Namespace) -> int:
    """Run a golden-eval JSON file. Each scenario replays unattended, headless,
    against the artifact resolved from its `tenant`. Compares outcome and
    optionally output values against regex patterns. Non-zero exit on any
    mismatch — suitable for CI or a promotion gate on `cua approve`."""
    import re as _re
    with open(args.evals, "r", encoding="utf-8") as f:
        spec = json.load(f)
    scenarios = spec.get("scenarios", [])
    policy = Policy.from_yaml(args.policy)
    results = []
    passes = 0
    for sc in scenarios:
        inputs = sc.get("inputs", {})
        # Scenario may name the artifact directly or resolve via tenant.
        # `artifact` wins when both are present so multi-capability suites
        # (open_sub_account vs lookup_member_balance) can address each
        # capability precisely.
        if "artifact" in sc:
            path = sc["artifact"]
        else:
            args_ns = argparse.Namespace(
                artifact=None, tenant=sc["tenant"], artifacts_dir=args.artifacts_dir,
            )
            try:
                path = _resolve_artifact_path(args_ns)
            except SystemExit as e:
                results.append({"name": sc["name"], "pass": False, "error": str(e)})
                continue
        artifact = load_artifact(path)
        result = replay_artifact(
            artifact,
            inputs,
            policy=policy,
            evidence_root=args.evidence_root,
            auto_approve_risky=bool(sc.get("auto_approve_risky", False)),
            unattended=True,
            headed=False,
            allow_draft=False,
            run_id=f"eval__{sc['name']}",
        )
        outcome_ok = result.outcome == sc["expected_outcome"]
        outputs_ok = True
        mismatches = []
        for k, pattern in (sc.get("expected_output_patterns") or {}).items():
            v = (result.outputs or {}).get(k, "")
            if not _re.match(pattern, str(v)):
                outputs_ok = False
                mismatches.append({"key": k, "pattern": pattern, "actual": v})
        p = outcome_ok and outputs_ok
        if p:
            passes += 1
        results.append({
            "name": sc["name"],
            "pass": p,
            "expected_outcome": sc["expected_outcome"],
            "got_outcome": result.outcome,
            "got_outputs": result.outputs,
            "output_mismatches": mismatches,
            "evidence_dir": result.evidence_dir,
        })
        status = "PASS" if p else "FAIL"
        print(f"[eval] {status} {sc['name']}: expected={sc['expected_outcome']} got={result.outcome}")

    summary = {
        "evals": args.evals,
        "total": len(scenarios),
        "pass": passes,
        "fail": len(scenarios) - passes,
        "results": results,
    }
    if args.report_out:
        with open(args.report_out, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"[eval] wrote report to {args.report_out}")
    print(f"[eval] {passes}/{len(scenarios)} passed")
    return 0 if passes == len(scenarios) else 1


def cmd_approve(args: argparse.Namespace) -> int:
    """Promote an artifact from draft -> approved (or deprecated)."""
    import datetime as _dt
    from .artifact.store import load_document, save_artifact, save_overlay
    doc = load_document(args.artifact)
    # Overlays inherit the approval state of the base at load-time; only
    # promote/demote on the base or on standalone artifacts.
    from .artifact.schema import Artifact, Overlay
    if isinstance(doc, Overlay):
        print("[approve] refusing: overlays inherit approval from base; approve the base instead.")
        return 2
    doc.approval_state = args.to
    doc.approved_by = args.by
    doc.approved_at = _dt.datetime.utcnow().isoformat() + "Z"
    save_artifact(doc, args.artifact)
    print(f"[approve] {args.artifact}: state={args.to} by={args.by} at={doc.approved_at}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the agent-facing HTTP capability API."""
    import uvicorn
    from .api import build_app
    app = build_app(
        artifacts_dir=args.artifacts_dir,
        policy_path=args.policy,
        evidence_root=args.evidence_root,
    )
    print(f"[serve] capability API on http://{args.host}:{args.port}/")
    print(f"[serve] GET  /capabilities         list capabilities")
    print(f"[serve] GET  /capabilities/tools   LLM tool-use schemas")
    print(f"[serve] POST /capabilities/{{n}}/{{v}}/{{t}}/invoke   deterministic replay")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    rows = list_artifacts(args.artifacts_dir)
    if not rows:
        print("(no artifacts)")
        return 0
    for r in rows:
        if "error" in r:
            print(f"{r['path']}\tERROR: {r['error']}")
            continue
        base = r.get("based_on")
        base_str = f"  overlay-of={base}" if base else ""
        print(
            f"{r['name']:<24} v{r['version']:<8} target={r['target']:<24} "
            f"kind={r['kind']:<8} outcomes={','.join(r['outcomes'])}{base_str}  [{r['path']}]"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("cua")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--policy", default="policy.yaml")
    p.add_argument("--evidence-root", default="evidence")
    p.add_argument("--artifacts-dir", default="artifacts", help="Where to look for --tenant shorthand.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("discover")
    pd.add_argument("--goal", required=True)
    pd.add_argument("--target", required=True, help="target base URL, e.g. http://127.0.0.1:5000")
    pd.add_argument("--out", required=True, help="artifact JSON output path")
    pd.add_argument("--mock-llm", action="store_true")
    pd.add_argument("--member-id", default=None, help="override extracted member id")
    pd.set_defaults(func=cmd_discover)

    def _add_replay_flags(parser):
        parser.add_argument("--artifact", default=None, help="path to artifact or overlay")
        parser.add_argument("--tenant", default=None, help="shorthand: pick artifact by tenant target name")
        parser.add_argument("--capability", default=None, help="disambiguates --tenant when multiple capabilities exist for that tenant")
        parser.add_argument("--inputs", default="{}", help="JSON inputs, e.g. '{\"member_id\":\"12345\"}'")
        parser.add_argument("--auto-approve-risky", action="store_true")
        parser.add_argument("--unattended", action="store_true", help="fail instead of prompting on escalation")
        parser.add_argument("--operator-port", type=int, default=None, help="start FastAPI operator console on this port")
        parser.add_argument("--headless", action="store_true", help="run browser headless (default = headed)")
        parser.add_argument("--allow-draft", action="store_true", help="run a draft (not yet approved) artifact — audited")

    pr = sub.add_parser("replay")
    _add_replay_flags(pr)
    pr.add_argument("--run-id", default=None)
    pr.set_defaults(func=cmd_replay)

    ps = sub.add_parser("stability")
    _add_replay_flags(ps)
    ps.add_argument("--n", type=int, default=5, help="number of runs")
    ps.add_argument("--report-out", default=None, help="write JSON report here")
    ps.set_defaults(func=cmd_stability)

    pc = sub.add_parser("catalog")
    pc.set_defaults(func=cmd_catalog)

    pe = sub.add_parser("eval", help="Run a golden replay eval suite (CI-friendly, non-zero on any fail)")
    pe.add_argument("--evals", default="evals/replay.json")
    pe.add_argument("--report-out", default=None)
    pe.set_defaults(func=cmd_eval)

    pap = sub.add_parser("approve", help="Promote a draft artifact to approved (or deprecated)")
    pap.add_argument("--artifact", required=True)
    pap.add_argument("--to", choices=["approved", "draft", "deprecated"], default="approved")
    pap.add_argument("--by", default=os.environ.get("USER") or os.environ.get("USERNAME") or "unknown")
    pap.set_defaults(func=cmd_approve)

    pa = sub.add_parser("serve", help="Start the agent-facing HTTP capability API")
    pa.add_argument("--host", default="127.0.0.1")
    pa.add_argument("--port", type=int, default=8770)
    pa.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    # `replay` requires either --artifact or --tenant; enforce here so
    # `stability` shares the same code path.
    if args.cmd in ("replay", "stability") and not (args.artifact or args.tenant):
        p.error("one of --artifact or --tenant is required")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
