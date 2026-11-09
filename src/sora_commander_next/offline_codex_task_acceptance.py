"""DEV_ONLY local post-job evidence adjudicator; never grants execution authority.

Separate the Codex CLI lifecycle from engineering acceptance. Inputs are caller
observations, NOT authenticated proofs. A REVIEW_READY result still requires a
human/trusted external verifier; no SUCCESS task or protected permissions exist.
No filesystem, process, IPC, network, or tool APIs are used.
"""
from dataclasses import dataclass, field
import re

MAX_ITEMS = 16
_SHA = re.compile(r"[0-9a-f]{64}")
_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}")
_TEST_ID = re.compile(r"test_[A-Za-z0-9_]{1,110}")
_BLOCKERS = frozenset(("ACCESS_DENIED", "OWNER_DENIED", "SANDBOX_BLOCK", "TASK_DECLINED"))
_BANNED = frozenset((".git", ".venv", "__pycache__", "node_modules", "dist", "build"))
_DEVICES = frozenset(("con", "prn", "aux", "nul", "clock$",
                      *(f"com{i}" for i in range(1, 10)),
                      *(f"lpt{i}" for i in range(1, 10))))


class TaskReceiptError(ValueError):
    """Ill-shaped task expectation/observation."""


def _sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _path(value):
    if type(value) is not str or not 1 <= len(value) <= 240 or "\\" in value:
        raise TaskReceiptError("bounded repo-relative POSIX path required")
    pieces = value.split("/")
    for piece in pieces:
        if (not piece or piece in (".", "..") or piece.startswith(".")
                or piece.endswith(".") or "~" in piece
                or _COMPONENT.fullmatch(piece) is None
                or piece.casefold() in _BANNED
                or piece.split(".", 1)[0].casefold() in _DEVICES):
            raise TaskReceiptError("unsafe evidence path")
    return value


def _tests(value):
    if (type(value) is not tuple or not 1 <= len(value) <= MAX_ITEMS
            or any(type(v) is not str or _TEST_ID.fullmatch(v) is None for v in value)
            or len(set(value)) != len(value)):
        raise TaskReceiptError("exact expected test IDs required")


@dataclass(frozen=True, slots=True)
class RequiredFile:
    path: str
    before_sha256: str | None
    must_change: bool = True

    def __post_init__(self):
        _path(self.path)
        if self.before_sha256 is not None and not _sha(self.before_sha256):
            raise TaskReceiptError("invalid pre-job snapshot digest")
        if type(self.must_change) is not bool:
            raise TaskReceiptError("exact required-change boolean")


@dataclass(frozen=True, slots=True)
class FileEvidence:
    path: str
    after_sha256: str | None

    def __post_init__(self):
        _path(self.path)
        if self.after_sha256 is not None and not _sha(self.after_sha256):
            raise TaskReceiptError("invalid post-job digest")


@dataclass(frozen=True, slots=True)
class TestEvidence:
    name: str
    exit_code: int
    cases: int
    failures: int
    errors: int
    output_complete: bool

    def __post_init__(self):
        if type(self.name) is not str or _TEST_ID.fullmatch(self.name) is None:
            raise TaskReceiptError("invalid expected test ID")
        if (type(self.exit_code) is not int or not -2**31 <= self.exit_code < 2**31
                or any(type(n) is not int or n < 0 or n > 100000
                       for n in (self.cases, self.failures, self.errors))
                or self.failures + self.errors > self.cases
                or type(self.output_complete) is not bool):
            raise TaskReceiptError("invalid test observation")


@dataclass(frozen=True, slots=True)
class TaskReview:
    cli_lifecycle: str
    task_acceptance: str
    reason: str
    observed_changes: int
    checked_tests: int
    authority: str = field(default="NONE_REVIEW_ONLY", init=False)
    release_permitted: bool = field(default=False, init=False)
    execution_permitted: bool = field(default=False, init=False)


