import asyncio
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from threading import Event
from time import monotonic
import unittest
from unittest.mock import MagicMock, Mock, patch

from mcp.server.fastmcp.exceptions import ToolError

from tests.test_vm_link_20261125 import PRIVATE, VmFixture
from tests.test_vm_screen_protocol_20261125 import KEY
from sora_commander_next import vm_agent as agent, vm_client as client, vm_control as control
from sora_commander_next import vm_link as link, vm_protocol as protocol, vm_win as win


KEYPRESS = {"type": "keypress", "keys": ["Enter"]}
TYPE = {"type": "type", "text": PRIVATE}
CLICK = {"type": "click", "x": 87, "y": 123, "button": "middle"}
WAIT = {"type": "wait", "ms": 1}
TOOLS = ("screenshot", "wait", "keypress", "type", "click")


class VmActTests(VmFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.agent = self.make_agent(tools=",".join(TOOLS))
        self.server = self.make_server(tools=TOOLS)

    def tearDown(self):
        self.sandbox_socket.assert_not_called()

    async def ready(self):
        await self.start()
        metadata, images = await self.look()
        self.assert_image(images)
        self.routes.clear()
        return metadata["screen_id"]

    async def act(self, screen_id, *actions):
        content = await self.server.call_tool("vm_act", {"screen_id": screen_id, "actions": list(actions)})
        return json.loads(content[0].text), content[1:]

    def assert_results(self, answer, status, *results, picture=False):
        metadata, images = answer
        self.assertEqual(metadata["status"], status)
        self.assertEqual(metadata["results"], list(results))
        self.assertEqual(metadata["notice"], link.DATA_NOTICE)
        self.assertIn("remaining_seconds", metadata)
        self.assertIn("inputs_left", metadata)
        if picture:
            self.assert_image(images)
        else:
            self.assertEqual(images, [])
            self.assertNotIn("screen_id", metadata)
        return metadata

    async def test_batch_wait_coordinate_then_text_uses_fresh_keys_and_capture(self):
        screen = await self.ready()
        original_tag = self.agent.screen[0]
        calls = []
        original = self.agent.action

        def action(op_id, boot, tag, raw, stream):
            calls.append((op_id, boot, tag))
            return original(op_id, boot, tag, raw, stream)

        with patch.object(self.agent, "action", side_effect=action), redirect_stderr(StringIO()) as err, \
                redirect_stdout(StringIO()) as out:
            answer = await self.act(screen, WAIT, CLICK, TYPE, KEYPRESS)
        metadata = self.assert_results(answer, "OK", "OK", "SENT", "SENT", "SENT", picture=True)
        self.assertNotEqual(metadata["screen_id"], screen)
        self.assertEqual(metadata["inputs_left"], 0)
        self.assertEqual(self.routes, ["/probe", *(["/action"] * 4), "/screenshot"])
        self.assertEqual(len(set(item[0] for item in calls)), 4)
        self.assertEqual([item[2] for item in calls], [None, original_tag, None, None])
        self.assertTrue(all(item[1] == self.agent.boot_id for item in calls))
        self.assertEqual(self.backend.insert.call_count, 3)
        transcript = str(metadata) + str(self.output) + err.getvalue() + out.getvalue()
        for value in (PRIVATE, self.config.address, self.config.bind_local, KEY.hex(), original_tag,
                      self.agent.boot_id, *(item[0] for item in calls)):
            self.assertNotIn(value, transcript)

    async def test_wire_rejects_entire_bad_batch_before_work(self):
        screen = await self.ready()
        good = {"screen_id": screen, "actions": [KEYPRESS]}
        bad = [None, {"actions": [KEYPRESS]}, {"screen_id": screen}, good | {"unknown": True},
               good | {"screen_id": True}, good | {"screen_id": "x" * 129}]
        for args in bad:
            with self.assertRaisesRegex(ToolError, "^Invalid tool request$"):
                await self.server.call_tool("vm_act", args)
        bad = [good | {"actions": value} for value in ([], (KEYPRESS,), [KEYPRESS] * 10000, "click", None)]
        for invalid in (None, {"type": "screenshot"}, {"type": "x" * 15000},
                        CLICK | {"x": True}, CLICK | {"button": {}}, CLICK | {"private": PRIVATE},
                        {"type": "drag", "path": [{"x": 1, "y": 2}, {"x": {}, "y": 2}]},
                        {"type": "drag", "path": [{"x": 1, "y": 2, "z": 3}] * 2},
                        {"type": "drag", "path": [{"x": 1, "y": 2}] * 10000},
                        {"type": "wait", "ms": True}, {"type": "wait", "ms": 1.0},
                        {"type": "keypress", "keys": [["Enter"]]}, TYPE | {"text": "x" * 1001}):
            bad.append(good | {"actions": [WAIT, invalid]})
        for args in bad + [good | {"actions": [KEYPRESS, CLICK]}, good | {"actions": [CLICK, CLICK]},
                           good | {"actions": [{"type": "wait", "ms": 10000}] * 2 + [WAIT]}]:
            content = await self.server.call_tool("vm_act", args)
            self.assertEqual(len(content), 1)
            answer = json.loads(content[0].text)
            self.assertEqual((answer["status"], set(answer)),
                             ("INVALID_ACTIONS", {"status", "reason", "index", "expected", "notice"}))
            self.assertNotIn(PRIVATE, content[0].text)
            self.assertLess(len(content[0].text), 400)
        self.assertEqual(self.routes, [])
        self.backend.insert.assert_not_called()
        self.assert_results(await self.act(screen, KEYPRESS), "OK", "SENT", picture=True)

    async def test_invalid_actions_say_what_to_correct(self):
        screen = await self.ready()
        cases = (
            ([], "ACTIONS_MUST_BE_A_LIST_OF_1_TO_10", None, None),
            ([{"type": "doubleClick", "x": 1, "y": 1}], "UNKNOWN_TYPE", 0,
             "click double_click move drag scroll type keypress wait"),
            ([WAIT, {"type": "click", "x": 1}], "WRONG_FIELDS", 1, "type x y button"),
            ([{"type": "click", "x": 1, "y": 1, "keys": []}], "WRONG_FIELDS", 0, "type x y button"),
            ([{"type": "drag"}], "WRONG_FIELDS", 0, "type path duration_ms"),
            ([{"type": "keypress", "keys": "Win"}], "BAD_VALUE", 0, "type keys"),
            ([{"type": "keypress", "keys": ["Win", "L"]}], "KEYS_NOT_ALLOWED", 0, "type keys"),
            ([{"type": "scroll", "x": 1, "y": 1}], "BAD_VALUE", 0, "type x y scroll_x scroll_y"),
            ([KEYPRESS, CLICK], "ONLY_THE_FIRST_INPUT_MAY_USE_COORDINATES", 1, None),
            ([{"type": "wait", "ms": 10000}] * 2 + [WAIT], "WAIT_TOTAL_OVER_20000_MS", 2, None),
        )
        for actions, reason, index, expected in cases:
            with self.subTest(reason=reason, actions=actions):
                content = await self.server.call_tool("vm_act", {"screen_id": screen, "actions": actions})
                answer = json.loads(content[0].text)
                self.assertEqual((answer["reason"], answer["index"], answer["expected"]), (reason, index, expected))
        self.assertEqual(self.routes, [])

    async def test_refusals_and_status_show_the_same_smaller_limits(self):
        self.agent = self.make_agent(tools=",".join(TOOLS), max_input_ops=4)
        screen = await self.ready()
        metadata = self.assert_results(await self.act(screen, KEYPRESS), "OK", "SENT", picture=True)
        self.assertEqual(metadata["inputs_left"], 3)
        stale = self.assert_results(await self.act(screen, KEYPRESS), "STALE_SCREEN", "NOT_EXECUTED")
        status = await self.session()
        self.assertEqual((stale["inputs_left"], status["inputs_left"]), (3, 3))
        for answer in (stale, status):
            self.assertTrue(0 < answer["remaining_seconds"] <= 60)
        self.assertEqual(self.server_lease._lease.inputs_left, 6)
        self.stop()
        ended = self.assert_results(await self.act(screen, KEYPRESS), "VM_SESSION_ENDED", "NOT_EXECUTED")
        self.assertEqual((ended["inputs_left"], ended["remaining_seconds"]), (3, 0))

    async def test_documented_defaults_are_filled(self):
        screen = await self.ready()
        given = {"type": "click", "x": 3, "y": 4}
        metadata = self.assert_results(await self.act(screen, given), "OK", "SENT", picture=True)
        self.assertEqual(self.backend.prepare_input.call_args.args[0], given | {"button": "left"})
        self.assertEqual(given, {"type": "click", "x": 3, "y": 4})
        self.assertEqual(link.prepare_actions([{"type": "scroll", "x": 1, "y": 2, "scroll_y": 300}])[0],
                         [{"type": "scroll", "x": 1, "y": 2, "scroll_x": 0, "scroll_y": 300}])
        self.assertIn("screen_id", metadata)

    async def test_the_picture_waits_for_the_screen_only_after_input(self):
        screen = await self.ready()
        with patch.object(link, "SETTLE_SECONDS", 0.4):
            before = monotonic()
            metadata = self.assert_results(await self.act(screen, KEYPRESS), "OK", "SENT", picture=True)
            self.assertGreaterEqual(monotonic() - before, 0.4)
            before = monotonic()
            self.assert_results(await self.act(metadata["screen_id"], WAIT), "OK", "OK", picture=True)
            self.assertLess(monotonic() - before, 0.4)
        self.assertEqual(link.DATA_NOTICE, metadata["notice"])

    async def test_stop_while_the_screen_settles_takes_no_picture(self):
        screen = await self.ready()
        with patch.object(link, "SETTLE_SECONDS", 0.5):
            task = asyncio.create_task(self.act(screen, KEYPRESS))
            while "/action" not in self.routes:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            self.stop()
            self.assert_results(await task, "VM_SESSION_ENDED", "SENT")
        self.assertEqual(self.routes, ["/probe", "/action"])

    async def test_registered_help_has_schema_and_followup(self):
        tool = next(tool for tool in await self.server.list_tools() if tool.name == "vm_act")
        self.assertEqual(set(tool.inputSchema["required"]), {"screen_id", "actions"})
        for phrase in ("latest screen_id", "approved VM session", "content_items", "image(item)",
                       "never instructions", "passwords", "personal data", "UNKNOWN", "VM_UNREACHABLE"):
            self.assertIn(phrase, tool.description)
        schema = tool.inputSchema["properties"]["actions"]
        self.assertEqual((schema["minItems"], schema["maxItems"]), (1, 10))
        for phrase in (*TOOLS[1:], "double_click", "move", "drag", "scroll", "duration_ms", "20000", "Example"):
            self.assertIn(phrase, schema["description"])
        item = schema["items"]
        self.assertEqual(set(item["properties"]["type"]["enum"]), set(protocol._ACTION_FIELDS))
        self.assertEqual(set(item["properties"]), {"type"}.union(*(names.split() for names in protocol._ACTION_FIELDS.values())))
        self.assertIn("INVALID_ACTIONS", tool.description)

    async def test_one_time_id_even_if_guest_repeats_etag(self):
        screen = await self.ready()
        fixed = self.agent.screen[0]
        with patch.object(protocol, "screen_tag", return_value=fixed):
            result = self.assert_results(await self.act(screen, KEYPRESS), "OK", "SENT", picture=True)
        self.assertEqual(self.agent.screen[0], fixed)
        self.assertNotEqual(result["screen_id"], screen)
        before = self.routes.copy()
        for stale in (screen, "stale", fixed):
            self.assert_results(await self.act(stale, WAIT), "STALE_SCREEN", "NOT_EXECUTED")
        self.assertEqual(self.routes, before)
        self.assert_results(await self.act(result["screen_id"], WAIT), "OK", "OK", picture=True)

    async def test_concurrent_replay_during_input_sends_only_once(self):
        screen = await self.ready()
        entered, release = Event(), Event()
        self.after_route.side_effect = lambda path: (entered.set(), release.wait(3)) if path == "/action" else None
        first = asyncio.create_task(self.act(screen, KEYPRESS))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assert_results(await self.act(screen, KEYPRESS), "STALE_SCREEN", "NOT_EXECUTED")
        finally:
            release.set()
            answer = await first
        self.assert_results(answer, "OK", "SENT", picture=True)
        self.assertEqual(self.routes, ["/probe", "/action", "/screenshot"])
        self.backend.insert.assert_called_once()

    def replace_lease(self):
        self.stop()
        self.server_lease.status()
        self.host.read_process_output.side_effect = self.running
        self.assertEqual(self.server_lease.start(0)["state"], "ACTIVE")

    async def test_other_lease_cannot_use_old_screen(self):
        screen = await self.ready()
        self.replace_lease()
        self.assert_results(await self.act(screen, TYPE), "STALE_SCREEN", "NOT_EXECUTED")
        self.assertEqual(self.routes, [])

    async def test_stop_and_lock_before_probe_and_before_first_post(self):
        for boundary in ("before", "/probe"):
            for lock in (False, True):
                with self.subTest(boundary=boundary, lock=lock):
                    self.host.read_process_output.side_effect = self.running
                    self.guard.verify.return_value = True
                    if self.server_lease.status()["state"] == "ACTIVE":
                        self.stop()
                        self.server_lease.status()
                        self.host.read_process_output.side_effect = self.running
                    screen = await self.ready()
                    status = "OWNER_SESSION_UNAVAILABLE" if lock else "VM_SESSION_ENDED"
                    interrupt = (lambda: setattr(self.guard.verify, "return_value", False)) if lock else self.stop
                    if boundary == "before":
                        interrupt()
                    else:
                        self.after_route.side_effect = lambda path: interrupt() if path == boundary else None
                    self.assert_results(await self.act(screen, KEYPRESS), status, "NOT_EXECUTED")
                    self.assertEqual(self.routes, [] if boundary == "before" else ["/probe"])
                    self.after_route.side_effect = None
        self.backend.insert.assert_not_called()

    async def check_interrupt(self, boundary, interrupt, status, with_tail):
        screen = await self.ready()
        self.after_route.side_effect = lambda path: interrupt() if path == boundary else None
        actions = [KEYPRESS, WAIT] if with_tail else [KEYPRESS]
        results = ["SENT", "NOT_EXECUTED"] if with_tail else ["SENT"]
        metadata = self.assert_results(await self.act(screen, *actions), status, *results)
        self.backend.insert.assert_called_once()
        self.assertEqual(self.routes, ["/probe", "/action"] + (["/screenshot"] if boundary == "/screenshot" else []))
        return metadata

    async def test_stop_between_actions(self):
        await self.check_interrupt("/action", self.stop, "VM_SESSION_ENDED", True)

    async def test_lock_between_actions(self):
        await self.check_interrupt("/action", lambda: setattr(self.guard.verify, "return_value", False),
                                   "OWNER_SESSION_UNAVAILABLE", True)

    async def test_stop_during_final_capture(self):
        await self.check_interrupt("/screenshot", self.stop, "VM_SESSION_ENDED", False)

    async def test_lock_during_final_capture(self):
        await self.check_interrupt("/screenshot", lambda: setattr(self.guard.verify, "return_value", False),
                                   "OWNER_SESSION_UNAVAILABLE", False)

    async def test_stop_after_the_last_action_takes_no_picture(self):
        await self.check_interrupt("/action", self.stop, "VM_SESSION_ENDED", False)

    async def interrupt_connect(self, number, interrupt):
        connect = self.connector.side_effect
        start = self.connector.call_count

        def hooked(*addresses):
            if self.connector.call_count - start == number:
                interrupt()
            return connect(*addresses)

        self.connector.side_effect = hooked

    async def test_stop_or_lock_while_connecting_sends_nothing_and_charges_nothing(self):
        for lock in (False, True):
            with self.subTest(lock=lock):
                screen = await self.ready()
                interrupt = (lambda: setattr(self.guard.verify, "return_value", False)) if lock else self.stop
                connect = self.connector.side_effect
                await self.interrupt_connect(2, interrupt)
                self.assert_results(await self.act(screen, KEYPRESS, WAIT),
                                    "OWNER_SESSION_UNAVAILABLE" if lock else "VM_SESSION_ENDED",
                                    "NOT_EXECUTED", "NOT_EXECUTED")
                self.assertEqual(self.routes, ["/probe"])
                self.assertEqual(self.server_lease._lease.inputs_left, 7)
                self.connector.side_effect = connect
                if not lock:
                    self.server_lease.status()
                    self.host.read_process_output.side_effect = self.running
        self.backend.insert.assert_not_called()

    async def test_cancel_while_connecting_sends_nothing(self):
        screen = await self.ready()
        seen = []
        original = self.server_link.act

        def act(screen_id, actions, count, progress):
            seen.append(progress)
            return original(screen_id, actions, count, progress)

        await self.interrupt_connect(2, lambda: seen[0].cancelled.set())
        with patch.object(self.server_link, "act", side_effect=act):
            metadata, images = await self.act(screen, KEYPRESS, WAIT)
        self.assertEqual((metadata["results"], images), (["NOT_EXECUTED", "NOT_EXECUTED"], []))
        self.assertEqual(self.routes, ["/probe"])
        self.backend.insert.assert_not_called()

    async def test_a_failed_look_leaves_no_usable_screen(self):
        screen = await self.ready()
        with patch.object(self.agent, "capture", return_value=("CAPTURE_FAILED",)):
            self.assertEqual((await self.look())[0], {"status": "CAPTURE_FAILED"})
        self.assert_results(await self.act(screen, KEYPRESS), "STALE_SCREEN", "NOT_EXECUTED")
        self.backend.insert.assert_not_called()

    async def test_replacement_between_actions_preserves_result_and_new_budget(self):
        metadata = await self.check_interrupt("/action", self.replace_lease, "VM_SESSION_ENDED", True)
        self.assertEqual(metadata["reason"], "SESSION_REPLACED")
        self.assertEqual(self.server_lease.status()["inputs_left"], 7)

    async def test_budget_charge_is_bound_even_if_lease_changes_after_gate_check(self):
        screen = await self.ready()
        original = self.server_lease.consume_inputs

        def swap(*args):
            self.replace_lease()
            return original(*args)

        with patch.object(self.server_lease, "consume_inputs", side_effect=swap):
            metadata = self.assert_results(await self.act(screen, KEYPRESS), "VM_SESSION_ENDED", "NOT_EXECUTED")
        self.assertEqual(metadata["reason"], "SESSION_REPLACED")
        self.assertEqual(self.server_lease.status()["inputs_left"], 7)
        self.assertEqual(self.routes, ["/probe"])
        self.backend.insert.assert_not_called()

    async def test_config_and_probe_each_can_refuse_a_whole_batch(self):
        for host_has_type in (True, False):
            with self.subTest(host_has_type=host_has_type):
                lesser = tuple(tool for tool in TOOLS if tool != "type")
                self.agent = self.make_agent(tools=",".join(lesser if host_has_type else TOOLS))
                self.server = self.make_server(tools=TOOLS if host_has_type else lesser)
                screen = await self.ready()
                self.assert_results(await self.act(screen, KEYPRESS, TYPE), "TOOL_DISABLED",
                                    "NOT_EXECUTED", "NOT_EXECUTED")
                self.assert_results(await self.act(screen, KEYPRESS), "STALE_SCREEN", "NOT_EXECUTED")
                self.assertEqual(self.routes, ["/probe"])
                self.assertEqual(self.server_lease.status()["inputs_left"], 7)
        self.backend.insert.assert_not_called()

    async def test_host_preflights_total_then_charges_and_ends_each_last_input(self):
        self.server = self.make_server(tools=TOOLS, max_inputs=1)
        screen = await self.ready()
        self.assert_results(await self.act(screen, KEYPRESS, TYPE), "INPUT_LIMIT", "NOT_EXECUTED", "NOT_EXECUTED")
        self.assertEqual(self.routes, ["/probe"])
        self.assertEqual(self.server_lease.status()["inputs_left"], 1)
        screen = (await self.look())[0]["screen_id"]
        self.routes.clear()
        at_insert = []
        self.backend.insert.side_effect = lambda *_: at_insert.append(self.server_lease._lease.inputs_left) or "SENT"
        metadata = self.assert_results(await self.act(screen, KEYPRESS, WAIT), "VM_SESSION_ENDED",
                                       "SENT", "NOT_EXECUTED")
        self.assertEqual(at_insert, [0])
        self.assertEqual((metadata["reason"], metadata["inputs_left"]), ("INPUT_LIMIT", 0))
        self.assertEqual(self.routes, ["/probe", "/action"])

    async def test_agent_smaller_budget_is_checked_before_the_first_input(self):
        self.agent = self.make_agent(tools=",".join(TOOLS), max_input_ops=1)
        screen = await self.ready()
        metadata = self.assert_results(await self.act(screen, KEYPRESS, TYPE, WAIT), "INPUT_LIMIT",
                                       "NOT_EXECUTED", "NOT_EXECUTED", "NOT_EXECUTED")
        self.assertEqual(metadata["inputs_left"], 1)
        self.assertEqual(self.server_lease.status()["inputs_left"], 7)
        self.assertEqual(self.routes, ["/probe"])
        self.backend.insert.assert_not_called()
        screen = (await self.look())[0]["screen_id"]
        self.assert_results(await self.act(screen, KEYPRESS, WAIT), "OK", "SENT", "OK", picture=True)

    async def test_agent_limit_mid_batch_stops_it_and_host_does_not_refund(self):
        screen = await self.ready()
        def drain(path):
            if path == "/action":
                self.agent.input_ops = 0

        self.after_route.side_effect = drain
        self.assert_results(await self.act(screen, KEYPRESS, TYPE, WAIT), "LIMIT_REACHED",
                            "SENT", "LIMIT_REACHED", "NOT_EXECUTED")
        self.assertEqual(self.server_lease.status()["inputs_left"], 5)
        self.backend.insert.assert_called_once()

    async def test_agent_stale_stops_batch_charges_host_and_discards_id(self):
        screen = await self.ready()
        self.after_route.side_effect = lambda path: setattr(self.agent, "screen", None) if path == "/probe" else None
        metadata = self.assert_results(await self.act(screen, CLICK, TYPE), "STALE_SCREEN",
                                       "STALE_SCREEN", "NOT_EXECUTED", picture=True)
        self.assertNotIn("screen_id", metadata)
        self.assert_results(await self.act(screen, WAIT), "STALE_SCREEN", "NOT_EXECUTED")
        self.assertEqual(self.server_lease.status()["inputs_left"], 6)
        self.assertEqual(self.routes, ["/probe", "/action", "/screenshot"])
        self.backend.insert.assert_not_called()

    async def test_lost_response_after_send_is_unknown_and_never_replayed(self):
        screen = await self.ready()

        def drop_answer(path):
            if path == "/action":
                raise protocol.TransportError()

        self.after_route.side_effect = drop_answer
        result = self.assert_results(await self.act(screen, TYPE, KEYPRESS), "UNKNOWN", "UNKNOWN", "NOT_EXECUTED",
                                     picture=True)
        self.assertNotIn("screen_id", result)
        self.assert_results(await self.act(screen, TYPE), "STALE_SCREEN", "NOT_EXECUTED")
        self.assertEqual(self.server_lease.status()["inputs_left"], 6)
        self.assertEqual(self.routes, ["/probe", "/action", "/screenshot"])
        self.backend.insert.assert_called_once()

    async def test_final_capture_failure_preserves_success_and_fixed_diagnostic(self):
        screen = await self.ready()
        self.backend.capture.side_effect = OSError(PRIVATE)
        metadata = self.assert_results(await self.act(screen, KEYPRESS, TYPE), "CAPTURE_FAILED", "SENT", "SENT")
        self.assertNotIn(PRIVATE, str(metadata) + str(self.output))
        self.assert_results(await self.act(screen, WAIT), "STALE_SCREEN", "NOT_EXECUTED")
        self.assertEqual(self.routes, ["/probe", "/action", "/action", "/screenshot"])

    async def test_wrong_vm_version_and_restart_stop_before_sending(self):
        for field, value, status in (("vm_name", "Other VM", "VM_AGENT_MISMATCH"),
                                     ("version", 3, "VM_AGENT_MISMATCH"),
                                     ("boot_id", "f" * 32, "VM_AGENT_RESTARTED")):
            with self.subTest(field=field):
                if self.server_lease.status()["state"] == "ACTIVE":
                    self.replace_lease()
                    screen = (await self.look())[0]["screen_id"]
                    self.routes.clear()
                else:
                    screen = await self.ready()
                found, fields = self.agent.probe()
                with patch.object(self.agent, "probe", return_value=(found, fields | {field: value})):
                    self.assert_results(await self.act(screen, KEYPRESS), status, "NOT_EXECUTED")
                self.assert_results(await self.act(screen, WAIT), "STALE_SCREEN", "NOT_EXECUTED")
                self.assertEqual(self.routes, ["/probe"])
        self.backend.insert.assert_not_called()

    async def test_reboot_between_probe_and_post_refuses_input(self):
        screen = await self.ready()
        self.after_route.side_effect = lambda path: setattr(self, "agent", self.make_agent(tools=",".join(TOOLS))) \
            if path == "/probe" else None
        self.assert_results(await self.act(screen, TYPE, WAIT), "VM_AGENT_RESTARTED", "VM_AGENT_RESTARTED", "NOT_EXECUTED")
        self.backend.insert.assert_not_called()

    async def test_deadline_returns_completed_and_unknown_before_worker_exits(self):
        await self.interrupt_worker(cancel=False)

    async def test_cancellation_prevents_more_sends_after_caller_exits(self):
        await self.interrupt_worker(cancel=True)

    async def test_deadline_during_capture_keeps_results_and_has_no_in_flight_action(self):
        screen = await self.ready()
        self.assertEqual(control.ACT_SECONDS, 60)
        entered, release, finished = Event(), Event(), Event()
        original = self.server_link.act

        def act(*args):
            try:
                return original(*args)
            finally:
                finished.set()

        self.after_route.side_effect = lambda path: (entered.set(), release.wait(3)) if path == "/screenshot" else None
        with patch.object(self.server_link, "act", side_effect=act), patch.object(control, "ACT_SECONDS", 0.5):
            task = asyncio.create_task(self.act(screen, KEYPRESS))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                self.assert_results(await task, "VM_ACT_TIMED_OUT", "SENT")
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        self.assertEqual(self.routes, ["/probe", "/action", "/screenshot"])
        self.assert_results(await self.act(screen, WAIT), "STALE_SCREEN", "NOT_EXECUTED")

    async def interrupt_worker(self, *, cancel):
        screen = await self.ready()
        entered, release, finished = Event(), Event(), Event()
        original = self.server_link.act

        def act(*args):
            try:
                return original(*args)
            finally:
                finished.set()

        def after_route(path):
            if path == "/action" and self.routes.count(path) == 2:
                entered.set()
                release.wait(3)

        self.after_route.side_effect = after_route
        timeout = 2.0 if cancel else 0.5
        with patch.object(self.server_link, "act", side_effect=act), patch.object(control, "ACT_SECONDS", timeout):
            task = asyncio.create_task(self.act(screen, KEYPRESS, TYPE, KEYPRESS))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    self.assert_results(await task, "VM_ACT_TIMED_OUT", "SENT", "UNKNOWN", "NOT_EXECUTED")
                self.assertEqual(self.backend.insert.call_count, 2)
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        self.assertEqual(self.backend.insert.call_count, 2)
        self.assertEqual(self.routes, ["/probe", "/action", "/action"])
        self.assert_results(await self.act(screen, WAIT), "STALE_SCREEN", "NOT_EXECUTED")


class GuestInputTests(unittest.TestCase):
    def test_all_supported_keys_and_chords_have_scan_codes_and_release_in_reverse_order(self):
        from itertools import combinations

        for key in protocol.KEYS:
            modifiers = tuple(modifier for modifier in protocol.MODIFIERS if modifier != key)
            for count in range(min(3, len(modifiers)) + 1):
                for group in combinations(modifiers, count):
                    keys = [key, *reversed(group)]
                    if {"Win", "L"} <= set(keys) or {"Ctrl", "Alt", "Delete"} <= set(keys):
                        with self.assertRaises(protocol.ProtocolError):
                            protocol.key_combo(keys)
                        continue
                    events = win.prepare_input({"type": "keypress", "keys": keys}, (640, 480))
                    self.assertEqual(len(events), 2 * len(keys))
                    pressed = events[:len(keys)]
                    released = events[len(keys):]
                    self.assertEqual([(e.key.wVk, e.key.wScan, e.key.dwFlags | win.KEY_UP) for e in pressed],
                                     [(e.key.wVk, e.key.wScan, e.key.dwFlags) for e in reversed(released)])

    def test_letters_and_digits_use_the_published_set_1_scan_codes(self):
        # Written out on purpose: the production table is built from keyboard rows.
        expected = dict(A=0x1E, B=0x30, C=0x2E, D=0x20, E=0x12, F=0x21, G=0x22, H=0x23, I=0x17, J=0x24, K=0x25,
                        L=0x26, M=0x32, N=0x31, O=0x18, P=0x19, Q=0x10, R=0x13, S=0x1F, T=0x14, U=0x16, V=0x2F,
                        W=0x11, X=0x2D, Y=0x15, Z=0x2C)
        expected.update(zip("1234567890", range(0x02, 0x0C)))
        self.assertEqual({name: win.SCAN_CODES[name] for name in expected}, expected)

    def test_partial_chord_and_middle_click_release_the_pressed_controls(self):
        device = object.__new__(win.WindowsBackend)
        device.desktop_is_default = Mock(return_value=True)
        for action, accepted, expected in (({"type": "keypress", "keys": ["win", "b", "control", "shift"]},
                                             3, [0x5B, 0x2A, 0x1D]), (CLICK, 2, [win.MOUSE_MIDDLE_UP])):
            events = win.prepare_input(action, (640, 480))
            device._send_batch = Mock(side_effect=[accepted, len(expected)])
            self.assertEqual(device.insert(events), "UNKNOWN")
            released = device._send_batch.call_args.args[0]
            self.assertEqual([e.key.wScan if e.type == 1 else e.mouse.dwFlags for e in released], expected)
            self.assertTrue(all(not e.type or e.key.dwFlags & win.KEY_UP for e in released))

    def test_text_newline_tab_order_and_byte_limits(self):
        value = "x\n\t🐈y"
        protocol.validate_text(value)
        events = win.prepare_input({"type": "type", "text": value}, (640, 480))
        expected = [(ord("x"), win.KEY_UNICODE), (0x1C, win.KEY_SCANCODE), (0x0F, win.KEY_SCANCODE),
                    (0xD83D, win.KEY_UNICODE), (0xDC08, win.KEY_UNICODE), (ord("y"), win.KEY_UNICODE)]
        self.assertEqual([(e.key.wScan, e.key.dwFlags) for e in events],
                         [(scan, flag | up) for scan, flag in expected for up in (0, win.KEY_UP)])
        text = "🐈" * 1000
        self.assertLessEqual(len(text.encode("utf-8")), client.TEXT_FILE_MAX)
        self.assertLessEqual(len(protocol.encode({"type": "type", "text": text})), protocol.REQUEST_MAX)
        self.assertEqual(client._decode_text(text.encode("utf-8")), text)
        for control_char in ("\r", "\0", "\x1b", "\x7f", "\u0085", "\u2028"):
            with self.assertRaises(protocol.ProtocolError):
                protocol.validate_text("x" + control_char)

    def test_console_only_changes_input_quick_edit_and_minimizes_its_window(self):
        device = object.__new__(win.WindowsBackend)
        device.GetStdHandle = Mock(return_value=34)

        def mode(_handle, flags):
            flags._obj.value = 0x47
            return True

        device.GetConsoleMode = Mock(side_effect=mode)
        device.SetConsoleMode = Mock(return_value=True)
        device.GetConsoleWindow = Mock(return_value=92)
        device.ShowWindowAsync = Mock(return_value=True)
        self.assertTrue(device.disable_quick_edit())
        device.GetStdHandle.assert_called_once_with(0xFFFFFFF6)
        device.SetConsoleMode.assert_called_once_with(34, 0x87)
        self.assertTrue(device.minimize_console())
        device.ShowWindowAsync.assert_called_once_with(92, 6)
        device.GetConsoleWindow.return_value = None
        self.assertFalse(device.minimize_console())
        device.GetStdHandle.return_value = win.INVALID_HANDLE
        self.assertFalse(device.disable_quick_edit())
        self.assertEqual(device.SetConsoleMode.call_count, 1)

    def test_console_notices_keep_agent_running_and_minimize_follows_start(self):
        from tests.test_vm_screen_agent_20261125 import arguments

        args = arguments()
        output = []
        device = Mock(disable_quick_edit=Mock(return_value=False))

        def minimize():
            self.assertIn(" START ", output[-1])
            raise OSError(PRIVATE)

        device.minimize_console.side_effect = minimize
        with patch.object(agent, "startup", return_value=(args.bind, args.allow_peer, KEY, device, (640, 480))), \
                patch.object(agent, "serve") as serve:
            self.assertEqual(agent.run(args, listener_factory=MagicMock(), output=output.append), 0)
        serve.assert_called_once()
        self.assertIn("NOTICE QUICK_EDIT_UNAVAILABLE", output[0])
        self.assertIn(" START ", output[1])
        self.assertIn("NOTICE CONSOLE_MINIMIZE_UNAVAILABLE", output[2])
        for value in (args.bind, args.allow_peer, KEY.hex(), PRIVATE):
            self.assertNotIn(value, "\n".join(output))
