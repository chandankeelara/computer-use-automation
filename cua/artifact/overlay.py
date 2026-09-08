"""Overlay resolver: base Artifact + Overlay -> concrete Artifact.

Rules (enforced here, not documented):
  1. Overlays can patch a step's target / value / expect / notes, but NOT
     the step's `action` or `id`. Attempting either raises.
  2. Overlays cannot change the base's `output_schema`. Callers depend on
     the shape.
  3. Overlays cannot change the number of steps or their order. Adding /
     removing steps is a version bump on the base.
  4. Overlays MAY: change entry URL, add outcomes, add recoveries, add
     allowed domains.

These rules matter because the identity triple `(name, version, target)`
is a promise: a caller who asked for `open_sub_account.v1.0.0` on tenant
X gets a shape-compatible artifact. If overlays could rewrite the output
schema, that promise breaks silently.
"""
from __future__ import annotations

import copy
from typing import Any

from .schema import Artifact, ArtifactId, Overlay, Provenance


class OverlayValidationError(RuntimeError):
    pass


def apply_overlay(base: Artifact, overlay: Overlay) -> Artifact:
    if overlay.based_on.name != base.id.name or overlay.based_on.version != base.id.version:
        raise OverlayValidationError(
            f"overlay based_on={overlay.based_on.name}@{overlay.based_on.version} "
            f"does not match base={base.id.name}@{base.id.version}"
        )
    # Start from a deep copy so we never mutate the cached base.
    data: dict[str, Any] = base.model_dump(mode="python", by_alias=True)

    # Identity flips to the overlay's tenant target — same name+version.
    data["id"] = {
        "name": base.id.name,
        "version": base.id.version,
        "target": overlay.id.target,
    }
    if overlay.title:
        data["title"] = overlay.title
    if overlay.description:
        data["description"] = overlay.description

    # Entry URL rewrite: replace the URL of the first navigate step.
    if overlay.entry_url:
        for step in data["steps"]:
            if step["action"] == "navigate":
                step["url"] = overlay.entry_url
                break

    # Apply step patches — validate id + action untouched.
    by_id = {s["id"]: s for s in data["steps"]}
    for p in overlay.step_patches:
        if p.step_id not in by_id:
            raise OverlayValidationError(f"step_patch references unknown step_id={p.step_id}")
        step = by_id[p.step_id]
        # Only whitelisted fields may change.
        if p.target is not None:
            step["target"] = p.target.model_dump(mode="python")
        if p.value is not None:
            step["value"] = p.value
        if p.expect is not None:
            step["expect"] = p.expect.model_dump(mode="python")
        if p.notes is not None:
            step["notes"] = f"[overlay] {p.notes}"

    # Extras append (do not replace).
    for o in overlay.extra_outcomes:
        data["outcomes"].append(o.model_dump(mode="python"))
    for r in overlay.extra_recoveries:
        data["recoveries"].append(r.model_dump(mode="python"))

    # Provenance: mark that this came from an overlay.
    data["provenance"] = {
        **data["provenance"],
        "goal": data["provenance"]["goal"] + f" [overlay:{overlay.id.target}]",
        "run_id": data["provenance"]["run_id"] + f"__overlay_{overlay.id.target}",
    }

    resolved = Artifact.model_validate(data)

    # Post-conditions the schema alone can't express.
    if len(resolved.steps) != len(base.steps):
        raise OverlayValidationError("step count changed — not allowed via overlay")
    if resolved.output_schema != base.output_schema:
        raise OverlayValidationError("output_schema changed — not allowed via overlay")
    for a, b in zip(resolved.steps, base.steps):
        if a.id != b.id or a.action != b.action:
            raise OverlayValidationError(
                f"step {b.id}: id/action changed under overlay (not allowed)"
            )
    return resolved
