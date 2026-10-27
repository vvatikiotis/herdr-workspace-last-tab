#[test]
fn plugin_commands_follow_workspace_history_spec() {
    let result = std::process::Command::new("python3")
        .arg("tests/protocol.py")
        .arg("-v")
        .env(
            "LAST_TAB_BINARY",
            env!("CARGO_BIN_EXE_herdr-workspace-last-tab"),
        )
        .output()
        .expect("Python 3 is required for the socket integration tests");
    assert!(
        result.status.success(),
        "{}{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
}
