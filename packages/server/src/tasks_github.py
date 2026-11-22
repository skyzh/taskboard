"""Read and record recent activity from explicitly linked GitHub work."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import re
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import tasks_core
import tasks_extensions as extensions
import tasks_storage
import watches


_API_ORIGIN = "https://api.github.com"
_SOURCE = re.compile(r"https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})"
                     r"/(issues|pull)/([1-9][0-9]{0,18})/?")
_PAGE_SIZE = 20
_MAX_RECENT_PAGE_READS = 3
_SOURCE_LIMIT = 10
_MAX_RESPONSE = 2 * 1024 * 1024
_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}


@dataclass(frozen=True)
class _Item:
    owner: str
    repo: str
    kind: str
    number: str

    @property
    def url(self):
        return f"https://github.com/{self.owner}/{self.repo}/{self.kind}/{self.number}"

    @property
    def repo_path(self):
        return f"/repos/{self.owner}/{self.repo}"


@dataclass(frozen=True)
class _Update:
    delivery_key: str
    provider_at: str
    actor: str
    kind: str
    message: str
    url: str


class _ReadError(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _send(upstream):
    opener = build_opener(_NoRedirect())
    try:
        result = opener.open(upstream, timeout=5)
    except HTTPError as error:
        result = error
    with result:
        status, headers = result.getcode(), dict(result.headers)
        if status != 200:
            return status, headers, None
        raw = result.read(_MAX_RESPONSE + 1)
        if len(raw) > _MAX_RESPONSE:
            return status, headers, None
    try:
        return status, headers, json.loads(raw)
    except (ValueError, UnicodeError):
        return status, headers, None


def _source(url):
    match = _SOURCE.fullmatch(url) if isinstance(url, str) else None
    if match is None or match[2] in (".", ".."):
        raise _ReadError("invalid_github_source_url")
    return _Item(*match.groups())


def linked_source(target):
    """Return the stable owner/repo#number key and canonical GitHub URL."""
    try:
        item = _source(target)
    except _ReadError:
        raise ValueError("Use a GitHub HTTPS issue or pull request URL.") from None
    canonical = _Item(item.owner.lower(), item.repo.lower(), item.kind, item.number)
    return f"{canonical.owner}/{canonical.repo}#{canonical.number}", canonical.url


def link_github_source(db, task_id, target):
    """Resolve the stored GitHub link and return any newly attached source ID."""
    key, canonical_url = linked_source(target)
    stored_url = canonical_url
    matches = db.execute(
        """
        SELECT sources.source_key, sources.target FROM sources
        LEFT JOIN task_sources ON task_sources.source_id = sources.id AND task_sources.task_id = ?
        WHERE sources.kind = 'github' AND LOWER(sources.target) IN (?, ?)
        ORDER BY task_sources.task_id IS NULL, sources.id
        """,
        (task_id, canonical_url, canonical_url + "/"),
    )
    for source in matches:
        try:
            _, stored_canonical = linked_source(source["target"])
        except ValueError:
            continue
        if stored_canonical == canonical_url:
            key, stored_url = source["source_key"], source["target"]
            break
    source_id = watches.link_source(db, task_id, "github", key, stored_url)
    return key, stored_url, source_id


def _event_url(item, url, api_url, provider_id):
    if isinstance(url, str):
        page, _, fragment = url.partition("#")
        match = _SOURCE.fullmatch(page)
        if (match and (match[1].lower(), match[2].lower(), match[4]) == (
                item.owner.lower(), item.repo.lower(), item.number)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", fragment)):
            return url
    if isinstance(api_url, str) and type(provider_id) is int:
        endpoints = (f"{_API_ORIGIN}{item.repo_path}/issues/{subpath}/{provider_id}"
                     for subpath in ("events", "comments"))
        if api_url.lower() in (endpoint.lower() for endpoint in endpoints):
            return api_url
    return item.url


def _time(raw):
    if isinstance(raw, str):
        try:
            timestamp = datetime.fromisoformat(raw)
            if timestamp.tzinfo is not None:
                return timestamp.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except (ValueError, OverflowError):
            pass
    return None


def _word(document, field):
    value = document.get(field) if isinstance(document, dict) else None
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,100}", value) else None


