"""Stdlib-only adapter boundary checks; these are not real SDK integration."""

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.mcp_adapter import _validate_arguments, serve_stdio


class Phase3BoundaryTests(unittest.TestCase):
    def test_wire_types_cannot_be_coerced_or_extra_fields_ignored(self):
        valid = {"executable": sys.executable, "argv": ["--version"], "cwd": str(ROOT)}
        _validate_arguments("run_command", valid)
        for changes in ({"timeout": True}, {"timeout": "1"}, {"argv": "--version"},
                        {"output_limit": 128.0}, {"background": 1}, {"env": {}},
                        {"argv": [1]}, {"cwd": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _validate_arguments("run_command", valid | changes)
        for name, arguments in (("ping", {"extra": 1}), ("stop_process", {}),
                                ("read_process_output", {"session_id": "x", "offset": True})):
            with self.subTest(name=name), self.assertRaises(ValueError):
                _validate_arguments(name, arguments)

    def test_stdio_always_closes_owned_jobs_even_on_transport_failure(self):
        from sora_commander_next.host import Host
        host = Host(jobs=Mock())
        host._dev_interactive_jobs = Mock()
        server = Mock()
        server.run.side_effect = RuntimeError("test transport failure")
        with patch("sora_commander_next.mcp_adapter.build_server", return_value=server):
            with self.assertRaises(RuntimeError):
                serve_stdio(host)
        server.run.assert_called_once_with(transport="stdio")
        host.jobs.close.assert_called_once_with()
        host._dev_interactive_jobs.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
