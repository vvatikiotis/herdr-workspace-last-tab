# Workspace Last Tab

Requirements for six-tab workspace history. The shortcut and listener lifecycle remain unchanged; the history replaces the earlier two-tab pair.

## Tracking

- A Herdr plugin startup hook launches one event listener per session. It subscribes to server events and processes tab-focus events sequentially, without polling or a shell-started background watcher.
- Individual event hooks must not update history: Herdr runs those hooks concurrently without guaranteeing execution order.
- The listener exits when its server connection closes. Herdr does not supervise startup commands or automatically restart a failed listener.
- Keep at most six distinct tabs total per workspace, including the current tab, ordered from most to least recently used. Preserve workspace and session isolation.
- Initialize a newly tracked workspace with only its active tab. Do not invent history for other open tabs.
- Visiting a tab moves it to the most-recent position without creating a duplicate. For A → B → C → A, recency order is A, C, B.
- Repeated observations of the same active tab do not change history.
- Visiting a seventh distinct tab evicts the least recently used entry completely. Closing newer tabs does not restore evicted entries; an evicted tab re-enters history only when visited again.

## Switching and closure

- The shortcut invokes the binary path with the `switch` subcommand as a Herdr shell command; a qualified plugin-action ID is not required.
- The shortcut targets only the focused workspace's most recently used surviving retained tab other than the active tab. Without such a tab, it does nothing.
- Successful switches update recency, so repeated presses toggle between the newest two surviving tabs. Do not add a picker or history traversal command.
- Closing any tab removes it from history and preserves the remaining order. Older retained tabs become eligible as the last tab. For A, B, C, D with A active, closing B leaves A, C, D; the shortcut then toggles A ↔ C.
- When the active tab closes, let Herdr choose the replacement as usual. Record that activated tab as the most recently used tab; the plugin must not select a replacement automatically.
- For A → B → C, closing C and activating B leaves B, A; the shortcut can then switch to A. This replaces the earlier no-fallback behavior.
- Closing a workspace deletes its entire history, without affecting other workspaces.

## Restart and upgrade

- Preserve retained history across Herdr server restarts. Remove references to workspaces and tabs that no longer exist.
- Move the restored active tab to the most-recent position, preserving the other entries' order and the six-tab limit.
- When replacing the two-tab version, preserve its saved current/last pair in recency order, discard invalid references, and build toward six as more tabs are visited. Do not invent prior history for other open tabs.

## Implementation constraints

- Store plugin-owned history under `HERDR_PLUGIN_STATE_DIR` when provided. Shell invocations without that variable resolve the same directory using `$XDG_STATE_HOME/herdr/plugins/user.herdr-workspace-last-tab`, or `$HOME/.local/state/herdr/plugins/user.herdr-workspace-last-tab` when `XDG_STATE_HOME` is unset. Do not create separate history or lock namespaces for shell invocations.
- Keep histories isolated by Herdr session; do not assume the directory itself is session-specific.

## Verified implementation choices

- Herdr 0.9.5 startup hooks launch commands asynchronously and do not supervise them. Individual event hooks also run concurrently, so they cannot serialize history updates.
- The listener subscribes to `tab.focused`, `workspace.metadata_updated`, `tab.closed`, and `workspace.closed`. Focus events are ordered within their subscription; different event types have no shared ordering guarantee. Live snapshots prune references to closed objects.
- Shortcut actions send a request through the private `workspace_last_tab_control` metadata token (five-second TTL), then wait for completion. The listener uses a second marker to ensure another focus drain before processing the request, using Herdr 0.9.5's focus-before-metadata subscription batch order. Only the listener advances history. Markers carry request identifiers, not saved history; duplicate or expired markers are ignored. The real-session test must be rerun on Herdr upgrades because this barrier depends on the server's batch implementation.
- Snapshot pruning runs on startup and at the prepared-action barrier after preceding focus events drain; closure events remove entries directly. Ahead-of-stream closure snapshots must not erase queued visits before the capacity limit is enforced.
- Focus history uses event payloads rather than replacing the active tab with the latest snapshot on every event. Mock-socket tests cover queued intermediate tab changes; the disposable Herdr test covers real event delivery and plugin-action dispatch.
- `HERDR_PLUGIN_STATE_DIR` is shared by sessions. Hex-encoded socket paths provide session-specific subdirectories and survive server restarts.
- State is JSON, with one listener as the sole writer, OS file locks preventing conflicting writes/duplicate listeners, and atomic replacement through a sibling file in the state directory.
- Each workspace is saved as a most-recent-first `tab_ids` list. The loader also accepts the previous `current_tab_id`/`last_tab_id` pair; startup writes the migrated list back to the same state file.
- A separate per-session action lock serializes complete shortcut invocations until completion acknowledgment, so a later action cannot overtake delayed focus events from an earlier action. Request-specific focus or acknowledgment failures do not terminate tracking in other workspaces.
- Subscription EOF exits normally. Other socket/protocol errors (including `events_lost`) fail explicitly; startup reconciliation cannot reconstruct missing tab changes. See README.md for lifecycle limitations.
