# Building Taskboard

Follow [Contributing](../../CONTRIBUTING.md) for development setup, test commands, and review. Track the intended outcome and the exact revision under review. Keep test records reproducible on a disposable ledger and include desktop and mobile checks when a change affects those interfaces.

## Shared service changes

Preserve the CLI and shared API contract. If a change touches the action manifest, check the mounted paths and methods as well as the client behavior. For a provider integration, use a fixture provider with fictional actors and verify login, authorization, and scope before setting up a real registration.

When changing how Activity or release receipts are matched to tickets, run the affected read or sync on a disposable copy in the previous release’s format. Check that existing entries keep their links, a second sync leaves one copy of each entry, and a release receipt appears on its source ticket. Verify that the predecessor can still read the upgraded ledger when rollback uses that ledger.

## Verify a release

A release operator selects an independently reviewed candidate and checks it against the expected active predecessor. Stage the exact artifacts, keep a recoverable prior release, and verify the mounted service after activation. If activation fails, restore the prior release and verify it before declaring the rollout resolved.

Follow the [operator guide](../../tools/release/README.md) to bind review and changelog to the candidate. Record which versions of the app and separately loaded [check package](../../tools/release/checks/README.md) were used. Record the resulting active release and check outcomes in the deployment record.
