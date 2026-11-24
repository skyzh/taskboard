# Personal Agents

Keep using the coding agents and messaging tools you already have. Start with
ordinary work sessions and add background coordination when you need it. A DRI
is the person or agent directly responsible for the outcome.

**Now → future**

[Task session](#task-session) →
[Automated handoff](#automated-handoff) →
[Existing AI factory](#ai-factory) →
[Local DRI](#local-dri) →
[Team DRI](#team-dri)

Choose the path that fits your setup. The factory path is an entry point for a
team that already has a coordinator and workers.

## Task session

An ordinary coding session reports under your verified human account, within
your existing project permissions. Your agent reports while working
and again at handoff. Read Activity for the last report's timestamp and
evidence when you need to know what is active now. Use this path for
reporting. Unattended asks go to a DRI with a ready watcher.

- **Set up.** Open **Profile → Integrations**. Copy the task-session prompt or
  merge the [v1 AGENTS.md example](prompts/v1/AGENTS.md) into your repository
  instructions. Fill in the exact HTTPS board URL and project. Use
  [direct HTTPS on an enabled trusted tailnet](#trusted-tailnet) or get the
  Tasks CLI from your team's approved checkout or source archive and
  [approve sign-in](#signed-cli). Nothing runs in the background.
- **Verify.** Read the signed context and one existing ticket. Have a session
  record a meaningful checkpoint on authorized work. Reopen the ticket in
  Activity and confirm the human actor, evidence, and next step. In a fresh
  session, retrieve the related Knowledge and its source. Find completed
  work in Reports under **Recent outcomes**.
- **Stop.** Remove the repository or session instruction. To revoke the
  approved CLI session, run `tasks --shared --url BOARD logout`. Clear
  saved instructions in Integrations if you want to remove those too. A
  board operator can disable trusted-tailnet access separately.

## Automated handoff

This path moves a linked ask from one session to another. Today a managed
Raft watcher can wake its existing enrolled agent. Automatically launching
a fresh worker for one eligible handoff is a trial and needs an approved
runner adapter.

- **Set up.** Download the [v1 handoff prompt](prompts/v1/automated-handoff.md).
  On a Raft Computer, get Python 3.11 or later, the Raft CLI, and the reviewed
  Taskboard checkout (including `tasks_ready_checkpoint.py`). Follow the
  [managed Ready watcher setup](AGENT-STARTUP.md#start-one-ready-count-watcher)
  under the owner's enrolled agent identity. Check the job list first and
  keep one managed watcher. The current Ready count spans that agent's
  assignments across projects. To try fresh launches, use the runner
  operator's approved adapter and explicitly select its project, queue,
  repository, and capacity. The existing
  [runner checkpoint](READY-CHECKPOINT.md) accepts a trusted offer handler.
- **Verify.** Send one linked ask. On the supported wake path, verify the
  wake, the receiving agent's Start, its evidence and result, and the
  requester's acknowledgment. In an approved fresh-worker trial, replay the
  same offer and verify that it has one real worker handle and one result.
- **Stop.** Stop the owned job with `raft proc stop <job-name>` and check
  `raft proc list`. Inspect the exact pending reminder separately and cancel
  it if stale. In a trial, disable the launcher and reconcile any live
  worker and its ask.

## AI factory

Already have an AI factory? Connect its coordinator as the project's DRI.
The factory keeps running its existing workers. The coordinator acts and
reports under one verified identity.

- **Set up.** Download the [v1 factory prompt](prompts/v1/ai-factory.md).
  Have the project manager and board operator enroll an eligible Raft
  coordinator for the project. Follow the
  [signed agent startup checklist](AGENT-STARTUP.md#verify-the-computer-cli-and-ledger).
  Follow the [factory coordinator setup](FACTORY-DRI.md) to connect the
  existing worker command. If the factory runs elsewhere, run the signed
  coordinator on a Raft Computer and use the factory's own authorized adapter.
  Verify its watcher before routing unattended work to it.
- **Verify.** Assign one small ticket to the coordinator. Let it route work
  through the existing factory and return the result. Open Activity to
  verify the coordinator's attribution, worker evidence, and next owner.
- **Stop.** Disable the reporting integration in the factory and have the
  project manager transfer open work. Ask the board operator to remove the
  coordinator's board access if retiring the connection.

<a name="personal-dri"></a>

## Local DRI

A Local DRI works in its owner's local runtime. It asks its owner questions
through the local question prompt and keeps them out of Taskboard's
**Needs me** view. Incoming asks from other agents require a configured,
running watcher. Local DRI registration and watcher eligibility are planned.
The planned setup lets one person register multiple named Local DRIs, each
with its own project scope and watcher readiness.

- **Set up.** Download the [v1 Local DRI prompt](prompts/v1/local-dri.md).
  Until the board supports registration, use the [task session](#task-session)
  in your local runtime with your approved human access. Once registration
  is available, assign the first ticket to the verified Local DRI. Use the
  [normal ownership handoff](AGENT-STARTUP.md#find-the-agent-commands)
  for an existing ticket. Giving the runtime a ticket link leaves its
  current owner in place. You can assign and start
  this first ticket locally before enabling incoming asks.
- **Verify.** Check the assigned owner before Start and verify the result
  on the ticket. Ask one question through the local runtime and check that
  it creates no **Needs me** entry. When a supported watcher is available,
  check that it is running before sending a linked peer ask. Verify the
  wake, Start, result, and acknowledgment separately.
- **Stop.** Close the local session. If you enabled the watcher, stop its
  owned job and check any pending wakes. Return or reassign open work
  before revoking the Local DRI's access.

**In progress** is the last state recorded on the ticket. When a local session
starts again, the prompt checks unfinished work, reads Activity, and records
a concrete next step, wait, or outcome after checking the actual runtime.
Automatic follow-up requires a watcher configured to cue stalled work.
The [current Raft watcher](READY-CHECKPOINT.md#recover-stalled-and-delegated-work)
supports that opt-in for Team DRIs.

## Team DRI

Give a Team DRI standing responsibility for a shared area. Existing Raft
agents are Team DRIs. The project manager chooses an eligible agent
and can route or transfer ownership. Confirm watcher readiness before
offering unattended work.

- **Set up.** Download the [v1 team DRI prompt](prompts/v1/team-dri.md).
  Use the [signed agent startup checklist](AGENT-STARTUP.md#verify-the-computer-cli-and-ledger)
  on the agent's runtime. Have the project manager confirm assignment
  eligibility and assign an ordinary ticket. Its runtime handles any
  background execution. For optional delivery through Raft, use
  [assigned Ready wakes](AGENT-STARTUP.md#choose-which-taskboard-events-wake-you).
- **Verify.** Check that the agent is the recorded owner. Have a teammate
  recover its blocker or completed result, decision sources, and evidence
  from Reports and Activity.
- **Stop.** Transfer open tickets to a named owner first. End wake
  subscriptions and have the manager or operator remove assignment
  eligibility or access as appropriate.

## Instructions for an on-demand session

1. **Check identity and publishing scope.** Use the exact HTTPS board and
   project. Read the signed context before publishing. Apply any personal
   publishing instructions supplied in the prompt or saved under
   **Integrations → Publishing instructions** on a board with that setting.
   With a compatible approved CLI connection, run
   `tasks --shared --url BOARD publishing-instructions` before writing to
   read the owner's current instructions. With trusted-tailnet HTTPS, use
   the instructions copied by the signed-in person. Keep excluded work,
   links, and evidence private. If you cannot confirm the relevant scope,
   continue the work privately and report the gap at handoff.
2. **Resolve the original work.** Open a known ticket ID directly. For a
   GitHub issue or PR, use `search-tasks` with the project and exact
   canonical URL, such as
   `https://github.com/example/repo/pull/123`. Use `/issues/123` for an
   issue. Strip fragments and query strings from the URL used to match the
   source, and keep exact review links as separate evidence. Compare the
   sources and current steps if more than one ticket matches, including
   results on later pages. Create a new
   ticket only when the work needs tracking and no matching ticket exists.
   Attach a newly opened GitHub issue or PR via `link-github`, passing the
   ticket ID and canonical URL. Read back that ticket to verify its source
   and keep using the original ticket for updates.
3. **Reuse Knowledge.** Look in the project's Knowledge index or use
   `search-with-decisions` for maintained decisions. Read the current
   brief, staged work, and exact decision or correction before applying it.
   Check who decided, why, and which scope it applies to. Use
   `search-activity` for past attempts and their evidence.
4. **Report what changed as you work.** Use `record-activity` for a
   meaningful checkpoint, decision, blocker, or handoff on the original
   ticket. Give a short outcome, relevant evidence and revision, remaining
   checks, and the next step or owner. Update the brief when the current
   conclusion changes, preserving the original scope and linking the exact
   decision. Use authorized transitions for actual starts and completions.
   If another person needs to act, record the next owner and tell your
   human at handoff. Ordinary task sessions use zero Inbox calls.
5. **Promote reusable guidance.** Record the decision, decision maker,
   rationale, scope, and exact source in Activity. Stage it against an
   existing Knowledge entry with the proposed brief and source event if
   permitted. For a new entry, a signed project agent or manager can use
   `create-knowledge`. A human session with no Knowledge write access
   leaves the source-linked proposal on the work ticket for the project
   manager. Use [organize and maintain Knowledge](SHARED-BOARD.md#organize-and-maintain-knowledge)
   to apply a settled stage. Keep open proposals on their work tickets.
6. **Verify the receipt.** Keep the returned ticket ID and Activity link.
   If a write's response is missing, read the ticket or search for the
   exact result before retrying. If reporting is unavailable, finish the
   user's work and identify the missing board update at handoff.

## Connect to Taskboard

Use the exact HTTPS board URL, including its mount path. If the project is
unclear, read `GET /api/v1/projects` and confirm the intended project before
writing.

### Signed CLI

1. Get the reviewed Taskboard checkout or verified source archive from your
   team. With Python 3.11 or later, use `./tasks` from that source root, or
   replace `tasks` below with your installed launcher.
2. Run `tasks --shared --url BOARD login`. Sign in to Taskboard and match
   the displayed code to the one in your terminal before approving.
3. Run `tasks --shared --url BOARD context` to verify the human profile.
   Run ordinary commands such as `search`, `add`, `log`, and `done` with
   the same board URL. To call the board API using this approval, run
   `tasks --shared --url BOARD api '/api/v1/reports?project=T'`. For a
   write, use `--method` and a JSON object via `--data-file` when the
   endpoint expects a body. Set `TASKBOARD_URL` for the session to omit
   repeated `--url` arguments.
4. The CLI stores the credential in an owner-only file for that board.
   Sign-in lasts up to eight hours and ends earlier if the approving
   browser session ends. Run `tasks --shared --url BOARD logout` to revoke
   it. After a board import to PostgreSQL, sign in again on the destination.

Use the enrolled DRI's signed Raft integration for standing-agent workflows.

### Trusted tailnet

An operator can enable direct HTTPS on a fixed-server Taskboard by setting
`TASKS_PERSONAL_AGENTS_TAILNET_SUBJECT` to one verified human subject on that
board. The authenticated portal must admit only the intended tailnet identity,
strip caller-supplied portal headers, and set its verified
`X-Portal-Auth-Method: tailscale` marker. Admitted requests use the configured
human's permissions. Remove the setting to return to signed access. LAN and
public ingress use sign-in.

From a device admitted to the configured tailnet board, call the API
directly or run the same CLI commands without login. Include
`X-Taskboard-Personal-Agent: 1` to select this mode. For writes, send
`Origin` with the board's scheme, host, and port, without its mount path.
Send POST parameters as JSON with `Content-Type: application/json`.
Verify the profile with `GET /api/v1/raft/context` before writing.

### Find the right API

Read `GET /.well-known/raft-agent-manifest.json` under the board mount for
the full action names and parameter contract. Resolve manifest paths
relative to the board origin. Use query parameters for GET and a JSON object
for POST. Direct board APIs such as Reports apply the existing permissions.

| Work | Shared action |
| --- | --- |
| Confirm identity | GET `context` |
| Find work and Knowledge | GET `search-tasks` with `project` and `q`, `search-with-decisions` with `q`, or `knowledge-areas` with optional `project` |
| Read current work | GET `task` or `task-activity` with `id` |
| Create and report | POST `create-task` with `project`, `title`, and `step`, or `record-activity` with `id`, `note`, optional `details`, and optional evidence `url` and `label` |
| Attach GitHub sources and other evidence | POST `link-github` with `id` and `github_url`. Use `link-reference` with `id`, `label`, and `url` for a TaskNote or another authorized HTTPS source. |
| Maintain the brief and Knowledge | POST `edit-task-details`, `create-knowledge`, or `knowledge-stage`, within the actor's project permissions |
| Ask or hand off | GET `assignees`. POST `peer-ask`, `raise-attention`, or `advance-task` with the `assign-human` transition. Use `handoff` or manager-only `change-ownership` for ownership. |
| Read Reports | GET `/api/v1/reports?project=PROJECT` under the board mount |

The [single-ticket pilot](PERSONAL-CODEX.md) remains available for existing
pilot sessions.
