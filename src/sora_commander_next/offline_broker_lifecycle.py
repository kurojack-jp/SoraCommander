"""Pure offline Broker lifecycle reducer. No process, IPC, ACL, or grants.

This is a design candidate only; production authority and locking must come
from an independently trusted Owner broker.
"""
from dataclasses import dataclass, replace

STATES = frozenset({"STOPPED", "STARTING", "READY", "DRAINING", "FAULTED"})
EVENTS = frozenset({"begin", "ready", "connect", "disconnect", "drain", "stopped", "fault"})


class LifecycleRejected(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    state: str = "STOPPED"
    epoch: int = 0
    connections: int = 0
    execution_enabled: bool = False

    def __post_init__(self):
        if (self.state not in STATES or type(self.epoch) is not int
                or not 0 <= self.epoch < 2**63
                or type(self.connections) is not int
                or not 0 <= self.connections <= 1
                or (self.state != "READY" and self.connections != 0)
                or type(self.execution_enabled) is not bool
                or self.execution_enabled):
            raise LifecycleRejected("invalid or privileged lifecycle snapshot")


def advance(snapshot, event):
    """A state update only: never launch, grant, retry, or stop a real job."""
    if type(snapshot) is not Snapshot or type(event) is not str or event not in EVENTS:
        raise LifecycleRejected("invalid lifecycle transition")
    state = snapshot.state
    if event == "begin" and state == "STOPPED":
        if snapshot.epoch == 2**63 - 1:
            raise LifecycleRejected("epoch exhausted")
        return Snapshot("STARTING", snapshot.epoch + 1)
    if event == "ready" and state == "STARTING":
        return replace(snapshot, state="READY")
    if event == "connect" and state == "READY" and snapshot.connections == 0:
        # This marks only an *externally verified* connection observation.
        # Caller must independently validate the OS client credential.
        return replace(snapshot, connections=1)
    if event == "disconnect" and state == "READY":
        return replace(snapshot, connections=0)
    if event == "drain" and state in ("STARTING", "READY"):
        return replace(snapshot, state="DRAINING", connections=0)
    if event == "stopped" and state == "DRAINING":
        return replace(snapshot, state="STOPPED", connections=0)
    if event == "fault" and state in ("STARTING", "READY", "DRAINING"):
        return replace(snapshot, state="FAULTED", connections=0)
    # Faulted state needs an external Owner-reviewed recovery procedure.
    raise LifecycleRejected("transition denied")


def describe(snapshot):
    """Read-only metadata; never claims OS-backed uniqueness or a grant."""
    if type(snapshot) is not Snapshot:
        raise LifecycleRejected("invalid lifecycle state")
    return {"state": snapshot.state, "epoch": snapshot.epoch,
            "connections": snapshot.connections,
            "accepting_connections": snapshot.state == "READY"
                                     and snapshot.connections == 0,
            "execution_authorized": False, "jobs_replayed": False,
            "broker_process_verified": False,
            "client_identity_verified": False}
