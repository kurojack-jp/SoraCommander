"""Injection-only NON-SECRET synthetic DEV settings. Never active Owner state.

Only trusted local test/bootstrap code supplies an existing scratch directory.
No default live path, environment lookup, MCP/request path, GUI or callback API.
Production storage provenance/ACL and activation require separate Owner review.
"""
import json
import os
from pathlib import Path
import stat
import secrets

from .owner_review_i18n import available_locales
from .owner_review_request_binding_candidate import bind_command_frame, bind_v2_frame


class SyntheticLocaleSettings:
    """Small restartable adapter, scoped to an explicitly injected test folder.

    Ordinary inherited ACLs are intentional for fake locale data, not secrets.
    This is not a hardened production store or a concurrent writer protocol.
    """
    def __init__(self, *, synthetic_directory):
        if not isinstance(synthetic_directory, Path) or not synthetic_directory.is_absolute():
            raise ValueError("trusted absolute synthetic directory required")
        self._directory = synthetic_directory
        self._path = synthetic_directory / "owner-ui-locale.dev.json"
        self._verify()

    def _verify(self):
        for directory in (self._directory, *self._directory.parents):
            if directory.is_symlink() or directory.is_junction() or not directory.is_dir():
                raise ValueError("unsafe synthetic settings directory")
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("unsafe synthetic settings file")

    @staticmethod
    def options():
        return available_locales()

    def selected_locale(self):
        self._verify()
        try:
            with self._path.open("rb") as source:
                raw = source.read(257)
        except FileNotFoundError:
            return "en"
        # Exact canonical bytes only: duplicates, extras, malformed/oversize
        # data and unknown preferences all yield the English default.
        for locale in self.options():
            if raw == self._encoded(locale):
                return locale
        return "en"

    @staticmethod
    def _encoded(locale):
        return (json.dumps({"ui.owner_dialog.locale": locale}) + "\n").encode("ascii")

    def select_locale(self, locale):
        if type(locale) is not str or locale not in self.options():
            raise ValueError("unsupported Owner UI locale preference")
        self._verify()
        # Synthetic-only atomic replacement preserves old bytes on write failure.
        # Not a production provenance, locking or race-resistance protocol.
        pending = self._directory / ("locale-" + secrets.token_hex(24) + ".dev.tmp")
        created = False
        try:
            with pending.open("xb") as target:
                created = True
                raw = self._encoded(locale)
                if target.write(raw) != len(raw):
                    raise OSError("short synthetic settings write")
                target.flush()
                os.fsync(target.fileno())
            self._verify()
            os.replace(pending, self._path)
            created = False
        except PermissionError:
            # No denied-operation cleanup retry. Surface any leftover to caller.
            raise
        except BaseException:
            if created:
                pending.unlink()
            raise

    def present_command(self, frame):
        return bind_command_frame(frame, locale=self.selected_locale())

    def present_v2(self, frame):
        return bind_v2_frame(frame, locale=self.selected_locale())
