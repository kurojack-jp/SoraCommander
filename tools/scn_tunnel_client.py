"""Select a checkout-local tunnel executable without following links."""

from pathlib import Path
import re
import stat
import subprocess
import sys


TUNNEL_CLIENT_DIR = Path("tools/tunnel-client")
INSTALL_HINT = (
    r"Put tunnel-client.exe at tools\tunnel-client\v0.0.16\tunnel-client.exe "
    "or in a newer version folder."
)
VERSION = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")
LAUNCHER_PATH = re.compile(rb"tools\\tunnel-client\\v[0-9]+\.[0-9]+\.[0-9]+\\tunnel-client\.exe\r?\n?")


def checkout_path_hold(root: Path) -> str | None:
    if " " in str(root):
        return "The folder path contains a space. Move Sora Commander to a folder without spaces."
    if any(char in '&%^!()"<>|' or not char.isprintable() for char in str(root)):
        return "The folder path contains an unsupported character. Move Sora Commander to a simpler path."
    return None


def launcher_path(root: Path) -> int:
    """Publish the selector path only after checking its complete wire output."""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(Path(__file__).absolute())], cwd=root,
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.stderr:
        sys.stderr.write(result.stderr.decode("ascii", errors="replace").replace("\r\n", "\n"))
    if result is None or result.returncode or not LAUNCHER_PATH.fullmatch(result.stdout):
        if result is None or not result.stderr or not result.returncode:
            print("[HOLD] Could not validate the tunnel-client selection.", file=sys.stderr)
        return 2
    print(result.stdout.decode("ascii").rstrip("\r\n"))
    return 0


def _ordinary(path: Path, kind: int) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_IFMT(info.st_mode) == kind
        and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        and (kind != stat.S_IFREG or info.st_nlink == 1)
    )


def select_tunnel_client(root: Path) -> Path | None:
    """Return the highest numeric version's absolute path, if present."""
    root = root.absolute()
    folder = root / TUNNEL_CLIENT_DIR
    try:
        for directory in (*reversed(root.parents), root, folder.parent, folder):
            if not _ordinary(directory, stat.S_IFDIR):
                return None
        candidates = []
        for directory in folder.iterdir():
            version = VERSION.fullmatch(directory.name)
            if version is None or not _ordinary(directory, stat.S_IFDIR):
                continue
            executable = directory / "tunnel-client.exe"
            if _ordinary(executable, stat.S_IFREG):
                number = tuple(int(part) for part in version.groups())
                candidates.append((number, directory.name, executable))
        return max(candidates)[2] if candidates else None
    except OSError:
        return None


def main(root: Path) -> int:
    root = root.absolute()
    executable = select_tunnel_client(root)
    if executable is None:
        print(f"[HOLD] {INSTALL_HINT}", file=sys.stderr)
        return 2
    print(f"[INFO] Selected tunnel-client {executable.parent.name}.", file=sys.stderr)
    # The batch caller already has the checkout path. Keep stdout ASCII so its
    # active code page cannot alter the checkout path when it captures the result.
    print(executable.relative_to(root))
    return 0


if __name__ == "__main__":
    checkout = Path(__file__).absolute().parents[1]
    if message := checkout_path_hold(checkout):
        print(f"[HOLD] {message}")
        raise SystemExit(6)
    if sys.argv[1:] == ["--check-checkout"]:
        raise SystemExit(0)
    if sys.argv[1:] == ["--launcher-path"]:
        raise SystemExit(launcher_path(checkout))
    raise SystemExit(main(checkout))
