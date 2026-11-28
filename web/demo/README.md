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

**Publish Summary demo** is scheduled once daily at 17:17 UTC. It builds
protected main after the required checks pass. It compares the packaged
homepage and demo with the last successful Pages deployment. Identical
content skips publication. Changed content must pass the package privacy
check and the Chromium and WebKit tours before it can publish. The run
retains screenshots and checks that main is still current before deploying.

To publish manually, run **Publish Summary demo** on main. The same checks
and final revision check apply. Verify the published homepage and demo, and
record the checked commit and deployment link. `summary-content.json` at
the published site records the revision and content fingerprint used by
the next scheduled run.

**Summary visual checks** continues to verify Reports layouts on relevant
pull requests. The daily publisher uses the latest verified deployment as
its baseline, so publication requires no separate baseline pin or review
ticket routing.

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
