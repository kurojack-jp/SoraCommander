"""Send-ToVm.ps1 with every Hyper-V command, prompt and elevation replaced. No VM and no administrator."""

import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import unittest

from tests._inherited_test_temp import inherited_test_temp
from tools.vm_guest.vm_guest import BUNDLE_FILES, HASHED_FILES, KEY, MANIFEST


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/vm_host/Send-ToVm.ps1"
INSTALL = ROOT / "tools/vm_host/Install-SendToVm.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
SERVICE = "Microsoft:11111111-2222-3333-4444-555555555555\\6C09BB55-D683-4DA0-8931-C9BF705F6480"
VM_NAME = 'Example "QA" VM'

HARNESS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$case = [IO.File]::ReadAllText($env:SCN_CASE) | ConvertFrom-Json
. $case.script
$script:calls = @()
function Get-VM { param($Name) $script:calls += "Get-VM $Name"
    if ($case.vms -eq 0) { return }
    1..$case.vms | ForEach-Object { [pscustomobject]@{ Name = $case.vm_name; State = $case.state } } }
function Get-VMIntegrationService { param($VM)
    [pscustomobject]@{ Id = 'x\00000000-0000-0000-0000-000000000000'; Enabled = $true }
    [pscustomobject]@{ Id = $case.service; Enabled = [bool]$case.enabled } }
function Copy-VMFile { param($VM, $SourcePath, $DestinationPath, $FileSource, [switch]$CreateFullPath, [switch]$Force)
    if ($SourcePath -match 'fail') { throw 'copy refused' }
    if ($case.mode -eq 'pin') {
        try { [IO.File]::Move($SourcePath, "$SourcePath.old") }
        catch [IO.IOException] { $script:calls += 'SOURCE_PINNED' }
        try {
            $parent = Split-Path -Parent $SourcePath
            [IO.Directory]::Move($parent, "$parent.old")
        }
        catch [IO.IOException] { $script:calls += 'PARENT_PINNED' }
        catch [UnauthorizedAccessException] { $script:calls += 'PARENT_PINNED' }
    }
    $script:calls += "COPY $($VM.Name)|$SourcePath|$DestinationPath|$FileSource|$CreateFullPath|$Force" }
function Replace-Source([string]$Path) {
    $time = [IO.File]::GetLastWriteTimeUtc($Path)
    [IO.File]::Move($Path, "$Path.old")
    [IO.File]::WriteAllBytes($Path, [byte[]]@(90))
    [IO.File]::SetLastWriteTimeUtc($Path, $time)
}
function Read-Host { param($Prompt) $script:calls += 'ASK'
    if ($case.mode -eq 'replace-after-ask') { Replace-Source $case.paths[0] }
    if ($case.mode -eq 'replace-bundle-after-ask') { Replace-Source (Join-Path $case.paths[0] 'vm_agent.py') }
    return $case.answer }
