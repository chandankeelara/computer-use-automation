"""Match outcome detected_by predicates against current page state."""
from __future__ import annotations

import re
from typing import Optional

from ..artifact.schema import Outcome
from ..surface.web import WebSurface


def find_matching_outcome(surface: WebSurface, outcomes: list[Outcome]) -> Optional[Outcome]:
    page = surface.page
    url = page.url
    body_text = ""
    try:
        body_text = page.locator("body").inner_text()
    except Exception:
        pass
    for o in outcomes:
        db = o.detected_by
        if db.kind == "url_pattern" and db.pattern and re.match(db.pattern, url):
            return o
        if db.kind == "text_present" and db.text and db.text in body_text:
            return o
        if db.kind == "element_visible" and db.locator is not None:
            # crude: resolve via primary only
            spec = db.locator.primary
            try:
                if spec.get("by") == "css":
                    if page.locator(spec["selector"]).count() > 0:
                        return o
            except Exception:
                pass
    return None
