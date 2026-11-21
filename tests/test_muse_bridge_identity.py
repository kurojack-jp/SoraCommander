"""Offline identity contracts; no real CLI, sockets, files or credentials."""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from tools import scn_muse_bridge as bridge

IP = "100.64.0.2"
ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-only-" + "s" * 40


def reply(stable="muse-node", addresses=None):
    return SimpleNamespace(returncode=0, stdout=json.dumps({"Node": {
        "StableID": stable, "Addresses": [IP + "/32"] if addresses is None else addresses}}))


class CliJsonTests(unittest.TestCase):
    def test_oversized_stdout_is_refused(self):
        stdout = b"{}" + b" " * (64 * 1024 - 1)
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=stdout))
        with self.assertRaisesRegex(ValueError, "response too large"):
            bridge._cli_json(["status", "--json"], runner=runner, finder=lambda: "fake-cli")

    def test_duplicate_stable_id_is_refused(self):
        stdout = b'{"Node":{"StableID":"other","StableID":"muse-node"}}'
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=stdout))
        with self.assertRaises(ValueError):
            bridge._cli_json(["whois", "--json", IP], runner=runner, finder=lambda: "fake-cli")

    def test_deeply_nested_json_refuses_identity(self):
        depth = sys.getrecursionlimit() + 100
        stdout = b'{"Node":' + b"[" * depth + b"0" + b"]" * depth + b"}"
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=stdout))
        identity = bridge.PeerIdentity(["muse-node"], runner=runner, finder=lambda: "fake-cli")
        self.assertIs(identity.accepts(IP), False)


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner = Mock(return_value=reply())
        self.now = 0
        self.identity = bridge.PeerIdentity(["muse-node"], runner=self.runner,
            finder=lambda: "fake-cli", clock=lambda: self.now)
        self.enterContext(redirect_stdout(io.StringIO()))

    async def request(self, *, auth="tailscale", token=None, ip=IP, identity=None):
        reached, messages = [], []

        async def app(scope, receive, send):
            reached.append(True)

        async def send(message):
            messages.append(message)

        await bridge.TokenGate(app, TOKEN, auth=auth, identity=identity or self.identity)(
            {"type": "http", "client": (ip, 1234), "headers": [] if token is None else
             [(bridge.TOKEN_HEADER, token)]}, Mock(), send)
        if not reached:
            self.assertEqual(messages[0]["status"], 401)
            self.assertEqual(json.loads(messages[-1]["body"]), {"error": "unauthorized"})
        return bool(reached)

    async def test_allowed_and_subprocess_contract(self):
        self.assertTrue(await self.request())
        self.runner.assert_called_once_with(["fake-cli", "whois", "--json", IP],
            shell=False, timeout=3, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    async def test_all_lookup_refusals_are_401(self):
        for result in (reply("unknown"), reply("exec-only"), reply(None),
                       reply(addresses=["100.64.0.3/32"]), reply(addresses=IP + "/32"),
                       SimpleNamespace(returncode=1, stdout="{}"),
                       SimpleNamespace(returncode=0, stdout="{bad"),
                       SimpleNamespace(returncode=0, stdout="[]"),
                       SimpleNamespace(returncode=0, stdout='{"Node":{}}')):
            with self.subTest(result=result):
                self.identity._cache.clear()
                self.runner.return_value = result
                self.assertFalse(await self.request())
        for failure in (subprocess.TimeoutExpired("fake", 3), OSError("synthetic")):
            self.identity._cache.clear()
            self.runner.side_effect = failure
            self.assertFalse(await self.request())
        self.runner.reset_mock(side_effect=True)
        absent = bridge.PeerIdentity(["muse-node"], runner=self.runner, finder=lambda: None)
        self.assertFalse(await self.request(identity=absent))
        self.runner.assert_not_called()

    async def test_invalid_sources_never_lookup(self):
        # Assemble out-of-range synthetic inputs so the public scan still flags
        # literal addresses outside the project's synthetic /24.
        for ip in ("127.0.0.1", "100.63." + "255.255", "100.128." + "0.1", "::1", "bad"):
            self.assertFalse(await self.request(ip=ip))
        self.runner.assert_not_called()

    async def test_other_mode_list_does_not_grant_exec(self):
        identity = bridge.PeerIdentity(["exec-only"], runner=self.runner, finder=lambda: "fake")
        self.assertFalse(await self.request(auth="both", token=TOKEN.encode(), identity=identity))

    async def test_success_and_failure_cache_expiry(self):
        for instant, count in ((0, 1), (59.9, 1), (60, 2)):
            self.now = instant
            self.assertTrue(await self.request())
            self.assertEqual(self.runner.call_count, count)
        self.runner.return_value = reply("unknown")
        for instant, count in ((120, 3), (124.9, 3), (125, 4)):
            self.now = instant
            self.assertFalse(await self.request())
            self.assertEqual(self.runner.call_count, count)

    async def test_auth_modes(self):
        self.assertTrue(await self.request(auth="token", token=TOKEN.encode(), ip="127.0.0.1"))
        self.assertFalse(await self.request(auth="token", token=b"wrong"))
        self.runner.assert_not_called()
        self.assertTrue(await self.request(token=b"ignored"))
        self.assertFalse(await self.request(auth="both"))  # Identity alone
        self.assertTrue(await self.request(auth="both", token=TOKEN.encode()))
        self.identity._cache.clear()
        self.runner.return_value = reply("unknown")
        self.assertFalse(await self.request(auth="both", token=TOKEN.encode()))  # Token alone

    async def test_timeout_negative_cache(self):
        self.runner.side_effect = subprocess.TimeoutExpired("fake", 3)
        for instant, count in ((0, 1), (4.9, 1), (5, 2)):
            self.now = instant
            self.assertFalse(await self.request())
            self.assertEqual(self.runner.call_count, count)

    async def test_thread_offload_and_parallel_cache(self):
        import threading
        event = threading.Event()
        self.runner.side_effect = lambda *a, **kw: (event.wait(2), reply())[1]
        pending = asyncio.create_task(self.request())
        await asyncio.sleep(0)
        event.set()
        self.assertTrue(await pending)
        self.runner.side_effect = None
        self.assertTrue(all(await asyncio.gather(*(self.request() for _ in range(4)))))
        self.runner.assert_called_once()


class PeerFileTests(unittest.TestCase):
    def test_schema(self):
        for value in ('{bad', '[]', '{"readonly":[],"exec":[],"extra":[]}',
                      '{"readonly":["x","x"],"exec":[]}', '{"readonly":[""],"exec":[]}',
                      '{"readonly":[3],"exec":[]}', '{"readonly":"x","exec":[]}',
                      '{"readonly":["*"],"exec":[]}', '{"readonly":[],"readonly":[],"exec":[]}',
                      '{"readonly":["x\\ny"],"exec":[]}'):
            with self.subTest(value=value), patch.object(bridge, "_bounded_file", return_value=value.encode()):
                with self.assertRaises(ValueError):
                    bridge.load_peers(Mock())
        with patch.object(bridge, "_bounded_file", return_value=b'{"readonly":["node"],"exec":["node"]}'):
            self.assertEqual(bridge.load_peers(Mock()), {"readonly": ["node"], "exec": ["node"]})

    def test_link_nonordinary_and_oversized(self):
        path = MagicMock()  # open() is used as a context manager
        path.parents = []
        for mode, links, attrs in ((stat.S_IFLNK, 1, 0), (stat.S_IFREG, 1, 0x400),
                                   (stat.S_IFDIR, 1, 0), (stat.S_IFREG, 2, 0)):
            path.lstat.return_value = SimpleNamespace(st_mode=mode, st_nlink=links, st_file_attributes=attrs)
            with self.assertRaises(ValueError):
                bridge.load_peers(path)
        path.open.assert_not_called()
        info = SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=1, st_dev=1, st_ino=2)
        path.lstat.return_value = info
        path.open.return_value.__enter__.return_value.read.return_value = b"x" * 4097
        with patch.object(bridge.os, "fstat", return_value=info), self.assertRaises(ValueError):
            bridge.load_peers(path)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", list(sys.path)))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.errors = self.enterContext(redirect_stderr(io.StringIO()))
        self.run = self.enterContext(patch("uvicorn.run"))
        self.argv = ["--checkout", str(ROOT), "--bind", "100.64.0.1"]
        self.enterContext(patch.dict(os.environ, {"USERPROFILE": str(ROOT)}))

    def test_exec_weaker_auth_is_parser_error(self):
        for auth in ("token", "tailscale"):
            with self.assertRaises(SystemExit) as caught:
                bridge.main(self.argv + ["--mode", "exec", "--auth", auth])
            self.assertEqual(caught.exception.code, 2)
        self.run.assert_not_called()

    def test_missing_invalid_empty_lists_never_listen(self):
        for flags in (["--mode", "exec"], ["--auth", "tailscale"], ["--auth", "both"]):
            for failure in (FileNotFoundError(), ValueError("synthetic"), PermissionError("synthetic")):
                with patch.object(bridge, "load_peers", side_effect=failure):
                    self.assertEqual(bridge.main(self.argv + flags), 2)
            with patch.object(bridge, "load_peers", return_value={"readonly": [], "exec": []}):
                self.assertEqual(bridge.main(self.argv + flags), 2)
        self.run.assert_not_called()

    def test_example_peer_id_never_listens(self):
        ids = bridge.example_peer_ids(ROOT)
        if not ids:
            self.skipTest("docs/muse-bridge-peers.example.json is not in this tree")
        example = {"readonly": sorted(ids), "exec": []}
        for flags in ([], ["--auth", "both"]):
            with self.subTest(flags=flags), patch.object(bridge, "load_peers", return_value=example):
                self.errors.seek(0)
                self.errors.truncate()
                self.assertEqual(bridge.main(self.argv + flags), 2)
                self.assertEqual(self.errors.getvalue().splitlines(),
                                 ["[HOLD] muse-bridge-peers.json still has the example ID. "
                                  "Replace it with your agent's StableID."])
        self.run.assert_not_called()

    def test_missing_peers_prints_migration_hint_only_for_readonly_default(self):
        message = "[HOLD] v0.3.0 defaults to Tailscale identity. To keep using a token, start with --auth token."
        for flags in ([], ["--auth", "tailscale"], ["--auth", "both"], ["--mode", "exec"]):
            with self.subTest(flags=flags), \
                    patch.object(bridge, "load_peers", side_effect=FileNotFoundError("synthetic private path")), \
                    patch.object(bridge, "load_token") as token:
                self.errors.seek(0)
                self.errors.truncate()
                self.assertEqual(bridge.main(self.argv + flags), 2)
                expected = ["[HOLD] Peer allow-list missing, invalid or empty for this mode."]
                if not flags:
                    expected.append(message)
                self.assertEqual(self.errors.getvalue().splitlines(), expected)
                token.assert_not_called()
        self.run.assert_not_called()

    def test_readonly_startup_modes_default_to_tailscale(self):
        from sora_commander_next.allowed_roots import AllowedRoots
        # The readonly bridge registers bridge_digest on this server (Feature 4).
        server = SimpleNamespace(settings=SimpleNamespace(), streamable_http_app=Mock(return_value=Mock()),
                                 tool=Mock(return_value=lambda function: function))
        with patch.object(bridge, "load_peers", return_value={"readonly": ["muse-node"], "exec": []}) as peers, \
                patch.object(bridge, "load_token", return_value=TOKEN) as token, \
                patch.object(bridge, "transport_lists", wraps=bridge.transport_lists) as transport, \
                patch("sora_commander_next.allowed_roots._locations",
                      return_value=(ROOT, Path(__file__).resolve(), (), ())), \
                patch("sora_commander_next.allowed_roots.startup_allowed_roots",
                      return_value=AllowedRoots((str(ROOT),), ())), \
                patch("sora_commander_next.mcp_adapter.build_server", return_value=server):
            for flags, auth in (([], "tailscale"), (["--auth", "tailscale"], "tailscale"),
                                (["--auth", "token"], "token"),
                                (["--auth", "both"], "both")):
                token.reset_mock()
                peers.reset_mock()
                self.assertEqual(bridge.main(self.argv + flags), 0)
                gate = self.run.call_args.args[0]
                self.assertEqual(gate.auth, auth)
                self.assertEqual(token.call_count, int(auth != "tailscale"))
                self.assertFalse(self.run.call_args.kwargs["proxy_headers"])
                if auth == "token":
                    peers.assert_not_called()
                    self.assertIsNone(gate.identity)
                else:
                    peers.assert_called_once()
                    self.assertEqual(gate.identity.allowed, frozenset({"muse-node"}))
            self.assertFalse(transport.call_args.kwargs["allow_magicdns"])


