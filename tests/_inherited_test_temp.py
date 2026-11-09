"""Disposable NON-SECRET synthetic fixtures only; never Owner/security state.

Inherit the existing repository scratch DACL using ordinary mkdir defaults.
No permission repair, fallback, collision retry, or cleanup retry is allowed.
"""
from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import secrets
import stat

_PARENT = Path(__file__).resolve().parents[1] / "temp"


class FixtureHold(RuntimeError):
    """FAIL/HOLD: unsafe or unverifiable synthetic fixture environment."""


def _identity(path, *, directory):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or stat.S_ISDIR(info.st_mode) != directory
            or (not directory and not stat.S_ISREG(info.st_mode))
            or not info.st_ino or (not directory and info.st_nlink != 1)
            or path.resolve(strict=True) != path):
        raise FixtureHold(f"FAIL/HOLD: unsafe fixture object: {path}")
    return info.st_dev, info.st_ino


def _acl(path):
    """Read only: require a valid, inheriting, nonempty ordinary allow DACL.

    This is an integrity/compatibility check, NOT a private-ACL guarantee.
    Inherited access by workspace principals is intentional for fake data.
    """
    if os.name != "nt":
        raise FixtureHold("FAIL/HOLD: Windows DACL verification required")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    get = api.GetFileSecurityW
    get.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID,
                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    get.restype = wintypes.BOOL
    size = wintypes.DWORD()
    get(str(path), 4, None, 0, ctypes.byref(size))
    error = ctypes.get_last_error()
    if error != 122 or not size.value:  # expected buffer sizing only
        raise ctypes.WinError(error)
    buffer = ctypes.create_string_buffer(size.value)
    if not get(str(path), 4, buffer, size, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    # Self-relative SECURITY_DESCRIPTOR: control at 2, DACL offset at 16.
    raw = buffer.raw
    control = int.from_bytes(raw[2:4], "little")
    offset = int.from_bytes(raw[16:20], "little")
    if not control & 4 or control & 0x1000 or offset < 20:
        raise FixtureHold("FAIL/HOLD: absent/protected DACL")
    acl = raw[offset:]
    if len(acl) < 8:
        raise FixtureHold("FAIL/HOLD: truncated DACL")
    length = int.from_bytes(acl[2:4], "little")
    count = int.from_bytes(acl[4:6], "little")
    if acl[0] != 2 or length > len(acl) or not count:
        raise FixtureHold("FAIL/HOLD: unverifiable DACL")
    cursor = 8
    for _ in range(count):
        ace = acl[cursor:length]
        width = int.from_bytes(ace[2:4], "little")
        if len(ace) < 4 or ace[0] != 0 or width < 16 or width > len(ace):
            raise FixtureHold("FAIL/HOLD: denied/unknown/malformed ACE")
        cursor += width
    if cursor != length:
        raise FixtureHold("FAIL/HOLD: unexpected DACL size")
    return raw[offset:offset + length]


def _verify(path, identity, *, directory=True):
    if _identity(path, directory=directory) != identity:
        raise FixtureHold(f"FAIL/HOLD: changed fixture identity: {path}")


@contextmanager
def _pin(path, identity):
    """Prevent directory rename/redirection while traversing its contents."""
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    create = api.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                       wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = api.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # Read attributes; share read/write but NOT delete. Open reparse point itself.
    handle = create(str(path), 0x80, 3, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _verify(path, identity)
        yield
    finally:
        if not close(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def _cleanup(child, child_id, parent_id, parent_acl):
    if child.parent != _PARENT:
        raise FixtureHold("FAIL/HOLD: cleanup target outside fixed parent")
    _verify(_PARENT, parent_id)
    if _acl(_PARENT) != parent_acl:
        raise FixtureHold("FAIL/HOLD: changed parent ACL; cleanup refused")
    _verify(child, child_id)
    # Inspect the complete tree BEFORE deleting anything. Never follow links.
    objects = []
    with ExitStack() as stack:
        stack.enter_context(_pin(child, child_id))

        def inspect(folder):
            for item in folder.iterdir():
                info = item.lstat()
                directory = stat.S_ISDIR(info.st_mode)
                identity = _identity(item, directory=directory)
                pin = None
                if directory:
                    pin = stack.enter_context(ExitStack())
                    pin.enter_context(_pin(item, identity))
                    inspect(item)
                objects.append((item, identity, directory, pin))

        inspect(child)
        for item, identity, directory, pin in objects:
            _verify(_PARENT, parent_id)
            _verify(child, child_id)
            _verify(item, identity, directory=directory)
            if directory:
                pin.close()
                _verify(item, identity)
                item.rmdir()
            else:
                item.unlink()
        _verify(_PARENT, parent_id)
        _verify(child, child_id)
    _verify(_PARENT, parent_id)
    _verify(child, child_id)
    child.rmdir()


@contextmanager
def inherited_test_temp():
    """Yield a string path compatible with the former synthetic fixtures.

    A real PermissionError is propagated immediately without cleanup attempts.
    Other body errors still trigger verified cleanup; failures remain visible.
    """
    parent_id = _identity(_PARENT, directory=True)
    parent_acl = _acl(_PARENT)
    with _pin(_PARENT, parent_id):
        child = _PARENT / ("scn-synthetic-" + secrets.token_hex(24))
        # Exclusive creation: collision raises; the preexisting path is kept.
        child.mkdir()
        child_id = _identity(child, directory=True)
        _verify(_PARENT, parent_id)
        if child.parent != _PARENT or _acl(_PARENT) != parent_acl:
            raise FixtureHold("FAIL/HOLD: changed parent after creation")
        _acl(child)
        try:
            with _pin(child, child_id):
                yield str(child)
        except PermissionError:
            # Stop on denial, including no cleanup retry.
            raise
        finally:
            import sys
            if not isinstance(sys.exception(), PermissionError):
                _cleanup(child, child_id, parent_id, parent_acl)
