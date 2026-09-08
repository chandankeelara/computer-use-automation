"""Structured evidence: run.json + steps.jsonl + screenshots + ax dumps."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional


class EvidenceLogger:
    def __init__(self, evidence_root: str, run_id: str):
        self.run_id = run_id
        self.dir = os.path.join(evidence_root, run_id)
        os.makedirs(os.path.join(self.dir, "screenshots"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "ax"), exist_ok=True)
        self.steps_path = os.path.join(self.dir, "steps.jsonl")
        self.manifest_path = os.path.join(self.dir, "run.json")
        self._step_n = 0
        self._t0 = time.time()

    def start_manifest(self, kind: str, meta: dict[str, Any]) -> None:
        manifest = {
            "run_id": self.run_id,
            "kind": kind,
            "started_at": time.time(),
            **meta,
        }
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

    def finalize(self, result: dict[str, Any]) -> None:
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            m = {"run_id": self.run_id}
        m["ended_at"] = time.time()
        m["duration_s"] = round(m["ended_at"] - self._t0, 3)
        m["result"] = result
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=2)

    def next_step(self) -> int:
        self._step_n += 1
        return self._step_n

    def screenshot_path(self, n: int) -> str:
        return os.path.join(self.dir, "screenshots", f"step_{n}.png")

    def ax_path(self, n: int) -> str:
        return os.path.join(self.dir, "ax", f"step_{n}.json")

    def dump_ax(self, n: int, ax: dict) -> str:
        p = self.ax_path(n)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(ax, f, indent=2)
        return p

    def log_step(self, entry: dict[str, Any]) -> None:
        with open(self.steps_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def write_intervention_request(self, payload: dict[str, Any]) -> str:
        p = os.path.join(self.dir, "intervention_request.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return p
