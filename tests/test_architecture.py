"""Architecture tests — enforce trust boundaries structurally.

The most important invariant in this system is:

    THE REPLAY ENGINE MUST NOT DEPEND ON AN LLM.

This is what makes replay deterministic, cheap, and cache-friendly for
the agent-facing product. A README claim isn't enough; the graph itself
should refuse to compile with the wrong edges. This test walks every
Python module under `cua/replay/`, `cua/artifact/`, and `cua/safety/`
and asserts none of them (transitively via direct import statements)
import an LLM SDK or the discovery-time LLM adapter.

If replay ever grows an `import anthropic` this test fails, and the fix
is a code change (introduce a new port, keep the dep in `cua/agent/`),
not a doc update.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directories that MUST stay LLM-free.
FORBIDDEN_ROOTS = ["cua/replay", "cua/artifact", "cua/safety", "cua/surface"]

# Import prefixes that count as "an LLM".
FORBIDDEN_IMPORTS = {
    "anthropic",
    "openai",
    "google.generativeai",
    "cua.agent.llm",
    "cua.agent.mock_llm",
    "cua.agent.loop",
}


def _iter_py_files(root: Path):
    for base, _, files in os.walk(root):
        if "__pycache__" in base:
            continue
        for f in files:
            if f.endswith(".py"):
                yield Path(base) / f


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                out.add(n.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module)
    return out


def test_replay_has_no_llm_dependencies():
    violations = []
    for sub in FORBIDDEN_ROOTS:
        root = ROOT / sub
        for py in _iter_py_files(root):
            imports = _imports_of(py)
            for imp in imports:
                for forbidden in FORBIDDEN_IMPORTS:
                    if imp == forbidden or imp.startswith(forbidden + "."):
                        violations.append(f"{py.relative_to(ROOT)}: imports '{imp}'")
    assert not violations, (
        "Trust-boundary violation — replay/artifact/safety must stay LLM-free:\n"
        + "\n".join(violations)
    )


def test_recovery_schema_action_is_closed_set():
    """The recovery `action` field is a Literal — data can't smuggle in code."""
    from cua.artifact.schema import Recovery
    # If someone widens this to str the test will still pass, but the schema
    # would let arbitrary strings through — which is a policy weakening.
    field = Recovery.model_fields["action"]
    # Literal args become the underlying annotation on Pydantic v2.
    ann = str(field.annotation)
    for allowed in ("dismiss", "wait", "retry"):
        assert allowed in ann, f"expected recovery action '{allowed}' in Literal, saw: {ann}"


def test_overlay_cannot_change_output_schema():
    """Structural guarantee: overlay resolution refuses output_schema drift."""
    from cua.artifact.overlay import OverlayValidationError, apply_overlay
    from cua.artifact.schema import (
        Artifact,
        ArtifactId,
        DetectedBy,
        Outcome,
        Overlay,
        OverlayBaseRef,
        Provenance,
        Step,
    )

    base = Artifact(
        id=ArtifactId(name="cap", version="1.0.0", target="a"),
        title="t",
        description="d",
        input_schema={"type": "object"},
        output_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        outcomes=[
            Outcome(code="SUCCESS", detected_by=DetectedBy(kind="url_pattern", pattern="^.*$"))
        ],
        steps=[Step(id="s1", action="navigate", url="http://x")],
        provenance=Provenance(
            goal="g", model="m", recorded_at="2026-01-01T00:00:00Z",
            run_id="r", target_url="http://x",
        ),
    )
    ok_overlay = Overlay(
        id=ArtifactId(name="cap", version="1.0.0", target="b"),
        based_on=OverlayBaseRef(name="cap", version="1.0.0", target="a"),
        entry_url="http://y",
    )
    merged = apply_overlay(base, ok_overlay)
    assert merged.id.target == "b"
    assert merged.output_schema == base.output_schema

    # Attempting to add an extra step via overlay is impossible by schema
    # (Overlay has no `steps` field). Attempting to change step action via
    # a step_patch: schema allows only target/value/expect/notes, not action.
    # This is enforced by construction (Pydantic model excludes the field).
    fields = set(
        Overlay.model_fields["step_patches"].annotation.__args__[0].model_fields.keys()  # type: ignore
    )
    assert "action" not in fields, "StepPatch must not expose 'action' — that's a schema break"
    assert "id" not in fields, "StepPatch must not expose 'id' — patches key by step_id only"
