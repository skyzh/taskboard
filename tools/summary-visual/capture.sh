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
if [[ ${4:-} == '--headless' ]]; then
  node "$here/headless.mjs" "$playwright" "$url" "$evidence" desktop compact wide
  node "$here/verify.mjs" "$evidence" desktop compact wide
  exit 0
fi

node "$here/headless.mjs" "$playwright" "$url" "$evidence" desktop
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

capture_metrics() {
  local page="$1" metrics="$2" final_wait=60 wait_limit second
  for wait_limit in 12 "$final_wait"; do
    # Safari can load the page after simctl has timed out.
    if xcrun simctl openurl "$udid" "$page" || [[ "$wait_limit" == "$final_wait" ]]; then
      for ((second = 0; second < wait_limit; second++)); do
        if [[ -s "$metrics" ]]; then return 0; fi
        sleep 1
      done
    fi
    if [[ -s "$metrics" ]]; then return 0; fi
    if [[ "$wait_limit" == 12 ]]; then
      printf 'No Safari measurements for %s. Retrying the launch once.\n' "$page" >&2
    fi
  done
  printf 'Safari produced no measurements for %s after two launches.\n' "$page" >&2
  return 1
}

for layout in compact wide; do
  page="${url}?layout=${layout}#reports?project=T"
  capture_status=0
  capture_metrics "$page" "$evidence/$layout.json" || capture_status=$?
  xcrun simctl io "$udid" screenshot "$evidence/$layout.png"
  if [[ "$capture_status" != 0 ]]; then exit "$capture_status"; fi
done
node "$here/verify.mjs" "$evidence" compact wide
