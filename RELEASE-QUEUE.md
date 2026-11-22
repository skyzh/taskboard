# Taskboard release receipts

The default Taskboard project, `T`, shows a deploy queue in Tasks. Each batch lists the affected tickets and its latest recorded stage or outcome. The project directory and overview declare the queue and the two ticket fields. Ticket reads include `queued_for_release` and `release_number`. Other projects use the existing task views.

The rollout ticket shows the active release beside the owner's current step. The service uses `TASKS_RELEASE_QUEUE_TICKET` to select that ticket. It defaults to `T-78`, matching the operator's `--queue-ticket` default. Set it to the same ticket as the operator when overriding either default, or set it to an empty string to hide the active release from ticket detail.

The latest activation or rollback receipt by operator timestamp identifies the active manifest. A failed rollback marks the active release unverified until a later receipt confirms it. Queued, running, and rejected attempts remain in the deploy queue while the current step remains the owner's text.

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
- For new successful releases, include the approved `changelog` object as described in [the operator guide](tools/release/README.md). One import associates all of its exact tickets with the batch. Each entry must match a typed merge receipt stored on that ticket. The batch publishes the owner's short summary and a link to that Activity receipt. Queued and failed attempts have no changelog.
- Historical receipts can omit the changelog and keep their existing numbers, stages and ticket links. The importer accepts exact replays and rejects different notes for a batch with a recorded changelog.

The read-only `/api/v1/deploy-queue` endpoint lists batches under existing board admission. It accepts `before` and `limit`. The operator importer records evidence. Deployment, approvals, and the source of trusted receipts remain with the release runner and existing authorization.
