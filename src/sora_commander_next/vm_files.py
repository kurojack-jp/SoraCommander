"""Plain-file storage for the DEV VM file transfer."""

from contextlib import ExitStack, contextmanager
import ctypes as ct
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

if __package__:
    from . import vm_protocol as p, vm_win as win
else:
    import vm_protocol as p
    import vm_win as win


LIST_MAX = 100
SCAN_MAX = 1000
REPARSE = 0x400
_OPEN_REPARSE = 0x00200000
_BACKUP_SEMANTICS = 0x02000000
_READ = 0x80000000
_WRITE = 0x40000000
_DELETE = 0x00010000
_SHARE_READ = 1
_SHARE_READ_WRITE = 3
_SHARE_READ_DELETE = 5
_SHARE_ALL = 7


class StoreError(Exception):
    """A fixed refusal. The result never contains a path or file content."""

    def __init__(self, result):
        super().__init__(result)
        self.result = result


def _plain(info, directory):
    return (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) \
        and not getattr(info, 'st_file_attributes', 0) & REPARSE and (directory or info.st_nlink == 1)


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _os_refusal(exc):
    return StoreError('FILE_BUSY' if getattr(exc, 'winerror', None) == 32 else 'FILE_FAILED')


def _windows_handle(path, directory, *, deny_changes=False):
    lib = ct.WinDLL('kernel32', use_last_error=True)
    create = win._bind(lib, 'CreateFileW', win.HANDLE, win.WIDE, win.U32, win.U32,
                       win.HANDLE, win.U32, win.U32, win.HANDLE)
    close = win._bind(lib, 'CloseHandle', win.I32, win.HANDLE)
    get_kind = win._bind(lib, 'GetFileType', win.U32, win.HANDLE)
    get_info = win._bind(lib, 'GetFileInformationByHandle', win.I32, win.HANDLE,
                         ct.POINTER(win.FileInformation))
    access, sharing, flags = (
        (0, _SHARE_READ_WRITE, _OPEN_REPARSE | _BACKUP_SEMANTICS) if directory
        else (_READ, _SHARE_READ if deny_changes else _SHARE_ALL, _OPEN_REPARSE))
    handle = create(os.fspath(path), access, sharing, None, 3, flags, None)
    if handle in (None, win.INVALID_HANDLE):
        raise ct.WinError(ct.get_last_error())
    info = win.FileInformation()
    if (get_kind(handle) != 1 or not get_info(handle, ct.byref(info))
            or info.attributes & REPARSE or bool(info.attributes & 0x10) != directory
            or (not directory and info.links != 1)):
        close(handle)
        raise StoreError('FILE_FAILED')
    return handle, close


def open_part(path, *, needs_marker=False):
    # Both endpoints use Windows. A download needs a separate ADS open, so
    # defer its DELETE access and obtain it from this handle via ReOpenFile.
    if os.name != 'nt':
        raise StoreError('FILE_FAILED')
    import msvcrt

    lib = ct.WinDLL('kernel32', use_last_error=True)
    create = win._bind(lib, 'CreateFileW', win.HANDLE, win.WIDE, win.U32, win.U32,
                       win.HANDLE, win.U32, win.U32, win.HANDLE)
    close = win._bind(lib, 'CloseHandle', win.I32, win.HANDLE)
    access, sharing = (_WRITE, _SHARE_READ_DELETE) if needs_marker else (_WRITE | _DELETE, _SHARE_READ)
    handle = create(os.fspath(path), access, sharing, None, 1, _OPEN_REPARSE, None)
    if handle in (None, win.INVALID_HANDLE):
        raise ct.WinError(ct.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    return os.fdopen(fd, 'wb')


def part_handle(stream):
    import msvcrt

    return msvcrt.get_osfhandle(stream.fileno())


@contextmanager
def pin_part(stream, *, delete_access):
    lib = ct.WinDLL('kernel32', use_last_error=True)
    reopen = win._bind(lib, 'ReOpenFile', win.HANDLE, win.HANDLE, win.U32, win.U32, win.U32)
    close = win._bind(lib, 'CloseHandle', win.I32, win.HANDLE)
    # ReOpenFile selects the same object even if its original name was
    # changed. The original stream continues denying competing writes.
    handle = reopen(part_handle(stream), _DELETE if delete_access else _READ, _SHARE_READ_WRITE, 0)
    if handle in (None, win.INVALID_HANDLE):
        raise ct.WinError(ct.get_last_error())
    try:
        yield handle
    finally:
        close(handle)


def local_path(path):
    path = Path(os.path.abspath(path))
    if os.name == 'nt':
        drive, rest = os.path.splitdrive(path)
        if not re.fullmatch(r'[A-Za-z]:', drive) or ':' in rest:
            raise StoreError('FILE_FAILED')
    return path


def local_drive(path):
    path = local_path(path)
    if os.name == 'nt':
        lib = ct.WinDLL('kernel32', use_last_error=True)
        drive_type = win._bind(lib, 'GetDriveTypeW', win.U32, win.WIDE)
        if drive_type(path.anchor) not in (2, 3, 5, 6):
            raise StoreError('FILE_FAILED')
    return path


@contextmanager
def checked_folder(folder):
    folder = local_path(folder)
    # On Windows, keep each ancestor open without FILE_SHARE_DELETE while using its paths.
    with ExitStack() as pins:
        for directory in (*reversed(folder.parents), folder):
            try:
                if not _plain(os.lstat(directory), True):
                    raise StoreError('FILE_FAILED')
                if os.name == 'nt':
                    handle, close = _windows_handle(directory, True)
                    pins.callback(close, handle)
            except OSError:
                raise StoreError('FILE_FAILED') from None
        yield folder


def _read_fd(path, *, deny_changes=False):
    if os.name == 'nt':
        import msvcrt

        handle, close = _windows_handle(path, False, deny_changes=deny_changes)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close(handle)
            raise
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)


