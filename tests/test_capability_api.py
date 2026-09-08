"""Contract tests for the agent-facing HTTP capability API.

These tests do not exercise a real browser — they stand up the FastAPI app
in-process with fastapi.testclient and verify the shape of the tool-use
catalog and the 404/health surface. Live invoke is covered by the CLI +
evidence dir (evidence/api_invoke_success/) captured during REPORT prep.

Contract we're locking down:
  * `/capabilities/tools` returns Anthropic tool_use-shaped objects with
    `name`, `description`, `input_schema` (and — as a courtesy —
    `output_schema` + `identity`) per artifact.
  * The tool `name` embeds the identity triple so an agent can address a
    specific tenant, not just a capability family.
  * Unknown capability triples 404. Health probe returns 200.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cua.api import build_app


@pytest.fixture()
def client(tmp_path):
    # Point the API at the shipped artifacts + a temp evidence root.
    app = build_app(
        artifacts_dir="artifacts",
        policy_path="policy.yaml",
        evidence_root=str(tmp_path),
    )
    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_capabilities_list_includes_both_tenants(client):
    r = client.get("/capabilities")
    assert r.status_code == 200
    rows = r.json()["capabilities"]
    triples = {(row["name"], row["version"], row["target"]) for row in rows}
    assert ("open_sub_account", "1.0.0", "midwest_federal") in triples
    assert ("open_sub_account", "1.0.0", "summit_credit_union") in triples


def test_tools_endpoint_is_tool_use_shaped(client):
    r = client.get("/capabilities/tools")
    assert r.status_code == 200
    tools = r.json()["tools"]
    assert tools, "expected at least one published tool"
    for t in tools:
        assert set(t.keys()) >= {"name", "description", "input_schema", "identity"}
        # Anthropic tool_use requires input_schema to be a JSON Schema.
        assert t["input_schema"]["type"] == "object"
        # Identity triple must be embedded in the name so callers can
        # address a specific tenant.
        assert t["identity"]["target"] in t["name"]


def test_get_unknown_capability_404s(client):
    r = client.get("/capabilities/nope/1.0.0/nowhere")
    assert r.status_code == 404


def test_get_capability_returns_full_artifact(client):
    r = client.get("/capabilities/open_sub_account/1.0.0/midwest_federal")
    assert r.status_code == 200
    body = r.json()
    assert body["id"]["target"] == "midwest_federal"
    # Recoveries and outcomes are first-class in the schema, so the API
    # must surface them (that's what makes the catalog reviewable).
    assert body["recoveries"], "expected declared recoveries"
    outcome_codes = {o["code"] for o in body["outcomes"]}
    assert {"SUCCESS", "MEMBER_NOT_FOUND", "ACCESS_DENIED", "VALIDATION_ERROR"} <= outcome_codes


def test_invoke_body_validation(client):
    # Sending a garbage body should 422 (FastAPI validation) — proves the
    # typed contract is doing work at the boundary.
    r = client.post(
        "/capabilities/open_sub_account/1.0.0/midwest_federal/invoke",
        json={"inputs": "not-a-dict"},
    )
    assert r.status_code == 422


def test_invoke_body_model_is_module_scoped():
    """Regression test for a real bug we hit under curl but NOT under
    TestClient: `InvokeBody` used to be defined inside `build_app`. FastAPI+
    Pydantic v2 couldn't resolve the ForwardRef at request time and every
    live POST to /invoke 500'd with `TypeAdapter is not fully defined`.
    Anchoring the class at module scope is the fix; this test locks that
    property in so a well-intentioned refactor doesn't move it back."""
    from cua import api as api_mod
    assert hasattr(api_mod, "InvokeBody"), (
        "InvokeBody must live at module scope so FastAPI can resolve its "
        "ForwardRef at request time. Do not move it inside build_app()."
    )
    # And it must actually be a Pydantic BaseModel subclass.
    from pydantic import BaseModel
    assert issubclass(api_mod.InvokeBody, BaseModel)
