"""Fake Tk and a fake monotonic clock exercise the isolated lease executable."""

import io
from ipaddress import IPv4Address
from types import SimpleNamespace
import unittest

from sora_commander_next import vm_lease as lease, vm_protocol as protocol


_ID = "a" * 32
_ADDRESS = str(IPv4Address((10 << 24) + 1))
_STARTED = f"LEASE {_ID} STARTED\n"
_MARKER = "sensitive synthetic error"


def _argv():
    return ["--lease-id", _ID, "--vm", "試験 VM", "--address", _ADDRESS,
            "--minutes", "1", "--max-inputs", "200", "--tools", "screenshot,wait",
            "--screenshots-are-sent-to-chatgpt", "yes", "--deadline", "60"]


class FakeWidget:
    def __init__(self, root, **options):
        self.root = root
        self.options = options

    def configure(self, **options):
        self.options.update(options)

    def pack(self, **_):
        pass


class FakeWindow:
    def __init__(self, action, output):
        self.action = action
        self.output = output
        self.now = 0
        self.destroyed = False
        self.quitting = False
        self.labels = []
        self.timer = None
        self.settings = {}
        self.protocols = {}
        self.button = None

    def title(self, text):
        self.settings["title"] = text

    def attributes(self, key, value):
        self.settings[key] = value

    def resizable(self, width, height):
        self.settings["resizable"] = (width, height)

    def protocol(self, key, fn):
        self.protocols[key] = fn

    def update_idletasks(self):
        self.output_before_map = self.output.getvalue()

    def winfo_viewable(self):
        if self.action == "minimised" and getattr(self, "minimised", False):
            return False
        return self.action != "hidden" and not getattr(self, "gone", False)

    def deiconify(self):
        self.restored = getattr(self, "restored", 0) + 1
        self.minimised = False

    def after(self, ms, fn):
        self.timer = (ms, fn)

    def quit(self):
        self.quitting = True

    def destroy(self):
        self.destroyed = True
        if self.action == "destroy-fails":
            raise RuntimeError(_MARKER)

    def mainloop(self):
        self.output_at_mainloop = self.output.getvalue()
        if self.action == "close":
            self.protocols["WM_DELETE_WINDOW"]()
        elif self.action in ("stop", "destroy-fails"):
            self.button.options["command"]()
        elif self.action == "deadline":
            while not self.quitting and self.now <= 61:
                ms, callback = self.timer
                self.now = round(self.now + ms / 1000, 3)
                callback()
        elif self.action in ("minimised", "cannot-restore"):
            self.minimised = True
            self.gone = self.action == "cannot-restore"
            for _ in range(20):
                if self.quitting:
                    return
                ms, callback = self.timer
                self.now = round(self.now + ms / 1000, 3)
                callback()
            self.button.options["command"]()
        elif self.action == "fails":
            raise RuntimeError(_MARKER)
        elif self.action in ("callback-fails", "callback-fails-then-stop"):
            self.report_callback_exception(RuntimeError, RuntimeError(_MARKER), None)
            if self.action == "callback-fails-then-stop":
                self.button.options["command"]()

    def label(self, root, **options):
        widget = FakeWidget(root, **options)
        self.labels.append(widget)
        return widget

    def make_button(self, root, **options):
        self.button = FakeWidget(root, **options)
        return self.button

    def toolkit(self):
        return SimpleNamespace(Tk=lambda: self, Label=self.label, Button=self.make_button)


