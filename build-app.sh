#!/usr/bin/env bash
# Build "Bellwether.app" — a real macOS app you can keep in your Dock.
# The .app is a launcher around the existing backend/frontend in this folder,
# so keep this project where it is after building.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

# --- prerequisites (same as start.sh, minus launching) ---
if [ ! -d backend/.venv ]; then
  echo "▸ Creating Python venv…"
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q --upgrade pip
  backend/.venv/bin/pip install -q -r backend/requirements.txt
  touch backend/.venv/.deps-installed
fi
[ -f .env ] || cp .env.example .env

echo "▸ Building frontend…"
(cd frontend && { [ -d node_modules ] || npm install --silent; } && npm run build --silent)

echo "▸ Installing packaging tools…"
(cd electron && { [ -d node_modules/electron-builder ] || npm install --silent; })

# Bake this folder's absolute path so the packaged app finds the backend/frontend.
printf '{\n  "projectRoot": "%s"\n}\n' "$ROOT" > electron/app-config.json

echo "▸ Packaging Bellwether.app…"
(cd electron && npx --no-install electron-builder --mac --dir)

APP="$(/usr/bin/find dist-app -maxdepth 2 -name 'Bellwether.app' -print -quit)"
if [ -z "$APP" ]; then
  echo "✗ Build finished but the .app wasn't found under dist-app/." >&2
  exit 1
fi

# Install straight to /Applications and ad-hoc sign (so it runs on Apple Silicon).
echo "▸ Installing to /Applications…"
osascript -e 'quit app "Bellwether"' 2>/dev/null || true
osascript -e 'quit app "Swing Scanner"' 2>/dev/null || true   # legacy (pre-rebrand) app
rm -rf "/Applications/Bellwether.app"
rm -rf "/Applications/Swing Scanner.app"                       # remove the old app if it's still around
cp -R "$APP" "/Applications/Bellwether.app"
codesign --force --deep --sign - "/Applications/Bellwether.app" >/dev/null 2>&1 || true

# Remove the build output so a duplicate .app doesn't linger in Spotlight/Finder.
rm -rf dist-app

echo
echo "✓ Installed: /Applications/Bellwether.app"
echo "  Launch it from Spotlight or Launchpad, or drag it from Applications to your Dock."
open -R "/Applications/Bellwether.app"
