"""Catalog: list capabilities by (name, target)."""
from __future__ import annotations

import glob
import os

from .store import load_artifact


def list_artifacts(artifacts_dir: str = "artifacts") -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(artifacts_dir, "*.json"))):
        try:
            a = load_artifact(path)
        except Exception as e:
            out.append({"path": path, "error": str(e)})
            continue
        out.append(
            {
                "path": path,
                "name": a.id.name,
                "version": a.id.version,
                "target": a.id.target,
                "title": a.title,
                "outcomes": [o.code for o in a.outcomes],
            }
        )
    return out
