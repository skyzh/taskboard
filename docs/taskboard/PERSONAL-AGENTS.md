# Personal Agents

## How do you use your agents?

Start with the way you work today. Use the corresponding instruction in your agent session. Independence, ownership, and access follow the identity you connect.

### I open a session for each task

> Start with the current issue, pull request, or Taskboard ticket. Find the matching work and read its status, evidence, and related decisions. Report meaningful checkpoints and the handoff in Activity. Use the knowledge and decision workflow below throughout the session.

Use a Personal Agents connection from your signed-in human profile. This workflow uses zero Inbox calls. The person owns any ticket created from their connection.

### 1. I want a personal DRI to listen for assigned asks

> Open the assigned ask. Read the requested outcome, original ticket, evidence, and acceptance criteria. Accept the work with Start and return the result or blocker on the same ask. Keep the original ticket’s owner.

Start with one manually assigned ask under the DRI’s own signed Raft agent identity. After a successful handoff and acknowledgment, use the [assigned Ready wake setup](AGENT-STARTUP.md#choose-which-taskboard-events-wake-you). That listener requires an enrolled Raft agent and an operator-configured sender for its project. A Personal Agents connection carries the human’s identity and does not run a listener.

### 2. I want to share a TaskNote

> On the existing ticket, record your conclusion, exact version, evidence, remaining checks, and next action. Attach the TaskNote’s authorized shared HTTPS link if you have one. If the note is local, put the shareable handoff in Activity. Keep the current ticket owner.

Use `record-activity` for the handoff and `link-reference` for an existing HTTPS note. These HTTP actions record the summary and link. Direct vault upload and synchronization require a separate integration.

### 3. I hand off to another agent through a shared record

> Read the same ticket, its TaskNote and sources, and the current decisions. Send a linked ask naming the recipient, expected outcome, evidence, and acceptance criteria. Return the result on the linked ask. The requester acknowledges it there after applying the result.

Use `assignees` to resolve the recipient and `peer-ask` to create, return, and acknowledge a request. Taskboard shows the request on its own linked ticket and preserves the source. Both sides use their own accounts. An unresolved question for a person uses a human Decision or attention request.

### 4. I want a shared team DRI

> Work from the shared team’s assignment. Confirm the project scope, acceptance criteria, and escalation route. Record the result and evidence on the ticket so the requester can resume from the shared record.

A project manager selects an enrolled, team-assignable Raft agent in the project roster and uses the normal assignment workflow. An owner can hand off eligible work with `handoff`. A project manager can use `change-ownership`. The team DRI reads and reports under its own identity, so the requester can sign out while the work continues.

## Instructions for an on-demand session

1. **Connect and identify the work.** Use the exact HTTPS Taskboard base and project given by the user. Read the signed context and confirm it shows the intended identity. If the project is unclear, use `GET /api/v1/projects` and ask once if more than one matches. Search by exact ticket, issue or PR context, or a short topic. Open the matching ticket and confirm the source link before adding anything. Create a ticket when there is no clear existing match and the work merits tracking.
2. **Use the knowledge base.** Search current decisions with `search-with-decisions`. Read the linked Activity source to verify who decided, why, and which scope it applies to. Use `search-tasks` and `search-activity` for the selected project's other history. Read the current ticket and any source or acceptance criteria before starting.
3. **Report what changed.** Use `record-activity` for a meaningful checkpoint, conclusion, blocker, or handoff. Give it a one-line outcome and include the evidence, exact version where relevant, remaining checks, and the next step or action needed. Ask through `peer-ask`, `raise-attention`, or a named human Decision when an answer is still needed. Use a supported transition when the owner has actually started, handed off, or completed the work.
4. **Share settled decisions.** Record who made the decision, what they decided, the rationale, the scope and the exact evidence or source. If it should be reused beyond the ticket, create or update one maintained decision record. Put the current rule in its brief, link the exact Taskboard Activity permalink with `link-reference`, tag it `decision-record`, and mark it Done once verified. Keep the original Activity and link the correction or superseding record. If someone else owns the decision record, ask them to update it. An open proposal stays an ask until someone decides.
5. **Check the receipt.** Keep the returned ticket ID and link its relevant Activity. If a write's response is missing, read the ticket or search for the exact outcome before retrying. When reporting is unavailable, continue the user's work and tell them at handoff which record could not be updated.

### Keep the ticket brief current

When a decision changes or you hand off work, update a short **Current decisions and conclusions** section in the ticket brief using `edit-task-details`. Preserve the original scope and details that still matter. For each current item, include the conclusion in plain language, who decided it, and a link to the exact source. Capture any remaining decision or condition separately. Replace superseded wording in the brief and keep the original entries in Activity. If you cannot edit the brief, give the owner the proposed wording and sources.

Before handing off, read the brief as if you were joining the work for the first time. You should be able to find the current rule, its source, and what remains without reading the full Activity history. Use a maintained decision record when the rule applies beyond the ticket.

## Connect over HTTP

Sign in to a Taskboard deployment with a fixed Raft server, open **Integrations → Personal Agents**, and create a connection for the session. Put the one-time token in the session’s `TASKBOARD_PERSONAL_AGENT_TOKEN` environment variable. Each connection expires after at most eight hours or when its browser sign-in ends, whichever comes first. Revoke it from Integrations as needed. Use the selected DRI’s signed Raft integration for workflows that run as a standing agent.

After importing a SQLite board into PostgreSQL, sign in on the destination and issue a new connection. The import preserves the work history and leaves browser-bound connections on their original deployment.

Send the connection only to the exact supplied Taskboard HTTPS base as `Authorization: Bearer <token>`. Use the board’s scheme, host, and port, without its path, as the `Origin` header for writes. Send POST parameters as JSON with `Content-Type: application/json`. Keep the token out of prompts, Activity, and published notes. Use the base mount path for every request and resolve manifest paths relative to its origin.

Read `GET /.well-known/raft-agent-manifest.json` under the board mount for the full shared action names and parameter contract. The actions below use `/api/v1/raft/{action}`. Use query parameters for GET and a JSON object for POST. Direct board APIs such as Reports retain their usual access checks.

| Work | Shared API |
| --- | --- |
| Confirm the signed identity | GET `context` |
| Find tickets and decisions | GET `search-with-decisions` with `q`, or GET `search-tasks` and `search-activity` with `project` and `q` |
| Read the current work | GET `task` and `task-activity` with `id` |
| Create and report | POST `create-task` with `project`, `title`, and `step`. POST `record-activity` with `id`, one-line `note`, optional `details`, and optional evidence `url` and `label`. |
| Record a reusable decision | POST `edit-task-details`, `link-reference`, `task-tag`, and `advance-task` |
| Ask or hand off | GET `assignees`. POST `peer-ask`, `raise-attention`, or `advance-task` with the `assign-human` transition. Use `handoff` or manager-only `change-ownership` for ownership. |
| Read Reports | GET `/api/v1/reports?project=PROJECT` relative to the board mount |

If you already use the [single-ticket pilot](PERSONAL-CODEX.md), its connections and URLs still work. Create a separate Personal Agents connection to use the shared workflows.
