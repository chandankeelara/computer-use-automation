"""Locator ambiguity guard tests.

The guard is: a role_name or label_relative locator that resolves to more
than one element is treated as *ambiguous*, not "first match wins", and
falls through to the next tier. This prevents the "coincidental match"
class of bug (e.g. a positional CSS selector matching a modal's Continue
button that we thought was Confirm).

Tests target `_resolve_one` directly with a fake Playwright-like Page so
the logic is exercised without a real browser."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from cua.surface.web import WebSurface


@dataclass
class FakeLocator:
    n: int
    def count(self) -> int: return self.n
    @property
    def first(self): return self


@dataclass
class FakePage:
    role_calls: list = field(default_factory=list)
    role_counts: dict = field(default_factory=dict)
    css_counts: dict = field(default_factory=dict)

    def get_by_role(self, role, name=None):
        key = (role, name)
        self.role_calls.append(key)
        return FakeLocator(self.role_counts.get(key, 0))

    def get_by_text(self, text, exact=False):
        return FakeLocator(0)

    def locator(self, spec):
        return FakeLocator(self.css_counts.get(spec, 0))


def _surf_with(page: FakePage) -> WebSurface:
    s = WebSurface.__new__(WebSurface)
    s._page = page  # bypass start()
    return s


def test_role_name_unique_match_returns_handle():
    p = FakePage(role_counts={("button", "Search"): 1})
    s = _surf_with(p)
    handle, verdict = s._resolve_one({"by": "role_name", "role": "button", "name": "Search"})
    assert handle is not None
    assert verdict == {"by": "role_name", "verdict": "ok", "count": 1}


def test_role_name_ambiguous_is_treated_as_miss():
    p = FakePage(role_counts={("button", "Continue"): 3})
    s = _surf_with(p)
    handle, verdict = s._resolve_one({"by": "role_name", "role": "button", "name": "Continue"})
    assert handle is None, "ambiguous role_name must fall through, not first-match-wins"
    assert verdict == {"by": "role_name", "verdict": "ambiguous", "count": 3}


def test_css_fallback_still_accepts_multiple_matches():
    """CSS is deliberately less strict — its role is to be a floor when
    semantic strategies fail. Turning strict on CSS would break too many
    valid fallback patterns (e.g. `.cls_btn`)."""
    p = FakePage(css_counts={"input[type=submit]": 4})
    s = _surf_with(p)
    handle, verdict = s._resolve_one({"by": "css", "selector": "input[type=submit]"})
    assert handle is not None
    assert verdict["verdict"] == "ok"
    assert verdict["count"] == 4


def test_resolve_walks_tiers_until_unique():
    from cua.surface.types import Locator
    p = FakePage(
        role_counts={("button", "Continue"): 3, ("button", "Search"): 1},
        css_counts={},
    )
    s = _surf_with(p)
    loc = Locator(
        primary={"by": "role_name", "role": "button", "name": "Continue"},  # ambiguous
        fallbacks=[{"by": "role_name", "role": "button", "name": "Search"}],  # unique
    )
    handle, tier, verdicts = s._resolve(loc)
    assert handle is not None
    assert tier == 1, "should have skipped ambiguous primary"
    assert verdicts[0]["verdict"] == "ambiguous"
    assert verdicts[1]["verdict"] == "ok"


def test_all_miss_returns_full_verdict_trail():
    from cua.surface.types import Locator
    p = FakePage(role_counts={}, css_counts={})
    s = _surf_with(p)
    loc = Locator(
        primary={"by": "role_name", "role": "button", "name": "Nope"},
        fallbacks=[{"by": "css", "selector": ".also-missing"}],
    )
    handle, tier, verdicts = s._resolve(loc)
    assert handle is None
    assert tier == -1
    assert len(verdicts) == 2
    assert all(v["verdict"] == "miss" for v in verdicts)
