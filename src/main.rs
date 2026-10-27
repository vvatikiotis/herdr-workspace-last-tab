use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    env, fs,
    io::{self, BufRead, BufReader, Write},
    os::unix::{ffi::OsStrExt, net::UnixStream},
    path::{Path, PathBuf},
    time::Duration,
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
            format!("{name} is required; invoke through Herdr's plugin interface"),
        )
    })
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
    let directory = required_path("HERDR_PLUGIN_STATE_DIR")?
        .join("sessions")
        .join(key);
    fs::create_dir_all(&directory)?;
    Ok(directory)
}

fn lock(directory: &Path) -> io::Result<fs::File> {
    let file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(directory.join("state.lock"))?;
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
            {"type": "tab.focused"}, {"type": "tab.closed"}, {"type": "workspace.closed"}
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
        let _lock = lock(directory)?;
        let mut state = load_state(directory)?;
        reconcile(&mut state, &snapshot(socket)?);
        save_state(directory, &state)?;
    }
    loop {
        let event = match read_json(&mut reader) {
            Ok(event) => event,
            Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(()),
            Err(error) => return Err(error),
        };
        let _lock = lock(directory)?;
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
            if live.workspaces.iter().any(|w| w.workspace_id == workspace)
                && valid_tab(&live, workspace, tab)
            {
                focus(&mut state, workspace, Some(tab.to_owned()));
            }
        }
        save_state(directory, &state)?;
    }
}

fn switch(socket: &Path, directory: &Path) -> io::Result<()> {
    let _lock = lock(directory)?;
    let live = snapshot(socket)?;
    let mut state = load_state(directory)?;
    prune(&mut state, &live);
    let target = live.focused_workspace_id.as_ref().and_then(|workspace| {
        let active = live
            .workspaces
            .iter()
            .find(|w| &w.workspace_id == workspace)?
            .active_tab_id
            .as_ref();
        let history = state.workspaces.get(workspace)?;
        let tab = history.last_tab_id.as_ref()?;
        (Some(tab) != active).then(|| (workspace.clone(), tab.clone()))
    });
    if let Some((workspace, tab)) = target {
        request(socket, "tab.focus", json!({"tab_id": tab}))?;
        focus(&mut state, &workspace, Some(tab));
    }
    save_state(directory, &state)
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
