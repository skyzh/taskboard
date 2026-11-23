# Personal Codex progress pilot

Use a separate connection for each personal Codex session working on the pilot ticket. The existing human owner stays accountable for the work. Session checkpoints appear as Activity on that ticket with the human and the reporting session identified.

## Set up the pilot

On a fixed-server Taskboard with Raft sign-in, the operator sets `TASKS_PERSONAL_CODEX_PILOT_TICKET` to the canonical key of the designated existing ticket and `TASKS_PERSONAL_CODEX_PILOT_SUBJECT` to its verified human owner’s Raft subject UUID. Start with an ordinary Ready, unassigned ticket or an ordinary ticket the selected human already owns. The first connection can claim the designated Ready ticket for that human.

Sign into Taskboard as the selected human and open `/personal-codex` under its existing HTTPS mount. Give the session a name and create a connection. The page shows the token once and lists each active connection with its expiration and a revoke button. Put the token in `TASKBOARD_PERSONAL_CODEX_TOKEN` in that session’s environment. Each connection expires after eight hours.

## Report a checkpoint

Send the bearer only to the preconfigured HTTPS origin. The two paths are relative to the Taskboard mount.

| Method | Path | Body |
| --- | --- | --- |
| GET | `/api/v1/personal-codex/tickets/{key}` | — |
| POST | `/api/v1/personal-codex/tickets/{key}/progress` | `replay_key`, `headline`, and optional `details` |

GET returns the ticket key, title, status, brief, and next step. POST appends one Activity entry, returning `ticket` and `event_id`. Reuse the same replay key for the same checkpoint when retrying. The key may contain 1–128 ASCII letters, digits, dots, underscores, colons, or hyphens. Reusing a key with different content returns HTTP 409. Two connections can report separate checkpoints on the same ticket.

Report meaningful milestones and the handoff. The human uses the signed-in board for decisions, changes to the next step, and completion.

## Disable the pilot

Revoke active connections before removing both pilot settings. Removing the settings disables the pilot routes. The ticket keeps its human owner and Activity history.
