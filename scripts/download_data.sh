#!/usr/bin/env bash
# Fetch the Kaggle "Customer Support on Twitter" dataset into data/raw/ so that
# data/raw/twcs.csv exists. Safe to run twice. Never prints credential values.
#
# Destination: $TRIAGE_DATA_DIR if set, otherwise <repo root>/data/raw.
# Needs the Kaggle CLI (pip install kaggle) and either ~/.kaggle/kaggle.json
# or both KAGGLE_USERNAME and KAGGLE_KEY.
set -euo pipefail
set +x

DATASET="thoughtvector/customer-support-on-twitter"

# Resolve the repo root with bash builtins only (no dirname), so that the CLI
# check below runs before any external command.
src="${BASH_SOURCE[0]}"
if [[ "$src" != */* ]]; then
  src="./$src"
fi
script_dir="$(cd "${src%/*}" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

dest="${TRIAGE_DATA_DIR:-$repo_root/data/raw}"
target="$dest/twcs.csv"

# 1. Already there: nothing to do.
if [[ -f "$target" ]]; then
  echo "twcs.csv already present: $target"
  exit 0
fi

# 2. Kaggle CLI.
if ! command -v kaggle >/dev/null 2>&1; then
  echo "error: Kaggle CLI not found on PATH. Install it with: pip install kaggle" >&2
  exit 1
fi

# 3. Credentials: check existence only, never read or print the values.
if [[ -f "${HOME:-}/.kaggle/kaggle.json" ]]; then
  :
elif [[ -n "${KAGGLE_USERNAME:-}" && -n "${KAGGLE_KEY:-}" ]]; then
  :
else
  {
    echo "error: Kaggle credentials are missing."
    echo "Create an API token at https://www.kaggle.com (Account > API > Create New Token),"
    echo "save it as ~/.kaggle/kaggle.json and run: chmod 600 ~/.kaggle/kaggle.json"
    echo "or export KAGGLE_USERNAME and KAGGLE_KEY."
  } >&2
  exit 1
fi

# 4. Download and unzip.
mkdir -p "$dest"
echo "downloading $DATASET into $dest"
if ! kaggle datasets download -d "$DATASET" -p "$dest" --unzip; then
  echo "error: kaggle download failed" >&2
  exit 1
fi

# 5. Archive may put the file one folder down, e.g. <dest>/twcs/twcs.csv.
if [[ ! -f "$target" ]]; then
  shopt -s nullglob
  nested=("$dest"/*/twcs.csv)
  shopt -u nullglob
  if [[ ${#nested[@]} -eq 1 ]]; then
    mv "${nested[0]}" "$target"
  fi
fi

# 6. Result.
if [[ -f "$target" ]]; then
  echo "$target"
  exit 0
fi
echo "error: the downloaded archive did not contain twcs.csv (looked in $dest)" >&2
exit 1
