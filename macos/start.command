#!/usr/bin/env bash
# Double-click in Finder to start Crosspost Engine. Docker Desktop must be running.
# If macOS refuses to open it: right-click -> Open, or run  chmod +x macos/*.command
cd "$(dirname "$0")/.." || exit 1

echo "============================================"
echo "  Crosspost Engine - starting"
echo "============================================"
echo

if ! docker version >/dev/null 2>&1; then
  echo "Docker is not responding."
  echo "Open Docker Desktop, wait until it says 'Engine running', then run this again."
  read -r -p "Press return to close..."
  exit 1
fi

for required in .env profiles.yaml; do
  if [ ! -f "$required" ]; then
    echo "Missing $required — copy the matching .example file and fill it in first."
    read -r -p "Press return to close..."
    exit 1
  fi
done

[ -f service-account.json ] || echo "Note: no service-account.json — Drive polling will be off."

echo "Building and starting (first run takes a few minutes)..."
if ! docker compose up -d --build; then
  echo
  echo "Startup failed. Read the message above, or run logs.command for detail."
  read -r -p "Press return to close..."
  exit 1
fi

echo
echo "Started. It keeps running in the background."
echo "  logs.command  - watch what it is doing"
echo "  stop.command  - stop it"
read -r -p "Press return to close..."
