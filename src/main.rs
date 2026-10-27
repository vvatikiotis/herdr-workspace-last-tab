use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    env, fs,
    io::{self, BufRead, BufReader, Write},
    os::unix::{ffi::OsStrExt, net::UnixStream},
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

#[derive(Default, Deserialize, Serialize)]
struct State {
    #[serde(default)]
    workspaces: HashMap<String, WorkspaceHistory>,
}

#[derive(Default, Deserialize, Serialize)]
struct WorkspaceHistory {
    current_tab_id: Option<String>,
    last_tab_id: Option<String>,
}

#[derive(Deserialize)]
struct Snapshot {
    focused_workspace_id: Option<String>,
    workspaces: Vec<Workspace>,
    tabs: Vec<Tab>,
}

#[derive(Deserialize)]
struct Workspace {
    workspace_id: String,
    active_tab_id: Option<String>,
}

#[derive(Deserialize)]
struct Tab {
    tab_id: String,
    workspace_id: String,
}

fn required_path(name: &str) -> io::Result<PathBuf> {
    env::var_os(name).map(PathBuf::from).ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{name} is required; run from Herdr so its session context is available"),
        )
    })
}

fn plugin_state_directory() -> io::Result<PathBuf> {
    if let Some(directory) = env::var_os("HERDR_PLUGIN_STATE_DIR") {
        return Ok(PathBuf::from(directory));
    }
    // Shell keybindings receive session context, but not plugin-specific paths.
    // Match Herdr's state directory for this plugin rather than inventing a
    // separate shell-mode history or lock namespace.
    let state_root = env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state")))
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "HERDR_PLUGIN_STATE_DIR, XDG_STATE_HOME, or HOME is required",
            )
        })?;
    Ok(state_root.join("herdr/plugins/user.herdr-workspace-last-tab"))
}

fn session_directory(socket: &Path) -> io::Result<PathBuf> {
    // Plugin state directories are shared by sessions. The socket path is stable
    // across server restarts, unlike a PID, and opaque IDs are only session-local.
    let key: String = socket
        .as_os_str()
        .as_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let directory = plugin_state_directory()?.join("sessions").join(key);
    fs::create_dir_all(&directory)?;
    Ok(directory)
}

fn lock(directory: &Path, name: &str) -> io::Result<fs::File> {
    let file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(directory.join(name))?;
    file.lock()?;
    Ok(file)
}

fn load_state(directory: &Path) -> io::Result<State> {
    match fs::read(directory.join("state.json")) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(io::Error::other),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(State::default()),
        Err(error) => Err(error),
    }
}

fn save_state(directory: &Path, state: &State) -> io::Result<()> {
    let temporary = directory.join("state.json.new");
    let mut file = fs::File::create(&temporary)?;
    serde_json::to_writer(&mut file, state)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    fs::rename(temporary, directory.join("state.json"))?;
    fs::File::open(directory)?.sync_all()
}

fn send_request(stream: &mut UnixStream, method: &str, params: Value) -> io::Result<()> {
    serde_json::to_writer(
        &mut *stream,
        &json!({"id": method, "method": method, "params": params}),
    )?;
    stream.write_all(b"\n")?;
    stream.flush()
}

fn read_json(reader: &mut BufReader<UnixStream>) -> io::Result<Value> {
    let mut line = String::new();
    if reader.read_line(&mut line)? == 0 {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "Herdr socket closed",
        ));
    }
    let response: Value = serde_json::from_str(&line).map_err(io::Error::other)?;
    if let Some(error) = response.get("error") {
        return Err(io::Error::other(error.to_string()));
    }
    Ok(response)
}

fn request(socket: &Path, method: &str, params: Value) -> io::Result<Value> {
    let mut stream = UnixStream::connect(socket)?;
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;
    send_request(&mut stream, method, params)?;
    read_json(&mut BufReader::new(stream))
}

fn snapshot(socket: &Path) -> io::Result<Snapshot> {
    let response = request(socket, "session.snapshot", json!({}))?;
    serde_json::from_value(response["result"]["snapshot"].clone()).map_err(io::Error::other)
}

fn valid_tab(snapshot: &Snapshot, workspace: &str, tab: &str) -> bool {
    snapshot
        .tabs
        .iter()
        .any(|t| t.workspace_id == workspace && t.tab_id == tab)
}

