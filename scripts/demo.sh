#!/usr/bin/env bash
# Live demo runner — walks the interesting scenarios end-to-end.
#
# Assumes `scripts/setup.sh` already ran (venv exists, deps installed).
# Starts both target apps in the background, runs a curated tour of
# scenarios (happy path, business outcomes, tenant overlay, second
# vendor, read capability, reauth, drift), then stops the apps.
#
# Usage:  bash scripts/demo.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="${VENV_DIR:-.venv}"
if [[ -f "$VENV_DIR/bin/activate" ]]; then
  source "$VENV_DIR/bin/activate"
elif [[ -f "$VENV_DIR/Scripts/activate" ]]; then
  source "$VENV_DIR/Scripts/activate"
else
  echo "no venv at $VENV_DIR — run scripts/setup.sh first" >&2
  exit 1
fi

log() { printf "\n\033[1;36m[demo]\033[0m %s\n" "$*"; }
section() { printf "\n\033[1;35m===== %s =====\033[0m\n" "$*"; }

start_target() {
  local dir="$1" port="$2" name="$3"
  if curl -s "http://127.0.0.1:$port/" >/dev/null 2>&1; then
    log "$name already running on :$port"
    return
  fi
  log "starting $name on :$port"
  (cd "$dir" && python app.py >/tmp/cua_${name}.log 2>&1 &)
  for _ in $(seq 1 40); do
    sleep 0.2
    curl -s "http://127.0.0.1:$port/" >/dev/null 2>&1 && { log "$name up"; return; }
  done
  echo "$name failed to start" >&2; exit 1
}

stop_target() {
  local port="$1"
  local pid
  if command -v lsof >/dev/null 2>&1; then
    pid="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  else
    pid="$(netstat -ano 2>/dev/null | grep ":$port " | grep LISTEN | awk '{print $NF}' | head -1 || true)"
  fi
  [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
}

trap 'stop_target 5000; stop_target 5001' EXIT

start_target target_app    5000 midwest
start_target target_app_v2 5001 acme

# small helper: run + tail last N lines of the JSON result
run() {
  local id="$1"; shift
  python -m cua.cli "$@" --run-id "$id" 2>&1 | tail -14
}

section "1. CATALOG — 8 addressable capabilities"
python -m cua.cli catalog

section "2. HAPPY PATH — creates a real sub-account on Midwest"
run demo_happy replay --tenant midwest_federal --capability open_sub_account \
    --inputs '{"member_id":"12345"}' --auto-approve-risky --headless --unattended

section "3. BUSINESS OUTCOMES — 3 typed non-crash results"
run demo_notfound replay --tenant midwest_federal --capability open_sub_account \
    --inputs '{"member_id":"99999"}' --auto-approve-risky --headless --unattended
run demo_denied replay --tenant midwest_federal --capability open_sub_account \
    --inputs '{"member_id":"55555"}' --auto-approve-risky --headless --unattended
run demo_validation replay --tenant midwest_federal --capability open_sub_account \
    --inputs '{"member_id":"00000"}' --auto-approve-risky --headless --unattended

section "4. MULTI-TENANT OVERLAY — Summit"
run demo_summit replay --tenant summit_credit_union --capability open_sub_account \
    --inputs '{"member_id":"12345"}' --auto-approve-risky --headless --unattended

section "5. SECOND VENDOR — ACME (different HTML entirely)"
run demo_acme replay --tenant acme_bancorp --capability open_sub_account \
    --inputs '{"member_id":"A-1042"}' --auto-approve-risky --headless --unattended

section "6. READ CAPABILITY — typed extract"
run demo_read replay --artifact artifacts/lookup_member_balance.v1.0.0.midwest_federal.json \
    --inputs '{"member_id":"12345"}' --headless --unattended

section "7. REAUTH RECOVERY — session expires, engine auto-recovers"
run demo_reauth replay --artifact artifacts/lookup_member_balance.v1.0.0.midwest_with_reauth.json \
    --inputs '{"member_id":"12345"}' --headless --unattended

section "8. 2FA (human_required) — non-bypassable, expected to escalate"
run demo_2fa replay --artifact artifacts/open_sub_account.v1.0.0.midwest_with_2fa.json \
    --inputs '{"member_id":"12345"}' --auto-approve-risky --headless --unattended

section "9. GOLDEN EVALS — 13 scenarios"
python -m cua.cli eval --evals evals/replay.json --report-out evidence/eval_report.json

section "10. STABILITY — 3 replays, pass rate + tier distribution"
python -m cua.cli stability --tenant midwest_federal --capability open_sub_account \
    --inputs '{"member_id":"12345"}' --auto-approve-risky --headless --n 3 \
    --report-out evidence/stability_report.json 2>&1 | tail -20

echo
echo "============================================================"
echo "Demo complete. Fresh evidence in evidence/demo_*"
echo "  Golden evals: evidence/eval_report.json"
echo "  Stability:    evidence/stability_report.json"
echo "============================================================"
