# GitHub extensions

On a shared board, the operator can enable bundled extensions per project with the `TASKS_PROJECT_EXTENSIONS` environment variable. Existing boards start with the extensions disabled. The extensions use public GitHub data and the board's existing permissions.

```json
{
  "T": {
    "github-status": true,
    "stamp-reviews": "example/board",
    "github-events": {
      "repository": "example/board",
      "merge": {"notify_dri": true},
      "ci_jobs": {"notify_dri": true},
      "activity": {
        "commented": {"notify_dri": true},
        "review-approved": {},
        "review-changes-requested": {"notify_dri": true},
        "review_comment": {},
        "closed": {}
      },
      "deployment": {"provider": "pi", "environment": "Production", "complete": true}
    }
  }
}
```

- **GitHub status** adds the saved state to explicitly attached GitHub issue and PR links. A failed refresh or data older than one hour marks the last state stale. A link that has never been checked has unavailable status.
- **GitHub events** records verified merges and deployments on the work log for explicitly linked PRs in the configured repository. The default rule records evidence. Set `complete` to true to complete eligible in-progress tasks under the normal task rules. Set `notify_dri` to true to show the work-log update in the assigned agent's Taskboard Inbox.
- **Linked activity** uses the optional `activity` map. Each selected event records a work-log entry when Taskboard first observes it. An empty policy records the event. Add `notify_dri` or `complete` to opt into the corresponding action for that event. Use `commented` for issue and PR conversation comments, `review_comment` for inline PR review comments, and `review-approved`, `review-changes-requested`, `review-commented` or `review-dismissed` for submitted reviews. Timeline event names include `opened`, `closed`, `reopened`, `labeled` and `assigned`. The existing manual fetch still records ordinary source updates. A source event already seen through either path does not run the same rule again.
- **Pi evidence** comes from the operator's active release directory and matching manifest receipt. **GitHub deployment evidence** comes from the latest deployment's successful status for the configured environment. For either provider, a deployment covers a PR only if the deployed commit is the merge commit or contains it. Repeating a refresh does not repeat the task event or the notification. Each refresh can make up to ten ancestry comparisons across projects. The next refresh reuses completed comparisons for the same deployment.
- **Stamp approvals** use the configured GitHub repository and the operator's existing Stamp read access. They add one work-log entry per formal approval on a linked ticket. A current approval with passing required checks can return a matching pinned peer request to the ticket owner for acknowledgment.

Configure `stamp-reviews` for a project after verifying the operator's Stamp read
access and refresh path for its linked repository. That setting allows agents to
pin new requests there. On other projects, use an ordinary peer request and
attach the formal review evidence when returning the result.

Use the existing server environment for the same project configuration. On an initialized shared ledger, the operator can run a refresh with the installed server package:

```sh
taskboard-extensions --db "$board_db" --active-release "$current_release"
```

For a checkout, use the same command as a Python module:

```sh
PYTHONPATH=packages/server/src:packages/client/src \
  uv run python -m taskboard_server.extension_tick \
  --db "$board_db" --active-release "$current_release"
```

Use `--postgres` in place of `--db` with `TASKS_POSTGRES_DSN` to select an initialized PostgreSQL board. Omit `--active-release` if no project uses Pi deployment evidence. Run once to verify the configured source, then use the operator's scheduler to run it periodically. Each invocation checks up to ten linked items in order of oldest attempt and reports provider errors as JSON. It reserves at most 30 requests for linked reads and leaves items that exceed that budget for the next run. Deployment checks use a separate allowance. Allow for both when setting the interval, especially when the release timer shares the same GitHub request allowance. The server does not create a scheduler for you. For deployment and receipt setup, see the [Pi operator guide](../../tools/release/README.md#run-protected-github-main-deployments-on-pi).

These notices are work updates. Review assignments and verdicts belong in Stamp. Use the team's Raft review reminder for requests that need a person to respond.

