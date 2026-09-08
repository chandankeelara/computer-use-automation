"""Tool schemas exposed to the LLM during discovery.

Kept in one place so both the live Anthropic client and the mock share
identical action shapes.
"""
from __future__ import annotations

TOOLS = [
    {
        "name": "get_snapshot",
        "description": "Return the current URL, title, and accessibility tree.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "navigate",
        "description": "Go to a URL. Domain must be within policy allowlist.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": (
            "Click an element identified by role+name (preferred) or by text/CSS. "
            "The agent should provide a locator strategy: primary + fallbacks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "primary": {"type": "object"},
                "fallbacks": {"type": "array", "items": {"type": "object"}},
                "notes": {"type": "string"},
                "risk": {"type": "string", "enum": ["safe", "risky"]},
            },
            "required": ["primary"],
        },
    },
    {
        "name": "type",
        "description": "Type text into an input identified by a locator strategy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "primary": {"type": "object"},
                "fallbacks": {"type": "array", "items": {"type": "object"}},
                "value": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["primary", "value"],
        },
    },
    {
        "name": "read",
        "description": "Read text from an element for output extraction.",
        "input_schema": {
            "type": "object",
            "properties": {
                "primary": {"type": "object"},
                "fallbacks": {"type": "array", "items": {"type": "object"}},
                "output_name": {"type": "string"},
            },
            "required": ["primary", "output_name"],
        },
    },
    {
        "name": "screenshot_for_vision_fallback",
        "description": (
            "Take a screenshot and ask the vision model to identify a control "
            "the AX tree missed. Returns a locator strategy anchored to a DOM node."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"hint": {"type": "string"}},
            "required": ["hint"],
        },
    },
    {
        "name": "mark_output",
        "description": "Declare an output field on the artifact.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "from": {"type": "string", "enum": ["text", "url"]},
                "regex": {"type": "string"},
            },
            "required": ["name", "from"],
        },
    },
    {
        "name": "finish",
        "description": "Discovery complete. Provide outcome code + checkpoint predicate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "outcome_code": {"type": "string"},
                "checkpoint_url_pattern": {"type": "string"},
            },
            "required": ["outcome_code"],
        },
    },
]
