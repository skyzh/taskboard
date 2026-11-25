---
name: taskboard-upgrade
description: Plan, rehearse, verify, or recover an upgrade of an existing Taskboard installation using SQLite or PostgreSQL. Use for release changes and upgrade failures.
---

# Taskboard Upgrade

Use the existing installation and its authorized operator. Start with the
recovery steps if an attempted upgrade has already failed. For a first
installation, follow the [installation guide](../../docs/taskboard/SETUP.md).

## Identify the release pair and installation

- Record the running source build and approved target as full commits or
  artifact digests. A checkout's HEAD identifies its files. Use the running
  service, launcher, or mounted release receipt to verify which build serves
  the board. When available, read `GET <mount>/api/v1/release`. If it lacks
  metadata, use the operator's installation record or build manifest.
- Identify whether you are upgrading an [installed private preview](../../docs/taskboard/LOCAL-INSTALL.md#upgrade-and-restart),
  [shared server](../../docs/taskboard/SHARED-BOARD.md), or the
  [managed Pi release path](../../tools/release/README.md#run-protected-github-main-deployments-on-pi).
  Record the SQLite ledger path or PostgreSQL database and schema, service
  owner, gateway mount, and identity and session configuration. Keep secrets
  in the existing private runtime configuration.
- Read the target's release and operator instructions for the exact pair.
  Shipped schema changes run through the server startup path. Treat a proposed
  migration framework as future work until its commands ship in the selected
  release. Keep a database-engine move separate from an in-place upgrade.
  Use the [explicit SQLite-to-PostgreSQL import](../../docs/taskboard/SHARED-BOARD.md#use-a-postgresql-ledger)
  only when that move is the task.

## Back up and rehearse

- Agree on a cutover window and how to account for writes between the
  backup and cutover. Take a recoverable backup of the right ledger and keep
  the prior code, deployment configuration, and any external session or asset
  data needed for recovery. Preserve the backup independently of the target.
- Follow the backend's [backup and rehearsal steps](references/database-rehearsal.md).
  Restore to a disposable destination and compare known project, ticket,
  association and Activity records with the source. Match the production
  database type, relevant features, and release pair during rehearsal. Keep
  the copied service isolated from production identities and wake or sync jobs.
- Start the target on the restored copy using the installation's real startup
  path. Run the checks below using fictional data and a synthetic signed
  session where the installation supports sign-in. For an isolated shared
  server, use a fixture identity provider and the actual client where possible.
  [Release compatibility fixtures](../../docs/taskboard/RELEASE-COMPATIBILITY.md)
  show this pattern. Rehearse re-opening the restored source with the previous
  release. If rollback would retain the upgraded ledger, verify
  previous-release reads on a separate upgraded copy too. Require a verified
  recovery path before changing the live board.

## Upgrade and read back

- Recheck the running build and backup after writes are quiesced or the agreed
  final restore point is taken. Capture a timestamped baseline on the existing
  board. Use the signed session for a shared board and the installed launcher
  for a private preview. Record a known actor, project, ticket and its owner,
  current step, and Activity.
  Let the authorized operator apply the target through the rehearsed path.
  Keep ordinary writes paused or covered by the agreed cutover plan through
  verification.
- Check `GET <mount>/healthz` and the release identity. On a Raft-connected
  board, run `./tasks --shared context` and `./tasks --shared show "$taskboard_ticket"`
  with the verified session for that board. For OpenID Connect, sign in to
  the mounted board with a known member and open the same ticket and its
  Activity. Compare the board, actor and project, the ticket's owner and current
  step, and its linked Activity or decision with the pre-cutover live baseline.
  Exercise the changed feature on a disposable ticket in the rehearsal. For a
  standalone read-only preview, use the installed launcher to show the existing
  ticket and verify it in the loopback preview. [Shared CLI](../../docs/taskboard/SHARED-BOARD.md#use-the-shared-cli)
- Record the observed release pair, backup and restore receipt, signed read
  results, and any remaining verification on the upgrade ticket. Hand the
  operator the status and next action if the upgrade is blocked.

## Recover after the first failure

- Treat a failed signed read as a failed upgrade even when health passes.
  Record the first failing stage and timestamp, the redacted status or error,
  and the actual active build. Capture the relevant service journal or
  activation receipt before another attempt can change it. Use the baseline
  and logs to distinguish a session, permission or routing problem from an
  application or ledger error. Ask the operator to stop retries and hold or
  re-establish write quiescence. Preserve the failed ledger separately from
  the pre-upgrade backup.
- Reproduce the failure and proposed recovery on a copy. Roll back code only
  when the prior build is verified against the resulting ledger. If recovery
  needs a restored ledger, restore the verified backup to a separate
  destination. Reconcile and verify writes made after the backup before
  switching. If writes cannot be recovered, keep the switch pending for an
  explicit operator decision. For managed Pi releases, reconcile the result and
  activation journal using the [release runbook](../../tools/release/README.md#run-protected-github-main-deployments-on-pi).
- Confirm recovered service and data with the affected operator. Repeat the
  signed or local checks before reopening writes, and link the evidence before
  closing the incident.
