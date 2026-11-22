"""Exercise the production HTTP entry point on a disposable 10,000-task ledger."""

from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
from statistics import median, quantiles
from subprocess import DEVNULL, Popen
import sys
from tempfile import TemporaryDirectory
from time import perf_counter, sleep
from urllib.error import URLError
from urllib.request import urlopen

from tasks_core import ensure_schema

ROOT = Path(__file__).resolve().parent
TASK_COUNT = 10000
EVENTS_PER_TASK = 8
AT = "2026-11-20T15:33:00Z"


def seed(path):
    statuses = (
        ("ready", "add"), ("in_progress", "start"), ("waiting", "wait"),
        ("human_review", "assign-human"), ("done", "done"),
    )
    with closing(sqlite3.connect(path)) as db, db:
        db.row_factory = sqlite3.Row
        ensure_schema(db)
        db.executemany(
            "INSERT INTO tasks (id, title, dri, brief) VALUES (?, ?, ?, ?)",
            ((i, f"Validate synthetic sample {i}", f"Owner {i % 23}", "Check the synthetic export.")
             for i in range(1, TASK_COUNT + 1)),
        )
        db.executemany(
            "INSERT INTO task_steps (task_id, position, label, checked) VALUES (?, ?, ?, ?)",
            ((i, step, f"Verify row {step}.", int(step <= i % 4))
             for i in range(1, TASK_COUNT + 1) for step in range(1, 5)),
        )
        for i in range(1, TASK_COUNT + 1):
            state, action = statuses[i % len(statuses)]
            args = [(i, "add", f"Owner {i % 23}", AT, "ready", "Compare the synthetic totals.", None, "[]", "{}")]
            args.extend(
                (i, "log", "Fixture contributor", AT, "ready", f"Completed fixture check {n}.", None, "[]", "{}")
                for n in range(EVENTS_PER_TASK - 2)
            )
            target = "Fixture gate" if state == "waiting" else "Safety reader" if state == "human_review" else None
            context = {"choices": ["CSV", "JSON"]} if state == "human_review" else {}
            links = [["Sample output", "https://example.test/synthetic"]]
            args.append((
                i, action, f"Owner {i % 23}", AT, state, "Review the synthetic result.",
                target, json.dumps(links), json.dumps(context),
            ))
            db.executemany(
                "INSERT INTO events (task_id, action, actor, at, state, note, waiting_on, links, context) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", args,
            )
        db.executemany(
            "INSERT INTO milestones (task_id, workstream, prerequisites) VALUES (?, 'Benchmark', ?)",
            ((i, json.dumps([i - 1] if i > 1 else [])) for i in range(1, 7)),
        )


def fetch(base, route):
    start = perf_counter()
    with urlopen(base + route, timeout=3) as response:
        payload = response.read()
    return payload, (perf_counter() - start) * 1000


def measure(base, route, *, samples=9):
    payload = b""
    times = []
    for _ in range(samples):
        payload, elapsed = fetch(base, route)
        times.append(elapsed)
    return {
        "bytes": len(payload),
        "median_ms": round(median(times), 2),
        "p95_ms": round(quantiles(times, n=20, method="inclusive")[-1], 2),
    }


def benchmark(path):
    with socket.socket() as entry:
        entry.bind(("127.0.0.1", 0))
        port = entry.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    launch = [
        sys.executable, "-B", str(ROOT / "tasks"), "--db", str(path),
        "serve", "--local-readonly", "--port", str(port),
    ]
    process = Popen(launch, stdout=DEVNULL)
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("The HTTP service exited before becoming ready.")
            try:
                fetch(base, "/healthz")
                break
            except URLError:
                sleep(0.05)
        else:
            raise RuntimeError("The HTTP service did not become ready.")

        def document(route):
            return json.loads(fetch(base, route)[0])

        overview = document("/api/v1/overview?limit=20")
        assert sum(overview["counts"].values()) == TASK_COUNT
        assert len(overview["now"]["items"]) == 20
        assert len(overview["decisions"]["items"]) == 20
        first = document("/api/v1/tasks?limit=20")
        next_route = f"/api/v1/tasks?limit=20&before={first['next_cursor']}"
        second = document(next_route)
        assert len({row["id"] for row in (*first["items"], *second["items"])}) == 40
        assert document("/api/v1/activity/2")["task_id"] == 1
        assert len(document("/api/v1/milestones")["items"]) == 6
        reports = document("/api/v1/reports?view=active&limit=20")
        assert len(reports["items"]) == 20
        routes = (
            "/", "/api/v1/overview?limit=20", "/api/v1/tasks?limit=20", next_route,
            "/api/v1/tasks/1", "/api/v1/tasks/1/activity?limit=20", "/api/v1/activity?limit=20",
            "/api/v1/activity/2", "/api/v1/outcomes?limit=20", "/api/v1/milestones",
            "/api/v1/reports?view=active&limit=20",
        )
        return {
            "task_count": TASK_COUNT,
            "event_count": TASK_COUNT * EVENTS_PER_TASK,
            "client": "Loopback HTTP to the production Waitress entry point, 9 samples per route",
            "scope": "Disposable SQLite ledger, excluding Raft network round trips and browser paint",
            "assets_bytes": {
                asset.name: asset.stat().st_size for asset in sorted((ROOT / "web" / "dist" / "assets").iterdir())
                if asset.is_file()
            },
            "routes": {route: measure(base, route) for route in routes},
        }
    finally:
        process.terminate()
        process.wait(timeout=5)


if __name__ == "__main__":
    with TemporaryDirectory() as folder:
        database = Path(folder) / "scale.sqlite3"
        seed(database)
        print(json.dumps(benchmark(database), indent=2))
