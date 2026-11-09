"""Phase9U: strict offline SHA-256 wheelhouse inventory verification."""
import hashlib
import json
from pathlib import Path
import re

class WheelhouseMismatch(ValueError):
    pass

def inventory(root):
    root=Path(root)
    if not root.is_dir() or root.is_symlink():
        raise WheelhouseMismatch("invalid wheelhouse root")
    result=[]
    for p in sorted(root.iterdir(),key=lambda q:q.name.lower()):
        if p.is_symlink() or not p.is_file() or p.suffix != ".whl":
            raise WheelhouseMismatch("unexpected wheelhouse entry")
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl",p.name):
            raise WheelhouseMismatch("invalid wheel filename")
        h=hashlib.sha256()
        with p.open("rb") as stream:
            for part in iter(lambda: stream.read(1024*1024),b""):
                h.update(part)
        result.append({"filename":p.name,"sha256":h.hexdigest(),"bytes":p.stat().st_size})
    if not result or len({p["filename"].lower() for p in result})!=len(result):
        raise WheelhouseMismatch("empty or duplicate wheelhouse")
    return result

def verify(root,expected):
    if type(expected) is not list or not expected:
        raise WheelhouseMismatch("external reviewed inventory required")
    for item in expected:
        if (type(item) is not dict or set(item)!={"filename","sha256","bytes"}
            or type(item["filename"]) is not str
            or type(item["sha256"]) is not str
            or re.fullmatch("[0-9a-f]{64}",item["sha256"]) is None
            or type(item["bytes"]) is not int or item["bytes"] <= 0):
            raise WheelhouseMismatch("invalid inventory record")
    actual=inventory(root)
    if actual != expected:
        raise WheelhouseMismatch("wheelhouse file/hash/size drift")
    return {"wheel_count":len(actual),"hashes_match_supplied_inventory":True,
            "publisher_authenticated":False,"install_permitted":False}

def audit_required_distributions(expected, *, required=("pywin32",),
                                 target_python="cp313",
                                 target_platform="win_amd64",
                                 minimum_pywin32=306):
    """Offline filename/target check, not package resolution or trust.

    Explicit target tags are supplied by the release reviewer, not inferred
    from the machine running this audit. This does NOT authorize install.
    """
    if (type(expected) is not list or not expected
            or type(required) is not tuple or not required
            or any(type(s) is not str or
                   re.fullmatch(r"[A-Za-z0-9_.-]+", s) is None
                   for s in required)
            or type(target_python) is not str
            or re.fullmatch(r"cp[0-9]{2,3}", target_python) is None
            or target_platform not in ("win_amd64", "win_arm64", "win32")
            or type(minimum_pywin32) is not int
            or not 306 <= minimum_pywin32 <= 9999):
        raise WheelhouseMismatch("invalid required-distribution audit input")
    names = set()
    for record in expected:
        if type(record) is not dict or type(record.get("filename")) is not str:
            raise WheelhouseMismatch("invalid wheel inventory entry")
        filename = record["filename"]
        if not filename.endswith(".whl"):
            raise WheelhouseMismatch("not a wheel filename")
        bits = filename[:-4].split("-")
        if len(bits) not in (5, 6):
            raise WheelhouseMismatch("unrecognized wheel tag format")
        # Wheel: distribution-version(-build)-python-abi-platform.
        dist, version, python_tag, abi_tag, platform_tag = (
            bits[0], bits[1], bits[-3], bits[-2], bits[-1])
        if not all((dist, version, python_tag, abi_tag, platform_tag)):
            raise WheelhouseMismatch("incomplete wheel tags")
        name = re.sub(r"[-_.]+", "-", dist).lower()
        if name == "pywin32":
            if re.fullmatch(r"[0-9]+", version) is None:
                raise WheelhouseMismatch("invalid pywin32 version")
            if int(version) < minimum_pywin32:
                raise WheelhouseMismatch("pywin32 below minimum version")
            if (target_python not in python_tag.split(".")
                    or target_python not in abi_tag.split(".")):
                raise WheelhouseMismatch("pywin32 does not match target Python ABI")
            if target_platform not in platform_tag.split("."):
                raise WheelhouseMismatch("pywin32 does not match target Windows architecture")
        names.add(name)
    normalized = {re.sub(r"[-_.]+", "-", x).lower() for x in required}
    missing = sorted(normalized - names)
    return {"required_present": not missing, "missing": missing,
            "target_python": target_python, "target_platform": target_platform,
            "minimum_pywin32": minimum_pywin32,
            "install_permitted": False, "publisher_authenticated": False}


def verify_required_wheelhouse(root, expected, *, required=("pywin32",),
                               target_python="cp313",
                               target_platform="win_amd64",
                               minimum_pywin32=306):
    """Tie required package tags to the SAME byte-verified wheel inventory.

    Never infer trust from hashes supplied by the same caller; this preview
    cannot grant installation or launch rights.
    """
    verified = verify(root, expected)
    audited = audit_required_distributions(
        expected, required=required, target_python=target_python,
        target_platform=target_platform, minimum_pywin32=minimum_pywin32)
    if not audited["required_present"]:
        raise WheelhouseMismatch("required wheel distributions are missing")
    return {**verified, **audited}


def load_inventory(raw):
    try:
        value=json.loads(raw,object_pairs_hook=_unique)
    except (TypeError,ValueError) as exc:
        raise WheelhouseMismatch("bad inventory JSON") from exc
    if type(value) is not list:
        raise WheelhouseMismatch("wheel inventory array required")
    return value

def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise WheelhouseMismatch("duplicate JSON key")
        result[key]=value
    return result
