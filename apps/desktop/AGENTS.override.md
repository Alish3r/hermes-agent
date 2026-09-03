# `apps/desktop/` instructions (the root `AGENTS.override.md` still applies; this condenses `apps/desktop/AGENTS.md`, which the hint loader would otherwise cut at 8,000 chars)

Read `DESIGN.md` beside this file for the visual and interaction contract. When a rule here and the code disagree, trust the code and fix whichever is wrong, but never break an invariant to make a change easier. History and rationale: `apps/desktop/AGENTS.md`.

## What this app is

Desktop is its own native chat surface: an Electron + React + nanostore renderer talking JSON-RPC (`requestGateway`) to a `tui_gateway` backend. It is not the browser dashboard and does not embed `hermes --tui`; it owns its composer, transcript and slash pipeline, and has no build or runtime dependency on the dashboard frontend: it spawns a headless `hermes serve` backend. `dashboard` and `serve` share `cmd_dashboard`/`start_server` but neither launches the other; the one exception is the compatibility fallback in `electron/backend-command.ts` + `backendSupportsServe()`, which rewrites the argv to the legacy `dashboard --no-open` only when the resolved runtime does not register `serve`.

Electron owns the machine (process lifecycle, native filesystem, install/update, a narrow typed capability bridge), the renderer owns the experience, the backend owns the work. The renderer never reaches for Node or Electron directly, and agent behavior lives behind the gateway, never reimplemented in React; when a change blurs a seam, fix the seam rather than widening it.

## State lives with its authority

Ask who is allowed to be right about a piece of state before asking where to store it: the backend for anything another Hermes surface can also change (the renderer's copy is a cache), Electron for machine and runtime facts, the renderer only for this window's presentation. Shared renderer state lives in small feature-owned stores, server data in the query layer, short-lived interaction detail in the component, hot coordination that must not paint in a ref; a new global store must earn its claim. Persisted state declares its scope in its own key, or one profile's setting bleeds into another. Sessions have several identities: durable navigation and pinned state key off the stable identity, live streaming off the runtime identity, state that must outlive compression off the lineage root; translate at the boundary.

## Server truth is cached, not owned

Merge, don't clobber: a refresh layers over what you know and never drops live or pinned rows. Paint optimistically, roll back a failed write visibly, let the authoritative refresh have the last word, and use generation counters so a stale response never overwrites newer intent. Only the foreground surface publishes into the shared view; flush terminal transitions immediately; preserve reference identity on no-ops.

## Switching context is a re-home, not a reboot

Changing profile, connection or mode keeps the shell and the user's work in place; only the gateway-bound view is cleared and repopulated. Three switch shapes: a connection/mode apply (local, remote, cloud) is the soft re-home, shell mounted, gateway-bound stores explicitly wiped (query invalidation alone cannot evict live session stores), then reconnect; a runtime home change (a different `HERMES_HOME`) is a hard re-home where the window legitimately reloads; a live profile swap in the same window activates another profile's socket while background profiles keep streaming, lists merge rather than wipe, and only an explicit user selection starts a fresh foreground draft. After any swap the active socket, active profile and connection atoms must agree, or REST and filesystem calls route to the wrong backend.

## Cross every seam as an observable ladder

Backend discovery, version fallbacks, connection and auth resolution, workspace-cwd selection and capability detection share one shape: precedence written down in one place; a candidate trusted only after validation; a failed read falls to the next rung while a failed authoritative write surfaces or rolls back; a missing capability handled differently from a transient failure; bounded retries ending in a real recovery affordance; one resolver per policy. One-time credentials are never reused (an OAuth connection mints a fresh WebSocket ticket on every dial; only a confirmed 401/403 means reauthentication), and a connection test must exercise the leg you will use, not just an HTTP probe. Keep older backends working with a narrow, tested fallback tied to an identified older runtime, never one that quietly degrades the feature it protects.

## Keep the waist narrow

The shell's internal registries are composition seams, not a public plugin ABI: no universal extension system, manifest or plugin adapter for a single consumer, and a shared contract only once more than one real consumer proves its shape. An agent-callable capability that acts on this renderer is a property of the session's client: wire it off the `source: 'desktop'` the app sends on `session.create`, never off an env var on the backend process, which may be a remote or cloud gateway (see `tools/AGENTS.md`).

## Slash commands are curated client-side, then dispatched

The backend's `commands.catalog` and `complete.slash` already include built-ins, user `quick_commands` and skill-derived commands. `src/lib/desktop-slash-commands.ts` is the load-bearing file: `isDesktopSlashCommand` gates execution and is true for any non-built-in so typed extension commands run; `isDesktopSlashSuggestion` gates discovery in both completion paths of `use-slash-completions.ts` and in `filterDesktopCommandsCatalog`; `isDesktopSlashExtensionCommand` must keep flowing into both of those paths. Curation hides noise (terminal-only and messaging-only built-ins), never user-activated extensions; dispatch (`runSlash` in `use-prompt-actions/slash.ts`) sends everything not desktop-owned to `slash.exec`. Tests: `npx vitest run src/lib/desktop-slash-commands.test.ts` from `apps/desktop`.

## Bot Mode (`src/plugins/hermes-bots/`)

Each bot is a Hermes profile, and one invariant is settled and not open for re-litigation: one bot equals one canonical forever-chat identified by name, the pair (profile, session titled exactly "Bot Chat"), which the state DB's UNIQUE(title) index makes a registry of at most one row. Clicking a bot row resolves the registry every time via `session.list {title, include_hidden: true}`; if no row exists it creates one titled `Bot Chat`, born hidden, kicked off with the bot's intro, re-running the lookup first so a concurrent or pre-existing row is adopted, never forked. There is no session-id pin and no per-bot session browser, by design; legacy `chat` keys in `ui_meta` are ignored and dropped from merges. Recency must never win: canonical Bot Chats are unconditionally hidden from the Sessions sidebar, so the bot row is the only door to the forever-chat, and side-chats from "New chat with this agent" stay visible there and are never the row's target. Reject any change that reintroduces a stored session-id pointer as identity (even "as a fallback" or "for verification") or consults recency, visibility or "where the user left off" for the row's target; such reports are about side-chats. The gateway reports the registry row as `canonical_session` on `profiles.list`, so preview and click identity are the same row. Contract tests: `tests/canonical-chat-*.test.mjs`, `tests/hide-bot-chats.test.mjs`.

## Respect the person, prove it

Never navigate, move focus or open a surface because something happened in the background; offer, don't hijack. Expensive stateful surfaces stay alive when hidden. Test what would break a user (resolver rungs, identity and scope boundaries, optimistic rollback, local and remote routing), and update the `DESIGN.md` checklist and all locales before handing off.

## Further reading

- `apps/desktop/DESIGN.md`
- `website/docs/developer-guide/desktop-plugin-sdk.md`
