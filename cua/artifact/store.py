"""Load / save artifacts + overlays as pretty JSON."""
from __future__ import annotations

import json
import os
from typing import Union

from .overlay import apply_overlay
from .schema import Artifact, Overlay, is_overlay


def save_artifact(artifact: Artifact, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = artifact.model_dump(mode="json", by_alias=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def save_overlay(overlay: Overlay, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = overlay.model_dump(mode="json", by_alias=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_document(path: str) -> Union[Artifact, Overlay]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if is_overlay(data):
        return Overlay.model_validate(data)
    return Artifact.model_validate(data)


def load_artifact(path: str, *, base_path: str | None = None) -> Artifact:
    """Load an artifact or overlay+base pair.

    If `path` is an overlay, `base_path` must be provided (or resolvable
    from a sibling file matching `<name>.v<version>.<based_on.target>.json`
    in the same directory).
    """
    doc = load_document(path)
    if isinstance(doc, Artifact):
        return doc
    overlay: Overlay = doc
    if base_path is None:
        base_path = _find_base(path, overlay)
    base = load_document(base_path)
    if not isinstance(base, Artifact):
        raise RuntimeError(f"base file {base_path} is not an Artifact")
    return apply_overlay(base, overlay)


def _find_base(overlay_path: str, overlay: Overlay) -> str:
    directory = os.path.dirname(overlay_path) or "."
    candidate = os.path.join(
        directory,
        f"{overlay.based_on.name}.v{overlay.based_on.version}.{overlay.based_on.target}.json",
    )
    if not os.path.exists(candidate):
        raise FileNotFoundError(
            f"could not find base artifact for overlay {overlay_path} at {candidate}"
        )
    return candidate
