"""Chunk-wise host transfers. Callers supply the authenticated request function."""

from contextlib import contextmanager
import hashlib
import os
import re
import secrets

if __package__:
    from . import vm_files as f, vm_protocol as p
else:
    import vm_files as f
    import vm_protocol as p


class UncertainTransfer(p.TransportError):
    """The request began, and its result could not be validated."""


def _accepted(reply, result='OK'):
    if reply['result'] != result:
        raise f.StoreError(reply['result'])
    return reply


def _request(method, path, headers=None, body=None):
    return method, path, headers or {}, body


class UploadTransfer:
    def __init__(self, handle, path, info, name):
        self.handle, self.path, self.info = handle, path, info
        self.name, self.sha256, self.boot_id = name, None, None
        self.length, self.offset = info.st_size, 0
        self.resource = None
        self.recovering = False
        self.retried = False
        self.complete = False
        self.final_pending = False

    def check_source(self):
        if not f.unchanged(self.path, self.handle, self.info):
            raise f.StoreError('FILE_CHANGED')

    def hash_source(self, check):
        digest = hashlib.sha256()
        self.handle.seek(0)
        for offset in range(0, self.length, p.CHUNK_MAX):
            check()
            chunk_length = min(p.CHUNK_MAX, self.length - offset)
            raw = self.handle.read(chunk_length)
            if len(raw) != chunk_length:
                raise f.StoreError('FILE_CHANGED')
            digest.update(raw)
        self.check_source()
        self.sha256 = digest.hexdigest()

    def _status(self, send):
        return send('upload_status', _request('GET', self.resource, {'SCN-Boot-Id': self.boot_id}))

    def _finish_uncertain(self, send):
        reply = self._status(send)
        if reply['result'] != 'OK' or reply['length'] != self.length:
            raise UncertainTransfer()
        if reply['complete'] is True and reply['offset'] == self.length:
            self.offset, self.complete, self.final_pending = self.length, True, False
        elif reply['complete'] is False and reply['offset'] == self.offset:
            self.final_pending = False
            raise f.StoreError('UPLOAD_NOT_COMPLETED')
        else:
            raise UncertainTransfer()

    def step(self, send, *, managed=False):
        """Issue one request. A lost nonfinal PATCH selects a status read next."""
        p.require(not self.complete)
        if self.final_pending:
            return self._finish_uncertain(send)
        if self.resource is None:
            request = _request('POST', '/uploads',
                               {'Content-Type': 'application/json', 'SCN-Boot-Id': self.boot_id,
                                'Idempotency-Key': secrets.token_hex(16)},
                               p.encode(dict(name=self.name, length=self.length, sha256=self.sha256)))
            reply = _accepted(send('upload_create', request), 'CREATED')
            p.hex_bytes(reply['id'], 16)
            p.integer(reply['offset'], 0, 0)
            self.resource = '/uploads/' + reply['id']
            return

        end = min(self.length, self.offset + p.CHUNK_MAX)
        if self.recovering:
            reply = _accepted(self._status(send))
            p.require(reply['length'] == self.length and reply['complete'] is False
                      and reply['offset'] in (self.offset, end))
            self.retried = reply['offset'] == self.offset
            self.offset = reply['offset']
            self.recovering = False
            return

        final = end == self.length
        self.handle.seek(self.offset)
        raw = self.handle.read(end - self.offset)
        if len(raw) != end - self.offset:
            raise f.StoreError('FILE_CHANGED')
        if final:
            self.check_source()
        request = _request('PATCH', self.resource,
                           {'SCN-Boot-Id': self.boot_id, 'Content-Type': 'application/partial-upload',
                            'Upload-Offset': str(self.offset),
                            'Upload-Complete': '?1' if final else '?0', 'Content-Digest': p.digest(raw)}, raw)
        try:
            reply = send('upload_patch', request)
        except UncertainTransfer:
            if managed and final:
                self.final_pending = True
                return
            if managed:
                self.recovering = True
                raise
            if final or self.retried:
                raise
            self.recovering = True
            return
        if reply['result'] == 'OFFSET_MISMATCH' and not final and not self.retried:
            self.recovering = True
            return
        _accepted(reply)
        if reply['length'] != self.length or reply['offset'] != end or reply['complete'] is not final:
            if final:
                if managed:
                    self.final_pending = True
                    return
                raise UncertainTransfer()
            raise p.ProtocolError()
        self.offset, self.complete, self.retried = end, final, False

    def cancel(self, send):
        if self.resource is not None and not self.complete:
            return send('upload_delete', _request('DELETE', self.resource, {'SCN-Boot-Id': self.boot_id}))
        return None


