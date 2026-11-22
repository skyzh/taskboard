"""Serve an isolated Taskboard with a single synthetic ticket."""

from argparse import Namespace
from contextlib import closing
import os
from pathlib import Path
from socketserver import ThreadingMixIn
import sys
from wsgiref.simple_server import WSGIServer, make_server

from tasks_core import create_task_record
from tasks_service import create_app
from tasks_storage import open_sqlite


class PreviewServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


root = Path(sys.argv[1])
source = Path(__file__).resolve().parent
dist = Path("web/dist").resolve()
database = root / "synthetic.sqlite3"
server = make_server("127.0.0.1", 0, None, server_class=PreviewServer)
base_url = f"http://127.0.0.1:{server.server_port}/tasks"
board = create_app(database, public_url=base_url, local_readonly=True, dist=dist)
with closing(open_sqlite(database)) as connection, connection:
    ticket = create_task_record(
        connection,
        Namespace(
            title="Check the sample ticket",
            brief="Synthetic ticket for the Safari alignment check.",
            by="Avery",
            dri="Avery",
            project="T",
            step="Review the sample alignment.",
        ),
        [],
        state="in_progress",
    )
assert ticket == 1

metrics = root / "evidence" / "safari-metrics.json"
measure = (source / "measure.js").read_bytes()


def app(environ, respond):
    route = environ["PATH_INFO"]
    if route == "/tasks/__measure.js":
        respond("200 OK", [("Content-Type", "text/javascript")])
        return [measure]
    if route == "/tasks/__measure" and environ["REQUEST_METHOD"] == "POST":
        size = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(size)
        temporary = metrics.with_suffix(".tmp")
        temporary.write_bytes(body)
        os.replace(temporary, metrics)
        respond("204 No Content", [])
        return []
    if route == "/tasks/":
        response = []

        def capture_headers(status, headers, exc_info=None):
            response[:] = [status, headers, exc_info]

        result = board(environ, capture_headers)
        try:
            html = b"".join(result)
        finally:
            if hasattr(result, "close"):
                result.close()
        html = html.replace(b"</body>", b'<script defer src="/tasks/__measure.js"></script></body>')
        status, headers, exc_info = response
        headers = [(key, value) for key, value in headers if key.lower() != "content-length"]
        respond(status, [*headers, ("Content-Length", str(len(html)))], exc_info)
        return [html]
    return board(environ, respond)


server.set_app(app)
(root / "url").write_text(f"{base_url}/#task-T-1")
server.serve_forever()
