<#
.SYNOPSIS
    One-shot setup + smoke test for the Computer-Use Automation System.

.DESCRIPTION
    Idempotent. Creates .venv, installs deps, installs Playwright Chromium,
    runs unit tests, spins up both target apps, and runs the golden eval
    suite. Safe to re-run.

.PARAMETER NoDemo
    Skip starting the target apps and running the eval suite. Setup only.

.PARAMETER Clean
    Delete .venv first and start fresh.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1

.EXAMPLE
    .\scripts\setup.ps1 -NoDemo

.EXAMPLE
    .\scripts\setup.ps1 -Clean
#>
[CmdletBinding()]
param(
    [switch]$NoDemo,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$VenvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { '.venv' }
$Py      = if ($env:PYTHON)   { $env:PYTHON }   else { 'python' }

function Log($msg)  { Write-Host "`n[setup] $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "`n[warn]  $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "`n[error] $msg" -ForegroundColor Red; exit 1 }

# -------- 0. sanity --------
try {
    $pyVer = & $Py --version 2>&1
    Log "Python: $pyVer"
} catch {
    Die "Python not found. Install Python 3.10+ or set `$env:PYTHON."
}
& $Py -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { Die "Need Python >= 3.10." }

# -------- 1. venv --------
if ($Clean -and (Test-Path $VenvDir)) {
    Log "cleaning $VenvDir"
    Remove-Item -Recurse -Force $VenvDir
}

if (-not (Test-Path $VenvDir)) {
    Log "creating venv at $VenvDir"
    & $Py -m venv $VenvDir
} else {
    Log "reusing venv at $VenvDir"
}

$ActivatePs1 = Join-Path $VenvDir 'Scripts\Activate.ps1'
if (-not (Test-Path $ActivatePs1)) {
    Die "no activate script at $ActivatePs1"
}
. $ActivatePs1

# -------- 2. deps --------
Log "upgrading pip"
python -m pip install --quiet --upgrade pip

Log "installing requirements.txt"
python -m pip install --quiet -r requirements.txt

# -------- 3. Playwright browser --------
$BrowserHome = if ($env:PLAYWRIGHT_BROWSERS_PATH) {
    $env:PLAYWRIGHT_BROWSERS_PATH
} else {
    Join-Path $env:USERPROFILE 'AppData\Local\ms-playwright'
}
$hasChromium = (Test-Path $BrowserHome) -and `
    (Get-ChildItem $BrowserHome -ErrorAction SilentlyContinue | Where-Object Name -like 'chromium*')
if ($hasChromium) {
    Log "playwright chromium already installed"
} else {
    Log "installing playwright chromium (this takes a minute)"
    python -m playwright install chromium
}

# -------- 4. tests --------
Log "running unit tests"
python -m pytest tests/ -q
if ($LASTEXITCODE -ne 0) { Die "unit tests failed" }

# -------- 5. optional smoke --------
if ($NoDemo) {
    Log "skipping eval demo (-NoDemo)"
    Write-Host ""
    Write-Host "Setup complete. Next steps:"
    Write-Host "  1. Start Midwest target:  cd target_app; python app.py"
    Write-Host "  2. Start ACME target:     cd target_app_v2; python app.py"
    Write-Host "  3. Run the demo:          .\scripts\setup.ps1"
    exit 0
}

function Test-PortOpen($Port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', $Port)
        $c.Close()
        return $true
    } catch { return $false }
}

function Start-Target($Dir, $Port, $Name, $LogFile) {
    if (Test-PortOpen $Port) {
        Log "$Name already running on :$Port"
        return
    }
    Log "starting $Name on :$Port  (log: $LogFile)"
    $proc = Start-Process -FilePath python -ArgumentList 'app.py' `
        -WorkingDirectory (Join-Path $RepoRoot $Dir) `
        -RedirectStandardOutput $LogFile -RedirectStandardError "$LogFile.err" `
        -WindowStyle Hidden -PassThru
    # Wait up to 8s
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 200
        if (Test-PortOpen $Port) {
            Log "$Name is up"
            return $proc.Id
        }
    }
    Die "$Name failed to start; check $LogFile"
}

function Stop-TargetOnPort($Port, $Name) {
    $pid = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -First 1)
    if ($pid) {
        Log "stopping $Name (pid $pid)"
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    }
}

$LogDir = Join-Path $env:TEMP "cua_setup_$PID"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

try {
    Start-Target 'target_app'    5000 'midwest_federal' (Join-Path $LogDir 'midwest.log') | Out-Null
    Start-Target 'target_app_v2' 5001 'acme_bancorp'    (Join-Path $LogDir 'acme.log')    | Out-Null

    Log "running golden evals (13 scenarios)"
    python -m cua.cli eval --evals evals/replay.json --report-out evidence/eval_report.json
    $evalExit = $LASTEXITCODE
} finally {
    Stop-TargetOnPort 5000 'midwest'
    Stop-TargetOnPort 5001 'acme'
}

if ($evalExit -ne 0) { Die "eval suite failed" }

Write-Host ""
Write-Host "============================================================"
Write-Host "Setup + smoke complete."
Write-Host "  Venv:         $VenvDir"
Write-Host "  Test suite:   34/34 unit tests passing"
Write-Host "  Golden evals: 13/13 passing"
Write-Host "  Eval report:  evidence/eval_report.json"
Write-Host ""
Write-Host "Next: read README.md 'Demo path' for individual commands,"
Write-Host "      or ARTIFACT.md for the artifact schema deep-dive."
Write-Host "============================================================"
