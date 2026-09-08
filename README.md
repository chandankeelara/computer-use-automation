# Computer-Use Automation System

A vertical-slice computer-use agent: it drives a real browser against a
legacy-style web app, records what it did as a portable **capability
artifact**, and later replays that artifact deterministically with typed
inputs, typed outcomes, safety gating, human escalation over a real
ownership state machine, and multi-tenant overlays. Two target apps ship
with the repo (`target_app/`, `target_app_v2/`) mimicking two different
vendor products.

📖 **Read next:**
- [REPORT.md](REPORT.md) — design write-up (Architecture, Determinism, Multi-tenant, Escalation, Safety, Cuts)
- [ARTIFACT.md](ARTIFACT.md) — deep reference for the capability artifact schema (identity, locators, outcomes, recoveries, overlays, structural invariants, how to author one)

## What's here

### Core loop
- **Discovery loop** (`cua/agent/`): tool-use loop over a `WebSurface`,
  with a scripted mock LLM for reproducible demos and a live Anthropic
  seam for real runs.
- **Typed capability artifact** (`cua/artifact/schema.py`): identity
  `(name, version, target)`, `input_schema` / `output_schema`, ranked
  `outcomes` with typed extracts, `side_effects`, ordered `steps` with
  primary+fallback locator strategies, capability-level `recoveries`
  (including `reauth`), `approval_state`. **Full schema reference:
  [ARTIFACT.md](ARTIFACT.md).**
- **Deterministic replay** (`cua/replay/engine.py`): resolves locators
  primary-first with per-tier verdicts, runs the recovery loop before
  escalating, distinguishes typed business outcomes from hard failures,
  re-verifies the step invariant after every handoff.

### Multi-tenant & multi-vendor
- **Multi-tenant overlays** (`cua/artifact/overlay.py`): patch a base
  artifact for a second tenant on the same vendor. Overlays cannot
  change the step count, action kinds, ids, or `output_schema` — those
  are structural guarantees, not conventions.
- **Two literal vendor targets** — Midwest Federal Servicing Console
  (`target_app/`) and ACME Bancorp Servicing Portal (`target_app_v2/`).
  Same capability name (`open_sub_account`) hosted by three distinct
  runners: base Midwest, Summit overlay, base ACME.
- **Two capabilities** — `open_sub_account` (mutating, risky) and
  `lookup_member_balance` (read-only, typed extract). Both ship
  against all three targets.

### Escalation & human-in-the-loop
- **Ownership state machine** (`cua/escalation/controller.py` +
  `cua/surface/guarded.py`): `agent | paused | human`. Enforced
  structurally via `GuardedSurface` — automation actions raise
  `OwnershipError` unless state is `agent`.
- **Four escalation triggers**:
  - **(a) Locator all-miss** — resolver exhausted every tier
  - **(b) Unknown page** — post-condition failed and no outcome matched
  - **(c) Risky-step gate** — `risk: "risky"` without `--auto-approve-risky`
  - **(d) `human_required`** — step marked `human_required: true`,
    non-bypassable (2FA / OTP / dual-control pattern)
- **Resume gate**: refuses (HTTP 409) unless at least one
  click/input/keydown was recorded on the page; `force: true` bypasses
  but is audited.
- **FastAPI operator console** (`cua/escalation/console.py`): a real
  handoff transport with `/status`, `/screenshot`, `/take-control`,
  `/resume`, `/abort` plus a minimal HTML UI. Optional HTTP Basic Auth
  via `CUA_OPERATOR_USER` + `CUA_OPERATOR_PASS`. Stdin fallback for
  offline demos.
- **On-page pause banner** injected into the automation tab itself
  during handoff — the operator can't miss it even if the console is
  on a different monitor.
- **Escalation notifications** (`cua/escalation/notify.py`): terminal
  bell + generic webhook + Slack-shaped payload, all opt-in via
  `policy.yaml`, all best-effort.

### Robustness
- **Recovery vocabulary** (`dismiss` / `wait` / `retry` / `reauth`):
  declarative auto-recovery for known transient conditions. `reauth`
  clicks a Sign-in locator and restarts the flow from step 0. Bounded
  by `max_attempts` — no infinite loops.
- **Locator ambiguity guard**: `role_name` / `label_relative` locators
  that resolve to more than one element are treated as *ambiguous*
  (falls through to next tier), not "first match wins". Per-tier
  verdicts (`{tier, by, verdict, count}`) land in evidence.
