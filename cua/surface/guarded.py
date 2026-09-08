"""GuardedSurface — wraps a Surface and enforces ownership structurally.

Every act call goes through `controller.assert_agent()`. If the session is
in `paused` or `human` state, the call raises OwnershipError instead of
silently reaching the browser. Perception (snapshot) is always allowed so
the operator console can display state.

This is a *structural* enforcement of the "only the current owner acts"
invariant rather than a documented convention. It's the seam every
"take-control-of-the-live-session" story needs.
"""
from __future__ import annotations

from typing import Optional

from ..escalation.controller import SessionController
from .base import Surface
from .types import Action, ExecutionResult, Snapshot


class GuardedSurface(Surface):
    def __init__(self, inner: Surface, controller: SessionController):
        self._inner = inner
        self._controller = controller

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._inner.start()

    def stop(self) -> None:
        self._inner.stop()

    # -- perception is always allowed ------------------------------------
    def snapshot(self, screenshot: bool = False) -> Snapshot:  # type: ignore[override]
        # WebSurface's snapshot takes a kwarg; forward it if supported.
        try:
            return self._inner.snapshot(screenshot=screenshot)  # type: ignore[call-arg]
        except TypeError:
            return self._inner.snapshot()

    # -- action requires ownership ---------------------------------------
    def execute(self, action: Action) -> ExecutionResult:
        self._controller.assert_agent()
        return self._inner.execute(action)

    # -- passthroughs for WebSurface-specific helpers --------------------
    def screenshot(self, path: str) -> str:
        return self._inner.screenshot(path)  # type: ignore[attr-defined]

    @property
    def page(self):  # type: ignore[override]
        return self._inner.page  # type: ignore[attr-defined]

    @property
    def controller(self) -> SessionController:
        return self._controller

    @property
    def inner(self) -> Surface:
        return self._inner
