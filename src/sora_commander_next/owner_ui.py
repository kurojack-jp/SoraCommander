"""LOCAL DEMO ONLY: same-process UI, NOT authenticated OS Owner access.

No remote route, import-time GUI, logging, or automatic approval. Production needs
Windows user-session binding and isolation from remote/untrusted Python callers.
"""

from pathlib import Path

from .policy import Decision


class OwnerController:
    def __init__(self, host):
        from .broker_host import BrokerHost
        if isinstance(host, BrokerHost):
            raise PermissionError("legacy Owner UI cannot attach to transport-only Host")
        self.host = host

    def pending_local(self):
        return self.host.approval.pending_local()

    def approve_local(self, pending_id):
        host = self.host
        try:
            with host.policy.lock:
                record = host.approval.consume_local(pending_id, host._context())
                if host.policy.classify(record.request, host.owner) != Decision.REVIEW:
                    raise PermissionError("request no longer REVIEW")
                if host.approval.clock() >= record.expires_at:
                    raise PermissionError("review expired during classification")
                return {**host.jobs.start(record.request), "decision": "REVIEW"}
        except Exception as exc:
            return {"status": "ERROR", "error": type(exc).__name__}

    def reject_local(self, pending_id):
        try:
            self.host.approval.reject_local(pending_id)
            return {"status": "REJECTED"}
        except Exception as exc:
            return {"status": "ERROR", "error": type(exc).__name__}


class OwnerView:
    """Create explicitly in a trusted local demo; caller owns mainloop()."""

    def __init__(self, controller):
        import tkinter as tk

        self.controller, self.records = controller, ()
        self.window = tk.Tk()
        self.window.title("Sora Commander Next — LOCAL DEMO ONLY")
        tk.Label(self.window, text="LOCAL DEMO ONLY: OS Owner identity is NOT verified.").pack()
        self.items = tk.Listbox(self.window, width=90, exportselection=False)
        self.items.pack(fill="both", expand=True)
        self.details = tk.Text(self.window, width=90, height=14, state="disabled")
        self.details.pack(fill="both", expand=True)
        self.items.bind("<<ListboxSelect>>", self._select)
        tk.Button(self.window, text="Approve once", command=lambda: self._act(True)).pack()
        tk.Button(self.window, text="Reject", command=lambda: self._act(False)).pack()
        self.result = tk.StringVar(value="Select a pending request to inspect its exact command.")
        tk.Label(self.window, textvariable=self.result).pack()
        self._refresh()

    def _refresh(self):
        records = self.controller.pending_local()
        if records != self.records:
            self.records = records
            self.items.delete(0, "end")
            for p in records:
                self.items.insert("end", f"{p.pending_id[:8]}  {Path(p.request.executable).name}")
            self._select()
        self.window.after(500, self._refresh)

    def _select(self, event=None):
        selected = self.items.curselection()
        text = ""
        if selected:
            p = self.records[selected[0]]
            r = p.request
            text = (f"Executable: {r.executable!r}\nargv (literal): {r.argv!r}\ncwd: {r.cwd!r}\n"
                    f"Local Owner label (unverified): {self.controller.host.owner!r}\n"
                    f"Configured roots: {self.controller.host.policy.roots!r}\n"
                    f"Timeout: {r.timeout}s; output: {r.output_limit} bytes\n"
                    f"Expires in: {max(0, p.expires_at - self.controller.host.approval.clock()):.0f}s\n"
                    "Effects: unclassified command; inspect every argument before approving.\n"
                    "Approval starts one background job. Descendant cleanup is unsupported.")
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def _act(self, approve):
        selected = self.items.curselection()
        if not selected:
            return
        pending_id = self.records[selected[0]].pending_id
        method = self.controller.approve_local if approve else self.controller.reject_local
        result = method(pending_id)
        self.result.set(result["status"])
