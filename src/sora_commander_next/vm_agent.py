"""Single-peer, single-process screen trial agent for an Owner-named Hyper-V guest."""

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler
import io
import math
import re
import secrets
import socket
import sys
import time

if __package__:
    from . import vm_files, vm_protocol as p, vm_uia, vm_win as win
else:
    import vm_files
    import vm_protocol as p
    import vm_uia
    import vm_win as win


BIOS = r'HARDWARE\DESCRIPTION\System\BIOS'
GUEST = r'SOFTWARE\Microsoft\Virtual Machine\Guest\Parameters'
MAX_FAILED_HANDSHAKES = 20
MAX_SCREEN_AGE = 120
MAX_RECORDED_OPS = 8192
FINAL_WRITE_SECONDS = 1
DRAG_MARGIN_SECONDS = 1


class StartupRefused(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'Invalid arguments. See --help.\n')


def parser():
    args = Parser(description='DEV screen agent for an Owner-named Hyper-V guest.')
    args.add_argument('--vm-name', required=True)
    args.add_argument('--bind', required=True)
    args.add_argument('--allow-peer', required=True)
    args.add_argument('--port', required=True, type=int)
    args.add_argument('--key-file', required=True)
    args.add_argument('--max-minutes', type=int, default=30)
    args.add_argument('--max-input-ops', type=int, default=200)
    args.add_argument('--tools', default=p.DEFAULT_TOOLS)
    args.add_argument('--files-dir', help='Absolute folder with inbox and outbox. Needed by file_put and file_get.')
    return args


def _gate(reason, check):
    try:
        if check():
            return
    except Exception:
        pass
    raise StartupRefused(reason)


def _network(args):
    p.vm_name(args.vm_name)
    p.selected_tools(args.tools)
    bind, peer = p.address(args.bind), p.address(args.allow_peer)
    p.require(bind != peer)
    p.integer(args.port, 1024, 65535)
    p.integer(args.max_minutes, 1, 30)
    p.integer(args.max_input_ops, 1, 200)
    return bind, peer


def _guest(reader, expected):
    for key, name, wanted in ((BIOS, 'SystemManufacturer', 'Microsoft Corporation'),
                              (BIOS, 'SystemProductName', 'Virtual Machine'),
                              (GUEST, 'VirtualMachineName', expected)):
        actual = reader(key, name)
        if type(actual) is not str or actual != wanted:
            return False
    return True


def _screen(backend):
    width, height = backend.screen_size()
    p.dimensions(width, height)
    return width, height


def startup(args, platform, backend_factory, registry_reader, key_reader, interface_reader):
    _gate('TLS_PSK_REQUIRED', lambda: p.ssl.HAS_PSK)
    _gate('WINDOWS_REQUIRED', lambda: platform == 'win32')
    try:
        bind, peer = _network(args)
    except p.ProtocolError:
        raise StartupRefused('INVALID_CONFIGURATION') from None
    _gate('HYPER_V_GUEST_REQUIRED', lambda: _guest(registry_reader, args.vm_name))
    try:
        key = p.decode_key(key_reader(args.key_file, p.KEY_FILE_MAX))
    except Exception:
        key = None
    if key is None:
        raise StartupRefused('INVALID_KEY_FILE')
    try:
        backend = backend_factory()
    except Exception:
        raise StartupRefused('WINDOWS_API_UNAVAILABLE') from None
    _gate('STANDARD_USER_REQUIRED', lambda: backend.standard_user() is True)
    _gate('DPI_AWARENESS_REQUIRED', lambda: backend.set_dpi() is True)
    _gate('INTERACTIVE_SESSION_REQUIRED', lambda: type(session := backend.session_id()) is int
          and session > 0)
    _gate('NOT_DEFAULT_DESKTOP', lambda: backend.desktop_is_default() is True)
    try:
        size = _screen(backend)
    except Exception:
        raise StartupRefused('INVALID_SCREEN') from None
    try:
        p.check_bound_addresses(bind, peer, interface_reader())
    except Exception:
        raise StartupRefused('INVALID_NETWORK_INTERFACE') from None
    return bind, peer, key, backend, size


