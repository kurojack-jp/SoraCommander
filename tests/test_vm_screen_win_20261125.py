import ctypes as ct
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch
import zlib

from tests.test_vm_screen_protocol_20261125 import KEY, NativeIsolatedTest
from sora_commander_next import vm_protocol as p, vm_win as w


class WindowsBoundaryTests(NativeIsolatedTest):
    def test_64_bit_input_layout_and_native_signatures(self):
        self.assertEqual(ct.sizeof(ct.c_void_p), 8)
        self.assertEqual(ct.sizeof(w.INPUT), 40)
        self.assertEqual(w.INPUT.event.offset, 8)
        self.assertEqual(ct.sizeof(w.BitmapHeader), 40)
        self.assertEqual(ct.sizeof(w.FileInformation), 52)
        libraries = {name: SimpleNamespace(**{func: Mock() for func in signatures})
                     for name, signatures in w.WindowsBackend._signatures().items()}
        with patch.object(ct, 'WinDLL', side_effect=lambda name, **_: libraries[name]) as loader:
            device = w.WindowsBackend()
        self.assertEqual(loader.call_count, 6)
        for signatures in w.WindowsBackend._signatures().values():
            for name in signatures:
                function = getattr(device, name)
                self.assertIs(type(function.argtypes), tuple)
                self.assertFalse(isinstance(function.restype, Mock))
                function.assert_not_called()
        self.assertEqual(device.SendInput.argtypes, (w.U32, ct.POINTER(w.INPUT), w.I32))
        self.assertEqual(device.SendInput.restype, w.U32)

    def test_mouse_edges_normalization_batch_order_and_signed_wheel(self):
        size = (1920, 1200)
        req = dict(type='drag', path=[dict(x=0, y=0), dict(x=99, y=75), dict(x=1919, y=1199)], duration_ms=0)
        first, down, middle, last, up = w.prepare_input(req, size)
        self.assertEqual((first.mouse.dx, first.mouse.dy), (0, 0))
        self.assertEqual((last.mouse.dx, last.mouse.dy), (65535, 65535))
        self.assertEqual(last.mouse.dwFlags, w.MOUSE_MOVE | w.MOUSE_ABSOLUTE | w.MOUSE_MOVE_NOCOALESCE)
        self.assertEqual((down.mouse.dwFlags, up.mouse.dwFlags), (w.MOUSE_LEFT_DOWN, w.MOUSE_LEFT_UP))
        self.assertTrue(all(w._absolute(x, size[0]) * size[0] // 65536 == x for x in range(size[0])))
        self.assertEqual((middle.mouse.dx, middle.mouse.dy),
                         (w._absolute(99, size[0]), w._absolute(75, size[1])))
        self.assertEqual(middle.mouse.dwFlags, last.mouse.dwFlags)
        move, vertical, horizontal = w.prepare_input(
            dict(type='scroll', x=0, y=0, scroll_x=-150, scroll_y=2200), size)
        self.assertEqual([event.mouse.dwFlags for event in (move, vertical, horizontal)],
                         [w.MOUSE_MOVE | w.MOUSE_ABSOLUTE, w.MOUSE_WHEEL, w.MOUSE_HWHEEL])
        self.assertEqual([ct.c_int32(event.mouse.mouseData).value for event in (vertical, horizontal)],
                         [-2400, -240])
        _, right = w.prepare_input(dict(type='scroll', x=0, y=0, scroll_x=1, scroll_y=0), size)
        _, up_wheel = w.prepare_input(dict(type='scroll', x=0, y=0, scroll_x=0, scroll_y=-149), size)
        self.assertEqual((right.mouse.dwFlags, right.mouse.mouseData), (w.MOUSE_HWHEEL, 120))
        self.assertEqual((up_wheel.mouse.dwFlags, up_wheel.mouse.mouseData), (w.MOUSE_WHEEL, 120))
        self.assertEqual(len(w.prepare_input(dict(type='move', x=0, y=0), size)), 1)
        right_click = w.prepare_input(dict(type='click', x=0, y=0, button='right'), size)
        self.assertEqual([event.mouse.dwFlags for event in right_click[1:]],
                         [w.MOUSE_RIGHT_DOWN, w.MOUSE_RIGHT_UP])
        double = w.prepare_input(dict(type='double_click', x=0, y=0), size)
        self.assertEqual([event.mouse.dwFlags for event in double[1:]],
                         [w.MOUSE_LEFT_DOWN, w.MOUSE_LEFT_UP] * 2)

    def test_unicode_surrogates_and_extended_scan_code_flags(self):
        codes = list(struct.iter_unpack('<H', 'あ😀'.encode('utf-16-le')))
        events = w.prepare_input(dict(type='type', text='あ😀'), None)
        self.assertEqual([event.key.wScan for event in events], [code for (code,) in codes for _ in range(2)])
        self.assertEqual([event.key.dwFlags for event in events], [w.KEY_UNICODE, w.KEY_UNICODE | w.KEY_UP] * 3)
        for combo in p.KEYS:
            events = w.prepare_input(dict(type='keypress', keys=combo.split('+')), None)
            self.assertEqual(len(events), 4 if '+' in combo else 2)
        extended = {'Left': 0x4B, 'Right': 0x4D, 'Up': 0x48, 'Down': 0x50, 'Home': 0x47,
                    'End': 0x4F, 'PageUp': 0x49, 'PageDown': 0x51, 'Insert': 0x52,
                    'Delete': 0x53, 'Win': 0x5B}
        for name, scan in extended.items():
            down, up = w.prepare_input(dict(type='keypress', keys=[name]), None)
            self.assertEqual((down.key.wScan, up.key.wScan), (scan, scan))
            self.assertEqual(down.key.dwFlags, w.KEY_SCANCODE | w.KEY_EXTENDED)
            self.assertEqual(up.key.dwFlags, w.KEY_SCANCODE | w.KEY_EXTENDED | w.KEY_UP)
        ime = w.prepare_input(dict(type='keypress', keys=['IME']), None)
        self.assertEqual((ime[0].key.wVk, ime[0].key.dwFlags), (0xF3, 0))

    def _send(self, results, events, secure=False):
        device = object.__new__(w.WindowsBackend)
        device.SendInput = Mock(side_effect=results)
        device.desktop_is_default = Mock(return_value=not secure)
        result = device.insert(events)
        for send in device.SendInput.call_args_list:
            size, batch, stride = send.args
            self.assertEqual((size, stride), (len(batch), 40))
        return result, device.SendInput.call_args_list

    def test_sendinput_full_zero_short_and_exception_have_distinct_outcomes(self):
        events = w.prepare_input(dict(type='keypress', keys=['home', 'control']), None)
        sent, calls = self._send([4], events)
        self.assertEqual((sent, len(calls)), ('SENT', 1))
        zero, calls = self._send([0], events)
        self.assertEqual((zero, len(calls)), ('NOT_SENT', 1))
        short, calls = self._send([2, 2], events)
        self.assertEqual((short, len(calls)), ('UNKNOWN', 2))
        self.assertEqual([event.key.wScan for event in calls[-1].args[1]], [0x47, 0x1D])
        self.assertTrue(all(event.key.dwFlags & w.KEY_UP for event in calls[-1].args[1]))
        failed, calls = self._send([RuntimeError(), 0], events)
        self.assertEqual((failed, len(calls)), ('UNKNOWN', 2))
        secure, calls = self._send([2], events, secure=True)
        self.assertEqual((secure, len(calls)), ('UNKNOWN', 1))
        drag = w.prepare_input(dict(type='drag', path=[dict(x=0, y=0), dict(x=1, y=1)], duration_ms=0), (640, 480))
        result, calls = self._send([2, 1], drag)
        self.assertEqual(result, 'UNKNOWN')
        self.assertEqual(calls[-1].args[1][0].mouse.dwFlags, w.MOUSE_LEFT_UP)

    def _gdi(self):
        device = object.__new__(w.WindowsBackend)
        trace = Mock()
        values = {'GetDC': 10, 'CreateCompatibleDC': 20, 'CreateCompatibleBitmap': 30,
                  'SelectObject': 40, 'BitBlt': 1, 'GetDIBits': 1,
                  'DeleteDC': 1, 'DeleteObject': 1, 'ReleaseDC': 1}
        for name, value in values.items():
            mock = Mock(return_value=value)
            trace.attach_mock(mock, name)
            setattr(device, name, mock)
        return device, trace

    def test_capture_uses_top_down_dib_after_unselect_and_frees_handles(self):
        device, trace = self._gdi()
        calls = []

        def pixels(dc, bitmap, start, lines, buffer, info, usage):
            header = info._obj.header
            calls.append(((dc, bitmap, start, lines, usage), (header.width, header.height, header.bit_count)))
            buffer.raw = bytes((10, 20, 30, 255, 40, 50, 60, 255))
            return 1

        device.GetDIBits.side_effect = pixels
        png = device.capture((2, 1))
        self.assertEqual(calls, [((10, 30, 0, 1, 0), (2, -1, 32))])
        self.assertTrue(png.startswith(w.PNG_SIGNATURE))
        self.assertEqual(struct.unpack('!II', png[16:24]), (2, 1))
        names = [call[0] for call in trace.mock_calls]
        self.assertLess(names.index('GetDIBits'), names.index('DeleteObject'))
        self.assertEqual(names[:7], ['GetDC', 'CreateCompatibleDC', 'CreateCompatibleBitmap',
                                    'SelectObject', 'BitBlt', 'SelectObject', 'GetDIBits'])
        self.assertEqual(trace.mock_calls[-3:], [call.DeleteDC(20), call.DeleteObject(30), call.ReleaseDC(None, 10)])
        chunk_len, = struct.unpack('!I', png[33:37])
        self.assertEqual(zlib.decompress(png[41:41 + chunk_len]), bytes((0, 30, 20, 10, 60, 50, 40)))

    def test_capture_api_failure_still_releases_all_owned_resources(self):
        for name in ('CreateCompatibleDC', 'CreateCompatibleBitmap', 'SelectObject', 'BitBlt', 'GetDIBits'):
            device, trace = self._gdi()
            getattr(device, name).return_value = None if name in (
                'CreateCompatibleDC', 'CreateCompatibleBitmap', 'SelectObject') else 0
            with self.subTest(api=name), self.assertRaises(w.WindowsError):
                device.capture((1, 1))
            self.assertEqual(trace.mock_calls[-1], call.ReleaseDC(None, 10))
            if name in ('SelectObject', 'BitBlt', 'GetDIBits'):
                device.DeleteObject.assert_called_once_with(30)
        device, _ = self._gdi()
        device.DeleteDC.return_value = 0
        with self.assertRaises(w.WindowsError):
            device.capture((1, 1))
        device.DeleteObject.assert_called_once_with(30)
        device.ReleaseDC.assert_called_once_with(None, 10)
        device, _ = self._gdi()
        device.DeleteDC.side_effect = RuntimeError('fixture')
        with self.assertRaises(RuntimeError):
            device.capture((1, 1))
        device.DeleteObject.assert_called_once_with(30)
        device.ReleaseDC.assert_called_once_with(None, 10)

    def _identity(self, elevation_type=1, elevation=0):
        device = object.__new__(w.WindowsBackend)
        device.GetCurrentProcess = Mock(return_value=w.INVALID_HANDLE)
        device.CloseHandle = Mock(return_value=1)
        device.IsUserAnAdmin = Mock(return_value=0)
        device.CreateWellKnownSid = Mock(return_value=1)
        device.CheckTokenMembership = Mock(return_value=1)

        def open_token(process, access, output):
            output._obj.value = 73
            return 1

        def token_info(token, kind, value, size, returned):
            value._obj.value = {18: elevation_type, 20: elevation}[kind]
            returned._obj.value = size
            return 1

        device.OpenProcessToken = Mock(side_effect=open_token)
        device.GetTokenInformation = Mock(side_effect=token_info)
        return device

    def test_token_rejects_admin_filtered_admin_and_api_errors(self):
        self.assertTrue(self._identity().standard_user())
        self.assertFalse(self._identity(elevation_type=3).standard_user())
        self.assertFalse(self._identity(elevation_type=2, elevation=1).standard_user())
        self.assertFalse(self._identity(elevation=1).standard_user())
        device = self._identity()
        device.IsUserAnAdmin.return_value = 1
        self.assertFalse(device.standard_user())
        for method in ('OpenProcessToken', 'GetTokenInformation', 'CloseHandle',
                       'CreateWellKnownSid', 'CheckTokenMembership'):
            device = self._identity()
            target = getattr(device, method)
            target.side_effect = None
            target.return_value = 0
            with self.subTest(api=method), self.assertRaises(w.WindowsError):
                device.standard_user()

    def test_desktop_and_monitor_checks_are_fail_closed(self):
        device = object.__new__(w.WindowsBackend)
        device.session_unlocked = Mock(return_value=True)
        device.OpenInputDesktop = Mock(return_value=13)
        device.CloseDesktop = Mock(return_value=1)

        def desktop(handle, index, buffer, size, length):
            buffer.value = 'Winlogon'
            return 1

        device.GetUserObjectInformationW = Mock(side_effect=desktop)
        self.assertFalse(device.desktop_is_default())
        device.CloseDesktop.assert_called_once_with(13)
        device.OpenInputDesktop.return_value = None
        self.assertFalse(device.desktop_is_default())
        device.GetSystemMetrics = Mock(return_value=2)
        with self.assertRaises(w.WindowsError):
            device.screen_size()
        device.GetSystemMetrics.assert_called_once_with(80)

    def test_registry_type_is_checked_using_injected_winreg(self):
        handle = MagicMock()
        raw = SimpleNamespace(HKEY_LOCAL_MACHINE=1, KEY_READ=2, KEY_WOW64_64KEY=4, REG_SZ=5,
                              OpenKey=Mock(return_value=handle), QueryValueEx=Mock(return_value=('Value', 5)))
        with patch.dict(sys.modules, {'winreg': raw}):
            self.assertEqual(w.read_registry('fixture', 'name'), 'Value')
            raw.OpenKey.assert_called_once_with(1, 'fixture', 0, 6)
            raw.QueryValueEx.return_value = ('Value', 8)
            with self.assertRaises(w.WindowsError):
                w.read_registry('fixture', 'name')

    def test_interface_table_uses_network_byte_order_and_refuses_api_errors(self):
        address = (10 << 24) + 2
        mask = 0xffffff00
        row = struct.pack('!I', address) + bytes(4) + struct.pack('!I', mask) + bytes(12)
        disconnected = row[:22] + struct.pack('=H', 0x08)
        payload = struct.pack('=I', 2) + row + disconnected

        def table(buffer, size, order):
            if buffer is None:
                size._obj.value = len(payload)
                return 122
            buffer.raw = payload
            return 0

        api = Mock(side_effect=table)
        with patch.object(ct, 'WinDLL', return_value=SimpleNamespace(GetIpAddrTable=api)):
            self.assertEqual(w.interface_addresses(), [(address, mask)])
            api.side_effect = None
            api.return_value = 5
            with self.assertRaises(w.WindowsError):
                w.interface_addresses()

    def test_key_file_rejects_links_reparse_non_disk_and_oversize_before_read(self):
        class Fixture:
            def __init__(self, **changes):
                self.attrs = {'attributes': 0, 'size_high': 0, 'size_low': 64, 'links': 1} | changes
                self.CreateFileW = Mock(return_value=21)
                self.GetFileType = Mock(return_value=1)
                self.CloseHandle = Mock(return_value=1)
                self.GetFileInformationByHandle = Mock(side_effect=self.info)
                self.ReadFile = Mock(side_effect=self.read)

            def info(self, handle, target):
                for name, value in self.attrs.items():
                    setattr(target._obj, name, value)
                return 1

            def read(self, handle, buffer, maximum, count, overlapped):
                raw = KEY.hex().encode('ascii')
                buffer[:len(raw)] = raw
                count._obj.value = len(raw)
                return 1

        for change in (dict(attributes=0x400), dict(attributes=0x10), dict(links=2),
                       dict(size_high=1), dict(size_low=129), {}):
            device = Fixture(**change)
            if not change:
                device.GetFileType.return_value = 3
            with patch.object(ct, 'WinDLL', return_value=device), self.assertRaises(w.WindowsError):
                w.read_key_bytes(Path('fixture.key'), 128)
            device.ReadFile.assert_not_called()
            device.CloseHandle.assert_called_once_with(21)
        device = Fixture()
        with patch.object(ct, 'WinDLL', return_value=device):
            self.assertTrue(p.decode_key(w.read_key_bytes(Path('fixture.key'), 128)) == KEY)
        device.CreateFileW.assert_called_once_with('fixture.key', 0x80000000, 1, None, 3, 0x00200000, None)
        device.CloseHandle.assert_called_once_with(21)

    def _locked_device(self, *, level=1, session=4, flags=1, size=None, ok=1):
        head = w.SessionInfoHead(level=level, session_id=session, state=0, flags=flags)
        device = object.__new__(w.WindowsBackend)
        freed = []

        def query(server, asked, kind, buffer, count):
            self.seen = (server, asked, kind)
            if ok:
                buffer._obj.value = ct.addressof(head)
                count._obj.value = ct.sizeof(head) if size is None else size
            return ok

        device.keep = head
        device.session_id = Mock(return_value=4)
        device.WTSQuerySessionInformationW = query
        device.WTSFreeMemory = lambda pointer: freed.append(pointer.value)
        device.OpenInputDesktop = Mock(return_value=13)
        device.CloseDesktop = Mock(return_value=1)

        def name(_handle, _kind, buffer, _size, _needed):
            buffer.value = 'Default'
            return 1

        device.GetUserObjectInformationW = name
        return device, freed, ct.addressof(head)

    def test_session_lock_state_is_required_before_the_desktop_name(self):
        self.assertEqual((ct.sizeof(w.SessionInfoHead), w.SessionInfoHead.session_id.offset,
                          w.SessionInfoHead.flags.offset), (20, 8, 16))
        device, freed, address = self._locked_device()
        self.assertIs(device.desktop_is_default(), True)
        self.assertEqual(self.seen, (None, 4, 25))
        self.assertEqual(freed, [address])
        for change in (dict(flags=0), dict(flags=0xFFFFFFFF), dict(flags=2), dict(level=0),
                       dict(session=5)):
            with self.subTest(change=change):
                device, freed, address = self._locked_device(**change)
                self.assertIs(device.desktop_is_default(), False)
                device.OpenInputDesktop.assert_not_called()
                self.assertEqual(freed, [address])
        for change, count in ((dict(size=19), 1), (dict(ok=0), 0)):
            with self.subTest(change=change):
                device, freed, address = self._locked_device(**change)
                with self.assertRaises(w.WindowsError):
                    device.desktop_is_default()
                self.assertEqual(len(freed), count)
