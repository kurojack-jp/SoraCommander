"""Approval v2 local dialog candidate, NOT activated.

This child displays a broker-supplied immutable command and returns one
decision via private stdin/stdout. It does not authenticate the Owner,
grant permissions, persist decisions or start a process. A protected broker
must independently validate Owner identity, nonce, request and session.
"""
import json
import re
import sys


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate field")
        result[name] = value
    return result


def parse_review(raw):
    """Strict bounded broker-to-UI message, containing no bearer credentials."""
    if type(raw) is not bytes or not 1 <= len(raw) <= 65536 or not raw.endswith(b"\n"):
        raise ValueError("invalid local review frame")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("malformed review frame") from exc
    required = {"nonce", "review_id", "spec_sha256", "owner_label",
                "executable", "argv", "cwd", "seconds", "timeout_ms",
                "output_limit", "allow_trusted", "unattended"}
    if type(data) is not dict or data.keys() != required:
        raise ValueError("invalid review schema")
    for key in ("nonce", "spec_sha256"):
        if type(data[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", data[key]) is None:
            raise ValueError("invalid review digest")
    if type(data["review_id"]) is not str or not 1 <= len(data["review_id"]) <= 128:
        raise ValueError("invalid review ID")
    if type(data["owner_label"]) is not str or not 1 <= len(data["owner_label"]) <= 256:
        raise ValueError("invalid Owner label")
    if (type(data["executable"]) is not str or
            not 1 <= len(data["executable"]) <= 1024 or
            type(data["cwd"]) is not str or not 1 <= len(data["cwd"]) <= 1024):
        raise ValueError("invalid command path")
    if (type(data["argv"]) is not list or len(data["argv"]) > 128
            or any(type(x) is not str or len(x) > 8192 or "\0" in x
                   for x in data["argv"])):
        raise ValueError("invalid command arguments")
    if type(data["allow_trusted"]) is not bool or type(data["unattended"]) is not bool:
        raise ValueError("explicit review switches required")
    if (type(data["seconds"]) not in (int, float)
            or not 0 < data["seconds"] <= 60
            or type(data["timeout_ms"]) is not int
            or not 10 <= data["timeout_ms"] <= 3600000
            or type(data["output_limit"]) is not int
            or not 128 <= data["output_limit"] <= 65536):
        raise ValueError("invalid execution limits")
    if "\0" in data["executable"] or "\0" in data["cwd"]:
        raise ValueError("invalid path")
    return data


def verify_review_reply_data_only(request, raw):
    """Bind a candidate child reply to an exact displayed review.

    This is only a wire-format check. It does NOT prove a human click, Owner
    identity, valid session, unchanged policy or dispatch authority.
    """
    if (type(request) is not dict or type(raw) is not bytes
            or not 1 <= len(raw) <= 512 or not raw.endswith(b"\n")):
        raise ValueError("invalid Owner review reply frame")
    try:
        response = json.loads(raw.decode("ascii"), object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid Owner review reply") from exc
    if (type(response) is not dict
            or response.keys() != {"nonce", "review_id", "spec_sha256", "decision"}
            or any(type(response[key]) is not str for key in response)
            or response["nonce"] != request.get("nonce")
            or response["review_id"] != request.get("review_id")
            or response["spec_sha256"] != request.get("spec_sha256")
            or response["decision"] not in {"ONCE", "TRUSTED", "DENY"}):
        raise ValueError("Owner review reply does not match displayed request")
    if response["decision"] == "TRUSTED" and request.get("allow_trusted") is not True:
        raise ValueError("TRUSTED was not offered to Owner")
    return response["decision"]


def review_text(request):
    """Render complete escaped command data plus explicit product risk boundary."""
    details = {
        "Owner": request["owner_label"],
        "Executable": request["executable"],
        "argv": request["argv"],
        "Working directory": request["cwd"],
        "Timeout limit (ms)": request["timeout_ms"],
        "Output limit (bytes)": request["output_limit"],
        "Review ID": request["review_id"],
        "Surface fingerprint": request["spec_sha256"],
        "Unattended run": request["unattended"],
    }
    rendered = json.dumps(details, ensure_ascii=True, indent=2)
    return (rendered + "\n\n"
            "IMPORTANT: Sora Commander approves the launch command only.\n"
            "It does NOT inspect scripts, dependencies, child commands or program effects.\n"
            "A TRUSTED approval permits the SAME launch command again even after\n"
            "a script or dependency is edited, until you explicitly revoke it.\n"
            "Closing this window or expiration rejects the current request.\n")


def main():
    request = parse_review(sys.stdin.buffer.readline(65537))
    import tkinter as tk
    import importlib.util
    from pathlib import Path
    # Candidate child runs as an isolated script; import only its fixed sibling.
    spec = importlib.util.spec_from_file_location(
        "_sora_owner_v2_visual_theme", Path(__file__).resolve().with_name("approval_visual_theme.py"))
    theme = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(theme)
    selected = {"decision": "DENY"}
    root = tk.Tk()
    def decide(value):
        selected["decision"] = value
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", lambda: decide("DENY"))
    root.after(round(request["seconds"] * 1000), lambda: (
        decide("DENY") if root.winfo_exists() else None))
    theme.shared_review(
        tk, root, kind="v2_candidate",
        title="Sora Commander Next — Owner approval v2 (LOCAL CANDIDATE)",
        heading="Owner approval v2 — LOCAL CANDIDATE (not active)",
        caution="Inspect every command argument. Close or expiry = DENY.",
        information=("TRUSTED approval can reuse a launch even after scripts change; "
                     "this candidate is NOT an active or authenticated approval path."),
        body=review_text(request), trusted_offered=request["allow_trusted"],
        reject=lambda: decide("DENY"), approve=lambda: decide("ONCE"),
        trusted=(lambda: decide("TRUSTED")) if request["allow_trusted"] else None)
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
    response = {
        "nonce": request["nonce"], "review_id": request["review_id"],
        "spec_sha256": request["spec_sha256"],
        "decision": selected["decision"],
    }
    sys.stdout.buffer.write((json.dumps(response, separators=(",", ":")) + "\n").encode("ascii"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
