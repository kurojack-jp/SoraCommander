"""Shared TLS, HTTP and narrow JSON contracts for the DEV VM screen trial."""

import base64
import hashlib
import io
import ipaddress
import json
import re
import ssl
import unicodedata
from urllib.parse import quote, unquote


VERSION = 5
HEADER_MAX = 8 * 1024
HEADER_COUNT = 32
PRE_HANDSHAKE_MAX = 16 * 1024
REQUEST_MAX = 16 * 1024
RESPONSE_MAX = 8 * 1024 * 1024
PNG_MAX = 4 * 1024 * 1024
FILE_MAX = 1 << 30
FILE_TOTAL_MAX = 4 << 30
FILE_FREE_MIN = 10 << 30
CHUNK_MAX = 4 << 20
TRANSFER_REQUEST_MAX = 8192
SIZE_MAX = 1 << 53
DRAG_MS_MAX = 2000
DRAG_MS = 300
KEY_FILE_MAX = 128
HANDSHAKE_SECONDS = 5
REQUEST_SECONDS = 15
IO_SLICE = 0.25
NON_INPUT_MAX = 600
CIPHER = 'ECDHE-PSK-CHACHA20-POLY1305'
PSK_ID = 'scn-vm-screen-1'

PLAIN_KEYS = (
    'Enter', 'Tab', 'Escape', 'Backspace', 'Delete', 'Insert', 'Left', 'Right',
    'Up', 'Down', 'Home', 'End', 'PageUp', 'PageDown', 'F1', 'F2', 'F3', 'F4',
    'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12', 'Space', 'IME', 'Win',
)
MODIFIERS = ('Ctrl', 'Shift', 'Alt', 'Win')
KEYS = frozenset(PLAIN_KEYS) | frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')
COORD_OPS = frozenset({'click', 'double_click', 'move', 'drag', 'scroll'})
INPUT_OPS = COORD_OPS | {'type', 'keypress'}
TOOLS = INPUT_OPS | {'screenshot', 'wait', 'elements', 'file_put', 'file_get'}
DEFAULT_TOOLS = 'screenshot,wait'
STATES = frozenset({'IN_PROGRESS', 'SENT', 'NOT_SENT', 'UNKNOWN', 'REFUSED', 'OK'})
RESULT_STATUS = {
    'OK': 200, 'SENT': 200, 'INVALID': 400, 'TOOL_DISABLED': 403,
    'NOT_FOUND': 404, 'METHOD_NOT_ALLOWED': 405, 'DUPLICATE_OP': 409,
    'BOOT_MISMATCH': 409, 'LENGTH_REQUIRED': 411, 'STALE_SCREEN': 412,
    'TOO_LARGE': 413, 'UNSUPPORTED_MEDIA_TYPE': 415, 'SCREEN_REQUIRED': 428,
    'LIMIT_REACHED': 429, 'NOT_DEFAULT_DESKTOP': 503, 'CAPTURE_FAILED': 503,
    'NOT_SENT': 503, 'UNKNOWN': 503, 'ELEMENTS_FAILED': 503,
    'CREATED': 201, 'FILE_EXISTS': 412, 'FILE_NOT_FOUND': 404, 'DIGEST_MISMATCH': 400,
    'FILE_FAILED': 503, 'PARTIAL_CONTENT': 206, 'TRANSFER_LIMIT': 429,
    'TRANSFER_BUSY': 409, 'OFFSET_MISMATCH': 409, 'FILE_CHANGED': 412,
    'FILE_TOO_LARGE': 413, 'NAME_NOT_ALLOWED': 400, 'RANGE_NOT_ALLOWED': 416,
    'DISK_FULL': 507, 'FILE_BUSY': 503, 'LENGTH_MISMATCH': 409,
}
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network((start, prefix)) for start, prefix in (
    (10 << 24, 8), (172 << 24 | 16 << 16, 12), (192 << 24 | 168 << 16, 16),
))


class ProtocolError(Exception):
    def __init__(self):
        super().__init__('protocol failure')


class TransportError(ProtocolError):
    pass


class WireObject(dict):
    def __repr__(self):
        return '<wire object>'


def require(condition):
    if not condition:
        raise ProtocolError()


def fields(obj, names):
    expected = set(names.split())
    require(isinstance(obj, dict) and len(obj) == len(expected) and obj.keys() == expected)


def integer(value, low, high):
    require(type(value) is int and low <= value <= high)


def hex_bytes(value, size):
    require(type(value) is str and len(value) == 2 * size
            and re.fullmatch('[0-9a-f]+', value) is not None)
    return bytes.fromhex(value)


