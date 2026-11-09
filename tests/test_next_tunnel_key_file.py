"""Synthetic tests for the per-user tunnel key file tool and local launcher.

USERPROFILE points at a disposable fixture, so the real key file, the real
tunnel configuration and the private launcher are never read. No tunnel-client,
MCP endpoint, Owner GUI or network traffic is started.
"""
import os
import shutil
import socket
import subprocess
import unittest
from pathlib import Path

from tests._inherited_test_temp import inherited_test_temp

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "Set-NextTunnelKey.ps1"
LAUNCHER = ROOT / "tools" / "start-sora-commander-local.cmd"
PS = shutil.which("powershell.exe")
KEY = "DUMMY-ONLY-NO-NETWORK-0123456789"


@unittest.skipUnless(os.name == "nt" and PS, "Windows PowerShell required")
class TunnelKeyFileTests(unittest.TestCase):
    def setUp(self):
        fixture = inherited_test_temp()
        self.home = Path(fixture.__enter__())
        self.addCleanup(fixture.__exit__, None, None, None)
        self.env = {
            key: value for key, value in os.environ.items()
            if key.upper() != "CONTROL_PLANE_API_KEY"
        }
        self.env["USERPROFILE"] = str(self.home)
        self.folder = self.home / ".config" / "tunnel-client"
        self.key_file = self.folder / "sora-next.key"
        # A disposable copy, so tests need no untracked tunnel binary.
        # The stub components are only checked for existence, never run.
        tools = self.home / "app" / "tools"
        (tools / "tunnel-client" / "v0.0.16").mkdir(parents=True)
        shutil.copyfile(TOOL, tools / TOOL.name)
        shutil.copyfile(LAUNCHER, tools / LAUNCHER.name)
        (tools / "next-development.cmd").write_text("TEST DATA ONLY",
                                                    encoding="ascii")
        (tools / "tunnel-client" / "v0.0.16" / "tunnel-client.exe").write_text(
            "TEST DATA ONLY", encoding="ascii")
        self.launcher_copy = tools / LAUNCHER.name

    def tool(self, *args, stdin=None):
        return subprocess.run(
            [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(TOOL), *args],
            input=stdin, text=True, capture_output=True, env=self.env,
            timeout=30,
        )

    def launcher(self, *args):
        return subprocess.run(
            ["cmd.exe", "/d", "/c", str(self.launcher_copy), *args],
            text=True, capture_output=True, env=self.env, timeout=30,
        )

    def write_key(self, data):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.key_file.write_bytes(data)

    def assert_no_key(self, result):
        self.assertNotIn(KEY, result.stdout + result.stderr)

    def test_stored_key_has_exact_bytes_and_passes_check(self):
        stored = self.tool("-FromStdin", stdin=KEY)
        self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
        self.assert_no_key(stored)
        self.assertEqual(self.key_file.read_bytes(), KEY.encode("ascii"))
        self.assertEqual([path.name for path in self.folder.iterdir()],
                         ["sora-next.key"])
        checked = self.tool("-Check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assert_no_key(checked)

    def test_stored_key_is_private_to_this_user(self):
        stored = self.tool("-FromStdin", stdin=KEY)
        self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
        script = (
            "$a=Get-Acl -LiteralPath $env:KEYPATH;"
            "$me=[Security.Principal.WindowsIdentity]::GetCurrent().User;"
            "$r=@($a.GetAccessRules($true,$true,"
            "[Security.Principal.SecurityIdentifier]));"
            "if($a.AreAccessRulesProtected -and $r.Count -eq 1 -and "
            "$r[0].IdentityReference -eq $me){exit 0}else{exit 1}"
        )
        # From PowerShell 7, its module path would make Windows PowerShell fail to load
        # Get-Acl. Windows PowerShell rebuilds the default path when none is given.
        env = {key: value for key, value in self.env.items()
               if key.upper() != "PSMODULEPATH"}
        env["KEYPATH"] = str(self.key_file)
        result = subprocess.run(
            [PS, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_input_never_writes_a_file(self):
        for text in ("", "short", "two words here", KEY + "\n" + KEY):
            with self.subTest(length=len(text)):
                result = self.tool("-FromStdin", stdin=text)
                self.assertEqual(result.returncode, 5,
                                 result.stdout + result.stderr)
                self.assert_no_key(result)
                self.assertFalse(self.key_file.exists())

    def test_invalid_input_keeps_an_existing_key(self):
        self.write_key(KEY.encode("ascii"))
        result = self.tool("-FromStdin", stdin="short")
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertEqual(self.key_file.read_bytes(), KEY.encode("ascii"))

    def test_hand_written_file_with_common_mistakes_is_rejected(self):
        key = KEY.encode("ascii")
        for name, data in (("bom", b"\xef\xbb\xbf" + key),
                           ("newline", key + b"\r\n"),
                           ("space", key + b" "),
                           ("empty", b""),
                           ("large", b"A" * 8193)):
            with self.subTest(name=name):
                self.write_key(data)
                result = self.tool("-Check")
                self.assertEqual(result.returncode, 5,
                                 result.stdout + result.stderr)
                self.assert_no_key(result)

    def test_hand_written_clean_file_is_accepted(self):
        self.write_key(KEY.encode("ascii"))
        result = self.tool("-Check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_no_key(result)

    def test_missing_file_holds(self):
        result = self.tool("-Check")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)

    def test_launcher_check_holds_without_a_key_file(self):
        result = self.launcher("--check")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("[HOLD]", result.stdout)
        self.assertNotIn("Single tunnel start", result.stdout)

    def test_launcher_check_holds_on_a_bad_key_file(self):
        self.write_key(KEY.encode("ascii") + b"\r\n")
        result = self.launcher("--check")
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assert_no_key(result)

    def test_launcher_check_holds_without_tunnel_configuration(self):
        self.write_key(KEY.encode("ascii"))
        result = self.launcher("--check")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assert_no_key(result)
        self.assertNotIn("Single tunnel start", result.stdout)

    def test_launcher_check_never_starts_anything(self):
        self.write_key(KEY.encode("ascii"))
        (self.folder / "sora-next.yaml").write_text("TEST DATA ONLY",
                                                    encoding="ascii")
        result = self.launcher("--check")
        # 3 means the real tunnel holds port 8080 on this machine.
        self.assertIn(result.returncode, (0, 3), result.stdout + result.stderr)
        self.assert_no_key(result)
        self.assertNotIn("Single tunnel start", result.stdout)

    def prepare_clean_check(self):
        self.write_key(KEY.encode("ascii"))
        (self.folder / "sora-next.yaml").write_text("TEST DATA ONLY",
                                                    encoding="ascii")

    def test_launcher_check_holds_while_a_tunnel_process_runs(self):
        self.prepare_clean_check()
        # A copy of a harmless system program, named like the real tunnel.
        system_root = Path(os.environ["SystemRoot"])
        fake = self.home / "tunnel-client.exe"
        shutil.copyfile(system_root / "System32" / "PING.EXE", fake)
        child = subprocess.Popen(
            [str(fake), "-n", "60", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            result = self.launcher("--check")
        finally:
            child.kill()
            child.wait(timeout=10)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("[HOLD]", result.stdout)
        self.assertNotIn("Nothing started", result.stdout)

    def test_launcher_check_holds_while_the_port_is_taken(self):
        self.prepare_clean_check()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        try:
            listener.bind(("127.0.0.1", 8080))
            listener.listen(1)
        except OSError as error:
            self.skipTest(f"port 8080 is not free on this machine: {error}")
        result = self.launcher("--check")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertNotIn("Nothing started", result.stdout)

    def test_launcher_wiring_is_manual_and_secret_free(self):
        text = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('--control-plane.api-key "%KEYREF%"', text)
        self.assertIn('set "KEYREF=file:%KEYFILE:\\=/%"', text)
        self.assertIn('set "CONTROL_PLANE_API_KEY="', text)
        self.assertNotIn("Watch-NextTunnel", text)
        self.assertEqual(text.count('"%TUNNEL%" run '), 1)
        self.assertIn('if /I not "%~1"=="--check" pause', text)
        self.assertEqual(text.count("exit /b"), 1)

    def test_tool_takes_no_key_argument(self):
        text = TOOL.read_text(encoding="utf-8")
        self.assertIn("-AsSecureString", text)
        self.assertNotIn("Write-Output $key", text)
        self.assertNotRegex(text, r"\[string\]\$(Key|ApiKey)")


if __name__ == "__main__":
    unittest.main()
