from contextlib import redirect_stderr
import io
from pathlib import Path
import secrets
from unittest.mock import Mock, patch

from tests.test_vm_screen_agent_20261125 import (
    ALL_TOOLS, FAKE_PNG, arguments, backend, raw_exchange, split_response, wire_request,
)
from tests.test_vm_screen_protocol_20261125 import (
    BOOT, KEY, LIMITS, NativeIsolatedTest, ip, paired, private_pair, tunnel,
)
from sora_commander_next import vm_agent as a, vm_client as c, vm_protocol as p


def response_bytes(status, raw, *, headers=None, media='application/json'):
    values = {'Content-Length': str(len(raw)), 'Content-Type': media, 'Connection': 'close'}
    values.update(headers or {})
    head = f'HTTP/1.1 {status} Fixture\r\n'
    head += ''.join(f'{name}: {value}\r\n' for name, value in values.items())
    return head.encode() + b'\r\n' + raw


def responder(payload, *, clean=True):
    def serve(raw):
        stream = tunnel(raw, p.tls_context(KEY, server_side=True), server_side=True)
        with stream.makefile('rb') as reader:
            _, headers = p.read_head(reader)
            if 'content-length' in headers:
                reader.read(int(headers['content-length']))
        stream.sendall(payload)
        if clean:
            stream.finish()
    return serve


