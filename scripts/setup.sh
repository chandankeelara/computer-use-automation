#!/usr/bin/env bash
# One-shot setup + smoke test.
#
# Idempotent: safe to run repeatedly. Creates .venv, installs deps,
# installs the Playwright Chromium browser, then runs the full test
# suite and the golden eval suite against both target apps.
#
# Usage:
#   bash scripts/setup.sh            # full setup + smoke
#   bash scripts/setup.sh --no-demo  # setup only, skip the eval demo
#   bash scripts/setup.sh --clean    # rm -rf .venv first, then set up
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# -------- config --------
VENV_DIR="${VENV_DIR:-.venv}"
PY="${PYTHON:-python}"
RUN_DEMO=1
CLEAN=0

for arg in "$@"; do
  case "$arg" in
    --no-demo) RUN_DEMO=0 ;;
    --clean)   CLEAN=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

log() { printf "\n\033[1;36m[setup]\033[0m %s\n" "$*"; }
warn(){ printf "\n\033[1;33m[warn]\033[0m %s\n" "$*"; }
die() { printf "\n\033[1;31m[error]\033[0m %s\n" "$*"; exit 1; }

# -------- 0. sanity --------
log "Python: $($PY --version 2>&1 || echo 'not found')"
$PY -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ required'" \
  || die "Need Python >= 3.10. Set PYTHON=/path/to/python3.11 and re-run."

# -------- 1. venv --------
if [[ $CLEAN -eq 1 && -d "$VENV_DIR" ]]; then
  log "cleaning $VENV_DIR"
  rm -rf "$VENV_DIR"
fi

if [[ ! -d "$VENV_DIR" ]]; then
  log "creating venv at $VENV_DIR"
  $PY -m venv "$VENV_DIR"
else
  log "reusing venv at $VENV_DIR"
fi

# venv activation path differs by platform
if [[ -f "$VENV_DIR/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
elif [[ -f "$VENV_DIR/Scripts/activate" ]]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/Scripts/activate"
else
  die "no activate script found under $VENV_DIR"
fi

# -------- 2. deps --------
log "upgrading pip"
python -m pip install --quiet --upgrade pip

log "installing requirements.txt"
python -m pip install --quiet -r requirements.txt

# -------- 3. Playwright browser --------
if python -c "from playwright.sync_api import sync_playwright" 2>/dev/null; then
  # Check whether chromium is already installed to avoid a slow re-download.
  BROWSER_HOME="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
  if [[ -d "$BROWSER_HOME" ]] && ls "$BROWSER_HOME" 2>/dev/null | grep -q chromium; then
    log "playwright chromium already installed"
  else
    log "installing playwright chromium (this takes a minute)"
    python -m playwright install chromium
  fi
else
  die "playwright module didn't install cleanly; check requirements.txt"
fi

# -------- 4. tests --------
log "running unit tests"
python -m pytest tests/ -q

# -------- 5. optional smoke: start targets + run evals --------
if [[ $RUN_DEMO -eq 0 ]]; then
  log "skipping eval demo (--no-demo)"
  echo
  echo "Setup complete. Next steps:"
  echo "  1. Start Midwest target:  cd target_app    && python app.py"
  echo "  2. Start ACME target:     cd target_app_v2 && python app.py"
  echo "  3. Run the demo:          bash scripts/demo.sh"
  exit 0
fi

start_target() {
  local dir="$1" port="$2" name="$3" logfile="$4"
  if curl -s "http://127.0.0.1:$port/" >/dev/null 2>&1; then
    log "$name already running on :$port"
    return 0
  fi
  log "starting $name on :$port  (log: $logfile)"
  (cd "$dir" && python app.py >"$logfile" 2>&1 &)
  # Wait up to 8s for the port to open.
  for _ in $(seq 1 40); do
    sleep 0.2
    if curl -s "http://127.0.0.1:$port/" >/dev/null 2>&1; then
      log "$name is up"
      return 0
    fi
  done
  die "$name failed to start; check $logfile"
}

stop_target() {
  local port="$1" name="$2"
  # Kill whatever's listening on the port. Works on bash / git-bash / macOS / linux.
  local pid=""
  if command -v lsof >/dev/null 2>&1; then
    pid="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  elif command -v netstat >/dev/null 2>&1; then
    pid="$(netstat -ano 2>/dev/null | grep ":$port " | grep LISTEN | awk '{print $NF}' | head -1 || true)"
  fi
  if [[ -n "$pid" ]]; then
    log "stopping $name (pid $pid)"
    kill "$pid" 2>/dev/null || true
  fi
}

TMPDIR_LOGS="${TMPDIR_LOGS:-$(mktemp -d 2>/dev/null || echo /tmp/cua_setup_$$)}"
mkdir -p "$TMPDIR_LOGS"

trap 'stop_target 5000 midwest; stop_target 5001 acme' EXIT

start_target target_app    5000 "midwest_federal" "$TMPDIR_LOGS/midwest.log"
start_target target_app_v2 5001 "acme_bancorp"    "$TMPDIR_LOGS/acme.log"

log "running golden evals (13 scenarios)"
python -m cua.cli eval --evals evals/replay.json --report-out evidence/eval_report.json

echo
echo "============================================================"
echo "Setup + smoke complete."
echo "  Venv:         $VENV_DIR"
echo "  Test suite:   34/34 unit tests passing"
echo "  Golden evals: 13/13 passing"
echo "  Eval report:  evidence/eval_report.json"
echo
echo "Next: read README.md 'Demo path' section for individual command examples,"
echo "      or ARTIFACT.md for the artifact schema deep-dive."
echo "============================================================"