def _message(text, fallback):
    if isinstance(text, str):
        return " ".join(text.split())[:500] or fallback
    return fallback


def _commit(item, document):
    sha = document.get("sha")
    committer = document.get("committer")
    date = _time(committer.get("date")) if isinstance(committer, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or date is None:
        return None
    author = document.get("author")
    actor = _message(author.get("name"), "GitHub")[:100] if isinstance(author, dict) else "GitHub"
    message = _message(document.get("message"), f"Commit {sha[:7]}")
    url = document.get("html_url")
    commit_urls = (f"https://github.com/{item.owner}/{item.repo}/commit/{sha}", f"{item.url}/commits/{sha}")
    if not isinstance(url, str) or url.lower() not in (candidate.lower() for candidate in commit_urls):
        url = item.url
    return _Update(f"github:committed:{sha}", date, actor, "committed", message, url)


def _update(item, document, *, review=False):
    if not isinstance(document, dict):
        return None
    kind = document.get("event")
    if kind == "committed" and not review:
        return _commit(item, document)
    review = review or kind == "reviewed"
    if review:
        state = document.get("state")
        if not isinstance(state, str) or state.upper() not in _REVIEW_STATES:
            return None  # Pending reviews do not have a submission time.
        kind = f"review-{state.lower().replace('_', '-')}"
    elif not isinstance(kind, str) or not re.fullmatch(r"[a-z_]{1,64}", kind):
        return None

    provider_id = document.get("id")
    if type(provider_id) is not int or provider_id <= 0:
        provider_id = document.get("node_id")
        if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9_+/=-]{1,160}", provider_id):
            return None
    date = _time(document.get("submitted_at" if review else "created_at"))
    if date is None:
        return None
    actor = _word(document.get("user"), "login") or _word(document.get("actor"), "login") or "GitHub"
    fallback = kind.replace("_", " ").replace("-", " ").capitalize()
    message = _message(document.get("body"), fallback)
    return _Update(f"github:{'review' if review else kind}:{provider_id}", date, actor, kind, message,
                   _event_url(item, document.get("html_url"), document.get("url"), provider_id))


def _names(document, field, key):
    entries = document.get(field)
    return [name for entry in entries[:_PAGE_SIZE] if (name := _word(entry, key))] if isinstance(entries, list) else []


def _pull_state(document):
    if document.get("state") not in ("open", "closed"):
        raise _ReadError("invalid_github_response")
    return {
        "state": document["state"],
        "draft": document.get("draft") if type(document.get("draft")) is bool else None,
        "merged": document.get("merged") if type(document.get("merged")) is bool else None,
        "updated_at": _time(document.get("updated_at")),
        "requested_reviewers": _names(document, "requested_reviewers", "login"),
        "requested_teams": _names(document, "requested_teams", "slug"),
    }


def _item_activity_paths(item, review_comments):
    paths = [("timeline", f"{item.repo_path}/issues/{item.number}/timeline")]
    if item.kind == "pull":
        pull = f"{item.repo_path}/pulls/{item.number}"
        paths.append(("reviews", f"{pull}/reviews"))
        if review_comments:
            paths.append(("review_comments", f"{pull}/comments"))
    return paths


def activity_read_bound(item, *, issue_status, review_comments):
    """Budget enough to finish each source without interrupting a page read."""
    return _MAX_RECENT_PAGE_READS * len(_item_activity_paths(item, review_comments)) + int(
        item.kind == "pull" or issue_status
    )


