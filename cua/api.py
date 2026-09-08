"""Agent-facing HTTP API — publish saved capabilities so an AI agent can
discover them and invoke them by name with typed args.

Endpoints:
  GET  /health                                     -> liveness probe
  GET  /capabilities                               -> list summaries
  GET  /capabilities/tools                         -> LLM tool-use JSON schemas
                                                      (Anthropic tool_use shape)
  GET  /capabilities/{name}/{version}/{target}     -> full artifact JSON
  POST /capabilities/{name}/{version}/{target}/invoke
       body: {inputs: {...}, auto_approve_risky?: bool, unattended?: bool}
       returns: {outcome, outputs, error, evidence_dir, tier_usage,
                 recoveries_applied, resolved_via_handoff}

This is the "stretch goal #1 (agent-facing capability interface)" from
the brief, made real: the same artifacts a human catalogs at the CLI
appear here as an LLM-callable tool surface with typed args. An agent
that discovers a capability here doesn't need any prior knowledge of
how it was built — just the goal, the identity triple, and the
input_schema.

Two safety notes worth calling out at the boundary:

  1. The `unattended` flag defaults to True on this transport. An HTTP
     caller cannot sit in front of a stdin prompt; if you want a real
     handoff you have to pass `unattended=False` AND start the run with
     an operator_port so the FastAPI operator console is up.
  2. `auto_approve_risky` is *not* something the caller can lie their
     way past — the underlying replay engine still routes risky steps
     through Policy + escalation. This flag only silences the gate for
     runs the operator has already pre-approved (e.g. an admin script).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class InvokeBody(BaseModel):
    """Request body for POST /capabilities/{name}/{version}/{target}/invoke.

    Defined at module scope (NOT inside build_app) because FastAPI+Pydantic
    v2 can't resolve the ForwardRef for a class defined inside a function —
    it raises `PydanticUserError: TypeAdapter is not fully defined` under
    real HTTP requests. Unit tests via TestClient didn't reproduce because
    they don't hit the same body-parsing code path. Found by curl.
    """
    inputs: dict[str, Any] = Field(default_factory=dict)
    auto_approve_risky: bool = False
    unattended: bool = True
    operator_port: Optional[int] = None
    headed: bool = False  # HTTP callers default to headless


def build_app(
    *,
    artifacts_dir: str = "artifacts",
    policy_path: str = "policy.yaml",
    evidence_root: str = "evidence",
):
    """Construct the FastAPI app. Lazy imports so pytest / offline runs
    that don't touch the API don't pay the FastAPI import cost."""
    from fastapi import Body, FastAPI, HTTPException

    from .artifact.registry import list_artifacts
    from .artifact.store import load_artifact
    from .replay.engine import replay_artifact
    from .safety.policy import Policy

    app = FastAPI(
        title="CUA Capability API",
        description=(
            "Agent-facing HTTP surface for saved computer-use capabilities. "
            "Backing store is filesystem artifacts under `artifacts/`."
        ),
        version="1.0.0",
    )

    def _find_path(name: str, version: str, target: str) -> str:
        rows = list_artifacts(artifacts_dir)
        for r in rows:
            if r.get("name") == name and r.get("version") == version and r.get("target") == target:
                return r["path"]
        raise HTTPException(404, f"no capability {name}@{version}/{target}")

    @app.get("/health")
    def health():
        return {"ok": True, "artifacts_dir": artifacts_dir}

    @app.get("/capabilities")
    def list_capabilities():
        return {"capabilities": list_artifacts(artifacts_dir)}

    @app.get("/capabilities/tools")
    def list_tools():
        """Anthropic tool_use-shaped schemas so an LLM catalog is one call.

        Each entry is a `{name, description, input_schema}` object matching
        the Anthropic Messages tool_use format. The tool name embeds the
        identity triple so the caller can address a specific tenant."""
        tools = []
        for row in list_artifacts(artifacts_dir):
            if "error" in row:
                continue
            try:
                a = load_artifact(row["path"])
            except Exception:
                continue
            tool_name = f"{a.id.name}__{a.id.version.replace('.', '_')}__{a.id.target}"
            desc_lines = [
                a.description,
                "",
                f"Declared outcomes: {', '.join(o.code for o in a.outcomes)}.",
                f"Side effects: {', '.join(f'{s.kind}(reversible={s.reversible})' for s in a.side_effects) or 'none'}.",
                "This capability replays deterministically; no LLM is invoked in the loop.",
            ]
            tools.append(
                {
                    "name": tool_name,
                    "description": "\n".join(desc_lines),
                    "input_schema": a.input_schema,
                    "output_schema": a.output_schema,
                    "identity": a.id.model_dump(),
                }
            )
        return {"tools": tools}

    @app.get("/capabilities/{name}/{version}/{target}")
    def get_capability(name: str, version: str, target: str):
        path = _find_path(name, version, target)
        artifact = load_artifact(path)
        return artifact.model_dump(mode="json", by_alias=True)

    @app.post("/capabilities/{name}/{version}/{target}/invoke")
    def invoke(name: str, version: str, target: str, body: InvokeBody = Body(...)):
        # NOTE: The explicit `Body(...)` marker is load-bearing. Without it
        # FastAPI's auto-body-inference gets confused by `dict[str, Any]`
        # fields on the Pydantic model and starts routing the whole body as
        # a query parameter (422 "Field required at loc=['query','body']").
        # The TestClient doesn't hit this because it serialises through a
        # different code path — this bug only shows up under real HTTP.
        # Found by curl-ing the live endpoint after unit tests passed.
        path = _find_path(name, version, target)
        artifact = load_artifact(path)
        policy = Policy.from_yaml(policy_path)
        result = replay_artifact(
            artifact,
            body.inputs,
            policy=policy,
            evidence_root=evidence_root,
            auto_approve_risky=body.auto_approve_risky,
            unattended=body.unattended,
            operator_port=body.operator_port,
            headed=body.headed,
        )
        payload = {
            "artifact": os.path.basename(path),
            "outcome": result.outcome,
            "outputs": result.outputs,
            "error": result.error,
            "evidence_dir": result.evidence_dir,
            "resolved_via_handoff": result.resolved_via_handoff,
            "tier_usage": result.tier_usage,
            "recoveries_applied": result.recoveries_applied,
        }
        # HTTP status mirrors outcome shape so callers can dispatch by code
        # without unwrapping the body: 200 = success/business outcome,
        # 409 = escalated-unresolved, 500 = hard failure.
        if result.outcome is None:
            raise HTTPException(500, payload)
        if result.outcome == "ESCALATED_UNRESOLVED":
            raise HTTPException(409, payload)
        return payload

    return app


def main():  # pragma: no cover — CLI entrypoint
    import argparse
    import uvicorn

    p = argparse.ArgumentParser("cua.api")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--artifacts-dir", default="artifacts")
    p.add_argument("--policy", default="policy.yaml")
    p.add_argument("--evidence-root", default="evidence")
    args = p.parse_args()
    app = build_app(
        artifacts_dir=args.artifacts_dir,
        policy_path=args.policy,
        evidence_root=args.evidence_root,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
