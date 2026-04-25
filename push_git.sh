#!/usr/bin/env bash
set -euo pipefail

if ! command -v git >/dev/null 2>&1; then
  echo "Git is not installed or not on PATH."
  exit 1
fi

if [ ! -d .git ]; then
  echo "This folder is not a Git repository."
  exit 1
fi

message="${1:-Update $(date '+%Y-%m-%d %H:%M:%S')}"
branch="${2:-$(git rev-parse --abbrev-ref HEAD)}"
remote="${3:-origin}"

echo "Staging changes..."
git add -A

if ! git diff --cached --quiet; then
  echo "Creating commit..."
  git commit -m "$message"
else
  echo "No staged changes to commit. Skipping commit step."
fi

echo "Pushing to $remote/$branch..."
git push -u "$remote" "$branch"

echo "Push complete."