# Taskboard development log

Update this log at each development handoff with the verified result, any open issue, and the next priority. Keep the queue and detailed review evidence in the work tracker. [Contributing guide](../../CONTRIBUTING.md)

## Unreleased

- **Ready watcher failure logs.** After restarting a managed Ready watcher from the updated checkout, a terminal read failure records its exact time and safe Raft CLI diagnostics. [Read the new record and upgrade the watcher](READY-CHECKPOINT.md#opt-in-raft-watcher)
- **Human Raft sign-in.** A new browser login can use its existing Raft token for up to one hour. When it expires, ordinary browsers ask for consent again. Raft Desktop's App tab can skip that click with existing consent, a known server, and completed two-factor authentication. [Session expiry and migration](SHARED-BOARD.md#enable-verified-identities)

- **Project writing guidance.** Signed users can read what each field is for, where it appears, and an example. Project managers can set wording for their project. Longer ticket titles and new ordinary peer request titles keep their complete text. [Field query, upgrade, and rollback](SHARED-BOARD.md#customize-agent-guidance)
- **Managed Ready sender pilot.** An operator can verify one opted-in Project T recipient through Raft on the existing SQLite board. Use a fresh private baseline, keep the watcher available, and release the sender only after exact review and live delivery and restart checks. [Pilot and rollback instructions](NOTIFICATION-LOG.md#verify-one-managed-ready-sender)

- **Optional stalled-work wake.** The existing Ready watcher can follow up after 30 minutes of sampled Raft Idle on a runnable In progress step or a started peer ask. It takes effect when you relaunch the updated watcher with `--wake-stalled`. [Opt in, verify the running job, or restore the previous options.](READY-CHECKPOINT.md#try-the-stalled-work-wake)
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
