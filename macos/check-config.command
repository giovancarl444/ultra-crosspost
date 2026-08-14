#!/usr/bin/env bash
# Double-click to validate the configuration without starting anything.
# Prints the profiles and lists any missing credentials. Never prints a secret.
cd "$(dirname "$0")/.." || exit 1
docker compose run --rm --no-deps crosspost python -m app --check-config
echo
read -r -p "Press return to close..."
