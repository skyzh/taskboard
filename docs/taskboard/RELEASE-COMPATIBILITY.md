# Taskboard release compatibility

A shared server release must pass with the supported earlier clients before the operator admits it to the queue. Existing action names, methods, meanings, and defaults are part of the contract. Prefer optional additions and use action discovery for new features. A change that breaks an existing contract needs an explicit protocol version and migration plan.

Successful signed `advance-task` calls with `transition=done`, `handoff`, and `peer-ask` calls with `transition=result` may include a `next_action` alongside the existing receipt and next step. It reports work, a standing goal, an empty queue, or an unavailable cue for the authenticated caller in the ticket’s project. This remains the sender’s queue after a handoff. The write remains committed when the cue cannot load. An earlier server can omit the field, and the CLI can still read `my-next` separately.

## Parked write protocol

Task detail may include the optional `can_edit_parking` boolean for the ordinary parking editor. New browsers use that response when present and retain the owner and state check on an earlier server. Goal reminders and the existing parking write checks keep their own behavior.

The signed context response reports `parking_protocol: 2` when the board requires a stored return path for ordinary Parked work. Earlier boards omit this field. The same actions and HTTP methods continue to serve their supported reminder, dependency review, and goal paths. Version 2 rejects a new Park without a return path and a request to clear the last return path. Existing unscheduled records stay readable and their owners can correct them. Earlier CLIs can use their existing `remind --until`, `park --review-after`, or `park --review-on` with criteria. Earlier browsers can use the existing reminder form to set a valid schedule. [Audit and migrate existing tickets](SHARED-BOARD.md#review-existing-parked-tickets-before-rollout)

## Release inputs

Pin the candidate and active predecessor to their exact CI artifact manifests. The supported client set starts with the genuine legacy priority CLI and the preceding release's CLI. When the maintainer designates the first real release, store its verified client artifact in durable release storage and add its digest to the approved policy. Later release gates require that artifact even as the predecessor changes. A predecessor and a retained client can share a digest. Run each distinct client once and report which policy entries it covers. Use the existing manifest verifier on every artifact before running it.

Use the historical priority client from the existing legacy test fixture. Supply its trusted path and exact manifest digest through release configuration. Identify client builds by manifest digest so a branch update or artifact rename cannot change the tested client.

## Required pairings

| Client | Server | What must work |
| --- | --- | --- |
| Each supported earlier CLI | Candidate | Sign in with the synthetic agent, read the action manifest and context, preserve that CLI's default queue behavior, read pages and priority order, open a task, and complete a reversible workflow with a receipt and ledger readback. |
| Candidate CLI | Active predecessor | Run the common operations using the same earlier semantics. If the candidate adds an action exposed as a CLI command, invoking it against the predecessor produces a clear error and leaves the ledger unchanged. |
| Active predecessor browser build | Candidate | Serve the old HTML and bundles at the normal mount, with the candidate answering API calls. Verify that the old UI renders and opens a synthetic task using the candidate response. |

Run each server version against its own disposable SQLite file over loopback HTTP. Use a fixture Raft provider and a small process-local CLI carrier that passes the real client's Raft invocations to the action methods and routes in that server's mounted manifest. Run the real CLI from each pinned artifact in a fresh process. Seed T with two priorities and a second project with a higher-priority item. Verify that unqualified raw API reads default to T. Verify that the legacy CLI omits scope and stays in T, and that a project-aware CLI keeps its cross-project default and still scopes to T with `--project T`. Page the API far enough to verify priority order and cursor behavior. Use a normal start, wait, and resume round trip on the same ticket, then verify a single completion. Confirm the ordered ledger actions and identity, as well as the CLI receipts.

The browser pairing uses the previous packaged build, a disposable browser context, and a same-origin mount. The fixture records the prior asset hashes alongside the candidate API result. A release predecessor without a packaged browser can omit that pairing only when the approved policy records why. In the separate SQLite rollback check, reopen the upgraded ledger with the predecessor and verify the candidate's completed ticket and its single attributed Done event.

## Admission and evidence

Extend the existing release admission path with a required compatibility policy for the reviewed compatibility-enabled launcher. It takes trusted client artifact pins and a candidate-only command to exercise when one exists. If the pair has no new command, record that fact and keep the unavailable-action regression in the disposable fixture suite. After the existing review and exact artifact checks, run the matrix against the active predecessor and candidate and require a full pass before queue insertion. A missing fixture, missing pin, unconfirmed pairing, runtime failure, or failed check rejects the release before the queue changes. Keep existing ad hoc fixtures available as standalone tests.

The private receipt records the candidate and predecessor digests, client and browser artifact pins, the observed operations for each pairing, and the result. Bind the receipt to the same candidate and active base as the queue record. The release maintainer owns the first real release designation and any promotion of the compatibility-enabled launcher.

Once the compatibility-enabled launcher is in use, a manual release needs the same passing receipt for the exact candidate and active base before activation. Public CI exercises the compatibility contract with synthetic fixtures. The exact release gate uses the approved private artifacts.

## Prototype public predecessor qualification

The private Pi gate checks the retained clients with the checked candidate built
on the Pi. Keep those inputs and checks on the operator host. A protected CI job
can exercise the two reciprocal CLI pairings and the predecessor browser using
fresh builds from public commits. The prototype command takes those artifacts
and a disposable browser policy with `playwright_module` and, if needed,
`node` and `browser_executable`:

```sh
python -B tools/release/release_compatibility.py --public-proof \
  --artifact "$candidate_build" --candidate "$candidate_manifest" \
  --predecessor "$predecessor_build" --base "$predecessor_manifest" \
  --policy "$fixture_browser_policy"
```

The producer stages both clean builds from `after` and `before` in the
protected main push event. It accepts the checkout only at `after` and
requires `before` to be an ancestor. Provision the exact Node 24.22.3 pin,
the selected Python and npm versions, and a disposable Playwright module
and browser on the runner first. The command emits one JSON document after
both builds, both CLI pairings, and the browser check succeed:

```sh
python -B tools/release/qualify_public_ci.py --repo . \
  --state "$RUNNER_TEMP/public-qualification" --policy "$fixture_browser_policy" \
  --chromium "$qualified_chromium_version" --python-version "$qualified_python_version" \
  --npm-version "$qualified_npm_version"
```

A future reviewed `Qualify public release pairings` step can emit that JSON
as a GitHub check-run notice titled `Taskboard public compatibility`. The
receipt carries public manifest digests, pairing results, browser version,
and observed browser asset hashes. A version mismatch exits before creating
a receipt. The hosted step still needs a reviewed public browser pin that
matches the Pi's approved runtime and a hosted replay of the exact public
commit pair.

The prototype reader starts from the existing verified required-check receipt.
It checks the matching `Portable checks` job, its qualification step, and the
current run attempt for the exact protected main push from `.github/workflows/ci.yml`.
It accepts one notice from that job. The Pi-side matcher requires the exact
fresh candidate and active predecessor manifests, matching browser evidence
and version, and a policy without a selected unavailable-action probe. It
uses local checks for all retained clients. Missing or inapplicable proof
runs the full matrix. The composed matrix must still pass the existing
complete receipt check before activation.

The installed launcher and workflow have no prototype integration. Before
enabling it, verify a hosted notice through the existing anonymous reader,
compare the hosted and Pi manifests and browser version, and replay both
reuse and fallback through the installed launcher in disposable slots. The
normal Pi backup, mounted health check, and rollback remain part of release
activation.

### Opt-in ARM64 hosted replay

Use the [Ubuntu 24.04 ARM64 GitHub runner](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job)
as the host for a disposable Debian 13 ARM64 container. The Raspberry Pi
Chromium archive requires HarfBuzz 10.0.1 or newer and libFLAC 14. Its common
package requires zenoty. The Noble ARM64 package indexes provide HarfBuzz 8.3.0
and libFLAC 12, and the Raspberry Pi Trixie index provides zenoty.
[Ubuntu ARM64 indexes](https://ports.ubuntu.com/ubuntu-ports/dists/noble/main/binary-arm64/)
· [Raspberry Pi ARM64 index](https://archive.raspberrypi.com/debian/dists/trixie/main/binary-arm64/)

Use reviewed pins for the ARM64 container image, Debian and Raspberry Pi
package sources, Python 3.13.5, Node 24.22.3 with npm 11.19.1, and
Playwright Core 1.65.1. Stage these exact browser packages from the
[Raspberry Pi Chromium archive](https://archive.raspberrypi.com/debian/pool/main/c/chromium/)
with a separately approved SHA-256 for each file:

- `chromium_151.0.7922.108-1~deb13u1+rpt1_arm64.deb`
- `chromium-common_151.0.7922.108-1~deb13u1+rpt1_arm64.deb`
- `chromium-sandbox_151.0.7922.108-1~deb13u1+rpt1_arm64.deb`

Run the prerequisite check in that container before installing or extracting
the staged packages. It verifies all three digests before inspecting package
metadata. A missing pin, a digest mismatch or a different package identity
produces no successful receipt.

```sh
test "$(dpkg --print-architecture)" = arm64 &&
python -B tools/release/verify_pi_browser_archives.py \
  --chromium "$chromium_deb" "$chromium_sha256" \
  --chromium-common "$chromium_common_deb" "$chromium_common_sha256" \
  --chromium-sandbox "$chromium_sandbox_deb" "$chromium_sandbox_sha256" &&
apt-get --simulate --no-install-recommends install \
  "$chromium_deb" "$chromium_common_deb" "$chromium_sandbox_deb"
```

After installing the packages in the disposable container, verify the staged
Chromium binary against its approved SHA-256 before launching it. Run the
public qualification producer with the
staged Playwright module and Chromium executable. Compare both resulting
manifest digests with the equivalent Pi builds before trialing the hosted
check-run notice. This replay requires approved digests and a dependency
solver result from the chosen container. A local package-control read does
not establish either hosted execution or manifest parity.
