import os
import unittest
from unittest.mock import patch

@unittest.skipUnless(os.name == "nt", "Windows only")
class ListenerTests(unittest.TestCase):
    def test_rejects_untrusted_guard(self):
        from sora_commander_next.windows_pipe_listener import serve_once
        with self.assertRaises(TypeError):
            serve_once(owner_guard=object(), dispatcher=None,
                       pipe_name=r"\\.\pipe\SoraCommanderNext-test")

    def test_rejects_nonlocal_endpoint_before_any_io(self):
        from sora_commander_next.windows_owner import OwnerSessionGuard
        from sora_commander_next.windows_pipe_listener import serve_once
        guard = object.__new__(OwnerSessionGuard)
        with self.assertRaises(ValueError):
            serve_once(owner_guard=guard, dispatcher=None,
                       pipe_name=r"\\remote\pipe\SoraCommanderNext-test")

    def test_rejects_path_separator(self):
        from sora_commander_next.windows_owner import OwnerSessionGuard
        from sora_commander_next.windows_pipe_listener import serve_once
        guard = object.__new__(OwnerSessionGuard)
        with self.assertRaises(ValueError):
            serve_once(owner_guard=guard, dispatcher=None,
                       pipe_name=r"\\.\pipe\SoraCommanderNext-foo\bar")

    def test_read_exact_disconnect(self):
        import sora_commander_next.windows_pipe_listener as mod
        with patch.object(mod, "ReadFile", return_value=False):
            with self.assertRaises(mod.PipeListenerError):
                mod._read_exact(123, 4)

    def test_write_all_disconnect(self):
        import sora_commander_next.windows_pipe_listener as mod
        with patch.object(mod, "WriteFile", return_value=False):
            with self.assertRaises(mod.PipeListenerError):
                mod._write_all(123, b"abc")


if __name__ == "__main__":
    unittest.main()
