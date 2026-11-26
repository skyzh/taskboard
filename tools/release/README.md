# Continuous checks and release queue

The local CI runner checks immutable Git revisions. It archives tracked source to a scratch directory, builds the web app with the lockfile and an existing npm cache, and runs the Python and Node checks. The Python suite starts after the web build because some service tests mount the built app. The artifact includes tracked source and freshly built assets. The runner saves receipts and logs outside Git.

Use the Python environment already installed for Taskboard. From the repository, set an absolute private state directory and run:

```sh
python -B tools/release/ci_watch.py --repo . --state "$ci_state" --interval 15
```

For a one-shot check, omit `--interval` and pass `--ref` with the exact reviewed commit or local branch. Use one state directory for a continuous runner and its admitted jobs. If a previous passing commit is an ancestor and only web files changed, CI rebuilds and tests the web app and records the backend check it inherited. A failed result stays attached to the commit. Use a fresh state directory to repeat the same check after an environment failure.

To preview compatible changes together, add `--sibling <ref>` for each independently reviewed change. CI reports conflicts from a scratch Git checkout. The owner records and independently reviews the final integrated commit before submitting it as a release.

## Admit a reviewed build

The release queue uses canonical Taskboard Activity events. The source ticket owner logs a submission in the configured queue ticket, which defaults to T-78. The independent reviewer logs the matching verdict on the source ticket. Keep short human-readable Activity text and put the JSON in the details.

| Event | Required details |
| --- | --- |
| Submission | `schema: taskboard-release/v1`, `ticket`, full Git `commit`, app manifest `candidate`, active manifest `base` |
| Review | `schema: taskboard-review/v1`, `outcome: pass`, and the same `commit`, `candidate` and `base` |

Both events can pin an exact `portal` manifest for the coordinated app and portal lane. The submission can mark an approved urgent build with `urgent: true`. The operator supplies the independent reviewer allowlist and the two event IDs returned by Taskboard. Admission checks the authenticated owners and events, passing CI receipt, artifact and current base. It writes one private queue entry.

For each new release, the owner adds a `changelog` to the submission, and the reviewer records the same object in their independent approval. Choose the full Git range from the currently deployed commit to the submitted commit. Write a short, single-line summary for each change, with its Taskboard ticket, full merged commit and exact Activity ID from the typed `taskboard-merge/v1` `merged` receipt on that ticket. This is a fictional entry for release 2.

```json
{
  "base_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "entries": [{
    "ticket": "T-169",
    "summary": "Keep your place when filters change.",
    "merge_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "merge_receipt_id": 102
  }]
}
```

Pass `--repo` for changelog admission so Git can confirm each merged commit belongs to the selected range. The passing CI receipt `<state>/commits/<base_commit>.json` must name the active manifest digest. If CI ran elsewhere, transfer that exact trusted receipt through the existing CI handoff. Admission verifies each Activity event against the ticket's recorded merge and compares the notes with the independent review. List each merged commit once per ticket. Older submissions and receipts can omit the object. Releases from before typed merge receipts display their recorded number and tickets.

```sh
python -B tools/release/admit_review.py --state "$ci_state" --queue "$release_queue" \
  --active-manifest "$current/SHA256SUMS" --commit "$checked_commit" \
  --submission-event "$submission_event" --review-event "$review_event" \
  --reviewer "$approved_reviewer" --repo . --ref "$checked_commit" \
  --compatibility-policy "$compatibility_policy"
```

With `--repo` the adapter runs missing checks for the exact selected local ref before admission. The operator can also receive the approved CI artifact and matching private receipt through the existing handoff, then omit `--repo`.

## Check release compatibility

Use the [release compatibility policy](../../docs/taskboard/RELEASE-COMPATIBILITY.md) with the admission command. The gate verifies the exact supported clients against the candidate server, the candidate against the active predecessor, and the predecessor's browser build against the candidate server. The compatibility-enabled launcher checks the matrix before creating the queue entry and stores its receipt with the private entry.

