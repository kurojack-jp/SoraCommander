"""T4 synthetic cue fixtures and real SDK metadata; no OGF or Owner storage."""
import asyncio
import base64
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._inherited_test_temp import inherited_test_temp
from sora_commander_next import ogf_cue as cue
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server, _validate_arguments

ROOT = Path(__file__).resolve().parents[1]
SESSION = '12345678-1234-1234-1234-123456789abc'
BASE = dict(session_id=SESSION, action='wave', emotion='happy', intensity=0.8, gaze='user')
spec = importlib.util.spec_from_file_location('scn_ogf_cue', ROOT / 'tools' / 'scn_ogf_cue.py')
sender = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender)


class CueTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp()))
        self.folder = self.root / 'storage'
        self.folder.mkdir()
        self.storage = self.enterContext(patch.object(cue, 'protected_storage', return_value=self.folder))
        self.inbox = self.root / 'external-brain' / 'inbox.json'
        self.inbox.parent.mkdir()
        self.bridge = cue.OgfBridge(str(self.inbox), 'profile.synthetic')
        self.host = SimpleNamespace(run_command_wait=Mock(return_value={'status': 'PENDING', 'pending_id': 'original'}),
                                    ogf_bridge=self.bridge)

    def send(self, **changes):
        return self.bridge.send(self.host, **(BASE | changes))

    def config(self, value):
        path = self.root / '.config' / 'sora-commander' / 'ogf-bridge.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if type(value) is bytes else json.dumps(value).encode())
        return path

    def test_config_missing_and_valid(self):
        env = {'USERPROFILE': str(self.root)}
        self.assertIsNone(cue.load_ogf_bridge(env))
        self.assertIsNone(cue.load_ogf_bridge({}))
        self.config({'inbox': str(self.inbox), 'profile_id': 'profile.synthetic'})
        self.assertEqual(cue.load_ogf_bridge(env), self.bridge)
        self.storage.assert_not_called()  # Config loading itself has no state effects.

    def test_config_invalid(self):
        good = {'inbox': str(self.inbox), 'profile_id': 'p'}
        for raw in (b'{', b'\xff', b' ' * 4097, b'{"inbox":1,"inbox":2}', [],
                    good | {'extra': 1}, good | {'profile_id': ''},
                    good | {'profile_id': 'p' * 65}, good | {'profile_id': 'a/b'},
                    good | {'inbox': 'relative'}, good | {'inbox': str(self.root / 'inbox.json')},
                    good | {'inbox': str(self.inbox.with_name('other.json'))}):
            with self.subTest(raw=type(raw).__name__):
                self.config(raw)
                with self.assertRaises(cue.OgfBridgeError):
                    cue.load_ogf_bridge({'USERPROFILE': str(self.root)})

    def test_config_link_and_directory_refused(self):
        path = self.config({'inbox': str(self.inbox), 'profile_id': 'p'})
        real = Path.lstat
        def linked(target, *a, **kw):
            info = real(target, *a, **kw)
            if target == path.parent:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, 'lstat', linked), self.assertRaises(cue.OgfBridgeError):
            cue.load_ogf_bridge({'USERPROFILE': str(self.root)})
        path.unlink()
        path.mkdir()
        with self.assertRaises(cue.OgfBridgeError):
            cue.load_ogf_bridge({'USERPROFILE': str(self.root)})

    def test_all_invalid_arguments_before_storage_or_launch(self):
        cases = {'session_id': ('bad', None, 1), 'action': ('bad', [], None),
                 'emotion': ('bad', [], None), 'gaze': ('bad', [], None),
                 'intensity': (-0.1, 1.1, float('nan'), float('inf'), True, '0.5'),
                 'ttl_ms': (99, 10001, True, 3000.0, '3000'),
                 'wait_seconds': (-1, 91, float('nan'), True, '50'),
                 'message': (1, 'x' * 121, 'a\n', '\x7f', '\x85', '\ud800')}
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=repr(value)):
                    arguments = BASE | {name: value}
                    with self.assertRaises(ValueError):
                        _validate_arguments('ogf_cue', arguments)
                    with self.assertRaises(ValueError):
                        self.bridge.send(self.host, **arguments)
        self.storage.assert_not_called()
        self.host.run_command_wait.assert_not_called()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_allowed_lists_and_boundaries(self):
        for action in cue.ACTIONS:
            cue.validate(**(BASE | {'action': action}))
        for emotion in cue.EMOTIONS:
            cue.validate(**(BASE | {'emotion': emotion}))
        for gaze in cue.GAZES:
            cue.validate(**(BASE | {'gaze': gaze}))
        for intensity, ttl, wait in ((0, 100, 0), (1, 10000, 90)):
            cue.validate(**(BASE | dict(intensity=intensity, ttl_ms=ttl, wait_seconds=wait, message='😀' * 120)))

    def test_sdk_feature_off_and_wire_types(self):
        host = Host()
        self.addCleanup(host.jobs.close)
        server = build_server(host)
        _, result = asyncio.run(server.call_tool('ogf_cue', BASE))
        self.assertEqual(result, {'status': 'DENY', 'error': 'OgfBridgeNotConfigured'})
        from mcp.server.fastmcp.exceptions import ToolError
        for change in ({'intensity': '0.5'}, {'ttl_ms': True}, {'extra': 1}, {'session_id': 1}):
            with self.assertRaises(ToolError):
                asyncio.run(server.call_tool('ogf_cue', BASE | change))
        self.storage.assert_not_called()

    def test_session_argv_fixed_command_and_unchanged_result(self):
        result = self.send(message='こんにちは')
        self.assertIs(result, self.host.run_command_wait.return_value)
        command = self.host.run_command_wait.call_args.kwargs
        argv = command['argv']
        self.assertNotIn(SESSION, json.dumps(command))
        session_path = Path(argv[argv.index('--session-file') + 1])
        self.assertEqual(session_path.read_text(), SESSION)
        self.assertRegex(session_path.name, r'^[0-9a-f]{32}\.session$')
        self.assertEqual(argv[:2], ['-I', str(ROOT / 'tools' / 'scn_ogf_cue.py')])
        self.assertEqual(command['cwd'], str(ROOT))
        self.assertTrue(Path(command['executable']).is_absolute())
        self.assertEqual((command['timeout'], command['output_limit'], command['wait_seconds']), (15, 4096, 50))
        self.assertEqual(base64.b64decode(argv[argv.index('--message-b64') + 1]).decode(), 'こんにちは')

    def test_sequence_persists_and_failed_launch_consumes_number(self):
        self.send()
        self.host.run_command_wait.side_effect = RuntimeError('synthetic launch failure')
        with self.assertRaises(RuntimeError):
            self.send()
        self.host.run_command_wait.side_effect = None
        cue.OgfBridge(self.bridge.inbox, self.bridge.profile_id).send(self.host, **BASE)
        argv = self.host.run_command_wait.call_args.kwargs['argv']
        self.assertEqual(argv[argv.index('--sequence') + 1], '3')
        self.assertNotIn(SESSION.encode(), (self.folder / 'sequence.sqlite3').read_bytes())

    def test_sequence_limit_denies(self):
        self.send()
        # closing(): a bare `with sqlite3.connect()` commits but leaves the file open.
        with closing(sqlite3.connect(self.folder / 'sequence.sqlite3')) as connection:
            connection.execute('UPDATE sequences SET sequence=1000000')
            connection.commit()
        self.host.run_command_wait.reset_mock()
        self.assertEqual(self.send()['error'], 'OgfSequenceExhausted')
        self.host.run_command_wait.assert_not_called()

    def test_stale_cleanup_at_startup_and_call(self):
        for startup in (True, False):
            stale = self.folder / ('a' * 32 + '.session')
            stale.write_text(SESSION)
            os.utime(stale, (1, 1))
            unrelated = self.folder / 'preserve.txt'
            unrelated.write_text('synthetic')
            if startup:
                self.bridge.startup()
            else:
                self.send()
            self.assertFalse(stale.exists())
            self.assertTrue(unrelated.exists())

    def test_storage_failure_never_launches(self):
        self.storage.side_effect = RuntimeError('mock ACL check refuses')
        self.assertEqual(self.send()['error'], 'OgfStorageUnavailable')
        self.host.run_command_wait.assert_not_called()


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp()))
        self.inbox = self.root / 'external-brain' / 'inbox.json'
        self.inbox.parent.mkdir()
        self.session = self.root / 'synthetic.session'

    def run_sender(self, **changes):
        self.session.write_text(SESSION, encoding='ascii')
        values = {'inbox': str(self.inbox), 'profile': 'p', 'sequence': '1', 'ttl-ms': '3000',
                  'action': 'wave', 'emotion': 'happy', 'intensity': '0.8', 'gaze': 'user',
                  'message-b64': '-', 'session-file': str(self.session)} | changes
        argv = [item for key, value in values.items() for item in ('--' + key, value)]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = sender.send(argv)
        self.assertNotIn(SESSION, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_exact_envelope_and_output(self):
        code, out, err = self.run_sender(**{'message-b64': base64.b64encode(('😀' * 120).encode()).decode()})
        self.assertEqual(code, 0)
        self.assertFalse(self.session.exists())
        raw = self.inbox.read_bytes()
        self.assertLessEqual(len(raw), 4096)
        self.assertEqual(json.loads(raw), {'version': 1, 'type': 'companion.intent', 'session': SESSION,
            'profileId': 'p', 'sequence': 1, 'ttlMs': 3000,
            'control': {'emotion': 'happy', 'intensity': 0.8, 'action': 'wave', 'gaze': 'user', 'message': '😀' * 120}})
        self.assertEqual(json.loads(out), {'status': 'SENT', 'sequence': 1, 'action': 'wave',
            'emotion': 'happy', 'gaze': 'user', 'bytes': len(raw)})
        self.assertEqual(err, '')

    def test_session_delete_before_validation(self):
        code, _, _ = self.run_sender(action='invalid')
        self.assertEqual(code, 2)
        self.assertFalse(self.session.exists())
        self.assertFalse(self.inbox.exists())

    def test_read_or_delete_failure_is_3(self):
        for operation in ('open', 'unlink'):
            with patch.object(Path, operation, side_effect=OSError('synthetic refusal')):
                # Write first; patching open also blocks write_text.
                if operation == 'open':
                    self.assertEqual(sender.send(['--session-file', str(self.session)]), 3)
                else:
                    self.assertEqual(self.run_sender()[0], 3)
            self.assertFalse(self.inbox.exists())

    def test_existing_inbox_unchanged(self):
        self.inbox.write_bytes(b'waiting')
        self.assertEqual(self.run_sender()[0], 4)
        self.assertEqual(self.inbox.read_bytes(), b'waiting')
        self.assertFalse(self.session.exists())

    def test_linked_parent_and_ancestor(self):
        real = Path.lstat
        for linked in (self.inbox.parent, self.root):
            def fake(path, *a, **kw):
                info = real(path, *a, **kw)
                return (SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                        if path == linked else info)
            with patch.object(Path, 'lstat', fake):
                self.assertEqual(self.run_sender()[0], 2)
            self.assertFalse(self.inbox.exists())

    def test_wrong_names_and_relative_path(self):
        for inbox in ('relative', str(self.root / 'inbox.json'), str(self.inbox.with_name('wrong.json'))):
            self.assertEqual(self.run_sender(inbox=inbox)[0], 2)

    def test_invalid_script_values_and_no_secret_output(self):
        cases = {'profile': ('', 'a/b', 'a' * 65), 'sequence': ('0', '1000001', '1.5'),
                 'ttl-ms': ('99', '10001', '3000.5'), 'intensity': ('nan', 'inf', '-1', '1.1'),
                 'action': ('invalid',), 'emotion': ('invalid',), 'gaze': ('invalid',),
                 'message-b64': ('!!', base64.b64encode(b'\xff').decode(),
                                 base64.b64encode(b'a\n').decode(), base64.b64encode(b'x' * 121).decode())}
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    self.assertEqual(self.run_sender(**{name: value})[0], 2)
                    self.assertFalse(self.session.exists())
                    self.assertFalse(self.inbox.exists())

    def test_atomic_rename_race_does_not_replace(self):
        real = Path.rename
        def race(path, target):
            self.assertFalse(self.session.exists())
            self.assertEqual(json.loads(path.read_bytes())['session'], SESSION)
            target.write_bytes(b'competing cue')
            return real(path, target)
        with patch.object(Path, 'rename', race):
            self.assertEqual(self.run_sender()[0], 4)
        self.assertEqual(self.inbox.read_bytes(), b'competing cue')
        self.assertEqual(list(self.inbox.parent.iterdir()), [self.inbox])


if __name__ == '__main__':
    unittest.main()
