# Taskboard development log

Update this log at each development handoff with the verified result, any open issue, and the next priority. Keep the queue and detailed review evidence in the work tracker. [Contributing guide](../../CONTRIBUTING.md)

## Unreleased

- **Reusable recurring tickets.** New ordinary schedules use one ticket across runs. Record a completed review to update **Last reviewed** and park the ticket until the next due run. For an existing schedule, settle its open run tickets, pause, adopt the reusable ticket, and resume at the intended UTC time. The schedule keeps its ID and earlier run links. [Adoption steps](SHARED-BOARD.md#recurring-work) · [Database upgrade](operators/DATABASE-MIGRATIONS.md)

- **Goal reviews in Schedules.** See active goals beside recurring work in the selected project or All projects. Open the existing goal ticket, see its date-only milestone deadline, and set or clear a daily Pacific review time for a goal you own.

- **Standing goals and daily reviews.** After finishing actionable work and reviews, recheck Inbox and do one focused standing-goal pass if offered. Record the result on the existing goal ticket. Set a daily Pacific review time for each active goal. An already completed pass counts for that day. Existing goal times and date-only milestone deadlines carry forward. [Agent guidance](AGENT-STARTUP.md) · [Goal wakes](READY-CHECKPOINT.md#try-goal-wakes)

- **Read back saved agent fields.** Ticket details and `tasks --shared show --json` expose current reminders and the viewer's watch state. Reconnect MCP after upgrading to read saved schedules, goals, wake subscriptions, Knowledge placement, and worker checkpoints. [Readback guide](INTERFACE-PARITY.md#read-back-saved-values)

- **Team recurring work.** Open Tasks → Schedules to see recurring work grouped by owner across the selected project or All projects. Open a schedule for its next due time and recent runs. Existing ownership and maintenance controls apply. [Recurring work](SHARED-BOARD.md#recurring-work)

- **Long ticket drafts.** The board asks for text confirmation when ticket or workflow writing exceeds 4,000 characters. Retry the same draft with `--force-text` in the CLI or `force_text: true` in signed tools. Activity and Knowledge save long text directly. [Writing guide](USING.md)

- **Reports Summary viewers.** The viewer count includes signed-in people in the selected Summary scope. Multiple tabs count as one person, and private project views stay in their own scope. Inactive views expire after 45 seconds.

- **Shared CLI write errors.** A confirmed rejection shows the reason and HTTP status on the current Raft CLI. A lost response still asks for a readback before retrying. [Shared CLI](SHARED-BOARD.md#use-the-shared-cli)

- **Ring 1 projects.** Create a private project for named verified people and Team DRIs. Its tickets and Knowledge inherit the membership list. Upgrade the server before creating the first private project. [Ring 1 setup and rollback](SHARED-BOARD.md#create-a-private-ring-1-project)

- **Browser notifications for human asks.** On a configured board, open Notifications → Browser notifications and enable alerts on that browser. New Decisions and attention requests can show a generic alert that opens Needs me. [Web Push setup](SHARED-BOARD.md#enable-browser-notifications-for-human-asks)

- **Return to Decisions.** The named recipient can return from a ticket's Decision panel or answer receipt to Notifications → Needs me and open the next request across projects.

- **Versioned database upgrades.** Use `taskboard-migrate` from the target release to check and upgrade an existing ledger before starting the target server. The first Alembic revision handles profile short roles on SQLite and PostgreSQL. Rehearse on a restored backup and verify an existing signed-in read before reopening writes. [Operator procedure](operators/DATABASE-MIGRATIONS.md) · [Upgrade skill](../../skills/taskboard-upgrade/SKILL.md)

- **PostgreSQL profile upgrade.** Existing PostgreSQL boards add missing profile columns during startup, preserving signed-in profiles and ticket assignments. This fixes a signed request failure after upgrading a board created before short roles.

- **Needs You sends across interfaces.** Use `assign-human` for a blocking Decision or `raise-attention` for a nonblocking request. MCP exposes both actions. Upgrade the board and clients together, and reconnect MCP to discover the tools. [Sending guide](INTERFACE-PARITY.md#send-a-needs-you-ask)

- **Project coordination channel in MCP.** Update and reconnect the adapter to use `taskboard_raft_channel`. Project managers can read the same channel setting as in the signed CLI. [MCP tool guide](MCP.md#available-tools)

- **Seven-day human sign-in.** New human sessions in fixed-server durable mode last up to seven days. Existing sessions keep their expiry and continue to use live checks and sign-out. [Session policy](SHARED-BOARD.md#enable-durable-human-browser-sessions)

- **Project owners.** Add eligible people and Team DRIs in Settings, and inspect effective permissions in Manage. Existing owners and assignments carry forward. [Ownership and permissions](SHARED-BOARD.md#manage-project-owners)

- **Signed agent usage read.** The Raft agent action `agent-usage-report` reads the existing board-member report, including the empty result while the approved daily feed is disabled. [Usage report setup](SHARED-BOARD.md#show-approved-daily-agent-usage)

- **Optional project ticket fields.** Managers can configure typed fields and collection prompts. Signed project writers can attach ticket-linked values and evidence, including partial effort and cost estimates. Upgrade the board and CLI, then opt in each project. [Configure fields](SHARED-BOARD.md#configure-optional-ticket-fields)

- **Local DRI ask recipients.** A Local DRI using Taskboard can choose another eligible human for a blocking Decision or nonblocking attention request. Omitting the recipient uses the registration creator. Upgrade the board and CLI to select a recipient with `--on`. [Setup and verification](LOCAL-DRI.md#choose-where-the-dri-asks-you)

- **GitHub webhook intake.** Operators can opt a project's configured repository into authenticated push and pull request cues. The scheduled extension refresh verifies the provider state and applies its existing rules. [Setup](GITHUB-EXTENSIONS.md#github-webhook-intake)

- **Project recurring work.** Managers can issue due work for their own project using the signed API or shared CLI. Upgrade the scheduler CLI before creating project-bound definitions. [Recurring work](SHARED-BOARD.md#recurring-work)

- **Project agent collaboration.** An agent can correct the title or brief or hand off assigned work in a project where it can work. Activity records the signed actor and the previous values. [Local DRI workflow](LOCAL-DRI.md#work-in-the-registered-project)

- **Local DRI workflow parity.** Upgrade the board and adapters to use ordinary agent actions within a Local DRI’s registered project. Verify the selected identity and scope with `context` and `capabilities`. [Local DRI workflow](LOCAL-DRI.md#work-in-the-registered-project)

- **Dependency publication age.** Refresh Python and npm locks with the seven-day resolver cutoff. The checked-in locks include the standalone MCP client and visual CI tooling. Use uv 0.13.9 or later for Python and npm 11 with `min-release-age` for npm lock refreshes. [Build from source](SETUP.md#build-from-source)

- **Project writing guidance in MCP.** Update and reconnect the MCP adapter to call `taskboard_field_guidance`. It reads the same effective project wording, field locations, and examples as the signed CLI. [MCP tool guide](MCP.md#available-tools)

- **Project discovery.** The shared CLI and MCP can list the projects and default already shown by the HTTP API. Upgrade the board, then update your CLI or MCP adapter to use `tasks --shared projects` or `taskboard_projects`. [Interface inventory](INTERFACE-PARITY.md)

- **Ticket tools in MCP.** Update the adapter and reconnect it to create tickets, correct details, record Activity with evidence, and page through a ticket's Activity. Existing CLI and HTTP clients keep their current permissions and behavior. [MCP tool guide](MCP.md#available-tools)

- **Signed agent usage.** A fixed Raft board can read a daily opt-in aggregate with measured tokens, an optional publisher-supplied cost estimate, and elapsed Taskboard worker time with coverage. [Configure the snapshot](SHARED-BOARD.md#show-approved-daily-agent-usage)

- **Initial shared project.** Choose the first project's prefix and title in the server TOML. Existing ledgers keep their project IDs and use their existing default. [Shared board setup](SHARED-BOARD.md#choose-the-initial-project)

- **Local DRI decision routing.** A creator can choose where an individual Local DRI asks for decisions. Existing registrations default to the local session. The Taskboard option routes a Decision on owned active work to the creator’s Needs me. [Setup and verification](LOCAL-DRI.md#choose-where-the-dri-asks-you)

- **Agent action discovery.** Signed context links to the current board’s HTTP action manifest. Personal and Local DRI prompts show when to post a checkpoint and how to report the same progress through the CLI and signed HTTP. [Progress examples](PERSONAL-AGENTS.md#report-progress)

- **Shared project creation.** Selected humans and agents can create projects through the signed HTTP API, shared CLI, and MCP. The configured human manages creation grants in Settings. Existing projects keep their owners and permissions. [Setup and rollout](SHARED-BOARD.md#create-projects-and-manage-creation-permissions) · [Interface inventory](INTERFACE-PARITY.md)

- **Saved Personal Agents instructions.** A signed-in human can save publishing instructions on their shared Raft board and read them on another device. Existing accounts start with an empty setting. Approved CLI sessions can read their owner's instructions with `tasks --shared --url BOARD publishing-instructions`. Upgrade both the board and CLI to use the command. [Setup](PERSONAL-AGENTS.md#connect-to-taskboard)

- **Local DRI routing.** A human can register named Local DRIs for individual projects. Ask the human in the runtime. The read-only Codex watcher can accept ordinary Ready asks from its configured requester while it is running. Existing Raft agents use Team DRI routing. [Setup and stop](LOCAL-DRI.md)

- **Optional recurring timer.** Operators on shared SQLite can schedule recurring issuance alongside the exact-time deferral timer. Review all due slots before enabling it and verify the issued tickets against recurring history. [Setup and rollback](SHARED-BOARD.md#run-the-sqlite-recurring-timer)

- **Knowledge areas and staged corrections.** Browse maintained area indexes and see staged work from signed search or either ticket. A signed reconciliation applies settled, source-backed changes and records conflicts for the entry owner. [Organize and maintain Knowledge](SHARED-BOARD.md#organize-and-maintain-knowledge)
- **Personal Agents sign-in.** On signed installations, the Tasks CLI saves an approved browser sign-in for the selected board. Trusted operators can enable credential-free Taskboard workflows over verified tailnet ingress. [Sign-in, tailnet setup, and migration](PERSONAL-AGENTS.md#connect-to-taskboard)
- **Personal Agents.** Signed-in humans can connect an on-demand session to the shared HTTP API under their own board permissions. The Integrations guide covers task sessions, decision knowledge, TaskNotes, assigned asks, and team DRI handoffs. Existing single-ticket connections keep their original scope. [Connection and prompts](PERSONAL-AGENTS.md)
- **Personal Codex progress.** The repo includes a session prompt for reporting checkpoints over HTTP. The existing signed connection is the default. Operators can enable credential-free reports on one pilot ticket through verified tailnet ingress. The first report on a Ready, unassigned pilot ticket records the configured human as its owner. [Prompt, setup, and rollback](PERSONAL-CODEX.md)
- **Return triggers for parked work.** Parking an ordinary ticket requires a Taskboard reminder or a dependency review. Existing tickets keep their history and standing goals keep their Inbox path. Before rollout, review older parked work with its owners and preserve agreed schedules and evidence. [Upgrade steps](SHARED-BOARD.md#review-existing-parked-tickets-before-rollout)
- **Goal wake pilot.** A Project T agent can opt its existing watcher into standing-goal offers after an observed queue drain, daily reviews, and dated deadline cues. The pilot preserves actionable Inbox priority and keeps its wake history across restart. [Set up, verify, and roll back](READY-CHECKPOINT.md#try-goal-wakes)
- **Ready watcher failure logs.** After restarting a managed Ready watcher from the updated checkout, a terminal read failure records its exact time and safe Raft CLI diagnostics. [Read the new record and upgrade the watcher](READY-CHECKPOINT.md#opt-in-raft-watcher)
- **Human Raft sign-in.** A new browser login can use its existing Raft token for up to one hour. When it expires, ordinary browsers ask for consent again. Raft Desktop's App tab can skip that click with existing consent, a known server, and completed two-factor authentication. [Session expiry and migration](SHARED-BOARD.md#enable-verified-identities)

- **Project writing guidance.** Signed users can read what each field is for, where it appears, and an example. Project managers can set wording for their project. Longer ticket titles and new ordinary peer request titles keep their complete text. [Field query, upgrade, and rollback](SHARED-BOARD.md#customize-agent-guidance)
- **Managed Ready sender pilot.** An operator can verify one opted-in Project T recipient through Raft on the existing SQLite board. Use a fresh private baseline, keep the watcher available, and release the sender only after exact review and live delivery and restart checks. [Pilot and rollback instructions](NOTIFICATION-LOG.md#verify-one-managed-ready-sender)

- **Optional stalled-work wake.** The existing Ready watcher can follow up after 30 minutes of sampled Raft Idle on a runnable In progress step or a started peer ask. It takes effect when you relaunch the updated watcher with `--wake-stalled`. [Opt in, verify the running job, or restore the previous options.](READY-CHECKPOINT.md#recover-stalled-and-delegated-work)
- **Timed follow-up regression check.** Portable CI now follows one exact-time deferral through the Ready watcher and a signed Start with a simulated Raft carrier. Existing schedules continue to work as configured. Once an operator verifies the deployed scheduler and a received wake, move eligible ticket follow-ups one at a time. [Verification](READY-CHECKPOINT.md#test-timed-follow-ups) · [Cutover and rollback](SHARED-BOARD.md#move-ticket-follow-ups-to-one-schedule)
- **Optional deferral timer.** Shared SQLite boards can run a separate 30-second timer that returns exact-time ticket reminders to Ready. Operators can verify wake and pickup before moving each Raft follow-up. [Setup and rollback](SHARED-BOARD.md#run-the-sqlite-deferral-timer)

## November 22, 2026

### Workflow change

- With this guide update, put blocking human questions on their existing tickets as named Decisions in every project using Taskboard. The recipient finds the Decision in **Notifications → Needs me**. When reviewing existing Parked tickets, move unanswered human blockers into this workflow. If someone already answered elsewhere, record the answer and its source once, clear any open Decision for that question, and continue the authorized work. [Decision workflow and migration steps](USING.md#ask-a-human-to-unblock-a-ticket)

## November 21, 2026

### Verified changes

- Standalone use keeps the SQLite ledger and read-only loopback preview. Shared boards can opt into OpenID Connect with explicit membership and can use PostgreSQL storage. Moving a ledger to PostgreSQL uses an explicit import from a SQLite backup. [Local setup](SETUP.md#install-or-upgrade-a-private-board) · [Shared setup](SHARED-BOARD.md)
- Reports show a terminal release receipt alongside the original next step. Owners and project managers can correct ticket titles and briefs while keeping the work history and recording a reason for the edit.
- Local Ubuntu CI rehearsal covered the portable checks, the required OpenID Connect fixtures, and notification preference writes on SQLite and PostgreSQL. The preference replay checks that a change updates the board revision and that repeating the save leaves the revision unchanged. [CI workflow](../../.github/workflows/ci.yml)
- Both hosted CI jobs passed on the initial source commit before the repository became public. The `main` rules require a squash pull request and both checks against current `main`. [Contribution workflow](../../CONTRIBUTING.md#propose-a-change)
- Notifications separate watched and subscribed updates from requests and decisions. Both views have direct links. In desktop and phone fixtures, the recipient's open count included items across pages and updated after handling a request or answering a decision. Raft and OpenID Connect sign-in returned to the selected view.
- The project notification log shows pending and historical Ready and merge notices imported from authorized operator receipts. Project managers see their project’s records, and recipients see their own. Desktop and phone fixtures verified access, filters, pagination and source links. A disposable PostgreSQL upgrade initialized both the goal and notification tables. [Notification log](NOTIFICATION-LOG.md)
- SQLite operators can opt in to recording pending observations and send confirmations from the existing Ready and merge sender commands. The project notification log now keeps the selected project through reload, copied links, browser history and sign-in. Signed-in desktop and phone fixtures verified the project and recipient scope after navigation. [Sender setup](NOTIFICATION-LOG.md#record-from-the-sender)

### Design decisions

- Keep identity and storage choices independent. Require explicit membership for shared OpenID Connect access and keep ordinary CLI and preview workflows available with SQLite.
- Extend shared responses additively. Test the preceding client against the candidate service, and verify that the preceding service can read the ledger after a disposable upgrade. [Compatibility policy](../../CONTRIBUTING.md#preserve-compatibility)
- Ready outboxes can supply pending observations. Both Ready and merge outboxes can supply send confirmations. Delivery and read times show Unknown on the current production connection. The log displays each delivery, read or acknowledgment time when an approved project source supplies it. [Receipt format](NOTIFICATION-LOG.md#import-one-observation)

### Next priorities

1. For the first release, verify a fresh standalone install and predecessor readback using a disposable ledger. [Local setup](SETUP.md#install-or-upgrade-a-private-board) · [Release checks](../../tools/release/README.md)
2. In the first reviewed release with these changes, open all three Notifications views. Handle a request and confirm the recipient count. Replay one authorized sender receipt in the release fixture, verify the manager and recipient views, then follow a Source activity link and check that Back returns to the same project.

### Review handoff follow-up

- Recent activity uses short relative times. Opening an activity entry or activating a standalone timestamp reveals the full time in the selected time zone. Deployment details and estimates keep their full timestamps.
- Actionable peer requests use a linked ticket. A review result gives the requester one Ready action for acknowledgment. The original ticket becomes Ready when that acknowledgment is recorded. The disposable replay checked Reports and Inbox at phone and desktop widths, the Ready watcher count, and an upgrade from a populated PostgreSQL board. [Agent workflow](AGENT-STARTUP.md#ask-a-peer-to-take-action)
- After rollout, verify one signed-in peer handoff and its acknowledgment through the shared queue. Previously unlinked reviews and waits remain readable during the transition.
