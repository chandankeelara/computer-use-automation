"""CLI entry point: `python -m cua.cli {discover,replay,catalog}`.

argparse-based (typer optional) so we don't pull an extra dep just for the
CLI. Keeps the demo self-contained.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

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
    # Parse member_id out of goal — the mock only cares about the id supplied.
    # We accept it via --inputs OR from the goal string; keep it simple.
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
    print(f"[discover] steps: {len(artifact.steps)}, outcomes: {[o.code for o in artifact.outcomes]}")
    return 0


def _extract_member_id_from_goal(goal: str) -> str | None:
    import re
    m = re.search(r"\b(\d{4,})\b", goal)
    return m.group(1) if m else None


def cmd_replay(args: argparse.Namespace) -> int:
    policy = Policy.from_yaml(args.policy)
    artifact = load_artifact(args.artifact)
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
    )
    print(json.dumps(
        {
            "outcome": result.outcome,
            "outputs": result.outputs,
            "error": result.error,
            "evidence_dir": result.evidence_dir,
            "resolved_via_handoff": result.resolved_via_handoff,
        },
        indent=2,
    ))
    if result.outcome is None:
        return 1
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
        print(
            f"{r['name']:<24} v{r['version']:<8} target={r['target']:<20} "
            f"outcomes={','.join(r['outcomes'])}  [{r['path']}]"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("cua")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--policy", default="policy.yaml")
    p.add_argument("--evidence-root", default="evidence")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("discover")
    pd.add_argument("--goal", required=True)
    pd.add_argument("--target", required=True, help="target base URL, e.g. http://127.0.0.1:5000")
    pd.add_argument("--out", required=True, help="artifact JSON output path")
    pd.add_argument("--mock-llm", action="store_true")
    pd.add_argument("--member-id", default=None, help="override extracted member id")
    pd.set_defaults(func=cmd_discover)

    pr = sub.add_parser("replay")
    pr.add_argument("--artifact", required=True)
    pr.add_argument("--inputs", default="{}", help="JSON inputs, e.g. '{\"member_id\":\"12345\"}'")
    pr.add_argument("--auto-approve-risky", action="store_true")
    pr.add_argument("--unattended", action="store_true", help="fail instead of prompting on escalation")
    pr.set_defaults(func=cmd_replay)

    pc = sub.add_parser("catalog")
    pc.add_argument("--artifacts-dir", default="artifacts")
    pc.set_defaults(func=cmd_catalog)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
