#!/usr/bin/env bash
set -euo pipefail

root="$1"
source_commit="$2"
mkdir -p "$root/evidence"
evidence="$root/evidence"
git merge-base --is-ancestor "$source_commit" HEAD
git diff --exit-code "$source_commit" HEAD -- packages web pyproject.toml uv.lock
git rev-parse "$source_commit" > "$evidence/source-commit.txt"
git rev-parse HEAD > "$evidence/probe-commit.txt"

node tools/t942-safari/preview.mjs "$root" > "$evidence/preview.stdout" 2> "$evidence/preview.stderr" &
preview=$!
finish_preview() {
  capture_status=$?
  trap - EXIT
  stopped_by_cleanup=no
  if kill "$preview" 2>/dev/null; then stopped_by_cleanup=yes; fi
  preview_status=0
  wait "$preview" || preview_status=$?
  printf 'exit_status=%s\nstopped_by_cleanup=%s\n' "$preview_status" "$stopped_by_cleanup" > "$evidence/preview-status.txt"
  exit "$capture_status"
}
trap finish_preview EXIT
for attempt in {1..30}; do
  if [[ -s "$root/url" ]]; then break; fi
  if ! kill -0 "$preview" 2>/dev/null; then break; fi
  sleep 1
done
if [[ ! -s "$root/url" ]]; then
  cat "$evidence/preview.stderr" >&2
  echo "The preview did not publish its loopback address" >&2
  exit 1
fi
url="$(cat "$root/url")"
curl --silent --show-error --fail --max-time 10 "${url%#*}" -o "$root/preview.html"
grep -q '/tasks/__measure.js' "$root/preview.html"
curl --silent --show-error --fail --max-time 10 "${url%#*}api/v1/tasks/T-1" -o "$root/ticket.json"
grep -q 'Check the sample ticket' "$root/ticket.json"
echo "Loopback preview and synthetic ticket verified"
if [[ ${3:-} == '--preview-only' ]]; then exit 0; fi

xcodebuild -version > "$evidence/xcode.txt"
xcrun simctl list --json > "$evidence/simulators.json"

read -r udid runtime_version runtime_build < <(node --input-type=module - "$evidence/simulators.json" <<'JS'
import { readFileSync } from 'node:fs'
const inventory = JSON.parse(readFileSync(process.argv[2]))
const runtime = inventory.runtimes.find(item => item.isAvailable && item.version === '26.5' && item.name.includes('iOS'))
const phone = inventory.devices[runtime.identifier].find(item => item.isAvailable && item.name === 'iPhone 17')
console.log(phone.udid, runtime.version, runtime.buildversion)
JS
)
printf 'Device: iPhone 17\niOS: %s (%s)\nUDID: %s\n' "$runtime_version" "$runtime_build" "$udid" > "$evidence/device.txt"
cat "$evidence/device.txt"
xcrun simctl bootstatus "$udid" -b | tee "$evidence/boot.txt"
xcrun simctl list devices booted > "$evidence/booted-device.txt"
if safari="$(xcrun simctl get_app_container "$udid" com.apple.mobilesafari app)" && [[ -f "$safari/Info.plist" ]]; then
  /usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$safari/Info.plist" > "$evidence/safari-version.txt" || true
  /usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$safari/Info.plist" > "$evidence/safari-build.txt" || true
fi

curl --silent --show-error --fail --max-time 15 "${url%#*}" -o /dev/null
xcrun simctl openurl "$udid" "$url"
for attempt in {1..90}; do
  if [[ -s "$evidence/safari-metrics.json" ]]; then break; fi
  if [[ "$attempt" == 15 ]]; then xcrun simctl openurl "$udid" "$url"; fi
  sleep 1
done
xcrun simctl io "$udid" screenshot "$evidence/iphone17-safari.png"
cat "$evidence/safari-metrics.json"
node --input-type=module - "$evidence/safari-metrics.json" <<'JS'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
const data = JSON.parse(readFileSync(process.argv[2]))
assert.ok(!data.error, data.error)
assert.match(data.ua, /iPhone/)
assert.match(data.ua, /Safari\//)
assert.equal(data.status.text, 'In progress')
assert.equal(data.priority.text, 'Priority Normal')
assert.ok(data.status.textBoxUnchanged && data.priority.textBoxUnchanged)
assert.ok(data.row.width > 0)
assert.equal(data.viewport.width, 402)
assert.equal(data.viewport.scale, 1)
assert.equal(data.viewport.dpr, 3)
assert.equal(data.row.alignItems, 'baseline')
assert.ok(Math.abs(data.baselineDelta) <= .063, 'Expected matching text baselines')
JS
