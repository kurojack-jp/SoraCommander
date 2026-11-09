"""Phase9F offline candidate inventory negative tests.

Only isolated inherited-ACL NON-SECRET synthetic fixtures. No live deployment,
Windows ACL change, Task/Service, remote listener, OS lock or privileged operation.
"""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from tests._inherited_test_temp import inherited_test_temp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next.release_inventory import (
    CandidateIntegrityError, DEV_ONLY_MODULES, MANIFEST, MODULES,
    OPTIONAL_DEV_ONLY_MODULES, PACKAGE,
    make_candidate, verify_candidate,
)


class U2GOfflineInventoryTests(unittest.TestCase):
    """Read actual source only; no staging, temporary paths or child launch."""

    def test_receipt_boundary_is_dev_only_and_inventory_is_complete(self):
        name = "offline_b01_receipt_adjudication.py"
        self.assertIn(name, DEV_ONLY_MODULES)
        self.assertNotIn(name, MODULES)
        source = ROOT / "src" / PACKAGE
        # Worktree-only locale candidates may be absent from a clean checkout.
        self.assertEqual({p.name for p in source.glob("*.py")} - OPTIONAL_DEV_ONLY_MODULES,
                         set(MODULES) | (DEV_ONLY_MODULES - OPTIONAL_DEV_ONLY_MODULES))
        self.assertFalse(set(MODULES) & DEV_ONLY_MODULES)

    def test_production_source_has_no_receipt_boundary_import(self):
        target = "offline_b01_receipt_adjudication"
        for name in MODULES:
            tree = ast.parse((ROOT / "src" / PACKAGE / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported = [node.module or "", *(a.name for a in node.names)]
                else:
                    continue
                self.assertFalse(any(target in item.split(".") for item in imported), name)


class Phase9FCandidateTests(unittest.TestCase):
    def setUp(self):
        # Non-secret synthetic release fixtures only. Preserve test assertions.
        self.home = Path(self.enterContext(inherited_test_temp()))
        self.source = ROOT / "src" / PACKAGE
        self.candidate = self.home / "candidate"

    def stage(self):
        digest = make_candidate(self.source, self.candidate)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        return digest

    def test_deterministic_candidate_with_external_digest(self):
        digest = self.stage()
        self.assertTrue(verify_candidate(
            self.candidate, trusted_manifest_sha256=digest))
        other = self.home / "other"
        digest2 = make_candidate(self.source, other)
        self.assertEqual(digest, digest2)
        self.assertEqual((other / MANIFEST).read_bytes(),
                         (self.candidate / MANIFEST).read_bytes())

    def test_requires_exact_trusted_external_digest(self):
        digest = self.stage()
        for wrong in (None, "", "0" * 63, "0" * 64, "x" * 64,
                      digest.upper(), 1, b"0" * 64):
            with self.subTest(wrong=wrong), self.assertRaises(CandidateIntegrityError):
                verify_candidate(self.candidate, trusted_manifest_sha256=wrong)

    def test_modified_staged_module_is_detected(self):
        digest = self.stage()
        victim = self.candidate / PACKAGE / "owner_approval_dialog.py"
        victim.write_bytes(victim.read_bytes() + b"\n# tampered\n")
        with self.assertRaises(CandidateIntegrityError):
            verify_candidate(self.candidate, trusted_manifest_sha256=digest)

    def test_changed_manifest_even_with_same_modules_is_detected(self):
        digest = self.stage()
        target = self.candidate / MANIFEST
        target.write_bytes(target.read_bytes() + b"\n")
        with self.assertRaises(CandidateIntegrityError):
            verify_candidate(self.candidate, trusted_manifest_sha256=digest)

    def test_missing_or_extra_code_blocks_release(self):
        digest = self.stage()
        extra = self.candidate / PACKAGE / "evil.py"
        extra.write_text("print('not executed')\n", encoding="utf-8")
        with self.assertRaises(CandidateIntegrityError):
            verify_candidate(self.candidate, trusted_manifest_sha256=digest)
        extra.unlink()
        (self.candidate / PACKAGE / "windows_owner.py").unlink()
        with self.assertRaises(CandidateIntegrityError):
            verify_candidate(self.candidate, trusted_manifest_sha256=digest)

    def test_unexpected_root_artifact_blocks_release(self):
        digest = self.stage()
        (self.candidate / "untrusted.cfg").write_bytes(b"danger")
        with self.assertRaises(CandidateIntegrityError):
            verify_candidate(self.candidate, trusted_manifest_sha256=digest)

    def test_disallows_overwriting_existing_bundle(self):
        digest = self.stage()
        before = (self.candidate / MANIFEST).read_bytes()
        with self.assertRaises(FileExistsError):
            make_candidate(self.source, self.candidate)
        self.assertEqual((self.candidate / MANIFEST).read_bytes(), before)
        self.assertTrue(verify_candidate(
            self.candidate, trusted_manifest_sha256=digest))

    def test_source_new_python_file_is_fail_closed(self):
        alternative = self.home / "source"
        shutil.copytree(self.source, alternative, ignore=shutil.ignore_patterns("__pycache__"))
        (alternative / "unexpected.py").write_bytes(b"NEW")
        with self.assertRaises(CandidateIntegrityError):
            make_candidate(alternative, self.candidate)
        self.assertFalse(self.candidate.exists())

    def test_source_missing_module_is_fail_closed(self):
        alternative = self.home / "source"
        shutil.copytree(self.source, alternative, ignore=shutil.ignore_patterns("__pycache__"))
        (alternative / "owner_approval_broker.py").unlink()
        with self.assertRaises(CandidateIntegrityError):
            make_candidate(alternative, self.candidate)
        self.assertFalse(self.candidate.exists())

    def test_manifest_path_escape_rejected_even_if_hash_supplied(self):
        self.stage()
        path = self.candidate / MANIFEST
        content = json.loads(path.read_text("ascii"))
        content["files"][0]["path"] = "../outside.py"
        forged = (json.dumps(content, sort_keys=True, separators=(",", ":"))+"\n").encode("ascii")
        path.write_bytes(forged)
        forged_hash = hashlib.sha256(forged).hexdigest()
        # Even a freshly provided digest cannot authorize path traversal.
        with self.assertRaises(CandidateIntegrityError):
            verify_candidate(self.candidate, trusted_manifest_sha256=forged_hash)

    def test_manifest_duplicate_keys_rejected(self):
        self.stage()
        path=self.candidate / MANIFEST
        data=path.read_bytes()
        # Repeat "format" explicitly, supplying the computed forged digest.
        forged=data.replace(b'{"files":', b'{"format":"ignored","files":',1)
        path.write_bytes(forged)
        with self.assertRaises(CandidateIntegrityError):
            verify_candidate(self.candidate, trusted_manifest_sha256=hashlib.sha256(forged).hexdigest())

    def test_manifest_wrong_record_size_rejected(self):
        self.stage()
        path=self.candidate / MANIFEST
        content=json.loads(path.read_text("ascii"))
        content["files"][0]["bytes"]=-1
        forged=(json.dumps(content,sort_keys=True,separators=(",",":"))+"\n").encode("ascii")
        path.write_bytes(forged)
        with self.assertRaises(CandidateIntegrityError):
            verify_candidate(self.candidate, trusted_manifest_sha256=hashlib.sha256(forged).hexdigest())

    def test_optional_windows_symlink_is_rejected(self):
        digest = self.stage()
        target = self.candidate / PACKAGE / "owner_approval_broker.py"
        copy = self.home / "original.py"
        copy.write_bytes(target.read_bytes())
        target.unlink()
        try:
            target.symlink_to(copy)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"unprivileged Windows symlink not available: {type(exc).__name__}")
        try:
            with self.assertRaises(CandidateIntegrityError):
                verify_candidate(self.candidate, trusted_manifest_sha256=digest)
        finally:
            # Remove only the symlink this test created, while its fixture is
            # still open. The inherited fixture deliberately refuses links.
            if target.is_symlink():
                target.unlink()

    def test_staged_host_import_smoke_is_locally_deny_only(self):
        digest=self.stage()
        script = ("import sys;sys.path.insert(0,sys.argv[1]);"
                  "from sora_commander_next.host import Host;"
                  "s=Host().status();"
                  "assert s['remote_ready'] is False;"
                  "assert s['approval']=='LOCAL_DEMO_ONLY';"
                  "print('STAGED_HOST_IMPORT_DENY_ONLY_PASS')")
        proc=subprocess.run(
            [sys.executable,"-I","-S","-B","-c",script,str(self.candidate)],
            cwd=str(self.home),capture_output=True,text=True,timeout=10)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertIn("STAGED_HOST_IMPORT_DENY_ONLY_PASS",proc.stdout)
        self.assertTrue(verify_candidate(
            self.candidate, trusted_manifest_sha256=digest))

    def test_only_explicit_source_modules_are_staged(self):
        digest=self.stage()
        staged=set(p.name for p in (self.candidate / PACKAGE).iterdir())
        self.assertEqual(staged,set(MODULES))
        self.assertNotIn("__pycache__",staged)
        self.assertTrue(verify_candidate(
            self.candidate, trusted_manifest_sha256=digest))


if __name__ == "__main__":
    unittest.main()
