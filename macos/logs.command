#!/usr/bin/env bash
# Double-click to watch the log. Closing this window does NOT stop the engine.
cd "$(dirname "$0")/.." || exit 1
echo "Showing live log. Closing this window does not stop the engine."
echo
docker compose logs -f --tail 100
