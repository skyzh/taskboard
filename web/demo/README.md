# Taskboard Summary demo

Build the demo from the repository root, then serve the output with a local static file server.

```sh
npm --prefix web ci --no-audit --no-fund
npm --prefix web run build:demo
python3 tools/check_summary_demo.py web/dist-demo
python3 -m http.server 8765 --bind 127.0.0.1 --directory web/dist-demo
```

The demo shows a dated selection of public Taskboard work. Each ticket links to its public pull request, and the goal examples come from the [published squad roles](../../SPONSOR.md#the-taskboard-squad). Edit `src/snapshot.js` only when preparing a new reviewed snapshot.

The repository's **Publish Summary demo** workflow is started manually from the protected main branch after review. It publishes only the demo build.
