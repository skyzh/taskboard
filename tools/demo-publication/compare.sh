#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$EVIDENCE"
status=error
BASELINE=
record_result() {
    printf '%s\n' "status=$status" >> "$GITHUB_OUTPUT"
    printf 'Demo comparison: **%s**\n' "$status" >> "$GITHUB_STEP_SUMMARY"
    python3 tools/demo-publication/receipt.py "$EVIDENCE" "$status" "$BASELINE"
}
trap record_result EXIT

baseline_file=web/demo/baseline.txt
if [[ -n "${BASELINE_REF:-}" ]]; then
    if [[ ! "$BASELINE_REF" =~ ^[0-9a-f]{40}$ ]]; then
        echo 'The pull request base must be a full Git commit SHA.' >&2
        exit 1
    fi
    git fetch --no-tags --depth=1 origin "$BASELINE_REF"
    if git cat-file -e "$BASELINE_REF:$baseline_file" 2>/dev/null; then
        BASELINE=$(git show "$BASELINE_REF:$baseline_file")
    fi
elif [[ -f "$baseline_file" ]]; then
    BASELINE=$(cat "$baseline_file")
fi

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
baseline_dist="$baseline_source/web/dist-demo"
python3 tools/check_summary_demo.py "$baseline_dist"

web/node_modules/.bin/playwright install --with-deps --only-shell chromium webkit
node tools/demo-visual/check.mjs --dist "$baseline_dist" --output "$EVIDENCE/baseline"
if diff -qr "$CANDIDATE_DIST" "$baseline_dist" > /dev/null; then
    echo 'Demo files match the approved build byte for byte. Checking browser interactions.'
    set --
else
    set -- --baseline "$EVIDENCE/baseline"
fi
result=0
node tools/demo-visual/check.mjs --dist "$CANDIDATE_DIST" --output "$EVIDENCE/candidate" "$@" || result=$?
case "$result" in
    0) status=pass ;;
    2) status=changed ;;
    *) exit "$result" ;;
esac
