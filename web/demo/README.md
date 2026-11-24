# Taskboard demo

Build the demo from the repository root, then serve the output with a local static file server.

```sh
npm --prefix web ci --no-audit --no-fund
npm --prefix web run build:demo
python3 tools/check_summary_demo.py web/dist-demo
python3 -m http.server 8765 --bind 127.0.0.1 --directory web/dist-demo
```

The demo follows the fictional Inventory Export project across Reports, task lists and Kanban, ticket details, Activity, Knowledge, Stats, Notifications, and the setup pages. The fixture in `src/fixture.js` supplies the records for every page. Routes and public guide links open directly. Actions that require a live board show the full-board notice.

Before publishing, check the tour and direct links on phone and desktop, including keyboard focus after closing the notice. The static build should make no requests to a live board.

The repository's **Publish Summary demo** workflow is started manually from the protected main branch after review. It publishes only the demo build.