fn prune(state: &mut State, snapshot: &Snapshot) {
    let workspaces: HashSet<&str> = snapshot
        .workspaces
        .iter()
        .map(|w| w.workspace_id.as_str())
        .collect();
    state.workspaces.retain(|id, history| {
        if !workspaces.contains(id.as_str()) {
            return false;
        }
        if history
            .last_tab_id
            .as_ref()
            .is_some_and(|t| !valid_tab(snapshot, id, t))
        {
            history.last_tab_id = None;
        }
        if history
            .current_tab_id
            .as_ref()
            .is_some_and(|t| !valid_tab(snapshot, id, t))
        {
            history.current_tab_id = None;
        }
        true
    });
}

fn focus(state: &mut State, workspace: &str, tab: Option<String>) {
    let history = state.workspaces.entry(workspace.to_owned()).or_default();
    if history.current_tab_id != tab {
        history.last_tab_id = history.current_tab_id.take();
        history.current_tab_id = tab;
    }
}

fn reconcile(state: &mut State, snapshot: &Snapshot) {
    prune(state, snapshot);
    for workspace in &snapshot.workspaces {
        focus(
            state,
            &workspace.workspace_id,
            workspace.active_tab_id.clone(),
        );
    }
}

fn listen(socket: &Path, directory: &Path) -> io::Result<()> {
    let singleton = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(directory.join("listener.lock"))?;
    match singleton.try_lock() {
        Ok(()) => (),
        Err(fs::TryLockError::WouldBlock) => return Ok(()),
        Err(error) => return Err(io::Error::other(error)),
    }
    let mut stream = UnixStream::connect(socket)?;
    send_request(
        &mut stream,
        "events.subscribe",
        json!({"subscriptions": [
            {"type": "tab.focused"}, {"type": "workspace.metadata_updated"},
            {"type": "tab.closed"}, {"type": "workspace.closed"}
        ]}),
    )?;
    let mut reader = BufReader::new(stream);
    let acknowledgment = read_json(&mut reader)?;
    if acknowledgment["result"]["type"] != "subscription_started" {
        return Err(io::Error::other(
            "Herdr did not acknowledge the event subscription",
        ));
    }
    {
        let _lock = lock(directory, "state.lock")?;
        let mut state = load_state(directory)?;
        reconcile(&mut state, &snapshot(socket)?);
        save_state(directory, &state)?;
    }
    let mut seen_controls = HashSet::new();
    loop {
        let event = match read_json(&mut reader) {
            Ok(event) => event,
            Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(()),
            Err(error) => return Err(error),
        };
        let _lock = lock(directory, "state.lock")?;
        let mut state = load_state(directory)?;
        let live = snapshot(socket)?;
        prune(&mut state, &live);
        let data = &event["data"];
        if event["event"] == "tab_focused" {
            let workspace = data["workspace_id"]
                .as_str()
                .ok_or_else(|| io::Error::other("missing workspace_id"))?;
            let tab = data["tab_id"]
                .as_str()
                .ok_or_else(|| io::Error::other("missing tab_id"))?;
            // Focus payloads, not the latest active tab from a snapshot, retain
            // intermediate changes. Ordering is guaranteed within tab.focused.
            if live.workspaces.iter().any(|w| w.workspace_id == workspace) {
                // Even a now-closed tab represents a real transition. Dropping
                // it would incorrectly retain older history on returning to B
                // after queued B -> C -> B events when C has already closed.
                focus(&mut state, workspace, Some(tab.to_owned()));
            }
        }
        let mut completion = None;
        if let Some((workspace, token)) = control(&event) {
            seen_controls.retain(|token: &String| fresh_control(token));
            if live.workspaces.iter().any(|w| w.workspace_id == workspace)
                && fresh_control(token)
                && seen_controls.insert(token.to_owned())
            {
                if let Some(id) = token.strip_prefix("r:") {
                    // Herdr 0.9.5 drains focus batches before metadata batches.
                    // A reply emitted here cannot be in the metadata batch
                    // already being delivered: another focus drain precedes it.
                    if let Err(error) = report_control(socket, workspace, &format!("p:{id}")) {
                        eprintln!("herdr-workspace-last-tab: could not prepare action: {error}");
                    }
                } else if let Some(id) = token.strip_prefix("p:") {
                    let phase = match switch_workspace(socket, &mut state, &live, workspace) {
                        Ok(()) => "d",
                        Err(error) => {
                            eprintln!("herdr-workspace-last-tab: action failed: {error}");
                            "e"
                        }
                    };
                    completion = Some((workspace.to_owned(), format!("{phase}:{id}")));
                }
            }
        }
        prune(&mut state, &live);
        save_state(directory, &state)?;
        if let Some((workspace, token)) = completion {
            if let Err(error) = report_control(socket, &workspace, &token) {
                eprintln!("herdr-workspace-last-tab: could not acknowledge action: {error}");
            }
        }
    }
}