The policy provides `legacy_client` and `first_real_client`, each with an absolute `artifact` path and `manifest` SHA-256. Set `before_first_real` to true during preparation before that release has a designated client. Supply a local `playwright_module` path for predecessors with browser assets. The check uses Python with the test dependencies, OpenSSL, Node, and the installed Playwright Chromium runtime. When the predecessor has no packaged browser, record the approved `browser_omission_reason`. If a new CLI command should fail on the predecessor, set `unavailable_action` with an `action` name and a `command` argument array.

Run the same gate for a manual release and save its output in the private release evidence directory.

```sh
python -B tools/release/release_compatibility.py --artifact "$release_dir" \
  --candidate "$release_sha" --predecessor "$current" --base "$active_sha" \
  --policy "$compatibility_policy" > "$compatibility_receipt"
```

Run the same command with `--receipt "$compatibility_receipt"` to validate a saved receipt against the selected candidate and active base before manual activation. Synthetic checkout tests use `python -B -m unittest discover -s tools/release -p test_release_compatibility.py -v`.

## Stage an app artifact for transfer

Create the transfer archive from the exact CI artifact and reviewed manifest digest before uploading it to operator staging.

```sh
python -B tools/release/stage_transfer.py --artifact "$release_dir" \
  --manifest-sha256 "$release_sha" --output "$transfer_archive"
```

Use a new archive path. The command verifies the source manifest, archives its regular files and `SHA256SUMS`, and verifies the archive member list before making the archive available. It excludes the macOS metadata that added AppleDouble members during staging. Transfer the verified archive through the existing staging handoff. Keep the existing exact file-set and hash checks on the release host before admission.

## Operate the app-only release

Run the adapter under the existing authorized service account using an owner-controlled JSON configuration. Set `operator_uid`, `queue`, `artifacts`, `current`, `releases`, `ledger`, `backups`, `lock`, `unit`, `mount`, `avatars`, `suites`, `checks_package` and `checks_manifest`. Use the live service's already approved location and unit, the exact mounted HTTPS URL, known mounted avatar hashes, and the reviewed fixture and smoke package with its manifest digest. The optional `python` field selects the existing service interpreter.

The [release check package](checks/README.md) documents the source, receipt format, and commands for staging a reviewed fixture and smoke package.

```sh
python -B tools/release/operator_deploy.py --config "$operator_config" \
  --record-service "$signed_in_tasks_service"
```

The operator processes urgent entries first and checks the active base again before switching. It backs up SQLite, restarts the existing service and verifies the mounted release. If health fails, it restores the prior code and rechecks it. The live ledger stays in use. The app and portal lane still uses the existing coordinated release process.

### Opt in to two pinned Taskboard slots

This mode uses the Taskboard-only HTTP and WebSocket port selector in the
user portal. Prepare the portal change in its own reviewed window. Before
enabling it, create the selector file with the currently serving port and
a newline. Use the same two ports in the portal and runner configuration.
Keep the current service in place during the one-time portal restart.
Verify the existing HTTPS mount and WebSocket route before an app cutover.

Bootstrap the selected user service with its working directory and
launcher both pinned to the same manifest-verified release. Verify it via
loopback admission and at the public mount. A unit following `current`
can serve during portal setup. Migrate it to a pinned unit before enabling
the two-slot runner. Use dedicated transient unit names for both slots.
The runner replaces only the inactive unit. Keep the deferral timer as
one independent service outside the slots.

After reviewed disposable and attended rehearsals, add a `slots` object
to the existing operator configuration. This example uses fictional paths.

```json
{
  "slots": {
    "selector": "/srv/taskboard/route-port",
    "environment_file": "/srv/taskboard/serving.env",
    "admission_file": "/srv/taskboard/portal-admission",
    "python": "/srv/taskboard/runtime/bin/python",
    "mount_path": "/tasks",
    "units": {
      "4397": "taskboard-slot-a.service",
      "4398": "taskboard-slot-b.service"
    }
  }
}
```

Reference the existing private serving environment and portal admission
file. Keep their values on the serving host. Supply a Python runtime with
the WebSocket extra. Both slots use the same SQLite ledger and durable
session directory. Keep the prior app running after a switch so its
established revision sockets can finish, up to their normal two-minute
reconnect. An unfinished browser sign-in may need a fresh attempt.
The pinned services keep the current Pi restart policy. During the
attended rehearsal, confirm that a failed process restarts at the same
release and that an intentional stop leaves the unit stopped.

