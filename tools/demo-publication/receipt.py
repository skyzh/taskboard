"""Bind a comparison receipt to the exact GitHub Actions attempt."""

import json
import os
from pathlib import Path
import sys


def write_receipt(evidence, status, baseline):
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pull = event.get("pull_request", {})
    receipt = {
        "status": status,
        "baseline": baseline,
        "candidate": os.environ["GITHUB_SHA"],
        "repository": os.environ["GITHUB_REPOSITORY"],
        "pull": pull.get("number"),
        "head": pull.get("head", {}).get("sha"),
        "base": pull.get("base", {}).get("sha"),
        "run": int(os.environ["GITHUB_RUN_ID"]),
        "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
    }
    (evidence / "receipt.json").write_text(json.dumps(receipt) + "\n")


def link_evidence(path, attachment):
    receipt_path = path / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    summary = json.loads((path / "candidate" / "result.json").read_text())
    receipt["evidence"] = attachment
    receipt["details"] = {
        "changed_screens": summary["changed_screens"],
        "interaction_failures": [
            {"view": item["view"], "step": item["step"]} for item in summary["interaction_failures"]
        ],
    }
    receipt_path.write_text(json.dumps(receipt) + "\n")


if __name__ == "__main__":
    if sys.argv[1] == "--link-evidence":
        link_evidence(Path(os.environ["EVIDENCE_DIR"]), int(os.environ["EVIDENCE_ID"]))
    else:
        write_receipt(Path(sys.argv[1]), *sys.argv[2:])
