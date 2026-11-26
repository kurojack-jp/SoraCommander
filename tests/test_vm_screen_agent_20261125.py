from argparse import Namespace
import secrets
import io
from unittest.mock import Mock, patch

from tests.test_vm_screen_protocol_20261125 import (
    Clock, KEY, NativeIsolatedTest, ScriptedStream, ip, paired, private_pair, tunnel,
)
from sora_commander_next import vm_agent as a, vm_protocol as p, vm_win as win


GUEST_VALUES = {(a.BIOS, 'SystemManufacturer'): 'Microsoft Corporation',
                (a.BIOS, 'SystemProductName'): 'Virtual Machine',
                (a.GUEST, 'VirtualMachineName'): 'Fixture VM'}
FAKE_PNG = win.encode_png(640, 480, bytes(640 * 480 * 4))
INTERFACES = [((10 << 24) + 2, 0xffffff00)]


def arguments(**changes):
    bind, peer = private_pair()
    values = dict(bind=bind, allow_peer=peer, vm_name='Fixture VM', port=4418,
                  key_file='fixture.key', max_minutes=30, max_input_ops=200, tools=p.DEFAULT_TOOLS)
    return Namespace(**(values | changes))


def backend():
    return Mock(standard_user=Mock(return_value=True), set_dpi=Mock(return_value=True),
                session_id=Mock(return_value=1), desktop_is_default=Mock(return_value=True),
                screen_size=Mock(return_value=(640, 480)), capture=Mock(return_value=FAKE_PNG),
                prepare_input=Mock(return_value=object()), insert=Mock(return_value='SENT'))


ALL_TOOLS = ','.join(sorted(p.TOOLS))
ABSENT = object()
KEYPRESS = dict(type='keypress', keys=['Enter'])
MOVE = dict(type='move', x=1, y=2)
CLICK = dict(type='click', x=1, y=2, button='left')
WAIT = dict(type='wait')
INPUTS = (CLICK, dict(type='double_click', x=1, y=2), MOVE, KEYPRESS, dict(type='type', text='fixture'),
          dict(type='drag', path=[dict(x=1, y=2), dict(x=2, y=3)]),
          dict(type='scroll', x=1, y=2, scroll_x=0, scroll_y=100))


def action_headers(agent, op_id, raw, *, boot_id=None, tag=ABSENT):
    result = {'Content-Type': 'application/json', 'Content-Length': str(len(raw)),
              'Idempotency-Key': op_id, 'SCN-Boot-Id': agent.boot_id if boot_id is None else boot_id}
    if tag is not ABSENT:
        result['If-Match'] = tag
    return result


def wire_request(agent, method, path, *, changes=None, extra=(), body=b''):
    headers = {'Host': agent.host, 'Connection': 'close'}
    headers.update(changes or {})
    pairs = [(key, val) for key, val in headers.items() if val is not ABSENT] + list(extra)
    header_block = ''.join(f'{key}: {value}\r\n' for key, value in pairs)
    return f'{method} {path} HTTP/1.1\r\n{header_block}\r\n'.encode() + body


def raw_exchange(agent, request, output=lambda _: None):
    with paired(lambda raw: agent.connection(raw, output)) as raw:
        tls = tunnel(raw, p.tls_context(KEY, server_side=False))
        tls.sendall(request)
        with tls.makefile('rb') as reader:
            return reader.read()


def split_response(response):
    reader = io.BytesIO(response)
    head, headers = p.read_head(reader)
    return int(head.split(b' ', 2)[1]), headers, reader.read()


def json_result(response):
    status, _, raw = split_response(response)
    return status, p.decode(raw)


