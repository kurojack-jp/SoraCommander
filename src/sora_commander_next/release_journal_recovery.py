"""Phase9Z disposable journal recovery analysis; no protected state writes."""
import json
from pathlib import Path

class JournalRecoveryError(ValueError):
    pass

FIELDS={"sequence","high_water","phase"}
PHASES={"STABLE","PREPARED"}

def parse_record(raw):
    if type(raw) is not bytes or len(raw)>1024:
        raise JournalRecoveryError("invalid journal bytes")
    try:
        def unique(pairs):
            obj={}
            for k,v in pairs:
                if k in obj: raise JournalRecoveryError("duplicate journal keys")
                obj[k]=v
            return obj
        d=json.loads(raw.decode("ascii"),object_pairs_hook=unique)
    except (ValueError,UnicodeError) as exc:
        raise JournalRecoveryError("corrupt journal") from exc
    if (type(d) is not dict or set(d)!=FIELDS
        or type(d["sequence"]) is not int
        or type(d["high_water"]) is not int
        or not 0<=d["sequence"]<=d["high_water"]<2**63
        or type(d["phase"]) is not str
        or d["phase"] not in PHASES):
        raise JournalRecoveryError("invalid journal record")
    return d

def review_recovery(*, main_bytes, pending_bytes=None, external_protected_floor):
    if type(external_protected_floor) is not int or not 0<=external_protected_floor<2**63:
        raise JournalRecoveryError("external protected floor required")
    main=parse_record(main_bytes)
    if main["high_water"]<external_protected_floor:
        raise JournalRecoveryError("main journal rollback")
    pending=None if pending_bytes is None else parse_record(pending_bytes)
    if pending is not None:
        if pending["high_water"]<external_protected_floor:
            raise JournalRecoveryError("pending journal rollback")
        if pending["high_water"]<main["high_water"]:
            raise JournalRecoveryError("pending journal rollback vs main")
    return {"status":"MANUAL_REVIEW_ONLY",
            "pending_found":pending is not None,
            "observed_high_water":max(main["high_water"],pending["high_water"] if pending else 0),
            "automatic_recovery":False,"writes_allowed":False,
            "install_allowed":False,"remote_allowed":False}