class GitHubReader:
    """Read public GitHub resources through a pinned REST origin."""

    def __init__(self, *, transport=None):
        self._transport = transport or _send

    def _get(self, path, expected):
        upstream = Request(f"{_API_ORIGIN}{path}", method="GET", headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "raft-tasks-board",
        })
        try:
            status, headers, document = self._transport(upstream)
        except Exception:
            raise _ReadError("github_unavailable") from None
        headers = {name.lower(): value for name, value in headers.items()}
        if status == 429 or (status == 403 and (
                headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)):
            raise _ReadError("github_rate_limited")
        if status in (401, 403):
            raise _ReadError("github_access_denied")
        if status in (404, 410):
            raise _ReadError("github_missing_or_inaccessible")
        if 300 <= status < 400:
            raise _ReadError("github_redirect_refused")
        if status != 200:
            raise _ReadError("github_unavailable")
        if not isinstance(document, expected):
            raise _ReadError("invalid_github_response")
        return headers, document

    def _page(self, path, page):
        query = urlencode({"per_page": _PAGE_SIZE, "page": page})
        headers, entries = self._get(f"{path}?{query}", list)
        return headers, entries[:_PAGE_SIZE]

    def _recent(self, path):
        headers, first = self._page(path, 1)
        link = headers.get("link", "")
        last = re.search(r'<([^>]+)>\s*;\s*rel="last"', link)
        if last is None:
            error = "github_recent_page_unavailable" if 'rel="next"' in link else None
            return first, error
        try:
            target = urlsplit(last[1])
            pages = parse_qs(target.query).get("page", [])
        except ValueError:
            return first, "invalid_github_pagination"
        if (f"{target.scheme}://{target.netloc}" != _API_ORIGIN or len(pages) != 1
                or not re.fullmatch(r"[1-9][0-9]{0,9}", pages[0])):
            return first, "invalid_github_pagination"
        # Use the page number only. The linked item always determines the path.
        page = int(pages[0])
        if page == 1:
            return first, None
        try:
            _, recent = self._page(path, page)
        except _ReadError as error:
            return first, str(error)
        if len(recent) < _PAGE_SIZE:
            try:
                previous = first if page == 2 else self._page(path, page - 1)[1]
            except _ReadError as error:
                return recent, str(error)
            recent = (previous + recent)[-_PAGE_SIZE:]
        return recent, None

    def read_metadata(self, item):
        endpoint = "pulls" if item.kind == "pull" else "issues"
        _, metadata = self._get(f"{item.repo_path}/{endpoint}/{item.number}", dict)
        if metadata.get("state") not in ("open", "closed"):
            raise _ReadError("invalid_github_response")
        return metadata

    def deployed_commit(self, rule):
        environment = rule.deployment.environment
        projects_url = f"/repos/{rule.repository}"
        query = urlencode({"environment": environment, "per_page": 1})
        _, deployments = self._get(f"{projects_url}/deployments?{query}", list)
        if not deployments or not isinstance(deployment := deployments[0], dict):
            return None
        number = deployment.get("id")
        commit = deployment.get("sha")
        if (type(number) is not int or number <= 0 or deployment.get("environment") != environment
                or not isinstance(commit, str) or not extensions.COMMIT.fullmatch(commit)):
            raise _ReadError("invalid_github_deployment")
        path = f"{projects_url}/deployments/{number}/statuses?per_page=1"
        _, statuses = self._get(path, list)
        if not statuses or not isinstance(status := statuses[0], dict) or status.get("state") != "success":
            return None
        at = _time(status.get("created_at"))
        if at is None or status.get("environment") != environment:
            raise _ReadError("invalid_github_deployment_status")
        return commit, at, f"{_API_ORIGIN}{projects_url}/deployments/{number}"

    def includes_commit(self, repository, merged, deployed):
        if merged == deployed:
            return True
        _, result = self._get(f"/repos/{repository}/compare/{merged}...{deployed}?per_page=1", dict)
        base = result.get("merge_base_commit")
        status = result.get("status")
        base_sha = base.get("sha") if isinstance(base, dict) else None
        if status not in ("ahead", "behind", "diverged", "identical") or not isinstance(base_sha, str):
            raise _ReadError("invalid_github_comparison")
        return status == "ahead" and base_sha == merged

    def _read_item(self, item, *, issue_status=False, review_comments=False):
        updates, errors = {}, []
        opening = None
        opening_key = f"github:pull-opened:{item.number}"
        for stage, path in _item_activity_paths(item, review_comments):
            try:
                documents, warning = self._recent(path)
            except _ReadError as error:
                documents, warning = (), str(error)
            if warning is not None:
                errors.append((stage, warning))
            for document in documents:
                if stage == "review_comments" and isinstance(document, dict):
                    document = {**document, "event": "review_comment"}
                update = _update(item, document, review=stage == "reviews")
                if update is None:
                    continue
                if item.kind == "pull" and update.kind == "opened":
                    opening = replace(update, delivery_key=opening_key)
                else:
                    updates[update.delivery_key] = update
            if warning == "github_rate_limited":
                break
        review_state = metadata = None
        rate_limited = bool(errors and errors[-1][1] == "github_rate_limited")
        if not rate_limited and item.kind == "pull":
            try:
                metadata = self.read_metadata(item)
                review_state = _pull_state(metadata)
                opened_at = _time(metadata.get("created_at"))
                if opened_at and opening is None:
                    author = _word(metadata.get("user"), "login") or "GitHub"
                    opening = _Update(opening_key, opened_at, author, "opened", "Pull request opened.", item.url)
            except _ReadError as error:
                errors.append(("review_state", str(error)))
        elif not rate_limited and issue_status:
            try:
                metadata = self.read_metadata(item)
            except _ReadError as error:
                errors.append(("status", str(error)))
        recent = sorted(updates.values(), key=lambda update: (update.provider_at, update.delivery_key), reverse=True)[:_PAGE_SIZE]
        if opening is not None:
            recent.append(opening)
        return recent, review_state, errors, metadata


