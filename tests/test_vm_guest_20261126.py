"""Guest verbs against synthetic registry, user, commands and install folders."""

from contextlib import contextmanager, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch

from sora_commander_next import vm_agent as agent, vm_protocol as protocol, vm_win as win
from tests._inherited_test_temp import inherited_test_temp
from tests.test_vm_setup_20261126 import ADDRESS, GUEST_USER, HOST, PORT, VM_NAME
from tools.vm_guest import vm_guest as guest


PARTIAL = "Some changes may have been made. Run check.cmd and look at the folder before trying again."


def firewall_rule(name, program, action, *, japanese=False):
    labels = ("規則名", "プログラム", "操作") if japanese else ("Rule Name", "Program", "Action")
    return f"{labels[0]}:    {name}\n---------------\n{labels[1]}:    {program}\n{labels[2]}:    {action}"


class GuestTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(inherited_test_temp()))
        self.incoming = self.temp / "SoraFromHost"
        self.source = self.incoming / ("20261126-120000-" + secrets.token_hex(16)) / "vm-guest"
        self.source.mkdir(parents=True)
        self.home = self.temp / "profile"
        self.local = self.home / "local"
        self.local.mkdir(parents=True)
        self.target = self.local / guest.RULE
        self.key = secrets.token_hex(32).encode() + b"\n"
        self.raw = dict(format=1, vm_name=VM_NAME, guest_user=GUEST_USER, address=ADDRESS, host_address=HOST,
                        port=PORT, max_minutes=12, max_inputs=42,
                        tools=["screenshot", "wait", "click", "file_put", "file_get"])
        self.write_bundle(self.source)
        self.system_dir = self.temp / "System32"
        self.system_dir.mkdir()
        for name in ("netsh", "whoami", "icacls", "ipconfig", "route", "nslookup"):
            (self.system_dir / (name + ".exe")).write_bytes(b"fake")

        self.enterContext(patch.dict(sys.modules, vm_agent=agent, vm_protocol=protocol, vm_win=win))
        self.enterContext(patch.dict(os.environ, USERPROFILE=str(self.home), LOCALAPPDATA=str(self.local)))
        self.enterContext(patch.object(guest, "_system_directory", return_value=self.system_dir))
        self.enterContext(patch.object(guest, "_incoming_root", return_value=self.incoming))
        self.user = self.enterContext(patch.object(guest, "_user", return_value=GUEST_USER))
        self.admin = self.enterContext(patch.object(guest, "_admin_group", return_value=False))
        self.elevated = self.enterContext(patch.object(guest, "_elevated", return_value=False))
        self.registry = self.enterContext(patch.object(win, "read_registry", side_effect=self.read_registry))
        self.key_reader = self.enterContext(patch.object(win, "read_key_bytes",
                                                         side_effect=lambda path, _: Path(path).read_bytes()))
        self.command = self.enterContext(patch.object(guest.subprocess, "run", side_effect=self.run_command))
        self.chdir = self.enterContext(patch.object(guest.os, "chdir"))
        self.agent = self.enterContext(patch.object(agent, "main", return_value=0))
        self.stream = MagicMock()
        self.stream.__enter__.return_value = self.stream
        self.socket = self.enterContext(patch.object(guest.socket, "socket", return_value=self.stream))
        self.rule_exists = False
        self.rule_name = guest.RULE
        self.firewall_listing = firewall_rule("Agent allow", sys.executable, "Allow") + "\n\nOk.\n"

    def write_bundle(self, folder, *, include_key=True):
        for name in guest.CODE_FILES:
            (folder / name).write_bytes(name.encode())
        (folder / guest.CONFIG).write_text(json.dumps(self.raw), encoding="utf-8")
        if include_key:
            (folder / guest.KEY).write_bytes(self.key)
        self.write_manifest(folder)

    def write_manifest(self, folder=None):
        folder = folder or self.source
        digests = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                   for name in guest.HASHED_FILES}
        (folder / guest.MANIFEST).write_text(json.dumps(dict(format=1, vm_name=self.raw["vm_name"],
                                                            sha256=digests)), encoding="utf-8")

    def matching_rule(self):
        return firewall_rule(self.rule_name, sys.executable, "Allow") + (
            f"\nEnabled: Yes\nDirection: In\nProfiles: Domain,Private,Public\n"
            f"LocalIP: {ADDRESS}/32\nRemoteIP: {HOST}/32\nProtocol: TCP\n"
            f"LocalPort: {PORT}\nRemotePort: Any\nEdge traversal: No")

    def read_registry(self, key, name):
        if key == agent.BIOS:
            return {"SystemManufacturer": "Microsoft Corporation", "SystemProductName": "Virtual Machine"}[name]
        if key == agent.GUEST:
            return self.raw["vm_name"]
        raise FileNotFoundError("synthetic missing legacy registry key")

    def run_command(self, argv, **kwargs):
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(Path(argv[0]).parent, self.system_dir)
        if argv[1:] == ["advfirewall", "firewall", "show", "rule", "name=all", "dir=in", "verbose"]:
            listing = (self.matching_rule() + "\n\n" if self.rule_exists else "") + self.firewall_listing
            return SimpleNamespace(returncode=0, stdout=listing, stderr="")
        if argv[1:5] == ["advfirewall", "firewall", "show", "rule"]:
            if not self.rule_exists or argv[5] != "name=" + self.rule_name:
                return SimpleNamespace(returncode=1, stdout="No rules match the specified criteria.", stderr="")
            return SimpleNamespace(returncode=0, stdout=self.matching_rule() + "\n\nOk.\n", stderr="")
        if argv[1:5] == ["advfirewall", "firewall", "add", "rule"]:
            self.rule_name = argv[5].removeprefix("name=")
            self.rule_exists = True
        if argv[1:5] == ["advfirewall", "firewall", "delete", "rule"]:
            self.rule_exists = False
        if Path(argv[0]).stem == "nslookup":
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0, stdout="IPv4 Address: " + ADDRESS, stderr="")

    def run_verb(self, verb, *, source=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = guest.main([verb], source=source or self.source)
        output = out.getvalue()
        self.assertNotIn(self.key.strip().decode(), output)
        return code, output

    def refuse(self, verb, reason, *, source=None):
        code, output = self.run_verb(verb, source=source)
        self.assertEqual(code, 1)
        self.assertIn("NG:", output)
        self.assertIn(reason, output)
        self.assertIn("Nothing was done.", output)

    def partial(self, verb, reason, *, source=None):
        code, output = self.run_verb(verb, source=source)
        self.assertEqual(code, 1, output)
        self.assertIn(f"NG: {reason}. {PARTIAL}", output)
        self.assertNotIn("Nothing was done.", output)
        return output

    def test_strict_config_and_no_change_for_every_bad_input(self):
        valid = json.dumps(self.raw)
        changed = {"format": (True, 2), "vm_name": ("", "x\n", "x" * 101), "guest_user": ("a/b", 5),
                   "address": (HOST, "localhost", 1), "host_address": ("localhost", ADDRESS),
                   "port": (True, 1023, 65536, "5678"), "max_minutes": (0, 31, 1.0),
                   "max_inputs": (0, 201, False), "tools": ([], "screenshot", [False], ["wait"],
                             ["screenshot", "screenshot"], ["screenshot", "other"])}
        invalid = ["{" , valid.replace("{", '{"format": 1,', 1), valid + " " * guest.CONFIG_LIMIT,
                   "[]", json.dumps(self.raw | {"extra": 1}), '{"format": Infinity}']
        invalid.extend(json.dumps({key: value for key, value in self.raw.items() if key != field})
                       for field in self.raw)
        invalid.extend(json.dumps(self.raw | {field: value}) for field, cases in changed.items() for value in cases)
        config = self.source / guest.CONFIG
        for data in invalid:
            with self.subTest(data_size=len(data)):
                config.write_text(data, encoding="utf-8")
                self.refuse("install", "invalid or unreadable guest configuration")
                self.assertFalse(self.target.exists())
        config.unlink()
        self.refuse("install", "invalid or unreadable guest configuration")
        self.registry.assert_not_called()
        self.key_reader.assert_not_called()
        self.command.assert_not_called()

    def test_non_json_constants_are_rejected_while_parsing(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaisesRegex(guest.Refused, "non-JSON constant"):
                json.loads(value, parse_constant=guest._constant)

    def test_config_and_bundle_links_are_refused(self):
        original = Path.lstat
        for path, message in ((self.source / guest.CONFIG, "invalid or unreadable guest configuration"),
                              (self.source / guest.KEY, "not an ordinary file"),
                              (self.source, "invalid or unreadable guest configuration")):
            def linked(item):
                info = original(item)
                if item == path:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info

            with self.subTest(path=path.name), patch.object(Path, "lstat", linked):
                self.refuse("install", message)
                self.assertFalse(self.target.exists())
        self.key_reader.assert_not_called()

    def test_plain_paths_require_an_absolute_unaliased_path_and_a_single_link(self):
        with patch.object(Path, "lstat", side_effect=AssertionError("unexpected path access")):
            for path in (Path("relative"), self.source / ".." / "file", self.source / "file:stream"):
                with self.subTest(path=path.name), self.assertRaisesRegex(guest.Refused, "ordinary absolute path"):
                    guest.plain(path, directory=False)
        original = Path.lstat
        key = self.source / guest.KEY

        def multiple_links(path):
            info = original(path)
            if path == key:
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=2)
            return info

        with patch.object(Path, "lstat", multiple_links):
            self.refuse("install", "not an ordinary file")
        self.key_reader.assert_not_called()

    def test_every_verb_requires_bios_and_exact_vm_name(self):
        self.elevated.return_value = True
        for name in ("SystemManufacturer", "SystemProductName", "VirtualMachineName"):
            self.registry.side_effect = lambda key, field: "wrong" if field == name else self.read_registry(key, field)
            for verb in guest.VERBS:
                with self.subTest(name=name, verb=verb):
                    self.refuse(verb, "not the configured Hyper-V guest")
        self.user.assert_not_called()
        self.command.assert_not_called()
        self.agent.assert_not_called()
        self.socket.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_standard_verbs_check_user_and_administrators_before_side_effects(self):
        standard = tuple(verb for verb in guest.VERBS if not verb.startswith("firewall"))
        self.user.return_value = "Other"
        for verb in standard:
            with self.subTest(verb=verb):
                self.refuse(verb, "sign in as the configured guest user")
        self.user.return_value = GUEST_USER.upper()
        self.admin.return_value = True
        for verb in standard:
            if verb != "check":
                with self.subTest(verb=verb):
                    self.refuse(verb, "standard user is required")
        self.command.assert_not_called()
        self.agent.assert_not_called()
        self.socket.assert_not_called()
        self.assertFalse(self.target.exists())
        code, output = self.run_verb("check")
        self.assertEqual(code, 1)
        self.assertIn("NG: administrator", output)
        self.assertIn("3.13 and True", output)
        self.assertIn(ADDRESS, output)
        self.assertEqual([Path(row.args[0][0]).stem for row in self.command.call_args_list], ["ipconfig", "route"])

    def test_group_detection_includes_a_filtered_admin_and_fails_closed(self):
        # Call the original token group reader with a mocked whoami result.
        for text, status, expected in (('"Everyone","S-1-1-0"', 0, False),
                                       ('"Everyone","S-1-1-0"\n"Admins","S-1-5-32-544","Deny only"', 0, True),
                                       ("", 0, None), ('"Everyone","S-1-1-0"', 1, None)):
            with patch.object(guest, "_system", return_value=SimpleNamespace(returncode=status, stdout=text)) as system:
                if expected is None:
                    with self.assertRaises(guest.Refused):
                        ADMIN_GROUP()
                else:
                    self.assertIs(ADMIN_GROUP(), expected)
                system.assert_called_once_with("whoami", "/groups", "/fo", "csv", "/nh", capture=True)

    def test_system_refuses_an_unchecked_executable_path(self):
        (self.system_dir / "netsh.exe").unlink()
        self.elevated.return_value = True
        self.refuse("firewall-admin", "the inbound firewall rules for python.exe could not be read")
        self.command.assert_not_called()

    def test_win32_system_directory_and_user_query_fail_closed(self):
        api = SimpleNamespace(kernel32=SimpleNamespace(GetSystemDirectoryW=MagicMock(return_value=0)),
                              advapi32=SimpleNamespace(GetUserNameW=MagicMock(return_value=False)))
        with patch.object(guest.ctypes, "windll", api):
            with self.assertRaisesRegex(guest.Refused, "cannot determine the Windows system directory"):
                SYSTEM_DIRECTORY()
            with self.assertRaisesRegex(guest.Refused, "cannot determine the signed-in user"):
                USER()

    def test_write_new_is_exclusive_and_rejects_a_short_write(self):
        path = self.source / "exclusive"
        path.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            guest.write_new(path, b"other", {})
        self.assertEqual(path.read_bytes(), b"keep")
        fdopen = os.fdopen

        @contextmanager
        def short_writer(fd, mode):
            with fdopen(fd, mode) as stream:
                yield SimpleNamespace(fileno=stream.fileno, write=lambda _: 0)

        with patch.object(guest.os, "fdopen", short_writer):
            with self.assertRaisesRegex(OSError, "incomplete write"):
                guest.write_new(self.source / "short", b"expected", {})

    def test_firewall_uses_exact_system_argv_and_both_rule_guards(self):
        for verb in ("firewall-admin", "firewall-remove-admin"):
            self.refuse(verb, "Run as administrator")
        self.command.assert_not_called()
        self.elevated.return_value = True
        self.user.return_value = "Different administrator"
        self.refuse("firewall-remove-admin", "does not exist")
        code, output = self.run_verb("firewall-admin")
        self.assertEqual(code, 0, output)
        self.assertIn("OK: verified firewall rule", output)
        netsh = str(self.system_dir / "netsh.exe")
        prefix = [netsh, "advfirewall", "firewall"]
        name = "name=" + self.rule_name
        listing = prefix + ["show", "rule", "name=all", "dir=in", "verbose"]
        scope = ["dir=in", "protocol=TCP", f"localip={ADDRESS}", f"localport={PORT}", f"remoteip={HOST}"]
        self.assertEqual([record.args[0] for record in self.command.call_args_list],
                         [listing, listing, prefix + ["add", "rule", name] + scope +
                          ["action=allow", f"program={sys.executable}", "profile=any", "edge=no"],
                          prefix + ["show", "rule", name, "verbose"], listing])
        self.command.reset_mock()
        self.refuse("firewall-admin", "already exists")
        self.assertEqual(self.run_verb("firewall-remove-admin")[0], 0)
        self.assertEqual([record.args[0] for record in self.command.call_args_list],
                         [listing, listing, prefix + ["show", "rule", name, "verbose"],
                          prefix + ["delete", "rule", name] + scope, prefix + ["show", "rule", name, "verbose"]])
        self.user.assert_not_called()
        self.admin.assert_not_called()

    def test_firewall_lists_only_python_and_refuses_block_in_both_languages(self):
        self.elevated.return_value = True
        for japanese, allow, block in ((False, "Allow", "Block"), (True, "許可", "ブロック")):
            with self.subTest(japanese=japanese):
                self.rule_exists = False
                allowed = firewall_rule("Python allow", sys.executable, allow, japanese=japanese)
                blocked = firewall_rule("Python block", r'"C:\Tools\PYTHON.EXE"', block, japanese=japanese)
                other = firewall_rule("Other block", r"C:\Tools\notpython.exe", block, japanese=japanese)
                self.firewall_listing = "\n\n".join((allowed, other, blocked, "OK" if japanese else "Ok."))
                code, out = self.run_verb("firewall-admin")
                self.assertEqual(code, 1)
                self.assertIn("an inbound Block rule for python.exe was found", out)
                self.assertIn(allowed, out)
                self.assertIn(blocked, out)
                self.assertNotIn(other, out)
                self.assertFalse(self.rule_exists)

    def test_firewall_listing_errors_are_never_reported_as_no_block_rule(self):
        self.elevated.return_value = True
        valid = firewall_rule("Python allow", sys.executable, "Allow")
        invalid = ("", "unexpected\nOk.", valid, valid + "\n\nunexpected footer",
                   valid.replace("Allow", "unexpected") + "\n\nOk.",
                   valid.replace("---------------", "unexpected") + "\n\nOk.",
                   valid.replace(sys.executable, sys.executable + "\ufffd") + "\n\nOk.",
                   valid.replace(sys.executable, '"' + sys.executable) + "\n\nOk.",
                   valid.replace(sys.executable, sys.executable + '"x') + "\n\nOk.")
        for listing in invalid:
            with self.subTest(listing=listing):
                self.rule_exists = False
                self.firewall_listing = listing
                self.refuse("firewall-admin", "the inbound firewall rules for python.exe could not be read")

    def test_firewall_listing_reads_shapes_of_real_netsh_output(self):
        # Shapes seen in real netsh output on Windows 11 (2026-11-26): rules without a program,
        # descriptions over several lines, and names the console code page cannot show.
        listing = "\n\n".join((
            "規則名:    \ufffd\ufffd サービス\n---------------\n説明:    一行目\n  続きの行\n有効:    はい\n操作:    許可",
            firewall_rule("Python allow", sys.executable, "許可", japanese=True)
            .replace("---------------", "---------------\n説明:    説明の一行目\n  説明の二行目"),
            firewall_rule("Other app", r"C:\Tools\other.exe", "ブロック", japanese=True),
        )) + "\n\nOk."
        rows = guest._firewall_rows(subprocess.CompletedProcess([], 0, listing, ""))
        self.assertEqual([(row[0], row[1], row[2]) for row in rows],
                         [("Python allow", sys.executable, False), ("Other app", r"C:\Tools\other.exe", True)])

    def test_tool_output_is_read_as_utf8_or_the_windows_code_page(self):
        # Windows 11 netsh wrote UTF-8 under code page 932 on the Owner's PC (2026-11-26).
        text = firewall_rule("Python allow", sys.executable, "許可", japanese=True) + "\n\nOk."
        locale_text = text.encode("utf-8").decode("utf-8")
        with patch("locale.getencoding", return_value="cp932"):
            for data in (text.encode("utf-8"), text.encode("cp932")):
                with self.subTest(data=data[:8]):
                    decoded = guest._decode(data)
                    self.assertEqual(decoded, locale_text)
                    rows = guest._firewall_rows(subprocess.CompletedProcess([], 0, decoded, ""))
                    self.assertEqual([(row[1], row[2]) for row in rows], [(sys.executable, False)])
        self.assertEqual(guest._decode("already text"), "already text")

    def test_rule_scope_matches_the_real_japanese_netsh_listing(self):
        # Copied field by field from the real rule netsh showed in the new VM (2026-11-26).
        listing = chr(10).join((
            "規則名:                                SoraVmAgent-" + "0" * 32,
            "----------------------------------------------------------------------",
            "有効:                                  はい",
            "方向:                                  入力",
            "プロファイル:                          ドメイン,プライベート,パブリック",
            "グループ:                              ",
            f"ローカル IP:                           {ADDRESS}/32",
            f"リモート IP:                           {HOST}/32",
            "プロトコル:                            TCP",
            f"ローカル ポート:                       {self.raw['port']}",
            "リモート ポート:                       任意",
            "エッジ トラバーサル:                   いいえ",
            f"プログラム:                            {sys.executable}",
            "インターフェイスの種類:                任意",
            "セキュリティ:                          NotRequired",
            "規則のソース:                          ローカル設定",
            "操作:                                  許可",
            "OK"))
        rows = guest._firewall_rows(subprocess.CompletedProcess([], 0, listing, ""))
        self.assertEqual(len(rows), 1)
        self.assertTrue(guest._rule_matches(rows[0], self.raw))

    def test_admin_rejects_installed_or_mixed_incoming_bundle(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        self.command.reset_mock()
        self.elevated.return_value = True
        for verb in ("firewall-admin", "firewall-remove-admin"):
            self.refuse(verb, "incoming SoraFromHost bundle", source=self.target)
        (self.source / "vm_guest.py").write_bytes(b"untrusted")
        for verb in ("firewall-admin", "firewall-remove-admin"):
            self.refuse(verb, "bundle hash mismatch: vm_guest.py")
        self.command.assert_not_called()

    def test_admin_accepts_the_incoming_bundle_from_the_existing_launcher(self):
        self.elevated.return_value = True
        out = io.StringIO()
        with patch.object(guest, "__file__", str(self.source / "vm_guest.py")), \
                patch.object(guest.sys, "flags", SimpleNamespace(isolated=0)), redirect_stdout(out):
            self.assertEqual(guest.main(["firewall-admin"]), 0)
            (self.source / guest.KEY).unlink()
            self.assertEqual(guest.main(["firewall-remove-admin"]), 0)

    def test_install_and_update_refuse_tampered_manifest_members(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        (self.source / guest.CODE_FILES[0]).write_bytes(b"changed")
        for verb in ("install", "update"):
            self.refuse(verb, "bundle hash mismatch")
        self.socket.assert_not_called()

    def test_post_add_block_rolls_back_only_the_same_scoped_rule(self):
        self.elevated.return_value = True
        for changed in (False, True):
            with self.subTest(changed=changed):
                self.rule_exists = False
                matching = self.matching_rule

                def command(argv, **kw):
                    result = self.run_command(argv, **kw)
                    if self.rule_exists and argv[3:6] == ["show", "rule", "name=all"]:
                        result.stdout = (matching() + "\n\n"
                                         + firewall_rule("new block", sys.executable, "Block") + "\n\nOk.")
                        if changed:
                            self.matching_rule = lambda: matching().replace(f"LocalPort: {PORT}", "LocalPort: 9999")
                    return result

                self.command.side_effect = command
                try:
                    code, output = self.run_verb("firewall-admin")
                finally:
                    self.matching_rule = matching
                self.assertEqual(code, 1)
                self.assertIn("Could not safely confirm removal" if changed else "verified absent", output)
                self.assertEqual(self.rule_exists, changed)

    def test_firewall_show_failures_are_reported_after_add(self):
        self.elevated.return_value = True
        for status, stderr, raises, named in ((1, "", False, False), (0, "error", False, False),
                                              (0, "", True, False), (1, "", False, True)):
            with self.subTest(status=status, stderr=stderr, raises=raises, named=named):
                self.rule_exists = False

                def fails_show(argv, **kwargs):
                    result = self.run_command(argv, **kwargs)
                    rule_filter = "name=" + self.rule_name if named else "name=all"
                    if self.rule_exists and argv[3:6] == ["show", "rule", rule_filter]:
                        if raises:
                            raise OSError("synthetic listing failure")
                        result.returncode, result.stderr = status, stderr
                    return result

                self.command.side_effect = fails_show
                code, output = self.run_verb("firewall-admin")
                self.assertEqual(code, 1)
                self.assertIn("could not be read", output)
                self.assertIn("Could not safely confirm removal" if named else "Rolled back firewall rule", output)
                self.assertEqual(self.rule_exists, named)

    def test_firewall_mutation_failure_reports_uncertain_changes_for_both_verbs(self):
        self.elevated.return_value = True
        for verb, exists in (("firewall-admin", False), ("firewall-remove-admin", True)):
            with self.subTest(verb=verb):
                self.rule_exists = exists

                def fails_after_mutation(argv, **kwargs):
                    result = self.run_command(argv, **kwargs)
                    if argv[3] in ("add", "delete"):
                        result.returncode = 1
                    return result

                self.command.side_effect = fails_after_mutation
                code, output = self.run_verb(verb)
                self.assertEqual(code, 1)
                self.assertIn("firewall add failed" if not exists else "firewall delete failed", output)
                self.assertIn(PARTIAL, output)
                self.assertEqual(self.rule_exists, not exists)

    def test_firewall_indeterminate_state_never_mutates(self):
        self.elevated.return_value = True
        self.command.side_effect = None
        self.command.return_value = SimpleNamespace(returncode=1, stdout="unexpected", stderr="error")
        for verb in ("firewall-admin", "firewall-remove-admin"):
            self.refuse(verb, "the inbound firewall rules for python.exe could not be read")
        self.assertEqual([item.args[0][3] for item in self.command.call_args_list], ["show", "show"])

    def test_install_refuses_bad_sets_keys_and_existing_target(self):
        self.target.mkdir()
        self.refuse("install", guest.RULE)
        self.assertEqual(list(self.target.iterdir()), [])
        self.target.rmdir()
        key_path = self.source / guest.KEY
        key_path.unlink()
        self.refuse("install", "unexpected bundle file set")
        key_path.write_bytes(b"invalid")
        self.refuse("install", "could not be verified")
        key_path.write_bytes(self.key)
        extra = self.source / "extra"
        for directory in (True, False):
            if directory:
                extra.mkdir()
            else:
                extra.write_bytes(b"extra")
            self.refuse("install", "unexpected bundle file set")
            extra.rmdir() if directory else extra.unlink()
        self.assertFalse(self.target.exists())
        self.command.assert_not_called()

    def test_install_checks_copy_and_invokes_icacls_from_system_directory(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        self.assertEqual({file.name for file in self.target.iterdir()}, set(guest.BUNDLE_FILES) | {guest.KEY})
        for item in self.target.iterdir():
            self.assertEqual(item.read_bytes(), (self.source / item.name).read_bytes())
        self.command.assert_called_once()
        self.assertEqual(self.command.call_args.args[0],
                         [str(self.system_dir / "icacls.exe"), str(self.target / guest.KEY)])

    def test_install_copy_mismatch_cleans_up(self):
        original = guest.write_new

        def corrupt(path, data, made):
            original(path, data, made)
            path.write_bytes(b"wrong")

        with patch.object(guest, "write_new", corrupt):
            self.refuse("install", "copy differs")
        self.assertFalse(self.target.exists())
        self.command.assert_not_called()

    def test_install_rollback_reports_leftover_folder(self):
        original = guest.write_new

        def fail_after_write(path, data, made):
            original(path, data, made)
            (self.target / "foreign").write_bytes(b"keep")
            raise OSError("synthetic copy failure")

        with patch.object(guest, "write_new", fail_after_write):
            out = self.partial("install", "synthetic copy failure")
        self.assertIn("Left in place:", out)
        self.assertEqual([path.name for path in self.target.iterdir()], ["foreign"])

    def test_rollback_preserves_a_replaced_file_and_directory(self):
        file = self.source / "replacement"
        file.write_bytes(b"keep")
        folder = self.source / "replacement-dir"
        folder.mkdir()
        with patch.object(guest.os.path, "samestat", return_value=False), redirect_stdout(io.StringIO()):
            self.assertTrue(guest.rollback({file: file.lstat()}, (folder, folder.lstat())))
        self.assertEqual(file.read_bytes(), b"keep")
        self.assertTrue(folder.is_dir())

    def test_install_failure_to_show_key_acl_rolls_back(self):
        self.command.side_effect = None
        self.command.return_value = SimpleNamespace(returncode=1)
        self.refuse("install", "cannot show key ACL")
        self.assertFalse(self.target.exists())

    def test_update_and_remove_require_known_install_and_free_port(self):
        for verb in ("update", "remove"):
            self.refuse(verb, "invalid or unreadable guest configuration")
        self.socket.assert_not_called()
        self.assertEqual(self.run_verb("install")[0], 0)
        keep = {path.name: path.read_bytes() for path in self.target.iterdir()}
        self.stream.bind.side_effect = OSError("busy")
        for verb in ("update", "remove"):
            self.refuse(verb, "stop the agent")
            self.assertEqual({path.name: path.read_bytes() for path in self.target.iterdir()}, keep)
        self.assertEqual(self.stream.bind.call_args_list, [call((ADDRESS, PORT))] * 2)
        self.assertEqual(self.stream.__exit__.call_count, 2)

    def test_update_refuses_different_config_and_duplicate_source(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        (self.source / guest.CONFIG).write_text(json.dumps(self.raw | {"port": PORT + 1}), encoding="utf-8")
        self.write_manifest()
        self.refuse("update", "differs from the installed configuration")
        self.socket.assert_not_called()
        self.refuse("update", "run update from the incoming bundle", source=self.target)

    def test_missing_installed_code_stops_update_and_start_but_removal_can_resume(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        (self.target / guest.CODE_FILES[0]).unlink()
        for verb in ("update", "start-view", "start-full"):
            with self.subTest(verb=verb):
                source = self.target if verb.startswith("start") else self.source
                self.refuse(verb, guest.CODE_FILES[0], source=source)
        self.agent.assert_not_called()
        self.socket.assert_not_called()
        self.assertEqual(self.run_verb("remove")[0], 0)

    def test_update_replaces_only_code_and_cmds_and_remove_keeps_other_files(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        original_json = (self.target / guest.CONFIG).read_bytes()
        for name in guest.CODE_FILES:
            (self.source / name).write_bytes(b"new " + name.encode())
        self.write_manifest()
        (self.source / guest.KEY).unlink()
        self.key_reader.reset_mock()
        self.assertEqual(self.run_verb("update")[0], 0)
        for name in guest.CODE_FILES:
            self.assertEqual((self.target / name).read_bytes(), (self.source / name).read_bytes())
        self.assertEqual((self.target / guest.KEY).read_bytes(), self.key)
        self.assertEqual((self.target / guest.CONFIG).read_bytes(), original_json)
        self.key_reader.assert_not_called()
        (self.target / "keep").write_bytes(b"keep")
        code, message = self.run_verb("remove", source=self.target)
        self.assertEqual(code, 0)
        self.assertIn("SoraVmFiles, the firewall rule, and the host key stay in place", message)
        self.assertEqual([path.name for path in self.target.iterdir()], ["keep"])
        self.assertEqual(self.run_verb("remove")[0], 1)

    def test_remove_empty_install_folder(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        self.assertEqual(self.run_verb("remove", source=self.target)[0], 0)
        self.assertFalse(self.target.exists())

    def test_remove_leaves_current_directory_before_rmdir_and_reports_empty_folder(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        rmdir = Path.rmdir

        def fail_rmdir(path):
            if path != self.target:
                return rmdir(path)
            self.chdir.assert_called_once_with(self.target.parent)
            self.assertEqual(list(path.iterdir()), [])
            raise OSError("synthetic folder removal failure")

        with patch.object(Path, "rmdir", fail_rmdir):
            code, out = self.run_verb("remove", source=self.target)
        self.assertEqual(code, 0)
        self.assertIn("The installed files were removed. The empty folder stays:", out)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_remove_failure_after_first_unlink_reports_partial_changes(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        unlink = Path.unlink
        first, second = (self.target / name for name in guest.BUNDLE_FILES[:2])

        def fail_unlink(path, *args, **kwargs):
            if path == second:
                raise OSError("synthetic unlink failure")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_unlink):
            self.partial("remove", "synthetic unlink failure", source=self.target)
        self.assertFalse(first.exists())
        self.assertTrue(second.is_file())
        self.chdir.assert_not_called()
        self.assertEqual(self.run_verb("remove")[0], 0)
        self.assertFalse(self.target.exists())

    def test_update_failure_after_staging_reports_partial_changes(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        keep = {path.name: path.read_bytes() for path in self.target.iterdir()}
        with patch.object(guest.os, "replace", side_effect=OSError("synthetic update failure")):
            self.partial("update", "synthetic update failure")
        self.assertEqual({path.name: path.read_bytes() for path in self.target.iterdir()}, keep)

    def test_update_holds_port_and_rechecks_before_replacing(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        destination = self.target / guest.CODE_FILES[0]
        original = guest.write_new

        def change_target(path, data, made):
            self.stream.__exit__.assert_not_called()
            original(path, data, made)
            destination.write_bytes(b"concurrent edit")

        with patch.object(guest, "write_new", side_effect=change_target):
            code, output = self.run_verb("update")
        self.assertEqual(code, 1)
        self.assertIn("file or directory changed", output)
        self.assertEqual(destination.read_bytes(), b"concurrent edit")
        self.stream.__exit__.assert_called_once()

    def test_update_compares_staged_bytes_before_replacing(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        keep = {path.name: path.read_bytes() for path in self.target.iterdir()}
        original = guest.write_new

        def corrupt(path, data, made):
            original(path, data + b"wrong", made)

        with patch.object(guest, "write_new", corrupt):
            self.partial("update", f"copy differs: {guest.CODE_FILES[0]}")
        self.assertEqual({path.name: path.read_bytes() for path in self.target.iterdir()}, keep)

    def test_start_uses_installed_agent_and_exact_view_and_full_arguments(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        self.refuse("start-view", "run the start command from the installed folder")
        self.agent.assert_not_called()
        base = ["--vm-name", VM_NAME, "--bind", ADDRESS, "--allow-peer", HOST, "--port", str(PORT),
                "--key-file", str(self.target / guest.KEY), "--max-minutes", "12", "--max-input-ops", "42"]
        self.assertEqual(self.run_verb("start-view", source=self.target)[0], 0)
        self.agent.assert_called_once_with(base + ["--tools", "screenshot,wait"])
        files = self.home / "SoraVmFiles"
        self.assertFalse(files.exists())
        self.agent.reset_mock()
        code, output = self.run_verb("start-full", source=self.target)
        self.assertEqual(code, 0)
        self.assertIn("This start lets the host click", output)
        self.assertIn("42 inputs, 12 minutes", output)
        self.agent.assert_called_once_with(base + ["--tools", ",".join(self.raw["tools"]), "--files-dir", str(files)])
        self.assertEqual({path.name for path in files.iterdir()}, {"inbox", "outbox"})

    def test_start_full_without_file_tools_does_not_create_file_folders(self):
        self.raw["tools"] = ["screenshot", "wait", "click"]
        (self.source / guest.CONFIG).write_text(json.dumps(self.raw), encoding="utf-8")
        self.write_manifest()
        self.assertEqual(self.run_verb("install")[0], 0)
        self.assertEqual(self.run_verb("start-full", source=self.target)[0], 0)
        self.assertNotIn("--files-dir", self.agent.call_args.args[0])
        self.assertFalse((self.home / "SoraVmFiles").exists())

    def test_start_full_failure_after_first_folder_reports_partial_changes(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        files = self.home / "SoraVmFiles"
        mkdir = Path.mkdir

        def fail_mkdir(path, *args, **kwargs):
            if path == files / "inbox":
                raise OSError("synthetic folder creation failure")
            return mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", fail_mkdir):
            self.partial("start-full", "synthetic folder creation failure", source=self.target)
        self.assertEqual(list(files.iterdir()), [])
        self.agent.assert_not_called()

    def test_start_full_refuses_a_file_in_place_of_a_transfer_folder(self):
        self.assertEqual(self.run_verb("install")[0], 0)
        files = self.home / "SoraVmFiles"
        files.mkdir()
        (files / "outbox").write_bytes(b"keep")
        self.partial("start-full", f"not an ordinary directory: {files / 'outbox'}", source=self.target)
        self.assertEqual((files / "outbox").read_bytes(), b"keep")
        self.agent.assert_not_called()

    def test_netcheck_judges_dns_by_lookup_not_by_nslookup_exit_code(self):
        # nslookup exited 0 after a failed lookup in the new VM (2026-11-26).
        self.stream.connect.side_effect = TimeoutError
        with patch.object(guest, "_dns_resolves", return_value=False) as resolves, \
                patch.object(guest.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(self.run_verb("netcheck")[0], 0)
        resolves.assert_called_once_with("example.com")
        with patch.object(guest, "_dns_resolves", return_value=True):
            self.refuse("netcheck", "did not establish blocked connections and failed DNS")
        with patch.object(guest.socket, "getaddrinfo", side_effect=OSError("no DNS")):
            self.assertFalse(guest._dns_resolves("example.com"))
        with patch.object(guest.socket, "getaddrinfo", return_value=[object()]):
            self.assertTrue(guest._dns_resolves("example.com"))

    def test_netcheck_uses_configured_host_ports_and_dns_probe(self):
        self.stream.connect.side_effect = TimeoutError
        with patch.object(guest, "_dns_resolves", return_value=False):
            self.assertEqual(self.run_verb("netcheck")[0], 0)
        addresses = [*guest.NETCHECK_OUTSIDE, *((HOST, port) for port in guest.HOST_PORTS)]
        self.assertEqual(self.stream.connect.call_args_list, [call(pair) for pair in addresses])
        self.assertEqual(self.stream.settimeout.call_args_list, [call(3)] * len(addresses))
        self.assertEqual(self.command.call_args.args[0], [str(self.system_dir / "nslookup.exe"),
                                                         "-timeout=2", "example.com"])


ADMIN_GROUP = guest._admin_group
SYSTEM_DIRECTORY = guest._system_directory
USER = guest._user


if __name__ == "__main__":
    unittest.main()
