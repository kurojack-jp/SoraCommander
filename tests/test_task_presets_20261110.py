"""T2 Owner task presets: strict local file, run_task = run_command_wait by name."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._inherited_test_temp import inherited_test_temp
from sora_commander_next import task_presets as presets
from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import build_server, _validate_arguments

CMD = r"C:\Windows\System32\cmd.exe"


def task(**changes):
    value = {"name": "ogf-verify", "description": "Synthetic full check",
             "cwd": r"D:\Synthetic\Project", "executable": CMD,
             "argv": ["/d", "/s", "/c", "echo synthetic > artifacts\\x.log 2>&1"],
             "timeout": 1800, "output_limit": 65536}
    value.update(changes)
    return value


class FileTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(inherited_test_temp()))
        self.env = {"USERPROFILE": str(self.home)}
        self.folder = self.home / ".config" / "sora-commander"

    def write(self, data):
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / "tasks.json"
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode("utf-8"))
        return path

    def load(self):
        return presets.load_task_presets(self.env)

    def test_missing_file_means_no_tasks(self):
        self.assertEqual(self.load(), ())
        self.assertEqual(presets.load_task_presets({}), ())

    def test_valid_file_with_bom(self):
        self.write(b"\xef\xbb\xbf" + json.dumps({"tasks": [task(), task(name="b-2", argv=[])]}).encode())
        loaded = self.load()
        self.assertEqual([t.name for t in loaded], ["ogf-verify", "b-2"])
        self.assertEqual(loaded[0].argv[-1], "echo synthetic > artifacts\\x.log 2>&1")
        self.assertEqual(loaded[0].timeout, 1800.0)
        self.assertEqual(loaded[0].public()["argv"], task()["argv"])

    def test_invalid_content_refuses(self):
        extra = task(); extra["shell"] = True
        missing = task(); del missing["description"]
        cases = {
            "bad json": b"{tasks:",
            "duplicate key": b'{"tasks": [], "tasks": []}',
            "duplicate task key": ('{"tasks": [{"name": "a", "name": "b"}]}').encode(),
            "top level": {"tasks": [task()], "more": 1},
            "not list": {"tasks": {}},
            "empty": {"tasks": []},
            "33 tasks": {"tasks": [task(name=f"t{i}") for i in range(33)]},
            "extra key": {"tasks": [extra]},
            "missing key": {"tasks": [missing]},
            "bad name": {"tasks": [task(name="Bad_Name")]},
            "long name": {"tasks": [task(name="a" * 41)]},
            "duplicate name": {"tasks": [task(), task()]},
            "bad description": {"tasks": [task(description="a\nb")]},
            "relative cwd": {"tasks": [task(cwd="relative")]},
            "relative executable": {"tasks": [task(executable="cmd.exe")]},
            "argv not list": {"tasks": [task(argv="echo")]},
            "argv item type": {"tasks": [task(argv=[1])]},
            "argv item too long": {"tasks": [task(argv=["a" * 8193])]},
            "argv too many": {"tasks": [task(argv=["a"] * 129)]},
            "timeout": {"tasks": [task(timeout=0)]},
            "timeout bool": {"tasks": [task(timeout=True)]},
            "output limit": {"tasks": [task(output_limit=65537)]},
            "batch metacharacter": {"tasks": [task(executable=r"C:\Synthetic\npm.cmd", argv=["run", "test&calc.exe"])]},
            "too large": b'{"tasks": [' + b" " * 65536 + b"]}",
            "not utf-8": b'{"tasks": ["\xff"]}',
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                self.write(data)
                with self.assertRaises(presets.TaskPresetError):
                    self.load()

    def test_folder_named_tasks_json_refuses(self):
        (self.folder / "tasks.json").mkdir(parents=True)
        with self.assertRaises(presets.TaskPresetError):
            self.load()

    def test_linked_path_refuses(self):
        self.write({"tasks": [task()]})
        real = Path.lstat
        def linked(path):
            info = real(path)
            if path.name == "sora-commander":
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400, st_nlink=1)
            return info
        with patch.object(Path, "lstat", linked):
            with self.assertRaises(presets.TaskPresetError):
                self.load()


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.addCleanup(self.host.jobs.close)
        self.host.task_presets = (presets._task(1, task()),)
        self.host.run_command_wait = Mock(return_value={"status": "REVIEW", "pending_id": "p" * 32})
        self.sdk = build_server(self.host)

    def call(self, name, args):
        return asyncio.run(self.sdk.call_tool(name, args))[1]

    def test_run_task_is_run_command_wait_with_the_preset(self):
        result = self.call("run_task", {"name": "ogf-verify", "wait_seconds": 0,
                                        "request_id": "a" * 32})
        self.assertEqual(result, {"status": "REVIEW", "pending_id": "p" * 32})
        self.host.run_command_wait.assert_called_once_with(
            executable=CMD, argv=task()["argv"], cwd=r"D:\Synthetic\Project",
            timeout=1800.0, output_limit=65536, wait_seconds=0, request_id="a" * 32)

    def test_unknown_name_starts_nothing(self):
        self.assertEqual(self.call("run_task", {"name": "missing"}),
                         {"status": "DENY", "error": "UnknownTask"})
        self.host.run_command_wait.assert_not_called()

    def test_no_presets_means_unknown(self):
        self.host.task_presets = ()
        self.assertEqual(self.call("run_task", {"name": "ogf-verify"})["error"], "UnknownTask")
        self.assertEqual(self.call("list_tasks", {}), {"tasks_configured": False, "tasks": []})
        self.host.run_command_wait.assert_not_called()

    def test_extra_or_invalid_arguments_refused_before_running(self):
        for args in ({"name": "ogf-verify", "argv": ["x"]}, {"name": "ogf-verify", "cwd": "C:\\"},
                     {"name": "ogf-verify", "executable": CMD}, {"name": 3},
                     {"name": "ogf-verify", "wait_seconds": 91}, {"name": "ogf-verify", "wait_seconds": True},
                     {"name": ""}, {}):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    _validate_arguments("run_task", args)
                with self.assertRaises(Exception):
                    self.call("run_task", args)
        self.host.run_command_wait.assert_not_called()

    def test_list_tasks_has_the_command_but_no_file_path(self):
        result = self.call("list_tasks", {})
        self.assertTrue(result["tasks_configured"])
        self.assertEqual(result["tasks"], [presets._task(1, task()).public()])
        self.assertNotIn("tasks.json", json.dumps(result))
        self.assertNotIn(".config", json.dumps(result))

    def test_registration_and_help(self):
        tools = {t.name: t for t in self.sdk._tool_manager.list_tools()}
        self.assertEqual(set(tools["run_task"].parameters["properties"]),
                         {"name", "wait_seconds", "request_id"})
        self.assertEqual(tools["list_tasks"].parameters.get("properties", {}), {})
        self.assertIn("approval window opens for every run", tools["run_task"].description)
        self.assertIn("never call run_task again", tools["run_task"].description)
        self.assertIn("no approval window", tools["list_tasks"].description)


class StartupTests(unittest.TestCase):
    def test_broker_client_refuses_an_invalid_file(self):
        from sora_commander_next import owner_broker_client_main as client
        guard = Mock()
        with patch.object(client, "OwnerSessionGuard", return_value=guard), \
                patch.object(client, "build_client_host",
                             side_effect=presets.TaskPresetError("file is not valid JSON")), \
                patch.object(client, "serve_stdio") as serve, \
                patch("sys.stderr") as stderr:
            with self.assertRaises(SystemExit) as stop:
                client.main()
        self.assertEqual(stop.exception.code, 2)
        serve.assert_not_called()
        written = "".join(c.args[0] for c in stderr.write.call_args_list)
        self.assertIn("[HOLD] tasks.json: file is not valid JSON", written)

    def test_broker_client_loads_presets_onto_its_host(self):
        from sora_commander_next import owner_broker_client_main as client
        guard = SimpleNamespace(sid="S-1-5-21-1-2-3-1001", _identity=SimpleNamespace(session_id=1))
        roots = SimpleNamespace(full=(), read_only=())
        loaded = (presets._task(1, task()),)
        with patch.object(client, "startup_allowed_roots", return_value=roots), \
                patch.object(client, "load_task_presets", return_value=loaded) as load, \
                patch.object(client, "OwnerBrokerPipeClient"), \
                patch.object(client, "DevBrokerHost") as host_class:
            host = client.build_client_host(guard, Path("D:/synthetic"), {"USERPROFILE": "D:/x"})
        load.assert_called_once_with({"USERPROFILE": "D:/x"})
        self.assertIs(host, host_class.return_value)
        self.assertEqual(host.task_presets, loaded)

    def test_usual_mode_checks_the_file_at_startup(self):
        source = (Path(__file__).resolve().parents[1] / "src" / "sora_commander_next"
                  / "stdio_main.py").read_text(encoding="utf-8")
        self.assertIn("task_presets = load_task_presets(os.environ)", source)
        self.assertIn('print(f"[HOLD] tasks.json: {error}", file=sys.stderr', source)
        self.assertIn("host.task_presets = task_presets", source)


if __name__ == "__main__":
    unittest.main()
