import ast
from contextlib import contextmanager
import os
import ssl
import threading
import ctypes
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from ipaddress import IPv4Address
from pathlib import Path
import queue
import secrets
import socket
import sys
import time
import unittest
from unittest.mock import patch

from tests._inherited_test_temp import inherited_test_temp
from sora_commander_next import vm_protocol as p


KEY = secrets.token_bytes(32)
BOOT = secrets.token_hex(16)
LIMITS = {'seconds': 60, 'input_ops': 100, 'non_input_ops': 600}
_WIN_DLL = getattr(ctypes, 'WinDLL', None)


def ip(value):
    return str(IPv4Address(value))


def private_pair():
    return ip((10 << 24) + 2), ip((10 << 24) + 1)


class NativeIsolatedTest(unittest.TestCase):
    def setUp(self):
        for name in ('WinDLL', 'CDLL', 'OleDLL'):
            self.enterContext(patch.object(ctypes, name, side_effect=AssertionError('native loader'),
                                           create=True))
        self.enterContext(patch.object(socket, 'socket', side_effect=AssertionError('real socket')))
        if sys.platform == 'win32':
            self.enterContext(patch('winreg.OpenKey', side_effect=AssertionError('real registry')))


class ScriptedStream:
    def __init__(self, data=b''):
        self.data = data
        self.written = bytearray()
        self.closed = False
        self.reads = 0

    def settimeout(self, value):
        self.timeout = value

    def recv(self, maximum):
        self.reads += 1
        result, self.data = self.data[:maximum], self.data[maximum:]
        return result

    def send(self, data):
        self.written.extend(data)
        return len(data)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class MemoryStream(ScriptedStream):
    def __init__(self, incoming, outgoing):
        super().__init__()
        self.incoming = incoming
        self.outgoing = outgoing
        self.on_send = None
        self.on_timeout = None

    def recv(self, maximum):
        if not self.data:
            try:
                self.data = self.incoming.get(timeout=self.timeout)
            except queue.Empty:
                if self.on_timeout is not None:
                    self.on_timeout(self.timeout)
                raise TimeoutError() from None
        return super().recv(maximum)

    def send(self, data):
        raw = bytes(data)
        if self.on_send is not None:
            raw = self.on_send(raw)
        if raw:
            self.outgoing.put(raw)
        return super().send(data)

    def __exit__(self, *_):
        super().__exit__()
        self.outgoing.put(b'')


def duplex():
    a, b = queue.Queue(), queue.Queue()
    return MemoryStream(a, b), MemoryStream(b, a)


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@contextmanager
def paired(handler, configure=lambda _client, _server: None):
    client, server = duplex()
    configure(client, server)
    errors = []

    def run():
        try:
            with server:
                handler(server)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        with client:
            yield client
    finally:
        thread.join(6)
        if thread.is_alive():
            raise RuntimeError('synthetic peer did not finish')
        if errors:
            raise errors[0]


def tunnel(raw, context, *, server_side=False, clock=time.monotonic, check=True):
    stream = p.TLSStream(raw, context, server_side=server_side, clock=clock,
                         deadline=clock() + p.HANDSHAKE_SECONDS)
    if check:
        stream.handshake()
    return stream



