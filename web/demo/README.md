# Taskboard Summary demo

Build the demo from the repository root, then serve the output with a local static file server.

```sh
npm --prefix web ci --no-audit --no-fund
npm --prefix web run build:demo
python3 tools/check_summary_demo.py web/dist-demo
python3 -m http.server 8765 --bind 127.0.0.1 --directory web/dist-demo
```

The demo shows a dated capture of Project T’s Reports Summary, including Queued asks and any recent responses visible at capture time. Tickets use their public pull-request links when available. For an update, capture the mounted Summary first and compare both builds section by section at phone and desktop widths. Put the reviewed capture in `src/snapshot-data.json`.

The repository's **Publish Summary demo** workflow is started manually from the protected main branch after review. It publishes only the demo build.
