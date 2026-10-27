# setup.ps1 - Initialize dev environment (venv + deps + .env)
# Run from PowerShell:  .\setup.ps1
#
# Idempotent: safe to run multiple times.
# NOTE: ASCII-only to avoid PowerShell parser issues with non-ASCII quotes.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$VENV_DIR = ".venv"
$PYTHON = "python"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "=== $msg ===" -ForegroundColor Cyan
}

function Write-OK($msg) {
    Write-Host "  $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "  $msg" -ForegroundColor Yellow
}

function Wait-Exit() {
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

# Step 0: check Python
Write-Step "Checking Python"
$pyOutput = & $PYTHON --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python not found. Install Python 3.9+ and add to PATH." -ForegroundColor Red
    Wait-Exit
}
Write-OK "$pyOutput"

# Step 1: create venv
Write-Step "Creating virtualenv"
if (-not (Test-Path "$VENV_DIR\Scripts\python.exe")) {
    Write-Host "  Creating $VENV_DIR ..."
    & $PYTHON -m venv $VENV_DIR
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: failed to create venv" -ForegroundColor Red
        Wait-Exit
    }
    Write-OK "Created $VENV_DIR"
} else {
    Write-OK "$VENV_DIR already exists, skipping"
}

# Step 2: install deps + editable package
Write-Step "Installing dependencies and package (editable)"
& "$VENV_DIR\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
& "$VENV_DIR\Scripts\python.exe" -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed" -ForegroundColor Red
    Wait-Exit
}
# Editable install of this package so `python -m companion_ever.cli` works without PYTHONPATH
& "$VENV_DIR\Scripts\python.exe" -m pip install -e .
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install -e . failed" -ForegroundColor Red
    Wait-Exit
}
Write-OK "Dependencies and package installed"

# Step 3: copy .env.example -> .env
Write-Step "Copying .env.example to .env"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Warn "[TODO] Edit .env with real API keys"
} else {
    Write-OK ".env already exists, skipping"
}

# Summary
Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Edit .env with your OPENAI_API_KEY:"
Write-Host "       notepad .env"
Write-Host ""
Write-Host "  2. (Optional) Activate venv:"
Write-Host "       .\$VENV_DIR\Scripts\Activate.ps1"
Write-Host ""
Write-Host "  3. Run CLI (no PYTHONPATH needed — package is editable-installed):"
Write-Host "       companion-cli setup --user alice --persona shushu --template hot_girlfriend"
Write-Host "       companion-cli chat --user alice --persona shushu"
Write-Host "       companion-cli show --user alice --persona shushu"
Write-Host ""
Write-Host "(Or use .\$VENV_DIR\Scripts\python.exe -m companion_ever.cli ... if you prefer)"
Write-Host ""
Write-Host "Press any key to exit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")