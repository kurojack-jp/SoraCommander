"""DEV Owner lease indicator. Runs standalone under isolated Python."""

import ctypes as ct
import ipaddress
from math import ceil, isfinite
import re
import sys
import time


# Keep the standalone executable independent of sys.path and VM transport.
# The configuration loader uses the prototype validators directly.
_TOOLS = frozenset({
    "click", "double_click", "move", "drag", "scroll", "type", "keypress",
    "screenshot", "wait", "elements", "file_put", "file_get",
})
_NETWORKS = tuple(ipaddress.IPv4Network(spec) for spec in (
    (10 << 24, 8), (172 << 24 | 16 << 16, 12), (192 << 24 | 168 << 16, 16),
))
_OPTIONS = {
    "--lease-id", "--vm", "--address", "--minutes", "--max-inputs",
    "--tools", "--screenshots-are-sent-to-chatgpt", "--deadline",
}


def _indicator_name(lease_id):
    return "Local\\SoraCommanderNext-VmLease-" + lease_id


def hold_indicator(lease_id):
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    create, close = kernel.CreateEventW, kernel.CloseHandle
    create.argtypes = [ct.c_void_p, ct.c_int, ct.c_int, ct.c_wchar_p]
    create.restype = ct.c_void_p
    close.argtypes = [ct.c_void_p]
    ct.set_last_error(0)
    handle = create(None, False, False, _indicator_name(lease_id))
    if not handle:
        raise ct.WinError(ct.get_last_error())
    if ct.get_last_error() == 183:
        close(handle)
        raise ValueError("lease indicator already exists")
    return lambda: close(handle)


def indicator_alive(lease_id):
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    open_event, close = kernel.OpenEventW, kernel.CloseHandle
    open_event.argtypes = [ct.c_ulong, ct.c_int, ct.c_wchar_p]
    open_event.restype = ct.c_void_p
    close.argtypes = [ct.c_void_p]
    handle = open_event(0x00100000, False, _indicator_name(lease_id))  # SYNCHRONIZE
    # Never retain a reference in the child. The kernel removes the name when
    # the client closes its handle, including on abnormal process exit.
    return bool(handle and close(handle))


def _arguments(argv):
    values = dict(zip(argv[::2], argv[1::2]))
    if len(argv) != 2 * len(_OPTIONS) or values.keys() != _OPTIONS:
        raise ValueError
    lease_id = values["--lease-id"]
    vm = values["--vm"]
    address = ipaddress.IPv4Address(values["--address"])
    minutes, inputs = values["--minutes"], values["--max-inputs"]
    tools = values["--tools"].split(",")
    deadline = float(values["--deadline"])
    if (not re.fullmatch("[0-9a-f]{32}", lease_id)
            or not 1 <= len(vm) <= 100 or not vm.isprintable()
            or not any(address in network and address not in (
                network.network_address, network.broadcast_address) for network in _NETWORKS)
            or not re.fullmatch("[1-9][0-9]?", minutes) or int(minutes) > 30
            or not re.fullmatch("[1-9][0-9]{0,2}", inputs) or int(inputs) > 200
            or len(set(tools)) != len(tools) or not set(tools) <= _TOOLS
            or "screenshot" not in tools
            or not isfinite(deadline) or deadline <= 0
            or values["--screenshots-are-sent-to-chatgpt"] != "yes"):
        raise ValueError
    return lease_id, vm, int(minutes), deadline


def _window(tk, lease_id, vm, deadline, clock, output, alive):
    root = tk.Tk()
    outcome = None
    failed = False
    hidden = 0

    def finish():
        nonlocal outcome
        outcome = "EXPIRED" if clock() >= deadline else "STOPPED"
        root.quit()

    def callback_failed(*_):
        nonlocal failed
        failed = True
        root.quit()

    def tick():
        nonlocal hidden
        if not alive(lease_id):
            finish()
            return
        # The Owner must be able to reach Stop: bring it back, and end the lease if that fails.
        hidden = 0 if root.winfo_viewable() else hidden + 1
        if hidden:
            root.deiconify()
        if hidden > 8:
            callback_failed()
            return
        remaining = deadline - clock()
        if remaining <= 0:
            finish()
            return
        seconds = ceil(remaining)
        timer.configure(text=f"残り時間 / Time left: {seconds // 60:02d}:{seconds % 60:02d}")
        root.after(max(1, min(250, int(remaining * 1000))), tick)

    try:
        root.report_callback_exception = callback_failed
        root.title("VM リース / VM Lease")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        tk.Label(root, text=f"VM 操作許可中 / VM lease active\n{vm}", wraplength=360,
                 padx=24, pady=12).pack()
        timer = tk.Label(root, pady=6)
        timer.pack()
        tk.Button(root, text="停止 / Stop", command=finish,
                  width=22, height=3).pack(padx=24, pady=(6, 18))
        root.protocol("WM_DELETE_WINDOW", finish)
        root.update_idletasks()
        if not root.winfo_viewable():
            return None
        tick()
        if outcome is None:
            print(f"LEASE {lease_id} STARTED", file=output, flush=True)
            root.mainloop()
    finally:
        root.destroy()
    return None if failed else outcome


def main(argv=None, *, toolkit=None, clock=time.monotonic, output=None, alive=indicator_alive):
    try:
        lease_id, vm, minutes, deadline = _arguments(sys.argv[1:] if argv is None else argv)
    except ValueError:
        return 2
    if output is None:
        output = sys.stdout
    try:
        now = clock()
        if not now < deadline <= now + minutes * 60 or not alive(lease_id):
            return 1
        if toolkit is None:
            import tkinter as toolkit
        outcome = _window(toolkit, lease_id, vm, deadline, clock, output, alive)
        if outcome is None:
            return 1
        print(outcome, file=output, flush=True)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
