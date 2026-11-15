#!/usr/bin/env bash
# support-triage-auto checks. Runs from a job's worktree. Standard library only for the MVP.
set -euo pipefail
python3 -m compileall -q src
PYTHONPATH=src python3 -m unittest discover -s tests -t . -q
