from contextlib import contextmanager
import ctypes as ct
import json
import os
from pathlib import Path
import secrets
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests._inherited_test_temp import inherited_test_temp
from tests.test_vm_screen_agent_20261125 import (
    ALL_TOOLS, arguments, backend, raw_exchange, split_response, wire_request,
)
from tests.test_vm_transfer_20261125 import allow_file_loader
from tests.test_vm_screen_protocol_20261125 import (
    Clock, KEY, NativeIsolatedTest, paired, private_pair,
)
from sora_commander_next import vm_agent as a, vm_client as c, vm_files as f, vm_protocol as p, vm_uia as u, vm_win as w


REAL_LOADER = getattr(ct, 'WinDLL', None)


@contextmanager
def temp_root():
    """The fixture's own ACL check may load advapi32. Nothing else in the test may load a library."""
    fixture = inherited_test_temp()
    with patch.object(ct, 'WinDLL', REAL_LOADER, create=True):
        root = fixture.__enter__()
    try:
        yield root
    finally:
        with patch.object(ct, 'WinDLL', REAL_LOADER, create=True):
            fixture.__exit__(None, None, None)


ITEM = dict(type='button', name='OK', x=1, y=2, width=3, height=4, enabled=True, focused=False, password=False)
def json_result(response):
    status, _, raw = split_response(response)
    return status, p.decode(raw, max_depth=3)


class Store:
    """An in-memory stand-in, so the HTTP tests need no disk."""

    def __init__(self):
        self.inbox, self.outbox, self.fail = {}, {}, None

    def _check(self):
        if self.fail is not None:
            raise self.fail

    def listing(self):
        self._check()
        return dict(inbox=[dict(name=n, size=len(d)) for n, d in self.inbox.items()],
                    outbox=[dict(name=n, size=len(d)) for n, d in self.outbox.items()], truncated=False)


class FileStoreTests(NativeIsolatedTest):
    def setUp(self):
        super().setUp()
        self.enterContext(allow_file_loader())

    def _store(self, root):
        for name in ('inbox', 'outbox'):
            (Path(root) / name).mkdir()
        return f.FileStore(str(root))

    def test_reparse_points_are_never_plain(self):
        for directory in (True, False):
            mode = 0o40755 if directory else 0o100644
            info = SimpleNamespace(st_mode=mode, st_nlink=1, st_file_attributes=0)
            self.assertTrue(f._plain(info, directory))
            info.st_file_attributes = f.REPARSE
            self.assertFalse(f._plain(info, directory))

    def test_listing_is_bounded_and_hides_other_names(self):
        with temp_root() as root:
            store = self._store(root)
            for name in ('b', 'a', '.part-x', '.bad name'):
                (Path(root) / 'outbox' / name).write_bytes(b'12')
            (Path(root) / 'outbox' / 'dir').mkdir()
            listing = store.listing()
            self.assertEqual(listing, dict(inbox=[], outbox=[dict(name='a', size=2), dict(name='b', size=2)],
                                           truncated=False))
            with patch.object(f, 'LIST_MAX', 1):
                self.assertTrue(store.listing()['truncated'])
            with patch.object(f, 'SCAN_MAX', 1):
                listing = store.listing()
            self.assertTrue(listing['truncated'])
            self.assertLessEqual(len(listing['outbox']), 1)

    def test_root_must_be_absolute_with_both_folders(self):
        with temp_root() as root:
            for value in ('relative', None, str(root)):
                with self.assertRaises(f.StoreError):
                    f.FileStore(value)
            (Path(root) / 'inbox').mkdir()
            (Path(root) / 'outbox').write_bytes(b'')
            with self.assertRaises(f.StoreError):
                f.FileStore(str(root))
            os.remove(Path(root) / 'outbox')
            (Path(root) / 'outbox').mkdir()
            f.FileStore(str(root))
            # A relative path to the same valid folder is still refused.
            with self.assertRaises(f.StoreError):
                f.FileStore(os.path.relpath(root))


