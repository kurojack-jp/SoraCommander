"""DEV_ONLY / NOT_REGISTERED: in-memory storage protocol experiment ONLY.

No filesystem adapter, OS identity, default path, GUI, or approval authority.
flush/fsync/atomic replace below are FAKE events, not Windows guarantees.
"""
from dataclasses import dataclass, replace
from threading import RLock

DEV_ONLY = True
NOT_REGISTERED = True
PROPOSED_RELATIVE_PATH = "SoraCommanderNext/preferences/owner-ui.json"
MAX_BYTES = 256
KEY = "ui.owner_dialog.locale"


class MalformedPreference(ValueError):
    """Bytes are not the exact single-key canonical preference."""


class TamperedPreference(ValueError):
    """Synthetic provenance or object identity cannot be trusted."""


class ChangedDestination(TamperedPreference):
    """Ambiguous/stale destination: do not retry."""


def encode_locale(locale):
    if type(locale) is not str or locale not in ("en", "ja"):
        raise ValueError("explicit en/ja required")
    return ('{"' + KEY + '": "' + locale + '"}\n').encode("ascii")


@dataclass(frozen=True)
class FakeObject:
    """Untrusted fake observations. owner is a TEST label, never a Windows SID."""
    raw: bytes = b""
    exists: bool = False
    owner: str = "synthetic-owner"
    trusted_parent: bool = True
    regular: bool = True
    symlink: bool = False
    reparse: bool = False
    links: int = 1
    generation: int = 0


@dataclass(frozen=True)
class Preference:
    status: str
    locale: str


class FakeAuthority:
    """Pure memory fake; deliberately cannot address a directory or live file.

    A lock and generation represent an indivisible compare-and-replace primitive.
    No portable stat/recheck/os.replace sequence is claimed equivalent to it.
    fail_at injects a crash BEFORE the named event (never retries or rolls back).
    """
    def __init__(self, *, initial=None, owner="synthetic-owner"):
        if type(owner) is not str or owner != "synthetic-owner":
            raise TamperedPreference("only fixed fake owner supported")
        self.owner = owner
        self.object = FakeObject() if initial is None else initial
        self.lock = RLock()
        self.events = []
        self.fail_at = None

    def _step(self, event):
        self.events.append(event)
        if self.fail_at == event:
            raise OSError("injected fake crash at " + event)

    def observe(self):
        with self.lock:
            self._step("read")
            obj = self.object
            _validate_object(obj, self.owner)
            # Only a fixed-size prefix escapes the fake storage boundary.
            return replace(obj, raw=obj.raw[:MAX_BYTES + 1]), obj

    def replace_if_unchanged(self, expected, raw):
        with self.lock:
            self._step("stage")
            staged = bytes(raw)
            self._step("flush")
            self._step("fsync")
            self._step("replace")
            _validate_object(self.object, self.owner)
            if self.object is not expected:
                raise ChangedDestination("destination changed; no replacement")
            # This assignment is the FAKE atomic visibility point.
            self.object = replace(expected, raw=staged, exists=True,
                                  generation=expected.generation + 1)


def _validate_object(obj, owner):
    if type(obj) is not FakeObject:
        raise TamperedPreference("invalid fake observation type")
    flags = (obj.exists, obj.trusted_parent, obj.regular, obj.symlink, obj.reparse)
    if (any(type(flag) is not bool for flag in flags)
            or type(obj.raw) is not bytes
            or type(obj.owner) is not str or obj.owner != owner
            or obj.trusted_parent is not True or obj.regular is not True
            or obj.symlink is not False or obj.reparse is not False
            or type(obj.links) is not int or obj.links != 1
            or type(obj.generation) is not int or obj.generation < 0
            or (not obj.exists and obj.raw != b"")):
        raise TamperedPreference("untrusted synthetic binding/object")


def _decode(obj):
    if not obj.exists:
        return Preference("MISSING", "en")  # no write or repair
    for locale in ("en", "ja"):
        if obj.raw == encode_locale(locale):
            return Preference("PRESENT", locale)
    # Byte equality rejects duplicates, extras, wrong types, BOM, whitespace,
    # invalid UTF-8 and oversize input without permissive JSON parsing.
    raise MalformedPreference("noncanonical or oversized locale bytes")


class DevLocaleStore:
    """Trusted bootstrap injects exactly this fake; request paths have no API."""
    def __init__(self, *, authority):
        if type(authority) is not FakeAuthority:
            raise TypeError("explicit in-memory FakeAuthority required")
        self._authority = authority

    def read(self):
        observed, _ = self._authority.observe()
        return _decode(observed)

    def selected_locale(self):
        return self.read().locale

    def select_locale(self, locale):
        raw = encode_locale(locale)
        observed, expected = self._authority.observe()
        _decode(observed)  # never overwrite malformed existing data
        self._authority.replace_if_unchanged(expected, raw)

    def present_command(self, frame):
        from .owner_review_request_binding_candidate import bind_command_frame
        return bind_command_frame(frame, locale=self.selected_locale())

    def present_v2(self, frame):
        from .owner_review_request_binding_candidate import bind_v2_frame
        return bind_v2_frame(frame, locale=self.selected_locale())