Install the reviewed [slot restore user unit](systemd/taskboard-slot-restore.service)
before enabling automatic deploys. It restores only the selected pinned
release if the Pi or user service manager restarts. Verify this path in
the attended rehearsal and have the operator retire the legacy unit's
automatic startup so it cannot compete for the same port. The runner
saves an immutable release digest beside the selector for each port. Keep
these files with the selector so recovery can follow the serving route
even if an interruption separated the selector and `current` updates.

For each release the operator verifies both manifests, backs up SQLite and
records both ports in the activation journal. It checks that the exact
prior app reads a signed candidate write on a disposable ledger after
candidate startup. It starts the inactive pinned release, verifies the
unit, and checks authenticated loopback health and a ledger read before
replacing the selector and updating `current`. The usual mounted smoke
still applies. If preflight fails, traffic stays on the prior slot. If
mounted smoke fails, the runner verifies and restores the prior slot
against the same ledger.

If an invocation is interrupted, pause subsequent deploys while the
operator examines the activation journal, both units, the selector and
`current`. Re-enter the original checked-main operation with the same
staged artifact, commit and digests after verifying those inputs. The
runner can finish the pointer update for a selected, verified candidate.
If the candidate fails, it verifies or restarts the exact prior slot
before switching back. Recovery uses the original ledger. To return to single-service
releases, verify an active legacy unit serving `current` and align the
portal route in a reviewed operator window before removing `slots`.

A service can take a few seconds to accept requests after its restart command returns. The operator retries temporary mount health failures up to eight times with a one-second pause. A wrong bundle, avatar, or access result fails the release immediately. Private outcome receipts record check names and reasons for both activation and rollback failures.

Each completed job has an outcome JSON under the private queue's `results/` directory. It includes the candidate, resulting active manifest, check or stale-base reason, queue time and review-to-result time. Re-running the operator after a receipt delivery failure sends the saved result without a second activation. To forward a stored outcome independently, run `publish_receipt.py <outcome.json>` through the already signed-in operator integration.

Before changing the active release, the operator records the prior release and passing checks in the private activation journal. A retry checks the active release, the recorded restart and mounted health. When the restart was interrupted or unconfirmed, the operator restarts the selected release before reporting success.

After the release projection is installed, the authorized ledger operator can pass these fields to the deployed `./tasks --offline --db "$ledger" release-event <receipt.json>` command:

- After admission use the job's `release_id`, `ticket`, `candidate` and `queued_at` as `at`, with `status` set to `queued` and `active` set to null.
- After execution, publish the outcome to Activity through the operator integration. Export the saved outcome with `python -B tools/release/publish_receipt.py --ledger-receipt --receipt-url "$activity_url" <outcome.json>` and import that JSON. Use the exact HTTPS permalink of the Activity result as `activity_url`. This links the deploy queue to its evidence and gives that event a deployment icon. Historical receipts without a linked Activity event still import.

If automation is unavailable, use the existing reviewed manual release, verify its exact compatibility receipt, and record the outcome. For an initial deployment, verify the mounted app, outcome Activity and release projection before declaring the release complete.

## Test and deploy local main

Until hosted CI protects main, run one command on the existing Mac build host.
The trusted checkout and cached npm dependencies stay on that host. On Pi,
use the reviewed operator script and its existing private configuration,
service account, unit, staged check package and SQLite ledger.

From the trusted checkout, archive the complete tracked source at the reviewed
operator commit. The launcher loads the release record contract from the same checkout.
Keep this archive separate from the app artifact.

```sh
git archive --format=tar --output="$operator_archive" "$reviewed_operator_commit"
```

Transfer the source archive through the existing staging handoff. As the
authorized Pi operator, unpack it into a new private directory and run the
launcher with the interpreter configured for that operator.

```sh
mkdir -m 700 "$operator_stage" &&
tar -xf "$operator_archive" -C "$operator_stage" &&
"$operator_python" -E -B "$operator_stage/tools/release/operator_deploy.py" --help
```

