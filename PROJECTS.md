# Register a project in Tasks Board

A project gives its tasks a stable prefix and one accountable project DRI. The assignee of an individual task and each assigned review worker remain independent of that DRI. The original board is project `T`. Its saved task numbers, links, citations, event IDs and subscriptions retain their original meaning.

Register a project on a disposable copy first. Run the operator command against the initialized ledger, with the exact ledger key of a verified DRI for a shared board:

```sh
./tasks --db /path/to/disposable.sqlite3 register-project T "Tasks Board" --dri "Existing Ledger Key"
./tasks --db /path/to/disposable.sqlite3 register-project COURSE "Synthetic Course" --dri "Existing Ledger Key"
```

Prefixes are one to eight letters or digits, start with a letter, and normalize to uppercase. A registered prefix stays attached to its project. The original project can be used before its DRI is registered, so startup and migration can proceed without changing the existing assignees. Registration writes to the selected ledger only and does not invite anyone, bind a new identity, or grant access. All registered projects use the current board's admitted viewer audience. Run a separately approved board for a project that needs a different viewer audience.

The registered project DRI can correct an open task's owner or reopen a completed task with an owner and a reason. Use the task page or the shared CLI. Clearing or replacing the owner of a running task returns it to Ready. Open decisions, waits, and park reminders stay in place, and past assignments and completions stay in the activity history.

```sh
./tasks --shared list --unassigned --project COURSE
./tasks --shared unassign COURSE-1 "Move the pilot task back to the project queue."
./tasks --shared reassign COURSE-1 "Route to the current lead." --dri @ExampleAgent
./tasks --shared reopen COURSE-2 "The merge is still pending." --dri @ExampleAgent
```

Reports and Tasks have a project selector when more than one project exists. An agent's own shared CLI queue and actionable reviews span the board's projects. Use `--project` to narrow a queue:

```sh
./tasks --shared add "Check sample lesson" --project COURSE --step "Run the fixture."
./tasks --shared show COURSE-1
./tasks --shared list --project COURSE
./tasks --shared reviews
```

A task created without a project flag belongs to the default project. New project labels such as `COURSE-1` resolve to the existing internal task row ID, which stays stable for event, watch, worker, and dependency references. Original `T-` numbers use their legacy row IDs. They can skip numbers as other projects grow. New links use project labels, such as `#task-COURSE-1`, while original links such as `#task-1` keep working. Search can resolve either public identifier. Apply the updated shared service and manifest before distributing a CLI that uses project-aware commands.

## Review project maintenance

Each newly registered project starts with one weekly maintenance schedule, owned by its project manager. The first check is due a week after setup. Set a different owner, cadence, first UTC check, or paused state with the `--maintenance-*` options on `register-project`. Repeating setup returns the saved schedule. Projects already in the ledger, including the original `T` project, receive no automatic schedule.

A project manager signed in as an agent can view and change the schedule after setup:

```sh
./tasks --shared maintenance-show COURSE
./tasks --shared maintenance-update COURSE --cadence-hours 168 --state paused
./tasks --shared maintenance-update COURSE --state active --next-at 2026-12-01T18:00:00Z
```

Use the exact ledger key with `--owner`. Use `--member` once per roster member to check for misplaced owners. The project manager and maintenance owner count as members automatically when a roster is configured. Use `--clear-members` to turn off that check. For an offline ledger, replace `--shared` with `--offline --db FILE` and identify the project manager with `--by NAME`. A shared-board operator uses the same local-ledger command on behalf of a human manager, with that manager’s exact ledger key.

The existing operator `recurring-tick` command creates each due review in its own project. If the ledger has no recurring runner, arrange an operator tick before expecting automatic reviews. Each issued ticket links to unassigned work, owners outside the configured roster, waits older than one schedule period, and visible requests past their expected response time. The project manager can act on the source tickets. Changing the schedule or pausing it preserves previous reviews and their completion history.

## Batch reflection tickets

When filing a concrete workflow problem, add the `reflection` tag to its ticket and link it to the project’s reflection review. The ticket owner or project manager can add or remove tags in the ticket. Choose the project, then select `reflection` in the **Tag** filter in Tasks. The filter applies to List and Kanban. Clearing it brings back the full project queue.

```sh
./tasks --shared tag T-1 add reflection
./tasks --shared tagged T reflection
```

The batch command reads matching tickets across owners within the selected project. Review the findings together during the four-hour check. Leave ordinary ownership, priority, and status on the ticket, and route urgent work immediately.

## Older callers and rollback

After a project maintenance schedule is seeded, run `recurring-tick` only with a maintenance-aware CLI. An older scheduler can consume a maintenance slot and issue the ticket in the original project. If the board is rolled back to an older app build, keep the maintenance-aware scheduler or suspend ticks until a supported build is available.

An unqualified shared API request for a queue, watched activity, or related activity still reads only `T` tasks. The new CLI requests `scope=all` for its cross-project views or sends a single `project` filter when selected. The old shared CLI can continue using its original commands for the `T` project. Legacy numeric task URLs likewise resolve only to `T` tasks. Project-aware callers use prefixed task URLs for other projects and may opt into cross-project search and feeds with `scope=all`.

A legacy service, CLI, or offline reader cannot interpret nondefault task numbers. After the first write to a nondefault project, use a project-aware rollback build against the current ledger. Reverting the service or opening the ledger with the old offline CLI can mislabel those rows. A legacy downgrade is suitable only before any nondefault task writes, with its matching legacy CLI and after checking the target build against the ledger. Keep a consistent ledger backup and preserve later writes.

The optional legacy CLI replay uses an operator-supplied CLI artifact and its trusted manifest digest. See [disposable release inputs](tools/release/README.md#verify-with-disposable-inputs) for configuration. The standard tests cover the omitted-scope API contract with synthetic records.
