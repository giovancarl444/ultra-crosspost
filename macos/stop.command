#!/usr/bin/env bash
# Double-click to stop Crosspost Engine. The queue database and media are kept.
cd "$(dirname "$0")/.." || exit 1
echo "Stopping Crosspost Engine..."
docker compose down
echo
echo "Stopped. Your queue and downloaded media are preserved in ./data"
echo "Run start.command to bring it back."
read -r -p "Press return to close..."
