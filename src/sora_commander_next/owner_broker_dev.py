"""DEV_ONLY pieces that give the Host-and-Broker split the same tools as today.

DevBrokerHost and RemoteEditPilot run in the MCP Host process and only forward.
OwnerBrokerBackend runs in the Owner Broker process, around the existing Host,
which keeps the review windows, jobs and file writers. Nothing starts these, and
a product composition never selects them. Interactive stdin is not forwarded.
"""
from .broker_host import MAX_REPLY_BYTES, BrokerHost
from .file_edits import StalePreviewError


class RemoteEditPilot:
    """Same calls as the disposable edit pilot, answered by the Broker."""

    def __init__(self, host, *, enable_create):
        if type(enable_create) is not bool:
            raise ValueError("explicit boolean create opt-in required")
        self._host = host
        self.enable_create = enable_create

    def request(self, path, content, expected_sha256):
        return self._host._call("request_text_edit", path=path, content=content,
                                expected_sha256=expected_sha256)

    def request_create(self, path, content):
        return self._host._call("request_text_create", path=path, content=content)

    def status(self, pending_id):
        return self._host._call("edit_review_status", pending_id=pending_id)


class DevBrokerHost(BrokerHost):
    """Personal DEV MCP Host. It holds no jobs, windows or file writers."""

    dev_broker_client = True
    _max_request_bytes = MAX_REPLY_BYTES

    def __init__(self, transport, *, read_roots=(), read_only_roots=(),
                 edit_pilot=False, create_pilot=False):
        super().__init__(transport, read_roots=read_roots, read_only_roots=read_only_roots)
        if type(edit_pilot) is not bool or type(create_pilot) is not bool:
            raise ValueError("explicit boolean pilot opt-in required")
        if create_pilot and not edit_pilot:
            raise ValueError("create pilot requires the edit pilot")
        self._edit_review_pilot = (RemoteEditPilot(self, enable_create=create_pilot)
                                   if edit_pilot else None)

    def ping(self):
        return {"status": "OK", "remote_ready": False, "host_role": "DEV_BROKER_CLIENT"}

    def status(self):
        # remote_ready stays false here, whatever the Broker answers.
        result = self._call("status")
        result = dict(result) if type(result) is dict else {}
        return {**result, "remote_ready": False, "host_role": "DEV_BROKER_CLIENT"}

    def recovery_overview(self):
        return self._call("recovery_overview")

    def dev_mutate(self, operation, **arguments):
        return self._call(operation, **arguments)

    def _call(self, action, **arguments):
        # The Broker repeats admission against its own startup snapshot. A
        # narrower client snapshot must not forward writes/cwd in its read-only roots.
        fields = {"run_command": ("cwd",), "run_command_wait": ("cwd",),
                  "create_directory": ("path",), "create_file": ("path",),
                  "move_file": ("source", "destination"),
                  "request_source_patch_dev": ("path",),
                  "request_source_create": ("path",), "request_source_patch": ("path",),
                  "request_text_edit": ("path",), "request_text_create": ("path",)}
        try:
            for field in fields.get(action, ()):
                self.policy.require_full_path(arguments.get(field))
        except (OSError, ValueError, TypeError):
            return {"status": "DENY", "error": "FullScopeRequired"}
        try:
            for field in fields.get(action, ()):
                if field != "cwd":
                    self.policy.require_write_path(arguments.get(field))
        except (OSError, ValueError, TypeError):
            return {"status": "REJECTED", "outcome": "NOT_STARTED",
                    "error": "RunningInstallWriteDenied", "retry_allowed": False}
        return super()._call(action, **arguments)


class OwnerBrokerBackend:
    """The actions OwnerBrokerService may reach, on the existing Host."""

    def __init__(self, host):
        self._host = host

    def __getattr__(self, name):
        # Only the existing Host's command, job and recovery methods.
        if name in ("run_command", "run_command_wait", "await_command_result",
                    "review_status", "request_status", "read_process_output",
                    "await_process_output", "stop_process", "stop_process_wait",
                    "list_sessions", "previous_sessions", "previous_output",
                    "status", "recovery_overview"):
            return getattr(self._host, name)
        raise AttributeError(name)

    def _mutate(self, operation, **arguments):
        from .dev_rdc_compat import mutate
        with self._host.policy.lock:
            try:
                fields = ("source", "destination") if operation == "move_file" else ("path",)
                for field in fields:
                    self._host.policy.require_write_path(arguments.get(field))
            except (OSError, ValueError, TypeError):
                return {"status": "REJECTED", "outcome": "NOT_STARTED",
                        "error": "DevOperationFailed", "retry_allowed": False}
            return mutate(operation, tuple(self._host.policy.roots), **arguments)

    def create_directory(self, **arguments):
        return self._mutate("create_directory", **arguments)

    def create_file(self, **arguments):
        return self._mutate("create_file", **arguments)

    def move_file(self, **arguments):
        return self._mutate("move_file", **arguments)

    def request_source_patch_dev(self, **arguments):
        return self._mutate("request_source_patch_dev", **arguments)

    def _pilot(self):
        pilot = getattr(self._host, "_edit_review_pilot", None)
        if pilot is None:
            raise PermissionError("edit pilot not attached")
        return pilot

    def request_text_edit(self, *, path, content, expected_sha256):
        # The same fixed results as the MCP adapter gives in one process today.
        try:
            self._host.policy.require_write_path(path)
            return self._pilot().request(path, content, expected_sha256)
        except StalePreviewError:
            return {"status": "ERROR", "error": "STALE_SHA256"}
        except Exception:
            return {"status": "ERROR", "error": "EDIT_REJECTED"}

    def request_text_create(self, *, path, content):
        try:
            self._host.policy.require_write_path(path)
            return self._pilot().request_create(path, content)
        except Exception:
            return {"status": "ERROR", "error": "CREATE_REJECTED"}

    def edit_review_status(self, *, pending_id):
        return self._pilot().status(pending_id)