class AgentFeatureTests(NativeIsolatedTest):
    def _agent(self, tools=ALL_TOOLS, **changes):
        device = backend()
        device.elements = Mock(return_value=dict(window='Fixture', elements=[ITEM], truncated=False))
        return a.Agent(arguments(tools=tools), KEY, device, (640, 480), files=Store(), clock=Clock(), **changes)

    def _get(self, agent, path, output=lambda _: None):
        return raw_exchange(agent, wire_request(agent, 'GET', path), output)

    def _expect(self, response, status, result):
        self.assertEqual((response[0], response[1]['result']), (status, result))

    def test_files_need_a_valid_folder_at_start(self):
        for factory, tools, expected in ((Mock(side_effect=f.StoreError('FILE_FAILED')), 'screenshot,file_get', 2),
                                         (Mock(side_effect=f.StoreError('FILE_FAILED')), 'screenshot,wait', 0)):
            listener, output = Mock(), []
            listener.return_value.__enter__ = Mock(return_value=Mock(accept=Mock(side_effect=KeyboardInterrupt)))
            listener.return_value.__exit__ = Mock(return_value=False)
            values = {(a.BIOS, 'SystemManufacturer'): 'Microsoft Corporation',
                      (a.BIOS, 'SystemProductName'): 'Virtual Machine',
                      (a.GUEST, 'VirtualMachineName'): 'Fixture VM'}
            args = arguments(tools=tools)
            args.files_dir = 'relative'
            code = a.run(args, platform='win32', backend_factory=backend,
                         registry_reader=lambda key, name: values[key, name],
                         key_reader=lambda *_: KEY.hex().encode(),
                         interface_reader=lambda: [((10 << 24) + 2, 0xffffff00)],
                         files_factory=factory, listener_factory=listener, clock=Clock(), output=output.append)
            if expected:
                self.assertEqual(code, 2)
                self.assertIn('INVALID_FILES_DIR', output[0])
                listener.assert_not_called()
            else:
                self.assertEqual(code, 130)
                factory.assert_not_called()

    def test_a_drag_never_starts_without_time_to_finish(self):
        action = dict(type='drag', path=[dict(x=1, y=1), dict(x=2, y=2)], duration_ms=2000)
        paced = w.prepare_input(action, (640, 480))
        self.assertGreater(paced.seconds(), 2)
        for left, status, result in ((2.5, 429, 'LIMIT_REACHED'), (30, 200, 'SENT')):
            agent = self._agent()
            agent.backend.prepare_input.return_value = paced
            shot = split_response(self._get(agent, '/screenshot'))[1]['etag']
            agent.deadline = agent.clock() + left
            raw = p.encode(action)
            response = raw_exchange(agent, wire_request(agent, 'POST', '/action', changes={
                'Content-Type': 'application/json', 'Content-Length': str(len(raw)), 'SCN-Boot-Id': agent.boot_id,
                'Idempotency-Key': secrets.token_hex(16), 'If-Match': shot}, body=raw))
            self._expect(json_result(response), status, result)
            self.assertEqual(agent.backend.insert.call_count, int(status == 200))
        agent = self._agent()
        agent.backend.prepare_input.return_value = paced
        agent.screen = ('"x"', (640, 480), 0)
        stream = SimpleNamespace(deadline=agent.clock() + 2.5, check_time=Mock())
        with self.assertRaises(p.TransportError):
            agent._input('0' * 32, action, '"x"', stream)
        agent.backend.insert.assert_not_called()
        self.assertIsNotNone(agent.screen)

    def test_lost_desktop_voids_the_screenshot_everywhere(self):
        for path in ('/elements', '/screenshot'):
            for states in ([False], [True, False]):
                agent = self._agent()
                agent.screen = ('"x"', (640, 480), 0)
                agent.backend.desktop_is_default = Mock(side_effect=states)
                self._expect(json_result(self._get(agent, path)), 503, 'NOT_DEFAULT_DESKTOP')
                self.assertIsNone(agent.screen)

    def test_elements_are_returned_and_fail_closed(self):
        agent, seen = self._agent(), []
        status, body = json_result(self._get(agent, '/elements', seen.append))
        self.assertEqual((status, body['result'], body['window'], body['elements']), (200, 'OK', 'Fixture', [ITEM]))
        agent.backend.elements.assert_called_once_with((640, 480))
        self.assertIn(' elements OK ', '\n'.join(seen))
        self.assertIsNone(agent.screen)
        for states, calls in (([False], 0), ([True, False], 1)):
            agent = self._agent()
            agent.backend.desktop_is_default = Mock(side_effect=states)
            self._expect(json_result(self._get(agent, '/elements')), 503, 'NOT_DEFAULT_DESKTOP')
            self.assertEqual(agent.backend.elements.call_count, calls)
        agent = self._agent()
        agent.backend.elements.side_effect = RuntimeError('secret')
        response = self._get(agent, '/elements')
        self._expect(json_result(response), 503, 'ELEMENTS_FAILED')
        self.assertNotIn(b'secret', response)


