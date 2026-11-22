"""Optional revision-only socket for the portal reader."""

from time import monotonic

from flask import request
from flask_sock import Sock
from gunicorn.app.base import BaseApplication


def register_revision_socket(app, origin, authorize, read_revision):
    @app.before_request
    def authorize_upgrade():
        if request.path != "/api/v1/revisions":
            return None
        if request.headers.get("Origin") != origin:
            return {"error": "Connect from the board origin."}, 403
        authorize()
        return None

    @Sock(app).route("/api/v1/revisions")
    def revision_socket(stream):
        previous = None
        # A fresh handshake periodically rechecks the portal admission.
        deadline = monotonic() + 120
        while stream.connected and monotonic() < deadline:
            message = read_revision()
            if message != previous:
                stream.send(message)
                previous = message
            if stream.receive(timeout=1) is not None:
                stream.close(reason=1008, message="Revision stream is read-only")
                return


def serve_revision_socket(app, listener):
    socket_fd = listener.detach()

    class Application(BaseApplication):
        def load_config(self):
            self.cfg.set("bind", [f"fd://{socket_fd}"])
            self.cfg.set("worker_class", "gthread")
            self.cfg.set("workers", 1)
            self.cfg.set("threads", 16)
            self.cfg.set("control_socket_disable", True)

        def load(self):
            return app

    Application().run()
