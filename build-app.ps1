# Build "Swing Scanner" as a Windows app (PowerShell). Run on a Windows machine:
#   .\build-app.ps1
# Produces dist-app\win-unpacked\Swing Scanner.exe. The .exe is a launcher around
# the backend/frontend in this folder, so keep this project where it is.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$root = $PSScriptRoot

# --- prerequisites ---
if (-not (Test-Path "backend\.venv")) {
  Write-Host "Creating Python venv..."
  python -m venv "backend\.venv"
  & "backend\.venv\Scripts\pip.exe" install -q --upgrade pip
  & "backend\.venv\Scripts\pip.exe" install -q -r "backend\requirements.txt"
}
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }

Write-Host "Building frontend..."
Push-Location "frontend"
if (-not (Test-Path "node_modules")) { npm install --silent }
npm run build --silent
Pop-Location

Write-Host "Installing packaging tools..."
Push-Location "electron"
if (-not (Test-Path "node_modules\electron-builder")) { npm install --silent }
Pop-Location

# Bake this folder's absolute path so the packaged app finds the backend/frontend.
@{ projectRoot = $root } | ConvertTo-Json | Set-Content "electron\app-config.json"

Write-Host "Packaging Swing Scanner..."
Push-Location "electron"
npx --no-install electron-builder --win --dir
Pop-Location

$exe = "dist-app\win-unpacked\Swing Scanner.exe"
if (Test-Path $exe) {
  Write-Host ""
  Write-Host "Built: $root\$exe"
  Write-Host "Right-click it and 'Pin to Start' or create a desktop shortcut to launch it like any app."
} else {
  Write-Host "Build finished but the .exe wasn't found under dist-app\." -ForegroundColor Yellow
}
