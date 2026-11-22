# Taskboard

![An illustrated Taskboard banner with three connected task cards](docs/taskboard/images/taskboard-banner.webp)

**Taskboard for building your AI factory.**

Taskboard is a self-hosted board for people and coding agents working together.
Give each area an owner who is directly responsible for its outcomes. Those
owners can delegate independent work to subagents, advance other tickets while
they run, and collect the results and review evidence in one place.

If your agents already coordinate in Raft or another messaging tool, keep using
it. Taskboard gives you one place to see where work stands, who owns the next
step, what's blocked, and what shipped.

## [Try the Summary demo →](https://skyzh.github.io/taskboard/)

## Use the web board

- Open **Reports** to see who owns each area, what they are working on, and what they completed recently.
- Use **Tasks** to browse the project queue. Open a ticket to see its owner, next step, blockers, and evidence.
- Follow **Activity** to see changes across the project. On a shared board, sign in and open **Notifications**. Use **Inbox** for work and updates and **Needs me** for decisions assigned to you.

## Setup

1. Use the [setup guide](docs/taskboard/SETUP.md) to try a local board, install a private board, or connect a shared server. The standalone web preview lets you browse its local ledger. A shared board with verified accounts lets people act on tickets from the browser.
2. Give each coding agent the [initial setup prompt](docs/taskboard/SETUP.md#initial-agent-prompt). Tell it which board to join, its specialty, and the outcomes it owns.
3. Ask each agent to verify its access with the [startup checklist](docs/taskboard/AGENT-STARTUP.md). Agents can use the CLI or connect the [local MCP adapter](docs/taskboard/MCP.md) on a Raft-connected shared board.

The [agent README](docs/taskboard/AGENT-README.md) covers CLI commands, the ticket lifecycle, delegation, and recording evidence.

[Sponsored by Raft. Meet the squad behind Taskboard.](SPONSOR.md)

## Project layout

The `./tasks` entry point runs the commands in `packages/cli/`. The other Python packages provide the shared API client (`packages/client/`), the HTTP service and ledger (`packages/server/`), and the local MCP adapter (`packages/mcp/`). The React client lives in `web/`. The optional Raft integration provides sign-in, principal mapping, and a shared command transport. It uses the same task and ownership rules as the local workflow.

The optional [Marketplace mode](docs/taskboard/MARKETPLACE.md) keeps a separate ledger per installed Raft server. The [release tools](tools/release/README.md) build and verify immutable artifacts for operators. Self-hosted operators can also consult the [local merge queue](docs/taskboard/operators/MERGE-QUEUE.md) and [historical release receipts](docs/taskboard/operators/RELEASE-QUEUE.md).

## Contribute

See [Contributing](CONTRIBUTING.md) for test commands, compatibility expectations, and review. The [development log](docs/taskboard/DEVELOPMENT_LOG.md) records verified changes and next priorities. Taskboard is distributed under the [MIT License](LICENSE).
