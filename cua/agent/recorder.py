"""Turn a stream of agent tool-calls into an Artifact.

The recorder is the layer that turns "what happened" into "what to replay":
  - It parameterizes literal input values back into {{input_name}} refs.
  - It infers input_schema (with sensitive markers) from a caller-provided
    input spec.
  - It builds outcomes / extracts from mark_output + finish calls.
  - It labels risky steps and rolls those up into side_effects.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional

from ..artifact.schema import (
    Artifact,
    ArtifactId,
    Checkpoint,
    DetectedBy,
    ExtractSpec,
    LocatorStrategy,
    Outcome,
    Provenance,
    SideEffect,
    Step,
)


class Recorder:
    def __init__(
        self,
        *,
        goal: str,
        target_url: str,
        target_name: str,
        capability_name: str,
        version: str,
        run_id: str,
        model: str,
        input_spec: dict[str, Any],
        output_spec: dict[str, Any],
    ):
        self._goal = goal
        self._target_url = target_url
        self._target_name = target_name
        self._name = capability_name
        self._version = version
        self._run_id = run_id
        self._model = model
        self._input_spec = input_spec
        self._output_spec = output_spec
        self._steps: list[Step] = []
        self._extracts: list[ExtractSpec] = []
        self._outcome_code = "SUCCESS"
        self._checkpoint_pattern: Optional[str] = None
        self._had_risky = False
        self._sn = 0

    def _next_id(self) -> str:
        self._sn += 1
        return f"s{self._sn}"

    def _parameterize(self, value: str) -> str:
        """Replace literal input values with {{name}} placeholders."""
        out = value
        for name, spec in (self._input_spec.get("properties") or {}).items():
            literal = spec.get("example")
            if literal and isinstance(literal, str) and literal in out:
                out = out.replace(literal, "{{" + name + "}}")
        return out

    # -- event handlers -------------------------------------------------
    def on_navigate(self, url: str) -> None:
        self._steps.append(
            Step(
                id=self._next_id(),
                action="navigate",
                url=url,
                risk="safe",
                notes="Load initial page.",
            )
        )

    def on_click(self, primary: dict, fallbacks: list[dict], notes: str, risk: str) -> None:
        if risk == "risky":
            self._had_risky = True
        self._steps.append(
            Step(
                id=self._next_id(),
                action="click",
                target=LocatorStrategy(primary=primary, fallbacks=fallbacks),
                risk=risk,  # type: ignore[arg-type]
                notes=notes,
            )
        )

    def on_type(self, primary: dict, fallbacks: list[dict], value: str, notes: str) -> None:
        self._steps.append(
            Step(
                id=self._next_id(),
                action="type",
                target=LocatorStrategy(primary=primary, fallbacks=fallbacks),
                value=self._parameterize(value),
                risk="safe",
                notes=notes,
            )
        )

    def on_mark_output(self, name: str, from_: str, regex: Optional[str]) -> None:
        self._extracts.append(
            ExtractSpec.model_validate(
                {"name": name, "from": from_, "regex": regex}
            )
        )

    def on_finish(self, outcome_code: str, checkpoint_url_pattern: Optional[str]) -> None:
        self._outcome_code = outcome_code
        self._checkpoint_pattern = checkpoint_url_pattern

    # -- build ----------------------------------------------------------
    def build(self) -> Artifact:
        outcomes = [
            Outcome(
                code=self._outcome_code,
                terminal=True,
                detected_by=DetectedBy(kind="url_pattern", pattern=self._checkpoint_pattern),
                extracts=self._extracts,
            ),
            # A declared business outcome — hand-added because discovery only
            # walked the success path. In a real system this would also be
            # discovered (via a second run against a non-existent member) or
            # authored by a domain SME.
            Outcome(
                code="MEMBER_NOT_FOUND",
                terminal=True,
                detected_by=DetectedBy(
                    kind="text_present", text='No member found'
                ),
            ),
        ]
        side_effects: list[SideEffect] = []
        if self._had_risky:
            side_effects.append(
                SideEffect(kind="creates_sub_account", reversible=False)
            )
        checkpoint = (
            Checkpoint(kind="url_pattern", pattern=self._checkpoint_pattern)
            if self._checkpoint_pattern
            else None
        )
        return Artifact(
            id=ArtifactId(name=self._name, version=self._version, target=self._target_name),
            title="Open sub-account for a member",
            description=(
                "Look up a member by ID and open a new sub-account. "
                "Returns the new sub-account number."
            ),
            input_schema=self._input_spec,
            output_schema=self._output_spec,
            outcomes=outcomes,
            side_effects=side_effects,
            steps=self._steps,
            checkpoint=checkpoint,
            provenance=Provenance(
                goal=self._goal,
                model=self._model,
                recorded_at=dt.datetime.utcnow().isoformat() + "Z",
                run_id=self._run_id,
                target_url=self._target_url,
            ),
        )
