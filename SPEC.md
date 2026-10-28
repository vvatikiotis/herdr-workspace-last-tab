# Workspace Last Tab: behavior specification

Terminology follows [GLOSSARY.md](GLOSSARY.md): [current tab](GLOSSARY.md#current-tab), [last tab](GLOSSARY.md#last-tab), and [workspace tab history](GLOSSARY.md#workspace-tab-history). Definitions are maintained only there.

Requirement IDs are stable: keep them when wording changes and do not reuse retired IDs. The design references these IDs without making the requirements implementation-dependent.

<a id="history"></a>
## Workspace tab history

- <a id="h1"></a> **H1:** Limit workspace tab history to six distinct tabs, including the current tab, ordered from most to least recently used.
- <a id="h2"></a> **H2:** Keep workspace tab history independent for each workspace and Herdr session.
- <a id="h3"></a> **H3:** Initialize workspace tab history with only the current tab. Do not add other open tabs until they are visited.
- <a id="h4"></a> **H4:** Visiting a tab moves it to the most-recent position in workspace tab history without creating a duplicate. For A → B → C → A, recency order is A, C, B.
- <a id="h5"></a> **H5:** Remaining on the current tab does not change workspace tab history.
- <a id="h6"></a> **H6:** Visiting a seventh distinct tab evicts the least recently used entry from workspace tab history completely. Closing newer tabs does not restore evicted entries; an evicted tab re-enters workspace tab history only when visited again.

## Switching

- <a id="s1"></a> **S1:** Expose a `switch` command so the shortcut can invoke the binary path directly.
- <a id="s2"></a> **S2:** The shortcut targets only the focused workspace's last tab.
- <a id="s3"></a> **S3:** Without a last tab, the shortcut does nothing.
- <a id="s4"></a> **S4:** Successful switches update workspace tab history, so repeated presses toggle between the current tab and the last tab.
- <a id="s5"></a> **S5:** Do not add a picker or a command to traverse workspace tab history.

## Closure

- <a id="c1"></a> **C1:** Closing any tab removes it from workspace tab history and preserves the remaining order. Older entries can become the last tab.
- <a id="c2"></a> **C2:** For workspace tab history A, B, C, D with A as the current tab, closing B leaves A, C, D; the shortcut then toggles A ↔ C.
- <a id="c3"></a> **C3:** When the current tab closes, let Herdr choose the replacement as usual. Record the new current tab at the most-recent position in workspace tab history; the plugin must not select a replacement automatically.
- <a id="c4"></a> **C4:** For A → B → C, closing C and making B the current tab leaves workspace tab history B, A; the shortcut can then switch to A.
- <a id="c5"></a> **C5:** Closing a workspace deletes its workspace tab history, without affecting other workspaces.

## Startup and restart

- <a id="r1"></a> **R1:** Start tracking automatically when the Herdr server starts, without requiring a shell startup wrapper or a separately launched tracking command.
- <a id="r2"></a> **R2:** Preserve workspace tab history across server restarts. Remove references to workspaces and tabs that no longer exist.
- <a id="r3"></a> **R3:** Move the restored current tab to the most-recent position in workspace tab history, preserving the other entries' order and the six-tab limit.

## Upgrade

- <a id="u1"></a> **U1:** When replacing the two-tab plugin version, preserve its workspace tab history in recency order, discard invalid references, and build toward six as more tabs are visited.
- <a id="u2"></a> **U2:** Do not invent earlier entries in workspace tab history for other open tabs.

Installation and operation are documented in [README.md](README.md). Architecture, storage, synchronization, testing, and current implementation limitations belong in [DESIGN-IMPLEMENTATION.md](DESIGN-IMPLEMENTATION.md).
