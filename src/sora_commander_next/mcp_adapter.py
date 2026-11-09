"""Optional SDK boundary. No HTTP, listener, remote grants or import-time startup."""

from .host import Host
from .broker_host import BrokerHost, validate_source_arguments
from .commands import Command
from .plugin_icon import pinned_plugin_icon
from .file_reads import (list_folder as list_folder_readonly,
                         list_folder_page as list_folder_page_readonly,
                         read_text_file as read_text_readonly,
                         read_text_page as read_text_page_readonly,
                         find_files as find_files_readonly,
                         search_code as search_code_readonly,
                         path_info as path_info_readonly,
                         read_text_lines as read_text_lines_readonly,
                         read_many_text_files as read_many_text_files_readonly,
                         compare_text_files as compare_text_files_readonly,
                         read_text_chunk as read_text_chunk_readonly,
                         sha256_file as sha256_file_readonly)
from .file_edits import (preview_text_edit as preview_edit_readonly,
                         preview_source_create as preview_source_create_readonly,
                         preview_source_patch as preview_source_patch_readonly,
                         StalePreviewError)
from typing import Any, Annotated
from pydantic import Field
import math
from time import monotonic, sleep
import sys
import importlib.util


def _local_registered_catalog(server, host):
    """Describe *this SDK server's* registered tools, never remote visibility.

    Tool signatures are a diagnostic fingerprint, not signed build identity,
    a secure release attestation, Owner consent, or a Broker trust result.
    Return no filesystem paths, tool defaults with secrets, or process IDs.
    """
    import hashlib
    import json
    from importlib.metadata import PackageNotFoundError, version

    tools = server._tool_manager.list_tools()
    if len(tools) > 96:
        raise RuntimeError("tool catalog exceeds diagnostic limit")
    definitions = sorted(
        ({"name": item.name, "input_schema": item.parameters} for item in tools),
        key=lambda item: item["name"],
    )
    names = [item["name"] for item in definitions]
    if len(names) != len(set(names)):
        raise RuntimeError("duplicate registered tool name")
    serialized = json.dumps(
        definitions, sort_keys=True, ensure_ascii=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("ascii")
    if len(serialized) > 128 * 1024:
        raise RuntimeError("tool catalog too large")
    try:
        installed_version = version("sora-commander-next")
    except PackageNotFoundError:
        installed_version = "UNKNOWN"
    return {
        "status": "LOCAL_REGISTRATION_SNAPSHOT",
        "registered_tool_count": len(names),
        "registered_tool_names": names,
        "tool_schema_sha256": hashlib.sha256(serialized).hexdigest(),
        "installed_distribution_version": installed_version,
        "read_roots_configured": bool(host.policy.roots),
        "edit_pilot_attached": getattr(host, "_edit_review_pilot", None) is not None,
        "chatgpt_tool_visibility": "UNVERIFIED",
        "protected_broker_acceptance": "NOT_ESTABLISHED_BY_CATALOG",
    }


def _validate_arguments(name, arguments):
    """Validate wire types before SDK coercion; never echo untrusted values."""
    if name in {"request_source_create", "request_source_patch", "source_review_status", "cancel_source_review"}:
        validate_source_arguments(name, arguments)
        return
    fields = {
        "run_command": ({"executable", "argv", "cwd"},
                        {"timeout", "output_limit", "background", "request_id", "interactive"}),
        "run_command_wait": ({"executable", "argv", "cwd"},
                             {"timeout", "output_limit", "wait_seconds", "request_id", "interactive"}),
        "send_input": ({"session_id", "input_text", "input_id"}, set()),
        "await_command_result": ({"pending_id"}, {"wait_seconds"}),
        "review_status": ({"pending_id"}, set()),
        "request_status": ({"request_id"}, set()),
        "preview_text_edit": ({"path", "content", "expected_sha256"}, set()),
        "preview_source_create": ({"path", "content"}, set()),
        "preview_source_patch": ({"path", "old_text", "new_text", "expected_sha256"}, set()),
        "request_source_patch_dev": ({"path", "old_text", "new_text", "expected_sha256"}, set()),
        "create_directory": ({"path"}, set()),
        "create_file": ({"path", "content"}, set()),
        "move_file": ({"source", "destination"}, set()),
        "list_processes": (set(), {"limit"}),
        "get_config": (set(), set()),
        "request_text_edit": ({"path", "content", "expected_sha256"}, set()),
        "request_text_edit_wait": ({"path", "content", "expected_sha256"},
                                   {"wait_seconds"}),
        "request_text_create": ({"path", "content"}, set()),
        "request_text_create_wait": ({"path", "content"}, {"wait_seconds"}),
        "edit_review_status": ({"pending_id"}, set()),
        "await_edit_result": ({"pending_id"}, {"wait_seconds"}),
        "read_process_output": ({"session_id"}, {"offset"}),
        "await_process_output": ({"session_id"}, {"wait_seconds"}),
        "stop_process": ({"session_id"}, set()),
        "stop_process_wait": ({"session_id"}, {"wait_seconds"}),
        "list_sessions": (set(), set()),
        "previous_sessions": (set(), set()),
        "recovery_overview": (set(), set()),
        "previous_output": (set(), {"session_id"}),
        "list_folder": ({"path"}, {"limit", "depth"}),
        "list_folder_page": ({"path"}, {"offset", "limit"}),
        "read_text_file": ({"path"}, {"max_bytes"}),
        "read_text_page": ({"path"}, {"offset", "max_bytes"}),
        "find_files": ({"path", "name"}, {"max_results", "depth"}),
        "search_code": ({"path", "text"}, {"max_results", "depth"}),
        "path_info": ({"path"}, set()),
        "read_text_lines": ({"path"}, {"start_line", "max_lines"}),
        "read_many_text_files": ({"paths"}, {"max_bytes_per_file"}),
        "compare_text_files": ({"before_path", "after_path"}, {"context_lines"}),
        "read_text_chunk": ({"path"}, {"offset", "max_bytes"}),
        "sha256_file": ({"path"}, set()),
        "ping": (set(), set()), "status": (set(), set()),
        "tool_catalog": (set(), set()),
    }
    required, optional = fields[name]
    if not isinstance(arguments, dict) or not required <= arguments.keys() or arguments.keys() - required - optional:
        raise ValueError("invalid fields")
    if name == "send_input":
        from .dev_rdc_compat import validate_input
        validate_input(**arguments)
    if name in {"create_directory", "create_file", "move_file", "request_source_patch_dev"}:
        from .dev_rdc_compat import _path
        for key in (("source", "destination") if name == "move_file" else ("path",)):
            _path(arguments[key])
    if name == "list_processes" and (type(arguments.get("limit", 100)) is not int
            or not 1 <= arguments.get("limit", 100) <= 200):
        raise ValueError("invalid process limit")
    if name == "list_folder" and (type(arguments.get("depth", 0)) is not int
            or not 0 <= arguments.get("depth", 0) <= 4):
        raise ValueError("invalid listing depth")
    read_paths = {
        "read_text_page": ("path",), "list_folder_page": ("path",),
        "find_files": ("path",), "search_code": ("path",),
        "path_info": ("path",), "read_text_lines": ("path",),
        "read_text_chunk": ("path",), "sha256_file": ("path",),
        "compare_text_files": ("before_path", "after_path"),
    }
    for key in read_paths.get(name, ()):
        value = arguments[key]
        if type(value) is not str or not value or "\0" in value:
            raise ValueError("invalid read path")
    if name == "read_many_text_files":
        names = arguments["paths"]
        if (type(names) is not list or not 1 <= len(names) <= 8
                or any(type(p) is not str or not p or "\0" in p for p in names)
                or len(set(names)) != len(names)
                or type(arguments.get("max_bytes_per_file", 8192)) is not int
                or not 1 <= arguments.get("max_bytes_per_file", 8192) <= 8192):
            raise ValueError("invalid batch read")
    if name == "compare_text_files":
        if (any(type(arguments[k]) is not str or not arguments[k]
                for k in ("before_path", "after_path"))
                or type(arguments.get("context_lines", 3)) is not int
                or not 0 <= arguments.get("context_lines", 3) <= 8):
            raise ValueError("invalid read-only diff")
    if name == "read_text_chunk" and (
            type(arguments.get("offset", -1)) is not int
            or not -1 <= arguments.get("offset", -1) <= 256 * 1024 * 1024
            or type(arguments.get("max_bytes", 16384)) is not int
            or not 4 <= arguments.get("max_bytes", 16384) <= 65536):
        raise ValueError("invalid stream read bounds")
    if name in {"run_command", "run_command_wait"}:
        if name == "run_command" and type(arguments.get("background", False)) is not bool:
            raise ValueError("invalid background")
        if not isinstance(arguments["argv"], list):
            raise ValueError("invalid argv")
        if "request_id" in arguments:
            rid = arguments["request_id"]
            if (type(rid) is not str or len(rid) != 32
                    or any(c not in "0123456789abcdef" for c in rid)):
                raise ValueError("invalid request id")
        Command(**{key: value for key, value in arguments.items()
                   if key not in ("background", "wait_seconds")})
    if name in {"run_command_wait", "await_command_result",
                "await_process_output", "stop_process_wait",
                "request_text_edit_wait", "request_text_create_wait",
                "await_edit_result"}:
        wait_seconds = arguments.get("wait_seconds", 50.0)
        if (type(wait_seconds) not in (int, float)
                or not math.isfinite(wait_seconds)
                or not 0 <= wait_seconds <= 90):
            raise ValueError("invalid wait seconds")
    if name == "request_status":
        rid = arguments["request_id"]
        if (type(rid) is not str or len(rid) != 32
                or any(c not in "0123456789abcdef" for c in rid)):
            raise ValueError("invalid request id")
    if name in {"review_status", "edit_review_status", "await_command_result",
                "await_edit_result"} and type(arguments["pending_id"]) is not str:
        raise ValueError("invalid pending ID")
    if name in {"read_process_output", "await_process_output",
                "stop_process", "stop_process_wait"}:
        if not isinstance(arguments["session_id"], str):
            raise ValueError("invalid session ID")
    if name == "previous_output" and "session_id" in arguments:
        sid = arguments["session_id"]
        if (type(sid) is not str or len(sid) != 48
                or any(c not in "0123456789abcdef" for c in sid)):
            raise ValueError("invalid archived session")
    if name == "read_process_output" and type(arguments.get("offset", 0)) is not int:
        raise ValueError("invalid offset")
    if name in {"preview_source_patch", "request_source_patch_dev"} and (
            type(arguments["path"]) is not str or not arguments["path"]
            or "\0" in arguments["path"]
            or type(arguments["old_text"]) is not str or not arguments["old_text"]
            or type(arguments["new_text"]) is not str
            or len(arguments["old_text"]) > 4096
            or len(arguments["new_text"]) > 4096
            or type(arguments["expected_sha256"]) is not str
            or len(arguments["expected_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in arguments["expected_sha256"])):
        raise ValueError("invalid source patch preview")
    if name in {"preview_source_patch", "request_source_patch_dev"}:
        if any(len(arguments[key].encode("utf-8")) > 4096 for key in ("old_text", "new_text")):
            raise ValueError("invalid literal byte length")
    if name == "preview_source_create" and (
            type(arguments["path"]) is not str or not arguments["path"]
            or "\0" in arguments["path"]
            or type(arguments["content"]) is not str
            or len(arguments["content"]) > 32768):
        raise ValueError("invalid source creation preview")
    if name in {"preview_text_edit", "request_text_edit",
                "request_text_edit_wait"}:
        if any(type(arguments[k]) is not str for k in ("path", "content", "expected_sha256")):
            raise ValueError("invalid preview")
    if name in {"request_text_create", "request_text_create_wait"}:
        if any(type(arguments[k]) is not str for k in ('path', 'content')):
            raise ValueError('invalid create')
    if name == "create_file" and (
            type(arguments["path"]) is not str or not arguments["path"]
            or "\0" in arguments["path"]
            or type(arguments["content"]) is not str
            or len(arguments["content"].encode("utf-8")) > 65536):
        raise ValueError("invalid file creation")
    if name in {"list_folder", "list_folder_page", "read_text_file", "read_text_page",
                "find_files", "search_code", "path_info", "read_text_lines"}:
        if type(arguments["path"]) is not str or not arguments["path"]:
            raise ValueError("invalid path")
        if name in {"list_folder", "list_folder_page", "read_text_file", "read_text_page"}:
            k = "limit" if name in {"list_folder", "list_folder_page"} else "max_bytes"
            if k in arguments and (type(arguments[k]) is not int
                    or not 1 <= arguments[k] <= (200 if k == "limit" else 65536)):
                raise ValueError("invalid read bound")
        if name == "read_text_page" and (type(arguments.get("offset", 0)) is not int
                                         or not 0 <= arguments.get("offset", 0) <= 4 * 1024 * 1024):
            raise ValueError("invalid page offset")
        if name == "list_folder_page" and (type(arguments.get("offset", 0)) is not int
                                           or not 0 <= arguments.get("offset", 0) <= 10000):
            raise ValueError("invalid folder offset")
        if name == "read_text_lines" and (
                type(arguments.get("start_line", 1)) is not int
                or not 1 <= arguments.get("start_line", 1) <= 1000000
                or type(arguments.get("max_lines", 50)) is not int
                or not 1 <= arguments.get("max_lines", 50) <= 100):
            raise ValueError("invalid line page")
        if name in {"find_files", "search_code"}:
            key = "name" if name == "find_files" else "text"
            value = arguments[key]
            if (type(value) is not str or not 1 <= len(value) <= 120
                    or "\0" in value or (name == "search_code" and any(
                        c in value for c in ("\r", "\n")))
                    or type(arguments.get("max_results", 50)) is not int
                    or not 1 <= arguments.get("max_results", 50) <= 100
                    or type(arguments.get("depth", 8)) is not int
                    or not 0 <= arguments.get("depth", 8) <= 12):
                raise ValueError("invalid search parameters")


def _await_edit_data_only(pilot, pending_id, wait_seconds):
    """Observe one existing disposable edit review, NEVER reissue or approve.

    The Owner's local dialog/verification is solely inside EditReviewPilot.
    A timed-out MCP call must retain pending_id for a later read-only wait.
    """
    if type(pending_id) is not str or len(pending_id) != 32:
        return {"status": "ERROR", "error": "InvalidPendingId"}
    if (type(wait_seconds) not in (int, float)
            or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 90):
        return {"status": "ERROR", "error": "InvalidWaitSeconds"}
    deadline = monotonic() + wait_seconds
    while True:
        try:
            result = pilot.status(pending_id)
            if type(result) is not dict or type(result.get("status")) is not str:
                raise ValueError("unexpected edit review result")
        except Exception:
            return {"status": "ERROR", "error": "EditStatusUnavailable",
                    "pending_id": pending_id}
        if result["status"] != "PENDING":
            return {"pending_id": pending_id, **result}
        left = deadline - monotonic()
        if left <= 0:
            return {"status": "PENDING", "pending_id": pending_id}
        sleep(min(.10, left))


def _await_submitted_edit_data_only(pilot, initial, wait_seconds):
    """Never send a second write request while observing one result."""
    if initial.get("status") != "REVIEW":
        return initial
    pending_id = initial.get("pending_id")
    return _await_edit_data_only(pilot, pending_id, wait_seconds)


def build_server(host: Host, *, approved_icon_path=None, approved_icon_sha256=None):
    # Never accept icon path/URI over MCP wire. Local release setup supplies
    # both the PNG and an independently trusted hash; both or neither.
    if (approved_icon_path is None) != (approved_icon_sha256 is None):
        raise ValueError("icon path and trusted digest must be supplied together")
    icon_metadata = (pinned_plugin_icon(approved_icon_path,
                     expected_sha256=approved_icon_sha256)
                     if approved_icon_path is not None else None)
    # A parent package explicitly blocked in sys.modules means the SDK is
    # unavailable even if a submodule was cached earlier by another test.
    if "mcp" in sys.modules and sys.modules["mcp"] is None:
        raise RuntimeError("Optional MCP SDK unavailable; pure core remains usable")
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("Optional MCP SDK unavailable; pure core remains usable") from exc
    # Old local test adapters and SDK revisions still work without branding.
    # A requested icon, however, MUST be supported and must never be ignored.
    registered_icons = []
    if icon_metadata is not None:
        try:
            from mcp.types import Icon
        except ImportError as exc:
            raise RuntimeError("MCP SDK does not support pinned icons") from exc
        registered_icons = [Icon(**icon_metadata)]
    class LocalFastMCP(FastMCP):
        async def call_tool(self, name, arguments):
            # SDK validation normally coerces bool/numbers and ignores extra fields.
            # Sanitize failures before SDK logging can include validation inputs.
            from mcp.server.fastmcp.exceptions import ToolError
            try:
                _validate_arguments(name, arguments)
            except Exception:
                raise ToolError("Invalid tool request") from None
            try:
                return await super().call_tool(name, arguments)
            except Exception:
                raise ToolError("Tool request failed") from None

    if registered_icons:
        server = LocalFastMCP("Sora Commander Next (local demo)",
                              icons=registered_icons)
    else:
        server = LocalFastMCP("Sora Commander Next (local demo)")

    def tool_decorator():
        return server.tool(icons=registered_icons) if registered_icons else server.tool()

    def read_errors_only(function):
        # Sanitize before FastMCP catches/logs the exception, not only afterward.
        from functools import wraps
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception:
                raise ValueError("Read request failed") from None
        return wrapped

    @tool_decorator()
    def run_command(executable: Annotated[str, Field(description="Absolute path to the program binary; this is not a shell command string.")], argv: Annotated[list[str], Field(description="Exact string argument vector for one command; avoid credentials, scripts or private file content.")], cwd: Annotated[str, Field(description="Absolute working directory for the process within the local Owner policy.")],
                    timeout: Annotated[float, Field(description="Execution timeout in seconds, 0.01 to 3600; default 5.")] = 5.0, output_limit: Annotated[int, Field(description="Maximum captured output bytes, 128 to 65536; default 8192.")] = 8192,
                    background: Annotated[bool, Field(description="Whether to return immediately after launch; default false, unlike the wait API.")] = False,
                    interactive: Annotated[bool, Field(description="DEV_ONLY opt-in stdin PIPE for an Owner-reviewed isolated Python numeric REPL; default false. Requires background or asynchronous invocation.")] = False,
                    request_id: Annotated[str | None, Field(description="Optional original request correlation ID (32 lowercase hex characters); NOT Owner permission.")] = None) -> dict[str, Any]:
        """Legacy immediate-start API. REVIEW returns pending_id.

        interactive=True requires background=True and the DEV_ONLY isolated
        Python numeric REPL allowlist, with explicit local launch review.

        Prefer run_command_wait for normal work: it observes the Owner GUI
        click and returns job output without asking the Owner to type
        'approved'. Do not reissue a command merely to check its result.

Use only for legacy immediate command submission. A REVIEW result needs a later status/wait call; normally prefer run_command_wait. Owner approval is local to Windows. Never resubmit the same operation on timeout or UNKNOWN. ERROR OwnerReviewUnavailable (reason OWNER_SESSION_UNAVAILABLE, REVIEW_BUSY or OWNER_REVIEW_FAILED; launch_outcome NOT_STARTED) means no Owner dialog was shown and nothing ran: report it and wait for the Owner, never resubmit automatically."""
        # An explicit client ID is a replay correlation label, NEVER a grant.
        # Omit it when absent to retain the Host's original UUID generation.
        replay = {"request_id": request_id} if request_id is not None else {}
        if interactive:
            if type(host) is not Host or importlib.util.find_spec(f"{__package__}.dev_rdc_compat") is None:
                return {"status": "DENY", "error": "DevInteractiveOnly"}
            replay["interactive"] = True
        return host.run_command(
            executable=executable, argv=argv, cwd=cwd,
            timeout=timeout, output_limit=output_limit,
            background=background, **replay)

    @tool_decorator()
    def run_command_wait(executable: Annotated[str, Field(description="Absolute path to the program binary; this is not a shell command string.")], argv: Annotated[list[str], Field(description="Exact string argument vector for one command; avoid credentials, scripts or private file content.")], cwd: Annotated[str, Field(description="Absolute working directory for the process within the local Owner policy.")],
                         timeout: Annotated[float, Field(description="Execution timeout in seconds, 0.01 to 3600; default 5.")] = 5.0, output_limit: Annotated[int, Field(description="Maximum captured output bytes, 128 to 65536; default 8192.")] = 8192,
                         wait_seconds: Annotated[float, Field(description="Seconds to await Owner click and original job, 0 to 90; default 50. On timeout use await_command_result, not run_command_wait again.")] = 50.0,
                         interactive: Annotated[bool, Field(description="DEV_ONLY opt-in stdin PIPE for an Owner-reviewed isolated Python numeric REPL; default false. Requires background or asynchronous invocation.")] = False,
                         request_id: Annotated[str | None, Field(description="Optional original request correlation ID (32 lowercase hex characters); NOT Owner permission.")] = None) -> dict[str, Any]:
        """Run, wait for local Owner approval, then return bounded output.

        interactive=True opts into the DEV_ONLY isolated numeric Python REPL.
        Use wait_seconds=0 for prompt asynchronous observation, then send_input.

        Owner permission comes only from the Windows GUI, not MCP. On a
        wait timeout use await_command_result(pending_id) or existing output
        tools; never repeat run_command_wait to avoid duplicate execution.

Preferred command submission for tasks needing Owner GUI approval and a bounded wait for output. Use once per operation. On PENDING or timeout, continue through await_command_result with the returned pending_id; never reissue this command. ERROR OwnerReviewUnavailable (reason OWNER_SESSION_UNAVAILABLE, REVIEW_BUSY or OWNER_REVIEW_FAILED; launch_outcome NOT_STARTED) means no Owner dialog was shown and nothing ran: report it and wait for the Owner, never resubmit automatically."""
        replay = {"request_id": request_id} if request_id is not None else {}
        if interactive:
            if type(host) is not Host or importlib.util.find_spec(f"{__package__}.dev_rdc_compat") is None:
                return {"status": "DENY", "error": "DevInteractiveOnly"}
            replay["interactive"] = True
        return host.run_command_wait(
            executable=executable, argv=argv, cwd=cwd, timeout=timeout,
            output_limit=output_limit, wait_seconds=wait_seconds, **replay)

    @tool_decorator()
    def await_command_result(pending_id: Annotated[str, Field(description="Opaque ID returned by a prior Owner review; required to observe the SAME request, never fabricate or resubmit.")],
                             wait_seconds: Annotated[float, Field(description="Bounded observation time in seconds, 0 to 90 (default 50); continue existing pending ID after timeout.")] = 50.0) -> dict[str, Any]:
        """Wait for an existing Owner decision/job, without another execution.

Use after an earlier run_command_wait or REVIEW when you already have pending_id. Wait for that same Owner decision and job without new execution, approval, or submission. Timeout is not permission to retry."""
        return host.await_command_result(pending_id, wait_seconds=wait_seconds)

    @tool_decorator()
    def ping() -> dict[str, Any]:
        """Use for a quick, read-only health probe of the connected MCP Host. No input; does not establish that protected remote readiness, Broker acceptance, or a particular tool is exposed in ChatGPT."""
        return host.ping()

    @tool_decorator()
    def status() -> dict[str, Any]:
        """Use for read-only diagnostic status and local tool registration fingerprint. No input. LOCAL_REGISTRATION_SNAPSHOT is not proof of ChatGPT-side tool publication, remote readiness, or security acceptance."""
        catalog = _local_registered_catalog(server, host)
        return {**host.status(),
                "registered_tool_count": catalog["registered_tool_count"],
                "tool_schema_sha256": catalog["tool_schema_sha256"],
                "tool_catalog_scope": catalog["status"],
                "chatgpt_tool_visibility": catalog["chatgpt_tool_visibility"]}

    @tool_decorator()
    def review_status(pending_id: Annotated[str, Field(description="Existing 32-hex pending_id from an Owner command review or DEV_ONLY send_input; NOT the request_id or input_id. Read-only; never fabricate or resubmit.")]) -> dict[str, Any]:
        """Read-only observation using an existing pending_id: returns the stored Owner command approval/launch result, or the DEV_ONLY send_input input outcome when the ID belongs to stdin input. Does not approve, launch, send input or rerun. For command reviews, RUNNING / LAUNCH_ACCEPTED may remain after the job has COMPLETED: this is NOT the current job state. Use request_status(request_id) or await_process_output(session_id) for the job. For send_input, observe its original pending_id, not the command launch, and never send the input again. On PENDING follow the same pending_id; UNKNOWN does not prove no job ran or no input was sent. Never resubmit to check status."""
        return host.review_status(pending_id)

    @tool_decorator()
    def request_status(request_id: Annotated[str, Field(description="REQUIRED original request correlation ID (32 lowercase hex characters); NOT Owner permission.")]) -> dict[str, Any]:
        """Read-only lookup by the original caller's request ID.

        No new command, approval, or queue entry is created. UNKNOWN does
        not prove the old request never ran and MUST NOT trigger auto-retry.

Use with the original request_id to check a prior command's status without re-submission. UNKNOWN is inconclusive and must never justify an automatic retry."""
        return host.request_status(request_id)

    @tool_decorator()
    def read_process_output(session_id: Annotated[str, Field(description="Opaque existing JobManager session ID; never use it to start a second job.")], offset: Annotated[int, Field(description="Existing output byte cursor for paginated reading (default 0).")] = 0) -> dict[str, Any]:
        """Use to page stdout/stderr for an existing current-process JobManager session. Reads bounded output from offset; does not start or retry commands. Use previous_output for a prior Host invocation."""
        return host.read_process_output(session_id, offset)

    @tool_decorator()
    def await_process_output(session_id: Annotated[str, Field(description="Opaque existing JobManager session ID; never use it to start a second job.")], wait_seconds: Annotated[float, Field(description="Bounded observation time in seconds, 0 to 90 (default 50); continue existing pending ID after timeout.")] = 50.0) -> dict[str, Any]:
        """Wait for an already-running JobManager session, never start another.

Use to wait on an already-started session_id for a bounded period and retrieve its result. This is observation, not a new command; do not reissue run_command on timeout."""
        return host.await_process_output(session_id, wait_seconds=wait_seconds)

    @tool_decorator()
    def list_sessions() -> dict[str, Any]:
        """Use to list current Host JobManager sessions and their states for observability. Read-only; prior-process sessions instead use previous_sessions or recovery_overview."""
        return host.list_sessions()

    @tool_decorator()
    def previous_sessions() -> dict[str, Any]:
        """Use to inspect prior Host invocation START/END journal snapshots without command replay. Read-only metadata; journal-only records are not proof of no side effects or original argv."""
        return host.previous_sessions()

    @tool_decorator()
    def recovery_overview() -> dict[str, Any]:
        """Read-only current/prior session guidance, never automatic replay.

        A production BrokerHost cannot claim this DEV_ONLY aggregation.

Use after reconnect to interpret current/prior session and opt-in archive evidence in READ_ONLY_MANUAL_RECOVERY mode. No arguments, grants or auto-retry. Owner verification is required for private archive IDs/commands; ARCHIVE_ONLY remains outcome-unknown. Not protected Broker acceptance."""
        if type(host) is BrokerHost:
            return {"status": "UNAVAILABLE", "error": "ProtectedBrokerNotAccepted"}
        return host.recovery_overview()

    @tool_decorator()
    def previous_output(session_id: Annotated[str | None, Field(description="Optional archived session ID (48 lowercase hex); omit for Owner-verified metadata index. Never disclose private IDs without Owner verification.")] = None) -> dict[str, Any]:
        """Read archived logs from a PREVIOUS Host invocation; never replay.

        Without ID returns a bounded metadata index. With ID returns only
        that old session's stored output. A local Owner opt-in is mandatory.

Use to read an Owner-verified result archive from a PREVIOUS Host invocation. Omit session_id for metadata index, supply the original archived session_id for output and eligible safe-command identity. May contain sensitive stdout; never repost IDs or output without Owner verification, and never replay jobs."""
        return host.previous_output(session_id)

    @tool_decorator()
    def stop_process(session_id: Annotated[str, Field(description="Opaque existing JobManager session ID; never use it to start a second job.")]) -> dict[str, Any]:
        """Use only when the Owner explicitly intends to cancel an existing session_id. This modifies a running job; does not start a new one. Confirm the exact target; do not infer outcomes from missing sessions."""
        return host.stop_process(session_id)

    @tool_decorator()
    def stop_process_wait(session_id: Annotated[str, Field(description="Opaque existing JobManager session ID; never use it to start a second job.")], wait_seconds: Annotated[float, Field(description="Bounded observation time in seconds, 0 to 90 (default 50); continue existing pending ID after timeout.")] = 50.0) -> dict[str, Any]:
        """Explicitly cancel a job, then observe that SAME job to completion.

Use for explicit cancellation of the existing session_id followed by a bounded wait for its observed result. No new job or automatic replay. Only cancel when Owner intended."""
        return host.stop_process_wait(session_id, wait_seconds=wait_seconds)

    @tool_decorator()
    def preview_text_edit(path: Annotated[str, Field(description="Path to the target within the already-configured Owner read/edit roots; path alone grants no access.")], content: Annotated[str, Field(description="Full proposed text; may contain secrets. Avoid submitting secrets; submitted writes require local Owner review. Preview alone never saves.")], expected_sha256: Annotated[str, Field(description="Expected 64-character lowercase SHA-256 of current file content; stale values must fail.")]) -> dict[str, Any]:
        # Domain failures are data, not opaque transport errors. Never echo
        # an untrusted path, content, or exception text back to the client.
        """Use before a requested replacement edit to validate path, proposed full content and the original file SHA-256. Preview only: no write or Owner grant. Do not expose sensitive content in chat."""
        try:
            if not host.policy.roots:
                raise PermissionError("read roots not configured")
            return preview_edit_readonly(path, host.policy.roots, content, expected_sha256)
        except StalePreviewError:
            return {"status": "ERROR", "error": "STALE_SHA256"}
        except Exception:
            return {"status": "ERROR", "error": "PREVIEW_REJECTED"}

    @tool_decorator()
    @read_errors_only
    def preview_source_create(path: Annotated[str, Field(description="Proposed new source file path within Owner-configured scope, not yet created.")], content: Annotated[str, Field(description="Full proposed text; may contain secrets. Avoid submitting secrets; submitted writes require local Owner review. Preview alone never saves.")]) -> dict[str, Any]:
        """Review a proposed general source file without creating it.

Use to validate and preview creating a new source file at a permitted path. No file is created; send full proposed content only when safe, then separately seek Owner-approved creation if appropriate."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return preview_source_create_readonly(path, host.policy.roots, content)

    @tool_decorator()
    @read_errors_only
    def preview_source_patch(path: Annotated[str, Field(description="Path to the target within the already-configured Owner read/edit roots; path alone grants no access.")], old_text: Annotated[str, Field(description="Literal existing text (at most 4096 UTF-8 bytes); may contain secrets. Submitted writes require local Owner review; exactly one match.")], new_text: Annotated[str, Field(description="Literal replacement (at most 4096 UTF-8 bytes); may contain secrets. Submitted writes require local Owner review; never a shell command.")],
                             expected_sha256: Annotated[str, Field(description="Expected 64-character lowercase SHA-256 of current file content; stale values must fail.")]) -> dict[str, Any]:
        """Build one literal source edit proposal; no file write or grant.

Use to preview one literal exact-text source replacement against the expected SHA-256. No write/approval. Source up to 65536 bytes; complete diff up to 16384 bytes. Returns old/new SHA-256 and the complete diff, never the whole proposed content. A generic tool failure is not an Owner rejection or a write; resolve stale or ambiguous source rather than guess."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        preview = preview_source_patch_readonly(
            path, host.policy.roots, old_text, new_text, expected_sha256)
        # Return the digest and complete diff, not the whole (up to 64 KiB)
        # proposed source; local writers rebuild and recheck it themselves.
        return {key: value for key, value in preview.items() if key != "content"}

    if type(host) is BrokerHost:
        @tool_decorator()
        def request_source_create(path: Annotated[str, Field(description="Exact source target in existing Broker scope; this path grants no privileges.")], content: Annotated[str, Field(description="Full submitted content may contain secrets; requires Broker-local Owner review before any write.")], expected_new_sha256: Annotated[str, Field(description="Required lowercase SHA-256 binding the complete proposed new content to Owner review.")],
                                  request_id: Annotated[str, Field(description="Required original request correlation ID; observe the same request, never approval or retry authority.")]) -> dict[str, Any]:
            """Forward one exact proposal for Broker-local Owner review; never approve."""
            return host.request_source_create(path, content, expected_new_sha256, request_id)

        @tool_decorator()
        def request_source_patch(path: Annotated[str, Field(description="Exact source target in existing Broker scope; this path grants no privileges.")], old_text: Annotated[str, Field(description="Exact existing literal; may contain secrets; requires one match and Broker-local Owner review.")], new_text: Annotated[str, Field(description="Exact submitted replacement may contain secrets; requires Broker-local Owner review before writing.")],
                                 expected_sha256: Annotated[str, Field(description="Required lowercase SHA-256 of the inspected current source; stale content must fail.")], expected_new_sha256: Annotated[str, Field(description="Required lowercase SHA-256 binding the complete proposed new content to Owner review.")],
                                 request_id: Annotated[str, Field(description="Required original request correlation ID; observe the same request, never approval or retry authority.")]) -> dict[str, Any]:
            """Forward a SHA-bound literal patch. Unknown result never authorizes retry."""
            return host.request_source_patch(path, old_text, new_text, expected_sha256,
                                             expected_new_sha256, request_id)

        @tool_decorator()
        def source_review_status(request_id: Annotated[str, Field(description="Required original request correlation ID; observe the same request, never approval or retry authority.")]) -> dict[str, Any]:
            """Observe the existing source review; no resubmission or approval."""
            return host.source_review_status(request_id)

        @tool_decorator()
        def cancel_source_review(request_id: Annotated[str, Field(description="Required original request correlation ID; observe the same request, never approval or retry authority.")]) -> dict[str, Any]:
            """Cancel an existing review before its commit boundary."""
            return host.cancel_source_review(request_id)

    # Explicit personal DEV Host only. Never attach these writers to BrokerHost
    # or broaden the disposable EditReviewPilot's target allowlist.
    # A DEV Broker client forwards the same tools; its Broker holds the writers.
    dev_client = type(host) is not BrokerHost and getattr(host, "dev_broker_client", False) is True
    if ((type(host) is Host or dev_client) and
            importlib.util.find_spec(f"{__package__}.dev_rdc_compat") is not None):
        from .dev_rdc_compat import mutate, list_processes as processes_readonly
        from .dev_rdc_compat import install_host_interactive
        # Release-side code never imports the DEV module: it dispatches
        # through interactive_hooks, installed here and only here.
        if not dev_client:
            install_host_interactive(host)

        @tool_decorator()
        def send_input(session_id: Annotated[str, Field(description="Exact OWN active interactive JobManager session ID (48 lowercase hex); never a PID or foreign session.")],
                       input_text: Annotated[str, Field(description="One UTF-8 numeric Python expression or print(expression), ending in newline, at most 1024 bytes; no secrets or other controls.")],
                       input_id: Annotated[str, Field(description="Unique 32 lowercase hex input ID consumed before GUI review; NEVER resend after timeout, error or UNKNOWN, even with a different ID.")]) -> dict[str, Any]:
            """DEV_ONLY: ask the local Owner GUI to send one exact numeric line ONCE to this manager's previously approved interactive Python process. Returns pending_id; observe through await_command_result or review_status only. No arbitrary code, secrets, PID attachment, input logging or retries. UNKNOWN stops the session."""
            from .dev_rdc_compat import send_input as submit_input
            if dev_client:
                return {"status": "DENY", "error": "DevInteractiveOnly"}
            try:
                manager = host._manager_for_session(session_id)
                if manager is not host._dev_interactive_jobs:
                    raise PermissionError("DEV session required")
                return submit_input(manager, session_id, input_text, input_id)
            except Exception:
                return {"status": "DENIED", "error": "DevInputDenied", "retry_allowed": False}

        def dev_mutate(operation, **arguments):
            if dev_client:
                return host.dev_mutate(operation, **arguments)
            with host.policy.lock:
                return mutate(operation, tuple(host.policy.roots), **arguments)

        @tool_decorator()
        def create_directory(path: Annotated[str, Field(description="Absolute absent directory under configured Owner roots; existing parent required, no recursive creation.")]) -> dict[str, Any]:
            """DEV_ONLY: ask the local Owner GUI to create one absent directory in configured roots. Reject links, protected names and existing targets; no command/job spawn. UNKNOWN requires manual reconciliation, never retry or assumed rollback."""
            return dev_mutate("create_directory", path=path)

        @tool_decorator()
        def create_file(path: Annotated[str, Field(description="Absolute absent file under configured Owner roots; existing parent directory required, never overwrite.")],
                        content: Annotated[str, Field(description="Full proposed UTF-8 text, at most 65536 bytes; may contain secrets. Avoid submitting secrets; the complete content is shown to the local Owner GUI before creation.")]) -> dict[str, Any]:
            """DEV_ONLY: ask the local Owner GUI to create one new text file in configured roots, showing its path and complete content first. Existing targets are never overwritten; links and protected names are rejected; no command or job spawn. UNKNOWN requires manual reconciliation, never retry or assumed rollback."""
            return dev_mutate("create_file", path=path, content=content)

        @tool_decorator()
        def move_file(source: Annotated[str, Field(description="Absolute existing ordinary single-link file in configured Owner roots; directories and protected names are denied.")], destination: Annotated[str, Field(description="Absolute absent destination in configured Owner roots; existing non-linked parent required, never overwrite.")]) -> dict[str, Any]:
            """DEV_ONLY Windows: request local Owner GUI review of one exact file move in existing roots. No overwrite, directory move or job spawn. Cooperative checks are not protected CAS. UNKNOWN requires manual reconciliation; never auto-retry or assume rollback."""
            return dev_mutate("move_file", source=source, destination=destination)

        @tool_decorator()
        def request_source_patch_dev(path: Annotated[str, Field(description="Absolute existing source file within configured Owner roots; DEV_ONLY, never a protected Broker permission.")], old_text: Annotated[str, Field(description="Exact literal anchor, one occurrence only, up to 4096 UTF-8 bytes; may contain secrets, avoid submitting them.")], new_text: Annotated[str, Field(description="Literal replacement up to 4096 UTF-8 bytes; may contain secrets. Full proposed source is shown to local Owner before writing.")], expected_sha256: Annotated[str, Field(description="Required lowercase SHA-256 of inspected current source; reverified after Owner review and immediately before replacement.")]) -> dict[str, Any]:
            """DEV_ONLY cooperative literal source patch: reuse preview_source_patch, show the complete proposed content and diff to local Owner GUI, then recheck SHA before replacement. One exact anchor only; no fuzzy edit, job spawn or protected Broker acceptance. Never retry an UNKNOWN result."""
            return dev_mutate("request_source_patch_dev", path=path, old_text=old_text, new_text=new_text, expected_sha256=expected_sha256)

        @tool_decorator()
        def list_processes(limit: Annotated[int, Field(description="Maximum process PID/name records, 1 to 200; default 100. Total scan capped at 4096 and results sorted by PID.")] = 100) -> dict[str, Any]:
            """Read-only bounded PID and executable basename listing using installed psutil or Windows Toolhelp snapshot. No command lines, environment, Owner identity, credentials or process control. Safe error on unavailable/incomplete enumeration; never starts a job or installs dependencies."""
            return processes_readonly(limit)

        @tool_decorator()
        def get_config() -> dict[str, Any]:
            """Read-only nonsecret DEV configuration view: reports flags, root count and rootsConfigured boolean only. No paths, credential values, identity or configuration mutation. This view grants no privileges and establishes no protected Broker acceptance."""
            with host.policy.lock:
                return {"devOnly": True, "rootsConfigured": bool(host.policy.roots),
                        "rootCount": len(host.policy.roots), "protectedBrokerAcceptance": False}

    # Never expose remote editing unless the locally opted-in disposable pilot exists.
    pilot = getattr(host, "_edit_review_pilot", None)
    if pilot is not None:
        @tool_decorator()
        def request_text_edit(path: Annotated[str, Field(description="Path to the target within the already-configured Owner read/edit roots; path alone grants no access.")], content: Annotated[str, Field(description="Full proposed text; may contain secrets. Avoid submitting secrets; this submission is written only after the local Owner review approves it.")], expected_sha256: Annotated[str, Field(description="Expected 64-character lowercase SHA-256 of current file content; stale values must fail.")]) -> dict[str, Any]:
            """Submit one full-content text replacement for local Owner review after inspecting the SHA-bound preview. Can write on Owner approval; never call twice after timeout. Use edit_review_status/await_edit_result with pending_id."""
            try:
                return pilot.request(path, content, expected_sha256)
            except StalePreviewError:
                return {"status": "ERROR", "error": "STALE_SHA256"}
            except Exception:
                return {"status": "ERROR", "error": "EDIT_REJECTED"}

        @tool_decorator()
        def request_text_edit_wait(path: Annotated[str, Field(description="Path to the target within the already-configured Owner read/edit roots; path alone grants no access.")], content: Annotated[str, Field(description="Full proposed text; may contain secrets. Avoid submitting secrets; this submission is written only after the local Owner review approves it.")], expected_sha256: Annotated[str, Field(description="Expected 64-character lowercase SHA-256 of current file content; stale values must fail.")],
                                   wait_seconds: Annotated[float, Field(description="Seconds to await Owner edit decision, 0 to 90; default 50. On timeout use await_edit_result.")] = 50.0) -> dict[str, Any]:
            """Submit ONE disposable edit review; wait for verified Owner result.

            Never repeats a write after a timeout. Use await_edit_result with
            the returned pending_id for a continuing review.

Preferred one-shot Owner-review edit submission with bounded wait. Uses expected old SHA-256 and complete proposed content. If wait expires, use await_edit_result with same pending_id, never resubmit."""
            if (type(wait_seconds) not in (int, float)
                    or not math.isfinite(wait_seconds)
                    or not 0 <= wait_seconds <= 90):
                return {"status": "ERROR", "error": "InvalidWaitSeconds"}
            initial = request_text_edit(path, content, expected_sha256)
            return _await_submitted_edit_data_only(pilot, initial, wait_seconds)

        @tool_decorator()
        def edit_review_status(pending_id: Annotated[str, Field(description="Opaque ID returned by a prior Owner review; required to observe the SAME request, never fabricate or resubmit.")]) -> dict[str, Any]:
            """Use with an existing pending_id to inspect an edit/create review status. Read-only observation; never approves, rewrites or resends content."""
            return pilot.status(pending_id)

        @tool_decorator()
        def await_edit_result(pending_id: Annotated[str, Field(description="Opaque ID returned by a prior Owner review; required to observe the SAME request, never fabricate or resubmit.")],
                              wait_seconds: Annotated[float, Field(description="Bounded observation time in seconds, 0 to 90 (default 50); continue existing pending ID after timeout.")] = 50.0) -> dict[str, Any]:
            """Observe an existing edit/create review; never resubmit content.

Use after a pending edit/create review to wait for that same decision/outcome. Never resubmit an edit or assume UNKNOWN means not written; on timeout reuse pending_id only."""
            return _await_edit_data_only(pilot, pending_id, wait_seconds)

        if getattr(pilot, 'enable_create', False) is True:
            @tool_decorator()
            def request_text_create(path: Annotated[str, Field(description="Proposed new file path in permitted Owner edit scope; must not overwrite.")], content: Annotated[str, Field(description="Full proposed text; may contain secrets. Avoid submitting secrets; this submission is written only after the local Owner review approves it.")]) -> dict[str, Any]:
                """Submit one new text file creation for local Owner review. Only enabled in the explicit DEV edit pilot; do not overwrite existing files or resubmit an uncertain result. Prefer preview_source_create first."""
                try:
                    return pilot.request_create(path, content)
                except Exception:
                    return {'status': 'ERROR', 'error': 'CREATE_REJECTED'}

            @tool_decorator()
            def request_text_create_wait(path: Annotated[str, Field(description="Proposed new file path for a single Owner-reviewed create; must not overwrite.")], content: Annotated[str, Field(description="Full proposed text; may contain secrets. Avoid submitting secrets; this submission is written only after the local Owner review approves it.")],
                                         wait_seconds: Annotated[float, Field(description="Seconds to await Owner create decision, 0 to 90; default 50. On timeout use await_edit_result.")] = 50.0) -> dict[str, Any]:
                """Submit ONE disposable create review, then wait for outcome.

Preferred one-shot new-file creation proposal with bounded Owner review wait. Use once; on PENDING continue via await_edit_result, never duplicate a possibly committed write."""
                if (type(wait_seconds) not in (int, float)
                        or not math.isfinite(wait_seconds)
                        or not 0 <= wait_seconds <= 90):
                    return {"status": "ERROR", "error": "InvalidWaitSeconds"}
                initial = request_text_create(path, content)
                return _await_submitted_edit_data_only(pilot, initial, wait_seconds)

    @tool_decorator()
    @read_errors_only
    def list_folder(path: Annotated[str, Field(description="Existing directory under Owner-approved read roots.")], limit: Annotated[int, Field(description="Maximum number of returned entries, 1 to 200; default 100.")] = 100, depth: Annotated[int, Field(description="Additional directory levels, 0 to 4; default 0 preserves immediate-child schema. Recursive entries include a relative path.")] = 0) -> dict[str, Any]:
        """List bounded entries under Owner-approved read roots. Read-only; depth=0 preserves immediate children and the original schema. Depth 1 to 4 adds relative paths and bounded recursion, excluding private names, links and denied folders. Limit bounds output, not authorization."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return list_folder_readonly(path, host.policy.roots, limit=limit, depth=depth)

    @tool_decorator()
    @read_errors_only
    def list_folder_page(path: Annotated[str, Field(description="Same existing directory for next listing page; must remain inside approved roots.")], offset: Annotated[int, Field(description="Entry offset for pagination, 0 to 10000; default 0.")] = 0,
                         limit: Annotated[int, Field(description="Maximum number of returned entries, 1 to 200; default 100.")] = 100) -> dict[str, Any]:
        """Continue a bounded listing without widening Owner read roots.

Use to page a folder listing when list_folder's output is insufficient. Provide the same path, entry offset and limit. Read-only and confined to Owner read roots."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return list_folder_page_readonly(path, host.policy.roots,
                                          offset=offset, limit=limit)

    @tool_decorator()
    def read_text_file(path: Annotated[str, Field(description="Path to the target within the already-configured Owner read/edit roots; path alone grants no access.")], max_bytes: Annotated[int, Field(description="Maximum UTF-8 file bytes, 1 to 65536; default 65536.")] = 65536) -> dict[str, Any]:
        """Use for a bounded UTF-8 read of an existing file within configured Owner read roots. Never modify the file; may return sensitive text, so share only when appropriate."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return read_text_readonly(path, host.policy.roots, max_bytes=max_bytes)

    @tool_decorator()
    @read_errors_only
    def read_text_page(path: Annotated[str, Field(description="Path to the target within the already-configured Owner read/edit roots; path alone grants no access.")], offset: Annotated[int, Field(description="Byte offset in UTF-8 file, 0 to 4194304; default 0. Use continuation offset.")] = 0,
                       max_bytes: Annotated[int, Field(description="Maximum UTF-8 bytes per page, 1 to 65536; default 16384.")] = 16384) -> dict[str, Any]:
        """Read a UTF-8 page within existing Owner-configured read roots.

Use for large UTF-8 files needing byte-offset pagination within approved read roots. Reuse the returned byte cursor rather than guessing; no file modifications."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return read_text_page_readonly(path, host.policy.roots,
                                       offset=offset, max_bytes=max_bytes)
    @tool_decorator()
    @read_errors_only
    def find_files(path: Annotated[str, Field(description="Root directory under existing Owner read roots for bounded name discovery.")], name: Annotated[str, Field(description="Literal filename/name pattern to find (1 to 120 characters).")], max_results: Annotated[int, Field(description="Maximum matches returned, 1 to 100; default 50.")] = 50,
                   depth: Annotated[int, Field(description="Maximum directory traversal depth, 0 to 12; default 8.")] = 8) -> dict[str, Any]:
        """Bounded name-only discovery inside configured Owner read roots.

Use to discover filenames by name under an allowed directory within bounded depth/result count. This is path discovery, not a content search or write authorization."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return find_files_readonly(path, host.policy.roots, name=name,
                                   max_results=max_results, depth=depth)

    @tool_decorator()
    @read_errors_only
    def search_code(path: Annotated[str, Field(description="Root directory under existing Owner read roots for literal UTF-8 content search.")], text: Annotated[str, Field(description="Exact UTF-8 text to search for (1 to 120 characters; no line breaks).")], max_results: Annotated[int, Field(description="Maximum matches returned, 1 to 100; default 50.")] = 50,
                    depth: Annotated[int, Field(description="Maximum directory traversal depth, 0 to 12; default 8.")] = 8) -> dict[str, Any]:
        """Literal UTF-8 source search; results include relative path and line.

Use for literal text search within bounded UTF-8 files under approved roots. Supply the exact search text, depth and result cap. Search results may expose sensitive lines; read-only."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return search_code_readonly(path, host.policy.roots, text=text,
                                    max_results=max_results, depth=depth)

    @tool_decorator()
    @read_errors_only
    def path_info(path: Annotated[str, Field(description="Path to the target within the already-configured Owner read/edit roots; path alone grants no access.")]) -> dict[str, Any]:
        """Read-only file size and bounded digest; never authorizes a write.

Use for non-mutating file or directory metadata checks on an allowed path, including size/digest when available. Not a write grant and not proof of user consent."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return path_info_readonly(path, host.policy.roots)

    @tool_decorator()
    @read_errors_only
    def read_text_lines(path: Annotated[str, Field(description="Path to the target within the already-configured Owner read/edit roots; path alone grants no access.")], start_line: Annotated[int, Field(description="First 1-based line to read (1 to 1000000); default 1.")] = 1,
                        max_lines: Annotated[int, Field(description="Maximum line count, 1 to 100; default 50.")] = 50) -> dict[str, Any]:
        """Read numbered UTF-8 source lines without creating a new grant.

