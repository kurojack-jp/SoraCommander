"""Native read-only Win32 acceptance on disposable temporary files."""
import os
import tempfile
import unittest
from pathlib import Path
from hashlib import sha256
from sora_commander_next.offline_native_windows_source_probe import inspect_native_readonly

@unittest.skipUnless(os.name == "nt", "Windows only")
class NativeReadProbeTests(unittest.TestCase):
    def test_utf8_and_identity_evidence(self):
        with tempfile.TemporaryDirectory(prefix="scn-native-read-") as tmp:
            f=Path(tmp)/"safe.py"
            data="# テスト\r\nx = 1\r\n".encode()
            f.write_bytes(data)
            proof=inspect_native_readonly(str(f))
            self.assertEqual(proof.digest,sha256(data).hexdigest())
            self.assertEqual(proof.size,len(data))
            self.assertGreater(proof.file_id,0)
            self.assertEqual(proof.authority,"NONE_READ_ONLY")
            self.assertEqual(f.read_bytes(),data)
    def test_share_zero_prevents_other_write_during_read(self):
        with tempfile.TemporaryDirectory(prefix="scn-native-share-") as tmp:
            f=Path(tmp)/"safe.py"
            f.write_bytes(b"x = 1\n")
            blocked=[]
            def try_write():
                try:
                    f.write_bytes(b"x = 2\n")
                except OSError:
                    blocked.append(True)
            proof=inspect_native_readonly(str(f),while_held=try_write)
            self.assertEqual(blocked,[True])
            self.assertEqual(proof.digest,sha256(b"x = 1\n").hexdigest())
            self.assertEqual(f.read_bytes(),b"x = 1\n")
    def test_reject_size_and_nul(self):
        with tempfile.TemporaryDirectory(prefix="scn-native-invalid-") as tmp:
            f=Path(tmp)/"safe.py"
            for contents in (b"a"*16385,bytes((0,))):
                f.write_bytes(contents)
                with self.assertRaises(ValueError):
                    inspect_native_readonly(str(f))
    def test_failed_read_does_not_poison_next_read(self):
        with tempfile.TemporaryDirectory(prefix="scn-native-recover-") as tmp:
            f=Path(tmp)/"safe.py"
            f.write_bytes(bytes((0,)))
            with self.assertRaises(ValueError):
                inspect_native_readonly(str(f))
            f.write_bytes(b"ok = True\n")
            result=inspect_native_readonly(str(f))
            self.assertEqual(result.digest,sha256(b"ok = True\n").hexdigest())

    def test_reparse_target_is_not_followed(self):
        with tempfile.TemporaryDirectory(prefix="scn-native-link-") as tmp:
            root=Path(tmp)
            target=root/"target.py"
            link=root/"alias.py"
            target.write_bytes(b"safe = True\n")
            try:
                os.symlink(target,link)
            except (OSError,NotImplementedError) as exc:
                self.skipTest(f"Windows symlink privilege unavailable: {exc}")
            with self.assertRaises((OSError,ValueError)):
                inspect_native_readonly(str(link))
            self.assertEqual(target.read_bytes(),b"safe = True\n")

    def test_callback_failure_releases_exclusive_handle(self):
        with tempfile.TemporaryDirectory(prefix="scn-native-callback-") as tmp:
            f = Path(tmp) / "safe.py"
            f.write_bytes(b"x = 1\n")
            def fail_while_held():
                raise RuntimeError("simulated observer failure")
            with self.assertRaisesRegex(RuntimeError, "observer failure"):
                inspect_native_readonly(str(f), while_held=fail_while_held)
            f.write_bytes(b"x = 2\n")
            self.assertEqual(inspect_native_readonly(str(f)).digest,
                             sha256(b"x = 2\n").hexdigest())

    def test_invalid_input_rejected(self):
        for arg in (None,123,"safe.py",r"\\server\share\safe.py",r"D:relative.py",r"D:\safe.py:stream",r"D:\src\..\safe.py",r"\\?\D:\safe.py"):
            with self.assertRaises(ValueError):
                inspect_native_readonly(arg)
