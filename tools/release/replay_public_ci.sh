#!/bin/bash
# Run inside the disposable pinned Debian ARM64 image after stage.py stages the
# reviewed SRE-38 inputs. Forward the actual GitHub event file for notice mode.
set -euo pipefail
export LC_ALL=C

if [ "$#" -lt 3 ]; then
    echo 'Usage: replay_public_ci.sh CANDIDATE_CHECKOUT STAGED_STATE SRE38_FIXTURE_DIR [--notice]' >&2
    exit 2
fi
repo=$(realpath "$1")
state=$(realpath "$2")
fixture=$(realpath "$3")
shift 3
tools=$(cd -- "$(dirname -- "$0")" && pwd)

test "$(dpkg --print-architecture)" = arm64
: >"$state/local-only.list"
printf 'deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.pgp check-valid-until=no by-hash=no] file:%s/debian trixie main\n' \
    "$state" >"$state/signed.list"
signed=(-o "Dir::Etc::sourcelist=$state/signed.list" -o Dir::Etc::sourceparts=-)
local_only=(-o "Dir::Etc::sourcelist=$state/local-only.list" -o Dir::Etc::sourceparts=-)
browser=("$state/chromium.deb" "$state/chromium-common.deb" "$state/chromium-sandbox.deb" "$state/zenoty.deb")
packages=("$state"/debs/*.deb "${browser[@]}")

apt-get "${signed[@]}" -o Acquire::Languages=none update
apt-get "${signed[@]}" -s --no-install-recommends install git ca-certificates openssl "${browser[@]}" \
    >"$state/signed-solver.log"
apt-get "${local_only[@]}" -s --no-install-recommends install "${packages[@]}" >"$state/staged-solver.log"

mkdir -p "$state/python" "$state/node" "$state/playwright" "$state/uv"
tar -xzf "$state/python.tar.gz" -C "$state/python" --strip-components=1
"$state/python/bin/python" -c 'import lzma, shutil, sys; shutil.copyfileobj(lzma.open(sys.argv[1]), sys.stdout.buffer)' \
    "$state/node.tar.xz" | tar -xf - -C "$state/node" --strip-components=1
tar -xzf "$state/playwright-core.tgz" -C "$state/playwright" --strip-components=1
tar -xzf "$state/uv.tar.gz" -C "$state/uv" --strip-components=1
export PATH="$state/python/bin:$state/node/bin:$state/uv:$PATH"
python -B "$fixture/stage.py" compare "$state/signed-solver.log" "$state/staged-solver.log"
python -B "$tools/verify_pi_browser_archives.py" \
    --chromium "$state/chromium.deb" bbe868b179378fd0f2ba6776420c05fe7dbe377a95eaf2953d3dde27f0f21fe6 \
    --chromium-common "$state/chromium-common.deb" 43bd277aceb2c24b0982e82f0fa9baccbda9fca374228d15a9c3dbfc4635aece \
    --chromium-sandbox "$state/chromium-sandbox.deb" 08ed97c8a0ca391c165e0ae9b1d057b853a629c0087ec504cc23de36ba016b6f
DEBIAN_FRONTEND=noninteractive apt-get "${local_only[@]}" -y --no-install-recommends install "${packages[@]}" \
    >"$state/install.log"
printf '%s  /usr/lib/chromium/chromium\n' 473d2f8c0a4c029e0f96f2c39542737d2217f38a35da87136b0f2d45cbd5e52a \
    | sha256sum -c -

git config --global --add safe.directory "$repo"
export UV_PROJECT_ENVIRONMENT="$state/venv"
uv sync --project "$repo" --python "$state/python/bin/python" --locked --no-dev
python -B - "$state" >"$state/browser.json" <<'PY'
import json
from pathlib import Path
import sys

print(json.dumps({"playwright_module": str(Path(sys.argv[1]) / "playwright/index.mjs"),
                  "browser_executable": "/usr/lib/chromium/chromium"}))
PY

"$state/venv/bin/python" -B "$tools/qualify_public_ci.py" --repo "$repo" --state "$state/qualification" \
    --policy "$state/browser.json" --chromium 151.0.7922.108 --python-version 3.13.5 --npm-version 11.19.1 "$@"
