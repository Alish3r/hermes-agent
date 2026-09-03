# `ui-tui/` instructions (the root `AGENTS.override.md` still applies)

Also covers `tui_gateway/` (the Python side) and the dashboard's `/chat` page. The TypeScript style section applies to every TS package in Hermes.

## Architecture

The TUI is a full replacement for the classic prompt_toolkit CLI, started with `hermes --tui` or `HERMES_TUI=1`. Node (Ink) renders the transcript, composer, prompts and activity; Python (`tui_gateway`) owns sessions, tools, model calls and slash-command logic. The transport is newline-delimited JSON-RPC over stdio, requests from Ink and events from Python, with the method and event catalog in `tui_gateway/server.py`. Built-in client commands (`/help`, `/quit`, `/clear`, `/resume`, `/copy`, `/paste`) are handled locally in `app.tsx`; everything else goes to `slash.exec`, which runs in the persistent `_SlashWorker` subprocess, with `command.dispatch` as the fallback. Theming arrives as skin data on `gateway.ready`.

Dev loop from `ui-tui/`: `npm install` once, then `npm run dev` (watch mode: rebuilds hermes-ink + tsx --watch), `npm run build`, `npm run typecheck`, `npm run lint`, `npm run fmt`, `npm test` (vitest).

## The dashboard embeds the real TUI

`hermes dashboard` serves `/chat` by embedding the actual `hermes --tui`, not a rewrite. `web/src/pages/ChatPage.tsx` mounts xterm.js with the WebGL renderer, the fit addon for container-driven resize and the unicode11 addon for wide characters; `/api/pty?token=...` (`hermes_cli/pty_bridge.py` plus the `@app.websocket("/api/pty")` endpoint in `hermes_cli/web_server.py`) upgrades to a WebSocket authenticated with the same ephemeral `_SESSION_TOKEN` as REST, passed as a query param because browsers cannot set `Authorization` on a WS upgrade. The server spawns whatever `hermes --tui` would spawn through `ptyprocess` (POSIX PTY: WSL works, native Windows does not); frames are raw PTY bytes each way, and resize is `\x1b[RESIZE:<cols>;<rows>]`, intercepted on the server and applied with `TIOCSWINSZ`.

Do not re-implement the primary chat experience in React. The transcript, the composer and input flow (slash-command behavior included) and the PTY-backed terminal belong to the embedded TUI, so anything added to Ink shows up in the dashboard automatically; if you find yourself rebuilding the transcript or composer for the dashboard, stop and extend Ink. Structured React UI around the TUI is fine when it is not a second chat surface: sidebar widgets, inspectors, summaries and status panels (`ChatSidebar`, `ModelPickerDialog`, `ToolCall`) may complement the embedded TUI. Keep their state independent of the PTY child's session and surface their failures non-destructively so the terminal pane keeps working.

## TypeScript style

- Prefer small nanostores over component state when state is shared, reused, or read by distant UI, and let each feature own its atoms: chat state near chat, shell state near shell, shared state in `src/store`.
- Components that render from an atom use `useStore`; non-rendering actions read with `$atom.get()`. Do not pass state through three components when the leaf can subscribe to the atom.
- Keep persistence beside the atom that owns it.
- Route roots stay thin: they compose routes and shell and are not controllers. `src/app` owns routes, pages and page-specific components; `src/store` owns shared atoms; `src/lib` owns shared pure helpers.
- No monolithic hooks; a hook owns one narrow job, and colocated action modules beat hidden god hooks.
- A pure side-effect callback uses the terse void form (`onState={st => void setGatewayState(st)}`), and async UI handlers make intent explicit (`onClick={() => void save()}`).
- Prefer interfaces for public props and shared object shapes over `type X = { ... }`, and extend React primitives for props (`React.ComponentProps<'button'>`, `React.ComponentProps<typeof Dialog>`, `Omit<...>`, `Pick<...>`).
- Table-driven mapping beats condition ladders for ids, routes and views.

## Further reading

- `tui_gateway/server.py` (JSON-RPC method and event catalog)
- `hermes_cli/pty_bridge.py` (dashboard PTY bridge)