fn switch_workspace(
    socket: &Path,
    state: &mut State,
    live: &Snapshot,
    workspace: &str,
) -> io::Result<()> {
    let active = live
        .workspaces
        .iter()
        .find(|w| w.workspace_id == workspace)
        .and_then(|w| w.active_tab_id.as_deref());
    let target = state
        .workspaces
        .get(workspace)
        .and_then(|h| h.last_tab_id.clone());
    if let Some(tab) =
        target.filter(|t| Some(t.as_str()) != active && valid_tab(live, workspace, t))
    {
        request(socket, "tab.focus", json!({"tab_id": tab}))?;
        focus(state, workspace, Some(tab));
    }
    Ok(())
}

const CONTROL_KEY: &str = "workspace_last_tab_control";
const CONTROL_TIMEOUT: Duration = Duration::from_secs(5);

fn control(event: &Value) -> Option<(&str, &str)> {
    if event["event"] != "workspace_metadata_updated" {
        return None;
    }
    let workspace = &event["data"]["workspace"];
    Some((
        workspace["workspace_id"].as_str()?,
        workspace["tokens"][CONTROL_KEY].as_str()?,
    ))
}

fn now_nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
}

fn fresh_control(token: &str) -> bool {
    token
        .split(':')
        .nth(1)
        .and_then(|n| n.parse::<u128>().ok())
        .is_some_and(|time| {
            let now = now_nanos();
            time <= now && now - time < CONTROL_TIMEOUT.as_nanos()
        })
}

fn report_control(socket: &Path, workspace: &str, token: &str) -> io::Result<()> {
    request(
        socket,
        "workspace.report_metadata",
        json!({
            "workspace_id": workspace, "source": "user.herdr-workspace-last-tab",
            "tokens": {CONTROL_KEY: token}, "ttl_ms": 5000
        }),
    )?;
    Ok(())
}

fn switch(socket: &Path, directory: &Path) -> io::Result<()> {
    // Serialize whole actions, not just writes: the preceding action's focus
    // event must drain through the next request's barrier before another toggle.
    let _action_lock = lock(directory, "action.lock")?;
    let Some(workspace) = snapshot(socket)?.focused_workspace_id else {
        return Ok(());
    };
    let mut stream = UnixStream::connect(socket)?;
    stream.set_read_timeout(Some(CONTROL_TIMEOUT))?;
    stream.set_write_timeout(Some(CONTROL_TIMEOUT))?;
    send_request(
        &mut stream,
        "events.subscribe",
        json!({"subscriptions": [{"type": "workspace.metadata_updated"}]}),
    )?;
    let mut reader = BufReader::new(stream);
    let acknowledgment = read_json(&mut reader)?;
    if acknowledgment["result"]["type"] != "subscription_started" {
        return Err(io::Error::other(
            "Herdr did not acknowledge the action subscription",
        ));
    }
    let id = format!("{}:{}", now_nanos(), std::process::id());
    report_control(socket, &workspace, &format!("r:{id}"))?;
    let deadline = std::time::Instant::now() + CONTROL_TIMEOUT;
    loop {
        let remaining = deadline.checked_duration_since(std::time::Instant::now()).ok_or_else(|| {
            io::Error::new(io::ErrorKind::TimedOut, "tab-history listener did not acknowledge the action; check plugin startup logs")
        })?;
        reader.get_ref().set_read_timeout(Some(remaining))?;
        let event = read_json(&mut reader).map_err(|error| {
            if matches!(error.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) {
                io::Error::new(io::ErrorKind::TimedOut, "tab-history listener did not acknowledge the action; check plugin startup logs")
            } else { error }
        })?;
        if control(&event) == Some((workspace.as_str(), format!("d:{id}").as_str())) {
            return Ok(());
        }
        if control(&event) == Some((workspace.as_str(), format!("e:{id}").as_str())) {
            return Err(io::Error::other(
                "tab-history listener could not focus the last tab; the target may have closed",
            ));
        }
    }
}

fn run() -> io::Result<()> {
    let command = env::args().nth(1).unwrap_or_default();
    if command != "listen" && command != "switch" {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "usage: herdr-workspace-last-tab <listen|switch>",
        ));
    }
    let socket = required_path("HERDR_SOCKET_PATH")?;
    let directory = session_directory(&socket)?;
    match command.as_str() {
        "listen" => listen(&socket, &directory),
        _ => switch(&socket, &directory),
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("herdr-workspace-last-tab: {error}");
        std::process::exit(1);
    }
}
