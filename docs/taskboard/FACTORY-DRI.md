# Connect an existing AI factory

The factory coordinator is the Taskboard owner. It reads an assigned ticket, calls the factory’s existing worker entry point, and records the outcome under the coordinator’s signed identity.

## Enroll the coordinator

1. Run the coordinator on a Computer with the Raft CLI, Python 3.11 or newer, and a checkout of Taskboard containing `tools/examples/factory_dri.py`.
2. Sign into the board’s Raft integration as the coordinator. Its first verified sign-in makes it visible to Taskboard. On a roster-restricted board, ask the board operator to include that identity in the project’s roster.
3. Run `./tasks --shared context` and check the board and coordinator name. Read the stable actor ID from the result of the signed context action. Have a project manager assign one ordinary Ready ticket to the coordinator. Choose one current Knowledge entry from the same project.

```sh
raft integration login --service tasks-board
raft integration invoke --service tasks-board --action context --json
./tasks --shared knowledge-areas --project T
```

If the current runtime cannot use this signed identity, run the coordinator on a Computer where Raft has enrolled it. Let that coordinator call the factory’s own authorized adapter using the factory’s existing access.

## Run one assignment

From the Taskboard checkout, run the adapter with the exact board URL, coordinator ID, ticket, Knowledge entry, and working directory. Replace the example ticket IDs and worker path with your own. If the installed Raft app has a different service key, use `--service` to select it.

```sh
python3 -m tools.examples.factory_dri \
  --board https://tasks.example.org/tasks/ --actor 'COORDINATOR_ACTOR_ID' \
  --ticket T-42 --knowledge T-10 --directory /path/to/workspace \
  -- /path/to/factory-worker
```

The chosen executable reads one JSON object from standard input with `ticket`, `brief`, `sources`, and the current signed `knowledge` detail. Return one JSON object on standard output. A verified outcome uses `{"status":"done","note":"Verified the fixture export.","url":"https://example.org/evidence","label":"Export evidence"}`. A blocker uses `{"status":"blocked","note":"Obtain approval for the sample export."}`. Evidence is optional and must be an authorized HTTPS link. Keep the note on one line.

Use this instruction with your factory worker, version 1:

> Read the assigned ticket, its sources, and current Knowledge. Confirm that the ticket and the authorized workspace match. Verify the requested result against current evidence. Return a brief outcome and an authorized evidence link, or identify the concrete approval or dependency needed to continue. Leave Taskboard reporting to the coordinator.

The board must accept Start before the adapter calls the command. A successful run records the outcome on the same ticket. A reported blocker or invalid worker outcome leaves a wait for coordinator follow-up. After interruption or an uncertain receipt, read the current ticket and its Activity and use the existing worker’s result to finish the record. Re-running the adapter leaves a prior Start in place and starts no worker for it. The adapter exits after one ticket. Press **Ctrl+C** to stop it and confirm the state of any run in the factory’s own runtime before resuming work.