def address(value):
    require(type(value) is str and 7 <= len(value) <= 15)
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        raise ProtocolError() from None
    require(any(ip in network and ip not in (network.network_address,
                network.broadcast_address) for network in PRIVATE_NETWORKS))
    return str(ip)


def check_bound_addresses(local, remote, interface_addresses):
    local_ip, remote_ip = ipaddress.IPv4Address(local), ipaddress.IPv4Address(remote)
    networks = [ipaddress.IPv4Network((local_ip, str(ipaddress.IPv4Address(mask))), strict=False)
                for ip, mask in interface_addresses if ip == int(local_ip)]
    require(networks)
    for network in networks:
        if network.prefixlen < 31:
            reserved = (network.network_address, network.broadcast_address)
            require(local_ip not in reserved and remote_ip not in reserved)


def decode_key(raw):
    require(len(raw) <= KEY_FILE_MAX and re.fullmatch(b'[0-9a-f]{64}(?:\r?\n)?', raw))
    return bytes.fromhex(raw[:64].decode('ascii'))


def vm_name(value):
    # Bounded for probe and safe for a single console line.
    require(type(value) is str and 1 <= len(value) <= 100 and value.isprintable())


def dimensions(width, height):
    integer(width, 640, 1920)
    integer(height, 480, 1200)


def limits(value):
    fields(value, 'seconds input_ops non_input_ops')
    integer(value['seconds'], 0, 1800)
    integer(value['input_ops'], 0, 200)
    integer(value['non_input_ops'], 0, NON_INPUT_MAX)


def _pairs(pairs):
    obj = WireObject(pairs)
    require(len(obj) == len(pairs))
    return obj


def _forbidden_number(_):
    raise ProtocolError()


def decode(raw, max_depth=2):
    # Bound nesting before the JSON decoder can recurse.
    quoted = escaped = False
    depth = 0
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
            require(depth <= max_depth)
        elif byte in (125, 93):
            depth -= 1
            require(depth >= 0)
    try:
        obj = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                         parse_constant=_forbidden_number, parse_float=_forbidden_number)
        require(isinstance(obj, WireObject))
        return obj
    except (ValueError, RecursionError):
        pass
    raise ProtocolError()


def encode(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':'), sort_keys=True).encode('utf-8')
    except (ValueError, TypeError):
        pass
    raise ProtocolError()


def tls_context(key, *, server_side):
    require(ssl.HAS_PSK and type(key) is bytes and len(key) == 32)
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server_side else ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers(CIPHER)
        context.options |= ssl.OP_NO_TICKET | ssl.OP_NO_COMPRESSION | ssl.OP_NO_RENEGOTIATION
        # A directly constructed SSLContext ignores SSLKEYLOGFILE. Keep logging off.
        context.keylog_filename = None
        if server_side:
            context.set_psk_server_callback(lambda identity: key if identity == PSK_ID else b'')
        else:
            # Safe only with TLS 1.2 and the pinned PSK cipher, which authenticates the peer.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.set_psk_client_callback(lambda _hint: (PSK_ID, key))
        return context
    except (ValueError, ssl.SSLError):
        pass
    raise TransportError()


def _check_negotiated(tls):
    require(tls.version() == 'TLSv1.2' and tls.cipher()[0] == CIPHER
            and not tls.session_reused)


class _TLSReader(io.RawIOBase):
    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    def readable(self):
        return True

    def readinto(self, buffer):
        return self.stream.readinto(buffer)


