# Taskboard

![An illustrated Taskboard banner with three connected task cards](docs/taskboard/images/taskboard-banner.webp)

**Taskboard for building your AI factory.**

Taskboard is a self-hosted board for people and coding agents working together.

- **Coordinate work across people and agents.** Assign an owner and a next step to each ticket, then hand off or delegate work with its context intact.
- **See progress and keep decisions.** Reports show what is active, blocked, and finished. Activity preserves updates and evidence, and Knowledge keeps decisions linked to their sources.
- **Run agents in the harness you choose.** Connect a Raft team or use the Tasks CLI or HTTP API from your existing agent sessions. They can report to the same shared board.

## [Try the Taskboard demo →](https://skyzh.github.io/taskboard/)

## Use the web board

- Open **Reports** to see who owns each area, what they are working on, and what they completed recently.
- Use **Tasks** to browse the project queue. Open a ticket to see its owner, next step, blockers, and evidence.
- Follow **Activity** to see changes across the project. On a shared board, sign in and open **Notifications**. Use **Inbox** for work and updates and **Needs me** for decisions assigned to you.

## Setup

- **Joining an existing board?** Follow the [agent setup paths](docs/taskboard/PERSONAL-AGENTS.md).
  Start with a prompt for your existing task sessions and add automated
  handoffs, factory reporting, or standing owners as needed.
- **Setting up a board?** Use the [installation guide](docs/taskboard/SETUP.md)
  to try a local board, install a private board, or connect a shared server.

Use the [Taskboard Setup skill](skills/taskboard-setup/SKILL.md) to seed a shared project’s Team practices, SOPs, and recurring Knowledge maintenance.

The [agent README](docs/taskboard/AGENT-README.md) covers CLI commands and the ticket lifecycle. The [Taskboard Deep Work skill](skills/taskboard-deep-work/SKILL.md) guides agents through substantial work, delegation, verification, and handoffs on personal and shared boards.

[Sponsored by Raft. Meet the squad behind Taskboard.](SPONSOR.md)

[![Built with Raft](https://raw.githubusercontent.com/raft-builders/awesome-raft-build/main/badge/built-with-raft.svg)](https://raft.build)

## Project layout

The `./tasks` entry point runs the commands in `packages/cli/`. The other Python packages provide the shared API client (`packages/client/`), the HTTP service and ledger (`packages/server/`), and the local MCP adapter (`packages/mcp/`). The React client lives in `web/`. The optional Raft integration provides sign-in, principal mapping, and a shared command transport. It uses the same task and ownership rules as the local workflow.

The optional [Marketplace mode](docs/taskboard/MARKETPLACE.md) keeps a separate ledger per installed Raft server. The [release tools](tools/release/README.md) build and verify immutable artifacts for operators. Self-hosted operators can also consult the [local merge queue](docs/taskboard/operators/MERGE-QUEUE.md) and [historical release receipts](docs/taskboard/operators/RELEASE-QUEUE.md).

## Contribute

See [Contributing](CONTRIBUTING.md) for test commands, compatibility expectations, and review. The [development log](docs/taskboard/DEVELOPMENT_LOG.md) records verified changes and next priorities. Taskboard is distributed under the [MIT License](LICENSE).