Use when exact numbered source lines are needed for review. Choose 1-based start_line and a bounded max_lines; no edits or permission changes."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return read_text_lines_readonly(path, host.policy.roots,
                                        start_line=start_line, max_lines=max_lines)

    @tool_decorator()
    @read_errors_only
    def read_many_text_files(paths: Annotated[list[str], Field(description="One to eight distinct existing file paths in approved read roots.")],
                             max_bytes_per_file: Annotated[int, Field(description="Per-file output byte cap, 1 to 8192; default 8192.")] = 8192) -> dict[str, Any]:
        """Read up to eight bounded existing UTF-8 files in one MCP call.

Use to read up to eight existing UTF-8 files in one bounded, read-only call. Provide distinct paths in approved roots; each file has its own byte limit."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return read_many_text_files_readonly(
            paths, host.policy.roots, max_bytes_per_file=max_bytes_per_file)

    @tool_decorator()
    @read_errors_only
    def compare_text_files(before_path: Annotated[str, Field(description="First existing file to show on the left/old side of a read-only comparison.")], after_path: Annotated[str, Field(description="Second existing file to show on the right/new side of a read-only comparison.")],
                           context_lines: Annotated[int, Field(description="Read-only diff context lines, 0 to 8; default 3.")] = 3) -> dict[str, Any]:
        """Compare two Owner-scoped existing files without editing either.

