"""DEV_ONLY lifecycle acceptance: actual Windows Owner and kernel mutex.

Real-process acceptance never replaces the Owner guard or mutex. Separate
fault-injection tests cover CLI validation and ambiguous cleanup only.
"""
import ast
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).absolute().parents[1]
SOURCE = ROOT / "src" / "sora_commander_next"
sys.path.insert(0, str(ROOT / "src"))
from sora_commander_next import offline_broker_owner_process_probe as probe
from sora_commander_next import release_inventory as inventory

# Only this fixed module is executed, with an isolated interpreter, fixed source
# path and cwd. No environment-supplied executable, import path or command.
_BOOTSTRAP = (
    "import runpy, sys; "
    f"sys.path.insert(0, {str(ROOT / 'src')!r}); "
    "runpy.run_module('sora_commander_next.offline_broker_owner_process_probe', "
    "run_name='__main__')")
_BASE = (sys.executable, "-I", "-S", "-B", "-c", _BOOTSTRAP, "--probe")
_READY = "OWNER_BROKER_PROBE_READY\n"
_CLOSED = "OWNER_BROKER_PROBE_CLOSED\n"
_DENIED = "OWNER_BROKER_PROBE_DENIED\n"


class ProbeBoundaryTests(unittest.TestCase):
    def test_inventory_is_dev_only_and_excluded_from_candidate_payload(self):
        name = "offline_broker_owner_process_probe.py"
        self.assertIn(name, inventory.DEV_ONLY_MODULES)
        self.assertNotIn(name, inventory.MODULES)
        self.assertNotIn(name, inventory._package_sources(SOURCE))

    def test_hold_parser_bounds(self):
        for value in ("0", "1", "4999", "5000"):
            self.assertEqual(probe._hold_ms(value), int(value))

    def test_probe_has_only_fixed_lifecycle_and_status_calls(self):
        tree = ast.parse(Path(probe.__file__).read_text(encoding="utf-8"))
        imports = set()
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(item.name for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
                else:
                    self.assertIsInstance(node.func, ast.Name)
                    calls.add(node.func.id)
        self.assertEqual(imports, {
            "argparse", "pathlib", "sys", "time", "broker_owner_process"})
        self.assertLessEqual(calls, {
            "len", "isascii", "isdecimal", "int", "ArgumentTypeError",
            "ArgumentParser", "add_argument", "parse_args", "BrokerOwnerProcess",
            "str", "Path", "query_status_local", "RuntimeError", "print",
            "monotonic", "sleep", "min", "SystemExit", "main"})
        construction = next(node for node in ast.walk(tree)
                            if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name)
                            and node.func.id == "BrokerOwnerProcess")
        self.assertEqual(ast.unparse(construction),
                         "BrokerOwnerProcess(sys.executable, str(Path(__file__).parent))")


@unittest.skipUnless(os.name == "nt", "actual Windows required")
class WindowsOwnerProbeTests(unittest.TestCase):
    def test_import_is_inert(self):
        from sora_commander_next import broker_owner_process as core
        with patch.object(core, "BrokerOwnerProcess") as factory, \
                patch.object(subprocess, "Popen") as spawn:
            spec = importlib.util.spec_from_file_location(
                "sora_commander_next._probe_import_test", probe.__file__)
            module = importlib.util.module_from_spec(spec)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                spec.loader.exec_module(module)
            self.assertEqual(output.getvalue(), "")
            factory.assert_not_called()
            spawn.assert_not_called()

    def test_invalid_cli_fails_before_owner_construction(self):
        from sora_commander_next import broker_owner_process as core
        invalid = ([], ["--hold-ms", "0"], ["--pro"], ["--probe", "extra"],
                   ["--probe", "--command", "ignored"],
                   ["--probe", "--cwd", "ignored"],
                   ["--probe", "--executable", "ignored"],
                   ["--probe", "--hold-ms"])
        invalid += tuple(["--probe", "--hold-ms", value] for value in (
            "-1", "5001", "99999", "1.0", "NaN", "inf", "true", "", " 1",
            "+1", "1e2", "\uFF11", "\u0661", "1\n"))
        with patch.object(core, "BrokerOwnerProcess") as factory:
            for argv in invalid:
                with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as result:
                    probe.main(argv)
                self.assertEqual(result.exception.code, 2)
            factory.assert_not_called()

    def test_injected_cleanup_failure_never_claims_closed_or_retries(self):
        from sora_commander_next import broker_owner_process as core
        context = Mock()
        context.__enter__ = Mock(return_value=context)
        context.__exit__ = Mock(side_effect=OSError("private cleanup details"))
        context.query_status_local.return_value = {
            "request_id": probe._REQUEST_ID, "status": "UNKNOWN"}
        with patch.object(core, "BrokerOwnerProcess", return_value=context) as factory, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(probe.main(["--probe"]), 1)
        self.assertEqual(output.getvalue(), _READY + _DENIED)
        factory.assert_called_once_with(sys.executable, str(SOURCE))
        context.__exit__.assert_called_once()

    def test_injected_entry_denial_has_only_constant_output_and_one_attempt(self):
        from sora_commander_next import broker_owner_process as core
        with patch.object(core, "BrokerOwnerProcess", side_effect=PermissionError(
                "private denial details")) as factory, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(probe.main(["--probe"]), 1)
        self.assertEqual(output.getvalue(), _DENIED)
        factory.assert_called_once()

    def _cleanup_child(self, child):
        try:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
        finally:
            child.stdout.close()
            child.stderr.close()

    def test_real_independent_process_denial_close_then_reacquire(self):
        # No Owner/mutex/runtime mocks: all three are real independent OS children.
        first = subprocess.Popen(
            [*_BASE, "--hold-ms", "5000"], cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            close_fds=True, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(self._cleanup_child, first)
        lines = queue.Queue()

        def collect():
            for line in first.stdout:
                lines.put(line)
            lines.put(None)

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        self.assertEqual(lines.get(timeout=5), _READY)
        self.assertIsNone(first.poll())
        self.assertNotEqual(first.pid, os.getpid())
        second = subprocess.run(
            _BASE, cwd=str(ROOT), capture_output=True, text=True,
            timeout=3, close_fds=True, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(second.stdout, _DENIED)
        self.assertEqual(second.stderr, "")
        self.assertIsNone(first.poll())
        self.assertEqual(lines.get(timeout=7), _CLOSED)
        self.assertIsNone(lines.get(timeout=3))
        self.assertEqual(first.wait(timeout=3), 0)
        reader.join(timeout=1)
        self.assertFalse(reader.is_alive())
        self.assertEqual(first.stderr.read(), "")
        third = subprocess.run(
            _BASE, cwd=str(ROOT), capture_output=True, text=True,
            timeout=5, close_fds=True, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(third.returncode, 0)
        self.assertEqual(third.stdout, _READY + _CLOSED)
        self.assertEqual(third.stderr, "")


if __name__ == "__main__":
    unittest.main()
