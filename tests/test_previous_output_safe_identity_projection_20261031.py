"""Owner archive response must not echo rejected command identities.

Uses a synthetic JobManager-shaped object, not the real archive, filesystem,
Owner dialog, MCP tunnel, command spawn, approval, or session replay.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from sora_commander_next.host import Host


class SyntheticResultManager:
    output_archive_path = "synthetic-owner-archive-enabled"
    def __init__(self, result):
        self.result = result
        self.calls = []
    def previous_output(self, session_id=None):
        self.calls.append(session_id)
        return self.result


class PreviousOutputCommandProjectionTests(unittest.TestCase):
    def owner_host(self, record):
        jobs = SyntheticResultManager(record)
        host = Host(jobs=jobs)
        verify = Mock(return_value=True)
        host._owner_review_dispatcher = SimpleNamespace(
            _controller=SimpleNamespace(_verify=verify))
        return host, jobs, verify

    def test_unrecognized_identity_is_not_echoed_in_single_output(self):
        marker = "FAKE_PRIVATE_ARG_DO_NOT_ECHO"
        record = {
            "session_id": "a" * 48,
            "request_id": "b" * 32,
            "status": "COMPLETED",
            "safe_command": {
                "executable": sys.executable,
                "argv": ["-c", "print('" + marker + "')"],
                "cwd": str(Path.cwd()),
            },
            "command_line": marker,
            "chunks": [],
        }
        host, jobs, verify = self.owner_host(record)
        output = host.previous_output("a" * 48)
        self.assertEqual(output["status"], "COMPLETED")
        self.assertFalse(output["command_identified"])
        self.assertNotIn("safe_command", output)
        self.assertNotIn("command_line", output)
        self.assertNotIn(marker, str(output))
        self.assertEqual(output["stdout"], "")
        self.assertEqual(output["stderr"], "")
        self.assertEqual(jobs.calls, ["a" * 48])
        verify.assert_called_once_with()
        # Do not mutate or "repair" the underlying stored observation.
        self.assertIn(marker, str(record))

    def test_unrecognized_identity_is_not_echoed_in_index(self):
        marker = "FAKE_FORGED_COMMAND"
        row = {
            "session_id": "c" * 48,
            "status": "RUNNING",
            "safe_command": "not-an-identity",
            "command_line": marker,
        }
        host, jobs, verify = self.owner_host({"sessions": [row], "count": 1})
        result = host.previous_output()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["sessions"][0]["status"], "RUNNING")
        self.assertFalse(result["sessions"][0]["command_identified"])
        self.assertNotIn("safe_command", result["sessions"][0])
        self.assertNotIn("command_line", result["sessions"][0])
        self.assertNotIn(marker, str(result))
        self.assertEqual(jobs.calls, [None])
        verify.assert_called_once_with()

    def test_valid_identity_still_returns_exact_approved_command(self):
        row = {
            "session_id": "d" * 48,
            "status": "COMPLETED",
            "safe_command": {
                "executable": sys.executable,
                "argv": ["--version"],
                "cwd": str(Path.cwd()),
            },
            "command_line": "INVALID_UNTRUSTED_SOURCE_FIELD",
        }
        host, jobs, verify = self.owner_host({"sessions": [row], "count": 1})
        result = host.previous_output()
        item = result["sessions"][0]
        self.assertTrue(item["command_identified"])
        self.assertEqual(item["safe_command"], row["safe_command"])
        self.assertIn("--version", item["command_line"])
        self.assertNotEqual(item["command_line"], row["command_line"])
        self.assertEqual(jobs.calls, [None])
        verify.assert_called_once_with()

    def test_absent_identity_does_not_add_command_fields(self):
        host, jobs, verify = self.owner_host({
            "session_id": "e" * 48, "status": "UNKNOWN"
        })
        result = host.previous_output("e" * 48)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(result["command_identified"])
        self.assertNotIn("safe_command", result)
        self.assertNotIn("command_line", result)
        self.assertEqual(jobs.calls, ["e" * 48])
        verify.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
