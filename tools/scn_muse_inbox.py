"""Identity-gated, write-only Muse bridge inbox."""

import hashlib
import os
from pathlib import Path
import re
import stat
from threading import Lock
from typing import Any
import unicodedata
from uuid import uuid4

from sora_commander_next.activity import publish, utc_now
from sora_commander_next.file_reads import _require_no_network_link


MAX_CONTENT = 256 * 1024
MAX_FILES = 200
MAX_TOTAL = 32 * 1024 * 1024
EXTENSIONS = {".md", ".txt", ".json"}
DEVICES = re.compile(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]", re.IGNORECASE)


class InboxUnavailable(ValueError):
    pass


def _reparse(info):
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _check_folder(folder, *, create=False):
    # Check the leaf itself before following any links in the v0.3.0 walk.
    # Walk the parents first so lstat never follows an unchecked parent.
    _require_no_network_link(folder.parent, InboxUnavailable)
    try:
        info = folder.lstat()
    except FileNotFoundError:
        if not create:
            raise
        folder.mkdir()
        info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or _reparse(info):
        raise InboxUnavailable("inbox is not an ordinary directory")
    _require_no_network_link(folder, InboxUnavailable)


def _name_error(filename):
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 100
            or filename[0] in " ." or filename[-1] in " ." or ".." in filename
            or any(ch in '\\/:<>|?*"' or unicodedata.category(ch) in {"Cc", "Cs"} for ch in filename)
            or DEVICES.fullmatch(filename.split(".", 1)[0].rstrip(" "))):
        return "NAME"
    if Path(filename).suffix.lower() not in EXTENSIONS:
        return "EXTENSION"
    return None


def _refuse(reason):
    print(f"[bridge] inbox refused: {reason}")
    return {"status": "REFUSED", "reason": reason}


class Inbox:
    def __init__(self, profile, store):
        home = Path(profile)
        if not home.is_absolute() or home.drive.startswith("\\"):
            raise InboxUnavailable("absolute local USERPROFILE required")
        self.folder = home / ".config" / "sora-commander" / "muse-inbox"
        _check_folder(self.folder, create=True)
        self.store = store
        self.lock = Lock()

    def drop(self, filename, content):
        reason = _name_error(filename)
        if reason:
            return _refuse(reason)
        if not isinstance(content, str) or "\0" in content:
            return _refuse("NOT_UTF8")
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            return _refuse("NOT_UTF8")
        if len(data) > MAX_CONTENT:
            return _refuse("TOO_LARGE")

        with self.lock:
            try:
                _check_folder(self.folder)
            except (InboxUnavailable, FileNotFoundError, NotADirectoryError):
                return _refuse("LINKED_FOLDER")
            except OSError:
                return _refuse("WRITE_FAILED")

            try:
                reason = self._write(filename, data)
            except OSError:
                return _refuse("WRITE_FAILED")
            if reason:
                return _refuse(reason)
            digest = hashlib.sha256(data).hexdigest()
            pending_id = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:32]
            publish(self.store, operation_type="inbox_drop", state="CREATED",
                    source="inbox", pending_id=pending_id)
            print(f"[bridge] inbox drop: {utc_now()} {filename} {len(data)} {digest}")
            return {"status": "CREATED", "filename": filename, "bytes": len(data), "sha256": digest}

    def _write(self, filename, data):
        count, total = 0, len(data)
        for entry in self.folder.iterdir():
            info = entry.lstat()
            if stat.S_ISREG(info.st_mode) and not _reparse(info):
                count += 1
                total += info.st_size
                if count >= MAX_FILES or total > MAX_TOTAL:
                    return "INBOX_FULL"

        temporary = self.folder / f".inbox-{uuid4().hex}.tmp"
        stream = temporary.open("xb")
        try:
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, self.folder / filename)
            except FileExistsError:
                return "EXISTS"
        finally:
            temporary.unlink()
        return None


def register_inbox(server, store, profile):
    inbox = Inbox(profile, store)

    @server.tool()
    def inbox_drop(filename: str, content: str) -> dict[str, Any]:
        """Create one .md, .txt or .json inbox file with at most 256 KiB of UTF-8 text.

        Returns CREATED with filename, bytes and SHA-256, or REFUSED with a reason.
        On any refusal REPORT_AND_WAIT. Never retry the same name. Use a new name
        for a subsequent authorized drop. The tool never returns file content.
        """
        return inbox.drop(filename, content)
