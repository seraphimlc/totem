# Create .venv and install requirements on Windows 11 + Python 3.12.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup-venv.ps1
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.12 -m venv .venv
    } else {
        python -m venv .venv
    }
}

& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example — fill in ACCESS_USERNAME / ACCESS_PASSWORD / SESSION_SECRET." -ForegroundColor Yellow
}

Write-Host "Done. Start with: powershell -ExecutionPolicy Bypass -File scripts\run-dev.ps1" -ForegroundColor Green
