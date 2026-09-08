"""Load / save artifacts as pretty JSON."""
from __future__ import annotations

import json
import os

from .schema import Artifact


def save_artifact(artifact: Artifact, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = artifact.model_dump(mode="json", by_alias=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_artifact(path: str) -> Artifact:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return Artifact.model_validate(data)
