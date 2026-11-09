"""Localized file window. Fake Tk and fake processes: no window, no file, no write."""
import ast
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from tests._no_real_dialog import NoRealDialogTestCase
from sora_commander_next import owner_file_dialog_localized as child
from sora_commander_next import owner_file_review_gate_dev_20261105 as gate
from sora_commander_next import owner_file_review_localized_dev_20261104 as review
from sora_commander_next import release_inventory as inventory

SOURCE = ROOT / "src" / "sora_commander_next"
OLD = "a" * 64
PATH = r"D:\repo\docs\メモ.md"
DIFF = "--- current\n+++ proposed\n@@ -1 +1 @@\n-古い行\n+新しい行\n"
FULL = "新しい行\n二行目\n"
NEW = sha256(FULL.encode()).hexdigest()
OPTIONS = dict(review_id="r-1", seconds=45.0, locale="ja")


def patch_proposal(**updates):
    return {"operation": "request_source_patch_dev", "path": PATH, "old_sha256": OLD, "new_sha256": NEW,
            "diff": DIFF, "full_proposed_content": FULL, **updates}


def create_proposal(content=FULL, **updates):
    data = content.encode()
    return {"operation": "create_file", "path": PATH, "bytes": len(data), "sha256": sha256(data).hexdigest(),
            "full_proposed_content": content, **updates}


DIRECTORY = {"operation": "create_directory", "path": r"D:\repo\新しいフォルダ"}
MOVE = {"operation": "move_file", "source": r"D:\repo\a.txt", "destination": r"D:\repo\b.txt"}


def body(proposal):
    return review.bound_proposal(proposal, **OPTIONS).presentation.screen.body


class DisplayTests(NoRealDialogTestCase):
    def test_a_patch_shows_readable_japanese_on_real_lines(self):
        text = body(patch_proposal())
        lines = text.split("\n")
        for expected in ("-古い行", "+新しい行", "@@ -1 +1 @@"):
            self.assertIn(expected, lines)
        for expected in (PATH, OLD, NEW, "'r-1'", "45"):
            self.assertIn(expected, text)
        self.assertNotIn("\\u", text)

    def test_a_patch_does_not_repeat_the_whole_file(self):
        self.assertNotIn("二行目", body(patch_proposal()))

    def test_a_patch_is_refused_unless_it_matches(self):
        for bad in (patch_proposal(full_proposed_content="別の内容\n"), patch_proposal(new_sha256="b" * 64),
                    patch_proposal(old_sha256="short"), patch_proposal(old_sha256=NEW), patch_proposal(path=""),
                    patch_proposal(diff=""), patch_proposal(full_proposed_content=None),
                    {**patch_proposal(), "extra": "x"}):
            with self.subTest(bad=str(bad)[:60]), self.assertRaises(ValueError):
                review.bound_proposal(bad, **OPTIONS)

    def test_a_new_file_shows_its_complete_content(self):
        text = body(create_proposal())
        for expected in (PATH, "新しい行", "二行目", create_proposal()["sha256"], str(create_proposal()["bytes"])):
            self.assertIn(expected, text)
        self.assertNotIn("JSON", text)

    def test_an_empty_new_file_is_allowed(self):
        self.assertIn(PATH, body(create_proposal("")))

    def test_a_new_file_is_refused_unless_it_matches(self):
        for bad in (create_proposal(bytes=1), create_proposal(sha256="b" * 64), create_proposal(sha256="x"),
                    create_proposal(path=""), {**create_proposal(), "extra": 1}, create_proposal(bytes="9")):
            with self.subTest(bad=str(bad)[:60]), self.assertRaises(ValueError):
                review.bound_proposal(bad, **OPTIONS)
        with self.assertRaises(ValueError):
            review.bound_new_file({**create_proposal(), "full_proposed_content": None}, **OPTIONS)

    def test_invisible_characters_are_shown(self):
        hidden = "a\u202eb\x1bc"
        text = body(create_proposal(hidden))
        self.assertIn("\\u202E", text)
        self.assertIn("\\u001B", text)
        self.assertNotIn("\u202e", text)
        self.assertNotIn(hidden, body(patch_proposal(diff="+" + hidden + "\n")))

    def test_a_folder_and_a_move(self):
        self.assertIn(DIRECTORY["path"], body(DIRECTORY))
        self.assertIn("新しいフォルダの作成", body(DIRECTORY))
        text = body(MOVE)
        self.assertIn(MOVE["source"], text)
        self.assertIn(MOVE["destination"], text)

    def test_english_labels_exist_for_every_operation(self):
        options = {**OPTIONS, "locale": "en"}
        for proposal in (patch_proposal(), create_proposal(), DIRECTORY, MOVE):
            self.assertTrue(review.bound_proposal(proposal, **options).presentation.screen.title)

    def test_unknown_or_malformed_proposals_are_refused(self):
        for bad in (None, [], {}, {"operation": "delete", "path": PATH}, {**DIRECTORY, "extra": 1},
                    {**MOVE, "destination": MOVE["source"]}, {"operation": "create_directory", "path": ""},
                    {"operation": "move_file", "source": "a"}):
            with self.subTest(bad=bad), self.assertRaises((ValueError, KeyError)):
                review.bound_proposal(bad, **OPTIONS)


