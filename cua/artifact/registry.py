"""Catalog: list capabilities by (name, target). Overlays surface as
distinct rows that reference their base."""
from __future__ import annotations

import glob
import os

from .schema import is_overlay
from .store import load_artifact, load_document


def list_artifacts(artifacts_dir: str = "artifacts") -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(artifacts_dir, "*.json"))):
        try:
            doc = load_document(path)
        except Exception as e:
            out.append({"path": path, "error": str(e)})
            continue

        if is_overlay(load_raw(path)):
            # Load with overlay resolution so we can report the merged view.
            try:
                merged = load_artifact(path)
            except Exception as e:
                out.append({"path": path, "kind": "overlay", "error": str(e)})
                continue
            out.append(
                {
                    "path": path,
                    "kind": "overlay",
                    "name": merged.id.name,
                    "version": merged.id.version,
                    "target": merged.id.target,
                    "title": merged.title,
                    "outcomes": [o.code for o in merged.outcomes],
                    "based_on": f"{doc.based_on.name}@{doc.based_on.version}/{doc.based_on.target}",  # type: ignore
                }
            )
        else:
            a = doc
            out.append(
                {
                    "path": path,
                    "kind": "artifact",
                    "name": a.id.name,
                    "version": a.id.version,
                    "target": a.id.target,
                    "title": a.title,
                    "outcomes": [o.code for o in a.outcomes],
                }
            )
    return out


def load_raw(path: str) -> dict:
    import json
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