_COMMON_HEADERS = {'host', 'connection'}
_TRANSFER_HEADERS = _COMMON_HEADERS | {'scn-boot-id'}
_UPLOAD_HEADERS = _TRANSFER_HEADERS | {'content-type', 'content-length'}
_CREATE_HEADERS = _UPLOAD_HEADERS | {'idempotency-key'}
_PATCH_HEADERS = _UPLOAD_HEADERS | {'upload-offset', 'content-digest', 'upload-complete'}
_ACTION_HEADERS = {'content-type', 'content-length', 'idempotency-key', 'scn-boot-id', 'if-match'}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def __init__(self, stream, agent, output):
        # One prevalidated request. The stdlib parser never sees an unbounded stream.
        self.stream = stream
        self.agent = agent
        self.output = output
        agent.current_op = 'request'
        self.handle_one_request()

    def log_message(self, *_):
        pass

    def send_response(self, code, message=None):
        self.send_response_only(code)

    def flush_headers(self):
        self.stream.sendall(b''.join(self._headers_buffer))
        self._headers_buffer.clear()

    def send_error(self, *_args, **_kwargs):
        self.reply('INVALID')

    def reply(self, result, data=None, png=None):
        status = p.RESULT_STATUS[result]
        remaining = self.agent.remaining()
        headers = {'Connection': 'close'}
        if png is None:
            headers['Content-Type'] = 'application/json'
            body = p.encode(dict(result=result, remaining=remaining, **(data or {})))
        elif result == 'PARTIAL_CONTENT':
            headers.update(data)
            headers.update({'Content-Type': 'application/octet-stream', 'Cache-Control': 'no-store',
                            'Content-Digest': p.digest(png)})
            body = png
        else:
            headers.update({
                'Content-Type': 'image/png', 'Cache-Control': 'no-store',
                'ETag': self.agent.screen[0], 'Content-Digest': p.digest(png),
                'SCN-Width': str(self.agent.size[0]), 'SCN-Height': str(self.agent.size[1]),
                'SCN-Seconds': str(remaining['seconds']),
                'SCN-Input-Ops': str(remaining['input_ops']),
                'SCN-Non-Input-Ops': str(remaining['non_input_ops']),
            })
            body = png
        headers['Content-Length'] = str(len(body))
        self.request_version = 'HTTP/1.1'
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.stream.sendall(body)

    def handle_one_request(self):
        with self.stream.makefile('rb') as reader:
            try:
                head, headers = p.read_head(reader)
                p.require('expect' not in headers and 'transfer-encoding' not in headers)
                request_line = head.split(b'\r\n', 1)[0].split(b' ')
                p.require(len(request_line) == 3 and request_line[2] == b'HTTP/1.1'
                          and 1 <= len(request_line[0]) <= 32
                          and all(65 <= byte <= 90 for byte in request_line[0])
                          and request_line[1].startswith(b'/')
                          and not request_line[1].startswith(b'//')
                          and b'?' not in request_line[1] and b'#' not in request_line[1])
                self.rfile = io.BytesIO(head)
                self.raw_requestline = self.rfile.readline()
                if not self.parse_request():
                    return
                response = self.dispatch(reader, headers)
            except p.TransportError:
                raise
            except p.HTTPError as exc:
                response = (exc.result,)
            except p.ProtocolError:
                response = ('INVALID',)
            self.agent.event(self.output, self.agent.current_op, response[0])
            if response[0] == 'LIMIT_REACHED' and self.agent.expired():
                self.stream.deadline = self.stream.response_deadline
            self.reply(*response)

    def dispatch(self, reader, headers):
        self.stream.check_time()
        p.require(headers.get('host') == self.agent.host and headers.get('connection') == 'close')
        if self.path in ('/probe', '/screenshot', '/action', '/elements', '/files'):
            route = self.path[1:]
        elif self.path.startswith('/action/'):
            target = self.path[len('/action/'):]
            p.hex_bytes(target, 16)
            route = 'status'
        elif self.path.startswith('/files/'):
            route = 'file_get'
        elif self.path == '/uploads':
            route = 'upload_create'
        elif self.path.startswith('/uploads/'):
            route = {'GET': 'upload_status', 'PATCH': 'upload_patch',
                     'DELETE': 'upload_delete'}.get(self.command, 'upload_status')
        else:
            return ('NOT_FOUND',)
        if self.command != {'action': 'POST', 'upload_create': 'POST',
                            'upload_patch': 'PATCH', 'upload_delete': 'DELETE'}.get(route, 'GET'):
            return ('METHOD_NOT_ALLOWED',)
        self.agent.current_op = route
        if route == 'action':
            p.require(headers.keys() <= _COMMON_HEADERS | _ACTION_HEADERS)
            length = p.content_length(headers.get('content-length'), p.REQUEST_MAX)
            if headers.get('content-type') != 'application/json':
                return ('UNSUPPORTED_MEDIA_TYPE',)
            op_id, boot_id = headers.get('idempotency-key'), headers.get('scn-boot-id')
            p.hex_bytes(op_id, 16)
            p.hex_bytes(boot_id, 16)
            raw = reader.read(length)
            if len(raw) != length:
                raise p.TransportError()
            return self.agent.action(op_id, boot_id, headers.get('if-match'), raw, self.stream)
        if route.startswith('upload_') or route in ('files', 'file_get'):
            return self.transfer(route, reader, headers)
        if route == 'status':
            p.require(headers.keys() == _COMMON_HEADERS | {'scn-boot-id'})
            p.hex_bytes(headers['scn-boot-id'], 16)
            if headers['scn-boot-id'] != self.agent.boot_id:
                return ('BOOT_MISMATCH',)
        else:
            p.require(headers.keys() == _COMMON_HEADERS)
        if route == 'elements' and 'elements' not in self.agent.tools:
            return ('TOOL_DISABLED',)
        if not self.agent.take_non_input():
            return ('LIMIT_REACHED',)
        if route == 'elements':
            return self.agent.elements()
        if route == 'probe':
            return self.agent.probe()
        if route == 'screenshot':
            return self.agent.capture()
        return self.agent.status(target)


    def transfer(self, route, reader, headers):
        available = self.agent.take_transfer()
        if route in ('upload_create', 'upload_patch'):
            # Drain any bounded body before refusing, including a bad header or exhausted cap.
            maximum = p.CHUNK_MAX if route == 'upload_patch' else p.REQUEST_MAX
            length = p.content_length(headers.get('content-length'), maximum)
            raw = reader.read(length)
            if len(raw) != length:
                raise p.TransportError()
        if not available:
            return ('LIMIT_REACHED' if self.agent.expired() else 'TRANSFER_LIMIT',)
        # Every transfer request names the agent run it was meant for, before any file is looked at.
        boot_id = headers.get('scn-boot-id')
        p.hex_bytes(boot_id, 16)
        if boot_id != self.agent.boot_id:
            return ('BOOT_MISMATCH',)
        if route == 'upload_create':
            p.require(headers.keys() == _CREATE_HEADERS)
            if 'file_put' not in self.agent.tools:
                return ('TOOL_DISABLED',)
            key = headers['idempotency-key']
            p.hex_bytes(key, 16)
            if key in self.agent.upload_keys:
                return ('DUPLICATE_OP',)
            self.agent.upload_keys.add(key)
            if headers['content-type'] != 'application/json':
                return ('UNSUPPORTED_MEDIA_TYPE',)
            self.stream.check_time()
            return self.agent.upload_create(p.decode(raw))
        if route == 'files':
            p.require(headers.keys() == _TRANSFER_HEADERS)
            if not self.agent.tools.intersection(('file_put', 'file_get')):
                return ('TOOL_DISABLED',)
            return self.agent.file_list()
        if route == 'file_get':
            if 'file_get' not in self.agent.tools:
                return ('TOOL_DISABLED',)
            p.require(headers.keys() <= _TRANSFER_HEADERS | {'range', 'if-match'})
            name = p.path_name(self.path)
            start, end = p.byte_range(headers.get('range'))
            match = headers.get('if-match')
            if match is not None:
                p.strong_tag(match)
            elif start:
                return ('FILE_CHANGED',)
            return self.agent.file_get(name, start, end, match)
        if 'file_put' not in self.agent.tools:
            return ('TOOL_DISABLED',)
        target = self.path[len('/uploads/'):]
        p.hex_bytes(target, 16)
        if route != 'upload_patch':
            p.require(headers.keys() == _TRANSFER_HEADERS)
            if route == 'upload_status':
                return self.agent.upload_status(target)
            return self.agent.upload_delete(target)
        p.require(headers.keys() == _PATCH_HEADERS)
        if headers['content-type'] != 'application/partial-upload':
            return ('UNSUPPORTED_MEDIA_TYPE',)
        offset = headers['upload-offset']
        complete = headers['upload-complete']
        p.require(re.fullmatch(r'[0-9]{1,10}', offset) and complete in ('?0', '?1'))
        return self.agent.upload_patch(target, int(offset), raw, headers['content-digest'],
                                       complete == '?1', self.stream.check_time)