def frame_data(raw):
    return json.loads(raw.decode("ascii"))


class FrameTests(NoRealDialogTestCase):
    def test_a_frame_is_ascii_with_exact_text_and_labels(self):
        raw = gate.request_frame("f" * 64, patch_proposal(), 45.0)
        data = frame_data(raw)
        self.assertEqual(data.keys(), child._FIELDS)
        self.assertEqual(child.parse_request(raw), data)
        self.assertIn("+新しい行\n", data["body"])
        self.assertEqual(data["locale"], "ja")
        self.assertEqual(data["labels"][1], "今回だけ承認")

    def test_the_largest_allowed_file_fits_even_if_every_byte_is_escaped(self):
        for content in ("\x01" * 65536, "語" * 21845, "a" * 65536):
            with self.subTest(content=content[:1]):
                raw = gate.request_frame("f" * 64, create_proposal(content), 45.0)
                self.assertLessEqual(len(raw), gate.MAX_REQUEST)
                child.parse_request(raw)

    def test_an_oversized_frame_is_refused(self):
        with self.assertRaises(ValueError):
            gate.request_frame("f" * 64, create_proposal("\x01" * 200000), 45.0)

    def test_the_two_sides_share_one_limit(self):
        self.assertEqual(gate.MAX_REQUEST, child.MAX_REQUEST)


class FakeChild:
    pid = 4242

    def __init__(self, reply=b"", returncode=0, communicate=None):
        self.reply, self.returncode, self._communicate = reply, returncode, communicate
        self.stdin, self.stdout = Mock(closed=False), Mock(closed=False)
        self.sent = None
        self.killed = False

    def communicate(self, input=None, timeout=None):
        self.sent = input
        if self._communicate is not None:
            self._communicate()
        return self.reply(input) if callable(self.reply) else self.reply, b""

    def poll(self):
        return 0

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


def decided(decision):
    def reply(sent):
        return json.dumps({"nonce": json.loads(sent)["nonce"], "decision": decision}).encode()
    return reply


