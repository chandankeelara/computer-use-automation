"""Pydantic v2 models for the capability artifact.

Identity = (name, version, target). That triple lets the registry hold
multiple concrete implementations of the same *capability* across tenants —
Midwest Federal Servicing Console vs. some other bank's console can both
publish `open_sub_account.v1.0.0` and callers pick by target.
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
    notes: str = ""


class Checkpoint(BaseModel):
    kind: Literal["url_pattern", "text_present"]
    pattern: Optional[str] = None
    text: Optional[str] = None


class Provenance(BaseModel):
    goal: str
    model: str
    recorded_at: str
    run_id: str
    target_url: str


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
    provenance: Provenance
