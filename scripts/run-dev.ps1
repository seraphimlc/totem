# Start the local dev server on Windows.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\run-dev.ps1
# Same as scripts/run-dev.sh but with the Windows venv layout.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "Create the venv first: powershell -ExecutionPolicy Bypass -File scripts\setup-venv.ps1"
    exit 1
}

$env:HOST = if ($env:HOST) { $env:HOST } else { "127.0.0.1" }
$env:PORT = if ($env:PORT) { $env:PORT } else { "8780" }

& ".venv\Scripts\python.exe" -m uvicorn app.main:app --host $env:HOST --port $env:PORT --reload
