"""Prepare a Windows checkout and list the remaining manual setup steps.

Exit codes: 0 when no step is on HOLD (TODOs are allowed), 2 for HOLD,
and 6 for an unsupported checkout path.
"""

import argparse
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

if __package__:
    from .scn_tunnel_client import INSTALL_HINT, checkout_path_hold, select_tunnel_client
else:
    sys.path.insert(0, str(Path(__file__).absolute().parent))
    from scn_tunnel_client import INSTALL_HINT, checkout_path_hold, select_tunnel_client


ROOT = Path(__file__).absolute().parents[1]
REQUIREMENTS = ("mcp>=1,<2", "pydantic>=2,<3", "pywin32>=306")
TUNNEL_GUIDE = "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
CHATGPT_GUIDE = "https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt"
KEY_COMMAND = r"tools\set-tunnel-key.cmd"
VERSION_PROBE = """
from pathlib import Path
import sys
sys.exit(sys.version_info[:2] != (3, 13)
         or sys.prefix == sys.base_prefix
         or Path(sys.prefix) != Path(sys.argv[1]))
"""
PACKAGE_PROBE = """
from importlib.metadata import version, PackageNotFoundError
import sys
try:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.version import InvalidVersion
except ImportError:
    sys.exit(2)
for spec in sys.argv[1:]:
    requirement = Requirement(spec)
    try:
        installed = version(requirement.name)
        if not requirement.specifier.contains(installed, prereleases=True):
            sys.exit(1)
    except (PackageNotFoundError, InvalidVersion):
        sys.exit(1)
"""
MUSE_DOCTOR = """
from pathlib import Path
import sys
sys.path.insert(0, str(Path(sys.argv[1]).parent))
import scn_doctor
scn_doctor.CHECKS = tuple(check for check in scn_doctor.CHECKS
                          if check is not scn_doctor.check_tunnel)
raise SystemExit(scn_doctor.main([]))
"""


class SetupHold(Exception):
    """A prerequisite or setup operation failed."""


