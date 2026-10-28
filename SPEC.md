# Workspace Last Tab: behavior specification

Requirement IDs are stable: keep them when wording changes and do not reuse retired IDs. The design references these IDs without making the requirements implementation-dependent.

## History

- <a id="h1"></a> **H1:** Keep at most six distinct tabs total per workspace, including the current tab, ordered from most to least recently used.
- <a id="h2"></a> **H2:** Keep history independent for each workspace and Herdr session.
- <a id="h3"></a> **H3:** Initialize a newly tracked workspace with only its active tab. Do not invent history for other open tabs.
- <a id="h4"></a> **H4:** Visiting a tab moves it to the most-recent position without creating a duplicate. For A → B → C → A, recency order is A, C, B.
- <a id="h5"></a> **H5:** Remaining on the same tab does not change history.
- <a id="h6"></a> **H6:** Visiting a seventh distinct tab evicts the least recently used entry completely. Closing newer tabs does not restore evicted entries; an evicted tab re-enters history only when visited again.

## Switching

- <a id="s1"></a> **S1:** Expose a `switch` command so the shortcut can invoke the binary path directly.
- <a id="s2"></a> **S2:** The shortcut targets only the focused workspace's most recently used surviving remembered tab other than the active tab.
- <a id="s3"></a> **S3:** Without such a tab, the shortcut does nothing.
- <a id="s4"></a> **S4:** Successful switches update recency, so repeated presses toggle between the newest two surviving tabs.
- <a id="s5"></a> **S5:** Do not add a picker or history traversal command.

## Closure

- <a id="c1"></a> **C1:** Closing any tab removes it from history and preserves the remaining order. Older retained tabs become eligible as the last tab.
- <a id="c2"></a> **C2:** For A, B, C, D with A active, closing B leaves A, C, D; the shortcut then toggles A ↔ C.
- <a id="c3"></a> **C3:** When the active tab closes, let Herdr choose the replacement as usual. Record that activated tab as the most recently used tab; the plugin must not select a replacement automatically.
- <a id="c4"></a> **C4:** For A → B → C, closing C and activating B leaves B, A; the shortcut can then switch to A.
- <a id="c5"></a> **C5:** Closing a workspace deletes its entire history, without affecting other workspaces.

## Startup and restart

- <a id="r1"></a> **R1:** Start tracking automatically when the Herdr server starts, without requiring a shell startup wrapper or a separately launched tracking command.
- <a id="r2"></a> **R2:** Preserve retained history across server restarts. Remove references to workspaces and tabs that no longer exist.
- <a id="r3"></a> **R3:** Move the restored active tab to the most-recent position, preserving the other entries' order and the six-tab limit.

## Upgrade

- <a id="u1"></a> **U1:** When replacing the two-tab plugin version, preserve its two remembered tabs in recency order, discard invalid references, and build toward six as more tabs are visited.
- <a id="u2"></a> **U2:** Do not invent prior history for other open tabs.

Installation and operation are documented in [README.md](README.md). Architecture, storage, synchronization, testing, and current implementation limitations belong in [DESIGN-IMPLEMENTATION.md](DESIGN-IMPLEMENTATION.md).
