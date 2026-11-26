"""Windows API boundary. Native libraries and the registry are opened on demand."""

import ctypes as ct
from contextlib import contextmanager
import os
import struct
import time
import zlib

if __package__:
    from . import vm_protocol as p
else:
    import vm_protocol as p


U16 = ct.c_uint16
U32 = ct.c_uint32
I32 = ct.c_int32
HANDLE = ct.c_void_p
WIDE = ct.c_wchar_p
P_U32 = ct.POINTER(U32)
INVALID_HANDLE = HANDLE(-1).value
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
MOUSE_MOVE = 0x0001
MOUSE_LEFT_DOWN = 0x0002
MOUSE_LEFT_UP = 0x0004
MOUSE_RIGHT_DOWN = 0x0008
MOUSE_RIGHT_UP = 0x0010
MOUSE_MIDDLE_DOWN = 0x0020
MOUSE_MIDDLE_UP = 0x0040
MOUSE_BUTTONS = {
    'left': (MOUSE_LEFT_DOWN, MOUSE_LEFT_UP),
    'right': (MOUSE_RIGHT_DOWN, MOUSE_RIGHT_UP),
    'middle': (MOUSE_MIDDLE_DOWN, MOUSE_MIDDLE_UP),
}
MOUSE_WHEEL = 0x0800
MOUSE_HWHEEL = 0x1000
MOUSE_MOVE_NOCOALESCE = 0x2000
MOUSE_ABSOLUTE = 0x8000
KEY_EXTENDED = 0x0001
KEY_UP = 0x0002
KEY_UNICODE = 0x0004
KEY_SCANCODE = 0x0008


class WindowsError(Exception):
    """A failed native check with a bounded, fixed reason."""


class MouseInput(ct.Structure):
    _fields_ = [('dx', I32), ('dy', I32), ('mouseData', U32), ('dwFlags', U32),
                ('time', U32), ('dwExtraInfo', ct.c_size_t)]


class KeyboardInput(ct.Structure):
    _fields_ = [('wVk', U16), ('wScan', U16), ('dwFlags', U32), ('time', U32),
                ('dwExtraInfo', ct.c_size_t)]


class HardwareInput(ct.Structure):
    _fields_ = [('uMsg', U32), ('wParamL', U16), ('wParamH', U16)]


class InputUnion(ct.Union):
    _fields_ = [('mouse', MouseInput), ('key', KeyboardInput), ('hardware', HardwareInput)]


class INPUT(ct.Structure):
    _anonymous_ = ('event',)
    _fields_ = [('type', U32), ('event', InputUnion)]


class BitmapHeader(ct.Structure):
    _fields_ = [('size', U32), ('width', I32), ('height', I32), ('planes', U16),
                ('bit_count', U16), ('compression', U32), ('size_image', U32),
                ('x_pixels_per_metre', I32), ('y_pixels_per_metre', I32),
                ('colours_used', U32), ('colours_important', U32)]


class BitmapInfo(ct.Structure):
    _fields_ = [('header', BitmapHeader), ('colour', U32)]


class FileInformation(ct.Structure):
    _fields_ = [('attributes', U32), ('created', U32 * 2), ('accessed', U32 * 2),
                ('modified', U32 * 2), ('volume', U32), ('size_high', U32),
                ('size_low', U32), ('links', U32), ('index_high', U32), ('index_low', U32)]


class SessionInfoHead(ct.Structure):
    # WTSINFOEXW up to SessionFlags. The union holds 64-bit values, so it starts at offset 8.
    _fields_ = [('level', U32), ('padding', U32), ('session_id', U32), ('state', U32),
                ('flags', U32)]


class IpAddressRow(ct.Structure):
    _fields_ = [('address', U32), ('index', U32), ('mask', U32), ('broadcast', U32),
                ('reassembly', U32), ('reserved', U16), ('kind', U16)]


