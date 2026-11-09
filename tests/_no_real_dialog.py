"""Keep automated tests from opening a real Owner approval window.

A real dialog can be approved by a person who has no idea a test opened it. The
guard refuses to start one and records the attempt. A broker catches the refusal
and returns ERROR, so a test could still pass; finish() therefore fails on any
recorded attempt, whatever the broker returned. Manual harnesses do not use this.

It matches by name, so an unrelated command naming one of these files, such as
`git show HEAD:.../owner_approval_dialog.py`, is refused too. That is deliberate.
It covers subprocess.Popen only, not os.system, os.spawn or other process APIs.
"""
import ntpath
import subprocess
import unittest

REAL_DIALOGS = frozenset({"owner_approval_dialog.py", "owner_approval_dialog_v2.py",
                          "owner_command_dialog_localized.py", "owner_file_dialog_localized.py"})


def _names(args):
    if isinstance(args, bytes):
        args = args.decode("utf-8", "replace")
    if isinstance(args, str):
        return tuple(name for name in REAL_DIALOGS if name in args)
    try:
        return tuple(ntpath.basename(str(item)) for item in args)
    except TypeError:
        return ()


class RealDialogGuard:
    def __init__(self):
        self.attempts = []
        self._previous = None

    def start(self):
        guard = self
        previous = self._previous = subprocess.Popen

        class Guarded(previous):
            def __init__(self, args, *a, **k):
                found = [name for name in _names(args) if name in REAL_DIALOGS]
                if found:
                    guard.attempts.append(found[0])
                    raise PermissionError("automated test tried to open a real Owner dialog")
                super().__init__(args, *a, **k)

        subprocess.Popen = Guarded
        return self

    def stop(self):
        if self._previous is not None:
            subprocess.Popen = self._previous
            self._previous = None

    def finish(self):
        self.stop()
        if self.attempts:
            raise AssertionError(f"automated test tried to open a real Owner dialog: {self.attempts}")


class NoRealDialogTestCase(unittest.TestCase):
    """Any attempt fails the test in cleanup, even if the code under test hid it."""

    def setUp(self):
        super().setUp()
        self.real_dialog_guard = RealDialogGuard().start()
        self.addCleanup(self.real_dialog_guard.finish)
