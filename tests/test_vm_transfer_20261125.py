"""Stage 4a against the real protocol, with in-memory TLS and synthetic files."""

import ctypes
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import secrets
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests._inherited_test_temp import inherited_test_temp
from tests.test_vm_screen_agent_20261125 import (
    ABSENT, ALL_TOOLS, KEYPRESS, action_headers, arguments, backend, raw_exchange, split_response, wire_request,
)
from tests.test_vm_screen_client_20261125 import responder, response_bytes
from tests.test_vm_screen_protocol_20261125 import KEY, NativeIsolatedTest, paired, private_pair
from sora_commander_next import vm_agent as a, vm_client as c, vm_files as f, vm_protocol as p, vm_transfer as t


WINDOWS_LOADER = getattr(ctypes, 'WinDLL', None)
FREE_BYTES = f.free_bytes
BAD_NAMES = ('', '.', '..', '.hidden', 'a/b', 'a\\b', 'a:b', 'a.', ' a', 'a ', 'con',
             'NUL.txt', 'Com1.log', 'LPT².csv', 'COM¹.txt', 'con .txt', 'AUX', 'x' * 121,
             '😀' * 61, 'e\u0301', 'a\u202e', 'a\u2028', 'a\u2029', 'a\ue000', 'a\u0378',
             'a\ud800', 'a\n', 'a\x7f', 'a<', 'a>', 'a"', 'a|', 'a?', 'a*')


def allow_file_loader():
    if WINDOWS_LOADER is not None:
        guarded = ctypes.WinDLL

        def load(name, *args, **kwargs):
            if name in ('kernel32', 'advapi32'):
                return WINDOWS_LOADER(name, *args, **kwargs)
            return guarded(name, *args, **kwargs)

        return patch.object(ctypes, 'WinDLL', side_effect=load)
    return patch.object(ctypes, 'WinDLL', create=True)


