"""Synthetic tests for the per-user tunnel key file tool and local launcher.

USERPROFILE points at a disposable fixture, so the real key file, the real
tunnel configuration and the private launcher are never read. No tunnel-client,
MCP endpoint, Owner GUI or network traffic is started.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import os
import shutil
import socket
import subprocess
import sys
import sysconfig
import unittest
import venv
from pathlib import Path
from unittest.mock import Mock, patch, sentinel

from tests._inherited_test_temp import inherited_test_temp
from tools import scn_tunnel_key

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "scn_tunnel_key.py"
PS_WRAPPER = ROOT / "tools" / "Set-NextTunnelKey.ps1"
CMD_WRAPPER = ROOT / "tools" / "set-tunnel-key.cmd"
LAUNCHER = ROOT / "tools" / "start-sora-commander-local.cmd"
SELECTOR = ROOT / "tools" / "scn_tunnel_client.py"
LAUNCH_CHECK = ROOT / "tools" / "scn_launch_check.py"
PS = Path(os.environ.get("SystemRoot", "")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
KEY = "DUMMY-ONLY-NO-NETWORK-0123456789"
LOOPBACK = socket.inet_ntoa(bytes((127, 0, 0, 1)))


@unittest.skipUnless(os.name == "nt", "Windows required")
class TunnelKeyFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One entry check per run. A scratch denial stops this entire class.
        cls.fixture = Path(cls.enterClassContext(inherited_test_temp()))

    def setUp(self):
        self.home = self.fixture / self._testMethodName
        self.home.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if key.upper() != "CONTROL_PLANE_API_KEY"
        }
        self.env["USERPROFILE"] = self.env["LOCALAPPDATA"] = str(self.home)
        self.folder = self.home / ".config" / "tunnel-client"
        self.key_file = self.folder / "sora-next.key"
        # A disposable copy, so tests need no untracked tunnel binary.
        # The stub components are only checked for existence, never run.
        tools = self.home / "app" / "tools"
        self.executable = tools / "tunnel-client" / "v0.1.0" / "tunnel-client.exe"
        self.executable.parent.mkdir(parents=True)
        for source in (TOOL, PS_WRAPPER, CMD_WRAPPER, LAUNCHER, SELECTOR, LAUNCH_CHECK):
            shutil.copyfile(source, tools / source.name)
        (tools / "next-development.cmd").write_text("TEST DATA ONLY",
                                                    encoding="ascii")
        self.executable.write_text("TEST DATA ONLY", encoding="ascii")
        self.launcher_copy = tools / LAUNCHER.name

    def tool(self, *args, stdin=None):
        return subprocess.run(
            [sys.executable, "-I", "-B", str(TOOL), *args],
            input=stdin, text=True, capture_output=True, env=self.env,
            timeout=30,
        )

    def prepare_venv(self, root):
        target = root / ".venv"
        venv.EnvBuilder().create(target)
        # Use the runner's installed pywin32, including its own .pth hooks.
        packages = sysconfig.get_path("purelib")
        (target / "Lib/site-packages/test_deps.pth").write_text(
            f"import site; site.addsitedir({ascii(packages)})\n", encoding="ascii")

    def launcher(self, *args):
        self.prepare_venv(self.launcher_copy.parents[1])
        return subprocess.run(
            [self.env["COMSPEC"], "/d", "/c", str(self.launcher_copy), *args],
            text=True, capture_output=True, env=self.env, timeout=30,
        )

    def write_key(self, data):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.key_file.write_bytes(data)

    def assert_no_key(self, result):
        self.assertNotIn(KEY, result.stdout + result.stderr)

    def test_stored_key_has_exact_bytes_and_passes_check(self):
        stored = self.tool("--from-stdin", stdin=KEY)
        self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
        self.assert_no_key(stored)
        self.assertEqual(self.key_file.read_bytes(), KEY.encode("ascii"))
        self.assertEqual([path.name for path in self.folder.iterdir()],
                         ["sora-next.key"])
        checked = self.tool("--check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assert_no_key(checked)

    @unittest.skipUnless(PS.is_absolute() and PS.is_file(), "Independent Windows PowerShell ACL inspection")
    def test_stored_key_is_private_to_this_user(self):
        stored = self.tool("--from-stdin", stdin=KEY)
        self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
        script = (
            "$a=Get-Acl -LiteralPath $env:KEYPATH;"
            "$me=[Security.Principal.WindowsIdentity]::GetCurrent().User;"
            "$r=@($a.GetAccessRules($true,$true,"
            "[Security.Principal.SecurityIdentifier]));"
            "if($a.AreAccessRulesProtected -and $r.Count -eq 1 -and "
            "$r[0].IdentityReference -eq $me -and !$r[0].IsInherited -and "
            "$r[0].AccessControlType -eq 'Allow' -and "
            "$r[0].FileSystemRights -eq 'FullControl'){exit 0}else{exit 1}"
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
                result = self.tool("--from-stdin", stdin=text)
                self.assertEqual(result.returncode, 5,
                                 result.stdout + result.stderr)
                self.assert_no_key(result)
                self.assertFalse(self.key_file.exists())

    def test_invalid_input_keeps_an_existing_key(self):
        self.write_key(KEY.encode("ascii"))
        result = self.tool("--from-stdin", stdin="short")
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
                result = self.tool("--check")
                self.assertEqual(result.returncode, 5,
                                 result.stdout + result.stderr)
                self.assert_no_key(result)

    def test_hand_written_clean_file_is_accepted(self):
        self.write_key(KEY.encode("ascii"))
        result = self.tool("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_no_key(result)

    def test_missing_file_holds(self):
        result = self.tool("--check")
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
        self.assertEqual(result.stderr.count("[INFO] Selected tunnel-client v0.1.0."), 1)
        self.assert_no_key(result)
        self.assertNotIn("Single tunnel start", result.stdout)

    def test_launcher_check_holds_when_no_tunnel_client_is_installed(self):
        self.prepare_clean_check()
        self.executable.rename(self.executable.with_suffix(".test"))
        result = self.launcher("--check")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("[HOLD] Put tunnel-client.exe at", result.stderr)
        self.assertIn("newer version folder", result.stderr)

    def prepare_clean_check(self):
        self.write_key(KEY.encode("ascii"))
        (self.folder / "sora-next.yaml").write_text("TEST DATA ONLY",
                                                    encoding="ascii")

    def test_launcher_check_holds_while_a_tunnel_process_runs(self):
        self.prepare_clean_check()
        # The stand-in waits on an empty pipe and never contacts the network.
        system_root = Path(os.environ["SystemRoot"])
        fake = self.home / "tunnel-client.exe"
        shutil.copyfile(system_root / "System32" / "sort.exe", fake)
        child = subprocess.Popen(
            [str(fake)], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            result = self.launcher("--check")
        finally:
            child.kill()
            child.wait(timeout=10)
            child.stdin.close()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("[HOLD]", result.stdout)
        self.assertNotIn("Nothing started", result.stdout)

    def test_launcher_check_holds_while_the_port_is_taken(self):
        self.prepare_clean_check()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        try:
            listener.bind((LOOPBACK, 8080))
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
        self.assertEqual(self.invoke([KEY]), 2)
        self.assertFalse(self.folder.exists())

    def invoke(self, argv, stdin=b""):
        output = io.StringIO()
        with (patch.dict(os.environ, USERPROFILE=str(self.home), LOCALAPPDATA=str(self.home)),
              patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin))),
              redirect_stdout(output), redirect_stderr(output)):
            code = scn_tunnel_key.main(argv)
        self.assertNotIn(KEY, output.getvalue())
        return code

    def test_check_exact_byte_boundaries(self):
        for size, code in ((7, 5), (8, 0), (4096, 0), (4097, 5), (8192, 5), (8193, 5)):
            with self.subTest(size=size):
                self.write_key(b"~" * size)
                self.assertEqual(self.invoke(["--check"]), code)
        for invalid in (b"\x00", b"\x1f", b"\x7f", b"\xff", b"\n"):
            self.write_key(KEY.encode() + invalid)
            self.assertEqual(self.invoke(["--check"]), 5)

    def test_interactive_replacement_requires_exact_y(self):
        self.write_key(b"old-demo-value")
        with patch.object(scn_tunnel_key.getpass, "getpass", return_value=KEY) as hidden:
            for answer in ("", "Y", "yes", " y"):
                with patch("builtins.input", return_value=answer):
                    self.assertEqual(self.invoke([]), 2)
                hidden.assert_not_called()
                self.assertEqual(self.key_file.read_bytes(), b"old-demo-value")
            with patch("builtins.input", return_value="y"):
                self.assertEqual(self.invoke([]), 0)
            hidden.assert_called_once()
        self.assertEqual(self.key_file.read_bytes(), KEY.encode())

    def test_reparse_file_and_parent_stop_before_input_or_read(self):
        import win32con
        import win32file

        self.write_key(KEY.encode())
        create = win32file.CreateFile
        info = win32file.GetFileInformationByHandle
        for marked in (self.folder.parent, self.folder, self.key_file):
            handles = {}

            def remember(path, *args):
                handle = create(path, *args)
                if Path(path) == marked:
                    self.assertTrue(args[4] & win32file.FILE_FLAG_OPEN_REPARSE_POINT)
                handles[int(handle)] = Path(path)
                return handle

            def reparse(handle):
                result = info(handle)
                if handles[int(handle)] == marked:
                    return (result[0] | win32con.FILE_ATTRIBUTE_REPARSE_POINT, *result[1:])
                return result

            with (patch.object(win32file, "CreateFile", side_effect=remember),
                  patch.object(win32file, "GetFileInformationByHandle", side_effect=reparse),
                  patch.object(win32file, "ReadFile") as read,
                  patch.object(scn_tunnel_key, "_save") as save,
                  patch.object(scn_tunnel_key.getpass, "getpass") as hidden):
                for args in (["--check"], [], ["--from-stdin"]):
                    self.assertEqual(self.invoke(args), 2)
                read.assert_not_called()
                save.assert_not_called()
                hidden.assert_not_called()

    def test_empty_temp_is_protected_before_write_on_same_handle(self):
        import win32file

        order = Mock()
        write = win32file.WriteFile

        def protect(handle):
            temporary, = self.folder.glob("sora-next.key.*.tmp")
            self.assertEqual(temporary.stat().st_size, 0)
            order.protect(int(handle))

        def write_checked(handle, data):
            order.write(int(handle))
            return write(handle, data)

        with (patch.object(scn_tunnel_key, "_protect", side_effect=protect),
              patch.object(win32file, "WriteFile", side_effect=write_checked)):
            self.assertEqual(self.invoke(["--from-stdin"], KEY.encode()), 0)
        first, second = order.mock_calls
        self.assertEqual((first[0], second[0]), ("protect", "write"))
        self.assertEqual(first.args, second.args)
        self.assertEqual(self.key_file.read_bytes(), KEY.encode())

    def test_failed_acl_write_or_replace_removes_temp_and_keeps_key(self):
        self.write_key(b"old-demo-value")
        for operation in ("tools.scn_tunnel_key._protect", "win32file.WriteFile", "os.replace"):
            with self.subTest(operation=operation), patch(operation, side_effect=OSError(KEY)):
                self.assertEqual(self.invoke(["--from-stdin"], KEY.encode()), 2)
                self.assertEqual(list(self.folder.iterdir()), [self.key_file])
                self.assertEqual(self.key_file.read_bytes(), b"old-demo-value")

    @unittest.skipUnless(PS.is_absolute() and PS.is_file(), "Windows PowerShell wrapper required")
    def test_legacy_wrapper_forwards_both_switches(self):
        # Use the synthetic checkout: the built public tree has no .venv.
        wrapper = self.launcher_copy.parent / PS_WRAPPER.name
        self.prepare_venv(self.launcher_copy.parents[1])
        for flag, code, stdin in (("-Check", 4, None), ("-FromStdin", 0, KEY), ("-Check", 0, None)):
            result = subprocess.run(
                [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(wrapper), flag],
                input=stdin, capture_output=True, text=True, env=self.env, timeout=30)
            self.assert_no_key(result)
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual(self.key_file.read_bytes(), KEY.encode())

    def test_cmd_supports_normal_path_and_refuses_unsafe_checkout(self):
        for name, code in (("ordinary", 4), ("has space", 6), ("bad&%PATH%^!()", 6)):
            root = self.home / name
            tools = root / "tools"
            tools.mkdir(parents=True)
            for source in (TOOL, SELECTOR, CMD_WRAPPER):
                shutil.copyfile(source, tools / source.name)
            self.prepare_venv(root)
            result = subprocess.run(
                [self.env["COMSPEC"], "/d", "/c", ".\\" + CMD_WRAPPER.name, "--check"], cwd=tools, env=self.env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
            self.assertFalse(self.folder.exists())


class TunnelKeyProfileTests(unittest.TestCase):
    def test_unset_userprofile_holds_before_file_access(self):
        out = io.StringIO()
        with (patch.dict(os.environ, {}, clear=True), redirect_stdout(out),
              patch.object(scn_tunnel_key, "_run") as run):
            self.assertEqual(scn_tunnel_key.main(["--check"]), 2)
            self.assertIn("[HOLD] USERPROFILE is not set.", out.getvalue())
            run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows ACL types required")
    def test_protected_acl_for_token_sid_and_fail_closed_readback(self):
        import ntsecuritycon
        import win32api
        import win32con
        import win32security as security

        sid = security.CreateWellKnownSid(security.WinBuiltinGuestsSid)
        token = Mock()
        descriptor = Mock()
        with (patch.object(win32api, "GetCurrentProcess", return_value=sentinel.process),
              patch.object(security, "OpenProcessToken", return_value=token) as open_token,
              patch.object(security, "GetTokenInformation", return_value=(sid, 0)) as identity,
              patch.object(security, "SetSecurityInfo") as apply_acl,
              patch.object(security, "GetSecurityInfo", return_value=descriptor)):
            descriptor.GetSecurityDescriptorControl.return_value = (security.SE_DACL_PROTECTED, 1)
            descriptor.GetSecurityDescriptorDacl.side_effect = lambda: apply_acl.call_args.args[5]
            scn_tunnel_key._protect(sentinel.file)
            open_token.assert_called_once_with(sentinel.process, win32con.TOKEN_QUERY)
            identity.assert_called_once_with(token, security.TokenUser)
            token.Close.assert_called_once()
            handle, kind, flags, owner, group, acl, sacl = apply_acl.call_args.args
            self.assertEqual((handle, kind), (sentinel.file, security.SE_FILE_OBJECT))
            self.assertEqual(flags, security.DACL_SECURITY_INFORMATION |
                             security.PROTECTED_DACL_SECURITY_INFORMATION)
            self.assertEqual((owner, group, sacl), (None, None, None))
            self.assertEqual(acl.GetAceCount(), 1)
            self.assertEqual(acl.GetAce(0), ((security.ACCESS_ALLOWED_ACE_TYPE, 0),
                                             ntsecuritycon.FILE_ALL_ACCESS, sid))
            descriptor.GetSecurityDescriptorControl.return_value = (0, 1)
            with self.assertRaises(scn_tunnel_key.UnsafePath):
                scn_tunnel_key._protect(sentinel.file)


if __name__ == "__main__":
    unittest.main()
