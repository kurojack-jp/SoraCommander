"""Phase9W hashed requirements derivation, NO installer authorization."""
import json,re
from pathlib import Path
from .release_wheelhouse import verify,WheelhouseMismatch
from .release_environment import EnvironmentMismatch

def make_require_hashes(wheelhouse,inventory_json,pins_text):
    try:
        expected=json.loads(Path(inventory_json).read_text(encoding="utf-8"))
        verify(wheelhouse,expected)
    except (OSError,ValueError,WheelhouseMismatch) as exc:
        raise EnvironmentMismatch("invalid wheel inventory") from exc
    pins=[line.strip() for line in Path(pins_text).read_text(encoding="ascii").splitlines() if line.strip()]
    if len(pins)!=len(expected):
        raise EnvironmentMismatch("pin/wheel count mismatch")
    wheelmap={}
    for item in expected:
        stem=item["filename"].split("-")
        if len(stem)<5:
            raise EnvironmentMismatch("unexpected wheel format")
        normalized=stem[0].lower().replace("_","-")
        version=stem[1]
        key=(normalized,version)
        if key in wheelmap:
            raise EnvironmentMismatch("ambiguous wheel choice")
        wheelmap[key]=item["sha256"]
    result=[]
    for pin in pins:
        if re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+",pin) is None:
            raise EnvironmentMismatch("invalid exact pin")
        name,version=pin.split("==",1)
        key=(name.lower().replace("_","-"),version)
        if key not in wheelmap:
            raise EnvironmentMismatch("unmatched package wheel")
        result.append(f"{name}=={version} --hash=sha256:{wheelmap.pop(key)}")
    if wheelmap:
        raise EnvironmentMismatch("unmatched wheel")
    return "\n".join(result)+"\n"
