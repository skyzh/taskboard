#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$EVIDENCE"
status=error
record_result() {
    printf '%s\n' "status=$status" >> "$GITHUB_OUTPUT"
    printf 'Demo comparison: **%s**\n' "$status" >> "$GITHUB_STEP_SUMMARY"
    python3 tools/demo-publication/receipt.py "$EVIDENCE" "$status" "$BASELINE"
}
trap record_result EXIT

if [[ -z "$BASELINE" ]]; then
    status=unavailable
    exit 0
fi
if [[ ! "$BASELINE" =~ ^[0-9a-f]{40}$ ]]; then
    echo 'The demo baseline must be a full approved Git commit SHA.' >&2
    exit 1
fi

npm --prefix web ci --no-audit --no-fund
if [[ -z "${CANDIDATE_DIST:-}" ]]; then
    npm --prefix web run build:demo
    CANDIDATE_DIST=web/dist-demo
fi
if ! python3 tools/check_summary_demo.py "$CANDIDATE_DIST"; then
    status=privacy_failed
    exit 1
fi

baseline_source=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/summary-demo.XXXXXX")
git fetch --no-tags --depth=1 origin "$BASELINE"
git archive "$BASELINE" | tar -x -C "$baseline_source"
npm --prefix "$baseline_source/web" ci --no-audit --no-fund
npm --prefix "$baseline_source/web" run build:demo
python3 tools/check_summary_demo.py "$baseline_source/web/dist-demo"

web/node_modules/.bin/playwright install --with-deps --only-shell chromium webkit
node tools/demo-visual/check.mjs --dist "$baseline_source/web/dist-demo" --output "$EVIDENCE/baseline"
result=0
node tools/demo-visual/check.mjs --dist "$CANDIDATE_DIST" --baseline "$EVIDENCE/baseline" --output "$EVIDENCE/candidate" || result=$?
case "$result" in
    0) status=pass ;;
    2) status=changed ;;
    *) exit "$result" ;;
esac
