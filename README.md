# Workspace Last Tab

A Herdr plugin that remembers the previous used tab independently in each workspace and session. Repeated invocations toggle between the current and last tab. Closing a workspace removes its history; closing the last tab clears the reference instead of selecting an older tab.

## Install locally

Requires Rust 1.89 or newer and Herdr 0.9.5 or newer on macOS or Linux.

```sh
cargo build --release --locked
herdr plugin link "$PWD"
```

Local linking does not run build commands. The manifest's build command is for GitHub installs.

Bind the action in Herdr's `config.toml`:

```toml
[[keys.command]]
key = "prefix+a"
type = "plugin_action"
command = "user.herdr-workspace-last-tab.switch"
description = "jump to last tab in this workspace"
```

The startup hook runs at the next server startup (including live handoff), not on linking, client attachment, or config reload. Restart your server only when convenient; restarting can affect running terminals. No shell alias, `nohup`, or polling watcher is needed.

The previous `herdr-workspace-last-tab-bin` executable and its old state directory are no longer used. Remove any old shell wrapper that starts `watch`. The old executable and state are not deleted automatically.

## Runtime and storage

Herdr runs `listen` as the plugin startup command. It subscribes to tab-focus, tab-close, and workspace-close events and exits when that server connection closes. A session-local lock prevents duplicate listeners. Herdr does not supervise or automatically restart failed startup commands; inspect failures with:

```sh
herdr plugin log list --plugin user.herdr-workspace-last-tab
```

`switch` is a short-lived plugin action. Both commands require Herdr's injected `HERDR_SOCKET_PATH` and `HERDR_PLUGIN_STATE_DIR`.

History lives under `HERDR_PLUGIN_STATE_DIR/sessions/<hex-encoded-socket-path>/state.json`. Socket identity separates sessions even when their workspace/tab IDs overlap and remains stable across server restarts. The listener prunes invalid references and reconciles restored active tabs on startup. Existing history is preserved; old standalone watcher history is not migrated.

State updates use an OS file lock and atomic replacement through a sibling `state.json.new` file in the state directory, never `/tmp`. Invalid state JSON causes an explicit failure rather than silently resetting history.

Tab-focus payloads preserve intermediate tab changes even when a snapshot already shows a later active tab. Herdr guarantees ordering within the tab-focus subscription, not across different subscription types; authoritative snapshots remove closed tabs and workspaces. Pane changes that repeat the same active tab do not change history.

Event subscriptions do not replay events from before connection. Startup reconciliation restores current state, not unknowable changes made while the listener was absent. A server-reported `events_lost` or socket error fails the listener explicitly; no accurate history can be reconstructed for dropped events. EOF ends it normally. As with other unsupervised startup commands, a failed listener requires restarting it/server after investigating the plugin logs.

## Tests

```sh
cargo check
cargo test
cargo clippy --all-targets -- -D warnings
cargo build --release --locked
python3 tests/live.py
```

`cargo test` runs the Python 3 mock-socket command tests. They exercise history through the plugin commands and focus requests, not by asserting on saved JSON. The real Herdr test uses private config/state directories and a disposable headless session, checks startup/event delivery and plugin-action dispatch, and stops only its own server. It does not simulate physical keyboard input or modify the active session's registry.

See [SPEC.md](SPEC.md) for agreed behavior and [GLOSSARY.md](GLOSSARY.md) for terminology.