class GateTests(NoRealDialogTestCase):
    def setUp(self):
        super().setUp()
        self.guard = Mock()
        self.guard._identity.session_id = 7
        self.latch = Mock()
        self.latch.start.return_value = self.latch
        self.job = Mock()
        for target, value in (("OwnerSessionEventLatch", Mock(return_value=self.latch)),
                              ("WindowsJob", Mock(return_value=self.job))):
            patcher = patch.object(gate, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_gate(self, fake, proposal=None, **options):
        with patch.object(gate.subprocess, "Popen", return_value=fake) as launched:
            result = gate.review_once(self.guard, proposal or patch_proposal(), **options)
        self.launched = launched
        return result

    def test_each_decision_comes_back(self):
        for decision in ("APPROVE", "DENY", "EXPIRED"):
            with self.subTest(decision=decision):
                self.assertEqual(self.run_gate(FakeChild(decided(decision))), decision)

    def test_the_child_is_the_fixed_script_isolated_and_checked(self):
        fake = FakeChild(decided("DENY"))
        self.run_gate(fake)
        args, kwargs = self.launched.call_args
        self.assertEqual(args[0], [sys.executable, "-I", "-X", "utf8", str(SOURCE / "owner_file_dialog_localized.py")])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["creationflags"], 0x00000004)
        self.job.attach_and_resume.assert_called_once_with(fake)
        self.guard.verify_process.assert_called_with(fake.pid)
        self.assertEqual(frame_data(fake.sent)["locale"], "ja")
        self.assertEqual(frame_data(fake.sent)["seconds"], 45.0)

    def test_everything_is_closed_afterwards(self):
        self.run_gate(FakeChild(decided("APPROVE")))
        self.latch.close.assert_called_once()
        self.job.close.assert_called_once()

    def test_a_running_child_is_killed(self):
        fake = FakeChild(decided("APPROVE"))
        fake.poll = lambda: None
        self.run_gate(fake)
        self.assertTrue(fake.killed)

    def test_an_untrusted_answer_is_an_error(self):
        good = "APPROVE"
        cases = {
            "wrong_nonce": lambda sent: json.dumps({"nonce": "0" * 64, "decision": good}).encode(),
            "extra_field": lambda sent: json.dumps({"nonce": json.loads(sent)["nonce"], "decision": good, "x": 1}).encode(),
            "unknown_decision": decided("MAYBE"),
            "not_json": lambda sent: b"yes",
            "not_ascii": lambda sent: "はい".encode(),
            "empty": lambda sent: b"",
            "too_long": lambda sent: decided(good)(sent) + b" " * 512,
            "not_a_dict": lambda sent: b"[1]",
            "nonce_number": lambda sent: json.dumps({"nonce": 1, "decision": good}).encode(),
        }
        for name, reply in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.run_gate(FakeChild(reply)), "ERROR")

    def test_a_bad_exit_is_an_error(self):
        self.assertEqual(self.run_gate(FakeChild(decided("APPROVE"), returncode=1)), "ERROR")

    def test_a_failed_start_is_an_error(self):
        with patch.object(gate.subprocess, "Popen", side_effect=OSError("no")):
            self.assertEqual(gate.review_once(self.guard, patch_proposal()), "ERROR")

    def test_a_bad_proposal_opens_no_window(self):
        for bad in (patch_proposal(new_sha256="b" * 64), {"operation": "delete"}, None,
                    create_proposal("\x01" * 200000)):
            with self.subTest(bad=str(bad)[:40]):
                self.assertEqual(self.run_gate(FakeChild(decided("APPROVE")), bad or {"operation": "x"}), "ERROR")
                self.launched.assert_not_called()

    def test_bad_seconds_open_no_window(self):
        for seconds in (0, -1, 46, "45", None, True):
            with self.subTest(seconds=seconds):
                self.assertEqual(self.run_gate(FakeChild(decided("APPROVE")), seconds=seconds), "ERROR")
                self.launched.assert_not_called()

    def test_a_session_failure_before_the_window_opens_none(self):
        self.guard.verify.side_effect = PermissionError("locked")
        self.assertEqual(self.run_gate(FakeChild(decided("APPROVE"))), "ERROR")
        self.launched.assert_not_called()

    def test_another_owner_child_is_an_error(self):
        self.guard.verify_process.side_effect = PermissionError("other token")
        self.assertEqual(self.run_gate(FakeChild(decided("APPROVE"))), "ERROR")

    def test_a_session_change_after_approval_is_an_error(self):
        self.guard.verify.side_effect = [None, PermissionError("locked")]
        fake = FakeChild(decided("APPROVE"))
        with patch.object(gate, "Thread") as thread:
            thread.side_effect = lambda target, daemon: SimpleNamespace(
                start=target, is_alive=lambda: False, join=lambda timeout=None: None)
            self.assertEqual(self.run_gate(fake), "ERROR")

    def test_a_latch_change_after_approval_is_an_error(self):
        self.latch.verify.side_effect = [None, None, PermissionError("session changed")]
        fake = FakeChild(decided("APPROVE"))
        with patch.object(gate, "Thread") as thread:
            thread.side_effect = lambda target, daemon: SimpleNamespace(
                start=target, is_alive=lambda: False, join=lambda timeout=None: None)
            self.assertEqual(self.run_gate(fake), "ERROR")

    def test_a_session_change_during_the_window_ends_it(self):
        calls = {"count": 0}

        def verify():
            calls["count"] += 1
            if calls["count"] > 1:
                raise PermissionError("locked")
        self.guard.verify.side_effect = verify
        fake = FakeChild(decided("APPROVE"), communicate=lambda: time.sleep(3))
        started = time.monotonic()
        self.assertEqual(self.run_gate(fake), "ERROR")
        # It stops while the window is open, without waiting for an answer.
        self.assertLess(time.monotonic() - started, 2)

    def test_a_timeout_is_expired_not_approved(self):
        def late():
            raise subprocess.TimeoutExpired("x", 1)
        self.assertEqual(self.run_gate(FakeChild(decided("APPROVE"), communicate=late), seconds=1), "EXPIRED")

    def test_an_ended_clock_is_expired_even_if_the_child_never_answers(self):
        fake = FakeChild(decided("APPROVE"), communicate=lambda: time.sleep(0.8))
        self.assertEqual(self.run_gate(fake, seconds=0.01), "EXPIRED")

    def test_a_reader_failure_is_an_error(self):
        def broken():
            raise OSError("pipe")
        self.assertEqual(self.run_gate(FakeChild(decided("APPROVE"), communicate=broken)), "ERROR")

    def test_cleanup_failures_do_not_change_the_answer(self):
        self.latch.close.side_effect = OSError("x")
        self.job.close.side_effect = OSError("x")
        fake = FakeChild(decided("DENY"))
        fake.wait = Mock(side_effect=OSError("x"))
        fake.stdin.close.side_effect = OSError("x")
        self.assertEqual(self.run_gate(fake), "DENY")