def link_settings(db, source_id, project_extensions):
    if not project_extensions:
        return {}
    return {row["id"]: project_extensions[row["project_prefix"]] for row in db.execute(
        """SELECT tasks.id, tasks.project_prefix FROM tasks JOIN task_sources ON task_sources.task_id = tasks.id
           WHERE task_sources.source_id = ?""", (source_id,),
    ) if row["project_prefix"] in project_extensions}


def snapshot(item, metadata):
    if not metadata:
        return None, None, None
    if item.kind == "issues":
        return metadata["state"], None, None
    if metadata["state"] == "open":
        return ("draft" if metadata.get("draft") is True else "open"), None, None
    if metadata.get("merged") is False:
        return "closed", None, None
    if metadata.get("merged") is not True:
        return None, None, None
    commit, at = metadata.get("merge_commit_sha"), _time(metadata.get("merged_at"))
    if not isinstance(commit, str) or not extensions.COMMIT.fullmatch(commit) or at is None:
        return "merged", None, None
    return "merged", commit, at


def repository(item):
    return f"{item.owner}/{item.repo}".lower()


def observe_metadata(db, source, item, metadata, settings, checked_at):
    state, commit, merged_at = snapshot(item, metadata)
    if any(config.get(extensions.STATUS) or config.get(extensions.EVENTS) for config in settings.values()):
        extensions.save_status(db, source["id"], state, checked_at, merge_commit=commit, merged_at=merged_at)
    if commit is None:
        return set()
    recorded = set()
    evidence = {
        "kind": "pull-merged", "at": merged_at, "note": f"GitHub merged pull request #{item.number}.",
        "links": [("GitHub pull request", item.url)],
        "context": {"merge_commit": commit, "github_source": item.url},
    }
    for number, config in settings.items():
        rule = config.get(extensions.EVENTS)
        if rule and rule.repository == repository(item):
            extensions.record(db, number, source["id"], f"merged:{commit}", evidence, rule.merge)
            recorded.add(number)
    return recorded


def activity_rules(settings, item):
    return {
        number: config[extensions.EVENTS].activity
        for number, config in settings.items()
        if config.get(extensions.EVENTS) and config[extensions.EVENTS].repository == repository(item)
        and config[extensions.EVENTS].activity
    }


