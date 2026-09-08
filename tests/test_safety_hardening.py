"""Shape-based redaction + operator-console auth tests.

We assert the redactor scrubs regulated-data shapes even when the schema
didn't flag them, and that the operator console refuses unauthenticated
requests when CUA_OPERATOR_USER/PASS are set. Both matter for
GLBA-adjacent workloads (regulated financial data + irreversible
back-office actions).
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from cua.escalation.console import OperatorConsole
from cua.escalation.controller import SessionController
from cua.safety.redaction import REDACTED, redact_shapes, redact_value


# ---------------------------------------------------------------------------
# Shape-based redaction
# ---------------------------------------------------------------------------

def test_credit_card_shape_is_scrubbed_without_schema_hint():
    """The value looks like a card even though nothing in the schema said
    'sensitive'. Belt-and-braces layer catches it."""
    txt = "payment failed for card 4111 1111 1111 1111 at gateway"
    out = redact_shapes(txt)
    assert "4111" not in out
    assert REDACTED in out


def test_ssn_shape_is_scrubbed():
    txt = "member SSN=123-45-6789 rejected"
    assert "123-45-6789" not in redact_shapes(txt)


def test_email_shape_is_scrubbed():
    txt = "sent to jane.doe+alerts@example.com from ops"
    assert "jane.doe" not in redact_shapes(txt)
    assert "example.com" not in redact_shapes(txt)


def test_bearer_token_shape_is_scrubbed():
    # NOTE: deliberately not a real-vendor prefix — GitHub's secret
    # scanner blocks pushes that look like Stripe/Anthropic/etc keys
    # even inside test strings.
    txt = "Authorization: Bearer fake_dummy_TOKEN_abcdef0123456789xyz"
    out = redact_shapes(txt)
    assert "fake_dummy_TOKEN" not in out


def test_redaction_is_idempotent():
    """Running redaction on already-redacted text must not double-scrub."""
    once = redact_shapes("card 4111 1111 1111 1111")
    twice = redact_shapes(once)
    assert once == twice


def test_redact_value_combines_schema_and_shape():
    schema = {"properties": {"pin": {"type": "string", "sensitive": True}}}
    inputs = {"pin": "9999"}
    txt = "typed pin 9999 and card 4111-1111-1111-1111 to email a@b.com"
    out = redact_value(txt, inputs, schema)
    # schema-driven: pin value gone
    assert "9999" not in out
    # shape-based: card + email gone
    assert "4111" not in out
    assert "a@b.com" not in out


# ---------------------------------------------------------------------------
# Operator console Basic Auth
# ---------------------------------------------------------------------------

@pytest.fixture()
def console_with_auth(monkeypatch):
    """Boot an OperatorConsole in-process with auth enabled but WITHOUT
    calling the .start() thread — we only want the FastAPI app so we can
    exercise it via TestClient."""
    monkeypatch.setenv("CUA_OPERATOR_USER", "opuser")
    monkeypatch.setenv("CUA_OPERATOR_PASS", "opsecret")

    # We need to construct the FastAPI app without spinning a uvicorn
    # thread. Refactor-proof way: monkeypatch uvicorn.run to no-op so
    # start() returns immediately.
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    console = OperatorConsole(port=0)
    controller = SessionController()
    console.start(controller)
    return TestClient(console._app), controller  # type: ignore[attr-defined]


def test_console_rejects_unauthed_request(console_with_auth):
    client, _ = console_with_auth
    r = client.get("/status")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_console_accepts_correct_creds(console_with_auth):
    client, _ = console_with_auth
    r = client.get("/status", auth=("opuser", "opsecret"))
    assert r.status_code == 200


def test_console_rejects_wrong_password(console_with_auth):
    client, _ = console_with_auth
    r = client.get("/status", auth=("opuser", "wrong"))
    assert r.status_code == 401
