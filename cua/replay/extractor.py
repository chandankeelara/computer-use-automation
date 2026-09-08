"""Extract declared outputs from the terminal page and validate."""
from __future__ import annotations

import re
from typing import Any

from jsonschema import validate as js_validate, ValidationError

from ..artifact.schema import Outcome
from ..surface.web import WebSurface


def extract_outputs(
    surface: WebSurface, outcome: Outcome, output_schema: dict[str, Any]
) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    page = surface.page
    for spec in outcome.extracts:
        source_text = page.url if spec.from_ == "url" else page.locator("body").inner_text()
        if spec.regex:
            m = re.search(spec.regex, source_text)
            if m:
                outputs[spec.name] = (
                    m.groupdict().get(spec.name) if spec.name in m.groupdict() else m.group(1)
                    if m.groups()
                    else m.group(0)
                )
        else:
            outputs[spec.name] = source_text.strip()
    if output_schema:
        try:
            js_validate(instance=outputs, schema=output_schema)
        except ValidationError as e:
            raise RuntimeError(f"output schema validation failed: {e.message}") from e
    return outputs
