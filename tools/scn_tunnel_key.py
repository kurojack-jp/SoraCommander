"""Store or check the current Windows user's tunnel key without displaying it."""

from contextlib import ExitStack
import getpass
import os
from pathlib import Path
import re
import sys
import uuid
import warnings

if __package__:
    from .scn_tunnel_client import checkout_path_hold
else:
    sys.path.insert(0, str(Path(__file__).absolute().parent))
    from scn_tunnel_client import checkout_path_hold


KEY = re.compile(rb"\A[\x21-\x7E]{8,4096}\Z")
LINK = "[HOLD] The key file or its folder is a link. Nothing was read or written."
CANCELLED = "[INFO] Cancelled. The key file was not changed."


class UnsafePath(Exception):
    """Fixed, displayable diagnostic for an unsafe filesystem object."""


def _open_existing(stack, path, *, directory=False, read=False):
    import ntsecuritycon
    import pywintypes
    import win32con
    import win32file

    # Open the reparse point itself. Deny concurrent write/delete until the
    # attributes and any bytes have been checked on this same handle.
    try:
        handle = win32file.CreateFile(
            str(path), win32con.GENERIC_READ if read else ntsecuritycon.FILE_READ_ATTRIBUTES,
            win32con.FILE_SHARE_READ, None, win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
    except pywintypes.error as error:
        if error.winerror in (2, 3):  # file or path not found
            return None
        raise
    stack.callback(handle.Close)
    attributes = win32file.GetFileInformationByHandle(handle)[0]
    if attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
        raise UnsafePath(LINK)
    if read and attributes & win32con.FILE_ATTRIBUTE_DIRECTORY:
        return None
    if bool(attributes & win32con.FILE_ATTRIBUTE_DIRECTORY) != directory:
        raise UnsafePath("[HOLD] Expected an ordinary key file and folder.")
    return handle


def _check(handle, path):
    import win32file

    if handle is None:
        print(fr"[HOLD] No key file at {path}. Run tools\set-tunnel-key.cmd or create it by hand.")
        return 4
    size = win32file.GetFileSize(handle)
    if size > 8192:
        print("[HOLD] The key file is too large to be a key.")
        return 5
    status, data = win32file.ReadFile(handle, size) if size else (0, b"")
    if status or len(data) != size or not KEY.fullmatch(data):
        print("[HOLD] The key file must hold the key only: one line, no spaces, no line break, no BOM.")
        return 5
    print("[OK] The key file has a valid form. Its value was not shown or checked against the service.")
    return 0


def _protect(handle):
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
    finally:
        token.Close()
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    win32security.SetSecurityInfo(
        handle, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None,
    )
    descriptor = win32security.GetSecurityInfo(
        handle, win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
    control, _ = descriptor.GetSecurityDescriptorControl()
    applied = descriptor.GetSecurityDescriptorDacl()
    if (not control & win32security.SE_DACL_PROTECTED
            or applied is None or applied.GetAceCount() != 1
            or applied.GetAce(0) != ((win32security.ACCESS_ALLOWED_ACE_TYPE, 0),
                                     ntsecuritycon.FILE_ALL_ACCESS, sid)):
        raise UnsafePath("[HOLD] Could not verify the private key file permissions.")


def _save(path, data):
    import win32con
    import win32file

    temporary = path.with_name(f"sora-next.key.{uuid.uuid4().hex}.tmp")
    handle = win32file.CreateFile(
        str(temporary), win32con.GENERIC_WRITE | win32con.WRITE_DAC, 0, None,
        win32con.CREATE_NEW, win32con.FILE_ATTRIBUTE_NORMAL, None,
    )
    try:
        try:
            _protect(handle)
            status, written = win32file.WriteFile(handle, data)
            if status or written != len(data):
                raise OSError("Incomplete key file write.")
        finally:
            handle.Close()
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink()
        raise


def _run(profile, *, check, from_stdin):
    config = profile / ".config"
    folder = config / "tunnel-client"
    path = folder / "sora-next.key"
    with ExitStack() as stack:
        parents = (profile, config, folder)
        missing = ()
        for index, parent in enumerate(parents):
            if _open_existing(stack, parent, directory=True) is None:
                missing = parents[index:]
                break
        with ExitStack() as existing:
            handle = None if missing else _open_existing(existing, path, read=check)
            if check:
                return _check(handle, path)
            if handle is not None and not from_stdin:
                if input("A key file already exists. Replace it? (y/N) ") != "y":
                    print(CANCELLED)
                    return 2
        if from_stdin:
            data = sys.stdin.buffer.read(4097)
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass("Paste your API key (input is hidden): ")
            data = value.encode("ascii") if value.isascii() else b""
        if not KEY.fullmatch(data):
            print("[HOLD] That does not look like one key. The key file was not changed.")
            return 5

        for parent in missing:
            try:
                parent.mkdir()
            except FileExistsError:
                pass  # Inspect and pin the entry that won the race.
            if _open_existing(stack, parent, directory=True) is None:
                raise UnsafePath("[HOLD] The key folder changed.")
        # Recheck after input, including a destination created during a prompt.
        with ExitStack() as destination:
            current = _open_existing(destination, path)
            if not from_stdin and handle is None and current is not None:
                print(CANCELLED)
                return 2
        _save(path, data)
    print(f"[OK] Saved to {path}. Only this Windows user can open it.")
    return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if any(arg not in ("--check", "--from-stdin") for arg in args):
        print("[HOLD] Use only --check or --from-stdin.")
        return 2
    if message := checkout_path_hold(Path(__file__).absolute().parents[1]):
        print(f"[HOLD] {message}")
        return 6
    profile = os.environ.get("USERPROFILE", "")
    if not profile.strip():
        print("[HOLD] USERPROFILE is not set.")
        return 2
    if not Path(profile).is_absolute():
        print("[HOLD] USERPROFILE must be an absolute path.")
        return 2
    try:
        import pywintypes
    except ImportError:
        print("[HOLD] Run tools\\setup.cmd to install the Windows dependencies.")
        return 2
    try:
        return _run(Path(profile), check="--check" in args, from_stdin="--from-stdin" in args)
    except ImportError:
        print("[HOLD] Run tools\\setup.cmd to install the Windows dependencies.")
    except (OSError, pywintypes.error) as error:
        code = getattr(error, "winerror", None)
        detail = f" (Windows error {code})" if isinstance(code, int) else ""
        print(f"[HOLD] Could not safely access the key file.{detail}")
    except UnsafePath as error:
        print(error)
    except (EOFError, KeyboardInterrupt):
        print(CANCELLED)
        return 2
    except getpass.GetPassWarning:
        print("[HOLD] Hidden key input is unavailable.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
