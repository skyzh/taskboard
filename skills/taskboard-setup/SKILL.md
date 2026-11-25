---
name: taskboard-setup
description: Set up or fill gaps in a shared Taskboard project's Team practices Knowledge, shared SOPs, and recurring Knowledge compaction and reflection. Use when onboarding a project or standardizing its operating practices.
---

# Taskboard Setup

Use this skill for project initialization. For installation or a signed connection, follow [Taskboard](../taskboard/SKILL.md). For ongoing ticket execution, use [Taskboard Deep Work](../taskboard-deep-work/SKILL.md).

## Inventory one project

1. Confirm the selected project prefix, signed identity (`./tasks --shared context`), project manager, and authority to make the requested changes. `./tasks --shared projects --json` reports the registered manager. A signed project agent or manager can create Knowledge. [Project writers](../../docs/taskboard/SHARED-BOARD.md#edit-shared-content-and-apply-workflow-permissions) can edit its title and brief. The project manager places entries and attaches links under Sources. For a human manager, use their approved [Personal Agents CLI connection](../../docs/taskboard/PERSONAL-AGENTS.md) for manager-only signed actions. If there is no supported manager connection, record the blocked steps and route them to the manager.
2. Run `./tasks --shared knowledge-areas --project "$project"`. Open the Team practices index and its entries if present. Use `./tasks --shared search "$query" --project "$project"` to find renamed or unplaced equivalents, and `./tasks --shared show "$entry"` to confirm each record's project, content, and sources. Check placement and child entries in the signed Knowledge view. Check additional result pages when relevant. Reuse each practice where its current guidance already covers the purpose, even if its title or area differs.
3. Read **Tasks → Schedules** for the selected project from the approved owners' signed sessions. Find active project maintenance, owner schedules, and any equivalent external scheduled run with the project manager. Check each candidate's purpose, project, owner, cadence, and evidence of issuance or wake delivery. An existing single wake can cover both jobs if it handles both outcomes. Record its reference for both. A generic project maintenance schedule covers a purpose only if its actual run does that work. If the inventory is incomplete or a match is paused, canceled, or ambiguously scoped, ask its owner how to proceed before creating a second schedule.

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

## Set up missing recurring work

For each uncovered purpose, agree on an eligible owner, cadence in whole hours, and future first run in UTC. Use the project manager for compaction so reconciliation can apply changes across the project. Use a signed project agent or manager who can publish Knowledge for reflection. Each schedule is created by its owner using their signed identity. Confirm there is an existing operator tick or authorized project manager available to issue due tickets. Use the project's existing wake delivery if it needs to notify the owner. Record unresolved issuance or delivery in the setup ticket.

```sh
./tasks --shared recurring-add "team-practices-$project-compaction" 'Compact project Knowledge' \
  --project "$project" --cadence-hours "$compaction_hours" --next-at "$compaction_first_utc" --step "$compaction_step"
./tasks --shared recurring-add "team-practices-$project-reflection" 'Reflect on project practices' \
  --project "$project" --cadence-hours "$reflection_hours" --next-at "$reflection_first_utc" --step "$reflection_step"
./tasks --shared recurring-show "$schedule"
```

Use the job steps from the [templates](references/team-practices.md#recurring-work) and include the selected project or index link. Read each returned R-number with the same owner, then confirm its project and state in Schedules. These keys are stable per owner and project. A retry of creation uses the same original settings, including the first UTC slot. An existing equivalent schedule keeps its ID, configuration, and occurrence history. Route cadence changes or resumption through that schedule's owner.

## Verify the setup

Read back the chosen project, index, entry links, and each recurring purpose's R-number or external wake reference. Record the owner, cadence, first or next due time, and issuance path. Repeat discovery to confirm the project is complete and the next setup pass needs no writes. Record created, reused, and unresolved items in the setup ticket. For a demonstration or skill test, do this on a disposable project with fictional sources and schedules.