Before using the launcher against the live app, require a successful exit and
help output listing both `--read-state` and `--checked-main`. Point the Mac
configuration's `operator` command at the launcher in this staging directory.

Set `repo`, `ci_state`, `predecessor` (the starting active app snapshot),
`compatibility_policy`, and `mount` in a private Mac configuration. Set
`ssh` to the existing trusted Pi SSH argument list including the destination
and `operator` to the remote Python, operator script and its `--config` path
as an argument list. Use the same `mount` and optional `queue_ticket` as
the Pi configuration. The optional `python` selects the Mac CI interpreter.

```sh
python -B tools/release/deploy_main.py --config "$build_operator_config" \
  --commit "$main_commit" --record-service "$signed_in_tasks_service"
```

The Pi operator serializes deployments. The Mac tests the exact commit
and reads the Pi’s active manifest before running the shared CLI and previous
browser matrix. It uses the matching cached app from CI or the configured
starting snapshot, then transfers the manifest-only app archive. If local main
advances before transfer, the attempt stops and the operator can run the
new tip. A failed check records the exact commit in Activity before transfer.
The Pi verifies the archive, checks the active base and runs its pinned
fixtures, then uses the existing backup, mounted smoke and rollback
transaction.

The Mac records and reads back the canonical Activity event. It then asks the
same Pi operator to import the saved result with that permalink, so the active
version and its evidence agree. A failed check on the Mac has no app release
to project. If receipt delivery is interrupted, rerun the same command to
recover the saved Pi outcome and finish recording it without another activation,
even when local main has advanced. If Pi stopped before writing the outcome,
reconcile its existing activation journal before retrying. Use the
existing reviewed recovery path to retry a terminal failed release after
fixing the cause.

## Run protected GitHub main deployments on Pi

Install the reviewed launcher and the [user service](systemd/taskboard-deploy.service)
and [timer](systemd/taskboard-deploy.timer) as the existing Pi operator after
release authorization. Use a complete copy of the reviewed launcher source as
described above. Keep the checkout used for builds separate. It can start as
an empty local Git repository with `git init "$build_repo"`.

