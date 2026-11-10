"""Synthetic T3 capture tests. All Win32 calls mocked; no desktop captured."""
import contextlib
import ctypes
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import io
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from tests._inherited_test_temp import inherited_test_temp

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "scn_capture_window.py"
spec = importlib.util.spec_from_file_location("scn_capture_window", SCRIPT)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)
NOW = datetime(2026, 11, 10, 1, 2, 3, tzinfo=timezone.utc)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(inherited_test_temp())).resolve()
        self.out = self.root / "captures"
        self.out.mkdir()
        self.enterContext(patch("sora_commander_next.__file__", str(self.root / "install/src/sora_commander_next/__init__.py")))
        self.api = Mock()
        self.api.default_desktop.return_value = True
        self.api.matching.return_value = [(123, "Open Guild Forge", "ogf-desktop.exe", 456)]
        self.api.geometry.return_value = ([10, 20, 12, 22], [2, 2], 144)
        self.api.capture.return_value = bytes((20, 40, 60, 0)) * 4

    def run_capture(self):
        return capture.capture_window("ogf-desktop.exe", "Open Guild Forge", str(self.out), api=self.api, now=NOW)

    def assert_empty(self):
        self.assertEqual(list(self.out.iterdir()), [])

    def test_zero_two_windows(self):
        for matches in ([], self.api.matching.return_value * 2):
            self.api.matching.return_value = matches
            with self.assertRaises(capture.CaptureError) as cm:
                self.run_capture()
            self.assertEqual(cm.exception.exit_code, 2)
            self.assert_empty()
        self.api.capture.assert_not_called()

    def test_locked_minimized_cloaked_zero_size_write_nothing(self):
        self.api.default_desktop.return_value = False
        with self.assertRaisesRegex(capture.CaptureError, "DESKTOP_NOT_DEFAULT"):
            self.run_capture()
        self.assert_empty()
        self.api.default_desktop.return_value = True
        for code in ("WINDOW_MINIMIZED", "WINDOW_CLOAKED", "WINDOW_SIZE_INVALID"):
            self.api.geometry.side_effect = capture.CaptureError(code)
            with self.assertRaisesRegex(capture.CaptureError, code):
                self.run_capture()
            self.assert_empty()
        self.api.capture.assert_not_called()

    def test_changed_target_or_locked_after_capture(self):
        self.api.default_desktop.side_effect = [True, False]
        with self.assertRaisesRegex(capture.CaptureError, "WINDOW_CHANGED"):
            self.run_capture()
        self.assert_empty()

    def test_missing_linked_install_output_refused(self):
        for path in (self.out / "missing", self.root / "install"):
            if path.name == "install":
                path.mkdir()
            with self.assertRaisesRegex(capture.CaptureError, "OUTPUT_NOT_ALLOWED"):
                capture.output_folder(str(path))
        real = Path.lstat
        def linked(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            if path == self.out:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, "lstat", linked):
            with self.assertRaisesRegex(capture.CaptureError, "OUTPUT_NOT_ALLOWED"):
                capture.output_folder(str(self.out))
        self.assert_empty()

    def decode(self, png):
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        pos, chunks = 8, []
        while pos < len(png):
            size = struct.unpack(">I", png[pos:pos + 4])[0]
            kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + size]
            crc = struct.unpack(">I", png[pos + 8 + size:pos + 12 + size])[0]
            self.assertEqual(crc, zlib.crc32(kind + data) & 0xFFFFFFFF)
            chunks.append((kind, data))
            pos += 12 + size
        self.assertEqual([c[0] for c in chunks], [b"IHDR", b"IDAT", b"IEND"])
        w, h = struct.unpack(">II", chunks[0][1][:8])
        rows = zlib.decompress(chunks[1][1])
        self.assertEqual(len(rows), h * (w * 3 + 1))
        return w, h, rows

    def test_png_crc_pixels_and_downscale(self):
        png, scale = capture.encode_png(bytes((20, 40, 60, 0)) * 3202, 1601, 2)
        w, h, rows = self.decode(png)
        self.assertLessEqual(max(w, h), 1600)
        self.assertEqual(rows[1:4], bytes((60, 40, 20)))
        self.assertEqual(scale, [w / 1601, h / 2])
        self.assertLess(len(png), capture.MAX_BYTES)

    def test_size_cap_forces_further_scaling(self):
        # Lower only the encoding cap to exercise the same retry without huge IO.
        with patch.object(capture, "MAX_BYTES", 80):
            png, scale = capture.encode_png(bytes(range(256)) * 16, 32, 32)
        self.assertLess(len(png), 80)
        self.assertLess(scale[0], 1)
        self.decode(png)

    def test_success_fields_hash_exclusive_create(self):
        result = self.run_capture()
        path = Path(result["png_path"])
        raw = path.read_bytes()
        self.decode(raw)
        self.assertEqual(result["png_sha256"], sha256(raw).hexdigest())
        self.assertEqual(result["png_bytes"], len(raw))
        self.assertEqual(set(result), {"status", "window_title", "process", "pid", "window_rect", "client_size", "dpi", "scale", "captured_at_utc", "png_path", "png_sha256", "png_bytes", "next"})
        with self.assertRaisesRegex(capture.CaptureError, "OUTPUT_EXISTS"):
            self.run_capture()
        self.assertEqual(path.read_bytes(), raw)

    def test_retains_exact_newest_twenty_leaves_other_files(self):
        for second in range(25):
            (self.out / f"capture-20261109T0102{second:02d}Z.png").write_bytes(b"synthetic")
        other = self.out / "capture-manual.png"
        other.write_bytes(b"keep")
        directory = self.out / "capture-20261101T010203Z.png"
        directory.mkdir()
        self.run_capture()
        files = sorted(p.name for p in self.out.iterdir() if p.is_file() and capture.CAPTURE_NAME.fullmatch(p.name))
        self.assertEqual(len(files), 20)
        self.assertEqual(files[0], "capture-20261109T010206Z.png")
        self.assertEqual(other.read_bytes(), b"keep")
        self.assertTrue(directory.is_dir())

    def test_stdout_one_json_and_exit_codes(self):
        args = ["--process", "ogf-desktop.exe", "--title", "Open Guild Forge", "--out", str(self.out)]
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = capture.main(args, api=self.api)
        self.assertEqual(code, 0)
        self.assertLess(len(stream.getvalue().encode()), 8192)
        self.assertEqual(len(stream.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(stream.getvalue())["status"], "OK")
        for matches, expected in (([], 2), ([(1, "title", "process", 2)], 3)):
            self.api.matching.return_value = matches
            self.api.default_desktop.return_value = expected != 3
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(capture.main(args, api=self.api), expected)
            self.assertEqual(json.loads(stream.getvalue())["status"], "ERROR")


class WindowsWrapperTests(unittest.TestCase):
    def setUp(self):
        self.dlls = {name: Mock() for name in ("user32", "kernel32", "gdi32", "dwmapi")}
        self.enterContext(patch.object(ctypes, "WinDLL", side_effect=lambda name, **kw: self.dlls[name]))
        self.api = capture.WindowsAPI()

    def test_geometry_refusals(self):
        user, dwm = self.api.user, self.api.dwm
        user.IsIconic.return_value = True
        with self.assertRaisesRegex(capture.CaptureError, "WINDOW_MINIMIZED"):
            self.api.geometry(123)
        user.IsIconic.return_value = False
        dwm.DwmGetWindowAttribute.return_value = -1
        with self.assertRaisesRegex(capture.CaptureError, "WINDOW_CLOAKED"):
            self.api.geometry(123)
        dwm.DwmGetWindowAttribute.return_value = 0
        user.GetDpiForWindow.return_value = 96
        with self.assertRaisesRegex(capture.CaptureError, "WINDOW_SIZE_INVALID"):
            self.api.geometry(123)

    def test_default_desktop_closed_and_unavailable(self):
        self.api.user.OpenInputDesktop.return_value = None
        self.assertFalse(self.api.default_desktop())
        self.api.user.OpenInputDesktop.return_value = 123
        self.api.user.GetUserObjectInformationW.return_value = False
        self.assertFalse(self.api.default_desktop())
        self.api.user.CloseDesktop.assert_called_once_with(123)

    def test_desktop_names_and_dpi(self):
        user = self.api.user
        user.OpenInputDesktop.return_value = 123
        for name, expected in (("Default", True), ("Winlogon", False), ("Secure", False)):
            def information(handle, index, buffer, size, needed):
                buffer.value = name
                return True
            user.GetUserObjectInformationW.side_effect = information
            self.assertEqual(self.api.default_desktop(), expected)
        user.SetProcessDpiAwarenessContext.return_value = False
        user.GetAwarenessFromDpiAwarenessContext.return_value = 0
        with self.assertRaisesRegex(capture.CaptureError, "DPI_UNAVAILABLE"):
            self.api.dpi_aware()
        user.GetAwarenessFromDpiAwarenessContext.return_value = 2
        self.api.dpi_aware()

    def test_visible_process_casefold_and_title_selection(self):
        user, kernel = self.api.user, self.api.kernel
        user.IsWindowVisible.side_effect = lambda hwnd: hwnd != 4
        titles = {1: "Open Guild Forge", 2: "Other App", 3: "Open Guild Forge - copy", 4: "Open Guild Forge"}
        user.GetWindowTextLengthW.side_effect = lambda hwnd: len(titles[hwnd])
        def text(hwnd, buffer, size):
            buffer.value = titles[hwnd]
            return len(buffer.value)
        user.GetWindowTextW.side_effect = text
        def process(hwnd, pid):
            ctypes.cast(pid, ctypes.POINTER(capture.W.DWORD))[0] = hwnd + 100
            return 1
        user.GetWindowThreadProcessId.side_effect = process
        kernel.OpenProcess.side_effect = lambda access, inherit, pid: pid
        def image(handle, flags, buffer, size):
            buffer.value = "C:\\synthetic\\OGF-DESKTOP.EXE" if handle == 101 else "C:\\synthetic\\other.exe"
            return True
        kernel.QueryFullProcessImageNameW.side_effect = image
        def enum(callback, argument):
            for hwnd in titles:
                if not callback(hwnd, argument):
                    return False
            return True
        user.EnumWindows.side_effect = enum
        matches = self.api.matching("ogf-desktop.exe", "Open Guild Forge")
        self.assertEqual(matches, [(1, titles[1], "OGF-DESKTOP.EXE", 101)])
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_printwindow_fallback_uses_only_target_dc_and_releases_handles(self):
        user, gdi = self.api.user, self.api.gdi
        user.GetWindowDC.return_value = 111
        gdi.CreateCompatibleDC.return_value = 222
        gdi.SelectObject.return_value = 444
        memory = ctypes.create_string_buffer(bytes((1, 2, 3, 0)) * 4)
        def dib(dc, header, usage, pointer, section, offset):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(memory)
            return 333
        gdi.CreateDIBSection.side_effect = dib
        for print_ok in (True, False):
            gdi.BitBlt.reset_mock()
            user.PrintWindow.return_value = print_ok
            self.assertEqual(self.api.capture(123, 2, 2), memory.raw[:16])
            user.GetWindowDC.assert_called_with(123)
            user.PrintWindow.assert_called_with(123, 222, 2)
            if print_ok:
                gdi.BitBlt.assert_not_called()
            else:
                gdi.BitBlt.assert_called_once_with(222, 0, 0, 2, 2, 111, 0, 0, 0x00CC0020)
            user.ReleaseDC.assert_called_with(123, 111)
            gdi.DeleteObject.assert_called_with(333)
            gdi.DeleteDC.assert_called_with(222)


if __name__ == "__main__":
    unittest.main()