Use to compare two existing Owner-scoped text files as a read-only diff. before_path and after_path are files, not patches; context_lines adjusts displayed context only."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return compare_text_files_readonly(
            before_path, after_path, host.policy.roots, context_lines=context_lines)

    @tool_decorator()
    @read_errors_only
    def read_text_chunk(path: Annotated[str, Field(description="Path to the target within the already-configured Owner read/edit roots; path alone grants no access.")], offset: Annotated[int, Field(description="Byte offset for streaming, -1 to 268435456; -1 requests a bounded tail (default -1).")] = -1,
                        max_bytes: Annotated[int, Field(description="Maximum bytes per chunk, 4 to 65536; default 16384.")] = 16384) -> dict[str, Any]:
        """Read a bounded UTF-8 tail or resume from a previous byte cursor.

Use to read a bounded UTF-8 tail (offset -1) or resume at an explicit byte cursor. Useful for logs and evolving files; never creates or changes them."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return read_text_chunk_readonly(path, host.policy.roots,
                                        offset=offset, max_bytes=max_bytes)

    @tool_decorator()
    @read_errors_only
    def sha256_file(path: Annotated[str, Field(description="Path to the target within the already-configured Owner read/edit roots; path alone grants no access.")]) -> dict[str, Any]:
        """Stream a bounded file hash, with no install/execute permissions.

Use to compute the SHA-256 of an approved existing file without reading full contents into chat or changing permissions. Hash alone does not establish a trusted signature."""
        if not host.policy.roots:
            raise PermissionError("read roots not configured")
        return sha256_file_readonly(path, host.policy.roots)

    @tool_decorator()
    def tool_catalog() -> dict[str, Any]:
        """Actual local SDK registration snapshot; NOT connector publication.

Use to inspect only the local FastMCP tool registration count, names and schema fingerprint. No args and no remote publication guarantee; compare against ChatGPT's visible action list for stale tool snapshots."""
        return _local_registered_catalog(server, host)

    return server


def serve_stdio(host: Host):
    """Own a disposable host until EOF; explicit invocation only, no listener."""
    try:
        build_server(host).run(transport="stdio")
    finally:
        if type(host) is BrokerHost:
            host.close()  # Connection only. Never cancel/close the Broker's jobs.
        else:
            host.close()
