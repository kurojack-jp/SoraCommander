"""Phase9T: offline, strict installed distribution inventory comparison.

This is not a wheel lock or publisher signature. It has no write paths.
"""
from dataclasses import dataclass
from importlib import metadata
import platform
import sys

class EnvironmentMismatch(ValueError):
    pass

@dataclass(frozen=True)
class EnvironmentSnapshot:
    python: str
    architecture: str
    packages: tuple[tuple[str,str],...]
    artifact_verified: bool = False
    install_permitted: bool = False

def installed_snapshot():
    if sys.platform != "win32":
        raise EnvironmentMismatch("Windows runtime required")
    packages=[]
    for d in metadata.distributions():
        name=d.metadata.get("Name")
        version=d.version
        if not name or not version:
            raise EnvironmentMismatch("invalid installed distribution")
        packages.append((name.lower().replace("_","-"),version))
    packages.sort()
    if len({k for k,v in packages}) != len(packages):
        raise EnvironmentMismatch("duplicate package identities")
    return EnvironmentSnapshot(
        python=".".join(str(x) for x in sys.version_info[:3]),
        architecture=platform.machine().lower(), packages=tuple(packages))

def compare_environment(reference, observed):
    if type(reference) is not EnvironmentSnapshot or type(observed) is not EnvironmentSnapshot:
        raise EnvironmentMismatch("snapshots required")
    if (reference.python != observed.python or
            reference.architecture != observed.architecture or
            reference.packages != observed.packages):
        raise EnvironmentMismatch("runtime distribution set drift")
    return {"status":"INSTALLED_METADATA_MATCH_ONLY",
            "wheel_hashes_verified":False,
            "publisher_authenticated":False,
            "installation_permitted":False}