function Start-Process { throw 'no process in a test' }
if ($case.mode -eq 'mapped') { function Test-LocalDrive([string]$Root) { return $false } }
$failure = ''
try {
    if ($case.mode -eq 'arguments') {
        $script:calls += (Get-ElevatedArguments 'C:\Program Files\x.ps1' $case.paths[0] ('A' * 64))
    }
    elseif ($case.mode -eq 'folders') {
        function Get-Date { [DateTime]::new(2026, 11, 26, 15, 20, 30) }
        $script:calls += (New-GuestFolder)
        $script:calls += (New-GuestFolder)
    }
    else {
        $paths = [string[]]$case.paths
        $bytes = Get-PlanBytes (New-SendPlan $paths)
        $hash = Get-PlanHash $bytes
        $planPath = Join-Path $PSScriptRoot ('scn-send-' + [Guid]::NewGuid().ToString('N') + '.json')
        $planFile = [IO.FileStream]::new($planPath, 'CreateNew', 'Write', 'Read', 4096, 'DeleteOnClose')
        try {
            $planFile.Write($bytes, 0, $bytes.Length)
            if ($case.mode -eq 'tamper-plan') { $planFile.WriteByte(32) }
            $planFile.Flush()
            $plan = Read-SendPlan $planPath $hash
            if ($case.mode -eq 'replace-before-ask') { Replace-Source $paths[0] }
            if ($case.mode -eq 'new-file') { [IO.File]::WriteAllText((Join-Path $paths[0] 'new.txt'), 'new') }
            Invoke-Send ([string[]]$plan.Dropped) $hash 6>$null
        }
        finally {
            $planFile.Dispose()
            if ([IO.File]::Exists($planPath)) { [IO.File]::Delete($planPath) }
        }
    }
}
catch { $failure = $_.Exception.Message }
'RESULT ' + (@{ calls = @($script:calls); failure = $failure } | ConvertTo-Json -Compress)
"""


@unittest.skipIf(os.environ.get("SCN_VM_SKIP_POWERSHELL_TESTS") == "1", "PowerShell deferred to Claude")
@unittest.skipUnless(os.name == "nt" and POWERSHELL.exists(), "Windows PowerShell only")
class SendToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(inherited_test_temp()))
        self.files = self.temp / "files"
        self.files.mkdir()
        self.script = self.temp / SCRIPT.name
        self.script.write_bytes(SCRIPT.read_bytes())
        self.config = self.temp / "send-to-vm.json"
        self.home = self.temp / "profile"
        self.home.mkdir()

    def file(self, name, data=b"x"):
        path = self.files / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def run_case(self, *paths, config=json.dumps({"vm_name": VM_NAME}), **changes):
        if config is None:
            if not self.config.is_dir():
                self.config.unlink(missing_ok=True)
        else:
            self.config.write_text(config, encoding="utf-8")
        case = dict(script=str(self.script), paths=list(paths), vms=1, vm_name=VM_NAME, state="Running",
                    service=SERVICE, enabled=True, answer="y", mode="send") | changes
        (self.temp / "case.json").write_text(json.dumps(case), encoding="utf-8")
        (self.temp / "harness.ps1").write_text(HARNESS, encoding="utf-8-sig")
        done = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                               str(self.temp / "harness.ps1")], capture_output=True, timeout=60, cwd=self.files,
                              env=dict(os.environ, SCN_CASE=str(self.temp / "case.json"), USERPROFILE=str(self.home),
                                       LOCALAPPDATA=str(self.home)))
        line = [row for row in done.stdout.decode("utf-8").splitlines() if row.startswith("RESULT ")]
        self.assertEqual(len(line), 1, done.stderr.decode("utf-8", "replace"))
        return json.loads(line[0][7:])

    def copies(self, result):
        return [call.split("|") for call in result["calls"] if call.startswith("COPY ")]

    def assert_refused(self, *paths, **changes):
        result = self.run_case(*paths, **changes)
        self.assertNotEqual(result["failure"], "")
        self.assertEqual(self.copies(result), [])
        return result

    def test_files_and_folders_go_to_one_new_folder_without_overwrite(self):
        first = self.file("日本語 & (1).vrm")
        self.file("素材/深い/声.wav")
        self.file("素材/log.txt")
        result = self.run_case(first, str(self.files / "素材"))
        self.assertEqual(result["failure"], "")
        rows = self.copies(result)
        self.assertEqual({row[0] for row in rows}, {"COPY " + VM_NAME})
        folders = {row[2].split("\\")[2] for row in rows}
        self.assertEqual(len(folders), 1)
        self.assertRegex(folders.pop(), r"^\d{8}-\d{6}-[0-9a-f]{32}$")
        self.assertEqual({row[2].split("\\", 3)[3] for row in rows},
                         {"日本語 & (1).vrm", "素材\\深い\\声.wav", "素材\\log.txt"})
        self.assertTrue(all(row[2].startswith("C:\\SoraFromHost\\") for row in rows))
        self.assertEqual({tuple(row[3:]) for row in rows}, {("Host", "True", "False")})

    def test_nothing_is_sent_without_yes(self):
        leftover = self.temp / "plan.json"
        leftover.write_bytes(b"previous plan")
        for answer in ("", "n", "yes please"):
            result = self.run_case(self.file("a.txt"), answer=answer)
            self.assertEqual((result["failure"], self.copies(result)), ("", []))
            self.assertIn("ASK", result["calls"])
            self.assertEqual(list(self.temp.glob("scn-send-*.json")), [])
        self.assertEqual(leftover.read_bytes(), b"previous plan")

    def test_vm_must_be_the_one_running_vm_with_the_service(self):
        path = self.file("a.txt")
        for change in (dict(vms=0), dict(vms=2), dict(vm_name="Other"), dict(state="Off"), dict(state="Paused"),
                       dict(enabled=False), dict(service="x\\99999999-0000-0000-0000-000000000000")):
            with self.subTest(change=change):
                self.assertNotIn("ASK", self.assert_refused(path, **change)["calls"])

    def test_only_plain_local_files_are_accepted(self):
        good = self.file("a.txt")
        for path in ("a.txt", "\\\\server\\share\\a.txt", "\\\\?\\" + good, "C:\\", str(self.files / "missing"), good + "*"):
            with self.subTest(path=path):
                self.assertEqual(self.assert_refused(path)["calls"], [])
        self.assert_refused()
        self.assert_refused(*[self.file(f"many/{number}.txt") for number in range(51)])

    def test_private_looking_names_stop_everything(self):
        good = self.file("a.txt")
        for name in ("synthetic.key", "b.PEM", "c.pfx", ".env", ".env.local", "id_ed25519", "x/.ssh/known", "y/.git/config"):
            with self.subTest(name=name):
                self.assertEqual(self.assert_refused(good, self.file(name))["calls"], [])
        self.assert_refused(str(self.files))  # Also when one is deeper inside a dropped folder.

    def test_the_same_target_name_twice_is_refused(self):
        self.assert_refused(self.file("one/a.txt"), self.file("two/A.TXT"))

    def test_a_directory_link_is_refused_and_not_followed(self):
        self.file("outside/secret.txt")
        (self.files / "drop").mkdir()
        done = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(self.files / "drop/link"), str(self.files / "outside")],
                              capture_output=True)
        if done.returncode:
            self.skipTest("junction not available")
        try:
            self.assertEqual(self.assert_refused(str(self.files / "drop"))["calls"], [])
            self.assertEqual(self.assert_refused(str(self.files / "drop/link"))["calls"], [])
            self.assertEqual(self.assert_refused(str(self.files / "drop/link/secret.txt"))["calls"], [])
        finally:
            os.rmdir(self.files / "drop/link")

    def test_it_stops_at_the_first_failure(self):
        result = self.run_case(self.file("a.txt"), self.file("fail.txt"), self.file("z.txt"))
        self.assertEqual(result["failure"], "copy refused")
        self.assertEqual([row[1].split("\\")[-1] for row in self.copies(result)], ["a.txt"])

    def test_paths_are_quoted_for_the_administrator_process(self):
        plan = "C:\\a b\\c&d.json"
        result = self.run_case(plan, mode="arguments")
        self.assertEqual(result["calls"], ['-NoProfile -ExecutionPolicy RemoteSigned -File "C:\\Program Files\\x.ps1" '
                                           f'-Elevated -PlanPath "{plan}" -PlanHash {"A" * 64}'])

    def test_ordinary_send_refuses_hard_links_and_mapped_drives(self):
        original = self.file("original.txt")
        alias = self.files / "alias.txt"
        os.link(original, alias)
        try:
            for path in (original, str(alias)):
                self.assertEqual(self.assert_refused(path)["calls"], [])
        finally:
            alias.unlink()
        good = self.file("plain.txt")
        self.assertIn("Only local drives", self.assert_refused(good, mode="mapped")["failure"])

    def test_plan_rejects_replacement_and_changed_folder_membership_before_prompt(self):
        before = self.file("before.txt")
        self.assertEqual(self.assert_refused(before, mode="replace-before-ask")["calls"], [])
        self.file("drop/old.txt")
        self.assertEqual(self.assert_refused(str(self.files / "drop"), mode="new-file")["calls"], [])
        tampered = self.assert_refused(self.file("tampered.txt"), mode="tamper-plan")
        self.assertIn("send plan changed", tampered["failure"])
        self.assertEqual(tampered["calls"], [])

    def test_replacement_at_confirmation_is_refused_and_copy_holds_pins(self):
        after = self.file("after.txt")
        refused = self.assert_refused(after, mode="replace-after-ask")
        self.assertIn("ASK", refused["calls"])
        self.assertIn("Source changed", refused["failure"])
        result = self.run_case(self.file("nested/pin.txt"), mode="pin")
        self.assertEqual(result["failure"], "")
        self.assertIn("SOURCE_PINNED", result["calls"])
        self.assertIn("PARENT_PINNED", result["calls"])
        self.assertEqual(len(self.copies(result)), 1)

    def test_two_sends_with_fixed_clock_get_distinct_folders(self):
        result = self.run_case(mode="folders")
        self.assertEqual(result["failure"], "")
        self.assertEqual(len(set(result["calls"])), 2)
        for folder in result["calls"]:
            self.assertRegex(folder, r"20261126-152030-[0-9a-f]{32}$")

    def test_missing_duplicate_or_invalid_name_file_refuses_before_vm_lookup(self):
        path = self.file("example.txt")
        invalid = (None, "[]", '{"vm_name":2}', '{"VM_NAME":"Example"}',
                   '{"vm_name":"Example","vm_name":"Second"}', '{"vm_name":"Example","extra":true}',
                   '{"vm_name":""}', json.dumps({"vm_name": "x" * 101}),
                   json.dumps({"vm_name": "bad\nname"}), " " * 4097)
        for config in invalid:
            with self.subTest(config=config):
                self.assertEqual(self.assert_refused(path, config=config)["calls"], [])
        self.config.unlink(missing_ok=True)  # The last case above left a file here.
        self.config.mkdir()
        result = self.assert_refused(path, config=None)
        self.assertEqual(result["calls"], [])

    def test_only_single_exact_guest_bundle_can_send_the_key(self):
        folder = str(self.files / "vm-guest")
        for name in HASHED_FILES:
            self.file("vm-guest/" + name)
        manifest = dict(format=1, vm_name=VM_NAME,
                        sha256={name: hashlib.sha256(b"x").hexdigest() for name in HASHED_FILES})
        self.file("vm-guest/" + MANIFEST, json.dumps(manifest, ensure_ascii=False).encode())
        result = self.run_case(folder)
        self.assertEqual(result["failure"], "")
        self.assertEqual(len(self.copies(result)), len(BUNDLE_FILES))
        key = self.file("vm-guest/" + KEY, secrets.token_hex(32).encode() + b"\n")
        result = self.run_case(folder)
        self.assertEqual(result["failure"], "")
        self.assertEqual({row[1].rsplit("\\", 1)[-1] for row in self.copies(result)}, set(BUNDLE_FILES) | {KEY})
        self.assertTrue(self.copies(result)[-1][1].endswith("\\" + KEY))
        self.assertEqual(self.assert_refused(key)["calls"], [])
        self.assertEqual(self.assert_refused(folder, self.file("some.txt"))["calls"], [])
        extra = Path(self.file("vm-guest/extra.txt"))
        self.assertEqual(self.assert_refused(folder)["calls"], [])
        extra.unlink()
        extra.mkdir()
        self.assertEqual(self.assert_refused(folder)["calls"], [])
        extra.rmdir()
        (self.files / "vm-guest" / BUNDLE_FILES[0]).unlink()
        self.assertEqual(self.assert_refused(folder)["calls"], [])

    def test_bundle_manifest_wrong_vm_hash_missing_or_changed_after_prompt_refuses_all(self):
        folder = str(self.files / "vm-guest")
        for name in HASHED_FILES:
            self.file("vm-guest/" + name)
        self.file("vm-guest/" + KEY, secrets.token_hex(32).encode() + b"\n")
        self.assertEqual(self.assert_refused(folder)["calls"], [])
        hashes = {name: hashlib.sha256(b"x").hexdigest() for name in HASHED_FILES}
        for name, digests in ((VM_NAME + " other", hashes),
                              (VM_NAME, hashes | {HASHED_FILES[0]: "0" * 64}),
                              (VM_NAME, {k: v for k, v in hashes.items() if k != HASHED_FILES[0]})):
            manifest = json.dumps(dict(format=1, vm_name=name, sha256=digests), ensure_ascii=False)
            self.file("vm-guest/" + MANIFEST, manifest.encode())
            self.assertEqual(self.assert_refused(folder)["calls"], [])
        self.file("vm-guest/" + MANIFEST, json.dumps(dict(format=1, vm_name=VM_NAME, sha256=hashes),
                                                     ensure_ascii=False).encode())
        result = self.assert_refused(folder, mode="replace-bundle-after-ask")
        self.assertIn("ASK", result["calls"])


class SendToolSourceTests(unittest.TestCase):
    def test_files_keep_the_mark_windows_powershell_needs(self):
        for path in (SCRIPT, INSTALL):
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_installer_uses_required_name_dotnet_hash_and_fixed_icon(self):
        text = INSTALL.read_text(encoding="utf-8-sig")
        self.assertIn("[Parameter(Mandatory = $true)][string]$VmName", text)
        self.assertIn("Set-InstalledFile $ConfigPath", text)
        self.assertIn('System32\\vmconnect.exe,0', text)
        self.assertIn("[Security.Cryptography.SHA256]::Create()", text)
        self.assertNotIn("Get-FileHash", text)

    def test_dropped_paths_never_fill_the_named_plan_parameters(self):
        # A shortcut appends dropped paths without names (found in the 2026-11-26 live check).
        script = SCRIPT.read_text(encoding="utf-8-sig")
        self.assertLess(script.index("[CmdletBinding(PositionalBinding = $false)]"), script.index("param("))
        if os.name != "nt" or not POWERSHELL.exists():
            self.skipTest("Windows PowerShell only")
        start = script.index("param(")
        header = script[:script.index("\n)", start) + 2]
        body = header + "\n'PLAN=' + $PlanPath + '|PATHS=' + ($Paths -join ';')\n"
        dropped = [r"C:\a\vm-guest", r"D:\b c.txt"]
        with inherited_test_temp() as temp:
            probe = Path(temp) / "probe.ps1"
            probe.write_text(body, encoding="utf-8-sig")
            done = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                   "-File", str(probe), *dropped], capture_output=True, text=True, timeout=60)
            named = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                    "-File", str(probe), "-PlanPath", r"C:\p.json"], capture_output=True, text=True,
                                   timeout=60)
        self.assertIn("PLAN=|PATHS=" + ";".join(dropped), done.stdout, done.stderr)
        self.assertIn(r"PLAN=C:\p.json|PATHS=", named.stdout, named.stderr)

    def test_elevation_and_copy_use_the_checked_plan(self):
        script = SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("$bytes = Get-PlanBytes (New-SendPlan $Paths)", script)
        self.assertIn("'scn-send-' + [Guid]::NewGuid().ToString('N')", script)
        self.assertIn("[IO.FileStream]::new($path, 'CreateNew', 'Write', 'Read', 4096, 'DeleteOnClose')", script)
        self.assertIn("$planFile.Dispose()", script)
        self.assertIn("if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) }", script)
        self.assertIn("$plan = Read-SendPlan $PlanPath $PlanHash", script)
        self.assertIn("Invoke-Send ([string[]]$plan.Dropped) $PlanHash", script)
        self.assertIn("$info.Links -ne 1", script)
        self.assertIn("FILE_LIST_DIRECTORY", script)
        self.assertIn("GetFinalPathNameByHandle", script)
        send = script.split("function Send-Files(", 1)[1].split("function New-GuestFolder", 1)[0]
        self.assertLess(send.index("Open-SendPath"), send.index("$checked.Identity -cne $item.Identity"))
        self.assertLess(send.index("$checked.Identity -cne $item.Identity"), send.index("Copy-VMFile"))
        self.assertLess(send.index("Copy-VMFile"), send.index("Close-SendPath"))

    def test_manual_send_folder_uses_a_run_specific_identifier(self):
        script = SCRIPT.read_text(encoding="utf-8-sig")
        folder = script.split("function New-GuestFolder", 1)[1].split("function Invoke-Send", 1)[0]
        self.assertIn("yyyyMMdd-HHmmss", folder)
        self.assertIn("[Guid]::NewGuid()", folder)
        self.assertIn("$folder = New-GuestFolder", script)

    def test_bundle_manifest_check_pins_entire_drop_before_copy(self):
        script = SCRIPT.read_text(encoding="utf-8-sig")
        verify = script.split("function Open-VerifiedBundle(", 1)[1].split("function Get-PlanBytes", 1)[0]
        for guard in ("Open-SendPath $item.Source", "$manifest.vm_name -cne $VmName",
                      "$digests[$name] -cne $match.Groups[2].Value", "$digests.Count -ne 0"):
            self.assertIn(guard, verify)
        invoke = script.split("function Invoke-Send(", 1)[1].split("function Test-Admin", 1)[0]
        self.assertLess(invoke.index("$pins = Open-VerifiedBundle"), invoke.index("Send-Files"))


if __name__ == "__main__":
    unittest.main()