class LeaseWindowTests(unittest.TestCase):
    def run_window(self, action):
        output = io.StringIO()
        window = FakeWindow(action, output)
        code = lease.main(_argv(), toolkit=window.toolkit(), clock=lambda: window.now,
                          output=output, alive=lambda _: True)
        return code, output.getvalue(), window

    def test_stop_close_and_ui(self):
        for action in ("stop", "close"):
            with self.subTest(action=action):
                code, output, window = self.run_window(action)
                self.assertEqual((code, output), (0, _STARTED + "STOPPED\n"))
                self.assertEqual(window.output_before_map, "")
                self.assertEqual(window.output_at_mainloop, _STARTED)
                self.assertTrue(window.destroyed)
                self.assertIs(window.settings["-topmost"], True)
                self.assertIn("試験 VM", window.labels[0].options["text"])
                self.assertIn("01:00", window.labels[1].options["text"])
                self.assertEqual(window.button.options["text"], "停止 / Stop")
                self.assertGreater(window.button.options["height"], 1)
                self.assertNotIn(_ADDRESS, output)

    def test_monotonic_deadline_expires_without_stop(self):
        code, output, window = self.run_window("deadline")
        self.assertEqual((code, output), (0, _STARTED + "EXPIRED\n"))
        self.assertEqual(window.now, 60)
        self.assertTrue(window.destroyed)

    def test_window_uses_existing_deadline_and_closes_when_client_handle_releases(self):
        output = io.StringIO()
        window = FakeWindow("deadline", output)
        window.now = 45
        code = lease.main(_argv(), toolkit=window.toolkit(), clock=lambda: window.now,
                          output=output, alive=lambda _: window.now < 46)
        self.assertEqual((code, output.getvalue()), (0, _STARTED + "STOPPED\n"))
        self.assertEqual(window.now, 46)
        self.assertEqual(window.labels[1].options["text"].split()[-1], "00:15")
        self.assertTrue(window.destroyed)

    def test_indicator_presence_lasts_only_while_client_holds_its_handle(self):
        release = lease.hold_indicator(_ID)
        try:
            self.assertTrue(lease.indicator_alive(_ID))
        finally:
            release()
        self.assertFalse(lease.indicator_alive(_ID))

    def test_a_minimised_stop_window_comes_back(self):
        code, output, window = self.run_window("minimised")
        self.assertEqual((code, output), (0, _STARTED + "STOPPED\n"))
        self.assertEqual(window.restored, 1)

    def test_a_stop_window_that_stays_hidden_ends_the_lease(self):
        code, output, window = self.run_window("cannot-restore")
        self.assertEqual((code, output), (1, _STARTED))
        self.assertTrue(window.destroyed)
        self.assertLess(window.now, 3)

    def test_window_failures_never_report_normal_exit_or_exception_text(self):
        for action in ("hidden", "fails", "callback-fails", "callback-fails-then-stop", "destroy-fails",
                       "unexpected-return"):
            with self.subTest(action=action):
                code, output, window = self.run_window(action)
                self.assertEqual(code, 1)
                self.assertEqual(output, "" if action == "hidden" else _STARTED)
                self.assertTrue(window.destroyed)
        output = io.StringIO()

        def cannot_create():
            raise RuntimeError(_MARKER)

        self.assertEqual(lease.main(_argv(), toolkit=SimpleNamespace(Tk=cannot_create), output=output,
                                    clock=lambda: 0, alive=lambda _: True), 1)
        self.assertEqual(output.getvalue(), "")

    def test_arguments_refused_without_entering_tk(self):
        substitutions = {
            "--lease-id": ["A" * 32, "0" * 31, _ID + "\n"], "--vm": ["", "VM\n", "A" * 101],
            "--address": ["example", str(IPv4Address(0)), str(IPv4Address(10 << 24))],
            "--minutes": ["0", "31", "+5", "1.0", "01"],
            "--max-inputs": ["0", "201", "1e1", "01"],
            "--tools": ["wait", "screenshot,wait,wait", "screenshot, wait", "screenshot,"],
            "--screenshots-are-sent-to-chatgpt": ["true", "Yes", ""],
            "--deadline": ["NaN", "inf", "-1", ""],
        }
        argv = _argv()
        invalid = [argv + ["--vm", "duplicate"], argv + ["--key", _MARKER], argv[:-1]]
        for name, values in substitutions.items():
            index = argv.index(name)
            invalid.append(argv[:index] + argv[index + 2:])
            invalid.extend(argv[:index + 1] + [value] + argv[index + 2:] for value in values)
        calls = []
        toolkit = SimpleNamespace(Tk=lambda: calls.append("attempted"))
        for index, arguments in enumerate(invalid):
            with self.subTest(case=index):
                output = io.StringIO()
                self.assertEqual(lease.main(arguments, toolkit=toolkit, output=output), 2)
                self.assertEqual(output.getvalue(), "")
        self.assertEqual(calls, [])

    def test_isolated_validator_matches_prototype_contract(self):
        self.assertEqual(lease._TOOLS, protocol.TOOLS)
        self.assertEqual(lease._NETWORKS, protocol.PRIVATE_NETWORKS)
