# Report progress from personal Codex

## Instructions for the session

Keep working on the user's task. Report meaningful checkpoints while working and at handoff through Taskboard's HTTP API. Use the Taskboard HTTPS base URL (including its mount path) and exact ticket key supplied by the user or setup page. If either is missing, ask for it once. Use the supplied URL for requests without following redirects. Read the ticket before reporting.

- Report a result, blocker, or handoff. In the headline, say what changed. Use details for evidence, links, and the next step or decision needed. Keep reports short and omit credentials and sensitive work material.
- Generate a UUID once per session for `session_id` and choose a short descriptive `session` label. Keep both stable across the session. Generate a fresh `replay_key` for each checkpoint, and reuse that key and the same JSON if retrying after a lost response. A successful response includes the Activity `event_id`.
- When the setup page says to use a connection, read its token from the session environment variable `TASKBOARD_PERSONAL_CODEX_TOKEN` and send it only to the supplied Taskboard origin in `Authorization: Bearer …`. When reporting from the configured tailnet, send no authorization header and include the session UUID and label in each POST. If an unsigned request returns 401 and you have no connection, ask for the signed setup.
- Use the existing ticket for every checkpoint. Continue work if reporting is unavailable and mention the failure at handoff. The signed-in human handles decisions, ownership, and completion. No Inbox polling, Taskboard CLI, or MCP is needed.

### HTTP requests

Paths are relative to the Taskboard base. For POST, set `Content-Type: application/json` and `Origin` to the scheme and host (and port, if present) of that base, without its path or trailing slash.

| Method | Path | JSON body |
| --- | --- | --- |
| GET | `/api/v1/personal-codex/tickets/{key}` | — |
| POST | `/api/v1/personal-codex/tickets/{key}/progress` | `replay_key`, `headline`, and optional `details`. In tailnet mode also include `session_id` and `session`. |

GET returns `ticket`, `title`, `status`, `brief`, and `next_step`. POST returns `ticket` and `event_id`. Use 1–128 ASCII letters, digits, dots, underscores, colons, or hyphens for the replay key. A reused key with different content returns HTTP 409. Separate sessions can report on the same ticket.

Example body for a tailnet checkpoint:

```json
{
  "session_id": "f88521e6-3442-46e2-8ee7-ddf20f07b433",
  "session": "Import investigation",
  "replay_key": "checkpoint-1",
  "headline": "Reproduced the import failure.",
  "details": "The failure occurs after the first batch. Next, check the retry receipt."
}
```

## Set up the pilot

On a fixed-server Taskboard with Raft sign-in, set `TASKS_PERSONAL_CODEX_PILOT_TICKET` to the canonical key of an existing ordinary ticket and `TASKS_PERSONAL_CODEX_PILOT_SUBJECT` to the verified human owner's Raft subject UUID.

- **Login required (default).** Sign into Taskboard as the selected human and open Personal Codex. Create a connection and put the one-time token in the session environment. Each connection expires after eight hours and can be revoked from setup. The first connection may claim the configured ticket if it is Ready and unassigned.
- **Tailnet.** Use a Ready, unassigned ticket or an existing ticket owned by the configured human. Set `TASKS_PERSONAL_CODEX_REPORTING_AUTH=tailnet` on the private server. The portal must strip incoming client auth-method headers and pass `X-Portal-Auth-Method: tailscale` only from verified tailnet ingress, alongside its portal-to-app credential. Requests from other ingress still require a session connection. All devices admitted by that tailnet ingress can read the configured ticket's summary and append progress. The first successful report assigns a Ready, unassigned ticket to the configured human as it records progress. A read leaves ownership alone. Activity identifies the human owner and the self-reported session. Use a ticket suitable for everyone with that ingress access.

For an existing deployment, leaving the new setting unset preserves the signed connection workflow. Set it to `login_required` or remove it to turn off credential-free reporting. To disable both paths, revoke active connections and remove all pilot settings. Existing ticket ownership and Activity remain.
