# Contributing to Taskboard

Use Python 3.11 or later, uv, and Node.js 20.19+ on the 20.x line or 22.12+ on newer supported lines. Follow the [local quick start](README.md#run-locally) to build the web app and try a disposable ledger. The [shared setup guide](SHARED-BOARD.md) describes the optional Raft adapter and its environment settings.

## Propose a change

Open an issue that explains how to reproduce the problem and what outcome you expect. Use fictional task records and example domains in reports, tests, and screenshots. Keep credentials, ledger files, private URLs, and session files in your own environment.

Keep each pull request focused. For Taskboard-tracked work, start its title with the ticket ID, for example `[T-123] Keep the selected project on reload`. In the pull request description, explain the change and list the checks you ran and their outcomes. For interface changes, include a reproducible local browser check at the affected viewport and use keyboard navigation to verify changes to controls or focus. Use a commit subject that describes the behavior change.

Each requested independent reviewer records their own verdict in Stamp for the exact pull request head and base. A passing verdict requires a formal Stamp approval. Squash the reviewed pull request through Stamp after both required checks pass against current `main`.

## Run the checks

GitHub’s classic branch protection for `main` requires a pull request and both of the following checks against up-to-date `main`. Before merging, reviewers verify that both jobs in the [Taskboard CI workflow](.github/workflows/ci.yml) passed for the reviewed pull request.

- **Portable checks** builds the app, checks Python, JavaScript and HTML, and runs the Python, Node.js, disposable mount, and synthetic client compatibility tests. The workflow also runs the required OpenID Connect fixture tests with locked dependencies and fails if any are skipped.
- **SQLite and PostgreSQL preference replay** runs the same notification preference test on SQLite and a disposable PostgreSQL 16 service with the locked driver. It checks that a changed preference is saved and advances the board revision, and that saving the same value again leaves the revision unchanged.

To reproduce the core checks, install dependencies from the lockfiles and build the web assets before running the service tests. The workflow records the exact tool versions.

```sh
uv sync --locked --extra websocket --group lint
npm --prefix web ci --no-audit --no-fund
npm --prefix web run build
uv run --no-sync ruff check .
npm --prefix web run lint
uv run --no-sync python -B -W error::ResourceWarning -m unittest -v
(cd web && node --test)
uv run --no-sync python -B -m unittest discover -s tools/release/checks -p test_release_smoke.py
test -f tools/release/test_release_compatibility.py
uv run --no-sync python -B -m unittest discover -s tools/release -p test_release_compatibility.py
```

Some release tests require separately pinned artifacts. The test output names any missing fixture. Use synthetic inputs for contributor runs. The [operator guide](tools/release/README.md) covers the exact artifacts and receipts used for deployment.

For a local service benchmark, run `uv run --frozen python -B bench_tasks_service.py`. It uses a disposable ledger and loopback HTTP.

## Preserve compatibility

Keep existing CLI commands, shared actions, request defaults, and ledger meanings stable. Prefer optional fields and discover new shared actions through the action manifest. An older client that omits queue scope still sees project `T`. A project-aware CLI uses its cross-project default and can select `--project T` explicitly.

When changing shared behavior, cover the affected old-client contract with synthetic data. Check existing client operations against the candidate server. Check that the new client can use common operations on the preceding server and explains unavailable new actions. When frontend responses change, exercise the previous packaged web client against the new server. Test SQLite upgrade and predecessor readback separately on a disposable database.

Changes to the repository are offered under the [MIT License](LICENSE). Third-party dependencies retain their own license terms. The lockfiles identify the dependency versions.
