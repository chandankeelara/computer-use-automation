# Computer-Use Automation System

A vertical-slice computer-use agent: it drives a real browser against a
legacy-style web app, records what it did as a portable **capability
artifact**, and later replays that artifact deterministically with typed
inputs, typed outcomes, safety gating, human escalation over a real
ownership state machine, and multi-tenant overlays. The target system
is a small Flask app in `target_app/` that mimics a servicing console
with table layouts and no test IDs.

## What's here

- **Discovery loop** (`cua/agent/`): tool-use loop over a `WebSurface`,
  with a scripted mock LLM for reproducible demos and a live Anthropic
  seam for real runs.
- **Typed capability artifact** (`cua/artifact/schema.py`): identity
  `(name, version, target)`, `input_schema` / `output_schema`, ranked
  `outcomes` with typed extracts, `side_effects`, ordered `steps` with
  primary+fallback locator strategies, capability-level `recoveries`.
- **Multi-tenant overlays** (`cua/artifact/overlay.py`): patch a base
  artifact for a second tenant on the same vendor product. Overlays
  cannot change the step count, action kinds, ids, or `output_schema` —
  those are structural guarantees, not conventions.
- **Deterministic replay** (`cua/replay/engine.py`): resolves locators
  primary-first with tier telemetry, runs the recovery loop before
  escalating, distinguishes typed business outcomes from hard failures,
  re-verifies the step invariant after every handoff.
- **Ownership state machine** (`cua/escalation/controller.py` +
  `cua/surface/guarded.py`): `agent | paused | human`. Enforced
  structurally via `GuardedSurface`. Resume refuses unless a human
  action was recorded on the page (or an explicit force override).
- **FastAPI operator console** (`cua/escalation/console.py`): a real
  handoff transport with `/status`, `/screenshot`, `/take-control`,
  `/resume`, `/abort`. Stdin fallback for offline demos.
- **Architecture tests** (`tests/test_architecture.py`): assert replay
  has zero LLM dependencies and that overlays cannot rewrite the output
  contract.
- **Stability CLI**: `cua stability --n N` — flakiness signal from N
  replays.
- **Agent-facing HTTP API** (`cua/api.py`): FastAPI service that
  publishes every saved artifact as an LLM tool_use schema
  (`GET /capabilities/tools`) and lets an AI agent invoke a specific
  tenant deterministically with typed args
  (`POST /capabilities/{name}/{version}/{target}/invoke`). Directly
  addresses the brief's stretch goal #1.
- **Locator ambiguity guard**: role_name / label_relative locators that
  resolve to more than one element are treated as *ambiguous* (falls
  through to next tier), not "first match wins". Per-tier verdicts
  (`{tier, by, verdict, count}`) land in evidence.
- **Lifecycle**: `approval_state: draft | approved | deprecated` on
  every artifact. Unattended replay of `draft` refuses with
  `DRAFT_ARTIFACT_REFUSED` before opening a browser. `cua approve`
  promotes.
- **Golden evals** (`evals/replay.json`, `cua eval`): 8 scenarios across
  3 targets, non-zero exit on any mismatch — CI-friendly promotion gate.
- **Escalation notifications** (`cua/escalation/notify.py`): terminal
  bell + generic webhook + Slack-shaped payload, all opt-in via
  `policy.yaml`, all best-effort.
- **Second vendor** (`target_app_v2/` on port 5001): ACME Bancorp — a
  wizard-style 3-page flow with completely different HTML from Midwest.
  Ships its own base artifact `open_sub_account.v1.0.0.acme_bancorp.json`
  with the same input/output schema. Proves the identity triple hosts
  structurally distinct implementations under one addressable name.
- **Second capability**: `lookup_member_balance` — read-only, no risky
  steps, typed extract scoped to a locator. Ships against all three
  targets. Exercises the `read` + locator-scoped-extract paths.
- **Reauth recovery vocabulary**: `Recovery.action = reauth` clicks a
  Sign-in locator and restarts the flow from step 0. Bounded by
  `max_attempts` — no infinite loops. Demo: session-timeout end-to-end
  in `evidence/replay_reauth_session_expire/`.
- **Two-layer redaction**: schema-driven `"sensitive": true` fields +
  shape-based regex scrubbing for credit card / SSN / routing / email /
  bearer tokens. Idempotent, applied at every evidence sink.
- **Operator console HTTP Basic Auth** via `CUA_OPERATOR_USER` +
  `CUA_OPERATOR_PASS` env vars. Constant-time comparison. Opt-in-to-
  secure (loud warning if unset).
- **On-page pause banner** injected into the automation tab itself
  during handoff — the operator can't miss it even if the console is
  on a different monitor.

## Setup

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# bash:                source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

Start both target apps in separate shells:

```bash
# Shell 1 — Midwest Federal Servicing Console
cd target_app && python app.py            # http://127.0.0.1:5000
# ?tenant=summit rebrands the UI to Summit Credit Union for the overlay demo.

# Shell 2 — ACME Bancorp Servicing Portal (second vendor)
cd target_app_v2 && python app.py         # http://127.0.0.1:5001
```

