---
name: taskboard-setup
description: Set up or fill gaps in a shared Taskboard project's Team practices Knowledge, shared SOPs, and recurring Knowledge compaction and reflection. Use when onboarding a project or standardizing its operating practices.
---

# Taskboard Setup

Use this skill for project initialization. For installation or a signed connection, follow [Taskboard](../taskboard/SKILL.md). For ongoing ticket execution, use [Taskboard Deep Work](../taskboard-deep-work/SKILL.md).

## Inventory one project

1. Confirm the selected project prefix, signed identity (`./tasks --shared context`), project manager, and authority to make the requested changes. `./tasks --shared projects --json` reports the registered manager. A signed project agent or manager can create Knowledge. [Project writers](../../docs/taskboard/SHARED-BOARD.md#edit-shared-content-and-apply-workflow-permissions) can edit its title and brief. The project manager places entries and attaches links under Sources. For a human manager, use their approved [Personal Agents CLI connection](../../docs/taskboard/PERSONAL-AGENTS.md) for manager-only signed actions. If there is no supported manager connection, record the blocked steps and route them to the manager.
2. Run `./tasks --shared knowledge-areas --project "$project"`. Open the Team practices index and its entries if present. Use `./tasks --shared search "$query" --project "$project"` to find renamed or unplaced equivalents, and `./tasks --shared show "$entry"` to confirm each record's project, content, and sources. Check placement and child entries in the signed Knowledge view. Project search matches titles and current ticket summaries. If a practice might have a different title, list `./tasks --shared tagged "$project" decision-record` and read candidate Knowledge briefs with `show`. Verify the record kind and check additional search pages when relevant. Reuse each practice where its current guidance already covers the purpose, even if its title or area differs.
3. Before creating recurring work, agree on a sweep mode. Read **Tasks → Schedules** for the selected project and any board-wide sweep's anchor project from the approved owners' signed sessions. Find active project maintenance, owner schedules, and any equivalent external scheduled run with the project manager. Check each candidate’s purpose, assigned or board scope, owner, cadence, and evidence of issuance or wake delivery. An existing single wake can cover both jobs if it handles both outcomes. Record its reference for both. A generic project maintenance schedule covers a purpose only if its actual run does that work. If the inventory is incomplete or a match is paused, canceled, or ambiguously scoped, ask its owner how to proceed before creating a second schedule.

## Seed missing practices

Use the [Team practices templates](references/team-practices.md). Adapt them to the project's actual ticket queue, ownership, review policy, release route, and sources. Keep private configuration and credentials out of Knowledge. Preserve existing guidance and links. Place new entries and any unplaced equivalents in the area. The index automatically lists its placed entries. Add links to equivalent entries kept in other areas. Use a linked work ticket for a policy change that needs a decision.

```sh
./tasks --shared knowledge-add 'Team practices' --project "$project" --brief "$index_brief"
./tasks --shared knowledge-place "$index" 'Team practices' --index
./tasks --shared knowledge-add "$title" --project "$project" --brief "$brief"
./tasks --shared knowledge-place "$entry" 'Team practices'
./tasks --shared link-reference "$entry" "$source_label" "$source_url"
./tasks --shared edit "$index" 'Link the project practices.' --brief "$updated_index_brief"
```

Run each create only for an absent record. Read the returned ID before placement. Read back the project, area, and index after each step. If a create succeeded but placement failed or the response was uncertain, search the project and reuse the existing ID before retrying. When an entry stays in another area, link it from the index with a verified board Knowledge URL such as `#knowledge?entry=OPS-7` on the board's actual base URL. Carry forward the existing brief and links when editing an index.

## Choose a Knowledge sweep

- **Per DRI.** Have each participating DRI review their assigned entries. The project manager can set a maintenance DRI on an entry with `./tasks --shared knowledge-assign "$entry" --dri "$identity"`, or clear it with `--clear`. Keep existing assignments unless the project requests a change. Leave entries unassigned when ownership is unnecessary and agree who will handle any unassigned work.
- **Whole board.** Use one DRI whose signed session can read all the projects the sweep should cover. The sweep includes assigned and unassigned entries. Leave existing assignments in place and use one agreed project to hold that DRI's recurring tickets. Reuse that anchor project when setting up another project on the same board. A project-bound Local DRI can sweep only its registered project.

For either mode, choose a signed owner, a cadence in whole hours, and a future first run in UTC. Fill gaps in the selected mode. Reuse an equivalent schedule or external wake, including one run that already covers both compaction and reflection. For a mode change, coordinate existing schedules with their owners before setting up overlapping work. Use a stable schedule key per owner and scope, and the [signed job steps](references/team-practices.md#recurring-work). The selected mode determines which entries the job reads. Applying staged changes still requires the existing project permissions.

```sh
./tasks --shared recurring-add "$schedule_key" "$title" --project "$anchor_project" \
  --cadence-hours "$hours" --next-at "$first_utc" --step "$step"
./tasks --shared recurring-show "$schedule"
```

For per-DRI jobs limited to the selected project, include that project in the key, such as `team-practices-OPS-assigned-compaction`. For a whole-board job, use a board-wide key such as `team-practices-board-compaction`. If distinct schedules are needed for compaction and reflection, use a distinct key for each. Create a schedule under its owner's signed identity. Use the same original settings for a creation retry and read back the R-number, state, project, and cadence. Confirm that the anchor has an operator tick or an authorized manager who can issue due tickets. For an external wake, record its owner, schedule, and verified delivery path.

## Verify the setup

Read back the chosen project, index, entry links, and each recurring purpose's R-number or external wake reference. Run the selected Knowledge sweep as its owner and confirm the intended coverage before relying on it. Record the owner, cadence, first or next due time, and issuance path. Repeat discovery to confirm the project is complete and the next setup pass needs no writes. Record created, reused, and unresolved items in the setup ticket. For a demonstration or skill test, do this on a disposable project with fictional sources and schedules.
