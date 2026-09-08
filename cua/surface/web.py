"""WebSurface — Playwright-backed Surface implementation.

Perception:
  Primary  = accessibility.snapshot() (serialized role/name/value tree)
  Fallback = screenshot (for vision fallback path; caller decides whether to
             invoke the LLM). This class does NOT store pixel coordinates in
             any artifact — it only returns paths and DOM handles.

Action:
  Locators are resolved primary-first then fallbacks. We deliberately
  restrict to a small, stable set of resolution kinds so artifacts stay
  portable.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
    TimeoutError as PWTimeout,
)

from .base import Surface
from .types import Action, ExecutionResult, Locator, Snapshot

log = logging.getLogger(__name__)


class WebSurface(Surface):
    def __init__(self, headed: bool = True, evidence_dir: Optional[str] = None):
        self._headed = headed
        self._evidence_dir = evidence_dir
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._step_counter = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not self._headed)
        self._ctx = self._browser.new_context()
        self._page = self._ctx.new_page()

    def stop(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    @property
    def page(self) -> Page:
        assert self._page is not None, "call start() first"
        return self._page

    # -- perception ------------------------------------------------------
    def snapshot(self, screenshot: bool = False) -> Snapshot:
        page = self.page
        # Playwright dropped page.accessibility in recent versions. Build a
        # serialized AX-like tree from the DOM via a small in-page JS walk.
        # Same shape: {role, name, value, children}. This is a *perception*
        # layer, not a locator — the artifact still resolves against the DOM.
        ax = page.evaluate(_AX_WALKER_JS) or {}
        shot_path = None
        if screenshot and self._evidence_dir:
            self._step_counter += 1
            shot_path = os.path.join(
                self._evidence_dir, "screenshots", f"snap_{self._step_counter}.png"
            )
            os.makedirs(os.path.dirname(shot_path), exist_ok=True)
            page.screenshot(path=shot_path, full_page=True)
        return Snapshot(url=page.url, title=page.title(), ax=ax, screenshot_path=shot_path)

    def screenshot(self, path: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.page.screenshot(path=path, full_page=True)
        return path

    # -- action ----------------------------------------------------------
    def execute(self, action: Action) -> ExecutionResult:
        try:
            if action.kind == "navigate":
                if not action.url:
                    return ExecutionResult(False, "navigate: no url")
                self.page.goto(action.url, wait_until="domcontentloaded")
                return ExecutionResult(True, f"navigated to {action.url}")

            if action.kind == "press":
                self.page.keyboard.press(action.value or "Enter")
                return ExecutionResult(True, f"pressed {action.value}")

            if action.kind in ("click", "type", "read", "assert"):
                if action.target is None:
                    return ExecutionResult(False, f"{action.kind}: no target locator")
                handle, tier, verdicts = self._resolve(action.target)
                if handle is None:
                    return ExecutionResult(
                        False,
                        f"locator all-miss (verdicts={verdicts})",
                        resolved_tier=-1,
                        tier_verdicts=verdicts,
                    )

                if action.kind == "click":
                    handle.click()
                    self.page.wait_for_load_state("domcontentloaded")
                    return ExecutionResult(True, "clicked", resolved_tier=tier, tier_verdicts=verdicts)
                if action.kind == "type":
                    handle.fill(action.value or "")
                    return ExecutionResult(
                        True, f"typed len={len(action.value or '')}",
                        resolved_tier=tier, tier_verdicts=verdicts,
                    )
                if action.kind == "read":
                    txt = handle.inner_text().strip()
                    return ExecutionResult(
                        True, "read", read_value=txt,
                        resolved_tier=tier, tier_verdicts=verdicts,
                    )
                if action.kind == "assert":
                    if handle.is_visible():
                        return ExecutionResult(
                            True, "asserted visible",
                            resolved_tier=tier, tier_verdicts=verdicts,
                        )
                    return ExecutionResult(
                        False, "assert: not visible",
                        resolved_tier=tier, tier_verdicts=verdicts,
                    )

            return ExecutionResult(False, f"unknown action {action.kind}")
        except PWTimeout as e:
            return ExecutionResult(False, f"timeout: {e}")
        except Exception as e:
            log.exception("execute error")
            return ExecutionResult(False, f"exception: {e}")

    # -- locator resolution ---------------------------------------------
    # Locator strategies where >1 match is treated as AMBIGUOUS (=miss),
    # not "first match wins". Anchoring/semantic strategies must uniquely
    # identify a control — otherwise we may be about to click the wrong
    # thing. Concrete failure mode this prevents: a positional CSS
    # matches an unexpected interstitial's "Continue Anyway" button and
    # the automation silently clicks it thinking it was "Confirm."
    _AMBIGUITY_STRICT = {"role_name", "label_relative"}

    def _resolve(self, loc: Locator):
        """Return (Locator handle, tier, per-tier verdicts).

        Tier 0 = primary. `verdicts` is a list of dicts capturing what the
        resolver observed at each tier (used for evidence / drift signal
        and to make debugging "why did fallback fire" cheap)."""
        tiers = [loc.primary] + (loc.fallbacks or [])
        verdicts: list[dict[str, Any]] = []
        for i, spec in enumerate(tiers):
            handle, verdict = self._resolve_one(spec)
            verdicts.append({"tier": i, **verdict})
            if handle is not None:
                return handle, i, verdicts
        return None, -1, verdicts

    def _resolve_one(self, spec: dict[str, Any]):
        """Return (handle_or_None, verdict_dict)."""
        by = spec.get("by")
        page = self.page
        strict = by in self._AMBIGUITY_STRICT
        try:
            if by == "role_name":
                role = spec["role"]
                name = spec.get("name")
                loc = page.get_by_role(role, name=name) if name else page.get_by_role(role)
                cnt = loc.count()
                if cnt == 0:
                    return None, {"by": by, "verdict": "miss", "count": 0}
                if cnt > 1 and strict:
                    return None, {"by": by, "verdict": "ambiguous", "count": cnt}
                return loc.first, {"by": by, "verdict": "ok", "count": cnt}
            if by == "text":
                loc = page.get_by_text(spec["text"], exact=spec.get("exact", False))
                cnt = loc.count()
                if cnt == 0:
                    return None, {"by": by, "verdict": "miss", "count": 0}
                return loc.first, {"by": by, "verdict": "ok", "count": cnt}
            if by == "css":
                loc = page.locator(spec["selector"])
                cnt = loc.count()
                if cnt == 0:
                    return None, {"by": by, "verdict": "miss", "count": 0}
                return loc.first, {"by": by, "verdict": "ok", "count": cnt}
            if by == "label_relative":
                # Anchor to label text, then walk to the next matching
                # element in DOM order.
                anchor = spec["anchor"]
                direction = spec.get("direction", "next_input")
                tag = {
                    "next_input": "input,textarea,select",
                    "next_button": "button,input[type=submit]",
                    "next_cell": "td,th",
                }.get(direction, "input")
                # Find the label cell, then the following sibling containing the tag.
                # Works with the target app's <td>label</td><td><input></td> pattern.
                xp = (
                    f"xpath=//*[normalize-space(text())={_xpath_lit(anchor)}]"
                    f"/following::{tag.split(',')[0]}[1]"
                )
                loc = page.locator(xp)
                cnt = loc.count()
                if cnt == 0:
                    return None, {"by": by, "verdict": "miss", "count": 0}
                if cnt > 1 and strict:
                    return None, {"by": by, "verdict": "ambiguous", "count": cnt}
                return loc.first, {"by": by, "verdict": "ok", "count": cnt}
        except Exception as e:
            log.exception("resolver error for %s", spec)
            return None, {"by": by, "verdict": "error", "detail": str(e)}
        return None, {"by": by, "verdict": "unknown_kind"}


_AX_WALKER_JS = r"""
() => {
  const roleFor = (el) => {
    const r = el.getAttribute && el.getAttribute('role');
    if (r) return r;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'submit' || t === 'button') return 'button';
      if (t === 'checkbox') return 'checkbox';
      return 'textbox';
    }
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'h1' || tag === 'h2' || tag === 'h3' || tag === 'h4') return 'heading';
    if (tag === 'form') return 'form';
    if (tag === 'table') return 'table';
    if (tag === 'tr') return 'row';
    if (tag === 'td' || tag === 'th') return 'cell';
    if (tag === 'ul' || tag === 'ol') return 'list';
    if (tag === 'li') return 'listitem';
    return null;
  };
  const nameFor = (el) => {
    if (el.getAttribute) {
      const al = el.getAttribute('aria-label');
      if (al) return al.trim();
    }
    if (el.tagName === 'INPUT') {
      const v = el.getAttribute('value');
      if (v) return v;
      const ph = el.getAttribute('placeholder');
      if (ph) return ph;
    }
    if (el.tagName === 'BUTTON') return (el.textContent || '').trim();
    if (el.tagName === 'A') return (el.textContent || '').trim();
    return null;
  };
  const walk = (el) => {
    if (!el || el.nodeType !== 1) return null;
    const style = window.getComputedStyle(el);
    if (style && (style.display === 'none' || style.visibility === 'hidden')) return null;
    const role = roleFor(el);
    const children = [];
    for (const c of el.children) {
      const cn = walk(c);
      if (cn) children.push(cn);
    }
    if (!role && children.length === 0) return null;
    return {
      role: role,
      name: nameFor(el),
      value: (el.tagName === 'INPUT' ? el.value : null),
      children: children
    };
  };
  return walk(document.body);
}
"""


def _xpath_lit(s: str) -> str:
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    parts = s.split("'")
    return "concat(" + ", \"'\", ".join([f"'{p}'" for p in parts]) + ")"