class ElementTests(NativeIsolatedTest):
    def test_select_keeps_visible_bounded_rows(self):
        def row(kind=50000, name='n', rect=(0, 0, 10, 10), enabled=1, focused=0, password=0, offscreen=0):
            return kind, name, rect, enabled, focused, password, offscreen

        items, truncated = u.select([
            row(), row(offscreen=1), row(rect=(700, 0, 800, 10)), row(rect=(5, 5, 5, 9)),
            row(rect=(-5, -5, 6, 7)), row(kind=50033, name=''), row(kind=50004, name='', password=1, focused=1),
            row(kind=1, name='odd\x1b[31m\n' + 'x' * 100, enabled=0)], (640, 480))
        self.assertFalse(truncated)
        self.assertEqual([item['type'] for item in items], ['button', 'button', 'edit', 'other'])
        self.assertEqual([items[1][key] for key in ('x', 'y', 'width', 'height')], [0, 0, 6, 7])
        self.assertEqual((items[2]['password'], items[2]['focused']), (True, True))
        self.assertTrue(items[3]['name'].isprintable())
        self.assertLessEqual(len(items[3]['name']), u.NAME_MAX)
        items, truncated = u.select([row()] * (u.ELEMENTS_MAX + 1), (640, 480))
        self.assertEqual((len(items), truncated), (u.ELEMENTS_MAX, True))
        p.validate_response(dict(result='OK', window='w', elements=items, truncated=True,
                                 remaining=dict(seconds=1, input_ops=1, non_input_ops=1)), 200, 'elements')

    class Com:
        def __init__(self, length=2, fail=None):
            self.length, self.fail, self.live, self.log = length, fail, [], []
            self.closed = 0
            self.user = SimpleNamespace(GetForegroundWindow=lambda: 77)

        def automation(self):
            return self._new('automation')

        def _new(self, name):
            if self.fail == name:
                raise u.UIAError('UIA_CALL')
            self.live.append(name)
            return name

        def interface(self, pointer, index, types=(), *args):
            names = {u.AUTOMATION_CONTROL_VIEW: 'condition', u.AUTOMATION_CREATE_CACHE: 'cache',
                     u.AUTOMATION_ELEMENT_FROM_HANDLE: 'root', u.ELEMENT_FIND_ALL_CACHED: 'found',
                     u.ARRAY_GET: 'element'}
            self.log.append((pointer, index, args))
            return self._new(names[index])

        def call(self, pointer, index, types, *args, result=None):
            self.log.append((pointer, index, args))

        def out(self, pointer, index, kind, types=(), *args):
            if index == u.ARRAY_LENGTH:
                return ct.c_int32(self.length)
            if self.fail == 'row':
                raise u.UIAError('UIA_CALL')
            return u.Rect(1, 2, 11, 22) if kind is u.Rect else ct.c_int32(50000 if index == u.CACHED_CONTROL_TYPE
                                                                          else int(index == u.CACHED_ENABLED))

        def text(self, pointer, index):
            return 'Title'

        def close(self):
            assert not self.live  # Objects are released before COM is closed.
            self.closed += 1

        def release(self, *pointers):
            for pointer in pointers:
                if pointer:
                    self.live.remove(pointer)

    def test_rows_are_cached_bounded_and_everything_is_released(self):
        com = self.Com()
        found = u.elements((640, 480), com_factory=lambda: com)
        self.assertEqual((found['window'], len(found['elements']), found['truncated']), ('Title', 2, False))
        self.assertEqual(found['elements'][0], dict(type='button', name='Title', x=1, y=2, width=10, height=20,
                                                    enabled=True, focused=False, password=False))
        self.assertEqual((com.live, com.closed), ([], 1))
        self.assertIn(('automation', u.AUTOMATION_ELEMENT_FROM_HANDLE, (77,)), com.log)
        self.assertEqual([entry[2][0] for entry in com.log if entry[:2] == ('cache', u.CACHE_ADD_PROPERTY)],
                         list(u.PROPERTIES))
        self.assertIn(('cache', u.CACHE_PUT_MODE, (0,)), com.log)
        com = self.Com(length=u.SCAN_MAX + 5)
        self.assertTrue(u.elements((640, 480), com_factory=lambda: com)['truncated'])
        self.assertEqual(sum(entry[1] == u.ARRAY_GET for entry in com.log), u.SCAN_MAX)
        for fail in ('automation', 'condition', 'cache', 'root', 'found', 'element', 'row'):
            com = self.Com(fail=fail)
            with self.assertRaises(u.UIAError):
                u.elements((640, 480), com_factory=lambda: com)
            self.assertEqual((com.live, com.closed), ([], 1), fail)

    def test_no_window_and_os_errors(self):
        com = self.Com()
        com.user.GetForegroundWindow = lambda: None
        self.assertEqual(u.elements((640, 480), com_factory=lambda: com),
                         dict(window='', elements=[], truncated=False))
        with self.assertRaises(u.UIAError):
            u.elements((640, 480), com_factory=Mock(side_effect=OSError('native')))

    def test_method_numbers_match_the_sdk_header(self):
        # Literals copied from UIAutomationClient.h (10.0.26100.0), not from the module under test.
        self.assertEqual(
            (u.RELEASE, u.AUTOMATION_ELEMENT_FROM_HANDLE, u.AUTOMATION_CONTROL_VIEW, u.AUTOMATION_CREATE_CACHE,
             u.AUTOMATION2_PUT_FOCUS, u.AUTOMATION2_PUT_CONNECTION, u.AUTOMATION2_PUT_TRANSACTION,
             u.CACHE_ADD_PROPERTY, u.CACHE_PUT_MODE, u.ELEMENT_FIND_ALL_CACHED, u.ARRAY_LENGTH, u.ARRAY_GET),
            (2, 6, 18, 20, 59, 61, 63, 3, 11, 8, 3, 4))
        self.assertEqual((u.CACHED_CONTROL_TYPE, u.CACHED_NAME, u.CACHED_FOCUS, u.CACHED_ENABLED,
                          u.CACHED_PASSWORD, u.CACHED_OFFSCREEN, u.CACHED_RECTANGLE), (53, 55, 58, 60, 67, 70, 75))
        self.assertEqual(u.PROPERTIES, (30001, 30003, 30005, 30008, 30010, 30019, 30022))

    def test_com_is_closed_once_and_a_half_made_object_is_released(self):
        com = object.__new__(u._Com)
        com.initialised, com.ole = False, Mock()
        com.close()
        com.ole.CoUninitialize.assert_not_called()
        com.initialised = True
        com.close()
        com.close()
        com.ole.CoUninitialize.assert_called_once_with()
        for init, expected in ((0, True), (1, True), (-2147417850, False)):
            com = object.__new__(u._Com)
            com.initialised, com.ole, com.release = False, Mock(), Mock()
            com.call = Mock(side_effect=u.UIAError('UIA_CALL'))
            with patch.object(u.ct, 'WinDLL', return_value=Mock(CoInitializeEx=Mock(return_value=init)), create=True):
                with self.assertRaises(u.UIAError):
                    com.automation()
            self.assertEqual((com.initialised, com.release.call_count), (expected, int(expected)))

    def test_value_is_never_requested(self):
        # ValueValue, LegacyIAccessibleValue and Text are the properties that hold typed content.
        self.assertFalse(set(u.PROPERTIES) & {30045, 30093, 30012})


