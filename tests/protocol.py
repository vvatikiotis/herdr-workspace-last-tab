"""Exercise the installed command interface against an isolated Herdr socket."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest

BINARY = os.environ.get('LAST_TAB_BINARY', str(Path(__file__).resolve().parents[1] / 'target/debug/herdr-workspace-last-tab'))


class Herdr:
    def __init__(self, root, name='server'):
        self.path = str(Path(root) / name)
        self.active = {'w': 'a', 'other': 'x'}
        self.tabs = {'w': ['a', 'b', 'c'], 'other': ['x', 'y']}
        self.focused = 'w'
        self.requests = []
        self.subscriber = None
        self.snapshots = 0
        self.condition = threading.Condition()
        self.closed = False
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(self.path)
        self.socket.listen()
        self.socket.settimeout(.1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.closed:
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()

    def handle(self, connection):
        with connection:
            request = json.loads(connection.makefile('rb').readline())
            method = request['method']
            if method == 'events.subscribe':
                self.subscriber = connection
                connection.sendall((json.dumps({'id': request['id'], 'result': {'type': 'subscription_started'}}) + '\n').encode())
                while not self.closed and self.subscriber is connection:
                    time.sleep(.01)
                return
            if method == 'session.snapshot':
                result = {'snapshot': {'focused_workspace_id': self.focused,
                    'workspaces': [{'workspace_id': w, 'active_tab_id': t} for w, t in self.active.items()],
                    'tabs': [{'workspace_id': w, 'tab_id': t} for w, ts in self.tabs.items() for t in ts]}}
            elif method == 'tab.focus':
                target = request['params']['tab_id']
                self.requests.append(target)
                self.active[self.focused] = target
                result = {}
            else:
                raise AssertionError(method)
            connection.sendall((json.dumps({'id': request['id'], 'result': result}) + '\n').encode())
            if method == 'session.snapshot':
                with self.condition:
                    self.snapshots += 1
                    self.condition.notify_all()

    def wait_snapshot(self, previous):
        with self.condition:
            if not self.condition.wait_for(lambda: self.snapshots > previous, timeout=3):
                raise AssertionError('listener did not request a snapshot')

    def event(self, event, **data):
        previous = self.snapshots
        self.subscriber.sendall((json.dumps({'event': event.replace('.', '_'), 'data': data}) + '\n').encode())
        self.wait_snapshot(previous)

    def focus(self, workspace, tab):
        self.active[workspace] = tab
        self.event('tab.focused', type='tab_focused', workspace_id=workspace, tab_id=tab)

    def close(self):
        self.closed = True
        if self.subscriber:
            try:
                self.subscriber.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.socket.close()
        self.thread.join(timeout=2)


class Protocol(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix='last-tab-', dir='/tmp')
        self.server = Herdr(self.root.name)
        self.env = dict(os.environ, HERDR_SOCKET_PATH=self.server.path,
                        HERDR_PLUGIN_STATE_DIR=self.root.name + '/state')
        self.listeners = []

    def tearDown(self):
        self.server.close()
        for listener in self.listeners:
            if listener.poll() is None:
                listener.terminate()
            listener.communicate(timeout=3)
        self.root.cleanup()

    def listen(self, server=None):
        server = server or self.server
        previous = server.snapshots
        process = subprocess.Popen([BINARY, 'listen'], env=dict(self.env, HERDR_SOCKET_PATH=server.path),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.listeners.append(process)
        deadline = time.monotonic() + 3
        while server.subscriber is None:
            if process.poll() is not None:
                self.fail(process.communicate()[1].decode())
            if time.monotonic() > deadline:
                self.fail('listener did not subscribe')
            time.sleep(.01)
        server.wait_snapshot(previous)
        return process

    def switch(self, server=None):
        server = server or self.server
        result = subprocess.run([BINARY, 'switch'], env=dict(self.env, HERDR_SOCKET_PATH=server.path),
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tracks_previous_tab_and_toggles(self):
        self.listen()
        self.switch()
        self.assertEqual(self.server.requests, [])
        self.server.focus('w', 'b')
        self.switch()
        self.assertEqual(self.server.requests, ['a'])
        self.server.event('tab.focused', type='tab_focused', workspace_id='w', tab_id='a')
        self.switch()
        self.assertEqual(self.server.requests, ['a', 'b'])

    def test_workspace_and_session_histories_are_independent(self):
        self.listen()
        self.server.focus('w', 'b')
        self.server.focus('other', 'y')
        self.switch()
        self.assertEqual(self.server.requests, ['a'])
        self.server.focused = 'other'
        self.switch()
        self.assertEqual(self.server.requests, ['a', 'x'])
        other = Herdr(self.root.name, 'second-session')
        try:
            self.listen(other)
            self.switch(other)
            self.assertEqual(other.requests, [])
        finally:
            other.close()

    def test_restart_retains_history_and_reconciles_changed_active_tab(self):
        listener = self.listen()
        self.server.focus('w', 'b')
        self.server.subscriber.shutdown(socket.SHUT_RDWR)
        self.assertEqual(listener.wait(timeout=3), 0)
        self.server.subscriber = None
        self.server.active['w'] = 'c'
        self.listen()
        self.switch()
        self.assertEqual(self.server.requests, ['b'])

    def test_closing_last_tab_does_not_fall_back(self):
        self.listen()
        self.server.focus('w', 'b')
        self.server.focus('w', 'c')
        self.server.tabs['w'].remove('b')
        self.server.event('tab.closed', type='tab_closed', workspace_id='w', tab_id='b')
        self.switch()
        self.assertEqual(self.server.requests, [])

    def test_closing_current_tab_does_not_restore_older_history(self):
        self.listen()
        self.server.focus('w', 'b')
        self.server.focus('w', 'c')
        self.server.tabs['w'].remove('c')
        self.server.active['w'] = 'b'
        self.server.event('tab.closed', type='tab_closed', workspace_id='w', tab_id='c')
        self.server.focus('w', 'b')
        self.switch()
        self.assertEqual(self.server.requests, [])

    def test_closed_workspace_history_is_deleted(self):
        self.listen()
        self.server.focus('w', 'b')
        del self.server.active['w']
        del self.server.tabs['w']
        self.server.focused = 'other'
        self.server.event('workspace.closed', type='workspace_closed', workspace_id='w')
        self.server.active['w'] = 'b'
        self.server.tabs['w'] = ['a', 'b']
        self.server.focused = 'w'
        self.server.focus('w', 'b')
        self.switch()
        self.assertEqual(self.server.requests, [])

    def test_focus_payload_preserves_intermediate_tabs_when_snapshot_is_ahead(self):
        self.listen()
        self.server.active['w'] = 'c'
        self.server.event('tab.focused', type='tab_focused', workspace_id='w', tab_id='b')
        self.server.event('tab.focused', type='tab_focused', workspace_id='w', tab_id='c')
        self.switch()
        self.assertEqual(self.server.requests, ['b'])

    def test_duplicate_focus_events_leave_last_tab_unchanged(self):
        self.listen()
        self.server.focus('w', 'b')
        self.server.focus('w', 'b')
        self.switch()
        self.assertEqual(self.server.requests, ['a'])

    def test_rapid_focus_events_are_processed_in_order(self):
        self.listen()
        previous = self.server.snapshots
        self.server.active['w'] = 'c'
        for tab in ['a', 'b', 'c'] * 20:
            self.server.subscriber.sendall((json.dumps({'event': 'tab_focused', 'data': {
                'type': 'tab_focused', 'workspace_id': 'w', 'tab_id': tab}}) + '\n').encode())
        with self.server.condition:
            self.assertTrue(self.server.condition.wait_for(lambda: self.server.snapshots >= previous + 60, timeout=5))
        self.switch()
        self.assertEqual(self.server.requests, ['b'])

    def test_second_listener_exits_without_replacing_the_first(self):
        self.listen()
        result = subprocess.run([BINARY, 'listen'], env=self.env, capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.server.focus('w', 'b')
        self.switch()
        self.assertEqual(self.server.requests, ['a'])

    def test_listener_exits_on_server_shutdown(self):
        listener = self.listen()
        self.server.close()
        self.assertEqual(listener.wait(timeout=3), 0)


if __name__ == '__main__':
    unittest.main()
