"""Surface interface.

A Surface is anything an agent or replay engine can perceive and act on.
The web implementation lives in cua.surface.web. Stubs / seams for
DesktopSurface (UIA) and VisionSurface (screenshot-only) would slot in as
sibling modules — they'd implement the same two methods.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .types import Action, ExecutionResult, Snapshot


class Surface(ABC):
    @abstractmethod
    def snapshot(self) -> Snapshot:
        """Return current perception (URL/title + accessibility tree)."""

    @abstractmethod
    def execute(self, action: Action) -> ExecutionResult:
        """Execute a single action, resolving locators as needed."""

    # --- lifecycle -------------------------------------------------------
    def start(self) -> None:  # optional override
        pass

    def stop(self) -> None:  # optional override
        pass


# ---------------------------------------------------------------------------
# Seams (documented, not implemented):
#
# class DesktopSurface(Surface):
#     """Windows UIA / macOS AX-based surface. Same contract:
#        snapshot() walks the UIA tree, execute() maps click/type to UIA invoke
#        patterns. Locator strategy would gain an {by: "automation_id"} option.
#     """
#
# class VisionSurface(Surface):
#     """Screenshot-only surface for apps with no accessibility tree.
#        snapshot() returns AX = {} + screenshot_path. Locator resolution
#        would need OCR + template match. Locators here MUST still be anchored
#        to stable descriptors (label text, image hash) — never raw pixels.
#     """
# ---------------------------------------------------------------------------
