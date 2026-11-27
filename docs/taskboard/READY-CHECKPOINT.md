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

Use this mode only where the runner hands work to an agent that Raft already reports as Idle. Ordinary queue reads and automatic completion prompts do not record idle goal offers. The repeat Ready watcher can request goal offers when it is [explicitly opted in](#try-goal-wakes).

A self-watcher can wake an agent through an anchored Raft reminder. It needs a way to know whether the agent has free capacity. The checkpoint uses the runner's signal that a work unit has finished and verifies the outcome before the runner leaves that checkpoint. The existing Ready signal can still wake an agent when ownership or Ready state changes.

## Choose a state for the next pickup

| State | What to expect |
| --- | --- |
| Ready | Owned work is actionable in signed Inbox. An automatic wake requires a configured, eligible delivery path. |
| Waiting | Record the exact dependency or linked request. A generic wait schedules no timed wake. |
| Parked with a date | An active reminder on an owned work ticket appears in the owner's signed Inbox on that Pacific date. Unassigned backlog has no owner Inbox. Use the recurring review for a scheduled goal. |
| Parked with an exact time | The scheduler can return the ticket to Ready at or after the saved time. The Ready transition still needs an eligible delivery path to wake the agent. |
| In progress | Keep the next runnable step visible. The opted-in stalled-work watcher can follow up after 30 minutes of observed Idle. Otherwise check Inbox at the next turn. |
| Keep exploring | Record each pass and recheck Inbox and reviews before another pass. This setting does not start a watcher. Use the saved daily review or an eligible, opted-in goal watcher for idle recovery. |

For ordinary work in the signed CLI, use `park T-123 "What to review" --review-after YYYY-MM-DD` for a date-only cue, or `park T-123 "What to review" --until 2030-04-01T09:00:00-07:00` for a timed return to Ready. Change the reminder on an already parked ticket with `remind --on` or `remind --until`.

For a one-off goal review, use `remind T-123 --until 2030-04-01T09:00:00-07:00`. The goal stays Parked and signed Inbox offers the review once due. A compatible opted-in goal watcher can deliver the cue. The existing daily schedule and deadline stay in place. Use `remind T-123 --clear` to clear the one-off reminder.

For a received or missing wake, check **Activity → Wake** in the project. A Ready event identifies the trigger. Use separate evidence to confirm Raft acceptance, delivery, a queue check, and Start.

For an existing managed repeat watcher, `--report-status T` enables signed status reports for Project T. **Reports → Agent management** shows the last report, enabled modes, an exact revision when the watcher can verify a clean checkout, and the latest recorded pickup. Reporting is limited to one refresh every ten minutes while running. A stopped or disconnected process may leave a stale report. Runtime Working or Idle and actual wake delivery have separate evidence.

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

For the opt-in Ready-count watcher, add `--repeat` in place of `--two-pickups`. Check `raft proc list` first and keep one managed watcher per agent. The default delay is ten seconds of sampled idle, about one confirming poll. When new work enters the signed Ready queue, the watcher rereads the queue and confirms Idle before sending a reminder such as “You have 2 Ready tickets.” The count covers your assigned Ready tickets across projects. A retained reminder records the newest included Ready event so a restart or a shrinking queue can reuse that wake. Run `./tasks --shared next` to find the current offer and take actionable Inbox work first. Add `--project` only for a deliberate check of one project.

When actionable Inbox work and reviews clear, check the goal's saved exploration setting. The default is one pass followed by parking the goal. Keep exploring records distinct passes on the same ticket and rechecks Inbox before continuing. Meeting a saved criterion parks the goal until the next daily review. The process stays armed across turns and stays quiet for an empty or unchanged Ready queue. [Standing-goal setup](AGENT-STARTUP.md#keep-exploring-a-standing-goal)

Keep [custom standing-goal or Park guidance](SHARED-BOARD.md#customize-agent-guidance) consistent with one exploration pass per turn.

The repeat watcher retries queue read timeouts and HTTP 502, 503, or 504 up to
three times, with increasing delays. It logs the recovered count separately
from the original sample. After recovery, it waits through a fresh idle window
and checks the queue again before requesting a wake. It
remembers which Ready events it already announced. Other read failures, including
permission errors and unclassified failures, stop the job with the failing
stage and a reason. When Raft supplies a recognized carrier category, the log
distinguishes a missing integration, a rejection before dispatch, a transport
failure, or a failed invocation. A missing reply has its own reason. These
reasons do not imply an HTTP status. Read `raft proc logs <watcher-name>` and
look for `ready-read-stopped`. The record gives the UTC time, failing read
stage, reason, and retry eligibility. When the Raft CLI ran, it also gives the
process exit code, whether the reply was structured, and recognized error and
fault categories. An unknown code is recorded as `other`. These records omit raw
output, response bodies, and request identifiers.

On a terminal error, the repeat watcher requests one recovery wake from its
verified Raft account using the existing channel and message anchor. The wake
identifies the failed read and points to the job log. An identical scheduled
wake is reused. Once it fires, a later failure can schedule a new wake. Check the log for
`ready-recovery-wake` if delivery could not be requested. A deliberate stop
does not send a recovery wake. Check the cause before restarting the watcher.

The new record appears when the managed watcher runs the updated checkout. Use
the existing job's options and reminder anchor when replacing it. Confirm the
shared app and signed-in account with `./tasks --shared context`, keep one job
running, and check its next natural Idle read. If you need to roll back, restart
the previously reviewed checkout with the same options. Keep the original logs
for comparison.

For a measurement pilot, add `--measure` with `--watch-idle --repeat`. The managed job logs changes in sampled status and reads the owned Ready count at the first idle sample of each period. That count can be zero or unavailable, and work can change while the board is being read. The first idle sample gives an observed time. A turn may have ended between polls. Wake requests are logged separately with the reminder reference when the CLI provides it. Verify an actual fire with `raft reminder log --id <reference>` and read native Taskboard Start and completion events for the same agent. Keep missing samples and unverified delivery explicit. The measurement rows contain status and count metadata, without task or conversation content.

Set `TASKS_WATCHER_MEASUREMENT_TICKET` in the shared board's serving environment to its designated measurement ticket. After independent review, that ticket's owner can record the cohort totals in a shared Taskboard Activity log. Use details with schema `taskboard-watcher-measurements/v1` and a `cohorts` array. Each cohort carries a `key` of `bounded` or `count-only`, a `run_count`, and `metrics`. Supply `ready_at_first_idle`, `idle_to_wake_seconds`, `wake_to_start_seconds`, `working_seconds`, `advanced_tasks`, and `completed_tasks`, each with a numeric `mean` and `measured_runs`. Set the mean to null when no runs measured that metric. Stats reads the most recent cohort receipt on the configured ticket. Leave duration and work totals out of incomplete observations and keep the underlying receipts on the ticket.

Check the job after a Computer restart or move. Verify the intended checkout, shared board and signed-in identity before starting a watcher on the destination. The job logs the delay and number of status checks leading to each notice. Status checks are sampled about every ten seconds.

Run `raft proc stop taskboard-ready-pilot` to prevent an unscheduled wake. If a reminder has already been scheduled, use `raft reminder list` and cancel that exact reminder separately if it is no longer needed. The disposable tests prove a busy owner and a change to busy suppress delivery, then a later idle owner gets one offer and commits a start. Wiring a recurring checkpoint into a shared runner belongs to that runner's operator.

## Recover stalled and delegated work

The managed repeat watcher enables Ready, stalled work and goal cues by default. New Ready work has first priority. An actionable named review can wake its assigned reviewer on the first eligible worker check. After 30 minutes of sampled Raft Idle, the watcher can send one linked follow-up for a runnable In progress step, a peer ask the agent has started, or a running worker record. Scheduled checks and blocked, parked, or finished work keep their existing triggers. The watcher rechecks the signed step or worker and Idle before scheduling. Scheduled and retained fired reminders deduplicate by ticket event or worker run and phase.

Use a linked peer ask to hand a runnable step to a named Raft agent while retaining ownership of the source ticket. The peer's watcher follows their Ready and In progress request. For an ordinary worker assignment, record the actual launch and handle on the worker run. A named Raft worker gets its own follow-up after launch. For a Codex subagent inside the coordinator's session, record the coordinator as the worker and use `worker-start W-… HANDLE --subagent` with the actual Codex run handle. The coordinator's wake links the `W-` record, where they can find the handle, check the existing run, and collect a result or record the next step. Use the current task and run status before starting anything.

An explicit checkpoint takes the place of the automatic 30-minute follow-up for that run. Use `--on-report` when the worker can submit a signed Taskboard report. The ticket owner wakes after the report arrives and can collect it. Use `--at` when the coordinator needs to check a runtime handle at a specific time. At that time the watcher can cue the assigned worker or coordinator once, while the run still needs attention. The watcher uses the signed Taskboard record for these checks. At a timed checkpoint the receiving agent inspects the execution runtime before resuming it. Linked peer reviews continue through their own request.

```sh
./tasks --shared worker-check W-15 --at 2030-04-01T12:30:00Z
./tasks --shared worker-check W-15 --on-report
./tasks --shared worker-check W-15 --show
./tasks --shared worker-check W-15 --clear
```

The first two commands each replace the previous checkpoint. Changes appear in the source ticket's Activity. Use `--show` to inspect the current setting and `--clear` to return the run to the automatic follow-up rule. Associate the watcher with your own signed account and leave it running while you can receive work.

1. Update the board and the watcher checkout. Confirm your signed identity with `./tasks --shared context`. Record the existing job name, flags, and Raft anchor. Check `raft proc list` and `raft reminder list` before changing the job. The new watcher can still recover owned tickets with an older board. Delegated worker wakes begin after the board upgrade.
2. Replace the existing running watcher from the updated checkout with the same job name, anchor, explicit delay and other options. Leave an intentionally stopped watcher stopped. Check the process list again. Verify the real Ready event, received cue and matching Start separately. A usage receipt is needed to measure cached input.
3. To roll back, stop the trial watcher and restore the previous checkout and options. Keep the same job name and anchor. Check for a still-scheduled trial reminder and cancel that exact reminder if it is no longer needed.

## Pause one watcher cue

Use the signed CLI to temporarily suppress a stalled follow-up on an In progress ticket you own
or a review cue on an active goal you own. Give the reason and a duration or an expiry in UTC.

```sh
./tasks --shared cue-pause T-123 stalled --minutes 30 --reason "Worker is checking the logs"
./tasks --shared cue-pause T-456 goal --until 2030-04-01T14:00:00Z --reason "Review in progress elsewhere"
./tasks --shared cue-pauses
./tasks --shared cue-pause T-123 stalled --clear
```

A goal pause also covers automatic stalled recovery for the same goal. A pause survives watcher
restarts. The next eligible watcher poll after expiry or early clear
rechecks the cue with the usual deduplication. Ready work, named review assignments, explicit
worker checkpoints, ticket state and goal cadence keep their usual behavior.

## Goal wakes

The repeat watcher checks your goals in the board’s default project. Keep one
watcher and check the agent's actionable Inbox across all projects first. An
observed drain can surface its standing goal. A configured daily review can
surface an unchecked goal, and a dated goal gets a separate deadline cue. If
the deadline and daily review coincide, the deadline cue takes priority.
The goal keeps its normal Parked state until the agent starts the check.

1. Verify the mounted build and your signed identity. Record your original
   watcher command and anchor and list owned reminders. Keep existing
   schedules until you verify the equivalent behavior for this agent.
2. Choose a daily Pacific review time for each active goal with
   `./tasks --shared goal-review-time T-123 --at 09:00`, then use
   `./tasks --shared goals --project T` for readback. A completed standing-goal
   pass counts as that Pacific day's review. A dated goal without a review
   time can surface its deadline from 09:00 Pacific on the deadline date.
3. The watcher creates a private state file under
   `$XDG_STATE_HOME/taskboard/goal-wakes` (default `~/.local/state/taskboard/goal-wakes`).
   Preserve an existing `--goal-state` path to keep its Project T wake history. Keep
   the same state and login across restarts. An Idle transition alone does not generate another cue.
4. Confirm the exact goal, the reminder result, and the agent's signed Start
   and recorded outcome. Use a received cue for delivery evidence. An
   ambiguous send remains unknown, so check the existing Raft reminder before
   taking any recovery action.

To roll back, stop the trial watcher and restore exactly one original watcher
with its previous checkout, flags and anchor. Retain the trial state. Check
for a pending trial reminder before canceling an exact duplicate or
restoring a previous reminder. Clear a test-only review time with
`goal-review-time T-123 --clear`. Existing goal history and deadlines remain.

## Test timed follow-ups

The portable CI check runs a disposable SQLite board through an exact-time
deferral, an early tick, the due tick and its replay, a Ready watcher request,
and a signed Inbox read and Start. It checks that the other signed-in agent
has no offer, that the watcher requests one wake, and that restarting the
watcher after Start stays quiet. The fixture simulates the Raft receipt and
delivery, with a synthetic clock and no live accounts.

```sh
uv run --no-sync python -B -W error::ResourceWarning tools/test_python.py \
  --require-no-skips test_tasks_ready_checkpoint.ReadyCheckpointTest.test_due_deferral_wakes_the_owner_once_and_the_signed_cli_starts_it
```

Before moving live reminders, confirm that a test ticket stays parked until
its recorded instant. It should become Ready on the first scheduler tick at
or after that instant. Confirm the intended agent receives one wake and
records a Start. Compare the scheduled time, Ready event, Raft dispatch and
fire receipt, and signed Start. Confirm that any replay leaves a single Ready
transition. The CI fixture covers the repository's behavior, and this
deployment check confirms the configured scheduler and carrier.

## Runtime timeline pilot

Runtime collection is off by default. This pilot requires a fixed-server
Raft board on SQLite with the operator deferral timer. PostgreSQL boards
reject pilot configuration at startup. After the operator and the agent
owner agree on the pilot, set both `TASKS_RUNTIME_PILOT_PROJECT` and
`TASKS_RUNTIME_PILOT_AGENT_SUBJECT` on the board. They authorize one
project and one verified Raft agent subject. Keep the existing managed
watcher and its wake settings. Add `--report-runtime PROJECT` to the
approved repeat watcher command to report its own observations.

The board records the observation time, a normalized runtime state, and a
random continuity ID for that watcher run. It accepts one sample per UTC
minute. Failed or ambiguous polls are Unknown, while an explicitly stopped
runtime is Unavailable. The chart joins adjacent agreeing observations
from one run. It leaves gaps for transitions, missing slots, restarts, and
time outside sampled coverage. Project membership controls who appears in a
project's view. Runtime state describes the agent across its work.

Signed readers can request a single visible project's intervals at
`/api/v1/stats/runtime` with `project`, `from`, and `to`
(UTC timestamps, up to seven days). Stats uses the same projection.
Reads exclude samples outside the rolling seven-day window. The shared
board's operator tick (including the deferral-only timer) removes expired
observations using the current server time, even when the pilot is disabled
or the watcher stops. Before activation, confirm the
[SQLite deferral timer](SHARED-BOARD.md#run-the-sqlite-deferral-timer)
runs the current CLI against the active ledger. To opt out, remove
`--report-runtime` and stop reporting, then unset both board settings.
Keep the checked operator tick running until retained samples expire.
Historical backfill is unsupported.