def fake_tk():
    tk = Mock()
    tk.Tk.return_value.winfo_screenwidth.return_value = 1366
    tk.Tk.return_value.winfo_screenheight.return_value = 768
    families = ("Segoe UI", "Consolas", "Yu Gothic UI", "BIZ UDゴシック")
    tk.Tk.return_value.tk.call.side_effect = lambda *args: families if args[:2] == ("font", "families") else 18
    tk.Tk.return_value.tk.splitlist.side_effect = lambda value: value
    tk.Tk.return_value.winfo_children.return_value = []
    tk.Tk.return_value.winfo_width.return_value = 1286
    tk.Tk.return_value.winfo_height.return_value = 668
    tk.Text.return_value.winfo_height.return_value = 380
    return tk


def valid_frame(**updates):
    data = frame_data(gate.request_frame("f" * 64, patch_proposal(), 9.0))
    data.update(updates)
    return (json.dumps(data) + "\n").encode()


def run_child(tk, loop, raw=None):
    root = tk.Tk.return_value
    timers = []
    root.after.side_effect = lambda delay, callback: timers.append((delay, callback))

    def button(label):
        return next(c.kwargs["command"] for c in tk.Button.call_args_list if c.kwargs["text"] == label)
    events = SimpleNamespace(button=button, close=lambda: root.protocol.call_args.args[1](),
                             expire=lambda: next(callback for delay, callback in timers if delay == 9000)())
    root.mainloop.side_effect = lambda: loop(events)
    output = io.BytesIO()
    with patch.dict(sys.modules, {"tkinter": tk}), \
            patch.object(child.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw or valid_frame()))), \
            patch.object(child.sys, "stdout", SimpleNamespace(buffer=output)):
        child.main()
    return output.getvalue(), root


class ChildTests(NoRealDialogTestCase):
    def test_bad_requests_are_refused_before_any_window(self):
        good = frame_data(valid_frame())
        bad = [b"", b"{}\n", b"not-json\n", valid_frame()[:-1], b" " * (child.MAX_REQUEST + 1) + b"\n",
               b'{"nonce":"a","nonce":"b"}\n']
        for name in child._FIELDS:
            missing = dict(good)
            missing.pop(name)
            bad.append((json.dumps(missing) + "\n").encode())
        for updates in (dict(nonce="short"), dict(seconds=0), dict(seconds=61), dict(seconds="9"), dict(locale="fr"),
                        dict(kind="command"), dict(kind="v2_candidate"), dict(body=""), dict(body="a\x00b"), dict(title=1),
                        dict(labels=["a", "b"]), dict(labels=["a", "b", ""]), dict(labels="abc"), dict(path="x")):
            bad.append((json.dumps({**good, **updates}) + "\n").encode())
        tk = fake_tk()
        for raw in bad:
            with self.subTest(raw=raw[:40]), patch.dict(sys.modules, {"tkinter": tk}), self.assertRaises(Exception), \
                    patch.object(child.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))):
                child.main()
        tk.Tk.assert_not_called()

    def test_valid_json_over_the_limit_is_refused(self):
        raw = valid_frame(body="a" * child.MAX_REQUEST)
        self.assertGreater(len(raw), child.MAX_REQUEST)
        with self.assertRaises(ValueError):
            child.parse_request(raw)

    def test_a_duplicate_field_is_refused_even_if_the_rest_is_valid(self):
        raw = valid_frame()
        duplicated = b'{"body":"another text",' + raw[1:]
        with self.assertRaises(ValueError):
            child.parse_request(duplicated)

    def test_exactly_one_window_is_created(self):
        tk = fake_tk()
        run_child(tk, lambda e: e.close())
        tk.Tk.assert_called_once()

    def test_each_control_gives_its_decision_with_the_nonce(self):
        for name, action, expected in (("approve", lambda e: e.button("今回だけ承認")(), "APPROVE"),
                                       ("reject", lambda e: e.button("却下")(), "DENY"),
                                       ("close", lambda e: e.close(), "DENY"),
                                       ("timer", lambda e: e.expire(), "EXPIRED")):
            with self.subTest(name=name):
                output, _ = run_child(fake_tk(), action)
                self.assertEqual(json.loads(output), {"nonce": "f" * 64, "decision": expected})

    def test_the_first_decision_wins(self):
        def both(events):
            events.button("却下")()
            events.button("今回だけ承認")()
        output, _ = run_child(fake_tk(), both)
        self.assertEqual(json.loads(output)["decision"], "DENY")

    def test_closing_without_a_decision_gives_no_answer(self):
        with self.assertRaises(RuntimeError):
            run_child(fake_tk(), lambda events: None)

    def test_the_repeat_button_stays_disabled(self):
        tk = fake_tk()
        run_child(tk, lambda e: e.close())
        repeat = next(c for c in tk.Button.call_args_list if c.kwargs["text"] == "繰り返し承認（現在使用不可）")
        self.assertEqual(repeat.kwargs["state"], "disabled")

    def test_the_exact_text_reaches_the_window(self):
        tk = fake_tk()
        run_child(tk, lambda e: e.close())
        shown = "".join(str(c.args[1]) for c in tk.Text.return_value.insert.call_args_list)
        self.assertIn("-古い行\n+新しい行\n", shown)
        self.assertIn(PATH, shown)

    def test_it_is_invisible_until_the_fit_check_passes(self):
        tk = fake_tk()
        output, root = run_child(tk, lambda e: e.close())
        alpha = [c.args[1] for c in root.attributes.call_args_list if c.args[0] == "-alpha"]
        self.assertEqual(alpha, [0.0, 1.0])
        self.assertTrue(output)

    def test_a_screen_too_small_gives_no_window_and_no_answer(self):
        tk = fake_tk()
        tk.Text.return_value.winfo_height.return_value = 30
        with self.assertRaises(RuntimeError):
            run_child(tk, lambda e: self.fail("window was shown"))
        root = tk.Tk.return_value
        self.assertNotIn(1.0, [c.args[1] for c in root.attributes.call_args_list if c.args[0] == "-alpha"])