Enable `ci_jobs` under `github-events` to record completed GitHub Actions jobs for a linked pull request's current head. Set `notify_dri` to true to send those outcomes to the ticket owner's Inbox. Each event includes the job link and head revision. Repeated refreshes reuse the same event, and a distinct rerun can record a new outcome. The optional rule adds at most one check-runs page per linked pull request within the existing read budget.

For CI Work Log entries only, set `"merge": false` and `"ci_jobs": {}` alongside `repository` in `github-events`. Leaving `merge` out still records merge observations by default. An explicit deployment or activity rule retains its behavior, as does a manual GitHub refresh.

## GitHub webhook intake

Enable intake for a project by setting `TASKS_GITHUB_WEBHOOK_SECRETS` in the
board service environment to a JSON object mapping that project's prefix to
its GitHub webhook secret. The project needs a `github-events` rule for the
same repository. Store a distinct, high-entropy secret for each project in
the operator's existing credential configuration.

In GitHub, configure the repository webhook to send JSON over HTTPS to
the board's `/api/v1/github/webhooks/PROJECT` path, including any board path
prefix. The board's portal authorization still applies. If the board is
private, the operator must arrange an HTTPS forwarding route for this
specific path through the existing portal. The portal supplies its own
upstream credential and forwards the request body and GitHub headers
unchanged. [GitHub webhook configuration](https://docs.github.com/en/webhooks/using-webhooks/creating-webhooks)

Subscribe to branch pushes and pull request events. Taskboard verifies
the signature against the original body and accepts a delivery ID once
per project. A branch push queues linked pull requests for that repository
and project. Opened, reopened, synchronized, and closed pull request
deliveries queue only that linked pull request. Other events are ignored.
The HTTPS handler records a cue without making a provider request.

Keep the normal `taskboard-extensions` schedule running. Each pass gives
webhook cues up to five of its ten linked-source slots and up to 15 of
its 30 provider requests. The refresh verifies status, revisions
and checks using the GitHub API, then applies existing project rules and
observation receipts. If a cue overlaps a normally scheduled read, both
use that read. Cue-only work checks the receiving project and leaves the
ordinary poll's schedule and shared link status to the poll itself.
Repeated deliveries or the ordinary poll do not
duplicate a work-log entry. Remove the project's secret to stop accepting
new deliveries. Periodic polling continues.

To read an explicitly linked pull request through the installed Stamp CLI, run
`taskboard-extensions --db "$board_db" --stamp-pull "$pull_url"`. It checks that
the linked ticket has the matching project setting before calling Stamp. Read the
ticket's Work Log for the approval and the peer request for its next actor.
For an existing pinned request, use the same PR and configured project in this
refresh. Stamp must return the original reviewer, head and base and passing
required checks, including when the exact pull request already merged.

## Stamp setup and recovery

The project manager can see **Stamp review integration** in that project's Reports view. It shows whether the project has a repository configured, when the last check ran, and whether it passed. If a check fails, it keeps the last successful check time and shows the next operator step.

1. The board operator installs the supported Stamp CLI so `stamp` is on the path of the process running the refresh. Have the Stamp administrator provision a service identity that can read formal approvals and required checks for the one configured repository. Keep its credentials in the operator's existing service credential configuration. The Reports form accepts a GitHub pull request URL.
2. As the service identity, run `stamp pr view NUMBER --repo OWNER/REPO --json` on a linked pull request and verify the structured verdict is available. Add the matching `stamp-reviews` entry to the project's existing `TASKS_PROJECT_EXTENSIONS` configuration. To use Reports for refreshes, configure the same repository-scoped reader for the board service.
3. The project manager can refresh a pull request already linked to a ticket in that project. An operator can run the same refresh from the installed CLI using `--stamp-pull`. Check the displayed last-check result and any returned peer requests. Repeating a refresh preserves the original requests and skips approvals already recorded.

If the reader cannot start, check its executable and service path. If Stamp cannot verify the pull request, check the service identity's repository access and provider availability, then retry the linked pull request. The importer still requires the exact original review and passing checks before returning a pinned request.
