"""Copied launchers reject spaces before reading real state or starting anything."""
import os
from pathlib import Path
import subprocess
import unittest

from tests._inherited_test_temp import inherited_test_temp

ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = ("start-broker-mode.cmd", "start-sora-commander-broker-dev.cmd",
             "start-owner-broker-dev.cmd", "start-sora-commander-local.cmd")
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
                    for component in ("start-owner-broker-dev.cmd",
                                      "start-sora-commander-broker-dev.cmd",
                                      "next-development.cmd", "next-development-broker-client.cmd"):
                        if component != name:
                            (tools / component).write_bytes(
                                b'@echo off\r\n>"%~dp0called" echo called\r\nexit /b 9\r\n')
                    (tools / "Set-NextTunnelKey.ps1").write_text(
                        "Set-Content -LiteralPath (Join-Path $PSScriptRoot 'called') 'called'; exit 9",
                        encoding="utf-8")
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
        for name in LAUNCHERS:
            with self.subTest(launcher=name):
                data = (ROOT / "tools" / name).read_bytes()
                self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        text = (ROOT / "tools" / "start-sora-commander-broker-dev.cmd").read_text()
        self.assertIn('--mcp.command "cmd.exe /d /c %MCP_FORWARD%"', text)


if __name__ == "__main__":
    unittest.main()