class DragTests(NativeIsolatedTest):
    PATH = [dict(x=0, y=0), dict(x=100, y=50), dict(x=0, y=100)]

    def _insert(self, batches, results, desktop=None):
        device = object.__new__(w.WindowsBackend)
        sent, slept = [], []
        answers = iter(results)

        def send(size, batch, stride):
            sent.append([(event.mouse.dwFlags, event.mouse.dx, event.mouse.dy) for event in batch])
            answer = next(answers, size)
            if isinstance(answer, Exception):
                raise answer
            return answer

        device.SendInput = send
        device.desktop_is_default = Mock(side_effect=desktop) if desktop else Mock(return_value=True)
        return device.insert(batches, sleep=slept.append), sent, slept

    def test_default_drag_is_paced_and_ends_exactly(self):
        batches = w.prepare_input(dict(type='drag', path=self.PATH), (640, 480))
        self.assertIsInstance(batches, w.Paced)
        result, sent, slept = self._insert(batches, [])
        self.assertEqual(result, 'SENT')
        self.assertEqual([flags for flags, _, _ in sent[0]], [w.MOUSE_MOVE | w.MOUSE_ABSOLUTE, w.MOUSE_LEFT_DOWN])
        self.assertEqual([flags for flags, _, _ in sent[-1]], [w.MOUSE_LEFT_UP])
        self.assertEqual(len(sent) - 2, 2 * (p.DRAG_MS // w.DRAG_STEP_MS // 2))
        corner = sent[(len(sent) - 2) // 2][0]
        self.assertEqual(corner[1:], (w._absolute(100, 640), w._absolute(50, 480)))
        self.assertEqual(sent[-2][0][1:], (w._absolute(0, 640), w._absolute(100, 480)))
        self.assertEqual((slept[0], slept[-1], slept[1]), (0.05, 0.05, 0.01))
        self.assertLess(sum(slept), p.DRAG_MS / 1000 + 0.2)

    def test_short_and_long_durations_stay_bounded(self):
        for duration in (1, p.DRAG_MS_MAX):
            for points in (2, 20):
                path = [dict(x=i, y=i) for i in range(points)]
                batches = w.prepare_input(dict(type='drag', path=path, duration_ms=duration), (640, 480))
                self.assertLessEqual(len(batches), p.DRAG_MS_MAX // w.DRAG_STEP_MS + points + 2)
        flat = w.prepare_input(dict(type='drag', path=self.PATH, duration_ms=0), (640, 480))
        self.assertNotIsInstance(flat, w.Paced)
        self.assertEqual(len(flat), 5)

    def test_failures_release_the_button_and_are_unknown(self):
        batches = w.prepare_input(dict(type='drag', path=self.PATH), (640, 480))
        up = [(w.MOUSE_LEFT_UP, 0, 0)]
        result, sent, _ = self._insert(batches, [0])
        self.assertEqual((result, len(sent)), ('NOT_SENT', 1))
        for results in ([1], [2, 0], [2, 1, 0], [RuntimeError()], [2, RuntimeError()]):
            result, sent, _ = self._insert(batches, results)
            self.assertEqual(result, 'UNKNOWN', results)
            if results != [1]:
                self.assertEqual(sent[-1], up)
        # Only the move of the first batch went in, so no button is down.
        result, sent, _ = self._insert(batches, [1])
        self.assertEqual((result, len(sent)), ('UNKNOWN', 1))
        # A secure desktop in the middle: nothing more is sent, not even the release.
        result, sent, _ = self._insert(batches, [], desktop=[True, False, False])
        self.assertEqual((result, len(sent)), ('UNKNOWN', 2))

    def test_a_release_that_could_not_be_sent_goes_first_next_time(self):
        batches = w.prepare_input(dict(type='drag', path=self.PATH), (640, 480))
        click = w.prepare_input(dict(type='click', x=1, y=1, button='left'), (640, 480))
        device = object.__new__(w.WindowsBackend)
        sent = []

        def send(size, batch, stride):
            sent.append([event.mouse.dwFlags for event in batch])
            return size

        device.SendInput = send
        device.desktop_is_default = Mock(return_value=False)
        self.assertEqual(device.insert(batches, sleep=lambda _: None), 'UNKNOWN')
        self.assertEqual(len(sent), 1)
        # Still secure: nothing of the next operation is sent.
        self.assertEqual(device.insert(click), 'NOT_SENT')
        self.assertEqual(len(sent), 1)
        device.desktop_is_default = Mock(return_value=True)
        self.assertEqual(device.insert(click), 'SENT')
        self.assertEqual(sent[1], [w.MOUSE_LEFT_UP])
        self.assertEqual(len(sent), 3)
        self.assertEqual(device.insert(click), 'SENT')
        self.assertEqual(len(sent), 4)

    def test_interrupt_during_a_pause_releases_the_button(self):
        batches = w.prepare_input(dict(type='drag', path=self.PATH), (640, 480))
        device = object.__new__(w.WindowsBackend)
        sent = []

        def send(size, batch, stride):
            sent.append([event.mouse.dwFlags for event in batch])
            return size

        device.SendInput = send
        device.desktop_is_default = Mock(return_value=True)
        with self.assertRaises(KeyboardInterrupt):
            device.insert(batches, sleep=Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(sent[-1], [w.MOUSE_LEFT_UP])
        self.assertEqual(len(sent), 2)

    def test_schema(self):
        for extra in (dict(), dict(duration_ms=0), dict(duration_ms=2000)):
            p.validate_action(p.WireObject(type='drag', path=[p.WireObject(x=0, y=0)] * 2, **extra))
        for extra in (dict(duration_ms=-1), dict(duration_ms=2001), dict(duration_ms=True), dict(ms=1)):
            with self.assertRaises(p.ProtocolError):
                p.validate_action(p.WireObject(type='drag', path=[p.WireObject(x=0, y=0)] * 2, **extra))


class ClientFeatureTests(NativeIsolatedTest):
    def setUp(self):
        super().setUp()
        device = backend()
        device.elements = Mock(return_value=dict(window='W', elements=[dict(ITEM, name='名前')], truncated=False))
        self.agent = a.Agent(arguments(tools=ALL_TOOLS), KEY, device, (640, 480), files=Store())
        self.output, self.writes, self.marks, self.connects = [], Mock(), Mock(), 0

    def _connector(self, handler=None):
        def connect(*_):
            self.connects += 1
            return paired(handler or (lambda raw: self.agent.connection(raw, lambda _: None)))
        return connect

    def _cli(self, command, *fields, handler=None):
        host, local = private_pair()
        args = c.parser().parse_args([command, '--host', host, '--bind-local', local, '--port', '4418',
                                      '--key-file', 'fixture.key', *fields])
        return c.run(args, connector=self._connector(handler), key_loader=lambda _: KEY, writer=self.writes,
                     output=self.output.append, marker=self.marks)

    def _tag(self):
        self.assertEqual(self._cli('screenshot', '--out', 'fixture.png'), 0)
        return self.output[-1].split('etag=', 1)[1].split()[0]

    def _script(self, root, actions):
        path = Path(root) / f'script-{secrets.token_hex(4)}.json'
        path.write_text(json.dumps(dict(actions=actions)), encoding='utf-8')
        return str(path)

    def test_elements_print_ascii_json_only(self):
        self.assertEqual(self._cli('elements'), 0)
        self.assertTrue(self.output[-1].isascii())
        self.assertEqual(json.loads(self.output[-1][3:])['elements'][0]['name'], '名前')
        for change in (dict(type='Button!'), dict(x=-1), dict(width=0), dict(height=0), dict(x=1919), dict(y=1199), dict(name='x' * 81), dict(enabled=1), dict(extra=1)):
            self.agent.backend.elements.return_value = dict(window='W', elements=[dict(ITEM, **change)],
                                                            truncated=False)
            self.assertEqual(self._cli('elements'), 2)

    def test_bad_listings_are_refused(self):
        for change in (dict(inbox=[dict(name='..', size=1)]), dict(outbox=[dict(name='a', size=-1)]),
                       dict(outbox=[dict(name='a')]), dict(truncated=1), dict(inbox=[dict(name='a', size=1)] * 101)):
            listing = dict(inbox=[], outbox=[], truncated=False) | change
            self.agent.files.listing = Mock(return_value=listing)
            self.assertEqual(self._cli('files'), 2, change)

    def test_run_in_order_then_screenshot(self):
        with temp_root() as root:
            tag = self._tag()
            script = self._script(root, [dict(type='click', x=1, y=2, button='left'), dict(type='type', text='あ'),
                                         dict(type='keypress', keys=['ENTER']), dict(type='wait', ms=1)])
            self.agent.sleep = lambda _: None
            code = self._cli('run', '--boot-id', self.agent.boot_id, '--etag', tag, '--script', script,
                             '--out', 'after.png')
            self.assertEqual(code, 0, self.output)
            kinds = [entry.args[0]['type'] for entry in self.agent.backend.prepare_input.call_args_list]
            self.assertEqual(kinds, ['click', 'type', 'keypress'])
            self.assertEqual([line for line in self.output if line[:2] in ('1 ', '2 ', '3 ', '4 ')],
                             ['1 click SENT', '2 type SENT', '3 keypress SENT', '4 wait OK'])
            self.assertIn('screenshot OK', self.output[-1])
            self.assertEqual(self.writes.call_args.args[0], Path('after.png'))

    def test_run_stops_at_the_first_failure_without_resending(self):
        with temp_root() as root:
            script = self._script(root, [dict(type='type', text='a'), dict(type='type', text='b'),
                                         dict(type='keypress', keys=['ENTER'])])
            self.agent.backend.insert.side_effect = ['SENT', 'UNKNOWN', AssertionError('must not run')]
            code = self._cli('run', '--boot-id', self.agent.boot_id, '--script', script, '--out', 'after.png')
            self.assertEqual(code, c.UNKNOWN_EXIT)
            self.assertEqual(self.agent.backend.insert.call_count, 2)
            self.assertIn(f'3 keypress {c.NOT_EXECUTED}', self.output)
            self.assertIn('screenshot OK', self.output[-1])
            self.agent.backend.insert.reset_mock(side_effect=True)
            self.agent.backend.insert.side_effect = ['NOT_SENT', 'SENT', 'SENT']
            self.assertEqual(self._cli('run', '--boot-id', self.agent.boot_id, '--script', script,
                                       '--out', 'after2.png'), c.REFUSED_EXIT)
            self.assertEqual(self.agent.backend.insert.call_count, 1)

    def test_run_keeps_its_result_when_the_final_screenshot_fails(self):
        with temp_root() as root:
            script = self._script(root, [dict(type='type', text='a')])
            for outcome, code in (('UNKNOWN', c.UNKNOWN_EXIT), ('NOT_SENT', c.REFUSED_EXIT), ('SENT', 2)):
                self.agent.backend.insert.side_effect = [outcome]
                self.writes.side_effect = OSError('disk')
                self.assertEqual(self._cli('run', '--boot-id', self.agent.boot_id, '--script', script,
                                           '--out', 'after.png'), code)
                self.assertEqual(self.output[-1], 'screenshot ERROR')

    def test_results_belong_to_their_operation(self):
        remaining = dict(seconds=1, input_ops=1, non_input_ops=1)
        for result, good, bad in (('FILE_EXISTS', 'upload_create', 'click'),
                                  ('FILE_NOT_FOUND', 'get', 'upload_create'),
                                  ('TRANSFER_LIMIT', 'get', 'type'), ('ELEMENTS_FAILED', 'elements', 'files'),
                                  ('DIGEST_MISMATCH', 'upload_patch', 'get'), ('CREATED', 'upload_create', 'click')):
            status = p.RESULT_STATUS[result]
            extra = dict(id='a' * 32, offset=0) if result == 'CREATED' else {}
            p.validate_response(dict(result=result, remaining=remaining, **extra), status, good)
            with self.assertRaises(p.ProtocolError):
                p.validate_response(dict(result=result, remaining=remaining, **extra), status, bad)

    def test_run_script_rules_are_checked_before_any_connection(self):
        click = dict(type='click', x=1, y=2, button='left')
        with temp_root() as root:
            for actions in ([], [click, click], [dict(type='type', text='a'), click],
                            [dict(type='wait', ms=1)] * 11, [dict(type='screenshot')], [1],
                            [dict(type='type', text='a', extra=1)], [dict(type='keypress', keys=['META', 'L'])]):
                code = self._cli('run', '--boot-id', self.agent.boot_id, '--etag', '"x"', '--script',
                                 self._script(root, actions), '--out', 'after.png')
                self.assertEqual(code, 2, actions)
            script = self._script(root, [click])
            self.assertEqual(self._cli('run', '--boot-id', self.agent.boot_id, '--script', script,
                                       '--out', 'after.png'), 2)
            self.assertEqual(self._cli('run', '--boot-id', 'bad', '--etag', '"x"', '--script', script,
                                       '--out', 'after.png'), 2)
        self.assertEqual(self.connects, 0)
        self.assertEqual(self.agent.backend.insert.call_args_list, [])

    def test_a_wait_before_the_click_is_allowed(self):
        with temp_root() as root:
            path = Path(self._script(root, [dict(type='wait', ms=1), dict(type='click', x=1, y=2, button='left'),
                                            dict(type='type', text='a')]))
            self.assertEqual(len(c.load_script(path)), 3)
