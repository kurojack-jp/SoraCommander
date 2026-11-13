"""Offline tests: only interpreter children, cwd always this repository."""

from dataclasses import replace
from pathlib import Path
import sys
import time
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.approval import LocalApproval
from sora_commander_next.commands import Command
from sora_commander_next.extensions import Registry
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server
from sora_commander_next.owner_ui import OwnerController
from sora_commander_next.policy import Decision, Grant, Pattern, Policy
from sora_commander_next.worker import execute


def command(argv=("--version",), **changes):
    return Command(sys.executable, argv, str(ROOT), **changes)


def payload(request):
    return {name: getattr(request, name) for name in request.__dataclass_fields__}


class ScaffoldTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.clock = lambda: self.now
        self.pattern = Pattern(sys.executable, ("--version",))
        self.policy = Policy((str(ROOT),), (self.pattern,), self.clock)

    def grant(self, argv=("--version",), owner="owner", root=None, expiry=110.0):
        return Grant("grant", owner, str(root or ROOT), (Pattern(sys.executable, argv),), expiry)

    def test_normalization_immutable_argv_and_unique_ids(self):
        args = ["--version"]
        request = command(args)
        args.append("changed")
        self.assertEqual(request.argv, ("--version",))
        self.assertTrue(Path(request.executable).is_absolute())
        self.assertNotEqual(request.request_id, command().request_id)

    def test_malformed_input_and_limits_fail_closed(self):
        cases = ({"argv": "--version"}, {"argv": [1]}, {"argv": ["a\0b"]},
                 {"argv": ["x"] * 129}, {"argv": ["x" * 8193]},
                 {"executable": "python"}, {"cwd": "."}, {"cwd": None},
                 {"timeout": 0}, {"timeout": 31}, {"timeout": float("nan")},
                 {"timeout": float("inf")}, {"timeout": True}, {"output_limit": 0},
                 {"output_limit": 65537}, {"output_limit": True}, {"request_id": ""},
                 {"env": {"INJECT": "bad"}})
        worker = Mock()
        host = Host(self.policy, worker=worker)
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(host.run_command(**(payload(command()) | changes))["status"], "ERROR")
        worker.assert_not_called()
        self.assertEqual(host.ping()["status"], "OK")

    def test_auto_exact_vetted_version_only(self):
        self.assertEqual(self.policy.classify(command(), "owner"), Decision.AUTO)
        self.assertEqual(self.policy.classify(command(("--version", "extra")), "owner"), Decision.REVIEW)
        self.assertEqual(Policy((str(ROOT),)).classify(command(), "owner"), Decision.REVIEW)

    def test_dynamic_network_destructive_and_shell_never_auto_even_if_listed(self):
        examples = (("pwsh.exe", ("-Command", "Get-Item .")), ("cmd.exe", ("/c", "echo hi")),
                    ("python.exe", ("-c", "print(1)")), ("curl.exe", ("--version",)),
                    ("tool.exe", ("read; delete",)), ("tool.exe", ("NAME=value",)),
                    ("script.ps1", ("--version",)), ("tool.exe", ("script.py",)))
        for executable, args in examples:
            with self.subTest(executable=executable, argv=args):
                request = Command(str(ROOT / executable), args, str(ROOT))
                policy = Policy((str(ROOT),), (Pattern(request.executable, args),))
                self.assertEqual(policy.classify(request, "owner"), Decision.REVIEW)
        # Listing these cannot allow them: git_push_guard refuses them first.
        for args in (("push",), ("reset", "--hard")):
            with self.subTest(argv=args):
                request = Command(str(ROOT / "git.exe"), args, str(ROOT))
                policy = Policy((str(ROOT),), (Pattern(request.executable, args),))
                self.assertEqual(policy.classify(request, "owner"), Decision.DENY)

    def test_default_and_sibling_scope_denied_without_filesystem_access(self):
        self.assertEqual(Policy().classify(command(), "owner"), Decision.DENY)
        # Both paths stay inside the repository; lexical prefix is insufficient.
        policy = Policy((str(ROOT / "src"),), (self.pattern,))
        request = replace(command(), cwd=str(ROOT / "src-other"))
        self.assertEqual(policy.classify(request, "owner"), Decision.DENY)
        with self.assertRaises(ValueError):
            Policy((str(ROOT.anchor),))

    def test_scoped_owner_exact_root_expiry_and_revocation(self):
        policy = Policy((str(ROOT),), clock=self.clock)
        policy.add_grant_local(self.grant(root=ROOT / "src"))
        request = replace(command(), cwd=str(ROOT / "src"))
        self.assertEqual(policy.classify(request, "owner"), Decision.SCOPED)
        self.assertEqual(policy.classify(request, "other-owner"), Decision.REVIEW)
        self.assertEqual(policy.classify(command(), "owner"), Decision.REVIEW)
        self.assertEqual(policy.classify(replace(request, argv=("-V",)), "owner"), Decision.REVIEW)
        self.now = 110.0
        self.assertEqual(policy.classify(request, "owner"), Decision.REVIEW)
        self.now = 100.0
        with self.assertRaises(PermissionError):
            policy.revoke_local("grant", "other-owner")
        policy.revoke_local("grant", "owner")
        self.assertEqual(policy.classify(request, "owner"), Decision.REVIEW)

    def test_invalid_grants_rejected(self):
        for grant in (self.grant(expiry=100), self.grant(expiry=float("inf")),
                      replace(self.grant(), patterns=()), replace(self.grant(), owner="")):
            with self.subTest(grant=grant), self.assertRaises(ValueError):
                self.policy.add_grant_local(grant)
        narrow = Policy((str(ROOT / "src"),), clock=self.clock)
        with self.assertRaises(ValueError):
            narrow.add_grant_local(self.grant())

    def test_deny_cannot_be_granted_or_approved(self):
        request = Command(str(ROOT / "runas.exe"), ("--version",), str(ROOT))
        self.policy.add_grant_local(Grant("deny", "owner", str(ROOT),
                                         (Pattern(request.executable, request.argv),), 110))
        worker = Mock()
        approval = LocalApproval(self.clock)
        host = Host(self.policy, "owner", approval, worker)
        self.assertEqual(host.run_command(**payload(request))["status"], "DENY")
        self.assertFalse(approval._pending)
        self.assertEqual(OwnerController(host).approve_local("deny")["status"], "ERROR")
        worker.assert_not_called()
        self.assertEqual(host.ping()["status"], "OK")

    def test_review_pending_bound_to_every_field_context_and_expiry(self):
        approval = LocalApproval(self.clock)
        worker = Mock()
        host = Host(self.policy, "owner", approval, worker)
        request = command(("unknown",))
        result = host.run_command(**payload(request))
        self.assertEqual(result["status"], "REVIEW")
        context = host._context()
        pending_id = result["pending_id"]
        self.assertTrue(approval.matches(pending_id, request, context))
        changes = ({"executable": str(ROOT / "other.exe")}, {"argv": ("changed",)},
                   {"cwd": str(ROOT / "src")}, {"timeout": 2}, {"output_limit": 128},
                   {"request_id": "substituted"})
        for change in changes:
            self.assertFalse(approval.matches(pending_id, replace(request, **change), context))
        self.assertFalse(approval.matches(pending_id, request, ("other-owner", *context[1:])))
        self.assertFalse(approval.matches(pending_id, request, ("owner", (str(ROOT / "src"),), 0)))
        self.policy.add_grant_local(self.grant())
        self.assertFalse(approval.matches(pending_id, request, ("owner", self.policy.roots, self.policy.generation)))
        self.now = 160.0
        self.assertFalse(approval.matches(pending_id, request, context))
        self.assertEqual(OwnerController(host).approve_local(pending_id)["status"], "ERROR")
        worker.assert_not_called()

    def test_pending_review_capacity_and_health(self):
        approval = LocalApproval(self.clock)
        host = Host(self.policy, approval=approval)
        for _ in range(128):
            self.assertEqual(host.run_command(**payload(command(("unknown",))))["status"], "REVIEW")
        self.assertEqual(host.run_command(**payload(command(("unknown",))))["status"], "ERROR")
        self.assertEqual(host.ping()["status"], "OK")
        self.now = 161
        self.assertEqual(host.run_command(**payload(command(("unknown",))))["status"], "REVIEW")

    def test_real_host_auto_success(self):
        result = Host(self.policy).run_command(**payload(command()))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["decision"], "AUTO")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("Python 3.13", result["stdout"] + result["stderr"])

    def test_real_scoped_inline_pattern_only(self):
        args = ("-I", "-S", "-c", "print('scoped')")
        self.policy.add_grant_local(self.grant(args))
        result = Host(self.policy, "owner").run_command(**payload(command(args)))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["decision"], "SCOPED")
        self.assertEqual(result["stdout"].strip(), "scoped")

    def test_worker_output_and_nonzero_failure(self):
        result = execute(command(("-I", "-S", "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)")))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["stdout"].strip(), "out")
        self.assertEqual(result["stderr"].strip(), "err")

    def test_worker_timeout_returns_bounded_and_child_exited(self):
        args = ("-I", "-S", "-c", "import time; time.sleep(5)")
        self.policy.add_grant_local(self.grant(args))
        host = Host(self.policy, "owner")
        started = time.monotonic()
        result = host.run_command(**payload(command(args, timeout=0.1)))
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertIsNotNone(result["exit_code"])
        self.assertLess(time.monotonic() - started, 2)
        for _ in range(100):
            self.assertEqual(host.ping()["status"], "OK")
        self.assertEqual(host.run_command(**payload(command()))["status"], "COMPLETED")

    def test_worker_combined_output_budget(self):
        result = execute(command(("-I", "-S", "-c", "import os; os.write(1,b'a'*100000); os.write(2,b'b'*100000)"), output_limit=128))
        self.assertEqual(result["status"], "OUTPUT_LIMIT")
        self.assertEqual(result["captured_bytes"], 128)
        self.assertLessEqual(len(result["stdout"]) + len(result["stderr"]), 128)

    def test_host_error_no_retry_and_next_command_healthy(self):
        worker = Mock(side_effect=RuntimeError("private message"))
        host = Host(self.policy, worker=worker)
        result = host.run_command(**payload(command()))
        self.assertEqual(result["status"], "DENY_UNPROTECTED_WORKER")
        self.assertNotIn("private", str(result))
        worker.assert_not_called()
        self.assertEqual(host.ping()["status"], "OK")
        host.worker = execute
        self.assertEqual(host.run_command(**payload(command()))["status"], "COMPLETED")

    def test_missing_executable_error_does_not_break_host(self):
        request = Command(str(ROOT / "missing-scaffold-test.exe"), ("--version",), str(ROOT))
        policy = Policy((str(ROOT),), (Pattern(request.executable, request.argv),))
        host = Host(policy)
        self.addCleanup(host.jobs.close)
        result = host.run_command(**payload(request))
        self.assertEqual(result, {
            "request_id": request.request_id, "status": "OUTCOME_UNKNOWN",
            "error": "LaunchOutcomeUnknown", "launch_phase": "spawning",
        })
        self.assertFalse(host.jobs._jobs)
        self.assertEqual(host.ping()["status"], "OK")
        policy.add_grant_local(Grant(
            "healthy-follow-up", host.owner, str(ROOT),
            (Pattern(sys.executable, ("--version",)),), time.monotonic() + 60))
        follow_up = host.run_command(**payload(command()))
        self.assertEqual(follow_up["status"], "COMPLETED", follow_up)
        self.assertEqual(follow_up["exit_code"], 0)

    def test_extension_import_runtime_and_response_failure_isolation(self):
        registry = Registry()
        with patch("sora_commander_next.extensions.import_module", side_effect=ImportError("broken")):
            self.assertEqual(registry.load_local("broken", "local.plugin")["status"], "ERROR")
        with patch("sora_commander_next.extensions.import_module", side_effect=RuntimeError("broken import")):
            self.assertEqual(registry.load_local("broken", "local.plugin")["status"], "ERROR")
        registry.register("broken", Mock(invoke=Mock(side_effect=RuntimeError("broken call"))))
        registry.register("bad", Mock(invoke=Mock(return_value=[])))
        registry.register("good", Mock(invoke=Mock(return_value={"status": "OK"})))
        for name in ("broken", "bad", "missing"):
            self.assertEqual(registry.invoke(name, "read", {})["status"], "ERROR")
        self.assertEqual(registry.invoke("good", "read", {})["status"], "OK")
        self.assertEqual(Host().ping()["status"], "OK")

    def test_session_tools_unknown_and_local_only_status(self):
        host = Host()
        self.assertEqual(host.read_process_output("anything")["status"], "ERROR")
        self.assertEqual(host.stop_process("anything")["status"], "ERROR")
        self.assertFalse(host.status()["remote_ready"])

    def test_core_without_site_packages_or_mcp(self):
        source = str(ROOT / "src")
        code = f"import sys; sys.path.insert(0, {source!r}); from sora_commander_next.host import Host; assert Host().ping()['status']=='OK'; assert 'mcp' not in sys.modules"
        self.assertEqual(execute(command(("-I", "-S", "-c", code)))["status"], "COMPLETED")

    def test_adapter_missing_sdk_and_fake_registration_only(self):
        with patch.dict(sys.modules, {"mcp": None}):
            with self.assertRaisesRegex(RuntimeError, "Optional MCP SDK"):
                build_server(Host())
        registered = {}

        class FakeFastMCP:
            def __init__(self, name):
                pass

            def tool(self):
                def register(func):
                    registered[func.__name__] = func
                    return func
                return register

        fake = ModuleType("mcp.server.fastmcp")
        fake.FastMCP = FakeFastMCP
        with patch.dict(sys.modules, {"mcp": ModuleType("mcp"), "mcp.server": ModuleType("mcp.server"), "mcp.server.fastmcp": fake}):
            build_server(Host())
        self.assertEqual(set(registered), {
            # Reads without approval; registration itself performs no reads.
            "git_snapshot", "git_diff", "git_log", "git_show", "git_remote_check",
            "list_tasks", "read_image_file",
            # Execution uses the existing Owner review path.
            "run_task", "ogf_cue",
            "run_command", "run_command_wait", "await_command_result",
            "read_process_output", "await_process_output", "stop_process",
            "stop_process_wait", "ping", "status", "list_folder",
            "read_text_file", "review_status", "preview_text_edit",
            "list_sessions", "previous_sessions", "recovery_overview", "request_status", "previous_output",
            # Ten bounded reads, two non-writing previews, local catalog only.
            "list_folder_page", "read_text_page", "find_files", "search_code",
            "path_info", "read_text_lines", "read_many_text_files",
            "compare_text_files", "read_text_chunk", "sha256_file",
            "preview_source_create", "preview_source_patch", "tool_catalog", "activity",
                "create_directory", "create_file", "move_file", "request_source_patch_dev",
                "list_processes", "get_config", "send_input",
        })
        self.assertFalse(set(registered) & {
            "approve", "reject", "approve_local", "reject_local",
            "request_source_create", "request_source_patch", "source_review_status",
            "cancel_source_review", "request_text_edit", "request_text_edit_wait",
            "edit_review_status", "await_edit_result", "request_text_create",
            "request_text_create_wait",
        })
        self.assertEqual(registered["ping"]()["status"], "OK")
        self.assertEqual(registered["run_command"](sys.executable, ["--version"], str(ROOT))["status"], "DENY")


if __name__ == "__main__":
    unittest.main()
