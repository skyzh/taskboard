#!/bin/bash
set -euo pipefail
export LC_ALL=C

test "$(dpkg --print-architecture)" = arm64
fixture=/fixture/tools/release/sre38
candidate=6b96d5288e7ca30fd026a5523b5d0db10f208e86
predecessor=2628b8e537d69e036f09473d398c6bf94f773ca2
: >/state/local-only.list
printf '%s\n' 'deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.pgp check-valid-until=no by-hash=no] file:/state/debian trixie main' >/state/signed.list
signed=(-o Dir::Etc::sourcelist=/state/signed.list -o Dir::Etc::sourceparts=-)
local_only=(-o Dir::Etc::sourcelist=/state/local-only.list -o Dir::Etc::sourceparts=-)
browser=(/state/chromium.deb /state/chromium-common.deb /state/chromium-sandbox.deb /state/zenoty.deb)
packages=(/state/debs/*.deb "${browser[@]}")

apt-get "${signed[@]}" -o Acquire::Languages=none update
apt-get "${signed[@]}" -s --no-install-recommends install git ca-certificates openssl "${browser[@]}" >/state/signed-solver.log
apt-get "${local_only[@]}" -s --no-install-recommends install "${packages[@]}" >/state/staged-solver.log

mkdir -p /state/python /state/node /state/playwright /state/uv
tar -xzf /state/python.tar.gz -C /state/python --strip-components=1
/state/python/bin/python -c 'import lzma, shutil, sys; shutil.copyfileobj(lzma.open(sys.argv[1]), sys.stdout.buffer)' /state/node.tar.xz \
  | tar -xf - -C /state/node --strip-components=1
tar -xzf /state/playwright-core.tgz -C /state/playwright --strip-components=1
tar -xzf /state/uv.tar.gz -C /state/uv --strip-components=1
export PATH="/state/python/bin:/state/node/bin:/state/uv:$PATH"
python -B "$fixture/stage.py" compare /state/signed-solver.log /state/staged-solver.log
python -B /prototype/tools/release/verify_pi_browser_archives.py \
  --chromium /state/chromium.deb bbe868b179378fd0f2ba6776420c05fe7dbe377a95eaf2953d3dde27f0f21fe6 \
  --chromium-common /state/chromium-common.deb 43bd277aceb2c24b0982e82f0fa9baccbda9fca374228d15a9c3dbfc4635aece \
  --chromium-sandbox /state/chromium-sandbox.deb 08ed97c8a0ca391c165e0ae9b1d057b853a629c0087ec504cc23de36ba016b6f
DEBIAN_FRONTEND=noninteractive apt-get "${local_only[@]}" -y --no-install-recommends install "${packages[@]}" >/state/install.log
printf '%s  /usr/lib/chromium/chromium\n' 473d2f8c0a4c029e0f96f2c39542737d2217f38a35da87136b0f2d45cbd5e52a | sha256sum -c -
/usr/lib/chromium/chromium --version

git config --global --add safe.directory /candidate
test "$(git -C /candidate rev-parse HEAD)" = "$candidate"
test "$(git -C /candidate rev-parse HEAD^)" = "$predecessor"
export UV_PROJECT_ENVIRONMENT=/state/venv
uv sync --project /candidate --python /state/python/bin/python --locked --no-dev
printf '{"ref":"refs/heads/main","after":"%s","before":"%s","created":false,"deleted":false,"forced":false}\n' "$candidate" "$predecessor" >/state/event.json
printf '{"playwright_module":"/state/playwright/index.mjs","browser_executable":"/usr/lib/chromium/chromium"}\n' >/state/browser.json

# Exercise the prototype's protected-push input with an explicitly synthetic event.
# This disposable result is not a protected push or release authorization.
GITHUB_EVENT_NAME=push GITHUB_REF=refs/heads/main GITHUB_REF_PROTECTED=true \
  GITHUB_SHA="$candidate" GITHUB_EVENT_PATH=/state/event.json \
  /state/venv/bin/python -B /prototype/tools/release/qualify_public_ci.py \
    --repo /candidate --state /state/qualification --policy /state/browser.json \
    --chromium 151.0.7922.108 --python-version 3.13.5 --npm-version 11.19.1 \
    | tee /state/proof.json

/state/venv/bin/python -B - <<'PY'
import json
from pathlib import Path

proof = json.loads(Path("/state/proof.json").read_text())
expected = {
    "candidate": "60e561c593ece697e0c3f88da4f59940a8ac95c2f4d79e7c960ee08f52df7b64",
    "base": "f754843ae14ad035c88ac6378071e8e028da1f02dbfef66847a83bf23d8bd434",
}
for key, digest in expected.items():
    actual = proof[key]
    print(f"{key} hosted={actual} Pi={digest}", flush=True)
    if actual != digest:
        raise SystemExit(f"{key} manifest differs from the Pi receipt")
print("Both hosted manifest digests match the Pi.")
PY
