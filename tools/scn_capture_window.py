"""Owner task preset helper. Captures exactly one selected window, never a desktop.

Standard library only. Importing this module does not load or call Windows APIs.
The calling run_task supplies the normal Owner command approval, not this script.
"""
import argparse
import ctypes
from ctypes import wintypes as W
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import stat
import struct
import sys
import zlib

MAX_BYTES = 4 * 1024 * 1024
CAPTURE_NAME = re.compile(r"capture-\d{8}T\d{6}Z\.png\Z", re.ASCII)


class CaptureError(Exception):
    def __init__(self, code, exit_code=3):
        super().__init__(code)
        self.code, self.exit_code = code, exit_code


class WindowsAPI:
    """Explicit pointer-sized ABI; each handle is released by its owning scope."""
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        signatures = (
            (self.user, "SetProcessDpiAwarenessContext", [W.HANDLE], W.BOOL),
            (self.user, "GetThreadDpiAwarenessContext", [], W.HANDLE),
            (self.user, "GetAwarenessFromDpiAwarenessContext", [W.HANDLE], ctypes.c_int),
            (self.user, "EnumWindows", [self.callback_type, W.LPARAM], W.BOOL),
            (self.user, "IsWindowVisible", [W.HWND], W.BOOL),
            (self.user, "IsIconic", [W.HWND], W.BOOL),
            (self.user, "GetWindowTextLengthW", [W.HWND], ctypes.c_int),
            (self.user, "GetWindowTextW", [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user, "GetWindowThreadProcessId", [W.HWND, ctypes.POINTER(W.DWORD)], W.DWORD),
            (self.user, "GetWindowRect", [W.HWND, ctypes.POINTER(W.RECT)], W.BOOL),
            (self.user, "GetClientRect", [W.HWND, ctypes.POINTER(W.RECT)], W.BOOL),
            (self.user, "GetDpiForWindow", [W.HWND], W.UINT),
            (self.user, "OpenInputDesktop", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.user, "GetUserObjectInformationW", [W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL),
            (self.user, "CloseDesktop", [W.HANDLE], W.BOOL),
            (self.kernel, "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.kernel, "QueryFullProcessImageNameW", [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)], W.BOOL),
            (self.kernel, "CloseHandle", [W.HANDLE], W.BOOL),
            (self.dwm, "DwmGetWindowAttribute", [W.HWND, W.DWORD, W.LPVOID, W.DWORD], ctypes.c_long),
            (self.user, "GetWindowDC", [W.HWND], W.HDC),
            (self.user, "ReleaseDC", [W.HWND, W.HDC], ctypes.c_int),
            (self.user, "PrintWindow", [W.HWND, W.HDC, W.UINT], W.BOOL),
            (self.gdi, "CreateCompatibleDC", [W.HDC], W.HDC),
            (self.gdi, "CreateDIBSection", [W.HDC, W.LPVOID, W.UINT, ctypes.POINTER(W.LPVOID), W.HANDLE, W.DWORD], W.HBITMAP),
            (self.gdi, "SelectObject", [W.HDC, W.HANDLE], W.HANDLE),
            (self.gdi, "DeleteObject", [W.HANDLE], W.BOOL),
            (self.gdi, "DeleteDC", [W.HDC], W.BOOL),
            (self.gdi, "GdiFlush", [], W.BOOL),
            (self.gdi, "BitBlt", [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HDC, ctypes.c_int, ctypes.c_int, W.DWORD], W.BOOL),
        )
        for dll, name, args, result in signatures:
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result

    def dpi_aware(self):
        if not self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            context = self.user.GetThreadDpiAwarenessContext()
            if self.user.GetAwarenessFromDpiAwarenessContext(context) != 2:
                raise CaptureError("DPI_UNAVAILABLE")

    def default_desktop(self):
        desktop = self.user.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS
        if not desktop:
            return False
        try:
            name, needed = ctypes.create_unicode_buffer(256), W.DWORD()
            return bool(self.user.GetUserObjectInformationW(
                desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed))
                and name.value.casefold() == "default")
        finally:
            self.user.CloseDesktop(desktop)

    def matching(self, process, title):
        matches, failed = [], []
        def visit(hwnd, unused):
            try:
                if not self.user.IsWindowVisible(hwnd):
                    return True
                count = self.user.GetWindowTextLengthW(hwnd)
                if count > 4096:
                    return True
                text = ctypes.create_unicode_buffer(count + 1)
                self.user.GetWindowTextW(hwnd, text, count + 1)
                if title not in text.value:
                    return True
                pid = W.DWORD()
                self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                handle = self.kernel.OpenProcess(0x1000, False, pid.value)
                if not handle:
                    raise CaptureError("PROCESS_UNAVAILABLE")
                try:
                    size = W.DWORD(32768)
                    image = ctypes.create_unicode_buffer(size.value)
                    if not self.kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                        raise CaptureError("PROCESS_UNAVAILABLE")
                    name = Path(image.value).name
                    if name.casefold() == process.casefold():
                        matches.append((hwnd, text.value, name, pid.value))
                finally:
                    self.kernel.CloseHandle(handle)
            except Exception:
                failed.append(True)
                return False
            return True
        callback = self.callback_type(visit)
        if not self.user.EnumWindows(callback, 0) or failed:
            raise CaptureError("ENUMERATION_FAILED", 2)
        return matches

    def geometry(self, hwnd):
        if self.user.IsIconic(hwnd):
            raise CaptureError("WINDOW_MINIMIZED")
        cloaked = W.DWORD()
        if self.dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4) != 0 or cloaked.value:
            raise CaptureError("WINDOW_CLOAKED")
        rect, client = W.RECT(), W.RECT()
        if not self.user.GetWindowRect(hwnd, ctypes.byref(rect)) or not self.user.GetClientRect(hwnd, ctypes.byref(client)):
            raise CaptureError("GEOMETRY_UNAVAILABLE")
        bounds = [rect.left, rect.top, rect.right, rect.bottom]
        size = [client.right - client.left, client.bottom - client.top]
        width, height = rect.right - rect.left, rect.bottom - rect.top
        dpi = self.user.GetDpiForWindow(hwnd)
        if min(width, height, *size, dpi) <= 0 or max(width, height) > 32768 or width * height > 100_000_000:
            raise CaptureError("WINDOW_SIZE_INVALID")
        return bounds, size, dpi

    def capture(self, hwnd, width, height):
        source = self.user.GetWindowDC(hwnd)  # NEVER GetDC(NULL) / desktop DC
        if not source:
            raise CaptureError("CAPTURE_FAILED")
        memory = bitmap = previous = None
        try:
            memory = self.gdi.CreateCompatibleDC(source)
            if not memory:
                raise CaptureError("CAPTURE_FAILED")
            # BITMAPINFOHEADER followed by unused RGBQUAD space; top-down BGRA.
            header = ctypes.create_string_buffer(struct.pack(
                "<IiiHHIIiiII", 40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0) + bytes(4))
            pixels = W.LPVOID()
            bitmap = self.gdi.CreateDIBSection(source, header, 0, ctypes.byref(pixels), None, 0)
            if not bitmap or not pixels.value:
                raise CaptureError("CAPTURE_FAILED")
            previous = self.gdi.SelectObject(memory, bitmap)
            if not previous or previous == ctypes.c_void_p(-1).value:
                previous = None
                raise CaptureError("CAPTURE_FAILED")
            if not self.user.PrintWindow(hwnd, memory, 2):  # PW_RENDERFULLCONTENT
                if not self.gdi.BitBlt(memory, 0, 0, width, height, source, 0, 0, 0x00CC0020):
                    raise CaptureError("CAPTURE_FAILED")
            if not self.gdi.GdiFlush():
                raise CaptureError("CAPTURE_FAILED")
            return ctypes.string_at(pixels, width * height * 4)
        finally:
            if previous:
                self.gdi.SelectObject(memory, previous)
            if bitmap:
                self.gdi.DeleteObject(bitmap)
            if memory:
                self.gdi.DeleteDC(memory)
            self.user.ReleaseDC(hwnd, source)


def output_folder(value):
    from sora_commander_next.policy import require_install_write_path
    import sora_commander_next
    path = Path(value)
    if not path.is_absolute() or any(p == ".." or ":" in p for p in path.parts[1:]):
        raise CaptureError("OUTPUT_NOT_ALLOWED")
    try:
        for part in (*reversed(path.parents), path):
            info = part.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise CaptureError("OUTPUT_NOT_ALLOWED")
        path = path.resolve(strict=True)
        require_install_write_path(str(path))
        package = Path(sora_commander_next.__file__).resolve().parent
        install = package.parent.parent if package.parent.name == "src" else package.parent
        if path.is_relative_to(install):
            raise CaptureError("OUTPUT_NOT_ALLOWED")
    except OSError as exc:
        raise CaptureError("OUTPUT_NOT_ALLOWED") from exc
    return path


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png(bgra, width, height):
    if len(bgra) != width * height * 4 or min(width, height) <= 0:
        raise CaptureError("PIXELS_INVALID")
    factor = min(1.0, 1600 / max(width, height))
    while True:
        w, h = max(1, int(width * factor)), max(1, int(height * factor))
        rows = bytearray()
        for y in range(h):
            rows.append(0)
            sy = min(height - 1, y * height // h)
            for x in range(w):
                offset = (sy * width + min(width - 1, x * width // w)) * 4
                b, g, r = bgra[offset:offset + 3]
                rows.extend((r, g, b))
        png = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
               + png_chunk(b"IDAT", zlib.compress(rows)) + png_chunk(b"IEND", b""))
        if len(png) < MAX_BYTES:
            return png, [w / width, h / height]
        factor *= 0.8


def retain_newest(folder):
    eligible = []
    for entry in folder.iterdir():
        if not CAPTURE_NAME.fullmatch(entry.name):
            continue
        info = entry.lstat()
        if (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and not getattr(info, "st_file_attributes", 0) & 0x400):
            eligible.append(entry)
    for entry in sorted(eligible, key=lambda p: p.name, reverse=True)[20:]:
        entry.unlink()  # exact pre-inspected script namespace only; no retries


def capture_window(process, title, out, *, api=None, now=None):
    if (not process or Path(process).name != process or any(c in process for c in "/\\:")
            or not title or len(title) > 1024):
        raise CaptureError("INVALID_TARGET", 2)
    folder = output_folder(out)
    api = WindowsAPI() if api is None else api
    api.dpi_aware()
    if not api.default_desktop():
        raise CaptureError("DESKTOP_NOT_DEFAULT")
    matches = api.matching(process, title)
    if len(matches) != 1:
        raise CaptureError("WINDOW_NOT_UNIQUE", 2)
    hwnd, window_title, actual_process, pid = matches[0]
    rect, client, dpi = api.geometry(hwnd)
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    pixels = api.capture(hwnd, width, height)
    # Fail closed if the target/desktop changed during the capture.
    if (not api.default_desktop() or api.matching(process, title) != matches
            or api.geometry(hwnd) != (rect, client, dpi)):
        raise CaptureError("WINDOW_CHANGED")
    png, scale = encode_png(pixels, width, height)
    instant = datetime.now(timezone.utc) if now is None else now
    stamp = instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if output_folder(out) != folder:
        raise CaptureError("OUTPUT_NOT_ALLOWED")
    path = folder / f"capture-{stamp}.png"
    try:
        with path.open("xb") as stream:
            stream.write(png)
    except FileExistsError as exc:
        raise CaptureError("OUTPUT_EXISTS") from exc
    retain_newest(folder)
    return {"status": "OK", "window_title": window_title, "process": actual_process,
            "pid": pid, "window_rect": rect, "client_size": client, "dpi": dpi,
            "scale": scale, "captured_at_utc": stamp, "png_path": str(path),
            "png_sha256": sha256(png).hexdigest(), "png_bytes": len(png),
            "next": "Read png_path with read_image_file, then call image(item)."}


def main(argv=None, *, api=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise CaptureError("INVALID_ARGUMENTS", 2)
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--process", required=True)
        parser.add_argument("--title", required=True)
        parser.add_argument("--out", required=True)
        args = parser.parse_args(argv)
        result = capture_window(args.process, args.title, args.out, api=api)
        code = 0
    except CaptureError as exc:
        result, code = {"status": "ERROR", "error": exc.code}, exc.exit_code
    except Exception:
        result, code = {"status": "ERROR", "error": "CAPTURE_FAILED"}, 3
    line = json.dumps(result, ensure_ascii=True)
    if len(line.encode("utf-8")) >= 8192:
        line, code = '{"status":"ERROR","error":"RESULT_TOO_LARGE"}', 3
    print(line)
    return code


if __name__ == "__main__":
    # -I intentionally ignores PYTHONPATH/cwd; use this script's own checkout.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    sys.exit(main())
