"""Explicit local Owner click dialog child. Private anonymous stdin/stdout only.

No MCP/network/clipboard; only bundled local presentation assets are read.
No approval bypass, no launch on import.
A response is emitted only after a real Tkinter button click (or close=DENY).
"""
import json
import re
import sys


def parse_request(raw):
    if type(raw) is not bytes or len(raw)>131072 or not raw.endswith(b"\n"):
        raise ValueError("invalid private UI request")
    payload=json.loads(raw.decode("utf-8"))
    if (type(payload) is not dict
            or set(payload)!={"nonce","executable","argv","cwd","seconds","pending_id",
                                "timeout","output_limit"}
            or type(payload["nonce"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}",payload["nonce"]) is None
            or type(payload["executable"]) is not str
            or not payload["executable"]
            or type(payload["argv"]) is not list
            or any(type(v) is not str for v in payload["argv"])
            or type(payload["cwd"]) is not str
            or type(payload["pending_id"]) is not str
            or type(payload["seconds"]) not in (int,float)
            or not 0 < payload["seconds"] <= 60
            or type(payload["timeout"]) not in (int,float)
            or type(payload["output_limit"]) is not int):
        raise ValueError("invalid UI request fields")
    return payload


def main():
    request=parse_request(sys.stdin.buffer.readline(131073))
    # No operation is performed on the Owner PC from this child. Only
    # returning a one-time decision is possible via private stdout pipe.
    import tkinter as tk
    # The broker invokes this file with -I, outside package import context.
    # Load only the fixed sibling presentation module; never alter sys.path.
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location(
        "_sora_approval_visual_theme", Path(__file__).resolve().with_name("approval_visual_theme.py"))
    theme=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(theme)
    # No click means no decision. Never mislabel expiry as an Owner rejection.
    selected={"decision":"EXPIRED"}
    root=tk.Tk()
    data=(f"Executable: {request['executable']!r}\n"
          f"argv (literal): {request['argv']!r}\n"
          f"Working directory: {request['cwd']!r}\n"
          f"Timeout: {request['timeout']} s\n"
          f"Output limit: {request['output_limit']} bytes\n"
          f"Review ID: {request['pending_id']!r}\n"
          f"Expires in at most {request['seconds']:.0f} seconds.\n"
          "Unknown command effects: inspect ALL fields; approval can have side effects.\n"
          "Not a remote MCP approval endpoint.\n")
    def decide(value):
        selected["decision"]=value
        root.destroy()
    theme.shared_review(
        tk, root, kind="command",
        title="Sora Commander Next — Owner approval, local ONLY",
        heading="ONE-TIME command approval — local Windows session",
        caution="Read the ENTIRE command before clicking Approve. Close = DENY.",
        information="時間切れは却下ではありません。操作は実行せず、次の指示を待ちます。 Timeout != Reject.",
        body=data, reject=lambda: decide("DENY"), approve=lambda: decide("APPROVE"))
    root.protocol("WM_DELETE_WINDOW",lambda:decide("DENY"))
    root.after(int(request["seconds"]*1000),lambda:decide("EXPIRED"))
    # Non-blocking Windows system alert when the Owner review appears.
    # Audio failure must never approve, deny, or change the expiry clock.
    def alert_review():
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            try:
                root.bell()
            except Exception:
                pass
    root.after(100, alert_review)
    root.mainloop()
    result={"nonce":request["nonce"],"decision":selected["decision"]}
    sys.stdout.buffer.write(json.dumps(result).encode("ascii")+b"\n")
    sys.stdout.buffer.flush()


if __name__=="__main__":
    main()
