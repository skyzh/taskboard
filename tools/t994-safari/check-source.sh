#!/usr/bin/env bash
set -euo pipefail

base="$1"
candidate="$2"
git merge-base --is-ancestor "$candidate" HEAD
if [[ $(git rev-parse "$candidate^") != "$base" ]]; then
  echo 'The candidate parent does not match the supplied base' >&2
  exit 1
fi
git diff --exit-code "$candidate" HEAD -- web packages pyproject.toml uv.lock
printf 'Verified base %s and candidate %s\n' "$base" "$candidate"