# Set 1 scan codes for a Windows keyboard. E0 is sent through KEY_EXTENDED.
SCAN_CODES = {
    'Enter': 0x1C, 'Tab': 0x0F, 'Escape': 0x01, 'Backspace': 0x0E,
    'F11': 0x57, 'F12': 0x58, 'Space': 0x39, 'Win': 0xE05B, 'Ctrl': 0x1D,
    'Shift': 0x2A, 'Alt': 0x38,
    'Delete': 0xE053, 'Insert': 0xE052, 'Left': 0xE04B, 'Right': 0xE04D,
    'Up': 0xE048, 'Down': 0xE050, 'Home': 0xE047, 'End': 0xE04F,
    'PageUp': 0xE049, 'PageDown': 0xE051,
}
SCAN_CODES.update({f'F{number}': 0x3A + number for number in range(1, 11)})
for _row, _first in (('1234567890', 0x02), ('QWERTYUIOP', 0x10),
                     ('ASDFGHJKL', 0x1E), ('ZXCVBNM', 0x2C)):
    SCAN_CODES.update(zip(_row, range(_first, _first + len(_row))))


def _check(ok, reason):
    if not ok:
        raise WindowsError(reason)


def _bind(library, name, result, *args):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = args
    return function


def read_registry(key, name):
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
        value, kind = winreg.QueryValueEx(handle, name)
    _check(kind == winreg.REG_SZ and type(value) is str, 'REGISTRY_TYPE')
    return value


