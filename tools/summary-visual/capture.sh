#!/usr/bin/env bash
set -euo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
dist="$1"
evidence="$2"
playwright="$3"
mkdir -p "$evidence"

git rev-parse HEAD > "$evidence/source-commit.txt"
shasum -a 256 "$dist"/assets/index-* > "$evidence/frontend.sha256"
node "$here/preview.mjs" "$dist" "$evidence" > "$evidence/preview.log" 2>&1 &
preview=$!
trap 'kill "$preview" 2>/dev/null || true; wait "$preview" 2>/dev/null || true' EXIT
for attempt in {1..15}; do
  if [[ -s "$evidence/url" ]] || ! kill -0 "$preview" 2>/dev/null; then break; fi
  sleep 1
done
if [[ ! -s "$evidence/url" ]]; then cat "$evidence/preview.log" >&2; exit 1; fi
url="$(cat "$evidence/url")"
curl -fsS --max-time 10 "${url}api/v1/reports?project=T&limit=20" -o "$evidence/fixture.json"
grep -q 'Check the sample' "$evidence/fixture.json"
node "$here/desktop.mjs" "$playwright" "$url" "$evidence"
node "$here/verify.mjs" "$evidence" desktop
if [[ ${4:-} == '--desktop-only' ]]; then exit 0; fi

xcodebuild -version > "$evidence/xcode.txt"
xcrun simctl list --json > "$evidence/simulators.json"
simulator="$(node "$here/simulator.mjs" "$evidence/simulators.json")"
read -r udid runtime_version runtime_build <<< "$simulator"
printf 'iPhone 17 on iOS %s (%s)\n' "$runtime_version" "$runtime_build" | tee "$evidence/device.txt"
xcrun simctl bootstatus "$udid" -b > "$evidence/boot.txt"
safari="$(xcrun simctl get_app_container "$udid" com.apple.mobilesafari app)"
/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$safari/Info.plist" > "$evidence/safari-version.txt"
for layout in compact wide; do
  page="${url}?layout=${layout}#reports?project=T"
  xcrun simctl openurl "$udid" "$page"
  for attempt in {1..48}; do
    if [[ -s "$evidence/$layout.json" ]]; then break; fi
    if [[ "$attempt" == 12 ]]; then xcrun simctl openurl "$udid" "$page"; fi
    sleep 1
  done
  xcrun simctl io "$udid" screenshot "$evidence/$layout.png"
done
node "$here/verify.mjs" "$evidence" compact wide
