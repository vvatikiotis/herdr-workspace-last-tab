# Workspace Last Tab: design and implementation

This document explains how the plugin implements [SPEC.md](SPEC.md). Requirement links point directly to stable IDs in the spec; known gaps are identified separately. For installation and operation, see [README.md](README.md).

## Processes and lifecycle

Implements: [R1](SPEC.md#r1), [S1](SPEC.md#s1).

The plugin is declared in `herdr-plugin.toml`. Its startup command launches `target/release/herdr-workspace-last-tab listen`. The `switch` command is invoked by a shell keybinding; a registered plugin action remains available as an alternative.

Herdr 0.9.5 runs startup hooks after restoring its session and making its API socket available. They run again during server startup or live handoff, but not when a client attaches, configuration reloads, or a plugin is linked or enabled. Startup commands are asynchronous and unsupervised: Herdr does not automatically restart a failed listener.

The listener subscribes to:

- `tab.focused`
- `workspace.metadata_updated`
- `tab.closed`
- `workspace.closed`

There is one listener per session, guarded by `listener.lock`. It exits normally on subscription EOF. Other socket/protocol errors, including `events_lost`, fail explicitly. Subscription history is not durable and does not replay events from before connection. Startup reconciliation restores current state, not unknowable changes made while tracking was absent.

Individual plugin event hooks are deliberately not used to write history. Herdr launches them concurrently without guaranteeing execution or completion order. A single subscription reader processes focus events sequentially without polling.

Both commands require `HERDR_SOCKET_PATH` from their Herdr invocation environment. The raw socket implementation supports macOS and Linux.

## History model

Implements: [H1](SPEC.md#h1), [H3](SPEC.md#h3), [H4](SPEC.md#h4), [H5](SPEC.md#h5), [H6](SPEC.md#h6).

`State` maps workspace IDs to `WorkspaceHistory`. Each history contains a `tab_ids` list, most recently used first, capped at six entries including the current tab.

On focus, the listener removes an existing occurrence of the tab, inserts it at the front, and truncates the list. Repeated observations of the same active tab preserve its order and do not consume another slot. Evicted entries are not kept elsewhere.

Focus payloads preserve intermediate visits even when the latest snapshot already shows a later active tab. Snapshot pruning runs at startup and at the prepared-action boundary described below, after preceding focus events have drained. Pruning on every snapshot would erase entries for subsequently closed tabs before queued visits had counted toward capacity, potentially retaining an entry that should have been evicted.

### Switching

Implements: [S2](SPEC.md#s2), [S3](SPEC.md#s3), [S4](SPEC.md#s4), [S5](SPEC.md#s5).

The switch target is the most recent valid retained tab other than the live active tab in that workspace. With no such target, the command does nothing. A successful focus updates history synchronously, so subsequent requests can toggle back. The command exposes no picker or history traversal mode.

### Closure

Implements: [C1](SPEC.md#c1), [C2](SPEC.md#c2), [C3](SPEC.md#c3), [C4](SPEC.md#c4), [C5](SPEC.md#c5).

Closure events remove the tab or workspace entry directly, preserving the remaining entries' order. The plugin does not choose a replacement after a closure; Herdr's resulting focus change updates recency. Removing a newer entry makes an older retained tab eligible for switching.

### Startup reconciliation

Implements: [H3](SPEC.md#h3), [R2](SPEC.md#r2), [R3](SPEC.md#r3).

Reconciliation removes invalid workspace/tab references, removes duplicate entries, enforces the limit, and moves each restored workspace's active tab to the front. With no saved history, recording the active tab creates only that entry. Switching panes without changing the active tab does not change recency.

## State directory and session isolation

Implements: [H2](SPEC.md#h2).

`HERDR_PLUGIN_STATE_DIR` is authoritative when provided. Shell keybindings do not receive that plugin-specific variable, so the command resolves the same directory Herdr uses:

1. `$XDG_STATE_HOME/herdr/plugins/user.herdr-workspace-last-tab`
2. `$HOME/.local/state/herdr/plugins/user.herdr-workspace-last-tab` when `XDG_STATE_HOME` is unset

The plugin state directory is shared by Herdr sessions. Session-specific files live under:

```text
<plugin-state-dir>/sessions/<hex-encoded-socket-path>/
  listener.lock
  action.lock
  state.lock
  state.json
```

Socket identity prevents collisions between sessions whose workspace/tab IDs overlap. It remains stable across server restarts, unlike a process ID. Shell commands and startup hooks must resolve the same history and lock namespace.

## Persistence and migration

Implements: [R2](SPEC.md#r2), [U1](SPEC.md#u1), [U2](SPEC.md#u2).

The listener is the sole history writer. It saves JSON in this form:

```json
{
  "workspaces": {
    "w1": { "tab_ids": ["w1:t3", "w1:t1", "w1:t2"] }
  }
}
```

The loader also accepts the previous plugin format:

```json
{
  "workspaces": {
    "w1": { "current_tab_id": "w1:t3", "last_tab_id": "w1:t1" }
  }
}
```

Version 0.3.0 preserves the valid current/last pair in recency order and writes the migrated list back to the same file during startup. Additional open tabs are not used to fabricate older history. If a list is present, it takes precedence over legacy fields.

The earlier standalone polling watcher's state directory and `herdr-workspace-last-tab-bin` executable are not used or migrated. Old files are not deleted automatically.

Writes hold `state.lock`, serialize to a sibling `state.json.new`, sync the file, rename it over `state.json`, and sync the directory. The staging file stays in the state directory, not a system temporary directory. A missing state file initializes empty history; invalid JSON fails explicitly rather than silently resetting it.

## Shortcut synchronization

Implements: [H4](SPEC.md#h4), [S2](SPEC.md#s2), [S3](SPEC.md#s3), [S4](SPEC.md#s4).

A latest-active snapshot cannot reveal intermediate tab visits still waiting in the subscription stream. Therefore `switch` does not independently advance history. It requests work from the listener and waits for completion.

A session-specific `action.lock` is held from before the command's initial snapshot through completion/error acknowledgment. This serializes entire shortcut invocations, not merely writes. It is separate from `state.lock`, allowing the listener to continue while a command waits. Without this serialization, delayed focus events from one action could roll history backward before a concurrent prepared request executes.

Requests use the private workspace token `workspace_last_tab_control`, with a five-second TTL. Tokens contain request identifiers, not saved history.

The exchange is:

1. `switch` subscribes to workspace metadata updates and reports an `r:` request token.
2. The listener receives it and reports a `p:` prepared token.
3. When that prepared token arrives, the listener prunes invalid references, chooses the target, performs the focus, updates history, and saves it.
4. The listener reports a `d:` completion token, or an `e:` token for a failed focus request. The waiting command returns success or error accordingly.

Duplicate and expired control tokens are ignored. The command waits up to five seconds for completion after reporting its request. A failed focus request does not terminate tracking; preparation/completion reporting failures are logged rather than propagated out of the listener. Controls for workspaces absent from the live snapshot are ignored.

### Why two markers are needed

The implementation relies on Herdr 0.9.5's subscription-batch behavior. The listener requests the focus subscription before the metadata subscription. Herdr drains subscriptions in that order and captures each batch before writing its events.

A prepared token generated while handling a request cannot be part of the metadata batch already captured for delivery. Another focus drain must therefore precede delivery of the prepared token. Focus events emitted before the request are consumed before target selection. The action lock ensures this also holds between successive shortcut invocations and their generated focus events.

This is a dependency on the server's implementation, not a published guarantee of ordering across event types. Rerun the real-session test when upgrading Herdr.

## Event-ordering limitation

Known gap affecting: [H6](SPEC.md#h6), [C1](SPEC.md#c1).

Herdr 0.9.5 guarantees ordering within the tab-focus subscription, not across different event types. If closures and visits overlap inside a delivery batch, the plugin must use their delivery order. A closure delivered after a later focus can cause an older entry to be evicted early.

This limits the eviction and closure behavior required by [H6](SPEC.md#h6), [C1](SPEC.md#c1). Exact cross-type chronology requires ordering information that the current Herdr API does not expose. The shortcut synchronization mechanism does not supply a general ordering number for all server events.

## Build and validation

Requires Rust 1.89 or newer. Local `herdr plugin link` does not run build commands; build the release binary first. The manifest build command runs for GitHub installs.

From the repository root:

```sh
mkdir -p tmp
export TMPDIR="$PWD/tmp"
cargo fmt --check
cargo check
cargo test
cargo clippy --all-targets -- -D warnings
cargo build --release --locked
python3 tests/live.py
```

`cargo test` runs `tests/protocol.rs`, which invokes the Python 3 mock-socket tests in `tests/protocol.py`. These tests exercise public commands and focus requests, not private Rust functions or saved-JSON assertions. Legacy JSON is supplied as migration input. Coverage includes isolation, toggling, closure fallback, bounded distinct history, eviction, migration, restart persistence, queued focus events, concurrent actions, and request-specific error recovery.

`tests/live.py` uses private config/state directories and a disposable headless Herdr server. It checks startup/event delivery, direct shell invocation, optional plugin-action dispatch, rapid focus changes, restart persistence, six-tab retention, closure fallback, and eviction. It does not simulate physical keyboard input, promise exact cross-type chronology, or modify the active session's plugin registry. It stops only its own server.

Temporary test environments are created under the repository's ignored `tmp/` directory. The live fixture uses a private default session with short paths to stay within Unix socket path limits. Restart readiness waits for the old listener's lock to be released and for the new listener to reconcile state.
