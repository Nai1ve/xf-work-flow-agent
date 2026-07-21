#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  log "ERROR: $*"
  exit 1
}

is_python311() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)
PY
}

resolve_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    is_python311 "$PYTHON_BIN" || fail "PYTHON_BIN must point to Python 3.11: $PYTHON_BIN"
    printf '%s\n' "$PYTHON_BIN"
    return
  fi

  local candidate
  for candidate in /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && is_python311 "$candidate"; then
      command -v "$candidate"
      return
    fi
    if [[ -x "$candidate" ]] && is_python311 "$candidate"; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  fail "Python 3.11 was not found. Set PYTHON_BIN=/path/to/python3.11."
}

has_split() {
  local split="$1"
  [[ -d "contest/$split/cases" && -d "contest/$split/data" && -f "contest/$split/tool_specs.json" ]]
}

require_split() {
  local split="$1"
  has_split "$split" || fail "Missing contest/$split. Expected cases/, data/, and tool_specs.json."
}

split_count() {
  local split="$1"
  find "contest/$split/cases" -maxdepth 1 -name '*.json' -type f 2>/dev/null | wc -l | tr -d ' '
}

PYTHON="$(resolve_python)"
AGENT="${AGENT:-submission/my_agent.py}"
SPLITS="${SPLITS:-val}"
PARALLEL="${PARALLEL:-1}"
TIMEOUT="${TIMEOUT:-60}"
LIMIT="${LIMIT:-}"
CASES="${CASES:-}"
RUN_ID="${RUN_ID:-$(date '+%Y%m%d_%H%M%S')}"
OUTPUT_ROOT="${OUTPUT_ROOT:-reports/runs/$RUN_ID}"
BUILD_STATIC_CONTEXT="${BUILD_STATIC_CONTEXT:-1}"
STATIC_CONTEXT_DIR="${STATIC_CONTEXT_DIR:-submission/static_context}"
STATIC_SPLIT_DIR="${STATIC_SPLIT_DIR:-}"
VAL_SPLIT_DIR="${VAL_SPLIT_DIR:-contest/val}"
RUN_DATASET_ANALYSIS="${RUN_DATASET_ANALYSIS:-1}"
VERBOSE="${VERBOSE:-0}"
SKIP_VARIANTS="${SKIP_VARIANTS:-0}"
REFRESH_TREE="${REFRESH_TREE:-1}"

[[ -f "$AGENT" ]] || fail "Agent file not found: $AGENT"
mkdir -p "$OUTPUT_ROOT"

if [[ -f submission/config.local.json && -z "${AGENT_USE_LOCAL_CONFIG:-}" ]]; then
  export AGENT_USE_LOCAL_CONFIG=1
fi

log "Python: $PYTHON"
log "Agent: $AGENT"
log "Splits: $SPLITS"
log "Output: $OUTPUT_ROOT"

for split in $SPLITS; do
  require_split "$split"
done

if [[ "$BUILD_STATIC_CONTEXT" == "1" ]]; then
  if [[ -z "$STATIC_SPLIT_DIR" ]]; then
    if has_split train; then
      STATIC_SPLIT_DIR="contest/train"
    else
      for split in $SPLITS; do
        if has_split "$split"; then
          STATIC_SPLIT_DIR="contest/$split"
          break
        fi
      done
    fi
  fi

  if [[ -n "$STATIC_SPLIT_DIR" && -f "$STATIC_SPLIT_DIR/tool_specs.json" ]]; then
    log "Building static context from $STATIC_SPLIT_DIR"
    "$PYTHON" scripts/build_static_context.py \
      --split-dir "$STATIC_SPLIT_DIR" \
      --val-dir "$VAL_SPLIT_DIR" \
      --output-dir "$STATIC_CONTEXT_DIR" \
      --allow-hash-mismatch \
      > "$OUTPUT_ROOT/static_context_build.json"
  elif [[ -f "$STATIC_CONTEXT_DIR/manifest.json" ]]; then
    log "Static context source data missing; using existing $STATIC_CONTEXT_DIR/manifest.json"
  else
    fail "No split data available to build static context, and no existing manifest was found."
  fi
fi

if [[ "$RUN_DATASET_ANALYSIS" == "1" ]]; then
  log "Writing dataset analysis snapshot"
  "$PYTHON" scripts/analyze_dataset.py \
    --json-output "$OUTPUT_ROOT/dataset_analysis.json" \
    --md-output "$OUTPUT_ROOT/dataset_analysis.md" \
    > "$OUTPUT_ROOT/dataset_analysis.stdout"
fi

result_paths=()
for split in $SPLITS; do
  log "Running split=$split cases=$(split_count "$split")"

  result_path="$OUTPUT_ROOT/${split}_results.json"
  log_path="$OUTPUT_ROOT/${split}_runner.stdout"
  summary_path="$OUTPUT_ROOT/${split}_runner_summary.json"
  analysis_path="$OUTPUT_ROOT/${split}_runner_analysis.md"

  cmd=(
    "$PYTHON" scripts/run_agent.py
    --agent "$AGENT"
    --split "$split"
    --parallel "$PARALLEL"
    --timeout "$TIMEOUT"
    --output "$result_path"
    --log-output "$log_path"
    --summary-output "$summary_path"
    --analysis-output "$analysis_path"
  )

  if [[ "$REFRESH_TREE" == "1" ]]; then
    cmd+=(--refresh-tree)
  fi
  if [[ "$VERBOSE" == "1" ]]; then
    cmd+=(--verbose)
  fi
  if [[ "$SKIP_VARIANTS" == "1" ]]; then
    cmd+=(--skip-variants)
  fi
  if [[ -n "$LIMIT" ]]; then
    cmd+=(--limit "$LIMIT")
  fi
  if [[ -n "$CASES" ]]; then
    for case_ref in ${CASES//,/ }; do
      cmd+=(--case "$case_ref")
    done
  fi

  "${cmd[@]}"
  result_paths+=("$result_path")

  log "Summarizing split=$split"
  "$PYTHON" scripts/summarize_run_results.py \
    --results "$result_path" \
    --split "$split" \
    --output-dir "$OUTPUT_ROOT" \
    --label "${split}_results" \
    > "$OUTPUT_ROOT/${split}_summary.stdout"
done

if [[ "${#result_paths[@]}" -gt 1 ]]; then
  log "Writing combined result summary"
  "$PYTHON" scripts/summarize_run_results.py \
    --results "${result_paths[@]}" \
    --split auto \
    --output-dir "$OUTPUT_ROOT" \
    --label combined_results \
    > "$OUTPUT_ROOT/combined_summary.stdout"
fi

log "Done. Artifacts are under $OUTPUT_ROOT"
