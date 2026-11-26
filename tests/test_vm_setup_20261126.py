"""Host-side bundle preparation with disposable configuration and synthetic keys."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests._inherited_test_temp import inherited_test_temp
from tools import scn_vm_setup as host
from tools.vm_guest import vm_guest as guest


ROOT = Path(__file__).resolve().parents[1]
VM_NAME = "Bundle VM"
GUEST_USER = "vmuser"
ADDRESS = ".".join(map(str, (10, 77, 77, 2)))
HOST = ".".join(map(str, (10, 77, 77, 1)))
PORT = 45678


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(inherited_test_temp()))
        self.out = self.temp / "vm-guest"
        self.checkout = self.temp / "checkout"
        self.checkout.mkdir()
        self.home = self.temp / "profile"
        self.settings = self.home / ".config" / "sora-commander" / "vm-control.json"
        self.settings.parent.mkdir(parents=True)
        self.key = self.home / "synthetic.key"
        self.raw = dict(vm_name=VM_NAME, address=ADDRESS, bind_local=HOST, port=PORT, key_file=str(self.key),
                        send_folder=str(self.home / "send"), receive_folder=str(self.home / "receive"),
                        max_minutes=12, max_inputs=42, tools=["screenshot", "wait", "file_put", "file_get"])
        for field in ("send_folder", "receive_folder"):
            Path(self.raw[field]).mkdir()
        self.settings.write_text(json.dumps(self.raw), encoding="utf-8")
        self.enterContext(patch.dict(os.environ, USERPROFILE=str(self.home), LOCALAPPDATA=str(self.home)))
        self.enterContext(patch.object(host, "CHECKOUT", self.checkout))

    def run_setup(self, *flags, output=None, user=GUEST_USER):
        text, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(text), redirect_stderr(errors):
            code = host.main(["make-guest", "--guest-user", user, "--out", str(output or self.out), *flags])
        return code, text.getvalue(), errors.getvalue()

    def old_config(self):
        folder = self.temp / "previous"
        folder.mkdir(exist_ok=True)
        path = folder / guest.CONFIG
        path.write_text(json.dumps(dict(format=1, vm_name=VM_NAME, guest_user=GUEST_USER,
                                       address=ADDRESS, host_address=HOST, port=PORT,
                                       max_minutes=12, max_inputs=42, tools=self.raw["tools"])), encoding="utf-8")
        return ("--previous-guest-config", str(path), "--previous-host-key", str(self.key))

    def activate(self, *flags):
        text, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(text), redirect_stderr(errors):
            code = host.main(["activate-key", "--guest-user", GUEST_USER, *flags])
        return code, text.getvalue(), errors.getvalue()

    def refused(self, *flags, output=None, user=GUEST_USER, reason=None):
        code, text, errors = self.run_setup(*flags, output=output, user=user)
        self.assertEqual((code, text), (2, ""))
        self.assertIn("[HOLD]", errors)
        if reason:
            self.assertIn(reason, errors)

    def assert_no_output(self):
        self.assertFalse(self.out.exists())
        self.assertFalse(self.key.exists())

    def test_missing_bad_and_duplicate_host_configuration(self):
        valid = self.settings.read_text(encoding="utf-8")
        for bad in (None, "{", valid.replace("{", '{"port": 1024,', 1), json.dumps(self.raw | {"port": True})):
            with self.subTest(bad=bad is not None):
                if bad is None:
                    self.settings.unlink()
                else:
                    self.settings.write_text(bad, encoding="utf-8")
                self.refused(reason="vm-control.json is missing" if bad is None else "invalid or unreadable")
                self.assert_no_output()

    def test_bad_guest_users_leave_nothing(self):
        for name in ("", "a" * 21, ".", "b.", r"domain\user", "user/name", "x&y", "x%X%", "x\ny", " name", "x@y|z"):
            with self.subTest(name=name):
                self.refused(user=name, reason="invalid local account")
                self.assert_no_output()

    def test_output_must_be_a_new_vm_guest_on_a_local_drive_outside_checkout(self):
        for path in (Path("vm-guest"), self.temp / "other",
                     self.temp / "missing" / "vm-guest", self.checkout / "vm-guest"):
            with self.subTest(path=path.name):
                self.refused(output=path)
                if path.is_absolute():
                    self.assertFalse(path.exists())
                self.assert_no_output()
        with patch.object(host, "_local_drive", return_value=False):
            self.refused()
            self.assert_no_output()
        self.out.mkdir()
        with patch.object(Path, "mkdir", side_effect=AssertionError("unexpected output creation")):
            self.refused(reason="already exists")
        self.assertEqual(list(self.out.iterdir()), [])
        self.out.rmdir()
        self.out.write_bytes(b"keep")
        self.refused()
        self.assertEqual(self.out.read_bytes(), b"keep")
        self.assertFalse(self.key.exists())

    def test_unc_paths_are_refused_before_any_filesystem_access(self):
        path = Path(r"\\server\share\vm-guest")
        for target, method in ((Path, "lstat"), (Path, "stat"), (Path, "resolve"), (Path, "open"),
                               (Path, "mkdir"), (host, "_local_drive")):
            self.enterContext(patch.object(target, method, side_effect=AssertionError("unexpected path access")))
        with self.assertRaisesRegex(guest.Refused, "use an absolute local drive"):
            host._out_folder(path)
        with self.assertRaisesRegex(guest.Refused, "ordinary absolute path"):
            guest.plain(path, directory=True)
        with (patch.dict(os.environ, USERPROFILE=str(path)),
              patch.object(host, "load_vm_control", side_effect=AssertionError("unexpected profile access"))):
            self.refused(reason="the host profile must be on a local drive")

    def test_host_rejects_nonordinary_spelling_before_using_output_parent(self):
        for name in ("suffix.", "suffix ", "file:stream"):
            output = self.temp / name / "vm-guest"
            with self.subTest(name=name), patch.object(guest, "plain") as plain:
                self.refused(output=output, reason="use an absolute local drive")
                plain.assert_not_called()

    def test_unexpected_guest_source_set_is_refused_before_writes(self):
        source = self.temp / "bundle-source"
        source.mkdir()
        with patch.object(host, "GUEST_SOURCE", source):
            self.refused("--update", reason="unexpected guest source file set")
        self.assert_no_output()

    def test_bundle_source_link_is_refused_and_cleans_output(self):
        source = host.AGENT_SOURCE / guest.AGENT_FILES[0]
        lstat = Path.lstat

        def linked(path):
            info = lstat(path)
            if path == source:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with patch.object(Path, "lstat", linked):
            self.refused("--update", reason="not an ordinary file")
        self.assert_no_output()

    def test_reparse_point_in_output_parent_is_refused_before_creation(self):
        parent = self.temp / "linked-parent"
        parent.mkdir()
        output = parent / "vm-guest"
        original = Path.lstat

        def lstat(path):
            info = original(path)
            if path == parent:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with patch.object(Path, "lstat", lstat):
            self.refused(output=output, reason="not an ordinary directory")
        self.assertFalse(output.exists())
        self.assertFalse(self.key.exists())

    def test_exact_bundle_keys_config_digests_and_instructions(self):
        code, out, errors = self.run_setup()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual({path.name for path in self.out.iterdir()}, set(guest.BUNDLE_FILES) | {guest.KEY})
        key = self.key.read_bytes()
        self.assertRegex(key, rb"^[0-9a-f]{64}\n$")
        self.assertEqual((self.out / guest.KEY).read_bytes(), key)
        self.assertNotIn(key.strip().decode(), out)
        self.assertNotIn(hashlib.sha256(key).hexdigest(), out)
        raw = (self.out / guest.CONFIG).read_bytes()
        self.assertNotIn(key.strip(), raw)
        self.assertEqual(json.loads(raw), dict(format=1, vm_name=VM_NAME, guest_user=GUEST_USER,
                         address=ADDRESS, host_address=HOST, port=PORT, max_minutes=12, max_inputs=42,
                         tools=self.raw["tools"]))
        for name in guest.BUNDLE_FILES:
            digest = hashlib.sha256((self.out / name).read_bytes()).hexdigest()
            self.assertIn(f"{digest} {name}", out)
        manifest = json.loads((self.out / guest.MANIFEST).read_bytes())
        self.assertEqual(manifest, dict(format=1, vm_name=VM_NAME, sha256={
            name: hashlib.sha256((self.out / name).read_bytes()).hexdigest() for name in guest.HASHED_FILES}))
        self.assertNotIn(key.strip(), (self.out / guest.MANIFEST).read_bytes())
        for action in ("check.cmd", "firewall-admin.cmd", "install.cmd", "Delete the vm-guest folder"):
            self.assertIn(action, out)
        self.assertIn("Run firewall-admin.cmd as administrator from the incoming folder. "
                      "It checks the bundle against the manifest automatically. "
                      "Then run install.cmd as the guest user.", out)
        self.assertNotIn("manifest SHA-256", out)

    def test_key_bundle_steps_remove_the_incoming_vm_copy_after_install(self):
        cleanup = r"delete the vm-guest copy under C:\SoraFromHost in the VM because it holds a key"
        for flags in ((), ("--replace-key",)):
            with self.subTest(flags=flags):
                if flags:
                    self.out = self.temp / "replacement" / "vm-guest"
                    self.out.parent.mkdir()
                code, out, errors = self.run_setup(*flags, *(self.old_config() if flags else ()))
                self.assertEqual((code, errors), (0, ""))
                self.assertIn(cleanup, out)
                self.assertGreater(out.index(cleanup), out.index("install.cmd"))
        self.out = self.temp / "update" / "vm-guest"
        self.out.parent.mkdir()
        code, out, errors = self.run_setup("--update")
        self.assertEqual((code, errors), (0, ""))
        self.assertNotIn(cleanup, out)

    def test_update_never_accesses_host_key(self):
        original = Path.open

        def open_file(path, *args, **kwargs):
            self.assertNotEqual(path, self.key)
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", open_file), patch.object(host.vm_client, "run") as make_key:
            code, out, errors = self.run_setup("--update")
        self.assertEqual((code, errors), (0, ""))
        make_key.assert_not_called()
        self.assertFalse(self.key.exists())
        self.assertEqual({item.name for item in self.out.iterdir()}, set(guest.BUNDLE_FILES))
        self.assertNotIn(guest.KEY, out)
        self.assertIn("update.cmd", out)

    def test_existing_key_is_kept_and_replace_requires_it(self):
        self.refused("--replace-key")
        self.assert_no_output()
        first = host.vm_client.secrets.token_hex(32).encode()
        self.key.write_bytes(first)
        with patch.object(host.vm_client, "run") as generate:
            self.refused(reason="already exists")
        generate.assert_not_called()
        self.assertFalse(self.out.exists())
        self.assertEqual(self.key.read_bytes(), first)
        code, output, errors = self.run_setup("--update")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.key.read_bytes(), first)
        self.assertNotIn(first.decode(), output)

    def test_atomic_replacement_after_bundle_and_rollback_on_failure(self):
        first = host.vm_client.secrets.token_hex(32).encode() + b"\n"
        self.key.write_bytes(first)
        flags = self.old_config()
        pending, receipt, backup = host._rotation_paths(self.key)
        code, out, errors = self.run_setup("--replace-key", *flags)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("current host key is still active", out)
        self.assertEqual(self.key.read_bytes(), first)
        self.assertEqual(pending.read_bytes(), (self.out / guest.KEY).read_bytes())
        self.assertTrue(receipt.is_file())

        def fail_replace(staged, target):
            self.assertEqual(target, self.key)
            self.assertEqual(staged.parent, target.parent)
            self.assertEqual(target.read_bytes(), first)
            self.assertEqual(staged.read_bytes(), (self.out / guest.KEY).read_bytes())
            self.assertEqual({item.name for item in self.out.iterdir()}, set(guest.BUNDLE_FILES) | {guest.KEY})
            raise OSError("synthetic replacement failure")

        with patch.object(host.os, "replace", side_effect=fail_replace) as replace:
            code, out, errors = self.activate(*flags)
            self.assertEqual(code, 2)
            self.assertIn(str(backup), out)
            self.assertIn("synthetic replacement failure", errors)
            replace.assert_called_once()
        self.assertEqual(self.key.read_bytes(), first)
        self.assertEqual(backup.read_bytes(), first)
        self.assertTrue(pending.is_file())
        backup.unlink()
        code, out, errors = self.activate(*flags)
        self.assertEqual((code, errors), (0, ""))
        self.assertNotEqual(self.key.read_bytes(), first)
        self.assertEqual(self.key.read_bytes(), (self.out / guest.KEY).read_bytes())
        self.assertNotIn(self.key.read_text().strip(), out)
        self.assertEqual(backup.read_bytes(), first)
        self.assertFalse(pending.exists())

    def test_failure_cleans_its_own_files_and_reports_foreign_file_left(self):
        original = guest.write_new

        def fail(path, data, made):
            original(path, data, made)
            if path.name == guest.CONFIG:
                (self.out / "other.txt").write_bytes(b"keep")
                raise OSError("synthetic write failure")

        with patch.object(guest, "write_new", fail):
            code, output, error = self.run_setup()
        self.assertEqual(code, 2)
        self.assertIn("Left in place", output)
        self.assertIn("synthetic write failure", error)
        self.assertEqual([item.name for item in self.out.iterdir()], ["other.txt"])
        self.assertFalse(self.key.exists())

    def test_replace_key_refuses_if_it_changes_before_the_replace(self):
        self.key.write_bytes(b"synthetic existing key")
        alternate = self.key.with_name("alternate.key")
        alternate.write_bytes(b"synthetic changed key")
        original = guest.write_new

        def write_new(path, data, made):
            original(path, data, made)
            if path == host._rotation_paths(self.key)[0]:
                os.replace(alternate, self.key)

        with patch.object(guest, "write_new", write_new):
            self.refused("--replace-key", *self.old_config(), reason="file or directory changed")
        self.assertEqual(self.key.read_bytes(), b"synthetic changed key")
        self.assertFalse(self.out.exists())
        self.assertFalse(any(path.exists() for path in host._rotation_paths(self.key)))

    def test_rotation_refuses_address_port_or_vm_name_changes_before_writes(self):
        self.key.write_bytes(host.vm_client.secrets.token_hex(32).encode())
        flags = self.old_config()
        for change in ({"address": HOST, "bind_local": ADDRESS}, {"port": PORT + 1},
                       {"vm_name": VM_NAME + " changed"}):
            with self.subTest(change=change):
                self.settings.write_text(json.dumps(self.raw | change), encoding="utf-8")
                code, out, errors = self.run_setup("--replace-key", *flags)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("old settings first", errors)
                self.assertIn("Install-SendToVm.ps1 -VmName", errors)
                self.assertFalse(self.out.exists())

    def test_failed_key_generator_leaves_no_bundle(self):
        with patch.object(host.vm_client, "run", return_value=1):
            self.refused(reason="cannot create the key files")
        self.assert_no_output()

    def test_flat_bundle_starts_in_fresh_isolated_python(self):
        self.assertEqual(self.run_setup("--update")[0], 0)
        flat = self.temp / "flat"
        flat.mkdir()
        for item in self.out.iterdir():
            (flat / item.name).write_bytes(item.read_bytes())
        runner = """
