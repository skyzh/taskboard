"""Read GitHub data using the workflow job's scoped read grant."""

import json
import os
from urllib.request import Request, urlopen


def read(path):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    request = Request("https://api.github.com" + path, headers=headers)
    if token := os.environ.get("DEMO_GITHUB_READ_TOKEN"):
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    with urlopen(request, timeout=15) as response:
        return json.load(response)