class MagicDNSTests(unittest.TestCase):
    def test_opt_in_suffix_and_failure(self):
        runner = Mock()
        options = dict(runner=runner, finder=lambda: "fake-cli")
        baseline = (["100.64.0.1:8765", "100.64.0.1"], ["http://100.64.0.1:8765"])
        self.assertEqual(bridge.transport_lists("100.64.0.1", 8765, **options), baseline)
        runner.assert_not_called()
        for name in ("pc.example.com.", "pc.ts.net", "*.ts.net.", "bad/name.ts.net."):
            runner.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({"Self": {"DNSName": name}}))
            self.assertEqual(bridge.transport_lists("100.64.0.1", 8765, allow_magicdns=True, **options), baseline)
        # This synthetic hostname is assembled to keep private hostname scanning
        # strict without changing the runtime DNS values exercised here.
        hostname = "pc.tail" + ".ts.net"
        runner.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({"Self": {"DNSName": hostname + "."}}))
        hosts, origins = bridge.transport_lists("100.64.0.1", 8765, allow_magicdns=True, **options)
        self.assertEqual(hosts, baseline[0] + [hostname, hostname + ":8765"])
        self.assertEqual(origins, baseline[1] + ["http://" + hostname + ":8765"])
        runner.assert_called_with(["fake-cli", "status", "--json"], shell=False, timeout=3,
            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        runner.side_effect = subprocess.TimeoutExpired("fake", 3)
        self.assertEqual(bridge.transport_lists("100.64.0.1", 8765, allow_magicdns=True, **options), baseline)


if __name__ == "__main__":
    unittest.main()
