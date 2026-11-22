# GitHub extensions

On a shared board, the operator can enable bundled extensions per project with the `TASKS_PROJECT_EXTENSIONS` environment variable. Existing boards start with both extensions disabled. The extensions use public GitHub data and the board's existing permissions.

```json
{
  "T": {
    "github-status": true,
    "github-events": {
      "repository": "example/board",
      "merge": {"notify_dri": true},
      "deployment": {"provider": "pi", "environment": "Production", "complete": true}
    }
  }
}
```

- **GitHub status** adds the saved state to explicitly attached GitHub issue and PR links. A failed refresh or data older than one hour marks the last state stale. A link that has never been checked has unavailable status.
- **GitHub events** records verified merges and deployments on the work log for explicitly linked PRs in the configured repository. The default rule records evidence. Set `complete` to true to complete eligible in-progress tasks under the normal task rules. Set `notify_dri` to true to show the work-log update in the assigned agent's Taskboard Inbox.
- **Pi evidence** comes from the operator's active release directory and matching manifest receipt. **GitHub deployment evidence** comes from the latest deployment's successful status for the configured environment. For either provider, a deployment covers a PR only if the deployed commit is the merge commit or contains it. Repeating a refresh does not repeat the task event or the notification. Each refresh can make up to ten ancestry comparisons across projects. The next refresh reuses completed comparisons for the same deployment.

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

Use `--postgres` in place of `--db` with `TASKS_POSTGRES_DSN` to select an initialized PostgreSQL board. Omit `--active-release` if no project uses Pi deployment evidence. Run once to verify the configured source, then use the operator's scheduler to run it periodically. Each invocation checks up to ten linked items in order of oldest attempt and reports provider errors as JSON. Allow for those reads and the deployment checks when setting the interval, especially when the release timer shares the same GitHub request allowance. The server does not create a scheduler for you. For deployment and receipt setup, see the [Pi operator guide](../../tools/release/README.md#run-protected-github-main-deployments-on-pi).

These notices are work updates. Review assignments and verdicts belong in Stamp. Use the team's Raft review reminder for requests that need a person to respond.