import runpy, sys, types
def guard(event, args):
    if event == 'import' and args[0].startswith('sora_commander_next'):
        raise RuntimeError('imported checkout package')
sys.addaudithook(guard)
sys.modules['winreg'] = types.SimpleNamespace(HKEY_LOCAL_MACHINE=0, KEY_READ=0, KEY_WOW64_64KEY=0)
def missing(*args):
    raise OSError('synthetic missing registry key')
sys.modules['winreg'].OpenKey = missing
sys.path.insert(0, '.')
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        for script, flag, status, text in (("vm_guest.py", "check", 1, "Nothing was done."),
                                          ("vm_agent.py", "--help", 0, "--max-input-ops")):
            with self.subTest(script=script):
                done = subprocess.run([sys.executable, "-E", "-s", "-B", "-c", runner, script, flag],
                                      cwd=flat, capture_output=True, text=True, timeout=20)
                self.assertEqual((done.returncode, done.stderr), (status, ""))
                self.assertIn(text, done.stdout)


class BundleSourceTests(unittest.TestCase):
    def test_cmd_sources_are_ascii_crlf(self):
        for path in (ROOT / "tools" / "vm-setup.cmd", *(host.GUEST_SOURCE / (verb + ".cmd") for verb in guest.VERBS)):
            with self.subTest(name=path.name):
                data = path.read_bytes()
                self.assertTrue(data.isascii())
                self.assertTrue(data.endswith(b"\r\n"))
                self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
                self.assertNotIn(b"\r", data.replace(b"\r\n", b""))

    def test_powershell_and_python_bundle_lists_match(self):
        source = (ROOT / "tools/vm_host/Send-ToVm.ps1").read_text(encoding="utf-8-sig")
        rows = re.search(r"\$BundleFiles = @\(([^)]+)\)", source)
        self.assertIsNotNone(rows)
        self.assertRegex(rows[1], r"^\s*'[^']+'(?:\s*,\s*'[^']+')*\s*$")
        self.assertEqual(tuple(re.findall(r"'([^']+)'", rows[1])), guest.BUNDLE_FILES)

    def _stage_sources(self):
        paths = ["tools/scn_vm_setup.py", "tools/vm-setup.cmd", "tools/vm_host/Send-ToVm.ps1",
                 "tools/vm_host/Install-SendToVm.ps1", "tests/test_vm_setup_20261126.py",
                 "tests/test_vm_guest_20261126.py", "tests/test_vm_send_tool_20261125.py",
                 *("tools/vm_guest/" + name for name in guest.GUEST_FILES)]
        result_doc = "docs/SCN_VM_STAGE6B_RESULT_2026-11-26.md"
        if (ROOT / result_doc).is_file():
            paths.append(result_doc)
        return {name: (ROOT / name).read_bytes() for name in paths}

    def test_stage_sources_keep_outside_addresses_on_the_netcheck_line(self):
        chosen = self._stage_sources()
        prohibited = ("Sora" + "TestVM", "sora" + "test", "vm-" + "trial", str(47000 + 650),
                      *(".".join(map(str, (192, 168, 250, end))) for end in (1, 2)))
        for path, data in chosen.items():
            for value in prohibited:
                self.assertNotIn(value.lower().encode(), data.lower(), path)
        guest_path = "tools/vm_guest/vm_guest.py"
        netcheck_lines = [line for line in chosen[guest_path].splitlines()
                          if line.startswith(b"NETCHECK_OUTSIDE = ")]
        self.assertEqual(len(netcheck_lines), 1)
        for address, _ in guest.NETCHECK_OUTSIDE:
            value = address.encode()
            self.assertEqual(netcheck_lines[0].count(value), 1)
            for path, data in chosen.items():
                self.assertEqual(data.count(value), int(path == guest_path), path)

    def test_stage_files_scan_clean_except_exact_outside_netcheck_addresses(self):
        if not (ROOT / "tools/build_public_tree.py").is_file():
            self.skipTest("the public tree omits the development public-tree scanner")
        from tools import build_public_tree

        self.assertEqual(build_public_tree.scan(self._stage_sources()), [])


if __name__ == "__main__":
    unittest.main()
