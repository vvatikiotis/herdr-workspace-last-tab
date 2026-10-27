"""Opt-in real Herdr test. Uses private config/state and a disposable server."""
import json
import fcntl
import os
from pathlib import Path
import socket
import shlex
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = 'user.herdr-workspace-last-tab'


def main():
    scratch = ROOT / 'tmp'
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='h-', dir=scratch) as temporary:
        root = Path(temporary)
        env = {k: v for k, v in os.environ.items() if not k.startswith('HERDR_')}
        env.update(XDG_CONFIG_HOME=str(root), XDG_STATE_HOME=str(root / 'state'), TMPDIR=str(scratch))
        config = root / 'herdr'
        config.mkdir(parents=True)
        shell_command = [str(ROOT / 'target/release/herdr-workspace-last-tab'), 'switch']
        config.joinpath('config.toml').write_text(f'''onboarding = false
[terminal]
default_shell = "/bin/sh"
[[keys.command]]
key = "prefix+a"
type = "shell"
command = {json.dumps(shlex.join(shell_command))}
''')
        env['HERDR_CONFIG_PATH'] = str(config / 'config.toml')
        socket_path = config / 'herdr.sock'

        def cli(*args):
            result = subprocess.run(['herdr', *args], env=env,
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr + result.stdout
            return result.stdout

        def request(method, **params):
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(5)
                connection.connect(str(socket_path))
                connection.sendall((json.dumps({'id': 'test', 'method': method, 'params': params}) + '\n').encode())
                result = json.loads(connection.makefile('rb').readline())
                assert 'error' not in result, result
                return result['result']

        def eventually(predicate, description):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    if predicate():
                        return
                except OSError:
                    pass  # A restarting test server may not have bound its socket yet.
                time.sleep(.03)
            raise AssertionError(description)

        cli('plugin', 'link', str(ROOT))
        with open(root / 'server.log', 'w+') as log:
            server = subprocess.Popen(['herdr', 'server'],
                                      env=env, stdout=log, stderr=log)
            try:
                eventually(lambda: socket_path.exists() or server.poll() is not None, 'server did not start')
                assert server.poll() is None, (log.seek(0), log.read())
                eventually(lambda: request('session.snapshot').get('snapshot') is not None, 'server not ready')
                # Readiness only: behavior is asserted through Herdr actions below.
                eventually(lambda: bool(list((root / 'state').glob('herdr/plugins/*/sessions/*/state.json'))),
                           'startup listener did not initialize')
                workspace = request('workspace.create', cwd=str(root), label='last-tab-test')
                workspace_id = workspace['workspace']['workspace_id']
                a = workspace['tab']['tab_id']
                b = request('tab.create', workspace_id=workspace_id, label='B', focus=True)['tab']['tab_id']
                c = request('tab.create', workspace_id=workspace_id, label='C', focus=True)['tab']['tab_id']
                def active():
                    snapshot = request('session.snapshot')['snapshot']
                    return next(w['active_tab_id'] for w in snapshot['workspaces'] if w['workspace_id'] == workspace_id)
                def switch():
                    # Shell keybindings have session context, not plugin paths.
                    shell_env = dict(env, HERDR_ENV='1', HERDR_SOCKET_PATH=str(socket_path))
                    result = subprocess.run(shell_command, env=shell_env, capture_output=True, text=True, timeout=7)
                    assert result.returncode == 0, result.stderr
                # No delivery sleeps: the action/listener barrier must catch up.
                switch()
                eventually(lambda: active() == b, 'C did not switch to B')
                switch()
                eventually(lambda: active() == c, 'B did not toggle back to C')
                # Several focus events can be drained in one subscription batch.
                request('tab.focus', tab_id=a)
                request('tab.focus', tab_id=b)
                request('tab.focus', tab_id=c)
                switch()
                eventually(lambda: active() == b, 'rapid A -> B -> C lost B')
                for expected in [c, b] * 5:
                    switch()
                    eventually(lambda: active() == expected, 'repeated toggle lost event ordering')
                state_file = next((root / 'state').glob('herdr/plugins/*/sessions/*/state.json'))
                request('server.stop')
                server.wait(timeout=10)
                # Startup commands can outlive their server briefly. Wait for
                # the old singleton lease before launching its replacement.
                def old_listener_exited():
                    with open(state_file.parent / 'listener.lock', 'rb') as lease:
                        try:
                            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            return False
                        fcntl.flock(lease, fcntl.LOCK_UN)
                        return True
                eventually(old_listener_exited, 'old listener did not exit')
                before_restart = state_file.stat().st_mtime_ns
                server = subprocess.Popen(['herdr', 'server'],
                                          env=env, stdout=log, stderr=log)
                eventually(lambda: state_file.stat().st_mtime_ns != before_restart,
                           'restart listener did not reconcile persisted history')
                switch()
                eventually(lambda: active() == c, 'history did not survive server restart')
                registered = request('plugin.action.list', plugin_id=PLUGIN)
                assert PLUGIN in json.dumps(registered), registered
                request('plugin.action.invoke', action_id=PLUGIN + '.switch')
                eventually(lambda: active() == b, 'optional plugin action stopped working')
                extra = [request('tab.create', workspace_id=workspace_id, label=label, focus=True)['tab']['tab_id']
                         for label in ['D', 'E', 'F', 'G']]
                g = extra[-1]
                # Recency is now G,F,E,D,B,C; A is still open but evicted.
                for tab in [b, *extra[:-1]]:
                    request('tab.close', tab_id=tab)
                switch()
                eventually(lambda: active() == c, 'sixth retained tab was forgotten')
                switch()
                eventually(lambda: active() == g, 'newest-two toggle changed')
                request('tab.close', tab_id=c)
                switch()
                assert active() == g, f'closing newer tabs resurrected an evicted tab: expected {g}, got {active()}'
                request('tab.focus', tab_id=a)
                switch()
                eventually(lambda: active() == g, 'revisited evicted tab did not re-enter history')
                print('PASS: startup, shell/action invocation, ordering, restart, six-tab retention, closure fallback, and eviction')
            except Exception:
                for saved in (root / 'state').glob('herdr/plugins/*/sessions/*/state.json'):
                    print('test history:', saved.read_text())
                try:
                    print('plugin logs:', json.dumps(request('plugin.log.list', plugin_id=PLUGIN, limit=20), indent=2))
                except OSError:
                    print('test server unavailable; plugin logs could not be retrieved')
                log.flush()
                log.seek(0)
                print('server log tail:', log.read()[-6000:])
                raise
            finally:
                if server.poll() is None:
                    try:
                        request('server.stop')
                    except OSError:
                        server.terminate()  # This process owns only the disposable server.
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == '__main__':
    main()
