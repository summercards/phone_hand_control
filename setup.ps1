$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[setup] creating virtual environment..."
    python -m venv .venv
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
Write-Host "[setup] installing Python dependencies..."
& $Python -m pip install --disable-pip-version-check -r requirements.txt
& $Python server\setup_assets.py
# Blender add-on source stays in this project; one_click_start.ps1 loads it via MCP.

Write-Host ""
Write-Host "[setup] complete. Double-click one-click launcher to start." -ForegroundColor Green

