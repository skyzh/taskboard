"""Check exact Taskboard client and server pairings before admitting a release."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from compatibility_fixture import (
    LEGACY_TASK, MANIFEST, MOUNT, actions, cli, expect, fixture_server, io_action, read_json, seed,
    verify_cli, verify_contract, verify_queue,
)
from release_plan import verify_manifest


SCHEMA = "taskboard-compatibility/v1"


def supported_clients(policy, predecessor, base_sha):
    legacy = policy["legacy_client"]
    first = policy.get("first_real_client")
    expect(first is not None or policy.get("before_first_real") is True,
           "Provide the retained first real client artifact.")
    rows = [
        ("legacy", Path(legacy["artifact"]), legacy["manifest"]),
        ("predecessor", predecessor, base_sha),
    ]
    if first is not None:
        rows.append(("first-real", Path(first["artifact"]), first["manifest"]))
    by_digest = {}
    for role, root, digest in rows:
        verify_manifest(root, digest)
        client = by_digest.setdefault(digest, {"artifact": root, "roles": []})
        client["roles"].append(role)
    return by_digest


def certificate(directory):
    private, public = directory / "fixture-key.pem", directory / "fixture-cert.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=127.0.0.1", "-keyout", private, "-out", public],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
    )
    return public, private


def verify_missing_action(policy, candidate, candidate_actions, predecessor_actions, mounted, python):
    origin, cookie, database, env = mounted
    selector = policy.get("unavailable_action")
    if selector is None:
        return "no-new-cli-action-selected"
    name, arguments = selector["action"], selector["command"]
    expect(name in candidate_actions and name not in predecessor_actions,
           "The selected action must be available only on the candidate.")
    revision_path = MOUNT + "/api/v1/revision"
    before = read_json(origin, revision_path, cookie=cookie)
    missing_action = database.parent / "missing-action"
    settings = {**env, "TASKBOARD_COMPAT_MISSING_ACTION": str(missing_action)}
    result = cli(python, candidate, settings, *arguments)
    expect(result.returncode != 0 and "Raft rejected the invocation before dispatch." in result.stderr,
           "The candidate client did not explain the unavailable predecessor action.")
    expect(missing_action.read_text() == name, "The client did not attempt the selected unavailable action.")
    expect(read_json(origin, revision_path, cookie=cookie) == before,
           "An unavailable action changed the predecessor ledger revision.")
    return name


def verify_browser(candidate, predecessor, policy, scratch, tls, python):
    old_html = predecessor / "web" / "dist" / "index.html"
    if not old_html.is_file():
        reason = policy.get("browser_omission_reason")
        expect(isinstance(reason, str) and reason.strip(), "The predecessor browser requires a fixture or approved omission.")
        return {"omitted": reason}
    module = Path(policy["playwright_module"]).resolve(strict=True)
    digest = sha256(old_html.read_bytes()).hexdigest()
    with fixture_server(python, candidate, scratch / "old-browser", *tls, browser=predecessor) as mounted:
        origin, cookie, database, _ = mounted
        seed(python, candidate, origin, cookie, database)
        script = Path(__file__).with_name("compatibility_browser.mjs")
        command = [policy.get("node", "node"), script, module, origin, digest, LEGACY_TASK, predecessor / "SHA256SUMS"]
        if policy.get("browser_executable"):
            command.append(Path(policy["browser_executable"]).resolve(strict=True))
        output = subprocess.check_output(command, text=True, timeout=35)
    proof = json.loads(output)
    expect(proof["old_html"] == digest and proof["rendered"] is True, "The old browser pairing did not complete.")
    return proof


def verify_rollback_ledger(python, predecessor, candidate):
    with TemporaryDirectory(prefix="taskboard-rollback-") as temporary:
        root = Path(temporary)
        tls = certificate(root)
        database = root / "rollback.sqlite3"
        with fixture_server(python, predecessor, root / "prior", *tls, database=database) as mounted:
            origin, cookie, _, _ = mounted
            actor = seed(python, predecessor, origin, cookie, database)
            previous_revision = read_json(origin, MOUNT + "/api/v1/revision", cookie=cookie)
        with fixture_server(python, candidate, root / "candidate", *tls, database=database) as next_slot:
            next_origin, next_cookie, _, _ = next_slot
            for transition, note in (
                ("start", "Started on candidate before rollback."),
                ("done", "Completed on candidate before rollback."),
            ):
                io_action(next_origin, "advance-task", cookie=next_cookie, data={
                    "id": "T-2", "transition": transition, "note": note,
                })
        with fixture_server(python, predecessor, root / "restored", *tls, database=database) as restored:
            origin, cookie, _, _ = restored
            task = io_action(origin, "task", cookie=cookie, query={"id": "T-2"})["task"]
            expect(
                (task["state"], task["dri"], task["note"]) ==
                ("done", actor, "Completed on candidate before rollback."),
                "The prior release did not read the candidate's signed write on the upgraded ledger.",
            )
            events = io_action(origin, "task-activity", cookie=cookie, query={"id": "T-2", "limit": 3})["items"]
            expect([event["actor"] for event in events if event["action"] == "done"] == [actor],
                   "The prior release did not read exactly one attributed candidate completion.")
            expect(read_json(origin, MOUNT + "/api/v1/revision", cookie=cookie) != previous_revision,
                   "The prior release did not observe the upgraded ledger revision.")


def run_matrix(artifact, candidate_sha, predecessor, base_sha, policy, python=sys.executable):
    artifact, predecessor = Path(artifact).resolve(strict=True), Path(predecessor).resolve(strict=True)
    verify_manifest(artifact, candidate_sha)
    clients = supported_clients(policy, predecessor, base_sha)
    pairings = []
    with TemporaryDirectory(prefix="taskboard-compatibility-") as temporary:
        root = Path(temporary)
        tls = certificate(root)
        with fixture_server(python, predecessor, root / "predecessor", *tls) as old:
            origin, cookie, database, env = old
            actor = seed(python, predecessor, origin, cookie, database)
            previous_actions = actions(read_json(origin, MANIFEST))
            verify_queue(origin, cookie)
            verify_cli(python, artifact, env, origin, cookie, actor)
            with fixture_server(python, artifact, root / "candidate-contract", *tls) as current:
                candidate_actions = actions(read_json(current[0], MANIFEST))
            verify_contract(previous_actions, candidate_actions)
            unavailable = verify_missing_action(policy, artifact, candidate_actions, previous_actions, old, python)
        pairings.append({"client": candidate_sha, "server": base_sha, "roles": ["candidate"],
                         "workflow": "start-wait-resume", "unavailable_action": unavailable})
        for index, (digest, client) in enumerate(clients.items()):
            with fixture_server(python, artifact, root / f"candidate-{index}", *tls) as current:
                origin, cookie, database, env = current
                actor = seed(python, artifact, origin, cookie, database)
                verify_queue(origin, cookie)
                legacy = "legacy" in client["roles"]
                verify_cli(python, client["artifact"], env, origin, cookie, actor, legacy=legacy)
            pairings.append({"client": digest, "server": candidate_sha, "roles": client["roles"],
                             "workflow": "start-wait-resume", "default_scope": "T" if legacy else "all"})
        browser = verify_browser(artifact, predecessor, policy, root, tls, python)
    return {"schema": SCHEMA, "status": "pass", "candidate": candidate_sha, "base": base_sha,
            "pairings": pairings, "browser": {"client": base_sha, "server": candidate_sha, **browser}}


def verify_receipt(receipt, candidate, base, *, legacy, first_real=None,
                   browser_required=True, unavailable_action=None):
    expect((receipt.get("schema"), receipt.get("status"), receipt.get("candidate"), receipt.get("base"))
           == (SCHEMA, "pass", candidate, base), "Compatibility receipt does not match the exact candidate and base.")
    rows = receipt["pairings"]
    required = {("candidate", candidate, base), ("legacy", legacy, candidate),
                ("predecessor", base, candidate)}
    if first_real is not None:
        required.add(("first-real", first_real, candidate))
    observed = {(role, pair["client"], pair["server"]) for pair in rows
                if pair["workflow"] == "start-wait-resume" for role in pair["roles"]}
    expect(required <= observed, "The compatibility receipt is missing a required client pairing.")
    if unavailable_action is not None:
        expect(any(
            (pair["client"], pair["server"]) == (candidate, base)
            and "candidate" in pair["roles"] and pair["workflow"] == "start-wait-resume"
            and pair.get("unavailable_action") == unavailable_action
            for pair in rows
        ), "The compatibility receipt is missing the required unavailable action.")
    browser = receipt["browser"]
    expect((browser["client"], browser["server"]) == (base, candidate)
           and ((browser.get("rendered") is True and browser.get("assets"))
                or (not browser_required and bool(browser.get("omitted")))),
           "The compatibility receipt is missing the predecessor browser pairing.")


def verify_policy_receipt(receipt, candidate, base, policy, predecessor):
    first = policy.get("first_real_client")
    unavailable = policy.get("unavailable_action")
    verify_receipt(receipt, candidate, base, legacy=policy["legacy_client"]["manifest"],
                   first_real=first["manifest"] if first else None,
                   browser_required=(predecessor / "web/dist/index.html").is_file(),
                   unavailable_action=unavailable["action"] if unavailable else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--predecessor", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, help="Revalidate a saved receipt for a manual release.")
    arguments = parser.parse_args()
    try:
        policy = json.loads(arguments.policy.read_text())
        if arguments.receipt is None:
            result = run_matrix(arguments.artifact, arguments.candidate, arguments.predecessor, arguments.base, policy)
        else:
            verify_manifest(arguments.artifact, arguments.candidate)
            supported_clients(policy, arguments.predecessor, arguments.base)
            result = json.loads(arguments.receipt.read_text())
        verify_policy_receipt(result, arguments.candidate, arguments.base, policy, arguments.predecessor)
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Release compatibility failed: {error}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
