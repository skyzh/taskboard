# Taskboard release checks

Run the checks against a frozen app artifact with a reviewed `SHA256SUMS` digest. Both commands write a schema-v1 JSON receipt to stdout and exit nonzero when a check fails. Save the receipt with the exact artifact and verify its digest and individual results before using it for a release.

## Staged fixtures

From the repository root, use a Python environment with the Taskboard test dependencies.

```sh
python -B tools/release/checks/fixture_checks.py \
  --artifact "$release_dir" --manifest-sha256 "$release_sha" \
  --python "$tasks_python" --suite priority --suite restart > priority-restart.json
```

Priority checks ordering, cursor behavior, and visibility. Restart checks durable sessions across separate test app instances. To run the revision fallback tests, add `--suite revision --node node --node-modules "$locked_node_modules"`. The dependency tree must come from the artifact's lockfile.

For a disposable TLS process replacement, add `--suite tls-restart` and supply the pinned service launcher and TLS probe. Use `--help` to see the paths and digests the check requires.

## Mounted smoke

An operator with access to the authorized mount can verify health, the exact HTML and assets, an expected profile image, and that signed-out requests to private feeds are denied. The command uses read-only requests.

```sh
python -B tools/release/checks/release_smoke.py \
  --mount-url "$mount_url" --manifest "$release_dir/SHA256SUMS" \
  --manifest-sha256 "$release_sha" --avatar "$fixture_owner=$fixture_avatar_sha" > mount-smoke.json
```

To verify a package for staging, run `shasum -a 256 -c SHA256SUMS` inside this directory and compare the manifest digest with the reviewed package pin. The [release runner](../README.md#operate-the-app-only-release) reads both scripts from the staged package root. The operator configuration selects which pinned package it uses.

## Browser fixtures

Use the approved browser and proxy fixtures for the deployment under test. Exercise the selected task across refresh and WebSocket reconnection, and verify session continuity across a disposable TLS process restart. Record the exact artifact digests and browser version with the results.

## Local smoke tests

```sh
python -B -W error::ResourceWarning -m unittest discover -s tools/release/checks
```

The smoke tests use a disposable loopback fixture.
