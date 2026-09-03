# `plugins/kanban/` instructions (the root `AGENTS.override.md` and `plugins/AGENTS.md` still apply)

Also covers `hermes_cli/kanban.py` (the `hermes kanban` CLI) and `tools/kanban_tools.py` (the worker toolset).

## Multi-agent work queue

Kanban is a durable SQLite-backed board that lets multiple profiles or workers collaborate on shared tasks. Users drive it through `hermes kanban <verb>`; workers spawned by the dispatcher drive it through the dedicated `kanban_*` toolset, so their schema footprint is zero outside a kanban task. Profiles that explicitly enable the `kanban` toolset outside a dispatcher-spawned task additionally get the board-routing tools (`kanban_list`, `kanban_unblock`). Read the current verbs and tool names from the code rather than a copied list.

The dispatcher is a long-lived loop (default every 60 s) that reclaims stale claims, promotes ready tasks, atomically claims, and spawns the assigned profiles. It runs inside the gateway by default (`kanban.dispatch_in_gateway: true`); `plugins/kanban/systemd/` ships `hermes-kanban-dispatcher.service` for standalone deployment, and `plugins/kanban/dashboard/` is the web UI.

## Isolation model

- The board is the hard boundary: workers are spawned with `HERMES_KANBAN_BOARD` pinned in their env so they cannot see other boards.
- A tenant is a soft namespace within a board, so one specialist fleet can serve several businesses with workspace-path and memory-key isolation.
- After `kanban.failure_limit` consecutive non-success attempts on the same task (default 2) the dispatcher auto-blocks it to prevent spin loops.

## Further reading

- `website/docs/user-guide/features/kanban.md`