def entry(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def is_link(info):
    return (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def run(command, root, *, quiet=False, env=None):
    return subprocess.run(
        command, cwd=root, env=env, check=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
    ).returncode


def check_location(root):
    if str(root).isascii():
        print("[OK] Checkout path uses ASCII and contains no spaces.")
        return
    report = root / "docs/SCN_NON_ASCII_PATHS_RESULT_2026-11-20.md"
    try:
        lines = report.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    # Require an explicit positive result about live launchers in the report.
    if any(line.strip().lower() == "- live launchers work on non-ascii checkout paths."
           for line in lines):
        print("[OK] Live launchers are verified for non-ASCII checkout paths.")
    else:
        print("[TODO] Caution: live launchers on non-ASCII checkout paths are unverified. "
              "Use an ASCII checkout path if startup fails.")


def prepare_python(root, check, environ):
    venv = root / ".venv"
    python = venv / "Scripts/python.exe"
    existing = entry(venv)
    if existing is None:
        if check:
            print("[TODO] Create the Python 3.13 .venv.")
            print("[TODO] Install the packages from INSTALL section 1.")
            return None
        venv.mkdir()  # Claim a new directory before invoking venv.
        if run(["py", "-3.13", "-I", "-B", "-m", "venv", ".venv"], root):
            raise SetupHold("Creating .venv failed. Inspect it before continuing.")
    elif is_link(existing) or not stat.S_ISDIR(existing.st_mode):
        raise SetupHold(".venv must be an ordinary Python 3.13 environment directory.")

    command = [str(python), "-I", "-B"]
    scripts_info = entry(python.parent)
    if (
        scripts_info is None or is_link(scripts_info) or not python.is_file()
        or run([*command, "-c", VERSION_PROBE, str(venv)], root, quiet=True)
    ):
        raise SetupHold(".venv does not provide a working Python 3.13 virtual environment.")
    print("[OK] Existing .venv uses Python 3.13." if existing else "[DONE] Created Python 3.13 .venv.")

    status = run([*command, "-c", PACKAGE_PROBE, *REQUIREMENTS], root, quiet=True)
    if status == 0:
        print("[OK] The packages from INSTALL section 1 are satisfied.")
    elif status != 1:
        raise SetupHold("Could not check package versions with .venv pip.")
    elif check:
        print("[TODO] Install or update the packages from INSTALL section 1.")
    else:
        pip_env = {name: value for name, value in environ.items()
                   if not name.upper().startswith("PIP_")}
        pip_env["PIP_CONFIG_FILE"] = os.devnull
        print("[TODO] Installing packages from PyPI.", flush=True)
        if run([*command, "-m", "pip", "--require-virtualenv", "--no-input",
                "--disable-pip-version-check", "--no-cache-dir", "install",
                "--index-url", "https://pypi.org/simple", *REQUIREMENTS], root, env=pip_env):
            raise SetupHold("pip could not install the packages from INSTALL section 1.")
        print("[DONE] Installed the packages from INSTALL section 1.")
    return command


def report_tunnel(root, profile, check):
    executable = select_tunnel_client(root)
    if executable is not None:
        print(f"[OK] tunnel-client {executable.parent.name} is present.")
    else:
        print(f"[TODO] {INSTALL_HINT} {TUNNEL_GUIDE}")

    config = profile / ".config"
    tunnel = config / "tunnel-client"
    for directory in (profile, config, tunnel):
        info = entry(directory)
        if info is not None and (is_link(info) or not stat.S_ISDIR(info.st_mode)):
            raise SetupHold("The tunnel configuration path must use ordinary directories.")
    key_info = entry(tunnel / "sora-next.key")
    if key_info is not None and stat.S_ISREG(key_info.st_mode) and not is_link(key_info):
        print("[OK] The tunnel key file is present.")
    else:
        print(f"[TODO] Create a tunnel and a runtime API key as in INSTALL section 3, then run {KEY_COMMAND}")

    yaml = tunnel / "sora-next.yaml"
    if entry(yaml) is not None:
        print("[OK] sora-next.yaml already exists.")
    elif check:
        print("[TODO] Copy sora-next.example.yaml to the tunnel configuration folder.")
    else:
        source = root / "docs/sora-next.example.yaml"
        source_info = entry(source)
        if source_info is None:
            source = root / "docs/public_release/sora-next.example.yaml"
            source_info = entry(source)
        if (source_info is None or not stat.S_ISREG(source_info.st_mode)
                or is_link(source_info) or source_info.st_nlink != 1):
            raise SetupHold("An ordinary sora-next.example.yaml template is required.")
        template = source.read_bytes()
        tunnel.mkdir(parents=True, exist_ok=True)
        # Write a complete temporary file first, then link it into place: the
        # link never replaces an existing file, and a failed write never leaves
        # a partial sora-next.yaml behind.
        temporary = tunnel / f".sora-next.yaml.{os.getpid()}.tmp"
        try:
            with temporary.open("xb") as output:
                output.write(template)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, yaml)
            except FileExistsError:
                print("[OK] sora-next.yaml already exists.")
            else:
                print("[DONE] Copied sora-next.example.yaml to the tunnel configuration folder.")
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    print("[TODO] Ensure the three marked values in sora-next.yaml are replaced "
          "as in INSTALL section 4.")


def finish(root, command, muse_only, environ):
    held = False
    if command is None:
        print("[TODO] Run scn_doctor.py once .venv is available.")
    else:
        doctor = str(root / "tools/scn_doctor.py")
        args = ["-c", MUSE_DOCTOR, doctor] if muse_only else [doctor]
        print("[OK] Running the read-only setup doctor.", flush=True)
        held = run([*command, *args], root, env=environ) != 0
        print("[HOLD] Setup doctor reported a problem." if held else "[OK] Setup doctor finished.")

    print("\nNext steps")
    if muse_only:
        print(r"[TODO] Copy docs\muse-bridge-peers.example.json to "
              r"%USERPROFILE%\.config\sora-commander\muse-bridge-peers.json and put "
              "your Muse agent's StableID in readonly (docs/muse-bridge.md, Setup).")
        print("[TODO] In Tailscale Access controls, limit the Muse node to this PC's "
              "tcp:8765 as described in docs/muse-bridge.md.")
        print(r"[TODO] Start the Muse bridge with tools\start-muse-bridge-tailscale.cmd.")
    else:
        print(r"[TODO] Start Broker mode with tools\start-broker-mode.cmd (INSTALL section 5).")
        print(f"[TODO] Connect ChatGPT using your Secure MCP Tunnel (INSTALL section 6). {CHATGPT_GUIDE}")
    return 2 if held else 0


def main(argv=None, *, root=ROOT, environ=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report without making changes")
    parser.add_argument("--muse-only", action="store_true", help="skip ChatGPT tunnel setup")
    args = parser.parse_args(argv)
    if message := checkout_path_hold(root):
        print(f"[HOLD] {message}")
        return 6
    environ = os.environ if environ is None else environ
    try:
        if sys.platform != "win32" or sys.version_info[:2] != (3, 13):
            raise SetupHold("Windows and Python 3.13 are required.")
        if not shutil.which("git", path=environ.get("PATH", "")):
            raise SetupHold("Install Git and make it available on PATH.")
        profile = Path(environ.get("USERPROFILE", ""))
        if not profile.is_absolute():
            raise SetupHold("USERPROFILE must be set to an absolute path.")
        print("[OK] Windows, Python 3.13 and Git are available.")
        check_location(root)
        command = prepare_python(root, args.check, environ)
        if not args.muse_only:
            report_tunnel(root, profile, args.check)
        if (profile / ".config/sora-commander/allowed-roots.txt").is_file():
            print("[OK] allowed-roots.txt is present.")
        else:
            print(r"[TODO] Choose allowed folders in tools\scn_config_builder.py "
                  "(docs/config-builder.md) or use the manual format in INSTALL section 4.")
        return finish(root, command, args.muse_only, environ)
    except (OSError, SetupHold) as error:
        print(f"[HOLD] {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
