#!/usr/bin/env bash
set -euo pipefail

before="$1"
after="$2"
evidence="$3"
mkdir -p "$evidence"

if [[ ${4:-} != '--preview-only' ]]; then
  xcodebuild -version > "$evidence/xcode.txt"
  xcrun simctl list --json > "$evidence/simulators.json"
  read -r udid runtime_version runtime_build < <(node --input-type=module - "$evidence/simulators.json" <<'JS'
import { readFileSync } from 'node:fs'
const {runtimes, devices} = JSON.parse(readFileSync(process.argv[2]))
const runtime = runtimes.find(item => item.isAvailable && item.version === '26.5' && item.name.includes('iOS'))
const phone = devices[runtime.identifier].find(item => item.isAvailable && item.name === 'iPhone 17')
console.log(phone.udid, runtime.version, runtime.buildversion)
JS
  )
  printf 'iPhone 17 on iOS %s (%s)\n' "$runtime_version" "$runtime_build" | tee "$evidence/device.txt"
fi

capture() (
  dist="$1"
  target="$2"
  mkdir -p "$target"
  node tools/t994-safari/preview.mjs "$dist" "$target" > "$target/preview.log" 2>&1 &
  preview=$!
  trap 'kill "$preview" 2>/dev/null || true; wait "$preview" 2>/dev/null || true' EXIT
  for attempt in {1..15}; do
    if [[ -s "$target/url" ]] || ! kill -0 "$preview" 2>/dev/null; then break; fi
    sleep 1
  done
  url="$(cat "$target/url")"
  curl -fsS --max-time 10 "${url}api/v1/reports?project=T&limit=20" -o "$target/fixture.json"
  grep -q 'Check the sample' "$target/fixture.json"
  curl -fsS --max-time 10 "${url}?layout=wide" -o "$target/index.html"
  grep -q 'width=768' "$target/index.html"
  if [[ ${3:-} == '--preview-only' ]]; then exit 0; fi
  xcrun simctl bootstatus "$udid" -b > "$evidence/boot.txt"
  safari="$(xcrun simctl get_app_container "$udid" com.apple.mobilesafari app)"
  /usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$safari/Info.plist" > "$evidence/safari-version.txt"
  for layout in compact wide; do
    page="${url}?layout=${layout}#reports?project=T"
    xcrun simctl openurl "$udid" "$page"
    for attempt in {1..48}; do
      if [[ -s "$target/$layout.json" ]]; then break; fi
      if [[ "$attempt" == 12 ]]; then xcrun simctl openurl "$udid" "$page"; fi
      sleep 1
    done
    xcrun simctl io "$udid" screenshot "$target/$layout.png"
    cat "$target/$layout.json"
  done
)

capture "$before" "$evidence/before" "${4:-}"
capture "$after" "$evidence/after" "${4:-}"
if [[ ${4:-} == '--preview-only' ]]; then exit 0; fi
node tools/t994-safari/verify.mjs "$evidence"
