"""Static-only fixtures; no scanned code, IPC, Owner UI, installs or ACL edits."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next import release_broker_import_audit as audit
from sora_commander_next.release_inventory import DEV_ONLY_MODULES, MODULES, PACKAGE

SOURCE = ROOT / "src" / PACKAGE


class BrokerActualSourceAuditTests(unittest.TestCase):
    def test_actual_source_positive_closure_and_data_only_evidence(self):
        result = audit.audit_broker_import_closure(SOURCE)
        self.assertEqual(result.roots, audit.ROOTS)
        self.assertEqual(result.reachable_modules, tuple(sorted(set(result.reachable_modules))))
        self.assertEqual(result.count, len(result.reachable_modules))
        self.assertTrue({PACKAGE, *(PACKAGE + "." + n[:-3] for n in audit.ROOTS),
                         PACKAGE + ".owner_stage_acl", PACKAGE + ".offline_result_archive"}
                        <= set(result.reachable_modules))
        for name in result.reachable_modules:
            filename = "__init__.py" if name == PACKAGE else name.split(".")[-1] + ".py"
            self.assertIn(filename, MODULES)
            self.assertNotIn(filename, DEV_ONLY_MODULES)
        for flag in ("authorization", "deployment_authorized", "authenticated"):
            self.assertIs(getattr(result, flag), False)
            with self.assertRaises(FrozenInstanceError):
                setattr(result, flag, True)
        self.assertIsInstance(result.roots, tuple)
        self.assertIsInstance(result.reachable_modules, tuple)
        self.assertEqual(result.status, "HOLD_LIVE")


class BrokerImportAuditTests(unittest.TestCase):
    def setUp(self):
        # Keep disposable fixtures under the ignored scratch area, never in
        # the project root where interrupted Windows cleanups left empty dirs.
        scratch = ROOT / "temp"
        if scratch.is_symlink():
            raise RuntimeError("linked test scratch directory")
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="next-import-audit-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.source = self.home / PACKAGE
        shutil.copytree(SOURCE, self.source, ignore=shutil.ignore_patterns("__pycache__"))
        for name in (*audit.ROOTS, "__init__.py", "commands.py", "policy.py"):
            self.write(name, "# synthetic, never executed\n")

    def write(self, name, source):
        (self.source / name).write_text(source, encoding="utf-8")

    def check(self):
        return audit.audit_broker_import_closure(self.source)

    def deny(self, text=None):
        with self.assertRaises(audit.BrokerImportAuditError) as caught:
            self.check()
        if text:
            self.assertIn(text, str(caught.exception))

    def test_synthetic_nested_transitive_cycle_absolute_and_relative(self):
        self.write(audit.ROOTS[0], "def never():\n from . import commands as c\n")
        self.write("commands.py", "if False:\n import sora_commander_next.policy as p\n")
        self.write("policy.py", "from .commands import Command\n")
        result = self.check()
        expected = {PACKAGE, *(PACKAGE + "." + n[:-3] for n in audit.ROOTS),
                    PACKAGE + ".commands", PACKAGE + ".policy"}
        self.assertEqual(result.reachable_modules, tuple(sorted(expected)))

    def test_scanned_source_never_executes_or_writes(self):
        marker = self.home / "MUST_NOT_EXIST"
        self.write(audit.ROOTS[0],
                   f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
                   "raise RuntimeError('scanned only')\n")
        self.check()
        self.assertFalse(marker.exists())

    def test_transitive_dev_only_injection_all_import_forms(self):
        self.write(audit.ROOTS[0], "from .commands import Command\n")
        for code in (
            "from .approval_v2_ledger import Ledger",
            "import sora_commander_next.approval_v2_ledger as ledger",
            "from sora_commander_next import approval_v2_ledger",
        ):
            with self.subTest(code=code):
                self.write("commands.py", "def dormant():\n if False:\n  " + code + "\n")
                self.deny("DEV_ONLY")

    def test_unlisted_local_present_or_missing_module_denied(self):
        for present in (False, True):
            if present:
                self.write("unreviewed.py", "raise RuntimeError('never run')\n")
            for code in ("from . import unreviewed", "from .unreviewed import x",
                         "import sora_commander_next.unreviewed"):
                with self.subTest(present=present, code=code):
                    self.write(audit.ROOTS[0], code + "\n")
                    self.deny("unclassified")

    def test_missing_reviewed_transitive_module_denied(self):
        self.write(audit.ROOTS[0], "from .commands import Command\n")
        (self.source / "commands.py").unlink()
        self.deny("missing")

    def test_missing_root_or_initializer_denied(self):
        for name in (*audit.ROOTS, "__init__.py"):
            with self.subTest(name=name):
                (self.source / name).unlink()
                self.deny("missing")
                self.write(name, "# restored synthetic fixture\n")

    def test_initializer_imports_are_traversed(self):
        self.write("__init__.py", "from . import approval_v2_ledger\n")
        self.deny("DEV_ONLY")

    def test_syntax_and_invalid_utf8_are_denied(self):
        for data in (b"def broken(:\n", b"\xff\n", b"\x00"):
            with self.subTest(data=data):
                (self.source / audit.ROOTS[0]).write_bytes(data)
                self.deny("invalid UTF8/Python")

    def test_dynamic_imports_aliases_eval_exec_all_conservatively_denied(self):
        for code in (
            "import importlib\nimportlib.import_module(target)",
            "import importlib as il\nil.import_module('sora_commander_next.commands')",
            "from importlib import import_module as load\nload(target)",
            "__import__(target)", "loader = __import__\nloader(target)",
            "eval(target)", "exec('import sora_commander_next.approval_v2_ledger')",
            "import builtins as b\ngetattr(b, '__import__')(target)",
            "__builtins__['__import__'](target)",
            "import importlib as il\ngetattr(il, 'import_' + 'module')(target)",
        ):
            with self.subTest(code=code):
                self.write("commands.py", code + "\n")
                self.write(audit.ROOTS[0], "from . import commands\n")
                self.deny("dynamic")

    def test_relative_escape_unclassified_subpackage_and_star_denied(self):
        for code in ("from ..commands import Command", "from ... import x",
                     "from .unknown.sub import x", "from . import *",
                     "from sora_commander_next.unknown.sub import x"):
            with self.subTest(code=code):
                self.write(audit.ROOTS[0], code + "\n")
                self.deny()

    def test_absolute_stdlib_not_mistaken_for_local_modules(self):
        self.write(audit.ROOTS[0], "import json, os.path\nfrom pathlib import Path\n"
                   "from ctypes import wintypes\nfrom win32security import GetTokenInformation\n")
        self.assertEqual(self.check().count, len(audit.ROOTS) + 1)

    def test_unknown_absolute_project_and_bare_local_import_denied(self):
        for code in ("import unreviewed_project", "import commands",
                     "from approval_v2_ledger import Ledger"):
            with self.subTest(code=code):
                self.write(audit.ROOTS[0], code + "\n")
                self.deny("unclassified absolute")

    def test_malformed_unknown_and_missing_roots_denied(self):
        for root in (None, 1, b"bad", "", PACKAGE, ".", self.home,
                     self.home / "missing" / PACKAGE, self.source / "commands.py",
                     str(self.source / ".." / PACKAGE), str(self.source) + " ",
                     "C:\\Program Files\\" + PACKAGE,
                     "\\\\server\\share\\" + PACKAGE,
                     "\\\\?\\C:\\source\\" + PACKAGE,
                     str(self.source) + ":stream", "bad\x00root"):
            with self.subTest(root=root), self.assertRaises(audit.BrokerImportAuditError):
                audit.audit_broker_import_closure(root)

    def test_reviewed_classification_and_root_corruption_denied(self):
        for names in (tuple(n for n in MODULES if n != audit.ROOTS[0]),
                      (*MODULES, next(iter(DEV_ONLY_MODULES))),
                      (*MODULES, "../escape.py"), (*MODULES, MODULES[0])):
            with self.subTest(names=names), patch.object(audit, "MODULES", names):
                self.deny("classification")

    def test_file_count_file_bytes_total_bytes_and_ast_bounds(self):
        for constant, value in (("MAX_FILES", 1), ("MAX_FILE_BYTES", 1),
                                ("MAX_TOTAL_BYTES", 1), ("MAX_AST_NODES", 1)):
            with self.subTest(constant=constant), patch.object(audit, constant, value):
                self.write(audit.ROOTS[0], "import os\n")
                self.deny("bound")

    def test_oversize_source_is_not_opened(self):
        self.write("__init__.py", "#" * (audit.MAX_FILE_BYTES + 1))
        with patch.object(Path, "open", side_effect=AssertionError("must stat before read")):
            self.deny("bound")

    def test_actual_ordinary_file_replaced_by_directory_denied(self):
        target = self.source / audit.ROOTS[0]
        target.unlink()
        target.mkdir()
        self.deny("ordinary")

    def test_same_named_subpackage_cannot_shadow_reviewed_module(self):
        shadow = self.source / Path(audit.ROOTS[0]).stem
        shadow.mkdir()
        (shadow / "__init__.py").write_text("# unreviewed\n", encoding="utf-8")
        self.deny("shadow")

    def test_reparse_bit_guard_on_file_root_and_ancestor_no_acl_changes(self):
        original = Path.lstat
        for target in (self.source / audit.ROOTS[0], self.source, self.home, self.home.parent):
            def fake(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == target:
                    return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size,
                                           st_file_attributes=0x400)
                return info
            with self.subTest(target=target), patch.object(Path, "lstat", fake):
                self.deny("reparse")

    def test_symlink_mode_guard_independent_of_windows_privilege(self):
        original = Path.lstat
        def fake(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.source:
                return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
            return info
        with patch.object(Path, "lstat", fake):
            self.deny("reparse")

    def test_actual_symlink_if_unprivileged_creation_available(self):
        alias = self.home / "alias"
        alias.mkdir()
        linked = alias / PACKAGE
        try:
            linked.symlink_to(self.source, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("unprivileged symlink unavailable: " + type(exc).__name__)
        with self.assertRaisesRegex(audit.BrokerImportAuditError, "reparse"):
            audit.audit_broker_import_closure(linked)
        ancestor = self.home / "linked-parent"
        ancestor.symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(audit.BrokerImportAuditError, "reparse"):
            audit.audit_broker_import_closure(ancestor / PACKAGE)

    def test_read_failure_denied(self):
        with patch.object(Path, "open", side_effect=PermissionError("synthetic denial")):
            self.deny("source read failed")


if __name__ == "__main__":
    unittest.main()
