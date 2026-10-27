use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    env, fs,
    io::{self, BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::{Path, PathBuf},
    thread,
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
struct Envelope {
    result: ResultBody,
}

#[derive(Deserialize)]
struct ResultBody {
    snapshot: Snapshot,
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

fn config_dir() -> PathBuf {
    env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))
        .expect("HOME is not set")
}

fn state_path() -> PathBuf {
    env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state")))
        .expect("HOME is not set")
        .join("herdr-workspace-last-tab/state.json")
}

fn socket_path() -> PathBuf {
    if let Some(path) = env::var_os("HERDR_SOCKET_PATH") {
        return PathBuf::from(path);
    }
    let session = env::var("HERDR_SESSION").ok();
    let root = config_dir().join("herdr");
    match session {
        Some(name) => root.join("sessions").join(name).join("herdr.sock"),
        None => root.join("herdr.sock"),
    }
}

fn load_state(path: &Path) -> State {
    fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default()
}

fn save_state(path: &Path, state: &State) -> io::Result<()> {
    let directory = path.parent().expect("state path has a parent");
    fs::create_dir_all(directory)?;
    let temporary = directory.join("state.json.tmp");
    let mut file = fs::File::create(&temporary)?;
    serde_json::to_writer(&mut file, state)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    fs::rename(temporary, path)
}

fn send_request(stream: &mut UnixStream, id: &str, method: &str, params: Value) -> io::Result<()> {
    serde_json::to_writer(
        &mut *stream,
        &json!({ "id": id, "method": method, "params": params }),
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
    serde_json::from_str(&line).map_err(io::Error::other)
}

fn snapshot(socket: &Path) -> io::Result<Snapshot> {
    let mut stream = UnixStream::connect(socket)?;
    send_request(&mut stream, "snapshot", "session.snapshot", json!({}))?;
    let mut reader = BufReader::new(stream);
    let response = read_json(&mut reader)?;
    serde_json::from_value::<Envelope>(response)
        .map(|envelope| envelope.result.snapshot)
        .map_err(io::Error::other)
}

fn reconcile(state: &mut State, snapshot: &Snapshot) {
    let tabs: HashMap<&str, &str> = snapshot
        .tabs
        .iter()
        .map(|tab| (tab.tab_id.as_str(), tab.workspace_id.as_str()))
        .collect();
    let live_workspaces: HashSet<&str> = snapshot
        .workspaces
        .iter()
        .map(|workspace| workspace.workspace_id.as_str())
        .collect();

    state.workspaces.retain(|workspace_id, history| {
        if !live_workspaces.contains(workspace_id.as_str()) {
            return false;
        }
        history.last_tab_id = history
            .last_tab_id
            .take()
            .filter(|tab_id| tabs.get(tab_id.as_str()) == Some(&workspace_id.as_str()));
        true
    });

    for workspace in &snapshot.workspaces {
        let history = state
            .workspaces
            .entry(workspace.workspace_id.clone())
            .or_default();
        history.current_tab_id = workspace.active_tab_id.clone();
        if history.last_tab_id == history.current_tab_id {
            history.last_tab_id = None;
        }
    }
}

fn observe_snapshot(state: &mut State, snapshot: &Snapshot) -> bool {
    let tabs: HashMap<&str, &str> = snapshot
        .tabs
        .iter()
        .map(|tab| (tab.tab_id.as_str(), tab.workspace_id.as_str()))
        .collect();
    let live_workspaces: HashSet<&str> = snapshot
        .workspaces
        .iter()
        .map(|workspace| workspace.workspace_id.as_str())
        .collect();
    let mut changed = false;

    state.workspaces.retain(|workspace_id, history| {
        let keep = live_workspaces.contains(workspace_id.as_str());
        changed |= !keep;
        if !keep {
            return false;
        }
        if history
            .last_tab_id
            .as_ref()
            .is_some_and(|tab_id| tabs.get(tab_id.as_str()) != Some(&workspace_id.as_str()))
        {
            history.last_tab_id = None;
            changed = true;
        }
        keep
    });

    for workspace in &snapshot.workspaces {
        let history = state
            .workspaces
            .entry(workspace.workspace_id.clone())
            .or_default();
        let next_tab_id = workspace.active_tab_id.clone();
        if history.current_tab_id != next_tab_id {
            let old_tab_id = history.current_tab_id.take();
            history.last_tab_id = old_tab_id.filter(|tab_id| {
                tabs.get(tab_id.as_str()) == Some(&workspace.workspace_id.as_str())
            });
            history.current_tab_id = next_tab_id;
            changed = true;
        }
        if history.last_tab_id == history.current_tab_id {
            history.last_tab_id = None;
            changed = true;
        }
    }
    changed
}

fn watch(socket: &Path, state_path: &Path) -> io::Result<()> {
    let mut state = load_state(state_path);
    let mut initialized = false;

    loop {
        match snapshot(socket) {
            Ok(live_snapshot) => {
                if initialized {
                    if observe_snapshot(&mut state, &live_snapshot) {
                        save_state(state_path, &state)?;
                    }
                } else {
                    reconcile(&mut state, &live_snapshot);
                    save_state(state_path, &state)?;
                    initialized = true;
                }
                thread::sleep(Duration::from_millis(250));
            }
            Err(error) => {
                eprintln!("herdr-workspace-last-tab: {error}");
                thread::sleep(Duration::from_secs(1));
            }
        }
    }
}

fn switch(socket: &Path, state_path: &Path) -> io::Result<()> {
    let live_snapshot = snapshot(socket)?;
    let Some(workspace_id) = live_snapshot.focused_workspace_id.as_deref() else {
        return Ok(());
    };
    let state = load_state(state_path);
    let Some(target_tab_id) = state
        .workspaces
        .get(workspace_id)
        .and_then(|history| history.last_tab_id.as_deref())
    else {
        return Ok(());
    };

    let active_tab_id = live_snapshot
        .workspaces
        .iter()
        .find(|workspace| workspace.workspace_id == workspace_id)
        .and_then(|workspace| workspace.active_tab_id.as_deref());
    let valid_target = live_snapshot.tabs.iter().any(|tab| {
        tab.tab_id == target_tab_id
            && tab.workspace_id == workspace_id
            && Some(tab.tab_id.as_str()) != active_tab_id
    });
    if !valid_target {
        return Ok(());
    }

    let mut stream = UnixStream::connect(socket)?;
    send_request(
        &mut stream,
        "focus",
        "tab.focus",
        json!({ "tab_id": target_tab_id }),
    )?;
    let mut reader = BufReader::new(stream);
    let response = read_json(&mut reader)?;
    if let Some(error) = response.get("error") {
        return Err(io::Error::other(error.to_string()));
    }
    Ok(())
}

fn main() {
    let command = env::args().nth(1).unwrap_or_default();
    let socket = socket_path();
    let state = state_path();
    let result = match command.as_str() {
        "watch" => watch(&socket, &state),
        "switch" => switch(&socket, &state),
        _ => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "usage: herdr-workspace-last-tab <watch|switch>",
        )),
    };
    if let Err(error) = result {
        eprintln!("herdr-workspace-last-tab: {error}");
        std::process::exit(1);
    }
}
