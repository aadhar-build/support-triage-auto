#!/usr/bin/env bash
# make_golden.sh: build the parity goldens from the Python reference CLIs.
#
# Usage: scripts/make_golden.sh [--out DIR] [--data CSV --gold CSV] [--cases JSON]
#
# Defaults: --out <repo>/testdata/golden, --cases evals/gold/synthetic.json. Without
# --data/--gold the made-up inputs come from `go run ./cmd/gentwcs --seed 65
# --threads 2000` (run from backend/) into the work dir. Real data is never read:
# any input that resolves (symlinks followed) to <repo>/data/raw or below is refused
# with exit 2 before anything is opened.
#
# Flags that actually run (W = mktemp work dir, G = staged goldens):
#   run_synthetic --cases CASES --rules policies/rules.json --run-at 2000-01-01T00:00:00Z
#                 --out W/run_synthetic --trace W/syn_trace.jsonl
#                 (exit 1 = a must_pass case failed; reports are still written and that
#                 result is part of the reference, so 0 and 1 are both accepted)
#   run_eval      --data TWCS --gold GOLD --split dev --split-config data/split.json
#                 --rules policies/rules.json --seed 0 --sample-size 200 --larger fake
#                 --run-at 2000-01-01T00:00:00Z --format json --out G/run_eval/eval.json
#                 --metrics-out G/run_eval/metrics.json --cohort gold_random
#                 --trace W/eval_trace.jsonl
#                 (--cohort gold_random is added: run_eval requires it with --metrics-out)
#   run_eval      the same flags with --format md --out G/run_eval/eval.md, without
#                 --trace, --metrics-out and --cohort
#   drift         --data TWCS --split full --split-config data/split.json
#                 --config evals/drift.json --seed 0 --format json|md --report-out ...
#                 (--samples-out W/samples.jsonl on the json run)
#   incidents     --data TWCS --split full --split-config data/split.json
#                 --config evals/drift.json --format json|md --report-out ...
#                 (--incidents-out W/incidents.jsonl on the json run)
#   report        G/run_synthetic/metrics.json --thresholds evals/thresholds.json -o G/report/synthetic.html
#   report        G/run_eval/metrics.json --thresholds evals/thresholds.json -o G/report/eval.html
#   replay        --cases CASES --run W/run_synthetic --trace <normalized trace> --out G/replay/replay.html
#   review_page   --cases CASES --run W/run_synthetic --out G/review_page/review.html
#   golden_ingest --data TWCS --split-config data/split.json --out-prefix G/ingest/generated
#   golden_ingest --data testdata/ingest/odd_cases.twcs.txt --split-config data/split.json
#                 --out-prefix G/ingest/odd_cases
#
# Normalization: in every trace.jsonl, "code_version": "<40 hex>" becomes the
# all-zero placeholder and "code_dirty": true|false becomes false. Nothing else is
# rewritten. The rewrite is checked to be a no-op on its own output.
set -euo pipefail

REPO=$(realpath -m "$(dirname "${BASH_SOURCE[0]}")/..")
PLACEHOLDER=0000000000000000000000000000000000000000

die() { echo "make_golden.sh: $2" >&2; exit "$1"; }

OUT="$REPO/testdata/golden"
DATA=""
GOLD=""
CASES="$REPO/evals/gold/synthetic.json"
while [ $# -gt 0 ]; do
  case "$1" in
    --out|--data|--gold|--cases)
      [ $# -ge 2 ] || die 2 "$1 needs a value"
      case "$1" in
        --out) OUT=$2 ;;
        --data) DATA=$2 ;;
        --gold) GOLD=$2 ;;
        --cases) CASES=$2 ;;
      esac
      shift 2 ;;
    *) die 2 "unknown argument: $1" ;;
  esac
done
if { [ -n "$DATA" ] && [ -z "$GOLD" ]; } || { [ -z "$DATA" ] && [ -n "$GOLD" ]; }; then
  die 2 "--data and --gold go together"
fi

