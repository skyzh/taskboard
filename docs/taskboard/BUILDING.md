# Building Taskboard

Follow [Contributing](../../CONTRIBUTING.md) for development setup, test commands, and review. Track the intended outcome and the exact revision under review. Keep test records reproducible on a disposable ledger and include desktop and mobile checks when a change affects those interfaces.

## Shared service changes

Preserve the CLI and shared API contract. If a change touches the action manifest, check the mounted paths and methods as well as the client behavior. For a provider integration, use a fixture provider with fictional actors and verify login, authorization, and scope before setting up a real registration.

When changing how Activity or release receipts are matched to tickets, run the affected read or sync on a disposable copy in the previous release’s format. Check that existing entries keep their links, a second sync leaves one copy of each entry, and a release receipt appears on its source ticket. Verify that the predecessor can still read the upgraded ledger when rollback uses that ledger.

## Verify a release

Merge the independently reviewed change into protected `main` through Stamp. The Pi timer deploys the tip of `main` after both required GitHub checks pass on that commit. Open **About** from the Taskboard account menu to verify the running commit, build time, and deployment time.

Use the [Pi operator guide](../../tools/release/README.md#run-protected-github-main-deployments-on-pi) for timer setup, rollout verification, and recovery.
