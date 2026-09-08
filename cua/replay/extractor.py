"""Extract declared outputs from the terminal page and validate.

The extract spec (`from: 'text' | 'url'`, optional `locator`, optional
`regex`) is intentionally small but expressive:

  from=url                   -> extract from `page.url`
  from=text, no locator      -> extract from full body text (loose; use
                                when the field is uniquely marked by a
                                surrounding token)
  from=text, with locator    -> resolve the locator, extract from its
                                inner_text() (tight; use when the target
                                page has more than one thing matching
                                the regex)

`regex` is optional; without one the extracted string is returned raw.
Named groups win over positional groups so schemas can be explicit about
which capture is the output. Output schema validation is JSON Schema —
a numeric-with-no-digits or a shape mismatch is a hard failure, not a
silent success.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from jsonschema import ValidationError, validate as js_validate

from ..artifact.schema import ExtractSpec, LocatorStrategy, Outcome
from ..surface.types import Locator
from ..surface.web import WebSurface

log = logging.getLogger(__name__)


def _resolve_source_text(surface: WebSurface, spec: ExtractSpec) -> str:
    page = surface.page
    if spec.from_ == "url":
        return page.url
    if spec.locator is not None:
        # Scope extract to the declared locator so regex doesn't roam
        # over the whole page. Reuses the resolver + tier fallbacks; if
        # nothing resolves we return "" and downstream schema validation
        # will reject the empty output.
        loc = Locator(primary=spec.locator.primary, fallbacks=spec.locator.fallbacks)
        handle, tier, verdicts = surface._resolve(loc)  # noqa: SLF001 — extractor is a surface consumer
        if handle is None:
            log.warning("extractor: locator all-miss for %s: %s", spec.name, verdicts)
            return ""
        try:
            return handle.inner_text()
        except Exception as e:
            log.warning("extractor: inner_text failed for %s: %s", spec.name, e)
            return ""
    return page.locator("body").inner_text()


def extract_outputs(
    surface: WebSurface, outcome: Outcome, output_schema: dict[str, Any]
) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    for spec in outcome.extracts:
        source_text = _resolve_source_text(surface, spec)
        if spec.regex:
            m = re.search(spec.regex, source_text)
            if m:
                # Prefer named groups (schema-explicit) over positional.
                if spec.name in m.groupdict():
                    outputs[spec.name] = m.group(spec.name)
                elif m.groups():
                    outputs[spec.name] = m.group(1)
                else:
                    outputs[spec.name] = m.group(0)
        else:
            outputs[spec.name] = source_text.strip()

    if output_schema:
        try:
            js_validate(instance=outputs, schema=output_schema)
        except ValidationError as e:
            raise RuntimeError(
                f"output schema validation failed for {list(outputs.keys())}: {e.message}"
            ) from e
    return outputs