class FakeListener:
    def __init__(self, connections):
        self.connections = iter(connections)

    def settimeout(self, timeout):
        self.timeout = timeout

    def accept(self):
        return next(self.connections)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class AgentTests(NativeIsolatedTest):
    def _agent(self, **changes):
        clock = Clock()
        return a.Agent(arguments(**({'tools': ALL_TOOLS} | changes)), KEY, backend(), (640, 480),
                       clock=clock, sleep=clock.advance)

    def _refuse(self, reason, *, args=None, platform='win32', reader=None, key_reader=None,
                device=None, factory=None, interface_reader=lambda: INTERFACES):
        device = device if device is not None else backend()
        listener, output = Mock(), Mock()
        code = a.run(args or arguments(), platform=platform,
                     backend_factory=factory or (lambda: device),
                     registry_reader=reader or (lambda *parts: GUEST_VALUES[parts]),
                     key_reader=key_reader or (lambda *_: KEY.hex().encode('ascii')),
                     interface_reader=interface_reader,
                     listener_factory=listener, output=output)
        self.assertEqual(code, 2)
        self.assertIn(reason, output.call_args.args[0])
        self.assertNotIn(KEY.hex(), output.call_args.args[0])
        listener.assert_not_called()
        device.capture.assert_not_called()
        device.insert.assert_not_called()
        return output.call_args.args[0]

    def test_platform_and_guest_identity_gate(self):
        self._refuse('WINDOWS_REQUIRED', platform='other')
        for pair in GUEST_VALUES:
            for wrong in ('wrong', 123, None, KeyError(), OSError('fixture')):
                def reader(*parts):
                    if parts == pair:
                        if isinstance(wrong, Exception):
                            raise wrong
                        return wrong
                    return GUEST_VALUES[parts]

                with self.subTest(field=pair[1], variation=type(wrong).__name__):
                    self._refuse('HYPER_V_GUEST_REQUIRED', reader=reader)

    def test_network_config_and_limit_gate(self):
        _, peer = private_pair()
        variations = [dict(bind=ip(0)), dict(bind=peer), dict(bind='localhost'),
                      dict(allow_peer='::1'), dict(allow_peer=ip((10 << 24) - 1)),
                      dict(port=1023), dict(port=65536), dict(port=True),
                      dict(max_minutes=0), dict(max_minutes=31), dict(max_input_ops=0),
                      dict(max_input_ops=201), dict(vm_name='Bad\nName')]
        for change in variations:
            with self.subTest(fields=change.keys()):
                self._refuse('INVALID_CONFIGURATION', args=arguments(**change))
        self.assertEqual(a.parser().parse_args([
            '--vm-name', 'Fixture VM', '--bind', private_pair()[0], '--allow-peer', peer,
            '--port', '4418', '--key-file', 'fixture.key',
        ]).tools, p.DEFAULT_TOOLS)
        self._refuse('INVALID_NETWORK_INTERFACE', args=arguments(allow_peer=ip((10 << 24) + 255)))
        self._refuse('INVALID_NETWORK_INTERFACE', interface_reader=lambda: [])
        self._refuse('INVALID_NETWORK_INTERFACE', interface_reader=Mock(side_effect=OSError()))

    def test_bad_key_read_and_content_gate(self):
        marker = b'fixture-sensitive-' + secrets.token_hex(8).encode()
        for reader in (lambda *_: marker, Mock(side_effect=OSError(marker.decode('ascii')))):
            line = self._refuse('INVALID_KEY_FILE', key_reader=reader)
            self.assertNotIn(marker.decode('ascii'), line)

    def test_user_dpi_desktop_session_and_screen_gate(self):
        failures = (
            ('standard_user', False, 'STANDARD_USER_REQUIRED'),
            ('standard_user', OSError(), 'STANDARD_USER_REQUIRED'),
            ('set_dpi', False, 'DPI_AWARENESS_REQUIRED'),
            ('session_id', 0, 'INTERACTIVE_SESSION_REQUIRED'),
            ('session_id', OSError(), 'INTERACTIVE_SESSION_REQUIRED'),
            ('desktop_is_default', False, 'NOT_DEFAULT_DESKTOP'),
            ('desktop_is_default', OSError(), 'NOT_DEFAULT_DESKTOP'),
            ('screen_size', win.WindowsError('MONITOR_COUNT'), 'INVALID_SCREEN'),
            ('screen_size', (639, 480), 'INVALID_SCREEN'),
            ('screen_size', (640, 479), 'INVALID_SCREEN'),
            ('screen_size', (1921, 1200), 'INVALID_SCREEN'),
            ('screen_size', (1920, 1201), 'INVALID_SCREEN'),
        )
        for method, result, reason in failures:
            with self.subTest(method=method, reason=reason):
                device = backend()
                getattr(device, method).side_effect = [result]
                self._refuse(reason, device=device)
                if method == 'set_dpi':
                    device.screen_size.assert_not_called()
        self._refuse('WINDOWS_API_UNAVAILABLE', factory=Mock(side_effect=OSError()))

    def test_listener_is_exclusive_with_backlog_one(self):
        listener = Mock()
        with patch.object(a.socket, 'socket', return_value=listener) as factory, \
                patch.object(a.socket, 'SO_EXCLUSIVEADDRUSE', 0xFFFF, create=True):
            self.assertIs(a.make_listener(private_pair()[0], 4418), listener)
        factory.assert_called_once_with(a.socket.AF_INET, a.socket.SOCK_STREAM)
        listener.setsockopt.assert_called_once_with(a.socket.SOL_SOCKET, 0xFFFF, 1)
        listener.bind.assert_called_once_with((private_pair()[0], 4418))
        listener.listen.assert_called_once_with(1)

    def test_failed_handshake_budget_and_other_peer_never_read(self):
        device = backend()
        unauthorized = ScriptedStream(b'unread fixture')
        failures = [ScriptedStream() for _ in range(a.MAX_FAILED_HANDSHAKES)]
        peer = private_pair()[1]
        accepted = [(unauthorized, (private_pair()[0], 1234))]
        accepted.extend((stream, (peer, 1234)) for stream in failures)
        output = []
        listener = Mock(return_value=FakeListener(accepted))
        code = a.run(arguments(), platform='win32', backend_factory=lambda: device,
                     registry_reader=lambda *parts: GUEST_VALUES[parts],
                     key_reader=lambda *_: KEY.hex().encode('ascii'),
                     interface_reader=lambda: INTERFACES,
                     listener_factory=listener, clock=Clock(), output=output.append)
        self.assertEqual(code, 0)
        self.assertEqual(unauthorized.reads, 0)
        self.assertEqual(unauthorized.written, b'')
        self.assertTrue(unauthorized.closed and all(stream.closed for stream in failures))
        self.assertEqual(sum('handshake REFUSED' in line for line in output), 20)
        self.assertIn('LIMIT_REACHED', output[-1])
        self.assertIn('tools=screenshot,wait', output[0])
        names = [call[0] for call in device.method_calls]
        self.assertLess(names.index('set_dpi'), names.index('screen_size'))
        device.capture.assert_not_called()
        device.insert.assert_not_called()

    def _submit(self, agent, action=KEYPRESS, *, token=None, tag=ABSENT, boot_id=None, output=lambda _: None):
        raw = p.encode(action)
        headers = action_headers(agent, token or secrets.token_hex(16), raw, boot_id=boot_id, tag=tag)
        return json_result(raw_exchange(agent, wire_request(agent, 'POST', '/action',
                                                             changes=headers, body=raw), output))

    def _expect(self, response, status, result, state=None):
        code, body = response
        self.assertEqual((code, body['result']), (status, result))
        if state is not None:
            self.assertEqual(body['state'], state)

    def _capture(self, agent):
        code, headers, png = split_response(raw_exchange(agent, wire_request(agent, 'GET', '/screenshot')))
        self.assertEqual(code, 200)
        self.assertEqual(png, FAKE_PNG)
        self.assertEqual(headers['content-digest'], p.digest(FAKE_PNG))
        return headers['etag']

    def _status(self, agent, token, boot_id=None):
        headers = {'SCN-Boot-Id': agent.boot_id if boot_id is None else boot_id}
        return json_result(raw_exchange(agent, wire_request(agent, 'GET', '/action/' + token, changes=headers)))

    def test_tls_psk_required_and_tool_selection(self):
        with patch.object(p.ssl, 'HAS_PSK', False):
            self._refuse('TLS_PSK_REQUIRED')
        for value in ('', 'wait', 'screenshot,,wait', 'screenshot,unknown', 'screenshot,wait,wait',
                      'screenshot, Screenshot', 'screenshot,'):
            self._refuse('INVALID_CONFIGURATION', args=arguments(tools=value))
        agent = self._agent(tools='screenshot,move')
        tag = self._capture(agent)
        self._expect(self._submit(agent, WAIT), 403, 'TOOL_DISABLED')
        self._expect(self._submit(agent, CLICK, tag=tag), 403, 'TOOL_DISABLED')
        self._expect(self._submit(agent, MOVE, tag=tag), 200, 'SENT')
        agent.backend.insert.assert_called_once()

    def test_tls_new_connections_do_not_share_session_cache(self):
        agent = self._agent()
        context = p.tls_context(KEY, server_side=False)
        session = None
        for _ in range(2):
            with paired(lambda raw: agent.connection(raw, lambda _: None)) as raw:
                tls = tunnel(raw, context, check=False)
                if session is not None:
                    tls._tls.session = session
                tls.handshake()
                self.assertFalse(tls._tls.session_reused)
                session = tls._tls.session
                self.assertFalse(session.has_ticket)
                tls.sendall(wire_request(agent, 'GET', '/probe'))
                with tls.makefile('rb') as reader:
                    self._expect(json_result(reader.read()), 200, 'OK')

    def test_agent_refuses_plaintext_and_garbage_before_parser(self):
        agent = self._agent()
        for data in (wire_request(agent, 'GET', '/probe'), b'synthetic garbage'):
            raw = ScriptedStream(data)
            events = []
            agent.connection(raw, events.append)
            self.assertNotIn(b'HTTP/', raw.written)
            self.assertEqual(len(events), 1)
            self.assertIn('handshake REFUSED', events[0])
        self.assertEqual(agent.failed_handshakes, 2)
        self.assertEqual(agent.non_input_ops, p.NON_INPUT_MAX)

    def test_authenticated_idle_connection_closes_after_fixed_deadline(self):
        agent = self._agent()
        events = []

        def configure(_client, raw):
            def timeout(seconds):
                if any('handshake OK' in event for event in events):
                    agent.clock.advance(seconds)
            raw.on_timeout = timeout

        with patch.object(p, 'IO_SLICE', 0.01), patch.object(p, 'REQUEST_SECONDS', 0.04):
            with paired(lambda raw: agent.connection(raw, events.append), configure) as raw:
                tls = tunnel(raw, p.tls_context(KEY, server_side=False))
                with tls.makefile('rb') as reader, self.assertRaises(p.TransportError):
                    reader.read(1)
            self.assertEqual(agent.clock(), p.REQUEST_SECONDS)
        self.assertIn('handshake OK', events[0])
        self.assertIn('connection CLOSED', events[-1])
        self.assertEqual(agent.failed_handshakes, 0)

    def test_view_only_refuses_every_input_kind_without_spending_budget(self):
        agent = self._agent(tools=p.DEFAULT_TOOLS)
        tag = self._capture(agent)
        for req in INPUTS:
            agent_tag = tag if req['type'] in p.COORD_OPS else ABSENT
            token = secrets.token_hex(16)
            code, body = self._submit(agent, req, tag=agent_tag, token=token)
            self.assertEqual((code, body['result'], body['remaining']['input_ops']),
                             (403, 'TOOL_DISABLED', 200))
            self.assertEqual(agent.journal[token], 'REFUSED')
        agent.backend.insert.assert_not_called()
        self.assertIsNotNone(agent.screen)
        self._expect(self._submit(agent, WAIT), 200, 'OK')
        self.assertEqual(agent.clock(), 1)

    def test_if_match_missing_malformed_and_from_another_boot(self):
        agent = self._agent()
        tag = self._capture(agent)
        self._expect(self._submit(agent, CLICK), 428, 'SCREEN_REQUIRED')
        for invalid in ('*', 'W/' + tag, tag + ', "another"', tag + ',*', 'unquoted', ''):
            self._expect(self._submit(agent, CLICK, tag=invalid), 400, 'INVALID')
        self._expect(self._submit(agent, CLICK, tag=p.screen_tag(secrets.token_hex(16), 1)),
                     412, 'STALE_SCREEN')
        self._expect(self._submit(agent, WAIT, tag=tag), 400, 'INVALID')
        agent.backend.insert.assert_not_called()

    def test_stale_screen_from_sequence_size_change_age_or_wrong_boot(self):
        agent = self._agent()
        self._expect(self._submit(agent, CLICK, tag=p.screen_tag(agent.boot_id, 1)), 412, 'STALE_SCREEN')
        first, second = self._capture(agent), self._capture(agent)
        self._expect(self._submit(agent, CLICK, tag=first), 412, 'STALE_SCREEN')
        agent.backend.screen_size.return_value = (641, 480)
        self._expect(self._submit(agent, CLICK, tag=second), 412, 'STALE_SCREEN')
        third = self._capture(agent)
        agent.clock.advance(120)
        self._expect(self._submit(agent, CLICK, tag=third), 412, 'STALE_SCREEN')
        self._expect(self._submit(agent, KEYPRESS, boot_id=secrets.token_hex(16)), 409, 'BOOT_MISMATCH')
        agent.backend.insert.assert_not_called()

    def test_any_attempted_input_invalidates_the_snapshot_even_if_uncertain_or_zero(self):
        for req in INPUTS:
            for outcome in ('SENT', 'UNKNOWN', 'NOT_SENT'):
                with self.subTest(action=req['type'], outcome=outcome):
                    agent = self._agent()
                    tag = self._capture(agent)
                    agent.backend.insert.return_value = outcome
                    action_tag = tag if req['type'] in p.COORD_OPS else ABSENT
                    self._expect(self._submit(agent, req, tag=action_tag), p.RESULT_STATUS[outcome], outcome)
                    self._expect(self._submit(agent, CLICK, tag=tag), 412, 'STALE_SCREEN')
                    agent.backend.insert.assert_called_once()
        agent = self._agent()
        tag = self._capture(agent)
        self._expect(self._submit(agent, WAIT), 200, 'OK')
        self._expect(self._submit(agent, CLICK, tag=tag), 200, 'SENT')

    def test_coordinates_outside_and_secure_desktop_are_certain_refusals(self):
        agent = self._agent()
        tag = self._capture(agent)
        for change in (dict(x=-1), dict(y=480), dict(x=640)):
            self._expect(self._submit(agent, CLICK | change, tag=tag), 400, 'INVALID')
        drag = dict(type='drag', path=[dict(x=0, y=0), dict(x=640, y=0), dict(x=1, y=1)])
        self._expect(self._submit(agent, drag, tag=tag), 400, 'INVALID')
        agent.backend.desktop_is_default.side_effect = [True, False]
        token = secrets.token_hex(16)
        self._expect(self._submit(agent, CLICK, tag=tag, token=token), 503, 'NOT_DEFAULT_DESKTOP')
        self.assertEqual(agent.journal[token], 'NOT_SENT')
        self.assertEqual(agent.input_ops, 200)
        agent.backend.insert.assert_not_called()
        self.assertIsNone(agent.screen)
        # The screenshot from before the secure desktop is void even after it is gone.
        agent.backend.desktop_is_default.side_effect = None
        agent.backend.desktop_is_default.return_value = True
        self._expect(self._submit(agent, CLICK, tag=tag), 412, 'STALE_SCREEN')
        agent.backend.insert.assert_not_called()

    def test_capture_checks_desktop_both_times_and_size_and_png_limits(self):
        agent = self._agent()
        agent.backend.desktop_is_default.side_effect = [False, True, False, True, True]

        def capture():
            return json_result(raw_exchange(agent, wire_request(agent, 'GET', '/screenshot')))

        self._expect(capture(), 503, 'NOT_DEFAULT_DESKTOP')
        agent.backend.capture.assert_not_called()
        self._expect(capture(), 503, 'NOT_DEFAULT_DESKTOP')
        agent.backend.capture.assert_called_once()
        self.assertIsNone(agent.screen)
        agent.backend.capture.return_value = bytes(p.PNG_MAX + 1)
        self._expect(capture(), 413, 'TOO_LARGE')
        agent.backend.desktop_is_default.side_effect = None
        agent.backend.screen_size.side_effect = [(640, 480), (641, 480)]
        self._expect(capture(), 503, 'CAPTURE_FAILED')

    def test_journal_in_progress_duplicate_and_status_never_reexecute(self):
        agent = self._agent()
        token = secrets.token_hex(16)
        seen = []

        def unknown(_):
            seen.append(agent.status(token))
            return 'UNKNOWN'

        agent.backend.insert.side_effect = unknown
        self._expect(self._submit(agent, token=token), 503, 'UNKNOWN')
        self.assertEqual(seen, [('OK', {'state': 'IN_PROGRESS'})])
        self._expect(self._submit(agent, token=token), 409, 'DUPLICATE_OP', 'UNKNOWN')
        self._expect(self._status(agent, token), 200, 'OK', 'UNKNOWN')
        self._expect(self._status(agent, secrets.token_hex(16)), 404, 'NOT_FOUND')
        self._expect(self._status(agent, token, boot_id=secrets.token_hex(16)), 409, 'BOOT_MISMATCH')
        agent.backend.insert.assert_called_once()

    def test_idempotency_key_missing_malformed_or_duplicate_after_refusal(self):
        agent = self._agent()
        raw = p.encode(KEYPRESS)
        for value in (ABSENT, 'A' * 32, '0' * 31, '"' + '0' * 32 + '"', 'g' * 32):
            headers = action_headers(agent, value, raw)
            self._expect(json_result(raw_exchange(agent, wire_request(agent, 'POST', '/action',
                                     changes=headers, body=raw))), 400, 'INVALID')
        self.assertEqual(agent.journal, {})
        token = secrets.token_hex(16)
        self._expect(self._submit(agent, CLICK, token=token), 428, 'SCREEN_REQUIRED')
        tag = self._capture(agent)
        self._expect(self._submit(agent, CLICK, token=token, tag=tag), 409, 'DUPLICATE_OP', 'REFUSED')
        self._expect(self._status(agent, token), 200, 'OK', 'REFUSED')
        agent.backend.insert.assert_not_called()

    def test_boot_id_is_required_and_refuses_wrong_boot_before_input(self):
        agent = self._agent()
        raw = p.encode(KEYPRESS)
        headers = action_headers(agent, secrets.token_hex(16), raw)
        headers['SCN-Boot-Id'] = ABSENT
        request = wire_request(agent, 'POST', '/action', changes=headers, body=raw)
        self._expect(json_result(raw_exchange(agent, request)), 400, 'INVALID')
        for req in (KEYPRESS, dict(type='type', text='fixture'), WAIT):
            self._expect(self._submit(agent, req, boot_id=secrets.token_hex(16)), 409, 'BOOT_MISMATCH')
        agent.backend.insert.assert_not_called()

    def test_monotonic_expiry_interrupts_wait_and_never_starts_input(self):
        agent = self._agent(max_minutes=1)
        agent.clock.advance(59)
        self._expect(self._submit(agent, dict(type='wait', ms=10000)), 429, 'LIMIT_REACHED')
        self.assertEqual(agent.clock(), 60)
        self.assertTrue(agent.stopping)
        agent.backend.insert.assert_not_called()

    def test_request_deadline_blocks_input_after_preparation(self):
        agent = self._agent()

        def prepare(*_):
            agent.clock.advance(p.REQUEST_SECONDS)
            return object()

        agent.backend.prepare_input.side_effect = prepare
        with self.assertRaises(p.TransportError):
            self._submit(agent)
        agent.backend.insert.assert_not_called()

    def test_independent_operation_caps_and_bounded_journal(self):
        agent = self._agent(max_input_ops=1)
        token = secrets.token_hex(16)
        self._expect(self._submit(agent, token=token), 200, 'SENT')
        self._expect(self._status(agent, token), 200, 'OK', 'SENT')
        self._expect(self._submit(agent), 429, 'LIMIT_REACHED')
        self.assertTrue(agent.stopping)
        agent.backend.insert.assert_called_once()
        agent = self._agent()
        for _ in range(p.NON_INPUT_MAX - 1):
            self.assertTrue(agent.take_non_input())
        self._expect(self._submit(agent, dict(type='wait', ms=1)), 200, 'OK')
        self._expect(json_result(raw_exchange(agent, wire_request(agent, 'GET', '/screenshot'))),
                     429, 'LIMIT_REACHED')
        self.assertTrue(agent.stopping)
        agent.backend.capture.assert_not_called()
        agent = self._agent(tools=p.DEFAULT_TOOLS)
        with patch.object(a, 'MAX_RECORDED_OPS', 1):
            self._expect(self._submit(agent), 403, 'TOOL_DISABLED')
            self._expect(self._submit(agent), 429, 'LIMIT_REACHED')
        self.assertEqual(len(agent.journal), 1)

    def test_http_route_method_and_host_validation(self):
        agent = self._agent()
        for method, path, headers, status, result in (
            ('GET', '/missing', {}, 404, 'NOT_FOUND'),
            ('GET', '/probe?secret', {}, 400, 'INVALID'),
            ('HEAD', '/probe', {}, 405, 'METHOD_NOT_ALLOWED'),
            ('POST', '/screenshot', {}, 405, 'METHOD_NOT_ALLOWED'),
            ('GET', '/action', {}, 405, 'METHOD_NOT_ALLOWED'),
            ('GET', '/probe', {'Host': ABSENT}, 400, 'INVALID'),
            ('GET', '/probe', {'Host': 'wrong.invalid:4418'}, 400, 'INVALID'),
            ('GET', '/probe', {'Connection': 'keep-alive'}, 400, 'INVALID'),
            ('GET', '/probe', {'Content-Length': '0'}, 400, 'INVALID'),
            ('GET', '/probe', {'Expect': '100-continue'}, 400, 'INVALID'),
            ('GET', '/probe', {'Transfer-Encoding': 'chunked'}, 400, 'INVALID'),
        ):
            with self.subTest(method=method, path=path, request_headers=headers):
                self._expect(json_result(raw_exchange(agent, wire_request(agent, method, path,
                                                                          changes=headers))), status, result)
        self.assertEqual(agent.non_input_ops, p.NON_INPUT_MAX)
        response = raw_exchange(agent, wire_request(agent, 'GET', '/probe'))
        code, _, raw = split_response(response)
        obj = p.decode(raw)
        self.assertEqual(obj['remaining']['non_input_ops'], p.NON_INPUT_MAX - 1)
        p.validate_response(obj, code, 'probe')

    def test_http_body_size_and_media_are_checked_before_read(self):
        agent = self._agent()
        raw = p.encode(WAIT)
        for change, body, status, result in (
            ({'Content-Length': ABSENT}, raw, 411, 'LENGTH_REQUIRED'),
            ({'Content-Length': '-1'}, raw, 400, 'INVALID'),
            ({'Content-Length': '1, 2'}, raw, 400, 'INVALID'),
            ({'Content-Length': str(p.REQUEST_MAX + 1)}, raw.ljust(p.REQUEST_MAX + 1), 413, 'TOO_LARGE'),
            ({'Content-Length': '9' * 200}, b'', 413, 'TOO_LARGE'),
            ({'Content-Type': 'text/plain'}, raw, 415, 'UNSUPPORTED_MEDIA_TYPE'),
            ({'Content-Type': ABSENT}, raw, 415, 'UNSUPPORTED_MEDIA_TYPE'),
            ({'Content-Type': 'application/json; charset=utf-8'}, raw, 415, 'UNSUPPORTED_MEDIA_TYPE'),
        ):
            with self.subTest(change=change):
                headers = action_headers(agent, secrets.token_hex(16), raw) | change
                request = wire_request(agent, 'POST', '/action', changes=headers, body=body)
                self._expect(json_result(raw_exchange(agent, request)), status, result)
        self.assertEqual(agent.journal, {})
        self.assertEqual(agent.non_input_ops, p.NON_INPUT_MAX)

    def test_http_json_body_is_strict(self):
        agent = self._agent()
        for raw in (b'{"type":"wait","type":"wait"}', b'{"type":"wait","ms":1.5}',
                    b'{"type":"wait","ms":NaN}', b'{"type":"wait","extra":0}', b'{"type":"wait","ms":[1]}',
                    b'{"type":"screenshot"}', b'{"type":"zoom"}', b'{"type":"wait"}{}',
                    b'{"type":"type","text":"\xff"}', b'', b'[]'):
            headers = action_headers(agent, secrets.token_hex(16), raw)
            request = wire_request(agent, 'POST', '/action', changes=headers, body=raw)
            self._expect(json_result(raw_exchange(agent, request)), 400, 'INVALID')
        self.assertEqual(agent.non_input_ops, p.NON_INPUT_MAX)
        agent.backend.insert.assert_not_called()

    def test_http_smuggling_shapes_and_no_reflection(self):
        agent = self._agent()
        marker = 'sensitive-' + secrets.token_hex(8)
        raw = p.encode(WAIT)
        headers = action_headers(agent, secrets.token_hex(16), raw)
        base = wire_request(agent, 'POST', '/action', changes=headers, body=raw)
        for req in (
            wire_request(agent, 'POST', '/action', changes=headers, extra=[('content-length', str(len(raw)))],
                         body=raw),
            wire_request(agent, 'POST', '/action', changes=headers, extra=[('Transfer-Encoding', 'chunked')],
                         body=raw),
            wire_request(agent, 'POST', '/action', changes=headers, extra=[('Expect', '100-continue')], body=raw),
            wire_request(agent, 'POST', '/action', changes=headers, extra=[('If-Match', '"one"'),
                         ('if-match', '"two"')], body=raw),
            base.replace(b'Host:', b'Host :'),
            base.replace(b'Host:', (f'Fake: {marker}\r\n Host:').encode()),
            base.replace(b'\r\n', b'\n'),
            base.replace(b'HTTP/1.1', b'HTTP/1.0'),
            b'GET /probe\r\n\r\n',
            base.replace(b'/action', f'https://{agent.host}/action'.encode()),
            base.replace(b'/action', b'/' + b'x' * p.HEADER_MAX),
            base.replace(b'Host:', b'X-Long: ' + b'x' * p.HEADER_MAX + b'\r\nHost:'),
            wire_request(agent, 'GET', '/probe', extra=[(f'X-{i}', marker) for i in range(p.HEADER_COUNT)]),
            wire_request(agent, 'GET', '/probe', extra=[('X-Unexpected', marker)]),
            wire_request(agent, 'POST', '/action', changes=headers, extra=[('X-Unexpected', marker)], body=raw),
        ):
            events = []
            response = raw_exchange(agent, req, events.append)
            self._expect(json_result(response), 400, 'INVALID')
            self.assertEqual(response.count(b'HTTP/1.1'), 1)
            self.assertNotIn(marker.encode(), response)
            self.assertNotIn(headers['Idempotency-Key'].encode(), response)
            self.assertNotIn(marker, '\n'.join(events))
            _, reply_headers, _ = split_response(response)
            self.assertEqual(set(reply_headers), {'connection', 'content-type', 'content-length'})
        agent.backend.insert.assert_not_called()
        self.assertEqual(agent.journal, {})

    def test_http_pipelined_second_request_never_executes(self):
        agent = self._agent()
        raw = p.encode(KEYPRESS)
        action = wire_request(agent, 'POST', '/action',
                              changes=action_headers(agent, secrets.token_hex(16), raw), body=raw)
        response = raw_exchange(agent, wire_request(agent, 'GET', '/probe') + action)
        self._expect(json_result(response), 200, 'OK')
        self.assertEqual(response.count(b'HTTP/1.1'), 1)
        agent.backend.insert.assert_not_called()

    def test_header_count_is_bounded_by_itself(self):
        def block(count):
            lines = ''.join(f'X-{i}: 1\r\n' for i in range(count))
            return io.BytesIO(f'GET / HTTP/1.1\r\n{lines}\r\n'.encode())

        self.assertEqual(len(p.read_head(block(p.HEADER_COUNT))[1]), p.HEADER_COUNT)
        with self.assertRaises(p.ProtocolError):
            p.read_head(block(p.HEADER_COUNT + 1))

    def test_console_names_each_operation_from_the_fixed_list(self):
        agent = self._agent()
        seen = []
        marker = 'sensitive-' + secrets.token_hex(8)
        raw_exchange(agent, wire_request(agent, 'GET', '/probe'), seen.append)
        raw_exchange(agent, wire_request(agent, 'GET', '/screenshot'), seen.append)
        self._submit(agent, dict(type='type', text=marker), output=seen.append)
        self._submit(agent, dict(type=marker), output=seen.append)
        raw_exchange(agent, wire_request(agent, 'GET', '/' + marker), seen.append)
        names = [line.split(' ')[1:3] for line in seen if ' handshake ' not in line]
        self.assertEqual(names, [['probe', 'OK'], ['screenshot', 'OK'], ['type', 'SENT'],
                                 ['action', 'INVALID'], ['request', 'NOT_FOUND']])
        self.assertNotIn(marker, '\n'.join(seen))