class TLSStream:
    """A blocking SSLObject over a raw stream with absolute monotonic deadlines."""

    def __init__(self, raw, context, *, server_side, deadline, clock):
        self.raw = raw
        self.clock = clock
        self.deadline = deadline
        self._handshake_bytes = 0
        self._incoming, self._outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self._tls = context.wrap_bio(self._incoming, self._outgoing, server_side=server_side)

    def check_time(self):
        if self.clock() >= self.deadline:
            raise TransportError()

    def _socket_call(self, call, value):
        while True:
            self.check_time()
            try:
                self.raw.settimeout(max(0, min(IO_SLICE, self.deadline - self.clock())))
                result = call(value)
            except TimeoutError:
                continue
            except OSError:
                break
            self.check_time()
            return result
        raise TransportError()

    def _flush(self):
        while self._outgoing.pending:
            pending = memoryview(self._outgoing.read())
            while pending:
                sent = self._socket_call(self.raw.send, pending)
                if not 0 < sent <= len(pending):
                    raise TransportError()
                pending = pending[sent:]

    def _receive(self):
        maximum = 8192
        if self._handshake_bytes is not None:
            maximum = min(maximum, PRE_HANDSHAKE_MAX - self._handshake_bytes)
            if maximum == 0:
                raise TransportError()
        data = self._socket_call(self.raw.recv, maximum)
        if not data or len(data) > maximum:
            raise TransportError()
        if self._handshake_bytes is not None:
            self._handshake_bytes += len(data)
        self._incoming.write(data)

    def _drive(self, call, *args):
        while True:
            self.check_time()
            try:
                result = call(*args)
            except ssl.SSLWantReadError:
                need_read = True
            except ssl.SSLWantWriteError:
                need_read = False
            except ssl.SSLError:
                break
            else:
                self._flush()
                self.check_time()
                return result
            self._flush()
            if need_read:
                self._receive()
        raise TransportError()

    def handshake(self):
        self._drive(self._tls.do_handshake)
        _check_negotiated(self._tls)
        self._handshake_bytes = None

    def readinto(self, buffer):
        return self._drive(self._tls.read, len(buffer), buffer)

    def sendall(self, data):
        pending = memoryview(data)
        while pending:
            written = self._drive(self._tls.write, pending[:16384])
            pending = pending[written:]

    def makefile(self, mode):
        require(mode == 'rb')
        return io.BufferedReader(_TLSReader(self))

    def finish(self):
        # Send close_notify. The one-shot server need not wait for the peer's alert.
        self.check_time()
        failed = False
        try:
            self._tls.unwrap()
        except ssl.SSLWantReadError:
            pass
        except ssl.SSLError:
            failed = True
        # OpenSSL can generate our alert before reporting unread peer application data.
        self._flush()
        if failed:
            raise TransportError()

    def close(self):
        # The caller that opened or accepted the raw stream owns it.
        pass


class HTTPError(ProtocolError):
    def __init__(self, result):
        super().__init__()
        self.result = result


_HEADER = re.compile(rb"([!#$%&'*+.^_`|~0-9A-Za-z-]+): *([ -~]*)\r\n")


def read_head(reader):
    """Bound and validate a complete header block before giving it to either parser."""
    first = reader.readline(HEADER_MAX + 1)
    lines = [first]
    headers = {}
    remaining = HEADER_MAX - len(first)
    require(first.endswith(b'\r\n') and all(32 <= byte <= 126 for byte in first[:-2]))
    while remaining >= 2:
        line = reader.readline(remaining + 1)
        remaining -= len(line)
        require(remaining >= 0)
        lines.append(line)
        if line == b'\r\n':
            return b''.join(lines), headers
        match = _HEADER.fullmatch(line)
        require(match is not None and len(headers) < HEADER_COUNT)
        name = match[1].decode('ascii').lower()
        require(name not in headers)
        headers[name] = match[2].decode('ascii').rstrip(' ')
    raise ProtocolError()


def content_length(value, maximum):
    if value is None:
        raise HTTPError('LENGTH_REQUIRED')
    require(re.fullmatch(r'[0-9]+', value) is not None)
    digits = value.lstrip('0') or '0'
    if len(digits) > len(str(maximum)) or int(digits) > maximum:
        raise HTTPError('TOO_LARGE')
    # Keep the value supplied to http.client within the interpreter's integer limit.
    require(len(value) <= len(str(maximum)))
    return int(digits)


def selected_tools(value):
    require(type(value) is str and len(value) <= sum(map(len, TOOLS)) + len(TOOLS) - 1)
    items = value.split(',')
    names = frozenset(items)
    require('screenshot' in names and names <= TOOLS and len(names) == len(items))
    return names


def screen_tag(boot_id, sequence):
    return f'"{boot_id}-{sequence}"'


def strong_tag(value):
    # RFC 9110 etagc, restricted to the visible ASCII supported by read_head.
    require(type(value) is str and re.fullmatch(r'"[!#-~]*"', value) is not None)


def screenshot_boot(tag):
    require(type(tag) is str and re.fullmatch(r'"[0-9a-f]{32}-[1-9][0-9]{0,18}"', tag) is not None)
    return tag[1:33]


_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    kind + digit for kind in ('COM', 'LPT') for digit in '123456789¹²³'
}
_NAME_CATEGORIES = {'Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp'}


def is_file_name(value):
    return (type(value) is str and 1 <= len(value) <= 120
            and not value.startswith('.') and not value.endswith('.') and value == value.strip()
            and all(char not in '<>:"/\\|?*' and unicodedata.category(char) not in _NAME_CATEGORIES
                    for char in value)
            and len(value.encode('utf-16-le')) <= 240 and unicodedata.is_normalized('NFC', value)
            and value.split('.', 1)[0].rstrip(' ').upper() not in _RESERVED)


