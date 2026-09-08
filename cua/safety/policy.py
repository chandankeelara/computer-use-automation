"""Policy layer.

Every action passes through Policy.check(). Denied stops the run.
RequiresApproval blocks unless caller passes auto_approve=True.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union
from urllib.parse import urlparse

import yaml


@dataclass
class Allowed:
    reason: str = "ok"


@dataclass
class Denied:
    reason: str


@dataclass
class RequiresApproval:
    reason: str


PolicyDecision = Union[Allowed, Denied, RequiresApproval]


class Policy:
    def __init__(self, allowed_domains, allowed_actions, risky_gate: bool):
        self.allowed_domains = set(allowed_domains)
        self.allowed_actions = set(allowed_actions)
        self.risky_gate = risky_gate

    @classmethod
    def from_yaml(cls, path: str) -> "Policy":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(
            allowed_domains=data.get("allowed_domains", []),
            allowed_actions=data.get("allowed_actions", []),
            risky_gate=bool(data.get("risky_actions_require_approval", True)),
        )

    def check(
        self,
        action_kind: str,
        *,
        url: Optional[str] = None,
        risk: str = "safe",
    ) -> PolicyDecision:
        if action_kind not in self.allowed_actions:
            return Denied(f"action '{action_kind}' not in allowed_actions")
        if action_kind == "navigate" and url:
            host = urlparse(url).netloc
            if host not in self.allowed_domains:
                return Denied(f"domain '{host}' not in allowed_domains")
        if self.risky_gate and risk == "risky":
            return RequiresApproval(f"{action_kind} is risky")
        return Allowed()
