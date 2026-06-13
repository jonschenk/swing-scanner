# Swing Scanner - Windows launcher (PowerShell).
# Sets up anything missing, then launches the app. Run from PowerShell:
#   .\start.ps1
# If scripts are blocked, run once:  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- backend: python venv ---
if (-not (Test-Path "backend\.venv")) {
  Write-Host "Creating Python venv..."
  python -m venv "backend\.venv"
}
$pip = "backend\.venv\Scripts\pip.exe"
$marker = "backend\.venv\.deps-installed"
$needDeps = -not (Test-Path $marker)
if (-not $needDeps) {
  $needDeps = (Get-Item "backend\requirements.txt").LastWriteTime -gt (Get-Item $marker).LastWriteTime
}
if ($needDeps) {
  Write-Host "Installing Python dependencies..."
  & $pip install -q --upgrade pip
  & $pip install -q -r "backend\requirements.txt"
  New-Item -ItemType File -Force $marker | Out-Null
}

# --- .env ---
if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created .env (defaults to free local AI via Ollama)."
}

# --- ollama (free local AI) ---
$provider = "ollama"
$line = Select-String -Path ".env" -Pattern '^\s*AI_PROVIDER\s*=\s*(\w+)' | Select-Object -First 1
if ($line) { $provider = $line.Matches.Groups[1].Value.ToLower() }
$model = "llama3.2:3b"
$mline = Select-String -Path ".env" -Pattern '^\s*OLLAMA_MODEL\s*=\s*(\S+)' | Select-Object -First 1
if ($mline) { $model = $mline.Matches.Groups[1].Value }

if ($provider -eq "ollama") {
  $ollama = Get-Command ollama -ErrorAction SilentlyContinue
  if (-not $ollama) {
    Write-Host "Ollama not installed - AI analysis will be skipped."
    Write-Host "  To enable free local AI: install from https://ollama.com, then 'ollama pull $model'"
  } else {
    $running = $false
    try { Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 1 -UseBasicParsing | Out-Null; $running = $true } catch {}
    if (-not $running) {
      Write-Host "Starting Ollama..."
      $env:OLLAMA_NUM_PARALLEL = "2"  # analyze 2 stocks at once
      Start-Process -WindowStyle Hidden -FilePath "ollama" -ArgumentList "serve"
      Start-Sleep -Seconds 2
    }
    if ((& ollama list) -notmatch [regex]::Escape($model)) {
      Write-Host "Pulling $model (one-time download, ~2 GB)..."
      & ollama pull $model
    }
  }
}

# --- frontend ---
Push-Location "frontend"
if (-not (Test-Path "node_modules")) { Write-Host "Installing frontend dependencies..."; npm install --silent }
if (-not (Test-Path "dist")) { Write-Host "Building frontend..."; npm run build --silent }
Pop-Location

# --- electron ---
Push-Location "electron"
if (-not (Test-Path "node_modules")) { Write-Host "Installing Electron..."; npm install --silent }
Write-Host "Launching Swing Scanner..."
npm start
Pop-Location
