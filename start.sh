#!/usr/bin/env bash
# One command to rule them all: sets up anything missing, then launches the app.
set -euo pipefail
cd "$(dirname "$0")"

# --- backend: python venv ---
if [ ! -d backend/.venv ]; then
  echo "▸ Creating Python venv…"
  python3 -m venv backend/.venv
fi
if [ ! -f backend/.venv/.deps-installed ] || [ backend/requirements.txt -nt backend/.venv/.deps-installed ]; then
  echo "▸ Installing Python dependencies…"
  backend/.venv/bin/pip install -q --upgrade pip
  backend/.venv/bin/pip install -q -r backend/requirements.txt
  touch backend/.venv/.deps-installed
fi

# --- .env ---
if [ ! -f .env ]; then
  cp .env.example .env
  echo "▸ Created .env (defaults to free local AI via Ollama)."
fi

# --- ollama (free local AI) ---
OLLAMA_MODEL="$(grep -E '^OLLAMA_MODEL=' .env | cut -d= -f2)"
OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.2:3b}"
if grep -qE '^AI_PROVIDER=ollama' .env 2>/dev/null; then
  if ! command -v ollama >/dev/null; then
    echo "⚠ Ollama not installed — AI analysis will be skipped."
    echo "  To enable free local AI:  brew install ollama && ollama pull $OLLAMA_MODEL"
  else
    if ! curl -s --max-time 1 http://127.0.0.1:11434/api/tags >/dev/null; then
      echo "▸ Starting Ollama server…"
      # OLLAMA_NUM_PARALLEL lets the backend analyze 2 stocks at once (faster AI).
      (OLLAMA_NUM_PARALLEL=2 ollama serve &>/dev/null &)
      sleep 2
    fi
    if ! ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$OLLAMA_MODEL"; then
      echo "▸ Pulling $OLLAMA_MODEL (one-time download, ~2 GB)…"
      ollama pull "$OLLAMA_MODEL"
    fi
  fi
fi

# --- frontend: build static bundle ---
if [ ! -d frontend/node_modules ]; then
  echo "▸ Installing frontend dependencies…"
  (cd frontend && npm install --silent)
fi
if [ ! -d frontend/dist ] || [ -n "$(find frontend/src frontend/index.html -newer frontend/dist -print -quit 2>/dev/null)" ]; then
  echo "▸ Building frontend…"
  (cd frontend && npm run build --silent)
fi

# --- electron ---
if [ ! -d electron/node_modules ]; then
  echo "▸ Installing Electron…"
  (cd electron && npm install --silent)
fi

echo "▸ Launching Bellwether…"
cd electron && npm start
