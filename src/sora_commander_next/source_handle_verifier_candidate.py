"""DEV_ONLY handle-level dry-run algorithm. Existing-source commit DENIED.

Injected backend only: open_read_handle, inspect_handle, read_bounded, close.
No DLL loader or real backend is selected. No staging/write/rename API exists.
Native exclusion, mapped writers and ancestor-path races remain unaccepted.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import difflib
from pathlib import PureWindowsPath
import re
from threading import Lock
from time import monotonic_ns

from .broker_host import decode_frame
from .source_review import SourcePolicy, SourceSnapshot, SourceConflict, MAX_SOURCE


@dataclass(frozen=True, slots=True)
class HandleRecord:
    final_path: str
    identity: tuple  # volume, file ID integer, size, mtime_ns, change_ns
    attributes: int
    reparse_tag: int
    links: int
    directory: bool
    delete_pending: bool


@dataclass(frozen=True, slots=True)
class SourceHandleDryRun:
    status: str
    review_sha256: str
    before_sha256: str
    after_sha256: str
    nonce: str
    authority: str = field(default="NONE_DATA_ONLY", init=False)
    commit: str = field(default="DENIED_NATIVE_EXCLUSION_NOT_ACCEPTED", init=False)


class NativeSourceHandleVerifierCandidate:
    def __init__(self, policy, *, _backend=None, _clock_ms=None):
        if type(policy) is not SourcePolicy:
            raise TypeError("exact local source policy required")
        self._policy, self._backend = policy, _backend
        self._clock = _clock_ms or (lambda: monotonic_ns() // 1_000_000)
        self._slot, self._seen, self._halted = Lock(), set(), False
        self._last_tick = -1

    def commit(self, path, before, data, last_check):
        # Even a perfect dry run and Owner approval do not prove exclusion.
        raise SourceConflict("STRONG_SOURCE_CAS_NOT_ACCEPTED")

    @staticmethod
    def _record(record, path, directory):
        if (type(record) is not HandleRecord or type(record.final_path) is not str
                or record.final_path != "\\\\?\\" + path
                or type(record.identity) is not tuple or len(record.identity) != 5
                or any(type(n) is not int or not 0 <= n < 2**128 for n in record.identity)
                or not record.identity[0] or not record.identity[1]
                or record.identity[0] >= 2**64 or any(n >= 2**63 for n in record.identity[2:])
                or type(record.attributes) is not int or not 0 <= record.attributes <= 0xffffffff
                or type(record.reparse_tag) is not int or record.reparse_tag != 0
                or type(record.links) is not int or record.links != 1
                or type(record.directory) is not bool or record.directory is not directory
                or bool(record.attributes & 0x10) is not directory
                or type(record.delete_pending) is not bool or record.delete_pending):
            raise SourceConflict("invalid handle identity/state")
        root = PureWindowsPath(path).parent == PureWindowsPath(path)
        if record.attributes & (0x400 if root else 0x402):
            raise SourceConflict("reparse or hidden source component")
        return record

    def verify_review_data_only(self, raw, before, *, current_owner):
        frame = decode_frame(raw)
        keys = {"v", "request_id", "path", "operation", "before_sha256", "after_sha256",
                "before_utf8", "after_utf8", "diff", "identity", "parents", "owner_sid",
                "logon_luid", "wts_session", "nonce", "issued_tick", "expires_tick"}
        if (set(frame) != keys or type(frame["v"]) is not int or frame["v"] != 1
                or frame["operation"] != "patch" or type(before) is not SourceSnapshot):
            raise SourceConflict("exact existing-source review required")
        before = SourceSnapshot(before.data, before.identity, before.parents)
        path = self._policy.target(frame["path"])
        data = frame["after_utf8"]
        if (before.data is None or type(data) is not str or "\0" in data
                or len(data.encode("utf-8")) > MAX_SOURCE
                or frame["before_utf8"] != before.data.decode("utf-8")
                or frame["before_sha256"] != sha256(before.data).hexdigest()
                or frame["after_sha256"] != sha256(data.encode("utf-8")).hexdigest()
                or frame["identity"] != list(before.identity)
                or frame["parents"] != [list(p) for p in before.parents]
                or type(frame["nonce"]) is not str or re.fullmatch("[0-9a-f]{64}", frame["nonce"]) is None
                or type(frame["request_id"]) is not str or re.fullmatch("[0-9a-f]{32}", frame["request_id"]) is None
                or type(frame["issued_tick"]) is not int or type(frame["expires_tick"]) is not int
                or not 0 <= frame["issued_tick"] < frame["expires_tick"] < 2**63
                or frame["expires_tick"] - frame["issued_tick"] > 45000):
            raise SourceConflict("review bytes/identity/hash/time mismatch")
        from .offline_b01_full_binding import _sid, _luid, _number
        _sid(frame["owner_sid"])
        _luid(frame["logon_luid"])
        _number(frame["wts_session"], 1, 0xffffffff, "source WTS")
        binding = (frame["owner_sid"], tuple(frame["logon_luid"]), frame["wts_session"])
        expected_diff = "".join(difflib.unified_diff(before.data.decode("utf-8").splitlines(keepends=True),
            data.splitlines(keepends=True), fromfile="current", tofile="proposed"))
        if frame["diff"] != expected_diff:
            raise SourceConflict("review display differs from proposed bytes")
        ancestors = tuple(str(p) for p in PureWindowsPath(path).parents)
        if (len(ancestors) > 32 or tuple(p[0] for p in before.parents) != ancestors):
            raise SourceConflict("complete ordered ancestor chain required")
        if self._backend is None:
            raise SourceConflict("NATIVE_HANDLE_BACKEND_NOT_ENABLED")
        if not self._slot.acquire(blocking=False):
            raise SourceConflict("source verifier busy")
        owned, primary, result = [], None, None
        try:
            if self._halted or frame["nonce"] in self._seen or len(self._seen) >= 64:
                raise SourceConflict("terminal or halted source review")
            self._seen.add(frame["nonce"])

            def check():
                tick = self._clock()
                if (type(tick) is not int or tick < self._last_tick
                        or not frame["issued_tick"] <= tick < frame["expires_tick"]):
                    raise SourceConflict("source deadline or clock changed")
                self._last_tick = tick
                owner = current_owner()
                if (type(owner) is not tuple or len(owner) != 3
                        or type(owner[0]) is not str or type(owner[1]) is not tuple
                        or len(owner[1]) != 2 or any(type(n) is not int for n in owner[1])
                        or type(owner[2]) is not int or owner != binding):
                    raise SourceConflict("source Owner binding changed")
                # Include observer latency; no time-of-check deadline extension.
                tick = self._clock()
                if type(tick) is not int or not self._last_tick <= tick < frame["expires_tick"]:
                    raise SourceConflict("source observer exceeded deadline")
                self._last_tick = tick

            check()
            samples = []
            for name in (*reversed(ancestors), path):
                directory = name != path
                # FILE_READ_ATTRIBUTES (+ FILE_READ_DATA for the file), share=0,
                # OPEN_EXISTING, OPEN_REPARSE_POINT (+ BACKUP_SEMANTICS dirs).
                handle = self._backend.open_read_handle(name, 0x80 if directory else 0x81,
                    0, 3, 0x200000 | (0x2000000 if directory else 0))
                if type(handle) is not int or not 0 < handle < 2**64 - 16:
                    raise SourceConflict("invalid borrowed backend return")
                owned.append(handle)
                check()
                record = self._record(self._backend.inspect_handle(handle), name, directory)
                samples.append((name, directory, handle, record))
                check()
            file_record = samples[-1][3]
            if file_record.identity != before.identity:
                raise SourceConflict("opened source identity changed")
            parent_records = tuple((name, record.identity[0], record.identity[1])
                for name, _, _, record in reversed(samples[:-1]))
            if parent_records != before.parents:
                raise SourceConflict("opened ancestor identity changed")
            actual = self._backend.read_bounded(owned[-1], MAX_SOURCE + 1)
            check()
            if type(actual) is not bytes or actual != before.data or len(actual) != file_record.identity[2]:
                raise SourceConflict("opened source bytes changed")
            for name, directory, handle, first in reversed(samples):
                if self._record(self._backend.inspect_handle(handle), name, directory) != first:
                    raise SourceConflict("source/ancestor changed during inspection")
                check()
            result = SourceHandleDryRun("DATA_DRY_RUN_MATCH", sha256(raw).hexdigest(),
                frame["before_sha256"], frame["after_sha256"], frame["nonce"])
        except BaseException as exc:
            primary = exc
        finally:
            failed = False
            for handle in reversed(owned):
                try:
                    if self._backend.close(handle) is not True:
                        failed = True
                except BaseException:
                    failed = True
            if failed:
                self._halted = True
            self._slot.release()
        if failed:
            if isinstance(primary, (KeyboardInterrupt, SystemExit)):
                raise primary
            raise OSError("source handle cleanup unknown; verifier halted")
        if primary is not None:
            raise primary
        # Cleanup latency is inside the result deadline as well.
        check()
        return result