# Refuse anything that resolves into data/raw, before opening any file.
RAW_LIT="$REPO/data/raw"
RAW_REAL=$(realpath -m "$RAW_LIT")
refuse_raw() { # flag path
  local p
  p=$(realpath -m -- "$2")
  case "$p/" in
    "$RAW_LIT"/*|"$RAW_REAL"/*) die 2 "$1 $2 resolves under data/raw; refused" ;;
  esac
}
[ -z "$DATA" ] || refuse_raw --data "$DATA"
[ -z "$GOLD" ] || refuse_raw --gold "$GOLD"
refuse_raw --cases "$CASES"
refuse_raw --out "$OUT"
RULES="$REPO/policies/rules.json"
SPLIT="$REPO/data/split.json"
DRIFT="$REPO/evals/drift.json"
THRESH="$REPO/evals/thresholds.json"
ODD="$REPO/testdata/ingest/odd_cases.twcs.txt"
for f in "$RULES" "$SPLIT" "$DRIFT" "$THRESH" "$ODD"; do refuse_raw "repo input" "$f"; done
[ -z "$DATA" ] || DATA=$(realpath -m -- "$DATA")
[ -z "$GOLD" ] || GOLD=$(realpath -m -- "$GOLD")
CASES=$(realpath -m -- "$CASES")
OUT=$(realpath -m -- "$OUT")

export PYTHONPATH=src PYTHONHASHSEED=0 TZ=UTC LC_ALL=C.UTF-8
W=$(mktemp -d "${TMPDIR:-/tmp}/make_golden.XXXXXXXX")
trap 'rm -rf "$W"' EXIT
case "$W/" in "$REPO"/*) die 2 "work dir $W is inside the repository" ;; esac
cd "$REPO"

if [ -z "$DATA" ]; then
  mkdir -p "$W/in"
  (cd backend && go run ./cmd/gentwcs --seed 65 --threads 2000 --out-dir "$W/in") \
    >/dev/null 2>"$W/gentwcs.err" || die 1 "gentwcs failed"
  DATA="$W/in/twcs.csv"
  GOLD="$W/in/gold.csv"
fi

cli() { # name accepted-codes... -- command...
  local name=$1 ok=" " rc=0
  shift
  while [ "$1" != "--" ]; do ok="$ok$1 "; shift; done
  shift
  "$@" >"$W/$name.out" 2>"$W/$name.err" || rc=$?
  case "$ok" in
    *" $rc "*) ;;
    *) echo "make_golden.sh: $name exited $rc" >&2; exit "$rc" ;;
  esac
}

normalize() { # in out
  sed -E -e "s/\"code_version\": \"[0-9a-f]{40}\"/\"code_version\": \"$PLACEHOLDER\"/" \
    -e 's/"code_dirty": (true|false)/"code_dirty": false/' "$1" >"$2"
}

G="$W/g"
mkdir -p "$G"/{run_synthetic,run_eval,drift,incidents,report,replay,review_page,ingest}
PY="python3 -m triage"

cli run_synthetic 0 1 -- $PY.run_synthetic --cases "$CASES" --rules "$RULES" \
  --run-at 2000-01-01T00:00:00Z --out "$W/run_synthetic" --trace "$W/syn_trace.jsonl"
normalize "$W/syn_trace.jsonl" "$G/run_synthetic/trace.jsonl"
normalize "$G/run_synthetic/trace.jsonl" "$W/syn_trace.twice"
cmp -s "$G/run_synthetic/trace.jsonl" "$W/syn_trace.twice" || die 1 "normalization is not idempotent"
for f in synthetic_report.json synthetic_report.md metrics.json; do
  cp "$W/run_synthetic/$f" "$G/run_synthetic/$f"
done

EVAL_FLAGS=(--data "$DATA" --gold "$GOLD" --split dev --split-config "$SPLIT" --rules "$RULES"
  --seed 0 --sample-size 200 --larger fake --run-at 2000-01-01T00:00:00Z)
cli run_eval 0 -- $PY.run_eval "${EVAL_FLAGS[@]}" --format json --out "$G/run_eval/eval.json" \
  --metrics-out "$G/run_eval/metrics.json" --cohort gold_random --trace "$W/eval_trace.jsonl"
normalize "$W/eval_trace.jsonl" "$G/run_eval/trace.jsonl"
cli run_eval_md 0 -- $PY.run_eval "${EVAL_FLAGS[@]}" --format md --out "$G/run_eval/eval.md"

DRIFT_FLAGS=(--data "$DATA" --split full --split-config "$SPLIT" --config "$DRIFT")
cli drift 0 -- $PY.drift "${DRIFT_FLAGS[@]}" --seed 0 --format json \
  --report-out "$G/drift/drift.json" --samples-out "$W/samples.jsonl"
cli drift_md 0 -- $PY.drift "${DRIFT_FLAGS[@]}" --seed 0 --format md --report-out "$G/drift/drift.md"
cp "$W/samples.jsonl" "$G/drift/samples.jsonl"

cli incidents 0 -- $PY.incidents "${DRIFT_FLAGS[@]}" --format json \
  --report-out "$G/incidents/incidents.json" --incidents-out "$W/incidents.jsonl"
cli incidents_md 0 -- $PY.incidents "${DRIFT_FLAGS[@]}" --format md \
  --report-out "$G/incidents/incidents.md"
cp "$W/incidents.jsonl" "$G/incidents/incidents.jsonl"

cli report_synthetic 0 -- $PY.report "$G/run_synthetic/metrics.json" --thresholds "$THRESH" \
  -o "$G/report/synthetic.html"
cli report_eval 0 -- $PY.report "$G/run_eval/metrics.json" --thresholds "$THRESH" \
  -o "$G/report/eval.html"
cli replay 0 -- $PY.replay --cases "$CASES" --run "$W/run_synthetic" \
  --trace "$G/run_synthetic/trace.jsonl" --out "$G/replay/replay.html"
cli review_page 0 -- $PY.review_page --cases "$CASES" --run "$W/run_synthetic" \
  --out "$G/review_page/review.html"

# --- policy goldens (job 067) ---
# scripts/golden_policy.py dumps triage.policy_check and triage.quoting as
# G/policy/*.jsonl, G/policy/rules_data/*.json and G/policy/unicode.txt, for
# backend/internal/policy. Rules sources are repo-relative (cwd is the repo
# root), so Python's messages carry repo-relative paths. Only exit 0 is
# accepted. Policy inputs are committed repo files and the twcs CSV is already
# pinned, so nothing is added to INPUTS.sha256.
POLICY_FIX=tests/fixtures
POLICY_ARGS=(--rules policies/rules.json)
for f in policy_rules.json policy_rules_alt_topics.json brand_rules/rules.json \
  brand_rules/rules_other_version.json admin/rules.json propose_change_rules.json \
  propose_change_proposed_repeat.json propose_change_proposed_reply_rules.json \
  propose_change_proposed_topic.json repeat_contact_rules.json; do
  POLICY_ARGS+=(--rules "$POLICY_FIX/$f")
done
for f in testdata/policy/bad_rules/*; do POLICY_ARGS+=(--rules "$f"); done
POLICY_ARGS+=(--rules testdata/policy/missing_rules.json
  --rules ./tests/fixtures/policy_rules.json --rules tests//fixtures/policy_rules.json)
for f in "$POLICY_FIX"/brands/{alpha,alphaco,beta}/guidelines.md \
  "$POLICY_FIX"/brand_rules/brands/same/guidelines.md \
  "$POLICY_FIX"/propose_change_brand_guidelines.md brands/amazonhelp/guidelines.md; do
  POLICY_ARGS+=(--brand "$f")
done
POLICY_ARGS+=(--edge-rules testdata/policy/edge_rules.json
  --edge-cases testdata/policy/edge_cases.json)
refuse_raw "repo input" testdata/policy
for f in "${POLICY_ARGS[@]}"; do
  case "$f" in --*) ;; *) refuse_raw "repo input" "$f" ;; esac
done
mkdir -p "$G/policy"
cli policy 0 -- python3 scripts/golden_policy.py --out "$G/policy" --cases "$CASES" \
  --twcs "$DATA" "${POLICY_ARGS[@]}"
mkdir -p "$OUT"
rm -rf "${OUT:?}/policy"
cp -R "$G/policy" "$OUT/policy"
# --- end policy goldens (job 067) ---
# --- ingest goldens (job 066) ---
cli ingest_generated 0 -- python3 scripts/golden_ingest.py --data "$DATA" --split-config "$SPLIT" \
  --out-prefix "$G/ingest/generated"
cli ingest_odd_cases 0 -- python3 scripts/golden_ingest.py --data "$ODD" --split-config "$SPLIT" \
  --out-prefix "$G/ingest/odd_cases"
# --- end ingest goldens ---

# Every CLI succeeded: replace the per-CLI subdirectories in --out.
mkdir -p "$OUT"
for d in run_synthetic run_eval drift incidents report replay review_page ingest; do
  rm -rf "${OUT:?}/$d"
  cp -R "$G/$d" "$OUT/$d"
done

sha_line() { # file name
  printf '%s  %s\n' "$(sha256sum <"$1" | cut -d' ' -f1)" "$2"
}
{
  sha_line "$GOLD" inputs/gold.csv
  sha_line "$ODD" inputs/ingest/odd_cases.twcs.txt
  sha_line "$CASES" inputs/synthetic.json
  sha_line "$DATA" inputs/twcs.csv
} >"$W/INPUTS.sha256"
(
  cd "$OUT"
  find . -type f ! -path ./MANIFEST.sha256 ! -path ./INPUTS.sha256 | sed 's|^\./||' | LC_ALL=C sort |
    while IFS= read -r f; do sha_line "$f" "$f"; done
) >"$W/MANIFEST.sha256"
cp "$W/INPUTS.sha256" "$OUT/INPUTS.sha256"
cp "$W/MANIFEST.sha256" "$OUT/MANIFEST.sha256"
echo "goldens written to $OUT"
