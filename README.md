# Workspace Last Tab

Switch back to the last-used tab in your current Herdr workspace. Press the shortcut again to return to the tab you just left.

The plugin remembers six distinct tabs per workspace, including the current tab. Older remembered tabs become available when newer ones close. History stays separate for each workspace and Herdr session.

## Install

You need Herdr 0.9.5 or newer, Rust 1.89 or newer, and macOS or Linux.

From this plugin's directory:

```sh
cargo build --release --locked
herdr plugin link "$PWD"
```

Linking registers the plugin but does not start tracking in an already-running server. Complete the configuration and restart steps below.

## Configure the shortcut

Add this to Herdr's `config.toml`, replacing the absolute path with your plugin directory:

```toml
[[keys.command]]
key = "prefix+a"
type = "shell"
command = "/absolute/path/to/herdr-workspace-last-tab/target/release/herdr-workspace-last-tab switch"
description = "jump to last tab in this workspace"
```

Press your Herdr prefix, then **A** to switch tabs.

## Start tracking

Restart the Herdr server after installation. Reloading configuration or opening another client is not enough.

**Stopping the server can interrupt running terminals and agents.** When you are ready, run these commands from a terminal outside the Herdr session you are restarting:

```sh
herdr server stop
herdr
```

For a named session, add `--session NAME` after `herdr` in both commands.

Tracking starts automatically when the server starts. No shell alias or separate background command is needed.

## Everyday use

- Visit tabs normally. The plugin remembers the six most recently used distinct tabs.
- The shortcut toggles only the newest two remembered tabs; it does not cycle through all six.
- Closing a remembered tab removes it from history. An older remembered tab can then become the shortcut's target.
- When the active tab closes, Herdr chooses the replacement as usual.
- Visiting a seventh distinct tab forgets the oldest entry. That tab is remembered again only if you revisit it.
- With no other remembered tab in the workspace, the shortcut does nothing.
- Closing a workspace clears its history. Other workspaces are unaffected.

History survives server restarts for tabs that Herdr restores.

## Update

From the plugin directory, rebuild and refresh its registration:

```sh
cargo build --release --locked
herdr plugin link "$PWD"
```

Then restart the server as described above. Rebuilding or reloading configuration alone does not replace the running listener, the process that records tab changes.

Existing two-tab plugin history is preserved automatically and grows toward six as you use more tabs. If you previously used the standalone polling watcher, remove its shell startup wrapper; that older watcher's history is not imported.

## Troubleshooting

If the shortcut does nothing:

1. Visit two different tabs in the same workspace, then try it again.
2. Check that the configured binary path exists and includes the `switch` argument.
3. Check registration and recent plugin errors:

   ```sh
   herdr plugin list
   herdr plugin log list --plugin user.herdr-workspace-last-tab
   ```

4. If you see `tab-history listener did not acknowledge the action`, tracking may not be running. Restart the server when it is safe to do so. Herdr does not automatically restart a failed listener.

**Known limitation:** very rapid sequences of tab changes and closures can forget an older entry earlier than expected. See the [design and implementation notes](DESIGN-IMPLEMENTATION.md#event-ordering-limitation) for the technical cause.

## Further reading

- [Behavior specification](SPEC.md)
- [Design, implementation, and testing](DESIGN-IMPLEMENTATION.md)
- [Terminology](GLOSSARY.md)
