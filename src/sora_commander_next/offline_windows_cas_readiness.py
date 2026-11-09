"""DEV_ONLY commit-readiness contract. This does NOT grant file-write authority."""
from dataclasses import dataclass
from .source_review import SourceConflict

@dataclass(frozen=True, slots=True)
class WindowsCasReadiness:
    exact_parent_handles: bool
    exact_target_handle: bool
    exclusive_writer_control: bool
    mapping_writers_excluded: bool
    atomic_identity_compare_and_swap: bool
    durable_postwrite_identity: bool
    owner_bound_one_time_review: bool
    authenticated_broker: bool

    def assess(self):
        fields = tuple(self.__dataclass_fields__)
        if any(type(getattr(self, name)) is not bool for name in fields):
            raise SourceConflict("unverified CAS readiness value")
        missing = tuple(name for name in fields if not getattr(self, name))
        return {"status": "HOLD" if missing else "REVIEW_REQUIRED",
                "missing": missing, "write_authorized": False}

    def commit(self, *args, **kwargs):
        raise SourceConflict("STRONG_SOURCE_CAS_NOT_ACCEPTED")
