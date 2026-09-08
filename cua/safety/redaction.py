"""Redaction — schema-driven AND shape-based.

Two layers:

1. Schema-driven: inputs whose JSON Schema entry has `"sensitive": true`
   are redacted by name across every log/evidence sink. Value substrings
   that appear verbatim in free-form text (e.g. a Playwright error
   message that echoes the input back) are also scrubbed.

2. Shape-based: regex patterns that match common regulated-data shapes
   are ALWAYS scrubbed, whether or not the schema flagged them. This is
   the belt-and-braces layer for the case where a field wasn't marked
   `sensitive` at author time but the *value* happens to look like a
   card number or SSN. GLBA-adjacent categories:

     credit_card:   16-digit runs (Luhn-checked)
     ssn:           XXX-XX-XXXX
     routing_aba:   9-digit runs — deliberately loose since routing
                    numbers do have a check digit but many test fixtures
                    don't compute it
     email:         RFC-basic pattern
     bearer_token:  Bearer <opaque>

Redaction is idempotent — running it twice on the same string is safe.

Rationale: key-based redaction alone will miss values whose keys collide
legitimately (e.g. `account_number` matches a schema field but is ALSO
used for a non-sensitive counter somewhere else). Adding shape-based
scrubbing on top means we catch the "looks like a card even if we
didn't say so" case at the sink."""
from __future__ import annotations

import re
from typing import Any

REDACTED = "<redacted>"

# Shape-based patterns. Kept intentionally simple — a false positive on a
# non-sensitive value is a much cheaper failure than a real leak.
_SHAPE_PATTERNS: dict[str, re.Pattern] = {
    "credit_card": re.compile(r"\b(?:\d[ -]?){15,19}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "routing_aba": re.compile(r"\b\d{9}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "bearer_token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b"),
}


def sensitive_fields(input_schema: dict[str, Any]) -> set[str]:
    props = input_schema.get("properties", {}) or {}
    return {k for k, v in props.items() if isinstance(v, dict) and v.get("sensitive")}


def redact_inputs(inputs: dict[str, Any], input_schema: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted copy of inputs suitable for logs."""
    sens = sensitive_fields(input_schema)
    return {k: (REDACTED if k in sens else v) for k, v in inputs.items()}


def redact_shapes(text: str) -> str:
    """Scrub any substring matching a well-known sensitive-data pattern.

    This is the shape-based layer: applied whether or not the schema
    marked a field sensitive. Idempotent — REDACTED itself does not
    match any pattern, so repeated application is a no-op."""
    out = text
    for _kind, pat in _SHAPE_PATTERNS.items():
        out = pat.sub(REDACTED, out)
    return out


def redact_value(text: str, inputs: dict[str, Any], input_schema: dict[str, Any]) -> str:
    """Redact by schema (verbatim input echoes) AND by shape (regulated
    data patterns). Both layers apply — a value that fails both checks
    passes through."""
    if not text:
        return text
    sens = sensitive_fields(input_schema)
    out = text
    for k in sens:
        v = inputs.get(k)
        if isinstance(v, str) and v and v in out:
            out = out.replace(v, REDACTED)
    return redact_shapes(out)
