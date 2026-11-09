"""Phase9L disposable filesystem A/B crash recovery laboratory.

Not an installer. Deliberately uses ONLY explicitly created EMPTY sandbox
roots with an exact sentinel. Does not run candidate code or change Windows
ACLs, services, credentials, remote connectivity or production files.
"""
import json
import os
from pathlib import Path
import shutil
import stat

class SandboxUpdateError(ValueError):
    pass

MARKER = ".sora-phase9l-disposable"
STATE = "state.json"
SLOTS = ("A", "B")

def _root(path):
    p = Path(path)
    if not p.is_absolute() or not p.is_dir() or p.is_symlink():
        raise SandboxUpdateError("absolute ordinary sandbox root required")
    if not (p / MARKER).is_file() or (p / MARKER).read_text("ascii") != "PHASE9L_TEST_ONLY\n":
        raise SandboxUpdateError("not an expressly marked disposable test root")
    if p.is_junction() if hasattr(p, "is_junction") else False:
        raise SandboxUpdateError("linked sandbox forbidden")
    for a in (p, *p.parents):
        if a.is_symlink() or (hasattr(a, "is_junction") and a.is_junction()):
            raise SandboxUpdateError("linked ancestor")
    return p

def _atomic_write(p, obj):
    data = (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    temp = p.with_name(p.name + ".tmp")
    if temp.exists():
        raise SandboxUpdateError("leftover temp journal requires manual inspection")
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(p)
    finally:
        if temp.exists():
            temp.unlink()

def _validate(record):
    if (type(record) is not dict or set(record) != {"active", "sequence", "high_water", "pending", "pending_sequence", "phase", "previous", "previous_sequence"}
            or record["active"] not in SLOTS
            or type(record["sequence"]) is not int or record["sequence"] < 1
            or type(record["high_water"]) is not int or record["high_water"] < record["sequence"]
            or record["phase"] not in ("STABLE", "STAGED", "NEEDS_HEALTH", "RECOVERED")
            or (record["pending"] is not None and record["pending"] not in SLOTS)
            or (record["previous"] is not None and record["previous"] not in SLOTS)):
        raise SandboxUpdateError("invalid journal")
    for key in ("pending_sequence", "previous_sequence"):
        if record[key] is not None and (type(record[key]) is not int or record[key] < 1):
            raise SandboxUpdateError("invalid sequence journal")
    return record

class DisposableAB:
    @classmethod
    def create(cls, root):
        p = Path(root)
        if p.exists() or not p.parent.is_dir() or p.parent.is_symlink():
            raise SandboxUpdateError("refuse existing/non-absolute sandbox")
        if not p.is_absolute():
            raise SandboxUpdateError("absolute temp root required")
        p.mkdir()
        (p / MARKER).write_text("PHASE9L_TEST_ONLY\n", encoding="ascii")
        for slot in SLOTS:
            (p / slot).mkdir()
        (p / "A" / "payload.txt").write_text("TEST_VERSION_1\n", encoding="ascii")
        _atomic_write(p / STATE, dict(active="A", sequence=1, high_water=1,
                       pending=None, pending_sequence=None, previous=None,
                       previous_sequence=None, phase="STABLE"))
        return cls(p)

    def __init__(self, root):
        self.root = _root(root)
        self._read()

    def _read(self):
        p = _root(self.root)
        if set(x.name for x in p.iterdir()) != {MARKER, STATE, "A", "B"}:
            raise SandboxUpdateError("unexpected sandbox entries")
        for slot in SLOTS:
            s = p / slot
            if not s.is_dir() or s.is_symlink() or (hasattr(s, "is_junction") and s.is_junction()):
                raise SandboxUpdateError("linked slot forbidden")
            if set(x.name for x in s.iterdir()) - {"payload.txt"}:
                raise SandboxUpdateError("unexpected slot entries")
        raw = (p / STATE).read_bytes()
        if len(raw) > 2048:
            raise SandboxUpdateError("oversized state")
        record = json.loads(raw.decode("ascii"))
        _validate(record)
        return record

    def snapshot(self):
        return dict(self._read())

    def stage(self, version, *, validated_preview=False):
        r = self._read()
        if validated_preview is not True or type(version) is not int or version <= r["high_water"] or r["phase"] != "STABLE":
            raise SandboxUpdateError("unverified/old/overlapping update")
        other = "B" if r["active"] == "A" else "A"
        target = self.root / other / "payload.txt"
        target.write_text(f"TEST_VERSION_{version}\n", encoding="ascii")
        r.update(pending=other, pending_sequence=version, phase="STAGED")
        _atomic_write(self.root / STATE, r)
        return self.snapshot()

    def switch(self, *, owner_simulated_ack=False):
        r = self._read()
        if owner_simulated_ack is not True or r["phase"] != "STAGED":
            raise SandboxUpdateError("cannot switch")
        r.update(previous=r["active"], previous_sequence=r["sequence"],
                 active=r["pending"], sequence=r["pending_sequence"],
                 high_water=r["pending_sequence"], pending=None, pending_sequence=None,
                 phase="NEEDS_HEALTH")
        _atomic_write(self.root / STATE, r)
        return self.snapshot()

    def health(self, *, healthy):
        r = self._read()
        if type(healthy) is not bool or r["phase"] != "NEEDS_HEALTH":
            raise SandboxUpdateError("invalid health transition")
        if healthy:
            r.update(previous=None, previous_sequence=None, phase="STABLE")
        else:
            r.update(active=r["previous"], sequence=r["previous_sequence"],
                     previous=None, previous_sequence=None, phase="RECOVERED")
        _atomic_write(self.root / STATE, r)
        return self.snapshot()

    def recover_after_crash(self):
        r = self._read()
        if r["phase"] == "NEEDS_HEALTH":
            return self.health(healthy=False)
        if r["phase"] == "STAGED":
            r.update(pending=None, pending_sequence=None, phase="STABLE")
            _atomic_write(self.root / STATE, r)
        return self.snapshot()

    def finalize_recovery(self):
        r = self._read()
        if r["phase"] != "RECOVERED":
            raise SandboxUpdateError("not recovering")
        r["phase"] = "STABLE"
        _atomic_write(self.root / STATE, r)
        return self.snapshot()
