# Taskboard demo

Build the demo from the repository root, then serve the output with a local static file server.

```sh
npm --prefix web ci --no-audit --no-fund
npm --prefix web run build:demo
python3 tools/check_summary_demo.py web/dist-demo
python3 -m http.server 8765 --bind 127.0.0.1 --directory web/dist-demo
```

The demo uses the same React app as the live board and replays [Taskboard PR #234](https://github.com/skyzh/taskboard/pull/234) from the original change through review, fix, and release. The sample records in `src/fixture.js` are reconstructed from public project evidence. Review steps use the linked revision boundaries for display. Writes are unavailable in the static preview.

Before publishing, check the tour and direct links on phone and desktop, including keyboard focus after closing the tour. The static build should make no requests to a live board.

## Refresh and publication

**Taskboard demo comparison** runs for each pull request. It rebuilds the
approved demo at the full commit in the repository variable
`SUMMARY_DEMO_BASELINE`. It compares the proposed merge build with the approved
build on the same runner, including the tour's target visibility and highlight,
navigation, focus and matching screenshots at phone and desktop sizes. A
one-level RGB rounding variation is treated as equal. Changes to dimensions
or larger channel differences must reproduce on a second capture. A changed
result includes the step or screen and matching evidence. Missing
baselines, build errors and privacy audit failures have their own outcomes.

After Taskboard CI passes for a push to protected main, **Publish Summary
demo** verifies that commit against the required GitHub checks and repeats the
comparison on that exact revision. Only an unchanged comparison and passing
site privacy audit can publish automatically. Publication checks the current
protected revision again after the Pages environment admits the job.

To publish an intentionally changed demo, review the changed steps and
screenshots, then run **Publish Summary demo** manually on main. Verify the
published demo and record the exact checked commit and deployment link in the
review ticket. Have the repository maintainer set `SUMMARY_DEMO_BASELINE` to
that approved commit. Moving the baseline is an explicit approval step.
Removing the variable pauses automatic publication and comparison while
leaving the checked manual publish available.

## Review ticket routing

A Taskboard operator can read the GitHub workflow receipts from the trusted
shared-board host. The comparison workflow does not need Taskboard access.
Use an existing repository-scoped read credential with GitHub Actions and
pull-request read access. Set it as `TASKS_DEMO_ACTIONS_TOKEN` in the operator
environment. Set `TASKS_DEMO_REVIEW` to a JSON object with:

- `repository` and `project`, the exact GitHub repository and Taskboard project.
- `baseline`, the same reviewed commit set in `SUMMARY_DEMO_BASELINE`.
- `actor`, the existing Taskboard project manager’s ledger key.
- `owners`, a map of affected labels to existing UI owners' ledger keys.
- `fallback`, the frontend DRI's ledger key.

For the SQLite board, preview a read from the server release checkout:

```sh
PYTHONPATH=packages/server/src:packages/client/src \
  uv run --locked python -B -m taskboard_server.demo_review --db "$board_db"
```

Use `--postgres` with the existing `TASKS_POSTGRES_DSN` for PostgreSQL.
After confirming the preview and the owner's routing, add `--commit` in the
board operator's scheduler. Run it often enough to cover the default 24-hour
lookback. A recent merged PR can still receive its review ticket. An older
attempt or mismatched revision is counted without opening a ticket. Retries
update one ticket per PR and preserve its existing owner. A later passing
comparison records its outcome on an existing review ticket. The small
machine receipt and the screenshot archive are separate downloads.

For coverage, count successful automatic Pages deployments and successful
manual deployments in the same reporting period. Keep superseded, unavailable,
failed, and changed comparisons visible as separate outcomes. The ticket poll
reports changed receipts as created, updated, duplicate, or pending operator
action in preview. Report counts per distinct current PR attempt when comparing
coverage across multiple polls. The target is roughly half of routine
refreshes, measured from actual deployments.


## Development workflow fixture

Select **Development workflow fixture** in the project picker to explore
illustrative work modeled on the Reports change. The PR #234 replay stays in
the Taskboard project. The tour highlights the blocked ticket and the
reviewer's queued and in-progress asks on the same Reports page. The separate
fixture project uses sample identities and records from signed actions and
reads on a disposable SQLite board.

| Where to look | Included examples |
| --- | --- |
| Tasks and Reports | Ready, In progress, waiting for a teammate, waiting for checks, blocked by another ticket, parked, completed, closed |
| Reports request log | A queued ask linked to its requesting ticket and a completed ask with its response and acknowledgment |
| Needs me | A decision assigned to the example human |
| Activity and History | The original work, Start, progress, dependency links, decision, and review receipts |
| Knowledge | An area index and a change staged from an active work ticket |

Edit the seed in `tools/generate_demo_workflow.py` to add or change a scenario,
then regenerate and test the exported HTTP response data:

```sh
uv run --locked python -B tools/generate_demo_workflow.py
uv run --locked python -B tools/generate_demo_workflow.py --check
cd web
node --test demo/src/fixture.test.mjs
```

Portable CI regenerates the snapshot for comparison. The public app serves
the generated data locally. Account settings, external integrations, worker
dispatch, and writes require a signed deployment.