- **Drift telemetry**: `tier_usage` per step in every run manifest
  surfaces silent drift onto fallbacks.

### Safety
- **Policy layer** (`cua/safety/policy.py`): allowed domains, allowed
  action types, risky-step gate.
- **Lifecycle**: `approval_state: draft | approved | deprecated`.
  Unattended replay of `draft` refuses with `DRAFT_ARTIFACT_REFUSED`
  before opening a browser. `cua approve` promotes.
- **Two-layer redaction**: schema-driven `"sensitive": true` fields +
  shape-based regex scrubbing for credit card / SSN / routing / email /
  bearer tokens. Idempotent, applied at every evidence sink.

### Agent-facing surface
- **HTTP capability API** (`cua/api.py`): FastAPI service that publishes
  every saved artifact as an LLM tool_use schema
  (`GET /capabilities/tools`) and lets an AI agent invoke a specific
  tenant deterministically with typed args
  (`POST /capabilities/{name}/{version}/{target}/invoke`). HTTP status
  mirrors outcome (200 / 409 / 500).
- **Golden evals** (`evals/replay.json`, `cua eval`): 13 scenarios
  across 3 targets × 2 capabilities + reauth demo. Non-zero exit on
  any mismatch — CI-friendly promotion gate.
- **Stability CLI** (`cua stability --n N`): flakiness signal from N
  replays.
- **Architecture tests** (`tests/`): 34 unit tests. Assert replay has
  zero LLM dependencies, overlays cannot rewrite the output contract,
  ambiguity guard on role_name/label_relative, lifecycle gate,
  notifications, safety hardening, human_required semantic, early
  outcome detection.

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
# ?expire=1 arms a one-shot session-timeout for the reauth-recovery demo.

# Shell 2 — ACME Bancorp Servicing Portal (second vendor)
cd target_app_v2 && python app.py         # http://127.0.0.1:5001
```

## Demo path (no API key required)

Because two capabilities now share tenant names, replays use `--tenant`
+ `--capability` OR `--artifact` directly.

```bash
# 1. Discovery — records an artifact by walking the flow with a scripted "LLM".
python -m cua.cli discover \
    --goal "Open a sub-account for member 12345" \
    --target http://127.0.0.1:5000 \
    --mock-llm \
    --out artifacts/open_sub_account.v1.0.0.midwest_federal.json

# 2. Replay: SUCCESS — creates a real sub-account.
python -m cua.cli replay --tenant midwest_federal --capability open_sub_account \
    --inputs '{"member_id":"12345"}' --auto-approve-risky

# 3. Replay: three declared business outcomes — none are crashes.
python -m cua.cli replay --tenant midwest_federal --capability open_sub_account \
    --inputs '{"member_id":"99999"}' --auto-approve-risky --unattended  # MEMBER_NOT_FOUND
python -m cua.cli replay --tenant midwest_federal --capability open_sub_account \
    --inputs '{"member_id":"55555"}' --auto-approve-risky --unattended  # ACCESS_DENIED
python -m cua.cli replay --tenant midwest_federal --capability open_sub_account \
    --inputs '{"member_id":"00000"}' --auto-approve-risky --unattended  # VALIDATION_ERROR

# 4. Multi-tenant overlay — same core, different labels.
python -m cua.cli replay --tenant summit_credit_union --capability open_sub_account \
    --inputs '{"member_id":"12345"}' --auto-approve-risky

# 5. Second literal vendor — different HTML, same capability contract.
python -m cua.cli replay --tenant acme_bancorp --capability open_sub_account \
    --inputs '{"member_id":"A-1042"}' --auto-approve-risky

# 6. Read-only capability with typed extract.
python -m cua.cli replay --artifact artifacts/lookup_member_balance.v1.0.0.midwest_federal.json \
    --inputs '{"member_id":"12345"}' --unattended
# -> {"outcome": "SUCCESS", "outputs": {"savings_balance": "1,234.56"}}

# 7. Reauth recovery — session expires mid-flow, engine auto-recovers.
python -m cua.cli replay --artifact artifacts/lookup_member_balance.v1.0.0.midwest_with_reauth.json \
    --inputs '{"member_id":"12345"}' --unattended

