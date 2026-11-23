"""Copied launchers reject spaces before reading real state or starting anything."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tests._inherited_test_temp import inherited_test_temp

ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = ("start-broker-mode.cmd", "start-broker-tunnel.cmd",
             "start-broker.cmd", "start-usual-mode.cmd")
WRAPPERS = (
    ("start-owner-broker-dev.cmd", "start-broker.cmd"),
    ("start-sora-commander-broker-dev.cmd", "start-broker-tunnel.cmd"),
    ("next-development-broker-client.cmd", "mcp-broker-client.cmd"),
    ("next-development.cmd", "mcp-direct.cmd"),
    ("start-sora-commander-local.cmd", "start-usual-mode.cmd"),
)
MESSAGE = ("[HOLD] The folder path contains a space. Move Sora Commander to a folder "
           "without spaces, such as C:\\SoraCommander.")


@unittest.skipUnless(os.name == "nt", "Windows batch launchers")
class PublicLaunchPathTests(unittest.TestCase):
    def test_check_rejects_spaces_before_any_component(self):
        with inherited_test_temp() as folder:
            home = Path(folder)
            for index, name in enumerate(LAUNCHERS):
                with self.subTest(launcher=name):
                    checkout = home / f"checkout {index} with spaces"
                    tools = checkout / "tools"
                    tools.mkdir(parents=True)
                    launcher = tools / name
                    launcher.write_bytes((ROOT / "tools" / name).read_bytes())
                    for component in ("start-broker.cmd",
                                      "start-broker-tunnel.cmd",
                                      "mcp-direct.cmd", "mcp-broker-client.cmd"):
                        if component != name:
                            (tools / component).write_bytes(
                                b'@echo off\r\n>"%~dp0called" echo called\r\nexit /b 9\r\n')
                    for helper in ("scn_tunnel_key.py", "scn_launch_check.py"):
                        (tools / helper).write_text(
                            "from pathlib import Path\n"
                            "Path(__file__).with_name('called').touch()\nraise SystemExit(9)\n",
                            encoding="ascii")
                    env = {key: value for key, value in os.environ.items()
                           if not key.startswith("SORA_NEXT_") and key != "CONTROL_PLANE_API_KEY"}
                    env.update(USERPROFILE=str(home / "profile"),
                               LOCALAPPDATA=str(home / "local"))
                    result = subprocess.run(
                        [env["COMSPEC"], "/d", "/c", str(launcher), "--check"],
                        input="\n", capture_output=True, text=True, env=env,
                        cwd=str(checkout), timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
                    self.assertEqual(result.stdout.strip(), MESSAGE)
                    self.assertEqual(result.stderr, "")
                    self.assertFalse((tools / "called").exists())
                    self.assertFalse((home / "profile").exists())
                    self.assertFalse((home / "local").exists())

    def test_batch_files_remain_crlf_and_command_quoting_is_unchanged(self):
        for name in (*LAUNCHERS, *(old for old, _ in WRAPPERS)):
            with self.subTest(launcher=name):
                data = (ROOT / "tools" / name).read_bytes()
                self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        text = (ROOT / "tools" / "start-broker-tunnel.cmd").read_text()
        self.assertIn('--mcp.command "cmd.exe /d /c %MCP_FORWARD%"', text)


    def test_legacy_wrappers_forward_arguments_cwd_stdio_and_exit_code(self):
        with inherited_test_temp() as folder:
            root = Path(folder)
            tools = root / "launcher folder"
            tools.mkdir()
            cwd = root / "caller folder"
            cwd.mkdir()
            stub = tools / "print_argv.py"
            stub.write_text(
                "import json, os, sys\n"
                "print(json.dumps([sys.argv[1:], os.getcwd(), sys.stdin.readline().rstrip('\\n')]))\n"
                "raise SystemExit(37)\n",
                encoding="ascii",
            )
            env = {**os.environ, "USERPROFILE": str(root / "profile"),
                   "LOCALAPPDATA": str(root / "local"), "SCN_TEST_PYTHON": sys.executable}
            cmd = subprocess.list2cmdline([env["COMSPEC"]])
            for old, new in WRAPPERS:
                with self.subTest(launcher=old):
                    launcher = tools / old
                    launcher.write_bytes((ROOT / "tools" / old).read_bytes())
                    (tools / new).write_bytes(
                        b'@echo off\r\n"%SCN_TEST_PYTHON%" -I -B "%~dp0print_argv.py" %*\r\n'
                        b'exit /b %ERRORLEVEL%\r\n'
                    )
                    result = subprocess.run(
                        f'{cmd} /d /v:off /s /c ""{launcher}" --check "two words" "" "a&b^c!" "50% done""',
                        input="synthetic stdin\n", capture_output=True, text=True, env=env,
                        cwd=cwd, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
                    self.assertEqual(result.stderr, "")
                    # Exact stdout also guards the two MCP wrappers against any extra output.
                    self.assertEqual(result.stdout, json.dumps([
                        ["--check", "two words", "", "a&b^c!", "50% done"],
                        str(cwd), "synthetic stdin",
                    ]) + "\n")


if __name__ == "__main__":
    unittest.main()
