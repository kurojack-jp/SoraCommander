"""Stage 4b MCP wire tests with the real VM agent over in-memory TLS."""

import asyncio
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
from threading import Event, Thread
from time import monotonic
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError

from tests._inherited_test_temp import inherited_test_temp
from tests.test_vm_link_20261125 import PRIVATE, VmFixture
from tests.test_vm_screen_protocol_20261125 import KEY
from tests.test_vm_transfer_20261125 import allow_file_loader
from sora_commander_next import vm_agent as agent, vm_client as client, vm_control as control, vm_files as files
from sora_commander_next import vm_link as link, vm_protocol as p, vm_transfer as wire, vm_transfers


NAME = "資料.bin"
PAYLOAD = b"synthetic transfer"
TOOLS = ("screenshot", "elements", "wait", "file_put", "file_get")


class VmFileToolsTests(VmFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(allow_file_loader())
        self.root = Path(self.enterContext(inherited_test_temp()))
        self.enterContext(patch.dict(os.environ, USERPROFILE=str(self.root), LOCALAPPDATA=str(self.root)))
        self.send = self.root / "send"
        self.receive = self.root / "receive"
        self.guest = self.root / "guest"
        for folder in (self.send, self.receive, self.guest, self.guest / "inbox", self.guest / "outbox"):
            folder.mkdir()
        self.enterContext(patch.multiple(p, CHUNK_MAX=4, FILE_MAX=32, FILE_TOTAL_MAX=64, FILE_FREE_MIN=10))
        self.free = self.enterContext(patch.object(files, "free_bytes", return_value=100))
        self.marker = self.enterContext(patch.object(client, "_mark"))
        self.exchange = self.enterContext(patch.object(client, "exchange", wraps=client.exchange))
        super().setUp()
        self.real_exchange = self.exchange._mock_wraps
        self.addCleanup(lambda: self.host.close())
        (self.send / NAME).write_bytes(PAYLOAD)
        (self.guest / "outbox" / NAME).write_bytes(PAYLOAD)

    def make_agent(self, **changes):
        args = Namespace(**(vars(self.args) | {"tools": ",".join(TOOLS)} | changes))
        instance = agent.Agent(args, KEY, self.backend, (640, 480), files=files.FileStore(str(self.guest)))
        self.addCleanup(instance.close_files)
        return instance

    def make_server(self, **config):
        original = control.VmTransfers

        def make_transfers(*args):
            self.transfers = original(*args)
            return self.transfers

        with patch.object(control, "VmTransfers", make_transfers):
            return super().make_server(**({"send_folder": self.send, "receive_folder": self.receive, "tools": TOOLS} | config))

    async def call(self, operation, **arguments):
        content, answer = await self.server.call_tool("vm_file_" + operation, arguments)
        self.assertEqual(len(content), 1)
        self.assertEqual(json.loads(content[0].text), answer)
        return answer

    async def send_file(self, name=NAME, wait=20):
        return await self.call("put", name=name, wait_seconds=wait)

    async def receive_file(self, name=NAME, wait=20):
        return await self.call("get", name=name, wait_seconds=wait)

    def ops(self):
        return [call.args[2] for call in self.exchange.call_args_list]

    def gate(self, operation, *, after=False):
        reached, release = Event(), Event()
        self.addCleanup(release.set)

        def exchange(*args, **kwargs):
            if args[2] != operation or reached.is_set():
                return self.real_exchange(*args, **kwargs)
            response = self.real_exchange(*args, **kwargs) if after else None
            reached.set()
            if not release.wait(4):
                raise AssertionError("fixture gate")
            return response if after else self.real_exchange(*args, **kwargs)

        self.exchange.side_effect = exchange
        return reached, release

    async def wait_event(self, event):
        self.assertTrue(await asyncio.to_thread(event.wait, 2))

    async def poll_for(self, transfer_id, state):
        deadline = monotonic() + 2
        while monotonic() < deadline:
            reply = await self.call("status", transfer_id=transfer_id, wait_seconds=0)
            if reply["status"] == state:
                return reply
            await asyncio.sleep(0.01)
        self.fail("expected " + state)

    def assert_clean_receive(self):
        self.assertEqual(list(self.receive.iterdir()), [])

    async def test_upload_chunks_digest_and_boot_headers(self):
        await self.start()
        reply = await self.send_file()
        self.assertEqual((reply["status"], reply["name"], reply["size"], reply["sha256"]),
                         ("DONE", NAME, len(PAYLOAD), sha256(PAYLOAD).hexdigest()))
        self.assertEqual((self.guest / "inbox" / NAME).read_bytes(), PAYLOAD)
        self.assertEqual(self.ops(), ["probe", "upload_create"] + ["upload_patch"] * 5)
        for exchange in self.exchange.call_args_list[1:]:
            self.assertEqual(exchange.args[3][2]["SCN-Boot-Id"], self.agent.boot_id)

    async def test_download_chunks_digest_and_mark_before_publish(self):
        await self.start()

        def mark(path):
            self.assertFalse((self.receive / NAME).exists())
            with files.plain_reader(path) as (stream, _):
                self.assertEqual(stream.read(), PAYLOAD)

        self.marker.side_effect = mark
        reply = await self.receive_file()
        self.assertEqual((reply["status"], reply["size"], reply["sha256"]),
                         ("DONE", len(PAYLOAD), sha256(PAYLOAD).hexdigest()))
        self.assertEqual(self.ops(), ["probe", "files"] + ["get"] * 5)
        headers = [call.args[3][2] for call in self.exchange.call_args_list[1:]]
        self.assertTrue(all(item["SCN-Boot-Id"] == self.agent.boot_id for item in headers))
        self.assertNotIn("If-Match", headers[1])
        self.assertTrue(all("If-Match" in item for item in headers[2:]))
        self.marker.assert_called_once()
        self.assertEqual(list(self.receive.iterdir()), [self.receive / NAME])
        self.assertEqual((self.receive / NAME).read_bytes(), PAYLOAD)

    async def test_empty_files_have_zero_byte_digest_and_one_request(self):
        await self.start()
        (self.send / NAME).write_bytes(b"")
        (self.guest / "outbox" / NAME).write_bytes(b"")
        for transfer in (self.send_file, self.receive_file):
            reply = await transfer()
            self.assertEqual((reply["status"], reply["size"], reply["sha256"]),
                             ("DONE", 0, sha256(b"").hexdigest()))
        self.assertEqual(self.ops().count("upload_patch"), 1)
        self.assertEqual(self.ops().count("get"), 1)

    async def test_wait_zero_status_busy_and_local_session_summary(self):
        await self.start()
        self.assertIsNone((await self.session())["transfer"])
        reached, release = self.gate("upload_patch", after=True)
        first = await self.send_file(wait=0)
        await self.wait_event(reached)
        transfer_id = first["transfer_id"]
        p.hex_bytes(transfer_id, 16)
        self.assertEqual(first["status"], "TRANSFERRING")
        self.assertEqual((await self.send_file())["status"], "TRANSFER_BUSY")
        self.assertEqual((await self.receive_file())["status"], "TRANSFER_BUSY")
        self.assertEqual((await self.call("status", transfer_id=transfer_id, wait_seconds=0))["bytes_done"], 0)
        before = self.ops()
        self.assertEqual((await self.session())["transfer"], dict(state="TRANSFERRING", direction="put",
                                                                bytes_done=0, total_bytes=len(PAYLOAD), reason=None))
        self.assertEqual(self.ops(), before)
        release.set()
        self.assertEqual((await self.call("status", transfer_id=transfer_id))["status"], "DONE")
        self.assertEqual((await self.session())["transfer"]["state"], "DONE")
        self.assertEqual((await self.send_file())["reason"], "FILE_EXISTS")
        self.assertEqual((await self.session())["transfer"]["reason"], "FILE_EXISTS")

    async def assert_foreground(self, tool, arguments, first_route):
        reached, release = self.gate("upload_patch", after=True)
        first = await self.send_file(wait=0)
        await self.wait_event(reached)
        foreground = asyncio.create_task(self.server.call_tool(tool, arguments))
        deadline = monotonic() + 1
        while not self.server_link._foreground_waiters and monotonic() < deadline:
            await asyncio.sleep(0.01)
        self.assertEqual(self.server_link._foreground_waiters, 1)
        # Wait past the normal 0.5 s lock limit before releasing the chunk.
        await asyncio.sleep(0.55)
        release.set()
        content = await foreground
        self.assertEqual(json.loads(content[0].text)["status"], "OK")
        self.assertEqual((await self.call("status", transfer_id=first["transfer_id"]))["status"], "DONE")
        ops = self.ops()
        first_patch = ops.index("upload_patch")
        self.assertIn(first_route, ops[first_patch + 1:ops.index("upload_patch", first_patch + 1)])

    async def test_look_gets_turn_between_chunks(self):
        await self.start()
        await self.assert_foreground("vm_look", {}, "screenshot")

    async def test_act_gets_turn_between_chunks(self):
        await self.start()
        picture, _ = await self.look()
        await self.assert_foreground("vm_act", {"screen_id": picture["screen_id"], "actions": [{"type": "wait", "ms": 1}]},
                                     "wait")

    async def test_lease_first(self):
        for operation, args in (("list", {}), ("put", {"name": "../x"}), ("get", {"name": NAME})):
            self.assertEqual((await self.call(operation, **args))["status"], "VM_SESSION_REQUIRED")
        self.assertEqual(self.ops(), [])

    async def test_config_capability_refuses_before_probe(self):
        self.server = self.make_server(tools=("screenshot",))
        await self.start()
        self.assertEqual((await self.send_file())["status"], "TOOL_DISABLED")
        self.assertEqual((await self.receive_file())["status"], "TOOL_DISABLED")
        self.assertEqual((await self.call("list"))["status"], "TOOL_DISABLED")
        self.assertEqual(self.ops(), [])

    async def test_agent_capability_refuses_after_probe(self):
        self.agent = self.make_agent(tools="screenshot")
        await self.start()
        self.assertEqual((await self.send_file())["reason"], "TOOL_DISABLED")
        self.assertEqual((await self.receive_file())["reason"], "TOOL_DISABLED")
        self.assertEqual((await self.call("list"))["status"], "TOOL_DISABLED")
        self.assertEqual(self.ops(), ["probe"] * 3)

    async def test_host_file_list_filters_names_links_and_caps(self):
        await self.start()
        (self.send / ".hidden").write_bytes(b"x")
        (self.send / "folder").mkdir()
        original = self.send / "hard-original"
        alias = self.send / "hard-alias"
        original.write_bytes(b"x")
        os.link(original, alias)
        try:
            first = await self.call("list")
            self.assertEqual(first["send"], [{"name": NAME, "size": len(PAYLOAD)}])
            self.assertEqual(first["outbox"], first["send"])
            self.assertEqual(first["inbox"], [])
            self.assertFalse(first["truncated"])
            for i in range(100):
                (self.send / f"file{i}").touch()
            reply = await self.call("list")
            self.assertEqual(len(reply["send"]), 100)
            self.assertTrue(reply["truncated"])
        finally:
            alias.unlink()

    async def test_missing_folders_fail_without_vm_request(self):
        self.server = self.make_server(send_folder=self.root / "missing", receive_folder=self.root / "absent")
        await self.start()
        self.assertEqual((await self.call("list"))["status"], "HOST_FOLDER_UNAVAILABLE")
        self.assertEqual((await self.send_file())["reason"], "HOST_FOLDER_UNAVAILABLE")
        self.assertEqual((await self.receive_file())["reason"], "HOST_FOLDER_UNAVAILABLE")
        self.assertEqual(self.ops(), [])

    async def test_linked_folders_are_rejected(self):
        link_path = self.root / "linked"
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(self.send), str(link_path))
            self.addCleanup(os.rmdir, link_path)
        else:
            link_path.symlink_to(self.send, target_is_directory=True)
            self.addCleanup(link_path.unlink)
        self.server = self.make_server(send_folder=link_path, receive_folder=link_path)
        await self.start()
        self.assertEqual((await self.call("list"))["status"], "HOST_FOLDER_UNAVAILABLE")
        self.assertEqual((await self.send_file())["reason"], "HOST_FOLDER_UNAVAILABLE")
        self.assertEqual((await self.receive_file())["reason"], "HOST_FOLDER_UNAVAILABLE")
        self.assertEqual(self.ops(), [])

    async def test_existing_target_and_invalid_names_never_connect(self):
        await self.start()
        (self.receive / NAME).write_bytes(b"preserve")
        self.assertEqual((await self.receive_file())["reason"], "FILE_EXISTS")
        self.assertEqual((self.receive / NAME).read_bytes(), b"preserve")
        for name in ("", "a/b", "NUL.txt", "x" * 121, "x\u202e"):
            self.assertEqual((await self.send_file(name))["status"], "NAME_NOT_ALLOWED")
        self.assertEqual(self.ops(), [])

    async def test_wire_rejects_bad_types_and_fields(self):
        for operation, args in [
            ("list", {"name": NAME}), ("put", {}), ("get", {"name": 9}),
            ("status", {}), ("cancel", {"transfer_id": False}), ("cancel", {"transfer_id": "x", "wait_seconds": 0}),
            ("put", {"name": NAME, "wait_seconds": True}), ("get", {"name": NAME, "wait_seconds": "0"}),
            ("put", {"name": NAME, "wait_seconds": float("nan")}),
            ("get", {"name": NAME, "wait_seconds": float("inf")}),
            ("put", {"name": NAME, "wait_seconds": -1}), ("put", {"name": NAME, "wait_seconds": 45.1}),
            ("status", {"transfer_id": "x", "wait_seconds": None}),
        ]:
            with self.subTest(operation=operation, args=args), self.assertRaisesRegex(ToolError, "^Invalid tool request$"):
                await self.server.call_tool("vm_file_" + operation, args)
        self.assertEqual((await self.call("status", transfer_id="x", wait_seconds=45))["status"], "TRANSFER_NOT_FOUND")

    async def test_last_finished_only(self):
        await self.start()
        one = await self.send_file()
        self.assertEqual((await self.call("status", transfer_id=one["transfer_id"]))["status"], "DONE")
        await self.receive_file()
        self.assertEqual((await self.call("status", transfer_id=one["transfer_id"]))["status"], "TRANSFER_NOT_FOUND")
        self.assertEqual((await self.call("cancel", transfer_id=one["transfer_id"]))["status"], "TRANSFER_NOT_FOUND")

    async def test_cancel_during_hash(self):
        await self.start()
        reached, release = Event(), Event()
        original = wire.UploadTransfer.hash_source

        def during_hash(active, check):
            reached.set()
            release.wait(3)
            return original(active, check)

        with patch.object(wire.UploadTransfer, "hash_source", during_hash):
            one = await self.send_file(wait=0)
            await self.wait_event(reached)
            status = await self.call("status", transfer_id=one["transfer_id"], wait_seconds=0)
            self.assertEqual((status["status"], status["bytes_done"], status["total_bytes"]), ("TRANSFERRING", 0, len(PAYLOAD)))
            task = asyncio.create_task(self.call("cancel", transfer_id=one["transfer_id"]))
            await asyncio.sleep(0.04)
            release.set()
            self.assertEqual((await task)["status"], "CANCELLED")
        self.assertEqual(self.ops(), [])

    async def cancel_at(self, operation, transfer):
        await self.start()
        reached, release = self.gate(operation, after=True)
        one = await transfer(wait=0)
        await self.wait_event(reached)
        future = asyncio.create_task(self.call("cancel", transfer_id=one["transfer_id"]))
        await asyncio.sleep(0.04)
        release.set()
        return await future

    async def test_cancel_after_create_deletes_only_the_part(self):
        self.assertEqual((await self.cancel_at("upload_create", self.send_file))["status"], "CANCELLED")
        self.assertEqual(self.ops(), ["probe", "upload_create", "upload_delete"])
        self.assertFalse((self.guest / "inbox" / NAME).exists())

    async def test_cancel_after_probe_sends_no_create(self):
        self.assertEqual((await self.cancel_at("probe", self.send_file))["status"], "CANCELLED")
        self.assertEqual(self.ops(), ["probe"])

    async def test_cancel_after_listing_sends_no_range(self):
        self.assertEqual((await self.cancel_at("files", self.receive_file))["status"], "CANCELLED")
        self.assertEqual(self.ops(), ["probe", "files"])
        self.assert_clean_receive()

    async def test_cancel_between_put_chunks(self):
        self.assertEqual((await self.cancel_at("upload_patch", self.send_file))["status"], "CANCELLED")
        self.assertEqual(self.ops(), ["probe", "upload_create", "upload_patch", "upload_delete"])

    async def test_cancel_between_get_ranges_removes_host_part(self):
        # Stop after the first range has been written.
        wrapper = wire.DownloadTransfer.step
        reached, release = Event(), Event()

        def step(active, send):
            wrapper(active, send)
            if active.offset == p.CHUNK_MAX:
                reached.set()
                release.wait(3)

        with patch.object(wire.DownloadTransfer, "step", step):
            await self.start()
            one = await self.receive_file(wait=0)
            await self.wait_event(reached)
            self.assertEqual(len(list(self.receive.iterdir())), 1)
            task = asyncio.create_task(self.call("cancel", transfer_id=one["transfer_id"]))
            await asyncio.sleep(0.04)
            release.set()
            self.assertEqual((await task)["status"], "CANCELLED")
        self.assertEqual(self.ops(), ["probe", "files", "get"])
        self.assert_clean_receive()

    async def test_cancel_during_mark_blocks_rename(self):
        await self.start()
        reached, release = Event(), Event()

        def mark(_):
            reached.set()
            release.wait(3)

        self.marker.side_effect = mark
        first = await self.receive_file(wait=0)
        await self.wait_event(reached)
        task = asyncio.create_task(self.call("cancel", transfer_id=first["transfer_id"]))
        await asyncio.sleep(0.04)
        release.set()
        self.assertEqual((await task)["status"], "CANCELLED")
        self.assert_clean_receive()

    async def deny_after_chunk(self, deny):
        await self.start()
        reached, release = self.gate("upload_patch", after=True)
        reply = await self.send_file(wait=0)
        await self.wait_event(reached)
        deny()
        release.set()
        return await self.call("status", transfer_id=reply["transfer_id"])

    async def test_stop_between_chunks(self):
        result = await self.deny_after_chunk(self.stop)
        self.assertEqual((result["status"], result["reason"]), ("FAILED", "STOPPED"))
        self.assertEqual(self.ops(), ["probe", "upload_create", "upload_patch"])

    async def test_expiry_between_chunks(self):
        result = await self.deny_after_chunk(lambda: setattr(self.server_lease._lease, "deadline", 0))
        self.assertEqual((result["status"], result["reason"]), ("FAILED", "HOST_DEADLINE"))
        self.assertEqual(self.ops(), ["probe", "upload_create", "upload_patch"])

    async def test_changed_lease_hides_old_transfer_and_sends_no_more(self):
        await self.start()
        reached, release = self.gate("upload_patch", after=True)
        old = await self.send_file(wait=0)
        await self.wait_event(reached)
        self.stop()
        self.assertEqual((await self.session())["state"], "ENDED")
        self.host.read_process_output.side_effect = self.running
        await self.start()
        release.set()
        self.assertEqual((await self.call("status", transfer_id=old["transfer_id"]))["status"], "TRANSFER_NOT_FOUND")
        self.assertIsNone((await self.session())["transfer"])
        deadline = monotonic() + 2
        while self.server_link.transferring.is_set() and monotonic() < deadline:
            await asyncio.sleep(0.01)
        self.assertFalse(self.server_link.transferring.is_set())
        self.assertEqual(self.ops(), ["probe", "upload_create", "upload_patch"])

    async def test_lock_pauses_until_explicit_same_put(self):
        result = await self.deny_after_chunk(lambda: setattr(self.guard.verify, "return_value", False))
        self.assertEqual((result["status"], result["reason"], result["bytes_done"]),
                         ("PAUSED", "OWNER_SESSION_UNAVAILABLE", 4))
        self.guard.verify.return_value = True
        await asyncio.sleep(0.29)
        self.assertEqual(self.ops(), ["probe", "upload_create", "upload_patch"])
        self.assertEqual((await self.receive_file())["status"], "TRANSFER_BUSY")
        resumed = await self.send_file()
        self.assertEqual((resumed["status"], resumed["transfer_id"]), ("DONE", result["transfer_id"]))
        self.assertEqual(self.ops().count("upload_create"), 1)

    async def fault_one_nonfinal(self):
        await self.start()
        lost = False

        def exchange(*args, **kwargs):
            nonlocal lost
            response = self.real_exchange(*args, **kwargs)
            if args[2] == "upload_patch" and not lost:
                lost = True
                raise wire.UncertainTransfer()
            return response

        self.exchange.side_effect = exchange
        result = await self.send_file()
        self.assertEqual((result["status"], result["reason"]), ("PAUSED", "VM_UNREACHABLE"))
        self.assertEqual(self.ops(), ["probe", "upload_create", "upload_patch"])
        return result

    async def test_unreachable_upload_resumes_with_one_offset_query(self):
        old = await self.fault_one_nonfinal()
        result = await self.send_file()
        self.assertEqual((result["status"], result["transfer_id"]), ("DONE", old["transfer_id"]))
        self.assertEqual(self.ops()[3:6], ["probe", "upload_status", "upload_patch"])
        offsets = [call.args[3][2]["Upload-Offset"] for call in self.exchange.call_args_list if call.args[2] == "upload_patch"]
        self.assertEqual(offsets, ["0", "4", "8", "12", "16"])

    async def test_changed_source_after_pause(self):
        await self.fault_one_nonfinal()
        (self.send / NAME).write_bytes(b"change")
        self.assertEqual((await self.send_file())["reason"], "FILE_CHANGED")
        self.assertEqual(self.ops(), ["probe", "upload_create", "upload_patch"])

    async def test_changed_boot_after_pause(self):
        await self.fault_one_nonfinal()
        self.agent.boot_id = "f" * 32
        # A foreground look can learn the new boot before the upload is resumed.
        picture, _ = await self.look()
        self.assertEqual(picture["status"], "VM_AGENT_RESTARTED")
        result = await self.send_file()
        self.assertEqual((result["status"], result["reason"]), ("FAILED", "VM_AGENT_RESTARTED"))
        self.assertEqual(self.ops()[-2:], ["probe", "probe"])

    async def test_a_restart_noticed_by_a_transfer_leaves_no_usable_screen(self):
        await self.start()
        picture, _ = await self.look()
        self.agent.boot_id = "f" * 32
        self.assertEqual((await self.send_file())["reason"], "VM_AGENT_RESTARTED")
        # The next probe sees the new boot twice, so only the cleared screen refuses the old one.
        content = await self.server.call_tool("vm_act", {"screen_id": picture["screen_id"],
                                                         "actions": [{"type": "wait", "ms": 1}]})
        self.assertEqual(json.loads(content[0].text)["status"], "STALE_SCREEN")
        self.assertNotIn("wait", self.ops())

    async def test_stop_while_connecting_sends_and_charges_nothing(self):
        for operation, at in (("put", 3), ("get", 3)):  # probe, then create or listing, then the first chunk
            with self.subTest(operation=operation):
                await self.start()
                connect = self.connector.side_effect
                start = self.connector.call_count
                self.exchange.reset_mock()

                def hooked(*addresses):
                    if self.connector.call_count - start == at:
                        self.stop()
                    return connect(*addresses)

                self.connector.side_effect = hooked
                result = await (self.send_file() if operation == "put" else self.receive_file())
                self.connector.side_effect = connect
                self.assertEqual((result["status"], result["reason"]), ("FAILED", "STOPPED"))
                self.assertEqual(self.ops(), ["probe", "upload_create" if operation == "put" else "files"])
                self.assertEqual(self.transfers._charged, 0)
                self.assert_clean_receive()
                self.agent.close_files()
                await self.session()
                self.host.read_process_output.side_effect = self.running

    async def test_a_lost_creation_is_never_sent_again(self):
        await self.start()

        def exchange(*args, **kwargs):
            response = self.real_exchange(*args, **kwargs)
            if args[2] == "upload_create":
                raise wire.UncertainTransfer()
            return response

        self.exchange.side_effect = exchange
        result = await self.send_file()
        self.assertEqual((result["status"], result["reason"]), ("PAUSED", "VM_UNREACHABLE"))
        result = await self.send_file()
        self.assertEqual((result["status"], result["reason"]), ("FAILED", "UPLOAD_NOT_COMPLETED"))
        self.assertEqual(self.ops(), ["probe", "upload_create"])

    async def test_budget_is_checked_again_before_every_chunk(self):
        for operation, name in (("put", "upload_patch"), ("get", "get")):
            with self.subTest(operation=operation):
                if operation == "put":
                    await self.start()
                self.exchange.reset_mock()
                self.transfers._charged = 0

                def exchange(*args, **kwargs):
                    response = self.real_exchange(*args, **kwargs)
                    if args[2] == name:
                        self.transfers._charged = p.FILE_TOTAL_MAX - 3
                    return response

                self.exchange.side_effect = exchange
                result = await (self.send_file() if operation == "put" else self.receive_file())
                self.assertEqual((result["status"], result["reason"]), ("FAILED", "TRANSFER_LIMIT"))
                self.assertEqual(self.ops().count(name), 1)
                self.agent.close_files()

    async def test_a_lying_final_status_is_unknown(self):
        await self.start()
        size = len(PAYLOAD)
        for reply in (dict(offset=8, length=size, complete=False), dict(offset=size, length=size + 1, complete=True),
                      dict(offset=16, length=size + 1, complete=False), dict(offset=size, length=size, complete=False)):
            with self.subTest(reply=reply):
                self.exchange.reset_mock()
                self.transfers._charged = 0
                self.agent.file_bytes = p.FILE_TOTAL_MAX

                def exchange(*args, **kwargs):
                    if args[2] == "upload_status":
                        return dict(result="OK", **reply)
                    response = self.real_exchange(*args, **kwargs)
                    if args[2] == "upload_patch" and args[3][2]["Upload-Complete"] == "?1":
                        raise wire.UncertainTransfer()
                    return response

                self.exchange.side_effect = exchange
                self.assertEqual((await self.send_file())["status"], "UNKNOWN")
                self.assertEqual(self.ops().count("upload_status"), 1)
                (self.guest / "inbox" / NAME).unlink()

    async def test_cancel_paused_with_valid_lease(self):
        pause = await self.fault_one_nonfinal()
        result = await self.call("cancel", transfer_id=pause["transfer_id"])
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(self.ops()[-1:], ["upload_delete"])

    async def test_cancel_locked_paused_never_sends_delete(self):
        pause = await self.deny_after_chunk(lambda: setattr(self.guard.verify, "return_value", False))
        self.assertEqual((await self.call("cancel", transfer_id=pause["transfer_id"]))["status"], "CANCELLED")
        self.assertEqual(self.ops(), ["probe", "upload_create", "upload_patch"])

    async def test_unreachable_download_removes_part_and_waits_for_cancel(self):
        await self.start()
        ranges = 0

        def exchange(*args, **kwargs):
            nonlocal ranges
            if args[2] == "get":
                ranges += 1
                if ranges == 2:
                    raise p.TransportError()
            return self.real_exchange(*args, **kwargs)

        self.exchange.side_effect = exchange
        pause = await self.receive_file()
        self.assertEqual((pause["status"], pause["reason"], pause["bytes_done"]), ("PAUSED", "VM_UNREACHABLE", 4))
        self.assert_clean_receive()
        self.assertEqual((await self.receive_file())["status"], "TRANSFER_BUSY")
        self.assertEqual(ranges, 2)
        self.assertEqual((await self.call("cancel", transfer_id=pause["transfer_id"]))["status"], "CANCELLED")

    async def test_boot_changes_before_listing(self):
        await self.start()
        # Change after the encoded probe response so only the listing sees the new boot.
        original = self.real_exchange

        def exchange(*args, **kwargs):
            reply = original(*args, **kwargs)
            if args[2] == "probe":
                self.agent.boot_id = "b" * 32
            return reply

        self.exchange.side_effect = exchange
        self.assertEqual((await self.call("list"))["status"], "VM_AGENT_RESTARTED")
        self.assertEqual(self.ops(), ["probe", "files"])

    async def test_boot_changes_before_first_range(self):
        await self.start()

        def exchange(*args, **kwargs):
            reply = self.real_exchange(*args, **kwargs)
            if args[2] == "files":
                self.agent.boot_id = "c" * 32
            return reply

        self.exchange.side_effect = exchange
        self.assertEqual((await self.receive_file())["reason"], "VM_AGENT_RESTARTED")
        self.assertEqual(self.ops(), ["probe", "files", "get"])
        self.assert_clean_receive()

    async def test_file_and_lease_budgets_at_exact_limits(self):
        await self.start()
        (self.send / NAME).write_bytes(b"x" * p.FILE_MAX)
        (self.guest / "outbox" / NAME).write_bytes(b"y" * p.FILE_MAX)
        self.assertEqual((await self.send_file())["status"], "DONE")
        self.assertEqual((await self.receive_file())["status"], "DONE")
        (self.send / "more").write_bytes(b"x")
        before = self.ops()
        result = await self.send_file("more")
        self.assertEqual((result["status"], result["reason"]), ("FAILED", "TRANSFER_LIMIT"))
        self.assertEqual(self.ops(), before)
        (self.guest / "outbox" / "more").write_bytes(b"x")
        self.assertEqual((await self.receive_file("more"))["reason"], "TRANSFER_LIMIT")
        self.assertEqual(self.ops()[-2:], ["probe", "files"])

    async def test_large_files_refused_before_chunk(self):
        await self.start()
        (self.send / NAME).write_bytes(b"x" * (p.FILE_MAX + 1))
        self.assertEqual((await self.send_file())["reason"], "FILE_TOO_LARGE")
        self.assertEqual(self.ops(), [])
        (self.guest / "outbox" / NAME).write_bytes(b"x" * (p.FILE_MAX + 1))
        self.assertEqual((await self.receive_file())["reason"], "FILE_TOO_LARGE")
        self.assertEqual(self.ops(), ["probe", "files"])

    async def test_cancel_never_refunds_host_budget(self):
        result = await self.cancel_at("upload_patch", self.send_file)
        self.assertEqual(result["status"], "CANCELLED")
        with patch.object(p, "FILE_TOTAL_MAX", len(PAYLOAD) + p.CHUNK_MAX - 1):
            self.assertEqual((await self.send_file())["reason"], "TRANSFER_LIMIT")
        self.assertEqual(self.ops().count("upload_create"), 1)

    async def test_receive_free_space_exact_and_below_limit(self):
        await self.start()
        self.free.return_value = len(PAYLOAD) + p.FILE_FREE_MIN - 1
        self.assertEqual((await self.receive_file())["reason"], "DISK_FULL")
        self.assertEqual(self.ops(), ["probe", "files"])
        self.free.return_value += 1
        self.assertEqual((await self.receive_file())["status"], "DONE")

    async def test_hostile_range_cannot_expand_listing_or_write(self):
        await self.start()

        def exchange(*args, **kwargs):
            reply = self.real_exchange(*args, **kwargs)
            if args[2] == "get":
                return reply | {"content_range": "bytes 0-3/32"}
            return reply

        self.exchange.side_effect = exchange
        self.assertEqual((await self.receive_file())["reason"], "FILE_CHANGED")
        self.assertEqual(self.ops().count("get"), 1)
        self.assert_clean_receive()

    async def test_hostile_agent_listing_path_is_rejected_on_wire(self):
        await self.start()
        malicious = {"inbox": [], "outbox": [{"name": "../" + PRIVATE, "size": 4}], "truncated": False}
        with patch.object(self.agent, "file_list", return_value=("OK", malicious)):
            reply = await self.call("list")
        self.assertEqual(reply, {"status": "VM_UNREACHABLE"})
        self.assertEqual(self.ops(), ["probe", "files"])

    async def test_foreign_probe_rejected_before_file_request(self):
        await self.start()

        def exchange(*args, **kwargs):
            reply = self.real_exchange(*args, **kwargs)
            if args[2] == "probe":
                return reply | {"vm_name": "different-vm"}
            return reply

        self.exchange.side_effect = exchange
        self.assertEqual((await self.send_file())["reason"], "VM_AGENT_MISMATCH")
        self.assertEqual(self.ops(), ["probe"])

    async def test_hostile_range_digest_and_failed_mark_leave_no_part(self):
        await self.start()

        def exchange(*args, **kwargs):
            reply = self.real_exchange(*args, **kwargs)
            return reply | {"content_digest": p.digest(b"bad")} if args[2] == "get" else reply

        self.exchange.side_effect = exchange
        self.assertEqual((await self.receive_file())["reason"], "DIGEST_MISMATCH")
        self.assert_clean_receive()
        self.exchange.side_effect = None
        self.marker.side_effect = OSError(PRIVATE)
        self.assertEqual((await self.receive_file())["reason"], "MARK_FAILED")
        self.assert_clean_receive()

    async def lost_final(self, disposition):
        await self.start()

        def exchange(*args, **kwargs):
            operation, request = args[2:4]
            if operation == "upload_patch" and request[2]["Upload-Complete"] == "?1":
                if disposition != "open":
                    self.real_exchange(*args, **kwargs)
                raise wire.UncertainTransfer()
            if operation == "upload_status" and disposition == "lost":
                raise p.TransportError()
            return self.real_exchange(*args, **kwargs)

        self.exchange.side_effect = exchange
        reply = await self.send_file()
        self.assertEqual(self.ops()[-2:], ["upload_patch", "upload_status"])
        self.assertEqual(self.ops().count("upload_patch"), 5)
        self.assertEqual(self.ops().count("upload_status"), 1)
        return reply

    async def test_lost_final_ack_status_complete(self):
        self.assertEqual((await self.lost_final("complete"))["status"], "DONE")

    async def test_lost_final_ack_status_open(self):
        reply = await self.lost_final("open")
        self.assertEqual((reply["status"], reply["reason"]), ("FAILED", "UPLOAD_NOT_COMPLETED"))
        self.assertFalse((self.guest / "inbox" / NAME).exists())

    async def test_lost_final_ack_status_also_lost(self):
        reply = await self.lost_final("lost")
        self.assertEqual(reply["status"], "UNKNOWN")
        self.assertEqual((await self.call("status", transfer_id=reply["transfer_id"]))["status"], "UNKNOWN")

    async def test_cancel_after_pending_final_status_keeps_verified_completion(self):
        await self.start()
        reached, release = Event(), Event()
        self.addCleanup(release.set)

        def exchange(*args, **kwargs):
            op, request = args[2:4]
            reply = self.real_exchange(*args, **kwargs)
            if op == "upload_patch" and request[2]["Upload-Complete"] == "?1":
                raise wire.UncertainTransfer()
            if op == "upload_status":
                reached.set()
                release.wait(3)
            return reply

        self.exchange.side_effect = exchange
        result = await self.send_file(wait=0)
        await self.wait_event(reached)
        cancellation = asyncio.create_task(self.call("cancel", transfer_id=result["transfer_id"]))
        await asyncio.sleep(0.04)
        release.set()
        self.assertEqual((await cancellation)["status"], "DONE")
        self.assertEqual(self.ops()[-2:], ["upload_patch", "upload_status"])

    async def test_shutdown_uses_host_close_and_cleans_local_part(self):
        await self.start()
        reached, release = self.gate("get", after=True)
        one = await self.receive_file(wait=0)
        await self.wait_event(reached)
        closed = asyncio.create_task(asyncio.to_thread(self.host.close))
        await asyncio.sleep(0.04)
        release.set()
        await asyncio.wait_for(closed, 3)
        self.assertEqual((await self.call("status", transfer_id=one["transfer_id"]))["status"], "CANCELLED")
        self.assert_clean_receive()
        self.assertEqual(self.ops(), ["probe", "files", "get"])

    async def test_shutdown_leaves_a_stuck_worker_as_daemon(self):
        await self.start()
        self.assertEqual(vm_transfers.CLOSE_SECONDS, 3)
        reached, release = self.gate("get", after=True)
        with patch.object(vm_transfers, "Thread", wraps=Thread) as thread:
            one = await self.receive_file(wait=0)
        await self.wait_event(reached)
        self.assertTrue(thread.call_args.kwargs["daemon"])
        with patch.object(vm_transfers, "CLOSE_SECONDS", 0.05):
            await asyncio.wait_for(asyncio.to_thread(self.host.close), 1)
        self.assertEqual((await self.call("status", transfer_id=one["transfer_id"], wait_seconds=0))["status"],
                         "TRANSFERRING")
        release.set()
        self.assertEqual((await self.call("status", transfer_id=one["transfer_id"]))["status"], "CANCELLED")
        self.assert_clean_receive()

    async def test_private_failures_and_payload_do_not_escape(self):
        await self.start()
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            first = await self.call("list")
            self.connector.side_effect = RuntimeError(PRIVATE)
            second = await self.send_file()
        body = json.dumps([first, second, output.getvalue(), self.output])
        for private in (PRIVATE, str(self.root), self.config.address, KEY.hex(), PAYLOAD.decode()):
            self.assertNotIn(private, body)
        self.assertEqual(second["reason"], "VM_UNREACHABLE")