class TransferTests(NativeIsolatedTest):
    def setUp(self):
        super().setUp()
        self.enterContext(allow_file_loader())
        self.root = Path(self.enterContext(inherited_test_temp()))
        self.enterContext(patch.dict(os.environ, USERPROFILE=str(self.root), LOCALAPPDATA=str(self.root)))
        self.enterContext(patch.object(p, 'FILE_MAX', 12))
        self.enterContext(patch.object(p, 'CHUNK_MAX', 4))
        self.enterContext(patch.object(p, 'FILE_TOTAL_MAX', 24))
        self.enterContext(patch.object(p, 'FILE_FREE_MIN', 10))
        self.space = self.enterContext(patch.object(f, 'free_bytes', return_value=100))
        for name in ('inbox', 'outbox'):
            (self.root / name).mkdir()
        self.agent = self.new_agent()
        self.store = self.agent.files
        self.events = []

    def new_agent(self, tools=ALL_TOOLS):
        agent = a.Agent(arguments(tools=tools), KEY, backend(), (640, 480), files=f.FileStore(str(self.root)))
        self.addCleanup(agent.close_files)
        return agent

    def link(self, source, name):
        # The fixture refuses links on purpose, so the test that made one removes it first.
        os.link(source, name)
        self.addCleanup(name.unlink)

    def wire(self, method, path, headers=None, body=b''):
        if path.startswith(('/files', '/uploads')):
            headers = {'SCN-Boot-Id': self.agent.boot_id, **(headers or {})}
        return split_response(raw_exchange(self.agent, wire_request(self.agent, method, path, changes=headers,
                                                                    body=body), self.events.append))

    def result(self, method, path, headers=None, body=b''):
        status, _, raw = self.wire(method, path, headers, body)
        return status, p.decode(raw, max_depth=3)

    def post(self, name='fixture', data=b'data', *, key=None, boot=None, length=None, digest=None):
        body = p.encode(dict(name=name, length=len(data) if length is None else length,
                             sha256=hashlib.sha256(data).hexdigest() if digest is None else digest))
        headers = {'SCN-Boot-Id': self.agent.boot_id if boot is None else boot,
                   'Idempotency-Key': key or secrets.token_hex(16), 'Content-Type': 'application/json',
                   'Content-Length': str(len(body))}
        return self.result('POST', '/uploads', headers, body)

    def begin(self, *args, **kwargs):
        status, reply = self.post(*args, **kwargs)
        self.assertEqual((status, reply['result'], reply['offset']), (201, 'CREATED', 0))
        return '/uploads/' + reply['id']

    def chunk(self, path, data, offset=0, *, final=True, digest=None, media='application/partial-upload'):
        return self.result('PATCH', path, {'Content-Type': media, 'Content-Length': str(len(data)),
                                           'Upload-Offset': str(offset), 'Upload-Complete': '?1' if final else '?0',
                                           'Content-Digest': p.digest(data) if digest is None else digest}, data)

    def code(self, response, status, result):
        self.assertEqual((response[0], response[1]['result']), (status, result))

    def empty_partial(self):
        self.assertEqual(list(self.store.partial.iterdir()), [])

    def test_post_validation_order_and_no_side_effect(self):
        self.agent.tools = {'screenshot'}
        self.code(self.post('../bad', boot='0' * 32, length=13), 409, 'BOOT_MISMATCH')
        self.code(self.post('../bad', length=13), 403, 'TOOL_DISABLED')
        self.agent.tools = p.TOOLS
        self.agent.file_bytes = 3
        self.code(self.post('../bad', length=13), 400, 'NAME_NOT_ALLOWED')
        for length in (-1, True, '4'):
            self.code(self.post(length=length), 400, 'INVALID')
        self.code(self.post(length=13), 413, 'FILE_TOO_LARGE')
        self.code(self.post(length=12), 429, 'TRANSFER_LIMIT')
        self.space.assert_not_called()
        self.agent.file_bytes = 24
        (self.store.inbox / 'FIXTURE').write_bytes(b'old')
        self.space.return_value = 13
        self.code(self.post(), 507, 'DISK_FULL')
        self.space.assert_called_with(self.store.root)
        self.space.side_effect = OSError('measurement failed')
        self.code(self.post(), 507, 'DISK_FULL')
        self.space.side_effect = None
        self.space.return_value = 14
        self.code(self.post(), 412, 'FILE_EXISTS')
        self.code(self.post('a', digest='bad'), 400, 'INVALID')
        self.assertEqual((self.store.inbox / 'FIXTURE').read_bytes(), b'old')
        self.assertEqual(self.agent.file_bytes, 24)
        self.empty_partial()

    def test_duplicate_busy_cancel_and_stale_id(self):
        key = secrets.token_hex(16)
        path = self.begin('first', b'123456', key=key)
        self.code(self.post('different', key=key), 409, 'DUPLICATE_OP')
        self.code(self.post('different'), 409, 'TRANSFER_BUSY')
        self.code(self.chunk(path, b'1234', final=False), 200, 'OK')
        self.code(self.result('DELETE', path), 200, 'OK')
        self.code(self.result('GET', path), 404, 'NOT_FOUND')
        self.code(self.chunk(path, b'56', 4), 404, 'NOT_FOUND')
        self.code(self.result('DELETE', path), 404, 'NOT_FOUND')
        self.code(self.post('first', key=key), 409, 'DUPLICATE_OP')
        self.assertEqual(self.agent.file_bytes, 20)
        self.empty_partial()

    def test_exact_limits_and_request_cap_are_independent(self):
        path = self.begin(data=b'123456789012')
        self.agent.non_input_ops = 0
        self.agent.transfer_requests = 4
        self.code(self.chunk(path, b'1234', final=False), 200, 'OK')
        self.code(self.chunk(path, b'5678', 4, final=False), 200, 'OK')
        self.code(self.chunk(path, b'9012', 8), 200, 'OK')
        self.assertEqual(self.result('GET', path)[1]['offset'], 12)
        self.code(self.result('GET', '/files'), 429, 'TRANSFER_LIMIT')
        self.assertEqual(self.agent.non_input_ops, 0)
        self.assertEqual((self.store.inbox / 'fixture').read_bytes(), b'123456789012')

    def test_patch_drains_before_cap_and_header_refusals(self):
        target = self.begin()
        self.code(self.chunk(target, b'data', media='text/plain'), 415, 'UNSUPPORTED_MEDIA_TYPE')
        self.agent.transfer_requests = 0
        self.code(self.chunk(target, b'data'), 429, 'TRANSFER_LIMIT')
        reader = Mock(read=Mock(return_value=b'data'))
        handler = SimpleNamespace(agent=self.agent)
        a.Handler.transfer(handler, 'upload_patch', reader, {'content-length': '4'})
        reader.read.assert_called_once_with(4)
        self.assertEqual(self.agent.upload.offset, 0)

    def test_offset_digest_size_and_completion_checks_before_append(self):
        path = self.begin(data=b'abcdef')
        self.code(self.chunk(path, b'12345', final=False), 413, 'TOO_LARGE')
        status, reply = self.chunk(path, b'abc', 1, final=False)
        self.code((status, reply), 409, 'OFFSET_MISMATCH')
        self.assertEqual(reply['offset'], 0)
        self.code(self.chunk(path, b'abc', final=False, digest=p.digest(b'bad')), 400, 'DIGEST_MISMATCH')
        self.code(self.chunk(path, b'', final=False), 409, 'LENGTH_MISMATCH')
        self.code(self.chunk(path, b'abc', final=True), 409, 'LENGTH_MISMATCH')
        self.assertEqual(next(self.store.partial.iterdir()).stat().st_size, 0)
        self.code(self.chunk(path, b'abcd', final=False), 200, 'OK')
        self.code(self.chunk(path, b'efx', 4), 409, 'LENGTH_MISMATCH')
        self.code(self.chunk(path, b'efx', 4, final=False), 409, 'LENGTH_MISMATCH')
        self.assertEqual(self.result('GET', path)[1]['offset'], 4)
        with f.plain_reader(next(self.store.partial.iterdir())) as (stream, _):
            self.assertEqual(stream.read(), b'abcd')
        self.code(self.chunk(path, b'ef', 4), 200, 'OK')
        self.assertEqual(self.result('GET', path)[1]['complete'], True)

    def test_bad_whole_digest_and_disk_error_discard(self):
        path = self.begin(data=b'good')
        self.code(self.chunk(path, b'evil'), 400, 'DIGEST_MISMATCH')
        self.empty_partial()
        self.assertEqual(self.agent.file_bytes, 20)
        self.code(self.result('GET', path), 404, 'NOT_FOUND')
        path = self.begin()
        with patch.object(f.Upload, 'append', side_effect=OSError('disk')):
            self.code(self.chunk(path, b'data'), 503, 'FILE_FAILED')
        self.code(self.result('GET', path), 404, 'NOT_FOUND')
        path = self.begin()
        with patch.object(f.os, 'fsync', side_effect=OSError('disk')):
            self.code(self.chunk(path, b'data'), 503, 'FILE_FAILED')
        self.empty_partial()
        self.assertEqual(list(self.store.inbox.iterdir()), [])

    def test_final_publication_does_not_replace_a_late_collision(self):
        path = self.begin('fixture')
        target = self.store.inbox / 'FIXTURE'
        target.write_bytes(b'old')
        self.code(self.chunk(path, b'data'), 412, 'FILE_EXISTS')
        self.assertEqual(target.read_bytes(), b'old')
        self.empty_partial()

    def test_empty_upload_and_download(self):
        path = self.begin(data=b'')
        self.code(self.chunk(path, b'', final=False), 409, 'LENGTH_MISMATCH')
        self.code(self.chunk(path, b''), 200, 'OK')
        self.assertEqual((self.store.inbox / 'fixture').stat().st_size, 0)
        (self.store.outbox / 'empty').touch()
        status, headers, data = self.wire('GET', '/files/empty', {'Range': 'bytes=0-3'})
        self.assertEqual((status, headers['content-range'], headers['content-digest'], data),
                         (206, 'bytes */0', p.digest(b''), b''))

    def test_names_listing_and_canonical_url(self):
        for name in BAD_NAMES:
            with self.subTest(name=repr(name)):
                self.assertFalse(p.is_file_name(name))
        for name in ('日本語😀.bin', 'two words', 'é.bin', '😀' * 60, '-a', '~a', 'a%2f'):
            self.assertTrue(p.is_file_name(name))
            self.assertEqual(p.path_name(p.file_path(name)), name)
        name = '日本語😀.bin'
        self.code(self.chunk(self.begin(name), b'data'), 200, 'OK')
        (self.store.outbox / name).write_bytes(b'1234')
        (self.store.outbox / '.hidden').touch()
        (self.store.outbox / 'e\u0301').touch()
        _, reply = self.result('GET', '/files')
        self.assertEqual(reply['inbox'], [dict(name=name, size=4)])
        self.assertEqual(reply['outbox'], [dict(name=name, size=4)])
        self.assertEqual(self.wire('GET', p.file_path(name), {'Range': 'bytes=0-3'})[2], b'1234')
        for spelling in ('%61', '%7E', '%c3%a9', '%FF', '%2F', '%2e%2e', '%', 'a/b', 'a%00'):
            self.code(self.result('GET', '/files/' + spelling, {'Range': 'bytes=0-3'}), 400, 'NAME_NOT_ALLOWED')
        self.assertNotIn(name, '\n'.join(self.events))

    def test_legacy_put_and_whole_get_refused(self):
        self.code(self.result('PUT', '/files/old'), 405, 'METHOD_NOT_ALLOWED')
        self.code(self.result('GET', '/files/old'), 416, 'RANGE_NOT_ALLOWED')
        self.code(self.result('HEAD', '/uploads/' + '0' * 32), 405, 'METHOD_NOT_ALLOWED')
        self.empty_partial()

    def test_tools_isolation_headers_and_store_errors(self):
        self.agent.tools = {'screenshot'}
        self.code(self.result('GET', '/files'), 403, 'TOOL_DISABLED')
        self.code(self.result('DELETE', '/uploads/' + 'a' * 32), 403, 'TOOL_DISABLED')
        self.agent.tools.add('file_put')
        path = self.begin()
        self.code(self.result('GET', '/files'), 200, 'OK')
        self.code(self.result('GET', '/files/fixture', {'Range': 'bytes=0-3'}), 403, 'TOOL_DISABLED')
        self.code(self.result('PATCH', path, {'Content-Type': 'application/partial-upload',
                                             'Content-Length': '4', 'Other': 'hidden'}, b'data'),
                  400, 'INVALID')
        self.code(self.result('DELETE', path), 200, 'OK')
        self.agent.tools = {'screenshot', 'file_get'}
        self.code(self.post(), 403, 'TOOL_DISABLED')
        (self.store.inbox / 'fixture').write_bytes(b'data')
        self.code(self.result('GET', '/files/fixture', {'Range': 'bytes=0-3'}), 404, 'FILE_NOT_FOUND')
        (self.store.outbox / 'large').write_bytes(b'x' * 13)
        self.code(self.result('GET', '/files/large', {'Range': 'bytes=0-3'}), 413, 'FILE_TOO_LARGE')
        (self.store.outbox / 'directory').mkdir()
        self.code(self.result('GET', '/files/directory', {'Range': 'bytes=0-3'}), 503, 'FILE_FAILED')
        with patch.object(self.store, 'listing', side_effect=RuntimeError('private')):
            self.code(self.result('GET', '/files'), 503, 'FILE_FAILED')
        self.assertNotIn('private', '\n'.join(self.events))

    def test_range_condition_digest_and_changed_file(self):
        path = self.store.outbox / 'report'
        path.write_bytes(b'abcdefgh')
        status, headers, data = self.wire('GET', '/files/report', {'Range': 'bytes=0-3'})
        self.assertEqual((status, data, headers['content-range'], headers['content-digest']),
                         (206, b'abcd', 'bytes 0-3/8', p.digest(b'abcd')))
        tag = headers['etag']
        p.strong_tag(tag)
        self.code(self.result('GET', '/files/report', {'Range': 'bytes=4-7'}), 412, 'FILE_CHANGED')
        self.assertEqual(self.wire('GET', '/files/report', {'Range': 'bytes=4-7', 'If-Match': tag})[2], b'efgh')
        for invalid in ('bytes=0-4', 'bytes=4-0', 'bytes=-2', 'bytes=0-', 'bytes=0-1,2-3'):
            self.code(self.result('GET', '/files/report', {'Range': invalid}), 416, 'RANGE_NOT_ALLOWED')
        path.write_bytes(b'123456789')
        self.code(self.result('GET', '/files/report', {'Range': 'bytes=4-7', 'If-Match': tag}), 412, 'FILE_CHANGED')

    def test_range_budget_busy_and_identity_checks(self):
        path = self.store.outbox / 'report'
        path.write_bytes(b'data')
        headers = {'Range': 'bytes=0-3'}
        self.agent.file_bytes = 3
        self.code(self.result('GET', '/files/report', headers), 429, 'TRANSFER_LIMIT')
        self.agent.file_bytes = 4
        failure = OSError('sharing violation')
        failure.winerror = 32
        with patch.object(f, '_read_fd', side_effect=failure):
            self.code(self.result('GET', '/files/report', headers), 503, 'FILE_BUSY')
        with patch.object(f, 'unchanged', return_value=False):
            self.code(self.result('GET', '/files/report', headers), 412, 'FILE_CHANGED')
        wrong = self.store.outbox / 'other'
        wrong.write_bytes(b'data')
        with patch.object(f.os, 'fstat', return_value=wrong.stat()):
            self.code(self.result('GET', '/files/report', headers), 503, 'FILE_FAILED')
        self.assertEqual(self.agent.file_bytes, 4)
        self.assertEqual(self.wire('GET', '/files/report', headers)[0], 206)
        self.assertEqual(self.agent.file_bytes, 0)

    def test_links_in_sources_and_outbox(self):
        path = self.store.outbox / 'report'
        path.write_bytes(b'data')
        self.link(path, self.root / 'second')
        self.code(self.result('GET', '/files/report', {'Range': 'bytes=0-3'}), 503, 'FILE_FAILED')
        self.assertEqual(self.result('GET', '/files')[1]['outbox'], [])
        with self.assertRaisesRegex(f.StoreError, '^FILE_FAILED$'):
            with t.open_upload(path, 'report', self.agent.boot_id):
                self.fail('linked source was accepted')

    def test_reparse_markers_in_every_directory_and_download_file(self):
        file_path = self.store.outbox / 'report'
        file_path.write_bytes(b'data')
        lstat = os.lstat
        for path in (self.root, self.store.inbox, self.store.outbox, self.store.partial, file_path):
            with self.subTest(directory=path.is_dir()):
                def reparse(candidate, *args, **kwargs):
                    actual = lstat(candidate, *args, **kwargs)
                    if Path(candidate) != path:
                        return actual
                    return SimpleNamespace(st_mode=actual.st_mode, st_nlink=actual.st_nlink,
                                           st_file_attributes=f.REPARSE)

                with patch.object(f.os, 'lstat', side_effect=reparse):
                    with self.assertRaisesRegex(f.StoreError, '^FILE_FAILED$'):
                        if path == file_path:
                            self.store.read_range('report', 0, 3, None, self.agent.boot_id, 4)
                        else:
                            f.FileStore(str(self.root))

    def test_cleanup_and_restart_preserve_parts_with_no_live_handle(self):
        others = [self.store.partial / name for name in (
            'other.part', 'scn-upload-' + 'c' * 31 + '.part', 'scn-upload-' + 'd' * 32 + '.part.bak')]
        for other in others:
            other.write_bytes(b'owner')
        keep = self.store.partial / 'keep.part'
        keep.write_bytes(b'keep')
        directory = self.store.partial / ('scn-upload-' + 'a' * 32 + '.part')
        directory.mkdir()
        linked = self.store.partial / ('scn-upload-' + 'b' * 32 + '.part')
        self.link(keep, linked)
        path = self.begin(data=b'123456')
        self.code(self.chunk(path, b'1234', final=False), 200, 'OK')
        previous = self.agent
        abandoned = self.store._part(previous.upload.id)
        previous.upload.handle.close()  # Simulate a process that stopped without cleanup.
        self.agent = self.new_agent()
        self.code(self.result('GET', path), 404, 'NOT_FOUND')
        self.code(self.result('DELETE', path), 404, 'NOT_FOUND')
        self.assertEqual(set(self.store.partial.iterdir()), {keep, directory, linked, abandoned, *others})
        path = self.begin(data=b'next')
        self.assertIsNone(self.agent.close_files())
        self.assertEqual(set(self.store.partial.iterdir()), {keep, directory, linked, abandoned, *others})

    def test_screenshot_probe_and_input_between_upload_chunks(self):
        path = self.begin(data=b'abcdef')
        self.code(self.chunk(path, b'abcd', final=False), 200, 'OK')
        self.assertEqual(self.result('GET', '/probe')[1]['version'], 5)
        self.assertEqual(self.wire('GET', '/screenshot')[0], 200)
        raw = p.encode(KEYPRESS)
        self.code(self.result('POST', '/action', action_headers(self.agent, secrets.token_hex(16), raw), raw),
                  200, 'SENT')
        self.code(self.chunk(path, b'ef', 4), 200, 'OK')
        self.agent.backend.insert.assert_called_once()

    def send(self, operation, request, handler=None):
        method, path, headers, body = request
        headers = {'Host': self.agent.host, 'Connection': 'close', **headers}
        if body is not None:
            headers['Content-Length'] = str(len(body))
        handler = handler or (lambda raw: self.agent.connection(raw, self.events.append))
        with paired(handler) as raw:
            return c.exchange(raw, p.tls_context(KEY, server_side=False), operation, (method, path, headers, body),
                              self.events.append)

    def test_budget_spent_after_creation_refuses_the_chunk_before_it_is_appended(self):
        path = self.begin(data=b'abcd')
        self.agent.file_bytes = 3
        self.code(self.chunk(path, b'abcd'), 429, 'TRANSFER_LIMIT')
        self.assertEqual((self.agent.upload.offset, self.agent.file_bytes), (0, 3))
        self.assertEqual(next(self.store.partial.iterdir()).stat().st_size, 0)

    def test_a_part_changed_by_another_program_is_never_published(self):
        path = self.begin(data=b'abcdef')
        self.code(self.chunk(path, b'abcd', final=False), 200, 'OK')
        with self.assertRaises(PermissionError):
            with next(self.store.partial.iterdir()).open('ab') as other:
                other.write(b'x')
        self.code(self.chunk(path, b'ef', 4), 200, 'OK')
        self.empty_partial()
        self.assertEqual(next(self.store.inbox.iterdir()).read_bytes(), b'abcdef')

    def test_guest_cannot_replace_part_at_publication_or_delete_a_reused_name(self):
        upload = self.store.create('report', 4, hashlib.sha256(b'data').hexdigest())
        self.addCleanup(upload.handle.close)
        part = self.store._part(upload.id)
        self.store.append(upload, b'data')

        def try_rename():
            with self.assertRaises(PermissionError):
                part.rename(part.with_suffix('.other'))

        self.store.finish(upload, try_rename)
        self.assertEqual((self.store.inbox / 'report').read_bytes(), b'data')
        part.write_bytes(b'other')
        self.store.discard(upload)
        self.assertEqual(part.read_bytes(), b'other')

    def test_host_keeps_the_verified_part_open_until_publication(self):
        (self.store.outbox / 'report').write_bytes(b'abc')
        target = self.root / 'result'
        with t.open_download('report', target, self.agent.boot_id) as download:
            download.step(self.send)

            def try_rename():
                with self.assertRaises(PermissionError):
                    download.part.rename(download.part.with_suffix('.other'))

            download.publish(lambda _: None, check=try_rename)
        self.assertEqual(target.read_bytes(), b'abc')

    def test_the_published_download_retains_the_real_windows_zone_stream(self):
        (self.store.outbox / 'report').write_bytes(b'abc')
        target = self.root / 'result'
        with t.open_download('report', target, self.agent.boot_id) as download:
            download.step(self.send)
            download.publish(c._mark)
        self.assertEqual(target.read_bytes(), b'abc')
        with open(f'{target}:Zone.Identifier', 'rb') as stream:
            self.assertEqual(stream.read(), b'[ZoneTransfer]\r\nZoneId=3\r\n')

    def test_download_refuses_a_replaced_staging_name_and_leaves_the_replacement(self):
        (self.store.outbox / 'report').write_bytes(b'abc')
        target = self.root / 'result'
        pin_part = f.pin_part
        with self.assertRaisesRegex(f.StoreError, '^FILE_CHANGED$'):
            with t.open_download('report', target, self.agent.boot_id) as download:
                download.step(self.send)
                part = download.part
                moved = part.with_suffix('.other')

                @contextmanager
                def pin(handle, *, delete_access):
                    if delete_access and not moved.exists():
                        part.rename(moved)
                        part.write_bytes(b'other')
                    with pin_part(handle, delete_access=delete_access) as native:
                        yield native

                with patch.object(f, 'pin_part', pin):
                    download.publish(lambda _: None)
        self.assertEqual(part.read_bytes(), b'other')
        self.assertFalse(moved.exists())
        self.assertFalse(target.exists())

    def test_a_tag_is_bound_to_one_agent_run(self):
        (self.store.outbox / 'report').write_bytes(b'abcdefgh')
        tag = self.wire('GET', '/files/report', {'Range': 'bytes=0-3'})[1]['etag']
        self.agent = self.new_agent()
        self.code(self.result('GET', '/files/report', {'Range': 'bytes=4-7', 'If-Match': tag}), 412, 'FILE_CHANGED')

    def test_every_transfer_request_names_this_agent_run_before_any_file_is_touched(self):
        path = self.begin(data=b'abcdef')
        (self.store.outbox / 'report').write_bytes(b'abcd')
        budget = self.agent.file_bytes
        requests = (('GET', '/files', {}, b''), ('GET', '/files/report', {'Range': 'bytes=0-3'}, b''),
                    ('GET', path, {}, b''), ('DELETE', path, {}, b''),
                    ('PATCH', path, {'Content-Type': 'application/partial-upload', 'Content-Length': '4',
                                     'Upload-Offset': '0', 'Upload-Complete': '?0',
                                     'Content-Digest': p.digest(b'abcd')}, b'abcd'))
        with patch.object(f, 'plain_reader', side_effect=AssertionError('file touched')), \
                patch.object(self.store, 'listing', side_effect=AssertionError('folder touched')):
            for method, target, headers, body in requests:
                for boot, status, result in (('0' * 32, 409, 'BOOT_MISMATCH'), (ABSENT, 400, 'INVALID'),
                                             ('short', 400, 'INVALID')):
                    with self.subTest(method=method, target=target, boot=boot):
                        self.code(self.result(method, target, {**headers, 'SCN-Boot-Id': boot}, body), status, result)
        self.assertEqual((self.agent.upload.offset, self.agent.file_bytes), (0, budget))

    def test_cli_asks_for_the_boot_id_once_and_uses_a_given_one(self):
        (self.store.outbox / 'report').write_bytes(b'abc')
        self.assertEqual(self.cli('files')[0], 0)
        self.assertEqual(self.cli('files', '--boot-id', '0' * 32), (c.REFUSED_EXIT, ['BOOT_MISMATCH']))
        self.assertEqual(self.cli('get', '--boot-id', '0' * 32, '--name', 'report', '--out',
                                  str(self.root / 'result')), (c.REFUSED_EXIT, ['BOOT_MISMATCH']))
        self.assertFalse((self.root / 'result').exists())
        self.assertEqual(sum(' probe OK' in event for event in self.events), 1)

    def test_free_bytes_asks_the_real_volume(self):
        # The fixture replaces the measurement, so ask the original once. Read-only.
        self.assertGreater(FREE_BYTES(self.root), 0)
        with self.assertRaises(OSError):
            FREE_BYTES(self.root / 'missing' / 'folder')

    def upload(self, data=b'abcdef'):
        source = self.root / 'source'
        source.write_bytes(data)
        return t.open_upload(source, 'a', self.agent.boot_id)

    def test_host_gives_up_after_a_second_loss_and_checks_the_reported_offset(self):
        def lost(operation, request):
            if operation == 'upload_patch':
                raise t.UncertainTransfer()
            return self.send(operation, request)

        with self.upload() as upload:
            for _ in range(3):  # create, lost, status
                upload.step(lost)
            with self.assertRaises(t.UncertainTransfer):
                upload.step(lost)
            upload.cancel(self.send)
        for offset, length, complete in ((3, 6, False), (0, 5, False), (6, 6, True)):
            with self.subTest(offset=offset, length=length):
                def lying(operation, request):
                    if operation == 'upload_status':
                        return dict(result='OK', offset=offset, length=length, complete=complete)
                    return lost(operation, request)

                with self.upload() as upload:
                    upload.step(lying)
                    upload.step(lying)
                    with self.assertRaises(p.ProtocolError):
                        upload.step(lying)
                    upload.cancel(self.send)
        self.empty_partial()

    def test_host_refuses_large_and_changing_sources(self):
        with self.assertRaisesRegex(f.StoreError, '^FILE_TOO_LARGE$'):
            with self.upload(b'x' * 13):
                self.fail('a large source was accepted')
        source = self.root / 'source'
        source.write_bytes(b'abcdef')
        calls = []

        def check():
            calls.append(1)
            if len(calls) == 2:
                with source.open('r+b') as handle:
                    handle.write(b'ABC')
                os.utime(source, ns=(1, 1))

        with self.assertRaisesRegex(f.StoreError, '^FILE_CHANGED$'):
            with t.open_upload(source, 'a', self.agent.boot_id, check=check):
                self.fail('a changed source was accepted')

    def test_host_checks_every_range_reply_and_the_space(self):
        target = self.root / 'result'

        def reply(data, span, tag='"first"'):
            return lambda *_: dict(result='OK', data=data, content_digest=p.digest(data), content_range=span, etag=tag)

        for data, span in ((b'abcd', 'bytes 1-3/8'), (b'abc', 'bytes 0-3/8'), (b'', 'bytes 0-3/8')):
            with self.subTest(span=span, data=data):
                with self.assertRaises(p.ProtocolError):
                    with t.open_download('report', target, self.agent.boot_id) as download:
                        download.step(reply(data, span))
                self.assertFalse(download.part.exists())
        self.space.return_value = 17
        with self.assertRaisesRegex(f.StoreError, '^DISK_FULL$'):
            with t.open_download('report', target, self.agent.boot_id) as download:
                download.step(reply(b'abcd', 'bytes 0-3/8'))
        self.assertFalse(download.part.exists())
        self.assertFalse(target.exists())

    def test_host_refuses_a_part_the_marker_changed(self):
        (self.store.outbox / 'report').write_bytes(b'abc')
        target = self.root / 'result'

        def marker(part):
            with part.open('ab') as handle:
                handle.write(b'x')

        code, output = self.cli('get', '--name', 'report', '--out', str(target), marker=marker)
        self.assertEqual((code, output[-1]), (c.REFUSED_EXIT, 'MARK_FAILED'))
        self.assertEqual([path.name for path in self.root.iterdir() if path.is_file()], [])

    def test_exchange_calls_a_started_upload_request_uncertain(self):
        headers = {'Content-Type': 'application/partial-upload', 'Upload-Offset': '0', 'Upload-Complete': '?1',
                   'Content-Digest': p.digest(b'data')}
        request = ('PATCH', '/uploads/' + '0' * 32, headers, b'data')
        with self.assertRaises(t.UncertainTransfer):
            self.send('upload_patch', request, responder(b'HTTP/1.1 garbage\r\n\r\n'))
        for status, operation in ((200, 'get'), (206, 'files')):
            raw = response_bytes(status, b'abcd', media='application/octet-stream', headers={
                'ETag': '"first"', 'Content-Range': 'bytes 0-3/4', 'Content-Digest': p.digest(b'abcd'),
                'Cache-Control': 'no-store'})
            with self.subTest(status=status), self.assertRaises(p.ProtocolError):
                self.send(operation, ('GET', '/files/report', {}, None), responder(raw))

    def test_host_recovers_lost_nonfinal_request_and_answer(self):
        for loss in ('request', 'answer'):
            with self.subTest(loss=loss):
                source = self.root / loss
                source.write_bytes(b'abcdef')
                offset_zero, operations = [], []

                def lossy(operation, request):
                    operations.append(operation)
                    if operation == 'upload_patch' and request[2]['Upload-Offset'] == '0':
                        offset_zero.append(request)
                        if len(offset_zero) == 1:
                            if loss == 'answer':
                                self.send(operation, request)
                            raise t.UncertainTransfer()
                    return self.send(operation, request)

                with t.open_upload(source, loss, self.agent.boot_id) as upload:
                    while not upload.complete:
                        upload.step(lossy)
                self.assertEqual(len(offset_zero), 2 if loss == 'request' else 1)
                self.assertEqual(operations[:3], ['upload_create', 'upload_patch', 'upload_status'])
                self.assertEqual((self.store.inbox / loss).read_bytes(), b'abcdef')

    def cli(self, command, *fields, marker=lambda _: None):
        host, local = private_pair()
        args = c.parser().parse_args([command, '--host', host, '--bind-local', local, '--port', '4418',
                                      '--key-file', 'fixture.key', *fields])
        output = []
        code = c.run(args, connector=lambda *_: paired(lambda raw: self.agent.connection(raw, self.events.append)),
                     key_loader=lambda _: KEY, marker=marker, output=output.append)
        return code, output

    def test_host_final_unknown_is_not_retried(self):
        source = self.root / 'source'
        source.write_bytes(b'123')
        exchange, requests = c.exchange, []

        def loss(stream, context, operation, request, output, **kwargs):
            requests.append(operation)
            reply = exchange(stream, context, operation, request, output, **kwargs)
            if operation == 'upload_patch':
                raise t.UncertainTransfer()
            return reply

        with patch.object(c, 'exchange', side_effect=loss):
            code, output = self.cli('put', '--boot-id', self.agent.boot_id, '--file', str(source), '--name', 'a')
        self.assertEqual((code, requests), (c.UNKNOWN_EXIT, ['upload_create', 'upload_patch']))
        self.assertIn('run files', output[-1])
        self.assertEqual((self.store.inbox / 'a').read_bytes(), b'123')

    def test_host_detects_source_change_before_final(self):
        source = self.root / 'source'
        source.write_bytes(b'123456')
        with t.open_upload(source, 'a', self.agent.boot_id) as upload:
            upload.step(self.send)
            upload.step(self.send)
            source.write_bytes(b'1234567')
            with self.assertRaisesRegex(f.StoreError, '^FILE_CHANGED$'):
                upload.step(self.send)
            upload.cancel(self.send)
        self.empty_partial()
        self.assertFalse((self.store.inbox / 'a').exists())

    def test_host_mark_order_mark_failure_and_exclusive_publication(self):
        (self.store.outbox / 'report').write_bytes(b'abcdef')
        target = self.root / 'result.bin'
        calls = []

        def mark(part):
            self.assertRegex(part.name, r'^\.scn-download-[0-9a-f]{32}\.part$')
            with f.plain_reader(part) as (stream, _):
                self.assertEqual((part.parent, stream.read()), (target.parent, b'abcdef'))
            self.assertFalse(target.exists())
            calls.append(part)

        code, output = self.cli('get', '--name', 'report', '--out', str(target), marker=mark)
        self.assertEqual(code, 0)
        self.assertEqual(output[:2], ['PROGRESS 4/6', 'PROGRESS 6/6'])
        self.assertEqual(target.read_bytes(), b'abcdef')
        self.assertFalse(calls[0].exists())
        self.assertEqual(self.cli('get', '--name', 'report', '--out', str(target))[0], c.REFUSED_EXIT)
        target.unlink()
        failing = Mock(side_effect=OSError('mark'))
        code, output = self.cli('get', '--name', 'report', '--out', str(target), marker=failing)
        self.assertEqual((code, output[-1]), (c.REFUSED_EXIT, 'MARK_FAILED'))
        self.assertFalse(target.exists())
        self.assertFalse(calls[0].exists())

        def race(part):
            mark(part)
            target.write_bytes(b'external')

        self.assertEqual(self.cli('get', '--name', 'report', '--out', str(target), marker=race)[0], c.REFUSED_EXIT)
        self.assertEqual(target.read_bytes(), b'external')
        self.assertFalse(calls[0].exists())

    def test_host_cancel_removes_only_its_new_part(self):
        (self.store.outbox / 'report').write_bytes(b'abcdef')
        target = self.root / 'result'
        old = self.root / ('.scn-download-' + '0' * 32 + '.part')
        old.write_bytes(b'old')
        with self.assertRaises(KeyboardInterrupt):
            with t.open_download('report', target, self.agent.boot_id) as download:
                download.step(self.send)
                part = download.part
                self.assertTrue(part.exists())
                download.step(Mock(side_effect=KeyboardInterrupt))
        self.assertFalse(part.exists())
        self.assertFalse(target.exists())
        # A part left by a crash neither blocks the same target nor is removed.
        with t.open_download('report', target, self.agent.boot_id) as download:
            download.step(self.send)
            self.assertNotEqual(download.part, part)
        with patch.object(t.secrets, 'token_hex', return_value='0' * 32):
            with self.assertRaisesRegex(f.StoreError, '^FILE_EXISTS$'):
                with t.open_download('report', target, self.agent.boot_id) as download:
                    download.step(self.send)
        self.assertEqual(old.read_bytes(), b'old')

    def test_each_chunk_is_written_out_before_it_is_acknowledged(self):
        path = self.begin(data=b'abcdef')
        with patch.object(f.os, 'fsync', wraps=os.fsync) as fsync:
            self.code(self.chunk(path, b'abcd', final=False), 200, 'OK')
            self.assertEqual(fsync.call_count, 1)

    def test_host_rejects_hostile_ranges_over_real_tls(self):
        target = self.root / 'result'

        def step(download, data, span, *, tag='"first"', digest=None, extra=None):
            headers = {'Content-Range': span, 'ETag': tag, 'Cache-Control': 'no-store',
                       'Content-Digest': p.digest(data) if digest is None else digest}
            raw = response_bytes(206, data, headers=headers | (extra or {}), media='application/octet-stream')
            download.step(lambda op, request: self.send(op, request, responder(raw)))

        cases = ((b'abcd', 'bytes 0-3/13', {}),
                 (b'abcde', 'bytes 0-4/5', {}),
                 (b'a', 'bytes 0-0/9', {}),
                 (b'abcd', 'bytes 0-3/9', {'digest': p.digest(b'bad')}),
                 (b'abcd', 'bytes 0-3/9', {'tag': 'W/"first"'}),
                 (b'abcd', 'bytes 0-3/9', {'extra': {'Cache-Control': 'public'}}),
                 (b'abcd', 'bytes 0-3/9', {'extra': {'X-Other': '1'}}))
        for data, span, options in cases:
            with self.subTest(span=span, options=options):
                with self.assertRaises(p.ProtocolError):
                    with t.open_download('report', target, self.agent.boot_id) as download:
                        step(download, data, span, **options)
                self.assertFalse(download.part.exists())
                self.assertFalse(target.exists())
        for total, tag in ((8, '"second"'), (9, '"first"')):
            with self.assertRaisesRegex(f.StoreError, '^FILE_CHANGED$'):
                with t.open_download('report', target, self.agent.boot_id) as download:
                    step(download, b'abcd', 'bytes 0-3/8')
                    step(download, b'efgh', f'bytes 4-7/{total}', tag=tag)
            self.assertFalse(download.part.exists())
            self.assertFalse(target.exists())