class ProtocolTests(NativeIsolatedTest):
    def _tls_pair(self, context, server_context=None, *, session=None, legacy=False):
        server_context = server_context or p.tls_context(KEY, server_side=True)
        negotiated = []

        def serve(raw):
            server = tunnel(raw, server_context, server_side=True, check=False)
            try:
                if legacy:
                    server._drive(server._tls.do_handshake)
                else:
                    server.handshake()
            except p.ProtocolError as exc:
                negotiated.append(exc)
                return
            negotiated.append((server._tls.version(), server._tls.cipher()[0], server._tls.session_reused))
            server.finish()

        with paired(serve) as raw:
            client = tunnel(raw, context, check=False)
            if session is not None:
                client._tls.session = session
            try:
                if legacy:
                    client._drive(client._tls.do_handshake)
                else:
                    client.handshake()
                with client.makefile('rb') as reader:
                    self.assertEqual(reader.read(1), b'')
            except p.ProtocolError as exc:
                return exc, negotiated
        return client._tls, negotiated

    def test_tls_pinned_settings_and_keylog_env(self):
        self.assertEqual(p.PSK_ID, 'scn-vm-screen-1')
        self.assertEqual(p.CIPHER, 'ECDHE-PSK-CHACHA20-POLY1305')
        with patch.dict(os.environ, SSLKEYLOGFILE='unused-synthetic-keylog'):
            contexts = [p.tls_context(KEY, server_side=side) for side in (True, False)]
        for context in contexts:
            self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertEqual(context.maximum_version, ssl.TLSVersion.TLSv1_2)
            self.assertIsNone(context.keylog_filename)
            options = ssl.OP_NO_TICKET | ssl.OP_NO_COMPRESSION | ssl.OP_NO_RENEGOTIATION
            self.assertEqual(context.options & options, options)
            self.assertEqual([cipher['name'] for cipher in context.get_ciphers()
                              if cipher['protocol'] != 'TLSv1.3'], [p.CIPHER])
        server, client = contexts
        self.assertFalse(client.check_hostname)
        self.assertEqual(client.verify_mode, ssl.CERT_NONE)
        actual, negotiated = self._tls_pair(client, server)
        self.assertEqual(actual.version(), 'TLSv1.2')
        self.assertEqual(actual.cipher()[0], p.CIPHER)
        self.assertEqual(negotiated, [('TLSv1.2', p.CIPHER, False)])

    def test_negotiation_rejects_version_cipher_and_session_reuse(self):
        for version, cipher, reused in (('TLSv1.3', p.CIPHER, False),
                                        ('TLSv1.2', 'fixture-other-cipher', False),
                                        ('TLSv1.2', p.CIPHER, True)):
            with self.subTest(version=version, cipher=cipher, reused=reused):
                tls = type('Negotiated', (), dict(version=lambda _: version,
                           cipher=lambda _: (cipher,), session_reused=reused))()
                with self.assertRaises(p.ProtocolError):
                    p._check_negotiated(tls)

    def test_tls_no_ticket_and_prior_session_cannot_resume(self):
        server = p.tls_context(KEY, server_side=True)
        client = p.tls_context(KEY, server_side=False)
        original, _ = self._tls_pair(client, server)
        self.assertFalse(original.session.has_ticket)
        second, negotiated = self._tls_pair(client, session=original.session)
        self.assertFalse(second.session_reused)
        self.assertFalse(second.session.has_ticket)
        self.assertEqual(negotiated, [('TLSv1.2', p.CIPHER, False)])

    def test_tls_wrong_key_wrong_identity_empty_key_and_no_psk(self):
        for identity, key in ((p.PSK_ID, secrets.token_bytes(32)),
                              ('fixture-other-identity', KEY), (p.PSK_ID, b'')):
            with self.subTest(identity=identity, empty=not key):
                context = p.tls_context(KEY, server_side=False)
                context.set_psk_client_callback(lambda _: (identity, key))
                exc, server = self._tls_pair(context)
                self.assertIsInstance(exc, p.ProtocolError)
                self.assertIsInstance(server[0], p.ProtocolError)
                for error in (exc, server[0]):
                    self.assertNotIn(KEY.hex(), str(error) + repr(error))
                    self.assertIsNone(error.__context__)
        with patch.object(ssl, 'HAS_PSK', False), self.assertRaises(p.ProtocolError):
            p.tls_context(KEY, server_side=True)

    def test_tls_certificate_only_server_is_refused(self):
        if find_spec('cryptography') is None:
            self.skipTest('cryptography is required to generate the ephemeral TLS test certificate')
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Ephemeral TLS test')])
        now = datetime.now(UTC)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(minutes=1))
                       .not_valid_after(now + timedelta(minutes=5)).sign(key, hashes.SHA256()))

        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.minimum_version = server.maximum_version = ssl.TLSVersion.TLSv1_2
        # Only the scratch fixture's DACL/identity checks need the real file APIs.
        with patch.object(ctypes, 'WinDLL', _WIN_DLL), inherited_test_temp() as path:
            pem = Path(path) / 'synthetic.pem'
            pem.write_bytes(certificate.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            server.load_cert_chain(pem)
        legacy = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        legacy.check_hostname = False
        legacy.verify_mode = ssl.CERT_NONE
        accepted, _ = self._tls_pair(legacy, server, legacy=True)
        self.assertNotEqual(accepted.cipher()[0], p.CIPHER)
        actual, server_results = self._tls_pair(p.tls_context(KEY, server_side=False), server)
        self.assertIsInstance(actual, p.ProtocolError)
        self.assertIsInstance(server_results[0], p.ProtocolError)

    def test_tls_anonymous_server_is_refused(self):
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.minimum_version = server.maximum_version = ssl.TLSVersion.TLSv1_2
        server.set_ciphers('AECDH-AES128-SHA:@SECLEVEL=0')
        actual, server_results = self._tls_pair(p.tls_context(KEY, server_side=False), server)
        self.assertIsInstance(actual, p.ProtocolError)
        self.assertIsInstance(server_results[0], p.ProtocolError)

    def test_plaintext_garbage_and_oversized_handshake(self):
        context = p.tls_context(KEY, server_side=True)
        for raw in (b'GET /probe HTTP/1.1\r\n\r\n', b'synthetic plaintext garbage'):
            stream = ScriptedStream(raw)
            with self.assertRaises(p.ProtocolError):
                tunnel(stream, context, server_side=True)
            self.assertNotIn(b'HTTP/', stream.written)
        # TLS handshake record declares more bytes than the pre-handshake budget.
        overflow = b'\x16\x03\x03\x40\x00' + bytes(p.PRE_HANDSHAKE_MAX + 512)
        stream = ScriptedStream(overflow)
        with self.assertRaises(p.ProtocolError):
            tunnel(stream, context, server_side=True)
        self.assertEqual(len(stream.data), len(overflow) - p.PRE_HANDSHAKE_MAX)

    def test_total_handshake_deadline_includes_final_raw_read(self):
        clock = Clock()

        class SlowStream(ScriptedStream):
            def recv(self, maximum):
                clock.advance(self.timeout)
                if clock() >= p.HANDSHAKE_SECONDS:
                    # The final byte arrived only when the total deadline expired.
                    return b'\x16'
                raise TimeoutError()

        stream = SlowStream()
        with self.assertRaises(p.TransportError):
            tunnel(stream, p.tls_context(KEY, server_side=True), server_side=True, clock=clock)
        self.assertEqual(clock(), p.HANDSHAKE_SECONDS)

    def test_slow_peer_cannot_reset_read_or_write_deadline(self):
        clock = Clock()

        class LateStream(ScriptedStream):
            def send(self, data):
                clock.advance(self.timeout)
                return super().send(data[:1])

        raw = LateStream()
        with self.assertRaises(p.TransportError):
            tunnel(raw, p.tls_context(KEY, server_side=False), clock=clock)
        self.assertEqual(clock(), p.HANDSHAKE_SECONDS)
        self.assertLess(len(raw.written), 30)

    def test_json_strict_and_shallow(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":1.0}', b'{"a":NaN}', b'{"a":Infinity}',
                    b'{"a":"\xff"}', b'{"a":{"b":{"c":1}}}', b'[]', b'{}{}',
                    b'{"type":"drag","path":[[[[0]]]]}'):
            with self.subTest(raw=raw), self.assertRaises(p.ProtocolError):
                p.decode(raw)
        self.assertEqual(p.decode(b'{"a":"{[{\\"}","b":{"c":1}}')['b'], {'c': 1})

    def test_fixed_network_and_key_grammar(self):
        for good in ((10 << 24) + 1, (172 << 24) + (31 << 16) + 1, (192 << 24) + (168 << 16) + 1):
            self.assertEqual(p.address(ip(good)), ip(good))
        bad = ('localhost', '::1', ip(0), ip((127 << 24) + 1), ip((169 << 24) + (254 << 16) + 1),
               ip(0xffffffff), ip(10 << 24), ip((11 << 24) - 1), ip((172 << 24) + (32 << 16) + 1),
               ip((100 << 24) + (64 << 16) + 1), ' ' + private_pair()[0])
        for value in bad:
            with self.assertRaises(p.ProtocolError):
                p.address(value)
        local, remote = private_pair()
        table = [((10 << 24) + 2, 0xffffff00)]
        p.check_bound_addresses(local, remote, table)
        for other, info in ((ip((10 << 24) + 255), table), (ip(10 << 24), table), (remote, [])):
            with self.assertRaises(p.ProtocolError):
                p.check_bound_addresses(local, other, info)
        raw = KEY.hex().encode('ascii')
        for suffix in (b'', b'\n', b'\r\n'):
            self.assertEqual(p.decode_key(raw + suffix) == KEY, True)
        for bad_key in (raw[:-1], raw + b'\r', raw + b'\n\n', b'a' * 129,
                        b'F' + raw[1:], b' ' + raw, raw + b'\x00'):
            with self.assertRaises(p.ProtocolError):
                p.decode_key(bad_key)

    def test_request_operation_schema_and_values(self):
        point = dict(x=0, y=1)
        for req in (dict(type='click', **point, button='left'), dict(type='click', **point, button='right'),
                    dict(type='double_click', **point), dict(type='move', **point),
                    dict(type='drag', path=[point] * 20), dict(type='drag', path=[point] * 2),
                    dict(type='scroll', **point, scroll_x=-100, scroll_y=2147483647),
                    dict(type='keypress', keys=['CONTROL', 'a']), dict(type='wait'),
                    dict(type='wait', ms=10000), dict(type='type', text='あ😀')):
            body = p.decode(p.encode(req), max_depth=4)
            self.assertEqual(p.validate_action(body), req['type'])
        for req in (dict(type='click', **point, button='wheel'), dict(type='click', **point, button='back'),
                    dict(type='click', **point, button='forward'), dict(type='click', **point, button='left', count=2),
                    dict(type='double_click', **point, button='left'), dict(type='move', x=True, y=1),
                    dict(type='drag', path=[point]), dict(type='drag', path=[point] * 21),
                    dict(type='drag', path=[point, dict(x=1, y=2, extra=0)]),
                    dict(type='drag', path=[point, [1, 2]]), dict(type='scroll', **point, scroll_x=0, scroll_y=0),
                    dict(type='scroll', **point, scroll_x=0), dict(type='wait', ms=[1]),
                    dict(type='wait', ms=0), dict(type='wait', ms=10001),
                    dict(type='wait', ms=False), dict(type='zoom'), dict(type='screenshot'),
                    dict(type='type_text', text='old'), dict(type='key', combo='Enter')):
            with self.subTest(req=req), self.assertRaises(p.ProtocolError):
                p.validate_action(req)
        for text in ('', 'x' * 1001, 'line\rbreak', '\ud800', '\ue001', '\u2028', ['text']):
            with self.assertRaises(p.ProtocolError):
                p.validate_text(text)

    def test_every_allowlisted_key_and_excluded_combos(self):
        for key in p.KEYS:
            self.assertEqual(p.key_combo(key.lower().split('+')), tuple(key.split('+')))
        for alias, name in (('RETURN', 'Enter'), ('esc', 'Escape'), ('del', 'Delete'),
                            ('arrowup', 'Up'), ('arrowdown', 'Down'), ('arrowleft', 'Left'),
                            ('arrowright', 'Right'), ('meta', 'Win'), ('cmd', 'Win'), ('command', 'Win')):
            self.assertEqual(p.key_combo([alias]), (name,))
        self.assertEqual(p.key_combo(['f4', 'option']), ('Alt', 'F4'))
        self.assertEqual(p.key_combo(['a', 'control']), ('Ctrl', 'A'))
        for keys in ([], ['win', 'l'], ['win', 'win'], ['cmd', 'meta'],
                     ['ctrl', 'control', 'a'], ['ctrl', 'alt', 'del'], ['a', 'b'],
                     ['f13'], ['ctrl'], ['İME'], ['ctrl', 'shift', 'alt', 'win', 'a'],
                     'Enter', [12], [['Enter']]):
            with self.subTest(keys=keys), self.assertRaises(p.ProtocolError):
                p.key_combo(keys)

    def test_scroll_notches_sign_rounding_minimum_and_cap(self):
        for pixels, expected in ((0, 0), (1, 1), (49, 1), (100, 1), (149, 1),
                                 (150, 2), (249, 2), (250, 3), (1999, 20), (20000, 20)):
            self.assertEqual(p.notches(pixels), expected)
            self.assertEqual(p.notches(-pixels), -expected)

    def test_wire_repr_and_failures_do_not_expose_body_or_key(self):
        marker = secrets.token_hex(32)
        self.assertNotIn(marker, repr(p.WireObject(text=marker)))
        with self.assertRaises(p.ProtocolError) as error:
            p.decode(('{"' + marker + '":1.0}').encode())
        self.assertNotIn(marker, repr(error.exception))

    def test_response_validation(self):
        probe = dict(result='OK', version=p.VERSION, boot_id=BOOT, vm_name='Fixture VM',
                     width=640, height=480, tools=p.DEFAULT_TOOLS, remaining=LIMITS)
        p.validate_response(probe, 200, 'probe')
        p.validate_response(dict(result='DUPLICATE_OP', remaining=LIMITS, state='REFUSED'), 409, 'click')
        for change in (dict(width=True), dict(boot_id=19), dict(tools=['screenshot']),
                       dict(width=480), dict(version=1), dict(extra='extra'),
                       dict(remaining=dict(LIMITS, seconds=-1)), dict(result=[]),
                       dict(tools='screenshot,screenshot'), dict(vm_name='line\nbreak')):
            with self.subTest(change=change), self.assertRaises(p.ProtocolError):
                p.validate_response(dict(probe, **change), 200, 'probe')
        with self.assertRaises(p.ProtocolError):
            p.validate_response(dict(result='SENT', remaining=LIMITS), 200, 'wait')
        with self.assertRaises(p.ProtocolError):
            p.validate_response(dict(result='UNKNOWN', remaining=LIMITS), 200, 'click')

    def test_agent_sources_use_only_stdlib_and_the_local_modules(self):
        directory = Path(p.__file__).resolve().parent
        sources = {'vm_agent', 'vm_client', 'vm_files', 'vm_protocol', 'vm_transfer', 'vm_uia', 'vm_win'}
        self.assertEqual(len(sources), 7)
        banned = {'subprocess', 'shutil', 'pickle', 'system', 'eval', 'exec'}
        for source in sorted(sources):
            tree = ast.parse((directory / f'{source}.py').read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = {name.name.split('.')[0] for name in node.names}
                    self.assertLessEqual(modules, sys.stdlib_module_names | sources)
                    self.assertFalse(modules & banned)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        self.assertEqual((node.level, node.module), (1, None))
                        self.assertLessEqual({name.name for name in node.names}, sources)
                    else:
                        self.assertIn(node.module.split('.')[0], sys.stdlib_module_names)
                        self.assertNotIn(node.module.split('.')[0], banned)
                    self.assertFalse({name.name for name in node.names} & banned)
                elif isinstance(node, ast.Name):
                    self.assertNotIn(node.id, banned)
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, banned)