class Agent:
    def __init__(self, args, key, backend, size, *, files=None, clock=time.monotonic,
                 sleep=time.sleep, random_bytes=secrets.token_bytes):
        self.vm_name = args.vm_name
        self.tools = p.selected_tools(args.tools)
        self.tools_arg = args.tools
        self.files = files
        self.file_bytes = p.FILE_TOTAL_MAX
        self.transfer_requests = p.TRANSFER_REQUEST_MAX
        self.upload = None
        self.upload_keys = set()
        self.completed_uploads = {}
        self.host = f'{args.bind}:{args.port}'
        self._key = key
        self._first_context = p.tls_context(key, server_side=True)
        self.backend = backend
        self.size = size
        self.clock = clock
        self.sleep = sleep
        self.boot_id = random_bytes(16).hex()
        self.deadline = clock() + args.max_minutes * 60
        self.input_ops = args.max_input_ops
        self.non_input_ops = p.NON_INPUT_MAX
        self.failed_handshakes = 0
        self.stopping = False
        self.journal = {}
        self.screen_seq = 0
        self.screen = None
        self.current_op = 'request'

    def expired(self):
        return self.clock() >= self.deadline

    def remaining(self):
        return dict(seconds=max(0, math.ceil(self.deadline - self.clock())),
                    input_ops=self.input_ops, non_input_ops=self.non_input_ops)

    def probe(self):
        return ('OK', dict(version=p.VERSION, boot_id=self.boot_id, vm_name=self.vm_name,
                           width=self.size[0], height=self.size[1], tools=self.tools_arg))

    def status(self, target):
        if target in self.journal:
            return ('OK', dict(state=self.journal[target]))
        return ('NOT_FOUND',)

    def take_non_input(self):
        if self.expired() or self.stopping or not self.non_input_ops:
            self.stopping = True
            return False
        self.non_input_ops -= 1
        return True

    def _file(self, call, *args):
        if self.files is None:
            return None, ('FILE_FAILED',)
        try:
            return call(*args), None
        except vm_files.StoreError as exc:
            return None, (exc.result,)
        except Exception:
            return None, ('FILE_FAILED',)

    def take_transfer(self):
        if self.expired() or self.stopping or not self.transfer_requests:
            return False
        self.transfer_requests -= 1
        return True

    def upload_create(self, request):
        p.fields(request, 'name length sha256')
        name, length = request['name'], request['length']
        if not p.is_file_name(name):
            return ('NAME_NOT_ALLOWED',)
        if type(length) is not int or length < 0:
            return ('INVALID',)
        if length > p.FILE_MAX:
            return ('FILE_TOO_LARGE',)
        if length > self.file_bytes:
            return ('TRANSFER_LIMIT',)
        digest = p.hex_bytes(request['sha256'], 32)
        if self.upload is not None:
            return ('TRANSFER_BUSY',)
        upload, refused = self._file(lambda: self.files.create(name, length, digest))
        if refused:
            return refused
        self.upload = upload
        return ('CREATED', dict(id=upload.id, offset=0))

    def upload_status(self, target):
        if self.upload is not None and self.upload.id == target:
            return ('OK', self.upload.status())
        if target in self.completed_uploads:
            return ('OK', self.completed_uploads[target])
        return ('NOT_FOUND',)

    def upload_delete(self, target):
        if self.upload is None or self.upload.id != target:
            return ('NOT_FOUND',)
        _, refused = self._file(lambda: self.files.discard(self.upload))
        self.upload = None
        return refused or ('OK',)

    def upload_patch(self, target, offset, raw, digest, complete, check_time):
        upload = self.upload
        if upload is None or upload.id != target:
            return ('NOT_FOUND',)
        if offset != upload.offset:
            return ('OFFSET_MISMATCH', dict(offset=upload.offset))
        if digest != p.digest(raw):
            return ('DIGEST_MISMATCH',)
        stop = offset + len(raw)
        if stop > upload.length or complete != (stop == upload.length) or (not raw and upload.length):
            return ('LENGTH_MISMATCH',)
        if len(raw) > self.file_bytes:
            return ('TRANSFER_LIMIT',)
        check_time()
        _, refused = self._file(lambda: self.files.append(upload, raw))
        if refused:
            self.upload_delete(target)
            return ('FILE_FAILED',)
        self.file_bytes -= len(raw)
        if not complete:
            return ('OK', upload.status())
        if upload.digest.digest() != upload.expected_digest:
            discarded = self.upload_delete(target)
            return ('DIGEST_MISMATCH',) if discarded[0] == 'OK' else discarded
        _, refused = self._file(lambda: self.files.finish(upload, check_time))
        if refused:
            self.upload_delete(target)
            return refused
        status = upload.status(complete=True)
        self.completed_uploads[target] = status
        self.upload = None
        return ('OK', status)

    def file_get(self, name, start, end, match):
        payload, refused = self._file(lambda: self.files.read_range(
            name, start, end, match, self.boot_id, self.file_bytes))
        if refused:
            return refused
        data, headers = payload
        self.file_bytes -= len(data)
        return ('PARTIAL_CONTENT', headers, data)

    def close_files(self):
        if self.files is not None:
            if self.upload is not None:
                self.upload_delete(self.upload.id)
            _, refused = self._file(self.files.cleanup)
            return refused

    def file_list(self):
        listing, refused = self._file(lambda: self.files.listing())
        return refused or ('OK', listing)

    def elements(self):
        if self._desktop_lost():
            return ('NOT_DEFAULT_DESKTOP',)
        try:
            found = self.backend.elements(_screen(self.backend))
        except Exception:
            return ('ELEMENTS_FAILED',)
        # The list can take seconds. Never return one that a lock or a UAC prompt interrupted.
        if self._desktop_lost():
            return ('NOT_DEFAULT_DESKTOP',)
        return ('OK', found)

    def _desktop(self):
        try:
            return self.backend.desktop_is_default() is True
        except Exception:
            return False

    def _desktop_lost(self):
        # A lock or a UAC prompt may have changed the screen, so an older screenshot is void.
        if self._desktop():
            return False
        self.screen = None
        return True

    def _input(self, op_id, req, tag, stream):
        if self._desktop_lost():
            self.journal[op_id] = 'NOT_SENT'
            return 'NOT_DEFAULT_DESKTOP'
        size = self.size
        screen = self.screen
        if req['type'] in p.COORD_OPS:
            if screen is None or screen[0] != tag or self.clock() - screen[2] >= MAX_SCREEN_AGE:
                return 'STALE_SCREEN'
            size = screen[1]
            if not all(0 <= point['x'] < size[0] and 0 <= point['y'] < size[1]
                       for point in p.points(req)):
                return 'INVALID'
        try:
            events = self.backend.prepare_input(req, size)
        except Exception:
            self.journal[op_id] = 'NOT_SENT'
            return 'NOT_SENT'
        if req['type'] in p.COORD_OPS:
            try:
                current = _screen(self.backend)
            except Exception:
                current = None
            if current != size or self.clock() - screen[2] >= MAX_SCREEN_AGE:
                return 'STALE_SCREEN'
        if self._desktop_lost():
            self.journal[op_id] = 'NOT_SENT'
            return 'NOT_DEFAULT_DESKTOP'
        if self.expired() or self.input_ops == 0:
            self.stopping = True
            return 'LIMIT_REACHED'
        if isinstance(events, win.Paced):
            # Never start a drag that cannot finish inside the request and the agent lifetime.
            end = self.clock() + events.seconds() + DRAG_MARGIN_SECONDS
            if end >= self.deadline:
                self.stopping = True
                return 'LIMIT_REACHED'
            if end >= stream.deadline:
                raise p.TransportError()
        stream.check_time()
        self.journal[op_id] = 'IN_PROGRESS'
        self.input_ops -= 1
        self.screen = None
        result = 'UNKNOWN'
        try:
            inserted = self.backend.insert(events)
            if type(inserted) is str and inserted in ('SENT', 'NOT_SENT', 'UNKNOWN'):
                result = inserted
        except Exception:
            pass
        finally:
            self.journal[op_id] = result
        return result

    def capture(self):
        if self._desktop_lost():
            return ('NOT_DEFAULT_DESKTOP',)
        try:
            size = _screen(self.backend)
            if self.expired():
                self.stopping = True
                return ('LIMIT_REACHED',)
            png = self.backend.capture(size)
            if self._desktop_lost():
                return ('NOT_DEFAULT_DESKTOP',)
            if _screen(self.backend) != size or type(png) is not bytes:
                return ('CAPTURE_FAILED',)
        except Exception:
            return ('CAPTURE_FAILED',)
        if self.expired():
            self.stopping = True
            return ('LIMIT_REACHED',)
        if len(png) > p.PNG_MAX:
            return ('TOO_LARGE',)
        self.size = size
        self.screen_seq += 1
        self.screen = (p.screen_tag(self.boot_id, self.screen_seq), size, self.clock())
        return ('OK', None, png)

    def _wait(self, op_id, milliseconds, stream):
        end = min(self.deadline, stream.deadline, self.clock() + milliseconds / 1000)
        while (remaining := end - self.clock()) > 0:
            self.sleep(min(remaining, p.IO_SLICE))
        if self.expired():
            self.stopping = True
            return 'LIMIT_REACHED'
        stream.check_time()
        self.journal[op_id] = 'OK'
        return 'OK'

    def action(self, op_id, boot_id, tag, raw, stream):
        if op_id in self.journal:
            return ('DUPLICATE_OP', dict(state=self.journal[op_id]))
        if len(self.journal) >= MAX_RECORDED_OPS:
            self.stopping = True
            return ('LIMIT_REACHED',)
        self.journal[op_id] = 'REFUSED'
        if self.expired() or self.stopping:
            self.stopping = True
            return ('LIMIT_REACHED',)
        if boot_id != self.boot_id:
            return ('BOOT_MISMATCH',)
        req = p.decode(raw, max_depth=4)
        op = self.current_op = p.validate_action(req)  # A fixed name, never request text.
        if op in p.COORD_OPS:
            if tag is None:
                return ('SCREEN_REQUIRED',)
            p.strong_tag(tag)
        else:
            p.require(tag is None)
        if op not in self.tools:
            return ('TOOL_DISABLED',)
        if op in p.INPUT_OPS:
            return (self._input(op_id, req, tag, stream),)
        if not self.take_non_input():
            return ('LIMIT_REACHED',)
        return (self._wait(op_id, req.get('ms', 1000), stream),)

    def event(self, output, op, result):
        remaining = self.remaining()
        output(f'{datetime.now().isoformat(timespec="seconds")} {op} {result} '
               f'seconds={remaining["seconds"]} input_ops={remaining["input_ops"]} '
               f'non_input_ops={remaining["non_input_ops"]}')

    def connection(self, stream, output):
        try:
            # OpenSSL can resume by TLS 1.2 session ID even with tickets disabled.
            # Use a fresh server context per connection to give it no previous session cache.
            context = self._first_context
            self._first_context = None
            if context is None:
                context = p.tls_context(self._key, server_side=True)
            tls = p.TLSStream(stream, context, server_side=True, clock=self.clock,
                              deadline=min(self.deadline, self.clock() + p.HANDSHAKE_SECONDS))
            tls.handshake()
        except p.ProtocolError:
            if not self.expired():
                self.failed_handshakes += 1
                if self.failed_handshakes >= MAX_FAILED_HANDSHAKES:
                    self.stopping = True
                self.event(output, 'handshake', 'REFUSED')
            return
        self.event(output, 'handshake', 'OK')
        try:
            request_deadline = self.clock() + p.REQUEST_SECONDS
            tls.deadline = min(self.deadline, request_deadline)
            # A final LIMIT_REACHED response may use the lifetime grace, within the request cap.
            tls.response_deadline = min(request_deadline, self.deadline + FINAL_WRITE_SECONDS)
            Handler(tls, self, output)
            tls.finish()
        except p.ProtocolError:
            self.event(output, 'connection', 'CLOSED')


