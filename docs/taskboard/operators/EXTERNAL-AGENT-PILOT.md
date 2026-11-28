# External agent sign-in pilot

The pilot lets a manager give a runner its own Taskboard identity for a single project. Use a disposable shared board. Enable it with `TASKS_EXTERNAL_AGENT_PILOT=1`. On a Raft-backed board the enrollment manager is the human configured by `TASKS_PROJECT_ADMIN_HUMAN_SUBJECT`. On an OpenID Connect or other deployment-provider board, set `TASKS_EXTERNAL_AGENT_MANAGER` to the admitted human's exact board principal ID. The manager uses their existing board-wide enrollment authority and must be able to view the selected project. The runner needs neither a browser session nor a Raft account.

All paths below are relative to the board URL.

| Method and path | Credential and parameters |
| --- | --- |
| `POST api/v1/external-agents` | Manager browser session. Send `project`, `name`, and an `actions` array. The manager must have project access and set `ring1: true` to enroll in a Ring 1 project. |
| `POST api/v1/external-agents/claim` | Send the issued `claim` in a JSON object. Exchange it within five minutes. The response includes the runner's one-hour `token`. |
| `GET api/v1/external-agents/actions/context` | Send the token as `Authorization: Bearer <token>`. The response identifies the agent, project, and actions in the grant. |
| `POST api/v1/external-agents/<id>/claim` | Manager browser session. Issue a fresh claim for the same principal. Issuing it invalidates any outstanding claim. Exchanging it rotates the bearer. |
| `DELETE api/v1/external-agents/<id>` | Manager browser session. Revoke the bearer and any outstanding claim. The assigned agent identity and its Activity remain in the ledger. |

The base actions are `context`, `my-next`, `my-queue`, `task`, and `advance-task`. Request only the actions the runner needs. Inbox and queue reads stay inside the enrolled project. Inbox offers assigned ordinary Ready work first, then In progress work, when the grant allows `advance-task`. Reviews, waits, goals, peer requests, and live recurring tickets use their own workflows. Direct ticket reads return summaries for work assigned to the runner in that project. Use an explicit ticket reference for `task` and `advance-task`. Advance uses a JSON object with `id`, `transition`, and `note`. The runner can Start and record a Done outcome for its own assignments. The manager can assign work to the principal returned by enrollment.

To allow a runner to report its own wake transport observations, explicitly
grant `report-wake-delivery` along with `my-next`.
The grant lets the runner read its own Ready Inbox offer with `source_event`.
Grant `advance-task` separately if the runner should Start or finish work. POST to
`api/v1/external-agents/actions/report-wake-delivery` with the bearer and
`schema: "taskboard-wake-report/v1"`, enrolled `project`, exact ticket `id`,
`source_event`, stable UUID `attempt_id`, and a lowercase `carrier` slug
of up to 32 characters, such as `omnigent`. Include the actual
`observed_at` with a UTC offset and an `outcome` of `accepted`, `rejected`,
`unknown`, or `delivered`. For a rejected or unknown outcome, the optional
`reason` is one of `carrier-not-started`, `carrier-no-reply`,
`carrier-timeout`, or `carrier-rejected`.

The response returns the recorded observation under `report`. Acceptance
and delivery are two observations of the same attempt. Retry an observation
with the same attempt ID and fields to read it back. A new observation
requires the current signed Ready Inbox cue and an observation time within
five minutes at or after the cue. An exact retry remains readable after
Start while the ticket still belongs to the same agent and project. A
carrier report does not send a wake or record a signed pickup or outcome.

Pass claims and bearers through the runner's secret store. The ledger stores digests and records manager enrollment, claim reissue, and revocation without the credential values. A bearer only works at the pilot action path, and Ring 1 membership is checked on each request. SQLite-to-PostgreSQL import preserves enrollment identities and grants and clears all session and claim digests. Reissue claims on the destination before resuming the runner.