def file_name(value):
    require(is_file_name(value))


def file_path(name):
    if not is_file_name(name):
        raise HTTPError('NAME_NOT_ALLOWED')
    return '/files/' + quote(name, safe='')


def path_name(path):
    try:
        name = unquote(path[len('/files/'):], errors='strict')
    except UnicodeError:
        raise HTTPError('NAME_NOT_ALLOWED') from None
    if file_path(name) != path:
        raise HTTPError('NAME_NOT_ALLOWED')
    return name


def byte_range(value):
    if value is not None:
        match = re.fullmatch(r'bytes=([0-9]{1,10})-([0-9]{1,10})', value)
        if match:
            start, end = map(int, match.groups())
            if 0 <= start <= FILE_MAX and 1 <= end - start + 1 <= CHUNK_MAX:
                return start, end
    raise HTTPError('RANGE_NOT_ALLOWED')


def digest(data):
    return 'sha-256=:' + base64.b64encode(hashlib.sha256(data).digest()).decode('ascii') + ':'


def validate_text(value):
    require(type(value) is str and 1 <= len(value) <= 1000)
    require(all(char in '\n\t' or unicodedata.category(char) not in {'Cc', 'Cs', 'Zl', 'Zp', 'Cn', 'Co'}
                for char in value))



_KEY_NAMES = {name.upper(): name for name in KEYS | set(MODIFIERS)}
_KEY_NAMES.update({
    'RETURN': 'Enter', 'ESC': 'Escape', 'DEL': 'Delete', 'ARROWUP': 'Up',
    'ARROWDOWN': 'Down', 'ARROWLEFT': 'Left', 'ARROWRIGHT': 'Right',
    'CONTROL': 'Ctrl', 'OPTION': 'Alt', 'META': 'Win', 'CMD': 'Win', 'COMMAND': 'Win',
})


def key_combo(keys):
    require(type(keys) is list and 1 <= len(keys) <= 4)
    names = set()
    for key in keys:
        require(type(key) is str and 1 <= len(key) <= 10 and key.isascii())
        canonical = _KEY_NAMES.get(key.upper())
        require(canonical is not None and canonical not in names)
        names.add(canonical)
    plain = names.difference(MODIFIERS)
    if not plain and 'Win' in names:
        plain.add('Win')
    require(len(plain) == 1 and not {'Win', 'L'} <= names and not {'Ctrl', 'Alt', 'Delete'} <= names)
    modifiers = names - plain
    return (*(modifier for modifier in MODIFIERS if modifier in modifiers), plain.pop())


_ACTION_FIELDS = {
    'click': 'x y button', 'double_click': 'x y', 'move': 'x y', 'drag': 'path duration_ms',
    'scroll': 'x y scroll_x scroll_y', 'type': 'text', 'keypress': 'keys', 'wait': 'ms',
}


def points(action):
    return action['path'] if action['type'] == 'drag' else (action,)


def validate_action(obj):
    require(isinstance(obj, dict) and 1 <= len(obj) <= 5)
    op = obj.get('type')
    require(type(op) is str and op in _ACTION_FIELDS)
    optional = {'wait': 'ms', 'drag': 'duration_ms'}.get(op)
    names = _ACTION_FIELDS[op].split()
    fields(obj, ' '.join(['type', *(name for name in names if name != optional or name in obj)]))
    if op == 'drag':
        require(type(obj['path']) is list and 2 <= len(obj['path']) <= 20)
        for point in obj['path']:
            fields(point, 'x y')
        integer(obj.get('duration_ms', DRAG_MS), 0, DRAG_MS_MAX)
    if op in COORD_OPS:
        for point in points(obj):
            for axis in ('x', 'y'):
                integer(point[axis], -(1 << 31), (1 << 31) - 1)
    if op == 'click':
        require(type(obj['button']) is str and obj['button'] in ('left', 'right', 'middle'))
    elif op == 'scroll':
        for axis in ('scroll_x', 'scroll_y'):
            integer(obj[axis], -(1 << 31), (1 << 31) - 1)
        require(obj['scroll_x'] != 0 or obj['scroll_y'] != 0)
    elif op == 'type':
        validate_text(obj['text'])
    elif op == 'keypress':
        key_combo(obj['keys'])
    elif op == 'wait':
        integer(obj.get('ms', 1000), 1, 10000)
    return op