@contextmanager
def plain_reader(path, *, deny_changes=False):
    path = local_path(path)
    try:
        with checked_folder(path.parent):
            before = os.lstat(path)
            if not _plain(before, False):
                raise StoreError('FILE_FAILED')
            with os.fdopen(_read_fd(path, deny_changes=deny_changes), 'rb') as handle:
                info = os.fstat(handle.fileno())
                if not _plain(info, False) or identity(info)[:2] != identity(before)[:2]:
                    raise StoreError('FILE_FAILED')
                yield handle, info
    except FileNotFoundError:
        raise StoreError('FILE_NOT_FOUND') from None
    except OSError as exc:
        raise _os_refusal(exc) from None


def unchanged(path, handle, info, *, compare_mtime=True):
    try:
        after = os.fstat(handle.fileno())
        named = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _os_refusal(exc) from None
    fields = slice(None) if compare_mtime else slice(3)
    return (_plain(after, False) and _plain(named, False)
            and identity(info)[fields] == identity(after)[fields] == identity(named)[fields])


def free_bytes(folder):
    if os.name != 'nt':
        info = os.statvfs(folder)
        return info.f_bavail * info.f_frsize
    lib = ct.WinDLL('kernel32', use_last_error=True)
    query = win._bind(lib, 'GetDiskFreeSpaceExW', win.I32, win.WIDE, ct.POINTER(ct.c_ulonglong),
                      win.HANDLE, win.HANDLE)
    available = ct.c_ulonglong()
    if not query(os.fspath(folder), ct.byref(available), None, None):
        raise ct.WinError(ct.get_last_error())
    return available.value


def reserve_space(folder, length):
    try:
        enough = free_bytes(folder) >= length + p.FILE_FREE_MIN
    except OSError:
        enough = False
    if not enough:
        raise StoreError('DISK_FULL')


def ensure_new(folder, name):
    try:
        with os.scandir(folder) as entries:
            if any(entry.name.casefold() == name.casefold() for entry in entries):
                raise StoreError('FILE_EXISTS')
    except OSError:
        raise StoreError('FILE_FAILED') from None


class _RenameInfo(ct.Structure):
    _fields_ = [('replace', win.U32), ('root', win.HANDLE), ('name_size', win.U32),
                ('name', ct.c_wchar * 1)]


def _set_file_info(handle, kind, info, size):
    lib = ct.WinDLL('kernel32', use_last_error=True)
    change = win._bind(lib, 'SetFileInformationByHandle', win.I32, win.HANDLE,
                       win.U32, ct.c_void_p, win.U32)
    if not change(handle, kind, ct.byref(info), size):
        raise ct.WinError(ct.get_last_error())


def publish_new(handle, final):
    name = os.fspath(final).encode('utf-16-le')
    size = max(ct.sizeof(_RenameInfo), _RenameInfo.name.offset + len(name) + 2)
    buffer = ct.create_string_buffer(size)
    info = _RenameInfo.from_buffer(buffer)
    info.name_size = len(name)
    ct.memmove(ct.addressof(buffer) + _RenameInfo.name.offset, name, len(name))
    try:
        _set_file_info(handle, 3, buffer, size)  # FileRenameInfo, ReplaceIfExists = FALSE
    except FileExistsError:
        raise StoreError('FILE_EXISTS') from None
    except OSError as exc:
        raise _os_refusal(exc) from None


def discard_part(stream, *, reopen=False):
    if stream.closed:
        return
    try:
        delete = ct.c_ubyte(1)
        if reopen:
            with pin_part(stream, delete_access=True) as handle:
                _set_file_info(handle, 4, delete, ct.sizeof(delete))  # FileDispositionInfo
        else:
            _set_file_info(part_handle(stream), 4, delete, ct.sizeof(delete))
    finally:
        stream.close()