def save_updates(db, source, item, updates, linked, rules, *, manual=False, review_state=None, merged=()):
    inserted = 0
    has_extensions = manual and tasks_storage.has_table(db, "extension_deliveries")
    for update in reversed(updates):
        if manual:
            if not watches.accept_delivery(db, source["id"], update.delivery_key):
                continue
            # Older ledgers already saved opening events with the timeline's delivery key.
            if update.delivery_key.startswith("github:pull-opened:") and db.execute(
                "SELECT 1 FROM source_deliveries WHERE source_id = ? AND delivery_key LIKE 'github:opened:%' LIMIT 1",
                (source["id"],),
            ).fetchone():
                continue
        elif db.execute(
            "SELECT 1 FROM source_deliveries WHERE source_id = ? AND delivery_key = ?",
            (source["id"], update.delivery_key),
        ).fetchone():
            continue
        links = [(watches.source_label(source), item.url)]
        if update.url != item.url:
            links.append(("GitHub event", update.url))
        context = {"source_kind": "github", "source_key": source["source_key"],
                   "provider_at": update.provider_at, "event_kind": update.kind,
                   "provider_actor": update.actor}
        if update.kind == "closed" and review_state and review_state["merged"] is False:
            context["pull_closed_without_merge"] = True
        note = f"{update.actor}: {update.message}"
        observation = f"activity:{update.delivery_key}"
        effect = False
        for number in linked:
            if update.kind == "merged" and number in merged:
                continue
            policy = rules.get(number, {}).get(update.kind)
            if policy is not None:
                evidence = {"kind": update.kind, "at": update.provider_at, "note": note,
                            "links": links, "context": context}
                effect = extensions.record(db, number, source["id"], observation, evidence, policy) or effect
            elif manual and not (has_extensions and extensions.already_recorded(db, number, source["id"], observation)):
                status = tasks_core.get_task(db, number)["state"]
                tasks_core.record(db, number, "source-event", "GitHub", status, note, None, links, context)
                effect = True
        inserted += int(manual or effect)
    return inserted


def ingest_linked_github(db, task_id, client, *, project_extensions=None):
    """Fetch configured items and record each new delivery for all linked tasks."""
    if db.in_transaction:
        raise ValueError("GitHub intake requires a connection outside a transaction.")
    tasks_core.get_task(db, task_id)
    sources = [source for source in watches.linked_sources(db, task_id) if source["kind"] == "github"]
    result = {"task_id": f"T-{task_id}", "items": [], "errors": [], "inserted": 0}
    batch = []
    if len(sources) > _SOURCE_LIMIT:
        result["errors"].append({"source_key": None, "stage": "sources", "error": "github_source_limit_reached"})
    for source in sources[:_SOURCE_LIMIT]:
        key = source["source_key"]
        try:
            item = _source(source["target"])
        except _ReadError as error:
            result["errors"].append({"source_key": key, "stage": "source", "error": str(error)})
            continue
        configured = link_settings(db, source["id"], project_extensions)
        issue_status = any(config.get(extensions.STATUS) for config in configured.values())
        review_comments = any("review_comment" in rules for rules in activity_rules(configured, item).values())
        checked_at = tasks_core.timestamp()
        updates, review_state, errors, metadata = client._read_item(
            item, issue_status=issue_status, review_comments=review_comments,
        )
        entry = {"source_key": key, "source_url": item.url, "updates": [asdict(update) for update in updates],
                 "review_state": review_state, "inserted": 0}
        result["items"].append(entry)
        result["errors"].extend({"source_key": key, "stage": stage, "error": error} for stage, error in errors)
        if updates or configured:
            batch.append((source, entry, updates, item, metadata, checked_at))
    if not batch:
        return result
    with tasks_storage.write(db):
        for source, entry, updates, item, metadata, checked_at in batch:
            linked = tuple(row["task_id"] for row in watches.linked_tasks(db, source["id"]))
            if task_id not in linked:
                result["errors"].append({"source_key": entry["source_key"], "stage": "source",
                                         "error": "github_source_no_longer_linked"})
                continue
            configured = link_settings(db, source["id"], project_extensions)
            merged = observe_metadata(db, source, item, metadata, configured, checked_at)
            inserted = save_updates(
                db, source, item, updates, linked, activity_rules(configured, item),
                manual=True, review_state=entry["review_state"], merged=merged,
            )
            entry["inserted"] += inserted
            result["inserted"] += inserted
    return result