def interface_addresses():
    lib = ct.WinDLL('iphlpapi', use_last_error=True)
    get_table = _bind(lib, 'GetIpAddrTable', U32, HANDLE, P_U32, I32)
    size = U32()
    _check(get_table(None, ct.byref(size), 0) == 122 and 4 <= size.value <= 1024 * 1024,
           'IP_INTERFACE_TABLE')
    table = ct.create_string_buffer(size.value)
    _check(get_table(table, ct.byref(size), 0) == 0 and 4 <= size.value <= len(table),
           'IP_INTERFACE_TABLE')
    count = U32.from_buffer(table).value
    stride = ct.sizeof(IpAddressRow)
    _check(count <= (size.value - 4) // stride, 'IP_INTERFACE_TABLE')
    addresses = []
    for row in (IpAddressRow * count).from_buffer(table, 4):
        if not row.kind & (0x08 | 0x40):  # disconnected or being deleted
            address, mask = struct.unpack('!II', struct.pack('=II', row.address, row.mask))
            addresses.append((address, mask))
    return addresses


def read_key_bytes(path, maximum):
    # Inspect and read the same handle, with writers and renames excluded.
    path = os.fspath(path)
    _check(':' not in os.path.splitdrive(path)[1] and not path.startswith('\\\\.\\'),
           'KEY_FILE')
    lib = ct.WinDLL('kernel32', use_last_error=True)
    create = _bind(lib, 'CreateFileW', HANDLE, WIDE, U32, U32, HANDLE, U32, U32, HANDLE)
    kind = _bind(lib, 'GetFileType', U32, HANDLE)
    info = _bind(lib, 'GetFileInformationByHandle', I32, HANDLE, ct.POINTER(FileInformation))
    read = _bind(lib, 'ReadFile', I32, HANDLE, HANDLE, U32, P_U32, HANDLE)
    close = _bind(lib, 'CloseHandle', I32, HANDLE)
    handle = create(path, 0x80000000, 1, None, 3, 0x00200000, None)
    _check(handle not in (None, INVALID_HANDLE), 'KEY_FILE')
    try:
        entry = FileInformation()
        _check(kind(handle) == 1 and info(handle, ct.byref(entry)), 'KEY_FILE')
        _check(not entry.attributes & (0x400 | 0x10) and entry.links == 1
               and entry.size_high == 0 and entry.size_low <= maximum, 'KEY_FILE')
        data = ct.create_string_buffer(maximum + 1)
        count = U32()
        _check(read(handle, data, len(data), ct.byref(count), None)
               and count.value == entry.size_low, 'KEY_FILE')
        return data.raw[:count.value]
    finally:
        _check(close(handle), 'KEY_FILE')


def _png_chunk(kind, data):
    return (struct.pack('!I', len(data)) + kind + data
            + struct.pack('!I', zlib.crc32(data, zlib.crc32(kind))))


def encode_png(width, height, bgra):
    _check(len(bgra) == width * height * 4, 'BITMAP_SIZE')
    rows = bytearray((1 + width * 3) * height)
    for y in range(height):
        row = bgra[y * width * 4:(y + 1) * width * 4]
        start = y * (1 + width * 3) + 1
        end = start + width * 3
        rows[start:end:3] = row[2::4]
        rows[start + 1:end:3] = row[1::4]
        rows[start + 2:end:3] = row[0::4]
    header = struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (PNG_SIGNATURE + _png_chunk(b'IHDR', header)
            + _png_chunk(b'IDAT', zlib.compress(rows, 1)) + _png_chunk(b'IEND', b''))


def _absolute(position, extent):
    if position == 0:
        return 0
    if position == extent - 1:
        return 65535
    return (2 * position + 1) * 65536 // (2 * extent)


def _mouse(flags, x=0, y=0, data=0):
    return INPUT(type=0, mouse=MouseInput(x, y, data, flags, 0, 0))


def _move(x, y, size, extra=0):
    w, h = size
    return _mouse(MOUSE_MOVE | MOUSE_ABSOLUTE | extra, _absolute(x, w), _absolute(y, h))


def _key(flags, scan=0, vk=0):
    return INPUT(type=1, key=KeyboardInput(vk, scan, flags, 0, 0))


def _scan(name, flags):
    if name == 'IME':
        return _key(flags, vk=0xF3)  # VK_DBE_SBCSCHAR, the Japanese half/full-width key
    scan = SCAN_CODES[name]
    return _key(flags | KEY_SCANCODE | (KEY_EXTENDED if scan >> 8 else 0), scan & 0xFF)


class Paced(list):
    """(delay in ms, events) batches of one operation. Each batch is one SendInput call."""

    def seconds(self):
        return sum(delay for delay, _ in self) / 1000


DRAG_STEP_MS = 10
DRAG_HOLD_MS = 50


def _drag(path, duration, size):
    first, *rest = path
    press = [_move(first['x'], first['y'], size), _mouse(MOUSE_LEFT_DOWN)]
    if duration == 0:
        return press + [_move(point['x'], point['y'], size, MOUSE_MOVE_NOCOALESCE) for point in rest] \
            + [_mouse(MOUSE_LEFT_UP)]
    steps = max(1, duration // DRAG_STEP_MS // len(rest))
    batches, delay = Paced([(0, press)]), DRAG_HOLD_MS
    start = first
    for point in rest:
        for step in range(1, steps + 1):
            x = start['x'] + (point['x'] - start['x']) * step // steps
            y = start['y'] + (point['y'] - start['y']) * step // steps
            batches.append((delay, [_move(x, y, size, MOUSE_MOVE_NOCOALESCE)]))
            delay = DRAG_STEP_MS
        start = point
    batches.append((DRAG_HOLD_MS, [_mouse(MOUSE_LEFT_UP)]))
    return batches


def prepare_input(req, size):
    op = req['type']
    if op == 'type':
        utf16 = req['text'].encode('utf-16-le')
        controls = {ord('\n'): 'Enter', ord('\t'): 'Tab'}
        return [(_scan(controls[code], flags) if code in controls else _key(KEY_UNICODE | flags, code))
                for (code,) in struct.iter_unpack('<H', utf16)
                for flags in (0, KEY_UP)]
    if op == 'keypress':
        names = p.key_combo(req['keys'])
        return [_scan(name, 0) for name in names] + [_scan(name, KEY_UP) for name in reversed(names)]
    if op == 'drag':
        return _drag(req['path'], req.get('duration_ms', p.DRAG_MS), size)
    move = _move(req['x'], req['y'], size)
    if op == 'move':
        return [move]
    if op == 'scroll':
        vertical = -p.notches(req['scroll_y'])
        horizontal = p.notches(req['scroll_x'])
        return [move] + [_mouse(flag, data=amount * 120) for flag, amount in
                         ((MOUSE_WHEEL, vertical), (MOUSE_HWHEEL, horizontal)) if amount]
    if op == 'double_click':
        return [move] + [_mouse(flags) for flags in (MOUSE_LEFT_DOWN, MOUSE_LEFT_UP) * 2]
    down, up = MOUSE_BUTTONS[req['button']]
    return [move, _mouse(down), _mouse(up)]


def _releases(events, prefix_known):
    held = {}
    for event in events:
        if event.type == 1:
            key = event.key
            flags = key.dwFlags & ~KEY_UP
            identity = (key.wVk, key.wScan, flags)
            if key.dwFlags & KEY_UP:
                if prefix_known:
                    held.pop(identity, None)
            else:
                held[identity] = _key(flags | KEY_UP, key.wScan, key.wVk)
        else:
            flags = event.mouse.dwFlags
            for down, up in MOUSE_BUTTONS.values():
                if flags & down:
                    held[down] = _mouse(up)
                elif flags & up and prefix_known:
                    held.pop(down, None)
    return list(reversed(held.values()))


class WindowsBackend:
    def __init__(self):
        for library, signatures in self._signatures().items():
            dll = ct.WinDLL(library, use_last_error=True)
            for name, signature in signatures.items():
                setattr(self, name, _bind(dll, name, *signature))

    @staticmethod
    def _signatures():
        return {
            'kernel32': {
                'GetCurrentProcess': (HANDLE,), 'GetCurrentProcessId': (U32,),
                'ProcessIdToSessionId': (I32, U32, P_U32), 'CloseHandle': (I32, HANDLE),
                'GetStdHandle': (HANDLE, U32), 'GetConsoleMode': (I32, HANDLE, P_U32),
                'SetConsoleMode': (I32, HANDLE, U32), 'GetConsoleWindow': (HANDLE,),
            },
            'advapi32': {
                'OpenProcessToken': (I32, HANDLE, U32, ct.POINTER(HANDLE)),
                'GetTokenInformation': (I32, HANDLE, I32, HANDLE, U32, P_U32),
                'CreateWellKnownSid': (I32, I32, HANDLE, HANDLE, P_U32),
                'CheckTokenMembership': (I32, HANDLE, HANDLE, ct.POINTER(I32)),
            },
            'shell32': {'IsUserAnAdmin': (I32,)},
            'wtsapi32': {
                'WTSQuerySessionInformationW': (I32, HANDLE, U32, I32, ct.POINTER(HANDLE), P_U32),
                'WTSFreeMemory': (None, HANDLE),
            },
            'user32': {
                'SetProcessDpiAwarenessContext': (I32, HANDLE), 'GetSystemMetrics': (I32, I32),
                'OpenInputDesktop': (HANDLE, U32, I32, U32), 'CloseDesktop': (I32, HANDLE),
                'GetUserObjectInformationW': (I32, HANDLE, I32, HANDLE, U32, P_U32),
                'GetDC': (HANDLE, HANDLE), 'ReleaseDC': (I32, HANDLE, HANDLE),
                'SendInput': (U32, U32, ct.POINTER(INPUT), I32),
                'ShowWindowAsync': (I32, HANDLE, I32),
            },
            'gdi32': {
                'CreateCompatibleDC': (HANDLE, HANDLE), 'DeleteDC': (I32, HANDLE),
                'CreateCompatibleBitmap': (HANDLE, HANDLE, I32, I32), 'DeleteObject': (I32, HANDLE),
                'SelectObject': (HANDLE, HANDLE, HANDLE),
                'BitBlt': (I32, HANDLE, I32, I32, I32, I32, HANDLE, I32, I32, U32),
                'GetDIBits': (I32, HANDLE, HANDLE, U32, U32, HANDLE, ct.POINTER(BitmapInfo), U32),
            },
        }

    def disable_quick_edit(self):
        handle = self.GetStdHandle(0xFFFFFFF6)  # STD_INPUT_HANDLE (-10 as a DWORD)
        mode = U32()
        if handle in (None, 0, INVALID_HANDLE) or not self.GetConsoleMode(handle, ct.byref(mode)):
            return False
        enable_extended_flags, enable_quick_edit = 0x0080, 0x0040
        return bool(self.SetConsoleMode(handle, (mode.value | enable_extended_flags) & ~enable_quick_edit))

    def minimize_console(self):
        window = self.GetConsoleWindow()
        return bool(window and self.ShowWindowAsync(window, 6))  # SW_MINIMIZE

    def set_dpi(self):
        return bool(self.SetProcessDpiAwarenessContext(HANDLE(-4)))

    @contextmanager
    def _token(self):
        token = HANDLE()
        _check(self.OpenProcessToken(self.GetCurrentProcess(), 0x0008, ct.byref(token)), 'TOKEN')
        try:
            yield token
        finally:
            _check(self.CloseHandle(token), 'TOKEN')

    def standard_user(self):
        with self._token() as token:
            for kind, expected in ((18, 1), (20, 0)):  # elevation type Default, elevation false
                value, returned = U32(), U32()
                _check(self.GetTokenInformation(token, kind, ct.byref(value), ct.sizeof(value),
                                               ct.byref(returned)) and returned.value == ct.sizeof(value),
                       'TOKEN')
                if value.value != expected:
                    return False
        # The shell wrapper has no distinct error result. Require a successful SID check too.
        sid = ct.create_string_buffer(68)  # SECURITY_MAX_SID_SIZE
        size, member = U32(len(sid)), I32()
        _check(self.CreateWellKnownSid(26, None, sid, ct.byref(size))
               and self.CheckTokenMembership(None, sid, ct.byref(member)), 'TOKEN')
        return not self.IsUserAnAdmin() and member.value == 0

    def session_id(self):
        session = U32()
        _check(self.ProcessIdToSessionId(self.GetCurrentProcessId(), ct.byref(session)), 'SESSION')
        return session.value

    def screen_size(self):
        _check(self.GetSystemMetrics(80) == 1, 'MONITOR_COUNT')  # SM_CMONITORS
        return self.GetSystemMetrics(0), self.GetSystemMetrics(1)

    def session_unlocked(self):
        # The lock screen with the clock is on the Default desktop, so its name cannot tell.
        session, buffer, size = self.session_id(), HANDLE(), U32()
        _check(self.WTSQuerySessionInformationW(None, session, 25, ct.byref(buffer), ct.byref(size))
               and buffer, 'SESSION_LOCK')  # WTSSessionInfoEx
        try:
            _check(size.value >= ct.sizeof(SessionInfoHead), 'SESSION_LOCK')
            head = SessionInfoHead.from_buffer_copy(ct.string_at(buffer, ct.sizeof(SessionInfoHead)))
            # WTS_SESSIONSTATE_UNLOCK is 1. Locked (0) and unknown are both refused.
            return head.level == 1 and head.session_id == session and head.flags == 1
        finally:
            self.WTSFreeMemory(buffer)

    def desktop_is_default(self):
        if not self.session_unlocked():
            return False
        desktop = self.OpenInputDesktop(0, 0, 0x0001)  # DESKTOP_READOBJECTS
        if not desktop:
            return False
        try:
            name = ct.create_unicode_buffer(256)
            needed = U32()
            return bool(self.GetUserObjectInformationW(desktop, 2, name, ct.sizeof(name),
                                                       ct.byref(needed)) and name.value == 'Default')
        finally:
            _check(self.CloseDesktop(desktop), 'DESKTOP')

    def capture(self, size):
        width, height = size
        screen = self.GetDC(None)
        _check(screen, 'SCREEN_DC')
        memory = bitmap = old = None
        try:
            memory = self.CreateCompatibleDC(screen)
            _check(memory, 'MEMORY_DC')
            bitmap = self.CreateCompatibleBitmap(screen, width, height)
            _check(bitmap, 'BITMAP')
            previous = self.SelectObject(memory, bitmap)
            _check(previous not in (None, INVALID_HANDLE), 'SELECT_BITMAP')
            old = previous
            _check(self.BitBlt(memory, 0, 0, width, height, screen, 0, 0, 0x40CC0020), 'CAPTURE')
            _check(self.SelectObject(memory, old) not in (None, INVALID_HANDLE), 'SELECT_BITMAP')
            old = None
            info = BitmapInfo(BitmapHeader(ct.sizeof(BitmapHeader), width, -height, 1, 32))
            pixels = ct.create_string_buffer(width * height * 4)
            _check(self.GetDIBits(screen, bitmap, 0, height, pixels, ct.byref(info), 0) == height,
                   'PIXELS')
        finally:
            self._release_capture(screen, memory, bitmap, old)
        return encode_png(width, height, pixels.raw)

    def _release_capture(self, screen, memory, bitmap, old):
        try:
            _check(old is None or self.SelectObject(memory, old) not in (None, INVALID_HANDLE),
                   'CAPTURE_CLEANUP')
        finally:
            try:
                _check(not memory or self.DeleteDC(memory), 'CAPTURE_CLEANUP')
            finally:
                try:
                    _check(not bitmap or self.DeleteObject(bitmap), 'CAPTURE_CLEANUP')
                finally:
                    _check(self.ReleaseDC(None, screen), 'CAPTURE_CLEANUP')

    prepare_input = staticmethod(prepare_input)

    def _send_batch(self, events):
        batch = (INPUT * len(events))(*events)
        return self.SendInput(len(batch), batch, ct.sizeof(INPUT))

    def elements(self, size):
        if __package__:
            from . import vm_uia
        else:
            import vm_uia
        return vm_uia.elements(size)

    def _insert_paced(self, batches, sleep):
        # Windows may insert other input between batches. A single SendInput call cannot pause.
        sent = []
        for delay, events in batches:
            if sent:
                try:
                    sleep(delay / 1000)
                except BaseException:  # Ctrl+C must not leave the button down.
                    self._release(sent, True)
                    raise
            try:
                count = self._send_batch(events) if not sent or self.desktop_is_default() else 0
            except Exception:
                count = None
            if count == len(events):
                sent += events
                continue
            if not sent and count == 0:
                return 'NOT_SENT'
            # Everything before this batch is known. This one is only known when count is a number.
            known = type(count) is int and 0 <= count < len(events)
            self._release(sent + (events[:count] if known else events), known)
            return 'UNKNOWN'
        return 'SENT'

    def _release(self, events, prefix_known):
        releases = _releases(events, prefix_known)
        if not releases:
            return
        try:
            if self.desktop_is_default() and self._send_batch(releases) == len(releases):
                return
        except Exception:
            pass
        # Best effort only: try again before the next input. The outcome stays UNKNOWN.
        self._pending = releases

    def _flush_pending(self):
        pending = getattr(self, '_pending', None)
        if not pending:
            return True
        if not self.desktop_is_default() or self._send_batch(pending) != len(pending):
            return False
        self._pending = None
        return True

    def insert(self, events, sleep=time.sleep):
        try:
            if not self._flush_pending():
                return 'NOT_SENT'
        except Exception:
            return 'NOT_SENT'  # Only a release was tried; none of this operation's events went in.
        if isinstance(events, Paced):
            return self._insert_paced(events, sleep)
        try:
            count = self._send_batch(events)
        except Exception:
            count = None
        if count == len(events):
            return 'SENT'
        if count == 0:
            return 'NOT_SENT'
        prefix_known = type(count) is int and 0 < count < len(events)
        self._release(events[:count] if prefix_known else events, prefix_known)
        return 'UNKNOWN'