class Upload:
    def __init__(self, upload_id, name, length, digest, handle):
        self.id, self.name, self.length = upload_id, name, length
        self.expected_digest, self.handle = digest, handle
        self.digest = hashlib.sha256()
        self.offset = 0

    def status(self, complete=False):
        return dict(offset=self.offset, length=self.length, complete=complete)

    def append(self, raw):
        if self.handle.write(raw) != len(raw):
            raise OSError('incomplete write')
        self.handle.flush()
        # One chunk at a time, so that the last request never has to write out a whole GiB.
        os.fsync(self.handle.fileno())
        self.digest.update(raw)
        self.offset += len(raw)


class FileStore:
    def __init__(self, root):
        if type(root) is not str or not os.path.isabs(root):
            raise StoreError('FILE_FAILED')
        self.root = local_path(root)
        self.inbox = self.root / 'inbox'
        self.outbox = self.root / 'outbox'
        self.partial = self.root / 'partial'
        with checked_folder(self.inbox), checked_folder(self.outbox):
            try:
                self.partial.mkdir(exist_ok=True)
            except OSError:
                raise StoreError('FILE_FAILED') from None
        self.cleanup()

    def _part(self, upload_id):
        return self.partial / f'scn-upload-{upload_id}.part'

    def create(self, name, length, digest):
        with checked_folder(self.inbox), checked_folder(self.partial):
            reserve_space(self.root, length)
            ensure_new(self.inbox, name)
            upload_id = secrets.token_hex(16)
            try:
                handle = open_part(self._part(upload_id))
            except OSError:
                raise StoreError('FILE_FAILED') from None
            return Upload(upload_id, name, length, digest, handle)

    def append(self, upload, raw):
        with checked_folder(self.partial):
            info = os.fstat(upload.handle.fileno())
            if info.st_size != upload.offset or not unchanged(self._part(upload.id), upload.handle, info):
                raise StoreError('FILE_FAILED')
            upload.append(raw)

    def finish(self, upload, check_time):
        part = self._part(upload.id)
        with checked_folder(self.partial), checked_folder(self.inbox):
            try:
                info = os.fstat(upload.handle.fileno())
                if not unchanged(part, upload.handle, info) or info.st_size != upload.length:
                    raise StoreError('FILE_FAILED')
                os.fsync(upload.handle.fileno())
            except OSError:
                raise StoreError('FILE_FAILED') from None
            ensure_new(self.inbox, upload.name)
            check_time()
            publish_new(part_handle(upload.handle), self.inbox / upload.name)
            upload.handle.close()

    def discard(self, upload):
        try:
            discard_part(upload.handle)
        except OSError as exc:
            raise _os_refusal(exc) from None

    def cleanup(self):
        # The active upload is discarded by handle. A name encountered after
        # restart does not establish who created that file.
        with checked_folder(self.partial):
            pass

    def _list(self, folder):
        items, truncated = [], False
        try:
            with checked_folder(folder), os.scandir(folder) as entries:
                for number, entry in enumerate(entries):
                    if number == SCAN_MAX:
                        truncated = True
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                        # scandir on Windows reports no link count, so ask again.
                        info = os.lstat(entry.path) if os.name == 'nt' else info
                    except OSError:
                        continue
                    if not p.is_file_name(entry.name) or not _plain(info, False):
                        continue
                    if len(items) == LIST_MAX:
                        truncated = True
                        break
                    items.append(dict(name=entry.name, size=min(info.st_size, p.SIZE_MAX)))
        except OSError:
            raise StoreError('FILE_FAILED') from None
        return sorted(items, key=lambda item: item['name']), truncated

    def listing(self):
        inbox, first = self._list(self.inbox)
        outbox, second = self._list(self.outbox)
        return dict(inbox=inbox, outbox=outbox, truncated=first or second)

    def read_range(self, name, start, end, expected, boot_id, budget):
        p.file_name(name)
        path = self.outbox / name
        try:
            with plain_reader(path) as (handle, info):
                tag = '"' + hashlib.sha256(p.encode([boot_id, *identity(info)])).hexdigest() + '"'
                if expected is not None and expected != tag:
                    raise StoreError('FILE_CHANGED')
                size = info.st_size
                if size > p.FILE_MAX:
                    raise StoreError('FILE_TOO_LARGE')
                if start >= size and (size or start):
                    raise StoreError('RANGE_NOT_ALLOWED')
                stop = min(end + 1, size)
                if stop - start > budget:
                    raise StoreError('TRANSFER_LIMIT')
                handle.seek(start)
                data = handle.read(stop - start)
                if len(data) != stop - start or not unchanged(path, handle, info):
                    raise StoreError('FILE_CHANGED')
        except StoreError as exc:
            if expected is not None and exc.result == 'FILE_NOT_FOUND':
                raise StoreError('FILE_CHANGED') from None
            raise
        content_range = f'bytes {start}-{stop - 1}/{size}' if size else 'bytes */0'
        return data, {'ETag': tag, 'Content-Range': content_range}