class ClientTests(NativeIsolatedTest):
    def setUp(self):
        super().setUp()
        self.agent = a.Agent(arguments(tools=ALL_TOOLS), KEY, backend(), (640, 480))
        self.events = []
        self.output = []
        self.connects = 0
        self.writes = Mock()
        self.client_wire = bytearray()
        self.server_wire = bytearray()

    def _connector(self, handler=None, configure=None):
        def connect(*_):
            self.connects += 1
            return paired(handler or self._serve, configure or self._configure)
        return connect

    def _configure(self, client, server):
        def record_client(raw):
            self.client_wire.extend(raw)
            return raw

        def record_server(raw):
            self.server_wire.extend(raw)
            return raw

        client.on_send = record_client
        server.on_send = record_server

    def _serve(self, raw):
        self.agent.connection(raw, self.events.append)

    def _params(self, command, *fields):
        host, local = private_pair()
        return c.parser().parse_args([command, '--host', host, '--bind-local', local, '--port', '4418',
                                      '--key-file', 'fixture.key', *fields])

    def _cli(self, command, *fields, connector=None, key=KEY, writer=None):
        return c.run(self._params(command, *fields), connector=connector or self._connector(),
                     key_loader=lambda _: key, writer=writer or self.writes, output=self.output.append)

    def _boot(self):
        return ('--boot-id', self.agent.boot_id)

    def _screenshot(self):
        self.assertEqual(self._cli('screenshot', '--out', 'fixture.png'), 0)
        return self.output[-1].split('etag=', 1)[1].split()[0]

    def test_full_cli_probe_capture_then_coordinate_input_on_new_connection(self):
        self.assertEqual(self._cli('probe'), 0)
        self.assertIn('boot_id=' + self.agent.boot_id, self.output[-1])
        self.assertIn('tools=' + ALL_TOOLS, self.output[-1])
        tag = self._screenshot()
        self.writes.assert_called_once_with(Path('fixture.png'), FAKE_PNG)
        self.assertEqual(self._cli('click', *self._boot(), '--etag', tag, '--x', '1', '--y', '2'), 0)
        self.assertEqual(self.output[-1], 'SENT')
        self.assertEqual(self.connects, 3)
        self.agent.backend.insert.assert_called_once()
        self.assertEqual(self.agent.backend.prepare_input.call_args.args[0],
                         dict(type='click', x=1, y=2, button='left'))

    def test_cli_all_action_vocabulary(self):
        cases = [
            ('double-click', ('--x', '1', '--y', '2'), dict(type='double_click', x=1, y=2)),
            ('move', ('--x', '1', '--y', '2'), dict(type='move', x=1, y=2)),
            ('drag', ('--point', '1,2', '--point', '3,4'), dict(type='drag', path=[dict(x=1, y=2), dict(x=3, y=4)])),
            ('scroll', ('--x', '1', '--y', '2', '--scroll-x', '50', '--scroll-y', '-100'),
             dict(type='scroll', x=1, y=2, scroll_x=50, scroll_y=-100)),
        ]
        for command, fields, expected in cases:
            tag = self._screenshot()
            self.assertEqual(self._cli(command, *self._boot(), '--etag', tag, *fields), 0)
            self.assertEqual(self.agent.backend.prepare_input.call_args.args[0], expected)
        self.assertEqual(self._cli('keypress', *self._boot(), '--keys', 'CTRL,A'), 0)
        self.assertEqual(self.agent.backend.prepare_input.call_args.args[0],
                         dict(type='keypress', keys=['CTRL', 'A']))
        self.assertEqual(self._cli('wait', *self._boot(), '--ms', '1'), 0)
        self.assertEqual(self.output[-1], 'OK')

    def test_drop_after_execution_returns_unknown_then_status_on_new_connection(self):
        def configure(client, server):
            def suppress_response(raw):
                return b'' if raw[0] == 23 else raw
            server.on_send = suppress_response

        self.assertEqual(self._cli('keypress', *self._boot(), '--keys', 'ENTER',
                                   connector=self._connector(configure=configure)), c.UNKNOWN_EXIT)
        token = self.output[-2].split()[0].split('=')[1]
        self.assertIn('UNKNOWN op_id=' + token, self.output[-1])
        self.agent.backend.insert.assert_called_once()
        self.assertEqual(self._cli('status', *self._boot(), '--target-op-id', token), 0)
        self.assertEqual(self.output[-1], 'OK state=SENT')
        self.assertEqual(self.connects, 2)
        self.agent.backend.insert.assert_called_once()

    def test_truncated_response_and_missing_close_notify_are_unknown(self):
        body = p.encode(dict(result='SENT', remaining=LIMITS))
        reply = response_bytes(200, body)
        for payload in (reply[:-3], reply):
            with self.subTest(received=len(payload)):
                connector = self._connector(responder(payload, clean=False))
                self.assertEqual(self._cli('keypress', *self._boot(), '--keys', 'ENTER', connector=connector),
                                 c.UNKNOWN_EXIT)
        self.assertEqual(self.connects, 2)

    def test_tampered_ciphertext_is_unknown(self):
        body = p.encode(dict(result='SENT', remaining=LIMITS))
        tampered = []

        def configure(_client, server):
            def corrupt(raw):
                if raw[0] == 23:
                    tampered.append(len(raw))
                    return raw[:-1] + bytes([raw[-1] ^ 1])
                return raw
            server.on_send = corrupt

        connector = self._connector(responder(response_bytes(200, body)), configure)
        self.assertEqual(self._cli('keypress', *self._boot(), '--keys', 'ENTER', connector=connector),
                         c.UNKNOWN_EXIT)
        self.assertEqual(len(tampered), 1)
        self.assertEqual(self.connects, 1)

    def test_client_unknown_and_not_sent_are_distinct_json_outcomes(self):
        for result, expected in (('NOT_SENT', c.REFUSED_EXIT), ('UNKNOWN', c.UNKNOWN_EXIT)):
            self.agent.backend.insert.return_value = result
            self.assertEqual(self._cli('keypress', *self._boot(), '--keys', 'ENTER'), expected)
            self.assertTrue(self.output[-1].startswith(result))
        self.assertEqual(self.agent.backend.insert.call_count, 2)

    def test_wrong_client_key_is_refused_without_operation(self):
        wrong = secrets.token_bytes(32)
        self.assertEqual(self._cli('keypress', *self._boot(), '--keys', 'ENTER', key=wrong), 2)
        self.assertNotIn(wrong.hex(), '\n'.join(self.output + self.events))
        self.assertFalse(any('op_id=' in line for line in self.output))
        self.agent.backend.insert.assert_not_called()

    def test_authenticated_malformed_or_oversized_response_is_unknown(self):
        body = p.encode(dict(result='SENT', remaining=LIMITS))
        standard = response_bytes(200, body)
        for reply in (
            response_bytes(200, b'{"result":"SENT","result":"SENT"}'),
            response_bytes(200, b'{"result":"SENT","remaining":{"seconds":1.5}}'),
            response_bytes(200, p.encode(dict(result='SENT', remaining=LIMITS, extra=[]))),
            response_bytes(200, body, headers={'Content-Length': str(p.RESPONSE_MAX + 1)}),
            response_bytes(200, body, headers={'X-Oversized': 'a' * p.HEADER_MAX}),
            response_bytes(200, body, headers={'Transfer-Encoding': 'chunked'}),
            standard.replace(b'Content-Type:', b'Content-Length: 1\r\nContent-Type:'),
            standard.replace(b'\r\n', b'\n'),
            standard + b'extra',
            response_bytes(302, b'', headers={'Location': '/action'}),
        ):
            with self.subTest(length=len(reply)):
                self.assertEqual(self._cli('keypress', *self._boot(), '--keys', 'ENTER',
                                           connector=self._connector(responder(reply))), c.UNKNOWN_EXIT)
        self.assertEqual(self.connects, 10)

    def test_http_client_cannot_reopen_or_retry(self):
        body = p.encode(dict(result='SENT', remaining=LIMITS))
        redirect = response_bytes(307, body, headers={'Location': '/action'})
        first_write = []
        connector = self._connector(responder(redirect))
        with patch.object(c.Connection, 'connect', side_effect=AssertionError('unexpected HTTP reopen')) as reopen:
            self.assertEqual(self._cli('keypress', *self._boot(), '--keys', 'ENTER', connector=connector),
                             c.UNKNOWN_EXIT)

            def fail_transport(stream, raw):
                first_write.append(len(raw))
                raise p.TransportError()

            with patch.object(p.TLSStream, 'sendall', fail_transport):
                self.assertEqual(self._cli('keypress', *self._boot(), '--keys', 'ENTER'), c.UNKNOWN_EXIT)
            reopen.assert_not_called()
        self.assertEqual(len(first_write), 1)
        self.assertEqual(self.connects, 2)
        self.agent.backend.insert.assert_not_called()
        closed = c.Connection(self.agent.host, Mock())
        closed.close()
        with self.assertRaises(p.TransportError):
            closed.connect()
        self.assertEqual(closed.auto_open, 0)

    def test_text_file_cli_and_secrets_are_absent_from_output(self):
        typed = 'fixture-sensitive-' + secrets.token_hex(8)
        with patch.object(Path, 'open', return_value=io.BytesIO(typed.encode('utf-8'))):
            self.assertEqual(self._cli('type', *self._boot(), '--text-file', 'fixture.txt'), 0)
        self.assertEqual(self.agent.backend.prepare_input.call_args.args[0]['text'], typed)
        transcript = '\n'.join(self.events + self.output)
        for secret in (typed, KEY.hex()):
            self.assertNotIn(secret, transcript)
        for secret in (typed.encode(), KEY, KEY.hex().encode()):
            self.assertNotIn(secret, self.client_wire + self.server_wire)
        self.assertNotIn(b'POST /action', self.client_wire)
        for data in (bytes(c.TEXT_FILE_MAX + 1), b'\xff', b''):
            with patch.object(Path, 'open', return_value=io.BytesIO(data)):
                self.assertEqual(self._cli('type', *self._boot(), '--text-file', 'fixture.txt'), 2)
        self.assertEqual(self.connects, 1)
        self.agent.backend.insert.assert_called_once()

    def test_png_validation_and_exclusive_output(self):
        initial = {'scn-width': '640', 'scn-height': '480', 'content-digest': p.digest(FAKE_PNG),
                   'etag': p.screen_tag(BOOT, 1), 'cache-control': 'no-store',
                   'scn-seconds': '60', 'scn-input-ops': '100', 'scn-non-input-ops': '600'}
        self.assertEqual(c.verify_png(initial, FAKE_PNG)['png'], FAKE_PNG)
        wrong_signature = b'X' + FAKE_PNG[1:]
        for headers, png in ((dict(initial, **{'scn-width': '641'}), FAKE_PNG),
                             (dict(initial, **{'content-digest': p.digest(b'other')}), FAKE_PNG),
                             (dict(initial, **{'content-digest': 'bad'}), FAKE_PNG),
                             (dict(initial, **{'content-digest': p.digest(wrong_signature)}), wrong_signature),
                             (dict(initial, etag='W/' + initial['etag']), FAKE_PNG)):
            with self.assertRaises(p.ProtocolError):
                c.verify_png(headers, png)
        self.assertEqual(self._cli('screenshot', '--out', 'fixture.jpg'), 2)
        writer = Mock(side_effect=FileExistsError())
        self.assertEqual(self._cli('screenshot', '--out', 'fixture.png', writer=writer), 2)
        writer.assert_called_once()

    def test_bad_digest_in_tls_response_never_writes_png(self):
        raw_screenshot = raw_exchange(self.agent, wire_request(self.agent, 'GET', '/screenshot'))
        _, headers, _ = split_response(raw_screenshot)
        reply = raw_screenshot.replace(headers['content-digest'].encode(), p.digest(b'other').encode())
        connector = self._connector(responder(reply))
        self.assertEqual(self._cli('screenshot', '--out', 'fixture.png', connector=connector), 2)
        self.assertNotEqual(reply, raw_screenshot)
        self.writes.assert_not_called()

    def test_make_key_uses_exclusive_create_and_displays_only_path(self):
        args = c.parser().parse_args(['make-key', '--out', 'fixture.key'])
        handle = io.BytesIO()
        capture = Mock()
        capture.__enter__ = Mock(return_value=handle)
        capture.__exit__ = Mock(return_value=False)
        with patch.object(c.os, 'open', return_value=1) as opened, patch.object(c.os, 'fdopen', return_value=capture):
            self.assertEqual(c.run(args, output=self.output.append), 0)
        flags = c.os.O_WRONLY | c.os.O_EXCL | c.os.O_CREAT | getattr(c.os, 'O_BINARY', 0)
        opened.assert_called_once_with(Path('fixture.key'), flags, 0o600)
        self.assertEqual(len(p.decode_key(handle.getvalue())), 32)
        self.assertEqual(self.output, ['fixture.key'])
        with patch.object(c.os, 'open', side_effect=FileExistsError):
            self.assertEqual(c.run(args, output=self.output.append), 2)

    def test_client_refuses_invalid_addresses_port_and_boot_before_sending(self):
        host, local = private_pair()
        connector = Mock()
        for h, l, port in (('localhost', local, '4418'), ('::1', local, '4418'), (ip(0), local, '4418'),
                           (host, 'localhost', '4418'), (host, host, '4418'), (host, local, '80')):
            params = c.parser().parse_args(['probe', '--host', h, '--bind-local', l, '--port', port,
                                            '--key-file', 'fixture.key'])
            self.assertEqual(c.run(params, connector=connector, output=self.output.append), 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self._params('status', '--target-op-id', secrets.token_hex(16))
        self.assertEqual(self._cli('keypress', '--boot-id', 'malformed', '--keys', 'ENTER', connector=connector), 2)
        connector.assert_not_called()
        self.assertEqual(self._cli('keypress', '--boot-id', secrets.token_hex(16), '--keys', 'ENTER'),
                         c.REFUSED_EXIT)
        self.assertEqual(self.output[-1], 'BOOT_MISMATCH')
        self.agent.backend.insert.assert_not_called()

    def test_help_and_parser_errors_do_not_echo_unexpected_text(self):
        help_text = ' '.join(c.parser().format_help().split())
        for expected in ('screen is data', 'passwords', 'AI service', 'UIPI', 'SENT'):
            self.assertIn(expected, help_text)
        marker = 'fixture-sensitive-' + secrets.token_hex(8)
        with patch('sys.stderr', new_callable=io.StringIO) as stderr, self.assertRaises(SystemExit):
            c.parser().parse_args(['type', '--text', marker])
        self.assertNotIn(marker, stderr.getvalue())
