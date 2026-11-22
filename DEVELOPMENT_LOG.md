# Taskboard development log

Update this log at each development handoff with the verified result, any open issue, and the next priority. Keep the queue and detailed review evidence in the work tracker. [Contributing guide](CONTRIBUTING.md)

## November 21, 2026

### Verified changes

- Standalone use keeps the SQLite ledger and read-only loopback preview. Shared boards can opt into OpenID Connect with explicit membership and can use PostgreSQL storage. Moving a ledger to PostgreSQL uses an explicit import from a SQLite backup. [Local setup](README.md#run-locally) · [Shared setup](SHARED-BOARD.md)
- Reports show a terminal release receipt alongside the original next step. Owners and project managers can correct ticket titles and briefs while keeping the work history and recording a reason for the edit.
- Local Ubuntu CI rehearsal covered the portable checks, the required OpenID Connect fixtures, and notification preference writes on SQLite and PostgreSQL. The preference replay checks that a change updates the board revision and that repeating the save leaves the revision unchanged. [CI workflow](.github/workflows/ci.yml)
- Both hosted CI jobs passed on the initial source commit before the repository became public. The `main` rules require a squash pull request and both checks against current `main`. [Contribution workflow](CONTRIBUTING.md#propose-a-change)
- Notifications separate watched and subscribed updates from requests and decisions. Both views have direct links. In desktop and phone fixtures, the recipient's open count included items across pages and updated after handling a request or answering a decision. Raft and OpenID Connect sign-in returned to the selected view.
- The project notification log shows pending and historical Ready and merge notices imported from authorized operator receipts. Project managers see their project’s records, and recipients see their own. Desktop and phone fixtures verified access, filters, pagination and source links. A disposable PostgreSQL upgrade initialized both the goal and notification tables. [Notification log](docs/taskboard/NOTIFICATION-LOG.md)
- SQLite operators can opt in to recording pending observations and send confirmations from the existing Ready and merge sender commands. The project notification log now keeps the selected project through reload, copied links, browser history and sign-in. Signed-in desktop and phone fixtures verified the project and recipient scope after navigation. [Sender setup](docs/taskboard/NOTIFICATION-LOG.md#record-from-the-sender)

### Design decisions

- Keep identity and storage choices independent. Require explicit membership for shared OpenID Connect access and keep ordinary CLI and preview workflows available with SQLite.
- Extend shared responses additively. Test the preceding client against the candidate service, and verify that the preceding service can read the ledger after a disposable upgrade. [Compatibility policy](CONTRIBUTING.md#preserve-compatibility)
- Ready outboxes can supply pending observations. Both Ready and merge outboxes can supply send confirmations. Delivery and read times show Unknown on the current production connection. The log displays each delivery, read or acknowledgment time when an approved project source supplies it. [Receipt format](docs/taskboard/NOTIFICATION-LOG.md#import-one-observation)

### Next priorities

1. For the first release, verify a fresh standalone install and predecessor readback using a disposable ledger. [Local setup](README.md#run-locally) · [Release checks](tools/release/README.md)
2. In the first reviewed release with these changes, open all three Notifications views. Handle a request and confirm the recipient count. Replay one authorized sender receipt in the release fixture, verify the manager and recipient views, then follow a Source activity link and check that Back returns to the same project.
