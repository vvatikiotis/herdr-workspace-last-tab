# Workspace Last Tab

A Herdr plugin that remembers up to six distinct recently used tabs per workspace and session, including the current tab. Repeated invocations still toggle only the newest two surviving tabs. Closing a tab removes it from history so older retained tabs become available; Herdr chooses the replacement when the active tab closes. Closing a workspace removes its entire history.

## Install locally

Requires Rust 1.89 or newer and Herdr 0.9.5 or newer on macOS or Linux.

```sh
cargo build --release --locked
herdr plugin link "$PWD"
```

Local linking does not run build commands. The manifest's build command is for GitHub installs.

Bind the binary and subcommand in Herdr's `config.toml` (replace the absolute path with your linked plugin directory):

```toml
[[keys.command]]
key = "prefix+a"
type = "shell"
command = "/absolute/path/to/herdr-workspace-last-tab/target/release/herdr-workspace-last-tab switch"
description = "jump to last tab in this workspace"
```

The startup hook runs at the next server startup (including live handoff), not on linking, client attachment, or config reload. Restart your server only when convenient; restarting can affect running terminals. No shell alias, `nohup`, or polling watcher is needed.

The previous `herdr-workspace-last-tab-bin` executable and its old state directory are no longer used. Remove any old shell wrapper that starts `watch`. The old executable and state are not deleted automatically.

## Runtime and storage

Herdr runs `listen` as the plugin startup command. It subscribes to tab-focus, workspace-metadata, tab-close, and workspace-close events and exits when that server connection closes. A session-local lock prevents duplicate listeners. Herdr does not supervise or automatically restart failed startup commands; inspect failures with:

```sh
herdr plugin log list --plugin user.herdr-workspace-last-tab
```

`switch` is a short-lived command that requests a switch from the listener and waits for completion (up to five seconds). It does not independently advance history from a potentially ahead-of-stream snapshot. Both commands require Herdr's injected `HERDR_SOCKET_PATH`. The registered plugin action remains supported, but is not required for the shell binding.

`HERDR_PLUGIN_STATE_DIR` takes precedence when provided. Ordinary shell commands do not receive it, so they resolve the same plugin state directory as Herdr: `$XDG_STATE_HOME/herdr/plugins/user.herdr-workspace-last-tab`, or `$HOME/.local/state/herdr/plugins/user.herdr-workspace-last-tab` if `XDG_STATE_HOME` is unset. Shell commands and startup hooks therefore share history and session locks.

A session-specific action lock serializes complete shortcut invocations, including their completion acknowledgment. It is separate from the listener's state lock, so the listener can continue processing events while an action waits.

Requests use the private workspace token `workspace_last_tab_control` with a five-second TTL. This token carries request identifiers, not tab history. The listener replies with a second marker before executing the request. Herdr 0.9.5 drains the focus subscription before the metadata subscription; a reply generated while handling one metadata batch cannot occur in that already-captured batch. The next focus drain therefore processes preceding focus events before the reply. A completion marker acknowledges the saved switch. Only the listener writes history, and duplicate/expired markers are ignored. A failed focus request returns an error acknowledgment without stopping tracking; controls for closed workspaces are ignored. This ordering depends on Herdr's subscription-batch implementation: rerun the real-session test when upgrading Herdr.

History lives under `HERDR_PLUGIN_STATE_DIR/sessions/<hex-encoded-socket-path>/state.json`. Socket identity separates sessions even when their workspace/tab IDs overlap and remains stable across server restarts. The listener prunes invalid references and reconciles restored active tabs on startup. Existing history is preserved; old standalone watcher history is not migrated.

Version 0.3.0 reads the previous plugin's `current_tab_id`/`last_tab_id` pair and saves a most-recent-first `tab_ids` list in the same file. It preserves valid existing entries and learns additional tabs as they are used, rather than filling the list with unvisited open tabs. A seventh distinct tab permanently evicts the oldest entry; closing newer entries does not restore it. Revisits move a tab to the front without consuming another slot.

After rebuilding, restart the Herdr server when convenient to replace its running listener. Rebuilding or reloading configuration alone does not replace the old two-tab listener.

The listener is the sole history writer. State updates use an OS file lock and atomic replacement through a sibling `state.json.new` file in the state directory, never `/tmp`. Invalid state JSON causes an explicit failure rather than silently resetting history.

Tab-focus payloads preserve intermediate tab changes even when a snapshot already shows a later active tab. Closure events remove their entries directly; snapshot pruning runs on startup and at the prepared-action barrier, after preceding focus events have drained. This prevents an ahead-of-stream closure snapshot from erasing visits before the six-entry limit is enforced. Pane changes that repeat the same active tab do not change history.

Herdr 0.9.5 guarantees ordering within the tab-focus subscription, not across different event types. If closures and visits overlap inside a delivery batch, the plugin must use their delivery order. A closure delivered after a later focus can therefore cause an older entry to be evicted early. Exact cross-type chronology requires ordering information from Herdr that the current API does not expose.

Event subscriptions do not replay events from before connection. Startup reconciliation restores current state, not unknowable changes made while the listener was absent. A server-reported `events_lost` or socket error fails the listener explicitly; no accurate history can be reconstructed for dropped events. EOF ends it normally. As with other unsupervised startup commands, a failed listener requires restarting it/server after investigating the plugin logs.

## Tests

```sh
mkdir -p tmp
export TMPDIR="$PWD/tmp"
cargo check
cargo test
cargo clippy --all-targets -- -D warnings
cargo build --release --locked
python3 tests/live.py
```

`cargo test` runs the Python 3 mock-socket command tests. They exercise history through the plugin commands and focus requests, not by asserting on saved JSON. The real Herdr test uses private config/state directories and a disposable headless session, checks startup/event delivery, shell invocation, and optional plugin-action dispatch, verifies restart persistence, six-tab retention, closure fallback, and eviction, and stops only its own server. Temporary test environments are created under this repository's `tmp/` directory. It does not simulate physical keyboard input or modify the active session's registry.

See [SPEC.md](SPEC.md) for agreed behavior and [GLOSSARY.md](GLOSSARY.md) for terminology.
