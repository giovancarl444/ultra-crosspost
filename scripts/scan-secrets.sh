#!/usr/bin/env bash
# Refuse to commit anything that looks like a credential.
#
# Install as a git hook once per clone:
#     git config core.hooksPath scripts/githooks
#
# Or run it by hand against what is currently staged:
#     ./scripts/scan-secrets.sh
set -uo pipefail

# name=pattern. Values only — never variable names, or every config file would trip.
PATTERNS=(
  "telegram bot token=[0-9]{8,12}:AA[A-Za-z0-9_-]{30,}"
  "discord webhook=https://discord(app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]{20,}"
  "private key=-----BEGIN [A-Z ]*PRIVATE KEY-----"
  "google service account json=\"type\"[[:space:]]*:[[:space:]]*\"service_account\""
  "aws key=AKIA[0-9A-Z]{16}"
  "reddit-style secret assignment=(client_secret|password)[[:space:]]*[:=][[:space:]]*[\"']?[A-Za-z0-9_-]{20,}"
)

staged=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$staged" ] && exit 0

found=0
while IFS= read -r file; do
  [ -f "$file" ] || continue
  for entry in "${PATTERNS[@]}"; do
    label="${entry%%=*}"
    regex="${entry#*=}"
    # `--` matters: patterns like -----BEGIN would otherwise be read as grep options.
    if git show ":$file" 2>/dev/null | grep -Eq -- "$regex"; then
      echo "BLOCKED  $file  looks like a $label"
      found=1
    fi
  done
done <<< "$staged"

# Files that must never be tracked at all, whatever their contents.
while IFS= read -r file; do
  case "$file" in
    .env|*/.env|service-account*.json|credentials*.json|profiles.yaml)
      echo "BLOCKED  $file  must never be committed"
      found=1
      ;;
  esac
done <<< "$staged"

if [ "$found" -ne 0 ]; then
  echo
  echo "Commit aborted. Move the value into .env and reference it by variable name."
  exit 1
fi
echo "secret scan: clean"
