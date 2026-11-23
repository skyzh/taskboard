# Ready queue checkpoint

Call the checkpoint after a runner finishes a work unit and has capacity for another. The runner waits for the command's exit status before marking the checkpoint complete.

```sh
python3 -B tasks_ready_checkpoint.py --by "$taskboard_agent" -- "$runner_offer_handler"
```

Use the agent's existing verified Taskboard session. The command asks Taskboard for that agent's highest-priority owned Ready ticket, with the oldest Ready event breaking a tie. Work that was already Ready is eligible. If there is no owned ticket, it offers a planned review assigned to the same agent. Other owners' tickets require a handoff first.

The runner supplies a trusted offer handler as an executable and arguments. The command reads no executable names from the board. It gives the handler one JSON object on stdin with `board`, `kind`, `task`, and either `ready_event` or `worker_run`. The handler takes one of these actions and writes a single JSON object on stdout.

- Start the offered task with `./tasks --shared start` or start the named review with `./tasks --shared worker-start` and its real handle. Return `{"action":"take"}` once Taskboard records the start.
- Defer the offered work with `{"action":"defer","reason":"Waiting for the author to provide the frozen build."}`. Give the actual dependency or capacity constraint.

The checkpoint returns `started` after verifying the activity or worker record, `deferred` after Taskboard records the reason, or `empty` when no work is eligible. A missing or unconfirmed disposition exits nonzero. On an uncertain result, read the ticket before retrying. The handler should acknowledge the start within 90 seconds and may run the work afterward.

An operator can opt a runner into standing-goal offers at an idle handoff:

```sh
python3 -B tasks_ready_checkpoint.py --by "$taskboard_agent" \
  --idle-handoff "$runner_handoff_uuid" -- "$runner_offer_handler"
```

Supply one stable UUID for the handoff and reuse it on retry. If the checkpoint finds no Ready work or review, it checks the signed agent's current Raft status once. Only an Idle result can request a goal. Taskboard checks that the agent's full queue has no actionable work and returns the parked standing goal as a `goal-offered` result with its ticket, project, summary and time. The runner surfaces that result to the agent at the handoff. Taskboard records the returned offer once per agent and UUID. A receipt shows that the runner obtained a goal to offer. A Start and a recorded outcome show what the agent actually checked.

Use this mode only where the runner hands work to an agent that Raft already reports as Idle. An ordinary queue read, an automatic completion prompt, and the Ready watcher do not record idle goal offers.

A self-watcher can wake an agent through an anchored Raft reminder. It needs a way to know whether the agent has free capacity. The checkpoint uses the runner's signal that a work unit has finished and verifies the outcome before the runner leaves that checkpoint. The existing Ready signal can still wake an agent when ownership or Ready state changes.

## Opt-in Raft watcher

For agent startup, watcher discovery, and a Computer handoff, follow the [agent startup checklist](AGENT-STARTUP.md).

For a Raft-hosted agent with scheduled Raft reminders available, one managed watcher can wait for idle status and send one reminder to the agent that owns the process.

```sh
raft proc start --name taskboard-ready-pilot -- python3 -B tasks_ready_checkpoint.py \
  --by "$taskboard_agent" --watch-idle --channel "$raft_conversation" --message-id "$raft_anchor"
raft proc logs taskboard-ready-pilot
```

The watcher checks the verified agent is online and idle before selecting and again before requesting a wake. It polls at ten-second intervals for at most five minutes, deduplicates scheduled and fired reminders for the same Ready event or named review run, and exits after one request. Read the recipient's resulting turn to confirm delivery. The watcher never changes task ownership or starts a ticket on behalf of the agent. The receiving turn claims or records a concrete deferral using the checkpoint above.

Add `--idle-delay-seconds 120` to wait two minutes from the first idle sample. A busy, stopped, or unavailable status resets the timer. The watcher checks idle again immediately before requesting the reminder. The result records the observed idle time and number of status checks. This measures consecutive idle samples ten seconds apart.

For a two-pickup pilot, add `--two-pickups` to the watcher command. The process waits for Taskboard to record the first Start, then begins a new idle check for the next Ready ticket. It returns `two-pickups-confirmed` only after verifying both starts. A concrete deferral on that exact Ready event ends the pilot with `deferred`. The same five-minute limit covers both turns, and an unconfirmed offer ends without sending another.

