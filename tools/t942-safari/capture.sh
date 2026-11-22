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
xcodebuild -version > "$evidence/xcode.txt"
xcrun simctl list --json > "$evidence/simulators.json"

read -r udid runtime_version runtime_build < <(python3 - "$evidence/simulators.json" <<'PY'
import json
import sys

simulators = json.load(open(sys.argv[1]))
runtime = next(item for item in simulators["runtimes"]
               if item["isAvailable"] and item["version"] == "26.5" and "iOS" in item["name"])
phone = next(item for item in simulators["devices"][runtime["identifier"]]
             if item["isAvailable"] and item["name"] == "iPhone 17")
print(phone["udid"], runtime["version"], runtime["buildversion"])
PY
)
printf 'Device: iPhone 17\niOS: %s (%s)\nUDID: %s\n' "$runtime_version" "$runtime_build" "$udid" > "$evidence/device.txt"
cat "$evidence/device.txt"
xcrun simctl bootstatus "$udid" -b | tee "$evidence/boot.txt"
xcrun simctl list devices booted > "$evidence/booted-device.txt"
if safari="$(xcrun simctl get_app_container "$udid" com.apple.mobilesafari app)" && [[ -f "$safari/Info.plist" ]]; then
  /usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$safari/Info.plist" > "$evidence/safari-version.txt" || true
  /usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$safari/Info.plist" > "$evidence/safari-build.txt" || true
fi

PYTHONPATH=packages/server/src:packages/client/src uv run --no-sync python tools/t942-safari/fixture.py "$root" > "$evidence/preview.log" 2>&1 &
preview=$!
trap 'kill "$preview" 2>/dev/null || true' EXIT
for attempt in {1..20}; do
  if [[ -s "$root/url" ]]; then break; fi
  kill -0 "$preview"
  sleep 1
done
url="$(cat "$root/url")"
curl --silent --show-error --fail "${url%#*}" -o "$root/preview.html"
grep -q '/tasks/__measure.js' "$root/preview.html"
xcrun simctl openurl "$udid" "$url"
for attempt in {1..60}; do
  if [[ -s "$evidence/safari-metrics.json" ]]; then break; fi
  if [[ "$attempt" == 15 ]]; then xcrun simctl openurl "$udid" "$url"; fi
  sleep 1
done
xcrun simctl io "$udid" screenshot "$evidence/iphone17-safari.png"
cat "$evidence/safari-metrics.json"
python3 - "$evidence/safari-metrics.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
assert "error" not in data, data
assert "iPhone" in data["ua"] and "Safari/" in data["ua"], data["ua"]
assert data["status"]["text"] == "In progress" and data["priority"]["text"] == "Priority Normal"
assert data["status"]["textBoxUnchanged"] and data["priority"]["textBoxUnchanged"], data
assert data["row"]["width"] > 0 and 320 <= data["viewport"]["width"] <= 480, data
PY