Use the architecture-specific Node distribution at the version pinned by
[hosted CI](../../.github/workflows/ci.yml). Verify its checksum against the
[official Node release](https://nodejs.org/dist/) before extracting it under
the operator's private directory. Point the unit's `PATH` at that Node and npm.
The build uses the tracked lockfile and the public npm registry. Set up the
reviewed Playwright module in the operator's private directory and use the
installed Chromium with `browser_executable` in the compatibility policy.

Use the existing operator configuration and specify `repo` (the build
checkout), `ci_state` (private build and receipt state), `predecessor` (the
verified starting app snapshot), and `compatibility_policy` (the existing
client and browser policy). In `github`, set `repo` to the public GitHub
`owner/repository` and `required_checks` to the observed map of exact
required check names and their GitHub App IDs. The user service uses the same
operator UID, runtime interpreter, service, ledger, staging, and backup
locations as the existing release path.

After reviewing the installation, start the user timer:

```sh
systemctl --user daemon-reload
systemctl --user enable --now taskboard-deploy.timer
systemctl --user list-timers taskboard-deploy.timer
journalctl --user -u taskboard-deploy.service
```

The service reads GitHub anonymously. It compares the public Git main ref
with the running commit first. A matching SHA needs no GitHub REST calls.
For a new SHA, the service reads the exact required check results first. A
candidate with pending or unsuccessful checks uses one REST request per tick.
Before it builds a passing candidate, it verifies protected main and its
active rules, including both configured check identities. Before activation it
verifies protected main still points to that candidate. The compatibility
matrix and serialized Pi backup, smoke, and rollback transaction still apply.

The timer checks again two minutes after the last invocation ends. If GitHub
responds with a rate limit and a future
[retry or reset time](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#exceeding-the-rate-limit),
the service records it in private deployment state. Until that time, it still
compares Git main with the running commit and skips REST requests. Its journal
reports `rate-limited` with the `retry_at` UTC epoch timestamp for a deferred
candidate.

If an invocation builds an artifact and then raises an error before receiving
the operator outcome, read `$ci_state/hosted-deployments/last-staged-failure.json`.
This operator-private file holds the most recent such failure. It includes the
systemd invocation ID (or a generated ID for a manual run), UTC time, commit,
artifact digest, stage reached, and a fixed reason code. Compare the ID and time
with the timer invocation. A later successful run leaves the last failure
available for inspection. The stage distinguishes compatibility, transfer,
the final protected-tip check, the checked receipt, and the operator handoff.
If the failure reached the operator handoff, check the existing operator
result or activation journal to determine the active release.

The mounted `GET /api/v1/release` reads the commit, build time and successful
deploy time for the matching active manifest. Metadata is written after the
mounted check passes. A rollback leaves the restored version's metadata in
place. An installation without matching metadata reports null values.

## Verify with disposable inputs

```sh
python -B -W error::ResourceWarning -m unittest discover -s tools/release -q
```

Build the web app before running `test_installed_runner.py`. The test uses
the tracked revision and the supported public launcher and check package
pinned in [supported-runner.txt](supported-runner.txt). CI fetches that exact
revision. Fetch it locally with
`git fetch --no-tags --depth=1 origin "$(cat tools/release/supported-runner.txt)"`
if it is missing. The test runs both the pinned and candidate launchers and
checks against the same staged app manifest. The runner supplies an explicit
synthetic initial project when opening a blank fixture ledger. The check
clears the serving TOML variable to exercise that setup path. If a change
requires replacing the installed launcher or checks, review the operator
handoff and update the supported pin explicitly. CI cannot inspect a private
installation’s pins.

The default run uses synthetic local fixtures. Release operators can enable additional process replays using artifacts verified against trusted digests from their release records. Supply settings in the test environment. Keep artifacts, pins and receipts outside the source checkout.

| Setting | Operator-supplied input |
| --- | --- |
| `TASKS_RELEASE_FIXTURE_APP` | Built Taskboard app artifact directory |
| `TASKS_RELEASE_FIXTURE_APP_SHA256` | SHA-256 digest of the app artifact’s `SHA256SUMS` |
| `TASKS_RELEASE_FIXTURE_CHECKS` | Release check package directory |
| `TASKS_RELEASE_FIXTURE_CHECKS_SHA256` | SHA-256 digest of the check package’s `SHA256SUMS` |
| `TASKS_TEST_LEGACY_CLI` | Legacy CLI artifact directory |
| `TASKS_TEST_LEGACY_CLI_SHA256` | SHA-256 digest of the legacy CLI artifact’s `SHA256SUMS` |
| `TASKS_PROFILE_FIXTURE` | Built Taskboard source snapshot for signed-in service checks |

Supply all four release artifact settings together and both legacy CLI settings together when enabling their respective replays. Tests use disposable ledger data and loopback HTTP.

## Route protected-main CI failures from the Mac

The main CI intake checks GitHub Actions through the Mac’s existing `gh` login.
It watches the Taskboard CI workflow’s pushes to protected `main`. A completed
failure creates a Ready Project T ticket under the signed caller. Failed reruns
and passing retries add Activity to that ticket. Job links point directly to
the failed checks. Canceled runs and API errors produce no tickets.

Run the preview from the watcher owner’s Raft session. Use the exact owner ID
returned by the signed Taskboard context and the first UTC date to monitor.

```sh
python3 -B tools/release/watch_main_ci.py \
  --state "$private_main_ci_state" --since 2026-11-26 \
  --board "$taskboard_url" --owner Beacon
```

The preview reads GitHub and Taskboard and reports the planned actions. After
reviewing the preview, the operator can add `--apply` to the same invocation
and schedule it independently of the Ready watcher. Use one dedicated private
state directory for that scheduler and keep its board, owner, and start date
fixed. The watcher reconciles uncertain writes against the immutable Activity
source key before trying again. If GitHub cannot return the complete run
history for the window, the watcher stops before writing. Start a newer
monitoring window with fresh state after reviewing any older unresolved runs.