def make_listener(bind, port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((bind, port))
        listener.listen(1)
        return listener
    except BaseException:
        listener.close()
        raise


def serve(agent, listener, peer, output):
    while not agent.stopping and not agent.expired():
        listener.settimeout(max(0, min(p.IO_SLICE, agent.deadline - agent.clock())))
        try:
            stream, remote = listener.accept()
        except TimeoutError:
            continue
        with stream:
            if remote[0] != peer:
                agent.event(output, 'peer', 'REFUSED')
                continue
            agent.connection(stream, output)
    agent.event(output, 'agent', 'LIMIT_REACHED')


def _console_notice(agent, method, notice, output):
    try:
        if method():
            return
    except Exception:
        pass
    agent.event(output, 'agent', notice)


def run(args, *, platform=sys.platform, backend_factory=win.WindowsBackend,
        registry_reader=win.read_registry, key_reader=win.read_key_bytes,
        interface_reader=win.interface_addresses, files_factory=vm_files.FileStore,
        listener_factory=make_listener, clock=time.monotonic, sleep=time.sleep, output=print):
    try:
        bind, peer, key, backend, size = startup(args, platform, backend_factory, registry_reader,
                                                 key_reader, interface_reader)
        files = None
        if p.selected_tools(args.tools).intersection(('file_put', 'file_get')):
            try:
                files = files_factory(args.files_dir)
            except Exception:
                raise StartupRefused('INVALID_FILES_DIR') from None
        agent = Agent(args, key, backend, size, files=files, clock=clock, sleep=sleep)
        try:
            with listener_factory(bind, args.port) as listener:
                _console_notice(agent, backend.disable_quick_edit, 'NOTICE QUICK_EDIT_UNAVAILABLE', output)
                agent.event(output, 'agent', f'START tools={args.tools}')
                _console_notice(agent, backend.minimize_console, 'NOTICE CONSOLE_MINIMIZE_UNAVAILABLE', output)
                serve(agent, listener, peer, output)
        finally:
            if agent.close_files():
                agent.event(output, 'file_cleanup', 'FILE_FAILED')
        return 0
    except StartupRefused as exc:
        output(f'{datetime.now().isoformat(timespec="seconds")} startup {exc} '
               'seconds=0 input_ops=0 non_input_ops=0')
        return 2
    except (OSError, p.ProtocolError):
        output(f'{datetime.now().isoformat(timespec="seconds")} agent NETWORK_FAILURE '
               'seconds=0 input_ops=0 non_input_ops=0')
        return 2
    except KeyboardInterrupt:
        return 130


def main(argv=None):
    return run(parser().parse_args(argv), output=lambda line: print(line, flush=True))


if __name__ == '__main__':
    raise SystemExit(main())
