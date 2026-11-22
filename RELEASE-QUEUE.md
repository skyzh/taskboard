# Taskboard release receipts

The [Pi timer](tools/release/README.md#run-protected-github-main-deployments-on-pi) deploys checked GitHub `main`. Open **About** in the account menu to see the running version. This page documents historical receipt reads and operator imports.

Older clients can read the recorded batches for project `T`, including each batch's tickets and latest stage or outcome. The project directory and overview still declare the queue. Ticket responses include `queued_for_release` and `release_number`. `TASKS_RELEASE_QUEUE_TICKET` selects which ticket response includes `active_release` and defaults to `T-78`. An empty string disables that field.

In this legacy readback, the latest activation or rollback receipt by operator time identifies the active manifest. A failed rollback leaves it unverified until a later receipt confirms it.

An operator with access to the selected SQLite ledger can import a receipt from the approved release runner:

```sh
./tasks --offline --db /path/to/selected.sqlite3 release-event .local/receipt.json
```

The operator can export the import JSON from a saved runner outcome with `publish_receipt.py --ledger-receipt`. The example describes fictional legacy data.

```json
{
  "release_id": "fixture-release-1",
  "ticket": "T-1",
  "candidate": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "status": "released",
  "active": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "at": "2026-11-21T07:42:00Z",
  "receipt_url": "https://example.test/releases/fixture-release-1"
}
```

- A batch uses a stable `release_id` and exact full candidate digest. Send one canonical `T-` ticket per receipt and reuse the same batch ID for all affected tickets.
- Each stage is `queued`, `running`, `released`, `rejected`, `rolled_back`, or `rollback_failed`. A released receipt requires the exact candidate as `active`. A rolled-back receipt identifies the restored digest. Use JSON `null` for an active digest that is unknown or absent.
- Include the runner's original UTC time. An optional `receipt_url` must be an HTTPS link intended for the board audience. Every ticket in the same batch carries the same timestamp, active digest and evidence URL for the same stage.
- Reimporting the same receipt is safe. Use a new release ID for each retry or different candidate. An earlier stage can be imported after its outcome and will preserve the recorded outcome.
- The importer assigns one stable number when it first records a successful activation. Rejections and rollbacks have no new number. Tickets retain their previous successful release number while waiting for a later attempt.
- To import a successful release with a reviewed changelog, use the `changelog` object described in [the operator guide](tools/release/README.md). One import associates all of its exact tickets with the batch. Each entry must match a typed merge receipt stored on that ticket. The batch publishes the owner's short summary and a link to that Activity receipt. Queued and failed attempts have no changelog.
- Historical receipts can omit the changelog and keep their existing numbers, stages and ticket links. The importer accepts exact replays and rejects different notes for a batch with a recorded changelog.

The read-only `/api/v1/deploy-queue` endpoint lists batches under existing board admission. It accepts `before` and `limit`. The operator importer records evidence. Deployment, approvals, and the source of trusted receipts remain with the release runner and existing authorization.
