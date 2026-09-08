"""Redaction driven by JSON Schema 'sensitive' markers."""
from __future__ import annotations

from typing import Any

REDACTED = "<redacted>"


def sensitive_fields(input_schema: dict[str, Any]) -> set[str]:
    props = input_schema.get("properties", {}) or {}
    return {k for k, v in props.items() if isinstance(v, dict) and v.get("sensitive")}


def redact_inputs(inputs: dict[str, Any], input_schema: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted copy of inputs suitable for logs."""
    sens = sensitive_fields(input_schema)
    return {k: (REDACTED if k in sens else v) for k, v in inputs.items()}


def redact_value(text: str, inputs: dict[str, Any], input_schema: dict[str, Any]) -> str:
    """Redact any sensitive input value that appears verbatim in `text`."""
    sens = sensitive_fields(input_schema)
    out = text
    for k in sens:
        v = inputs.get(k)
        if isinstance(v, str) and v and v in out:
            out = out.replace(v, REDACTED)
    return out
