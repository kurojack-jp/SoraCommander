"""Deterministic human-readable preview for offline development-job scopes.

DATA ONLY: no Owner consent, no GUI, no command dispatch, no file access.
The complete canonical scope remains authoritative; this preview cannot grant rights.
"""
from .dev_job_scope_offline import DevJobScope

def build_review_preview(scope):
    if type(scope) is not DevJobScope:
        raise ValueError("immutable development-job scope required")
    # Re-validate untrusted serialized input, even if constructed by a caller.
    verified = DevJobScope(scope.canonical_json)
    data = verified.to_data()
    cmd = data["command"]
    lines = [
        "OFFLINE PREVIEW ONLY - NOT AN OWNER APPROVAL",
        "Operation: " + data["operation"],
        "Job ID: " + data["job_id"],
        "Scope SHA-256: " + verified.digest,
        "Repository: " + data["repo_root"],
        "Task file: " + data["task_file"],
        "Task SHA-256: " + data["task_sha256"],
        "Runner: " + data["runner_executable"],
        "Runner SHA-256: " + data["runner_sha256"],
        "Writable files (EXACT):",
        *("  " + name for name in data["write_files"]),
        "Focused test files (EXACT):",
        *("  " + name for name in data["focused_test_files"]),
        "Runner argv (JSON): " + __import__("json").dumps(cmd["argv"], ensure_ascii=True),
        "Working directory: " + cmd["cwd"],
        "Background: " + str(cmd["background"]),
        "Timeout (ms): " + str(cmd["timeout_ms"]),
        "Output limit (bytes): " + str(cmd["output_limit"]),
        "Created (ms): " + str(data["created_at_ms"]),
        "Expires (ms): " + str(data["expires_at_ms"]),
        "Maximum attempts: " + str(data["max_attempts"]),
        "Environment mode: " + data["environment"]["mode"],
        "Environment SHA-256: " + data["environment"]["block_sha256"],
        "Runtime epoch: " + data["runtime_epoch"],
        "Clock epoch: " + data["clock_epoch"],
        "Network: DENY; child processes: DENY; shell: DENY; elevation: DENY",
        "No live authorization or process execution is implemented here.",
    ]
    return "\n".join(lines) + "\n"
