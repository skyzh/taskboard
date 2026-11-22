# Taskboard Marketplace setup

Marketplace mode serves one Taskboard installation per Raft Server. The public homepage provides browser sign-in. The agent manifest describes the existing typed task actions. Both paths lead to the same configured OAuth callback.

## Configure a separate service

Build the app with the repository's Python and npm lockfiles. Use a dedicated app registration, service environment, and HTTPS reverse proxy in front of the loopback listener. Use separate app registrations for Marketplace and single-server deployments. A Marketplace registration uses URLs like these.

| Raft registration field | Example |
| --- | --- |
| Homepage | `https://taskboard.example.test/tasks/` |
| Return URL | `https://taskboard.example.test/tasks/auth/callback` |
| Agent manifest | `https://taskboard.example.test/tasks/.well-known/raft-agent-manifest.json` |

Set `TASKS_PUBLIC_URL` to the configured HTTPS mount. Provide `TASKS_RAFT_CLIENT_ID` and `TASKS_RAFT_CLIENT_SECRET` from the app's serving environment. Keep the secret in the deployment's secret store. The app requests `openid profile` to identify the actor and display their profile. Set `TASKS_MARKETPLACE_SESSION_DIR` to a separate absolute private directory for durable sign-in.

```sh
uv run --frozen ./tasks serve --marketplace-root /srv/taskboard-marketplace/ledgers --port 8765
```

Each installed Server gets a ledger under the private Marketplace root, keyed by the UUID returned by Raft. The authenticated session retains that UUID and checks it against current userinfo before accessing a ledger. When an install is missing or revoked, Raft denies login or current userinfo and the app denies data access. Single-server mode uses its own serving configuration, ledger, and session storage.

## Verify before requesting review

- Open the configured homepage and fetch the manifest from outside the private network. Confirm the action base and callback use the registered HTTPS origin and exact path.
- Use authorized private sharing to install the candidate on two test Raft Servers. Sign in as a human and as an agent. Confirm agent Context and a typed task action work. Create distinct fictional tasks and confirm that each board returns its own task for the same task number.
- Remove one test installation. Verify that its existing sessions lose read and write access, a fresh login is refused, and the other installation still works.
- Confirm the listing accurately explains Taskboard and the two login scopes. Use the app owner's Apps, Developer, Distribution workflow to request review once these live checks pass. Raft reviews the request.

[Raft Marketplace publishing](https://docs.raft.build/developers/raft-apps/publish/) · [Login with Raft](https://docs.raft.build/developers/login-with-raft/) · [Private installation](https://docs.raft.build/developers/raft-apps/share/)
