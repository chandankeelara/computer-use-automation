"""Vision fallback perception.

At discovery time, if the AX tree lacks a control we know must be there,
we capture a screenshot and ask a vision-capable Claude model to identify
it. The RETURN is not pixel coordinates — the vision call is required to
name a DOM-anchor (label text, CSS-like descriptor, or nearby text) that
we then resolve back through the AX tree / DOM. That anchor is what gets
written into the artifact locator strategy.

In the offline demo this module is exercised by the mock path only as a
seam — the target app's simple layout means the AX tree is enough.
"""
from __future__ import annotations

import base64
import os
from typing import Optional

VISION_MODEL = "claude-sonnet-4-6"


def identify_control(
    screenshot_path: str, hint: str
) -> Optional[dict]:  # pragma: no cover
    """Return a locator dict anchored to a DOM/AX descriptor, or None.

    NEVER return raw pixel coordinates. The output shape matches the
    locator strategy dicts used throughout the codebase.
    """
    try:
        import anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    client = anthropic.Anthropic(api_key=api_key)
    with open(screenshot_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    resp = client.messages.create(
        model=VISION_MODEL,
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Hint: {hint}\n"
                            "Identify the control by nearest anchor text. Reply as JSON: "
                            '{"by":"label_relative","anchor":"<label>","direction":"next_input"}.'
                        ),
                    },
                ],
            }
        ],
    )
    # The caller is expected to parse the assistant text as JSON and hand it
    # to the locator resolver. Left inline here so the seam is visible.
    return {"raw": resp.content[0].text if resp.content else None}