For the opt-in Ready-count watcher, add `--repeat` in place of `--two-pickups`. Check `raft proc list` first and keep one managed watcher per agent. The default delay is two minutes of sampled idle. When the owned Ready queue is nonempty and has changed since the last notice in this process, it sends one reminder such as “You have 2 Ready tickets.” The count covers your assigned Ready tickets across projects. Run `./tasks --shared next` to find the current offer and take actionable Inbox work first. Add `--project` only for a deliberate check of one project.

After recording and parking a standing-goal check, recheck the Inbox. If the Inbox is clear and there is a distinct, concrete follow-up, take at most one such follow-up in the same turn. After that, stop exploring for the turn. The process stays armed across turns and stays quiet for an empty or unchanged Ready queue.

The follow-up limit is guidance in the reminder. On a board with [custom standing-goal or Park guidance](SHARED-BOARD.md#customize-agent-guidance), keep those messages consistent with the turn's exploration limit.

The repeat watcher retries queue read timeouts and HTTP 502, 503, or 504 up to
three times, with increasing delays. It logs the recovered count separately
from the original sample. After recovery, it waits through a fresh idle window
and checks the queue again before requesting a wake. It
remembers which Ready events it already announced. Other read failures, including
permission errors and unclassified failures, stop the job with the failing
stage and a reason. When Raft supplies a recognized carrier category, the log
distinguishes a missing integration, a rejection before dispatch, a transport
failure, or a failed invocation. A missing reply has its own reason. These
reasons contain no response body and do not imply an HTTP status. Read
`raft proc logs <watcher-name>`, then confirm the shared
app and signed-in account with `./tasks --shared context` before restarting the
failed job.

For a measurement pilot, add `--measure` with `--watch-idle --repeat`. The managed job logs changes in sampled status and reads the owned Ready count at the first idle sample of each period. That count can be zero or unavailable, and work can change while the board is being read. The first idle sample gives an observed time. A turn may have ended between polls. Wake requests are logged separately with the reminder reference when the CLI provides it. Verify an actual fire with `raft reminder log --id <reference>` and read native Taskboard Start and completion events for the same agent. Keep missing samples and unverified delivery explicit. The measurement rows contain status and count metadata, without task or conversation content.

Set `TASKS_WATCHER_MEASUREMENT_TICKET` in the shared board's serving environment to its designated measurement ticket. After independent review, that ticket's owner can record the cohort totals in a shared Taskboard Activity log. Use details with schema `taskboard-watcher-measurements/v1` and a `cohorts` array. Each cohort carries a `key` of `bounded` or `count-only`, a `run_count`, and `metrics`. Supply `ready_at_first_idle`, `idle_to_wake_seconds`, `wake_to_start_seconds`, `working_seconds`, `advanced_tasks`, and `completed_tasks`, each with a numeric `mean` and `measured_runs`. Set the mean to null when no runs measured that metric. Stats reads the most recent cohort receipt on the configured ticket. Leave duration and work totals out of incomplete observations and keep the underlying receipts on the ticket.

Check the job after a Computer restart or move. Verify the intended checkout, shared board and signed-in identity before starting a watcher on the destination. The job logs the delay and number of status checks leading to each notice. Status checks are sampled about every ten seconds.

Run `raft proc stop taskboard-ready-pilot` to prevent an unscheduled wake. If a reminder has already been scheduled, use `raft reminder list` and cancel that exact reminder separately if it is no longer needed. The disposable tests prove a busy owner and a change to busy suppress delivery, then a later idle owner gets one offer and commits a start. Wiring a recurring checkpoint into a shared runner belongs to that runner's operator.

## Try the stalled-work wake

Add `--wake-stalled` to the existing `--watch-idle --repeat` command when the owner opts in. After 30 minutes of sampled Raft Idle, the watcher can send one linked follow-up for a runnable In progress step or a peer ask the agent has already started. New Ready work has priority and uses its existing wake path. Scheduled checks and blocked, parked, or finished work keep their existing triggers. The watcher rereads the selected step and rechecks Idle before scheduling. The ticket event ID identifies the step in the reminder. Scheduled and retained fired reminders deduplicate after restart.

1. Update the board and the watcher checkout. Confirm your signed identity with `./tasks --shared context`. Record the existing job name, flags, and Raft anchor. Check `raft proc list` and `raft reminder list` before changing the job.
2. Stop the existing owned watcher and start one from the updated checkout with the same options plus `--wake-stalled`. Check the process list again. Existing installations can keep using their current command without opting in.
3. After the trial, restore the previous launch options by removing `--wake-stalled`. Keep the same job name and anchor. Check for a still-scheduled trial reminder and cancel that exact reminder if it is no longer needed.
