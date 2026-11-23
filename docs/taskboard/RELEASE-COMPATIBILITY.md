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
