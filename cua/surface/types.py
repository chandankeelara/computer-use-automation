"""Runtime types shared across surface / agent / replay layers.

These are dataclasses (not Pydantic) because they cross the hot path many
times per run and we don't need JSON (de)serialization semantics here — the
serialization contract lives in cua.artifact.schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

ActionKind = Literal["navigate", "click", "type", "press", "read", "assert"]


@dataclass
class Locator:
    """Locator strategy: primary + ordered fallbacks.

    A locator is not a single selector. It is a resolution plan: try primary,
    then walk fallbacks. Only when all miss do we escalate.
    """
    primary: dict[str, Any]
    fallbacks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Action:
    kind: ActionKind
    target: Optional[Locator] = None
    value: Optional[str] = None  # may contain {{param}} placeholders at artifact level
    # Only for navigate: URL to open
    url: Optional[str] = None


@dataclass
class Snapshot:
    """Serialized accessibility snapshot + URL + title."""
    url: str
    title: str
    ax: dict[str, Any]  # nested {role, name, value, children}
    # Optional pixel screenshot path (for evidence / vision fallback)
    screenshot_path: Optional[str] = None


@dataclass
class ExecutionResult:
    ok: bool
    detail: str = ""
    read_value: Optional[str] = None
    # Which fallback tier resolved (0=primary, 1..=fallbacks, -1=none/N/A)
    resolved_tier: int = -1
    # Per-tier verdicts collected during resolution. Elements look like
    # {"tier": 0, "by": "role_name", "verdict": "ok|miss|ambiguous", "count": N}.
    # Load-bearing for evidence: "primary matched 3 things so we rejected
    # it and used the CSS fallback" is very different from "primary missed."
    tier_verdicts: list[dict] = field(default_factory=list)
