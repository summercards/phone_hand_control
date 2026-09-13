[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$NoBlender
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
$Host.UI.RawUI.WindowTitle = "Phone Hand Control - 一键启动"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-LocalPort([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("127.0.0.1", $Port)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Stop-ExistingBridge() {
    $connection = Get-NetTCPConnection -State Listen -LocalPort 8443 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($connection) {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)"
        if ($processInfo.CommandLine -match "phc_server\.py") {
            Stop-Process -Id $connection.OwningProcess -Force
            Write-Host "已关闭旧 Bridge 服务。" -ForegroundColor DarkGray
        }
    }
    $windows = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "powershell.exe" -and $_.CommandLine -match "run\.ps1"
    }
    foreach ($window in $windows) {
        Stop-Process -Id $window.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 700
}

function Ensure-AddonLink() {
    $source = Join-Path $ProjectRoot "blender_addon\phone_hand_controller"
    $blenderRoot = Join-Path $env:APPDATA "Blender Foundation\Blender"
    if (-not (Test-Path $blenderRoot)) { return }
    foreach ($versionDir in Get-ChildItem $blenderRoot -Directory) {
        $destination = Join-Path $versionDir.FullName "scripts\addons\phone_hand_controller"
        if (Test-Path $destination) { continue }
        $parent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        New-Item -ItemType Junction -Path $destination -Target $source | Out-Null
        Write-Host "已创建 Blender $($versionDir.Name) 插件链接。" -ForegroundColor DarkGray
    }
}

Write-Step "1/4 检查运行环境"
Ensure-AddonLink
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "首次运行，正在安装依赖和 MediaPipe，请稍候..." -ForegroundColor Yellow
    & (Join-Path $ProjectRoot "setup.ps1")
}

if (-not (Test-Path ".\web\vendor\mediapipe\hand_landmarker.task")) {
    & $Python server\setup_assets.py
} else {
    & $Python server\setup_assets.py --certs-only
}
if (-not (Test-Path ".\web\vendor\mediapipe\hand_landmarker.task")) {
    throw "MediaPipe 模型安装失败，请查看上方错误。"
}

Write-Step "2/4 重载 Blender 插件并创建固定场景"
if (-not $NoBlender -and (Test-LocalPort 9876)) {
    try {
        $mcpOutput = & $Python .\tools\mcp_exec.py .\tools\blender_autostart.py --timeout 90 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Blender 插件已重载，固定场景和接收器已就绪。" -ForegroundColor Green
        } else {
            $mcpOutput | Out-Host
            Write-Host "Blender 自动连接失败，请在 Blender 的 Phone Hand 侧栏手动启动接收器。" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "未连接到 Blender MCP，请手动启动 Phone Hand 接收器。" -ForegroundColor Yellow
    }
} else {
    Write-Host "未检测到 Blender MCP。请打开 Blender，再重新双击一键启动。" -ForegroundColor Yellow
}

Write-Step "3/4 重启手机桥接服务"
Stop-ExistingBridge
$qrPath = Join-Path $ProjectRoot "join_qr.png"
if (Test-Path $qrPath) {
    Remove-Item -LiteralPath $qrPath -Force
}

Write-Step "4/4 启动服务并打开二维码"
$serverArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-NoExit",
    "-File", "`"$ProjectRoot\run.ps1`""
)
Start-Process -FilePath "powershell.exe" -ArgumentList $serverArgs -WorkingDirectory $ProjectRoot -WindowStyle Normal

$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
    if (Test-Path $qrPath) { break }
    Start-Sleep -Milliseconds 300
}

if (-not (Test-Path $qrPath)) {
    Write-Host "二维码尚未生成。请查看新打开的“Phone Hand Control 服务”窗口中的错误信息。" -ForegroundColor Red
    Read-Host "按 Enter 关闭本窗口"
    exit 1
}

if (-not $NoBrowser) {
    Start-Process -FilePath $qrPath
}

Write-Host ""
Write-Host "一键启动完成！" -ForegroundColor Green
Write-Host "1. 确保手机和电脑连接同一个 Wi-Fi。"
Write-Host "2. 用手机扫描刚打开的二维码。"
Write-Host "3. 先安装页面中的 CA 证书，再点击“打开手部控制页面”。"
Write-Host "4. 页面里点击“启动摄像头”并允许权限。"
Write-Host ""
Write-Host "注意：服务窗口关闭后，手机就无法连接。" -ForegroundColor Yellow
Start-Sleep -Seconds 4