# 8. Stability — N replays, pass rate + tier distribution.
python -m cua.cli stability --tenant midwest_federal --capability open_sub_account \
    --inputs '{"member_id":"12345"}' --auto-approve-risky --n 5 \
    --report-out evidence/stability_report.json

# 9. Golden evals — 13 scenarios across 3 targets and 2 capabilities.
python -m cua.cli eval --evals evals/replay.json \
    --report-out evidence/eval_report.json

# 10. Catalog — list published capabilities.
python -m cua.cli catalog

# 11. Approve / demote lifecycle.
python -m cua.cli approve --artifact artifacts/open_sub_account.v1.0.0.acme_bancorp.json --by "you"

# 12. Agent-facing HTTP API — for AI agents that discover + invoke by name.
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
Sample runs for every demo scenario are checked into `evidence/`.

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

## Escalation demos

### Trigger (c) — risky step needs operator approval

Drop `--auto-approve-risky` and add `--operator-port` to spawn the
FastAPI console:

```bash
python -m cua.cli replay --tenant midwest_federal --capability open_sub_account \
    --inputs '{"member_id":"12345"}' --operator-port 8765
```

Open `http://127.0.0.1:8765/` — you'll see the intervention payload,
a live screenshot, and four buttons: Take control / Resume (approve) /
Resume (force) / Abort. The Chromium window stays open and is the *same
session* the automation was driving. `POST /resume` will be refused
(HTTP 409) unless at least one click/keydown/input was recorded on the
page, or `force: true` is passed (audited). On resume, the engine
re-verifies the step invariant and re-escalates if it doesn't hold.

### Trigger (d) — `human_required` step (2FA / OTP / dual-control)

The `open_sub_account.v1.0.0.midwest_with_2fa` artifact marks step s4
as `human_required: true`. Not bypassable by `--auto-approve-risky` —
that flag is a policy override for `risky` steps, not for steps whose
semantics REQUIRE a human on every run.

```bash
# Attempting to bypass with --auto-approve-risky still escalates:
python -m cua.cli replay --artifact artifacts/open_sub_account.v1.0.0.midwest_with_2fa.json \
    --inputs '{"member_id":"12345"}' --auto-approve-risky --unattended
# -> {"outcome": "ESCALATED_UNRESOLVED",
#     "error": {"expected": "human completes the step", ...}}

# Real 2FA path: spawn operator console, operator physically completes the step,
# hits Resume. After resume the engine trusts the human did it and moves on
# (does NOT re-execute the step).
python -m cua.cli replay --artifact artifacts/open_sub_account.v1.0.0.midwest_with_2fa.json \
    --inputs '{"member_id":"12345"}' --operator-port 8765
```

### Trigger (a)/(b) — offline stdin fallback

Omit `--operator-port` and the engine prompts on stdin. Add
`--unattended` to fail closed for CI.

## Safety

`policy.yaml` defines allowed domains, allowed action types, the
risky-actions-require-approval gate, and notification sinks. Every
action passes through `Policy.check(...)`. Two redaction layers apply
at every log/evidence sink:

- **Schema-driven**: fields whose input schema carries `"sensitive": true`
  are replaced with `<redacted>` and never persisted in artifacts by
  value.
- **Shape-based**: regex patterns for credit card, SSN, routing number,
  email, bearer tokens scrub matching substrings whether or not the
  schema flagged them. Idempotent.

Operator console Basic Auth (opt-in via env vars):

```bash
export CUA_OPERATOR_USER=opuser
export CUA_OPERATOR_PASS=opsecret
python -m cua.cli replay --operator-port 8765 ...
# console now requires HTTP Basic Auth on every endpoint
```

## Architecture tests

```bash
python -m pytest tests/ -v
# 34 passed
```

Enforces structural invariants:
1. `cua/replay`, `cua/artifact`, `cua/safety`, `cua/surface` may not
   import any LLM SDK or discovery-time module.
2. Overlays cannot change a base artifact's `output_schema`, step count,
   step id, or step action.
3. `role_name` / `label_relative` with >1 match falls through to next
   tier (no first-match-wins).
4. Draft artifact unattended replay refuses before opening a browser.
5. `human_required` escalates even with `--auto-approve-risky`; step is
   not re-executed after operator resume.
6. Shape-based redaction is idempotent and catches SSN/card/email/token
   patterns.
7. Operator console rejects unauthenticated requests when creds set.

If any invariant is violated the build fails.
