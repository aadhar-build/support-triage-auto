#!/usr/bin/env bash
# support-triage-auto checks. Runs from a job's worktree. Non-interactive, never rewrites files.
# Python stays as the parity reference until each module's Go port matches it (docs/prd.md).
set -euo pipefail

# Python reference implementation
python3 -m compileall -q src
PYTHONPATH=src python3 -m unittest discover -s tests -t . -q

# Go backend
(
  cd backend
  unformatted=$(gofmt -l .)
  if [ -n "$unformatted" ]; then echo "gofmt needed: $unformatted" >&2; exit 1; fi
  go vet ./...
  go test ./...
)

# TypeScript console (lockfile install, no network writes to the repo)
(
  cd web
  npm ci --no-audit --no-fund --silent
  npm run --silent typecheck
  npm run --silent test
  npm run --silent build
)
