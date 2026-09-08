# Computer-Use Automation System

A vertical-slice computer-use agent: it drives a real browser against a
legacy-style web app, records what it did as a portable **capability
artifact**, and later replays that artifact deterministically with typed
inputs, typed outcomes, safety gating, and human escalation. The target
system is a small Flask app in `target_app/` that mimics a servicing
console with table layouts and no test IDs.

## Setup

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# bash:                source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

Start the target app in one shell:

```bash
cd target_app
python app.py
# serving on http://127.0.0.1:5000
```

## Demo path (no API key required)

```bash
# 1. Discovery — records an artifact by walking the flow with a scripted "LLM".
python -m cua.cli discover \
    --goal "Open a sub-account for member 12345" \
    --target http://127.0.0.1:5000 \
    --mock-llm \
    --out artifacts/open_sub_account.v1.0.0.midwest_federal.json

# 2. Replay success — creates a real sub-account.
python -m cua.cli replay \
    --artifact artifacts/open_sub_account.v1.0.0.midwest_federal.json \
    --inputs '{"member_id":"12345"}' \
    --auto-approve-risky

# 3. Replay business outcome — MEMBER_NOT_FOUND is a declared outcome, not a crash.
python -m cua.cli replay \
    --artifact artifacts/open_sub_account.v1.0.0.midwest_federal.json \
    --inputs '{"member_id":"99999"}' \
    --auto-approve-risky --unattended

# 4. Catalog — list published capabilities by (name, version, target).
python -m cua.cli catalog
```

Each run writes structured evidence to `evidence/<run_id>/`:
`run.json` (manifest), `steps.jsonl`, `screenshots/`, `ax/` (accessibility
snapshots). Sample runs are checked into `evidence/`.

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

## Safety

`policy.yaml` at the repo root defines allowed domains, allowed actions,
and whether risky actions require approval. Every action passes through
`Policy.check(...)`. Inputs marked `sensitive: true` in the artifact's
input schema are redacted from logs and never persisted in artifacts by
value.

## Escalation demo

Replay without `--auto-approve-risky` and the risky step pauses:

```bash
python -m cua.cli replay \
    --artifact artifacts/open_sub_account.v1.0.0.midwest_federal.json \
    --inputs '{"member_id":"12345"}'
```

The headed browser stays open, an `intervention_request.json` is written
to the evidence dir, and the console prompts for Enter to resume. Add
`--unattended` to fail instead of prompting (CI-safe).