def _lifecycle(job):
    if type(job) is not dict:
        raise TaskReceiptError("job observation object required")
    status, code, started = (job.get("status"), job.get("exitCode"),
                             job.get("codexStarted"))
    flags = job.get("evidence")
    if (type(status) is not str or status not in
            ("SUCCESS", "FAILED", "UNKNOWN", "STARTING", "RUNNING")
            or (code is not None and (type(code) is not int or
                                      not -2**31 <= code < 2**31))
            or type(started) is not bool or type(flags) is not dict
            or any(type(flags.get(k)) is not bool for k in
                   ("turnStarted", "turnCompleted", "turnFailed", "errorEvent"))
            or type(flags.get("malformedLines")) is not int
            or flags["malformedLines"] < 0):
        raise TaskReceiptError("malformed or incomplete job observation")
    if (status == "SUCCESS" and code == 0 and started
            and flags["turnStarted"] and flags["turnCompleted"]
            and not flags["turnFailed"] and not flags["errorEvent"]
            and flags["malformedLines"] == 0):
        return "SUCCESS"
    if status == "FAILED" and started and code is not None:
        return "FAILED"
    return "UNKNOWN"


def assess_task_evidence(job, *, required_files, required_tests,
                         observed_files=(), observed_tests=(), blocker=None):
    """Classify *supplied* facts, never infer PASS from Codex exit alone.

    A completed set becomes REVIEW_READY, not PASS: inputs have no independent
    provenance. The final Owner/CI gate must verify real snapshots, test logs,
    expected file scope and test authenticity separately.
    """
    cli = _lifecycle(job)
    if (type(required_files) is not tuple or
            not 1 <= len(required_files) <= MAX_ITEMS or
            any(type(r) is not RequiredFile for r in required_files)):
        raise TaskReceiptError("bounded exact required file list")
    _tests(required_tests)
    for rows, cls in ((observed_files, FileEvidence), (observed_tests, TestEvidence)):
        if (type(rows) is not tuple or len(rows) > MAX_ITEMS
                or any(type(v) is not cls for v in rows)):
            raise TaskReceiptError("bounded typed observations only")
    file_names = tuple(row.path for row in required_files)
    seen_files = tuple(row.path for row in observed_files)
    test_names = tuple(row.name for row in observed_tests)
    if (len(set(x.casefold() for x in file_names)) != len(file_names)
            or len(set(x.casefold() for x in seen_files)) != len(seen_files)
            or len(set(test_names)) != len(test_names)
            or any(p not in file_names for p in seen_files)
            or any(t not in required_tests for t in test_names)):
        raise TaskReceiptError("duplicate or out-of-scope observations")
    if blocker is not None and (type(blocker) is not str or blocker not in _BLOCKERS):
        raise TaskReceiptError("unknown recorded blocker category")

    def result(state, reason, changed=0, tested=0):
        return TaskReview(cli, state, reason, changed, tested)

    if blocker is not None:
        return result("HOLD", blocker)
    if cli == "FAILED":
        return result("HOLD", "CODEX_CLI_FAILED")
    if cli != "SUCCESS":
        return result("UNVERIFIED", "CODEX_CLI_NOT_CONFIRMED")
    if set(seen_files) != set(file_names):
        return result("UNVERIFIED", "POST_JOB_FILE_EVIDENCE_MISSING")
    file_map = {row.path: row.after_sha256 for row in observed_files}
    changed = 0
    for item in required_files:
        actual = file_map[item.path]
        # Editing and create tasks never authorize deleting a required file.
        # In particular a must_change=False observation must not turn
        # disappearance of an existing source into REVIEW_READY.
        if actual is None:
            return result("HOLD", "REQUIRED_SOURCE_FILE_ABSENT")
        if item.must_change and actual == item.before_sha256:
            return result("HOLD", "REQUIRED_SOURCE_CHANGE_ABSENT")
        if actual != item.before_sha256:
            changed += 1
    if set(test_names) != set(required_tests):
        return result("UNVERIFIED", "REQUIRED_TEST_EVIDENCE_MISSING", changed)
    for test in observed_tests:
        if test.exit_code != 0 or test.failures or test.errors:
            return result("HOLD", "REQUIRED_TEST_FAILED", changed, len(observed_tests))
        if test.cases == 0 or not test.output_complete:
            return result("UNVERIFIED", "TEST_OUTPUT_INCOMPLETE", changed, len(observed_tests))
    return result("REVIEW_READY", "EXTERNAL_EVIDENCE_VERIFICATION_REQUIRED",
                  changed, len(observed_tests))
