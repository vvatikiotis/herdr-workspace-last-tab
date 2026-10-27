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
        self.control_subscribers = []
        self.deferred = []
        self.defer_focus = False
        self.fail_focus = False
        self.hold_done = False
        self.pending_done = []
        self.done_held = threading.Event()
        self.second_request = threading.Event()
        self.request_count = 0
        self.send_lock = threading.Lock()
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
                is_listener = any(s['type'] == 'tab.focused' for s in request['params']['subscriptions'])
                if is_listener:
                    self.subscriber = connection
                else:
                    self.control_subscribers.append(connection)
                connection.sendall((json.dumps({'id': request['id'], 'result': {'type': 'subscription_started'}}) + '\n').encode())
                if is_listener:
                    while not self.closed and self.subscriber is connection:
                        time.sleep(.01)
                else:
                    connection.recv(1)
                    self.control_subscribers.remove(connection)
                return
            if method == 'session.snapshot':
                result = {'snapshot': {'focused_workspace_id': self.focused,
                    'workspaces': [{'workspace_id': w, 'active_tab_id': t} for w, t in self.active.items()],
                    'tabs': [{'workspace_id': w, 'tab_id': t} for w, ts in self.tabs.items() for t in ts]}}
            elif method == 'tab.focus':
                if self.fail_focus:
                    connection.sendall((json.dumps({'id': request['id'], 'error': {
                        'code': 'tab_not_found', 'message': 'tab closed during focus'}}) + '\n').encode())
                    return
                target = request['params']['tab_id']
                self.requests.append(target)
                workspace = next(w for w, tabs in self.tabs.items() if target in tabs)
                self.active[workspace] = target
                self.focused = workspace
                focus_event = {'event': 'tab_focused', 'data': {'workspace_id': workspace, 'tab_id': target}}
                if self.defer_focus:
                    self.deferred.append(focus_event)
                else:
                    self.emit(focus_event)
                result = {}
            elif method == 'workspace.report_metadata':
                if request['params']['workspace_id'] not in self.active:
                    connection.sendall((json.dumps({'id': request['id'], 'error': {
                        'code': 'workspace_not_found', 'message': 'workspace closed'}}) + '\n').encode())
                    return
                token = request['params']['tokens'].get('workspace_last_tab_control', '')
                if token.startswith('r:'):
                    self.request_count += 1
                    if self.request_count == 2:
                        self.second_request.set()
                # Herdr drains focus batches before metadata batches. A marker
                # generated in response cannot occur in that same metadata batch.
                for event in self.deferred:
                    self.emit(event)
                self.deferred.clear()
                self.emit({'event': 'workspace_metadata_updated', 'data': {'workspace': {
                    'workspace_id': request['params']['workspace_id'], 'tokens': request['params']['tokens']}}})
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

    def emit(self, event):
        recipients = [self.subscriber]
        if event['event'] == 'workspace_metadata_updated':
            recipients += list(self.control_subscribers)
        with self.send_lock:
            for connection in recipients:
                if connection:
                    token = event.get('data', {}).get('workspace', {}).get('tokens', {}).get('workspace_last_tab_control', '')
                    if self.hold_done and connection is not self.subscriber and token.startswith('d:'):
                        self.pending_done.append((connection, event))
                        self.done_held.set()
                        continue
                    try:
                        connection.sendall((json.dumps(event) + '\n').encode())
                    except OSError:
                        pass

    def event(self, event, **data):
        previous = self.snapshots
        self.emit({'event': event.replace('.', '_'), 'data': data})
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

    def test_restart_preserves_last_tab_when_active_tab_is_unchanged(self):
        listener = self.listen()
        self.server.focus('w', 'b')
        self.server.subscriber.shutdown(socket.SHUT_RDWR)
        self.assertEqual(listener.wait(timeout=3), 0)
        self.server.subscriber = None
        self.listen()
        self.switch()
        self.assertEqual(self.server.requests, ['a'])

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

    def test_queued_request_for_closed_workspace_does_not_stop_other_history(self):
        self.listen()
        del self.server.active['w']
        del self.server.tabs['w']
        self.server.focused = 'other'
        self.server.event('workspace.metadata_updated', workspace={
            'workspace_id': 'w', 'tokens': {'workspace_last_tab_control': f'r:{time.time_ns()}:{os.getpid()}'}})
        self.server.focus('other', 'y')
        self.switch()
        self.assertEqual(self.server.requests, ['x'])

    def test_concurrent_action_waits_until_previous_completion_is_delivered(self):
        self.listen()
        self.server.focus('w', 'b')
        self.server.hold_done = True
        first = subprocess.Popen([BINARY, 'switch'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = None
        try:
            self.assertTrue(self.server.done_held.wait(2))
            second = subprocess.Popen([BINARY, 'switch'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertFalse(self.server.second_request.wait(.5), 'second action overtook unacknowledged first action')
        finally:
            with self.server.send_lock:
                self.server.hold_done = False
                for connection, event in self.server.pending_done:
                    try:
                        connection.sendall((json.dumps(event) + '\n').encode())
                    except OSError:
                        pass
            for process in [first, second]:
                if process:
                    _, error = process.communicate(timeout=7)
                    self.assertEqual(process.returncode, 0, error)
        self.assertEqual(self.server.requests, ['a', 'b'])

    def test_concurrent_actions_each_toggle_once(self):
        self.listen()
        self.server.focus('w', 'b')
        processes = [subprocess.Popen([BINARY, 'switch'], env=self.env,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(16)]
        try:
            for process in processes:
                _, error = process.communicate(timeout=7)
                self.assertEqual(process.returncode, 0, error)
            self.assertEqual(self.server.requests, ['a', 'b'] * 8)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_failed_focus_does_not_stop_listener(self):
        self.listen()
        self.server.focus('w', 'b')
        self.server.fail_focus = True
        result = subprocess.run([BINARY, 'switch'], env=self.env, capture_output=True, text=True, timeout=7)
        self.assertNotEqual(result.returncode, 0)
        self.server.fail_focus = False
        self.switch()
        self.assertEqual(self.server.requests, ['a'])

    def test_action_waits_for_preceding_queued_focus_events(self):
        self.listen()
        self.server.active['w'] = 'c'
        self.server.deferred = [
            {'event': 'tab_focused', 'data': {'workspace_id': 'w', 'tab_id': tab}}
            for tab in ['b', 'c']]
        self.switch()
        self.assertEqual(self.server.requests, ['b'])

    def test_queued_focus_of_closed_current_tab_clears_older_history(self):
        self.listen()
        self.server.active['w'] = 'b'
        self.server.tabs['w'].remove('c')
        for tab in ['b', 'c', 'b']:
            self.server.event('tab.focused', type='tab_focused', workspace_id='w', tab_id=tab)
        self.switch()
        self.assertEqual(self.server.requests, [])

    def test_delayed_action_focus_event_does_not_break_next_toggle(self):
        self.listen()
        self.server.focus('w', 'b')
        self.server.defer_focus = True
        self.switch()
        self.switch()
        self.assertEqual(self.server.requests, ['a', 'b'])
        self.switch()
        self.assertEqual(self.server.requests, ['a', 'b', 'a'])

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