@contextmanager
def prepare_upload(path, name):
    p.file_path(name)
    path = f.local_path(path)
    with f.plain_reader(path) as (handle, info):
        if info.st_size > p.FILE_MAX:
            raise f.StoreError('FILE_TOO_LARGE')
        yield UploadTransfer(handle, path, info, name)


@contextmanager
def open_upload(path, name, boot_id, *, check=lambda: None):
    p.hex_bytes(boot_id, 16)
    with prepare_upload(path, name) as active:
        active.boot_id = boot_id
        active.hash_source(check)
        yield active


def range_info(value):
    if value == 'bytes */0':
        return 0, -1, 0
    match = re.fullmatch(r'bytes ([0-9]{1,10})-([0-9]{1,10})/([0-9]{1,10})', value)
    p.require(match is not None)
    return tuple(map(int, match.groups()))


class DownloadTransfer:
    def __init__(self, path, target, boot_id, expected_length):
        self.path, self.target, self.boot_id = path, target, boot_id
        self.expected_length = expected_length
        # A part left by a crash must not block the next download of the same name.
        self.part = target.with_name(f'.scn-download-{secrets.token_hex(16)}.part')
        self.handle = None
        self.offset, self.length, self.tag = 0, None, None
        self.digest = hashlib.sha256()
        self.complete = False

    def step(self, send):
        """Fetch and verify one range, then write exactly its declared bytes."""
        p.require(not self.complete)
        end = self.offset + p.CHUNK_MAX - 1
        known_length = self.length if self.length is not None else self.expected_length
        if known_length:
            end = min(end, known_length - 1)
        headers = {'SCN-Boot-Id': self.boot_id, 'Range': f'bytes={self.offset}-{end}'}
        if self.tag is not None:
            headers['If-Match'] = self.tag
        reply = _accepted(send('get', _request('GET', self.path, headers)))
        start, stop, total = range_info(reply['content_range'])
        if self.expected_length is not None and total != self.expected_length:
            raise f.StoreError('FILE_CHANGED')
        p.integer(total, 0, min(p.FILE_MAX, p.FILE_TOTAL_MAX))
        data = reply['data']
        p.require(start == self.offset and stop == min(end, total - 1)
                  and len(data) == stop - start + 1 and (total == 0 or stop >= start))
        p.strong_tag(reply['etag'])
        if reply['content_digest'] != p.digest(data):
            if self.expected_length is not None:
                raise f.StoreError('DIGEST_MISMATCH')
            raise p.ProtocolError()
        if self.length is None:
            self.length, self.tag = total, reply['etag']
            f.reserve_space(self.target.parent, total)
            self.handle = f.open_part(self.part, needs_marker=True)
        elif total != self.length or reply['etag'] != self.tag:
            raise f.StoreError('FILE_CHANGED')
        if self.expected_length is not None:
            f.reserve_space(self.target.parent, total - self.offset)
        if self.handle.write(data) != len(data):
            raise f.StoreError('FILE_FAILED')
        self.digest.update(data)
        self.offset += len(data)
        self.complete = self.offset == self.length

    def publish(self, marker, *, check=lambda: None):
        p.require(self.complete and self.handle is not None and not self.handle.closed)
        self.handle.flush()
        os.fsync(self.handle.fileno())
        info = os.fstat(self.handle.fileno())
        with f.pin_part(self.handle, delete_access=False):
            if not f.unchanged(self.part, self.handle, info):
                raise f.StoreError('FILE_CHANGED')
            try:
                marker(self.part)
            except OSError:
                raise f.StoreError('MARK_FAILED') from None
        with f.pin_part(self.handle, delete_access=True) as handle:
            if not f.unchanged(self.part, self.handle, info, compare_mtime=False):
                raise f.StoreError('FILE_CHANGED')
            f.ensure_new(self.target.parent, self.target.name)
            check()
            f.publish_new(handle, self.target)
        self.handle.close()

    def close(self):
        if self.handle is not None:
            f.discard_part(self.handle, reopen=True)


@contextmanager
def open_download(name, target, boot_id, *, expected_length=None):
    path = p.file_path(name)
    p.hex_bytes(boot_id, 16)
    target = f.local_path(target)
    with f.checked_folder(target.parent):
        f.ensure_new(target.parent, target.name)
        transfer = DownloadTransfer(path, target, boot_id, expected_length)
        try:
            yield transfer
        except FileExistsError:
            raise f.StoreError('FILE_EXISTS') from None
        except OSError:
            raise f.StoreError('FILE_FAILED') from None
        finally:
            try:
                transfer.close()
            except OSError:
                raise f.StoreError('FILE_FAILED') from None