def notches(pixels):
    if pixels == 0:
        return 0
    magnitude = min(20, max(1, (abs(pixels) + 50) // 100))
    return magnitude if pixels > 0 else -magnitude


def _elements(obj):
    require(type(obj['window']) is str and len(obj['window']) <= 80 and type(obj['truncated']) is bool)
    require(type(obj['elements']) is list and len(obj['elements']) <= 150)
    for item in obj['elements']:
        fields(item, 'type name x y width height enabled focused password')
        require(type(item['type']) is str and re.fullmatch('[a-z]{1,16}', item['type']) is not None)
        require(type(item['name']) is str and len(item['name']) <= 80)
        for name in ('x', 'y', 'width', 'height'):
            integer(item[name], 1 if name in ('width', 'height') else 0, 1920)
        require(item['x'] + item['width'] <= 1920 and item['y'] + item['height'] <= 1200)
        require(all(type(item[name]) is bool for name in ('enabled', 'focused', 'password')))


def _listing(obj):
    require(type(obj['truncated']) is bool)
    for folder in ('inbox', 'outbox'):
        require(type(obj[folder]) is list and len(obj[folder]) <= 100)
        for item in obj[folder]:
            fields(item, 'name size')
            file_name(item['name'])
            integer(item['size'], 0, SIZE_MAX)


def validate_response(obj, status, operation):
    result = obj.get('result')
    require(type(result) is str and RESULT_STATUS.get(result) == status)
    extra = ''
    if result == 'OK':
        require(operation in ('probe', 'status', 'wait', 'elements', 'files',
                              'upload_status', 'upload_patch', 'upload_delete'))
        if operation == 'probe':
            extra = 'version boot_id vm_name width height tools'
        elif operation == 'elements':
            extra = 'window elements truncated'
        elif operation == 'files':
            extra = 'inbox outbox truncated'
        elif operation == 'status':
            extra = 'state'
        elif operation in ('upload_status', 'upload_patch'):
            extra = 'offset length complete'
    elif result == 'DUPLICATE_OP':
        require(operation in INPUT_OPS or operation in ('wait', 'upload_create'))
        if operation != 'upload_create':
            extra = 'state'
    elif result in ('SENT', 'NOT_SENT', 'UNKNOWN'):
        require(operation in INPUT_OPS)
    elif result == 'CREATED':
        require(operation == 'upload_create')
        extra = 'id offset'
    elif result == 'OFFSET_MISMATCH':
        require(operation == 'upload_patch')
        extra = 'offset'
    elif result == 'PARTIAL_CONTENT':
        raise ProtocolError()
    elif result in _FILE_REFUSALS:
        require(operation in _FILE_REFUSALS[result])
    elif result == 'ELEMENTS_FAILED':
        require(operation == 'elements')
    fields(obj, f'result remaining {extra}')
    limits(obj['remaining'])
    if 'id' in obj:
        hex_bytes(obj['id'], 16)
        integer(obj['offset'], 0, 0)
    if 'offset' in obj:
        integer(obj['offset'], 0, FILE_MAX)
    if 'length' in obj:
        integer(obj['length'], obj['offset'], FILE_MAX)
        require(type(obj['complete']) is bool and (not obj['complete'] or obj['offset'] == obj['length']))
    if 'state' in obj:
        require(type(obj['state']) is str and obj['state'] in STATES)
    if 'elements' in obj:
        _elements(obj)
    if 'inbox' in obj:
        _listing(obj)
    if 'version' in obj:
        integer(obj['version'], VERSION, VERSION)
        hex_bytes(obj['boot_id'], 16)
        vm_name(obj['vm_name'])
        dimensions(obj['width'], obj['height'])
        selected_tools(obj['tools'])


_FILE_REFUSALS = {
    'FILE_EXISTS': ('upload_create', 'upload_patch'),
    'FILE_NOT_FOUND': ('get',),
    'FILE_TOO_LARGE': ('get', 'upload_create'),
    'FILE_FAILED': ('get', 'files', 'upload_create', 'upload_status', 'upload_patch', 'upload_delete'),
    'DIGEST_MISMATCH': ('upload_patch',),
    'TRANSFER_LIMIT': ('get', 'files', 'upload_create', 'upload_status', 'upload_patch', 'upload_delete'),
    'TRANSFER_BUSY': ('upload_create',),
    'FILE_BUSY': ('get',),
    'FILE_CHANGED': ('get',),
    'NAME_NOT_ALLOWED': ('get', 'upload_create'),
    'DISK_FULL': ('upload_create',),
    'LENGTH_MISMATCH': ('upload_patch',),
    'RANGE_NOT_ALLOWED': ('get',),
}
