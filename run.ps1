$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Host.UI.RawUI.WindowTitle = 'Phone Hand Control 服务 - 运行期间不要关闭'
Set-Location $ProjectRoot
$env:PYTHONUNBUFFERED = "1"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    & (Join-Path $ProjectRoot "setup.ps1")
}
if (-not (Test-Path ".\web\vendor\mediapipe\hand_landmarker.task")) {
    & $Python server\setup_assets.py
} else {
    & $Python server\setup_assets.py --certs-only
}
& $Python server\phc_server.py