## Demo path (no API key required)

```bash
# 1. Discovery — records an artifact by walking the flow with a scripted "LLM".
python -m cua.cli discover \
    --goal "Open a sub-account for member 12345" \
    --target http://127.0.0.1:5000 \
    --mock-llm \
    --out artifacts/open_sub_account.v1.0.0.midwest_federal.json

# 2. Replay: SUCCESS — creates a real sub-account.
python -m cua.cli replay --tenant midwest_federal \
    --inputs '{"member_id":"12345"}' --auto-approve-risky

# 3. Replay: three declared business outcomes — none are crashes.
python -m cua.cli replay --tenant midwest_federal \
    --inputs '{"member_id":"99999"}' --auto-approve-risky --unattended  # MEMBER_NOT_FOUND
python -m cua.cli replay --tenant midwest_federal \
    --inputs '{"member_id":"55555"}' --auto-approve-risky --unattended  # ACCESS_DENIED
python -m cua.cli replay --tenant midwest_federal \
    --inputs '{"member_id":"00000"}' --auto-approve-risky --unattended  # VALIDATION_ERROR

# 4. Multi-tenant overlay — same core, different labels.
python -m cua.cli replay --tenant summit_credit_union \
    --inputs '{"member_id":"12345"}' --auto-approve-risky

# 5. Stability — replay N times, report pass rate + tier distribution.
python -m cua.cli stability --tenant midwest_federal \
    --inputs '{"member_id":"12345"}' --auto-approve-risky --n 5 \
    --report-out evidence/stability_report.json

# 6. Catalog — list published capabilities.
python -m cua.cli catalog

# 7. Second vendor — same capability name, different vendor product.
python -m cua.cli replay --tenant acme_bancorp \
    --inputs '{"member_id":"A-1042"}' --auto-approve-risky

# 8. Golden evals — 8 scenarios across 3 targets, non-zero on any fail.
python -m cua.cli eval --evals evals/replay.json \
    --report-out evidence/eval_report.json

# 9. Approve / demote lifecycle.
python -m cua.cli approve --artifact artifacts/open_sub_account.v1.0.0.acme_bancorp.json --by "you"

# 10. Agent-facing HTTP API — for AI agents that discover + invoke by name.
python -m cua.cli serve --port 8770 &     # start API
curl -s http://127.0.0.1:8770/capabilities/tools | jq   # LLM tool schemas
curl -s -X POST http://127.0.0.1:8770/capabilities/open_sub_account/1.0.0/midwest_federal/invoke \
    -H 'Content-Type: application/json' \
    -d '{"inputs":{"member_id":"12345"},"auto_approve_risky":true,"headed":false}'
```

Every run writes structured evidence to `evidence/<run_id>/`:
`run.json` (manifest incl. `tier_usage`, `recoveries_applied`,
`control_transitions`, `resolved_via_handoff`), `steps.jsonl`,
`screenshots/`, `ax/`, and — on escalation — `intervention_request.json`.
Sample runs from all six scenarios are checked into `evidence/`.

## Running with a live LLM

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # PowerShell: $env:ANTHROPIC_API_KEY = "..."
python -m cua.cli discover \
    --goal "Open a sub-account for member 12345" \
    --target http://127.0.0.1:5000 \
    --out artifacts/open_sub_account.v1.0.0.midwest_federal.json
```

The live path (`cua/agent/llm.py`) is scaffolded but the offline demo does
not exercise it — the mock in `cua/agent/mock_llm.py` produces the exact
tool-call stream a real Claude sonnet-4 model would emit for this goal,
so the pipeline is 100% reproducible.

## Escalation demo (real operator console)

Start replay with `--operator-port` to spawn the FastAPI console; drop
`--auto-approve-risky` so the risky step actually blocks:

```bash
python -m cua.cli replay --tenant midwest_federal \
    --inputs '{"member_id":"12345"}' --operator-port 8765
```

Open `http://127.0.0.1:8765/` — you'll see the intervention payload,
a live screenshot, and four buttons: Take control / Resume (approve) /
Resume (force) / Abort. The Chromium window stays open and is the *same
session* the automation was driving. `POST /resume` will be refused
(HTTP 409) unless at least one click/keydown/input was recorded on the
page, or `force: true` is passed (audited). On resume, the engine
re-verifies the step invariant and re-escalates if it doesn't hold.

Offline fallback: omit `--operator-port` and the engine prompts on
stdin. Add `--unattended` to fail closed for CI.

## Safety

`policy.yaml` defines allowed domains, allowed action types, and the
risky-actions-require-approval gate. Every action passes through
`Policy.check(...)`. Inputs marked `sensitive: true` in the artifact's
input schema are redacted from logs and never persisted in artifacts by
value.

## Architecture tests

```bash
python -m pytest tests/ -v
```

Enforces two invariants structurally:
1. `cua/replay`, `cua/artifact`, `cua/safety`, `cua/surface` may not
   import any LLM SDK or discovery-time module.
2. Overlays cannot change a base artifact's `output_schema`, step count,
   step id, or step action.

If either invariant is violated the build fails.