class ConnectionScopeTests(NoRealDialogTestCase):
    def tree(self, name):
        return ast.parse((SOURCE / name).read_text(encoding="utf-8"))

    def names_used(self, name):
        tree = self.tree(name)
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                found.update(alias.name for alias in node.names)
                found.add(getattr(node, "module", None) or "")
        return found

    def test_only_dev_rdc_compat_imports_the_gate_or_the_child(self):
        for path in SOURCE.glob("*.py"):
            if path.name == "owner_dialog_integrity.py":
                # Hashing a child filename is not importing or executing it.
                names = self.names_used(path.name)
                self.assertFalse({"owner_file_review_gate_dev_20261105", "owner_file_dialog_localized",
                                  "importlib", "__import__", "exec", "eval", "compile",
                                  "spec_from_file_location", "exec_module", "Popen"} & names)
                continue
            if path.name in ("owner_file_review_gate_dev_20261105.py", "owner_file_dialog_localized.py",
                             "release_inventory.py", "dev_rdc_compat.py"):
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("owner_file_review_gate_dev_20261105", text)
                self.assertNotIn("owner_file_dialog_localized", text)

    def test_neither_can_write_or_reach_the_writer(self):
        forbidden = {"open", "write_text", "write_bytes", "replace", "unlink", "rename", "remove", "file_edits",
                     "dev_rdc_compat", "mkdir", "rmdir", "os"}
        self.assertFalse(forbidden & self.names_used("owner_file_review_gate_dev_20261105.py"))
        self.assertFalse(forbidden & self.names_used("owner_file_dialog_localized.py"))

    def test_the_inventory_marks_them_dev_only(self):
        for name in ("owner_file_review_gate_dev_20261105.py", "owner_file_dialog_localized.py"):
            self.assertIn(name, inventory.DEV_ONLY_MODULES)
            self.assertNotIn(name, inventory.MODULES)

    def test_the_guard_knows_the_new_window(self):
        from tests._no_real_dialog import REAL_DIALOGS
        self.assertIn("owner_file_dialog_localized.py", REAL_DIALOGS)
        self.assertEqual(gate.DIALOG_SCRIPT.name, "owner_file_dialog_localized.py")
        with self.assertRaises(PermissionError):
            subprocess.Popen([sys.executable, str(gate.DIALOG_SCRIPT)])
        self.assertEqual(self.real_dialog_guard.attempts, ["owner_file_dialog_localized.py"])
        self.real_dialog_guard.attempts.clear()


if __name__ == "__main__":
    import unittest
    unittest.main()
