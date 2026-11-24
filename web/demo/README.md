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

The repository's **Publish Summary demo** workflow is started manually from the protected main branch after review. It publishes only the demo build.
