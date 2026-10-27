"""Opt-in real Herdr test. Uses private config/state and a disposable server."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = 'user.herdr-workspace-last-tab'


def main():
    with tempfile.TemporaryDirectory(prefix='last-tab-live-', dir='/tmp') as temporary:
        root = Path(temporary)
        env = {k: v for k, v in os.environ.items() if not k.startswith('HERDR_')}
        env.update(XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'))
        config = root / 'config/herdr'
        config.mkdir(parents=True)
        config.joinpath('config.toml').write_text('''onboarding = false
[terminal]
default_shell = "/bin/sh"
[[keys.command]]
key = "prefix+a"
type = "plugin_action"
command = "user.herdr-workspace-last-tab.switch"
''')
        env['HERDR_CONFIG_PATH'] = str(config / 'config.toml')
        socket_path = config / 'sessions/last-tab-test/herdr.sock'

        def cli(*args):
            result = subprocess.run(['herdr', '--session', 'last-tab-test', *args], env=env,
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
                if predicate():
                    return
                time.sleep(.03)
            raise AssertionError(description)

        cli('plugin', 'link', str(ROOT))
        with open(root / 'server.log', 'w+') as log:
            server = subprocess.Popen(['herdr', '--session', 'last-tab-test', 'server'],
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
                    request('plugin.action.invoke', action_id=PLUGIN + '.switch')
                # Allow async server delivery, then assert through the action.
                time.sleep(.3)
                switch()
                eventually(lambda: active() == b, 'C did not switch to B')
                time.sleep(.15)
                switch()
                eventually(lambda: active() == c, 'B did not toggle back to C')
                # Several focus events can be drained in one subscription batch.
                request('tab.focus', tab_id=a)
                request('tab.focus', tab_id=b)
                request('tab.focus', tab_id=c)
                time.sleep(.3)
                switch()
                eventually(lambda: active() == b, 'rapid A -> B -> C lost B')
                registered = request('plugin.action.list', plugin_id=PLUGIN)
                assert PLUGIN in json.dumps(registered), registered
                print('PASS: real startup hook, rapid focus events, action dispatch, and toggle')
            except Exception:
                print('plugin logs:', json.dumps(request('plugin.log.list', plugin_id=PLUGIN, limit=20), indent=2))
                log.flush()
                log.seek(0)
                print('server log tail:', log.read()[-6000:])
                raise
            finally:
                if socket_path.exists() and server.poll() is None:
                    request('server.stop')
                server.wait(timeout=10)


if __name__ == '__main__':
    main()
