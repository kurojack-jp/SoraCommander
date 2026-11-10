"""Owner-configured OGF cues; every send uses the normal command review."""
import base64
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time

from .task_presets import _plain_file, _no_duplicates, tasks_file, TaskPresetError

ACTIONS = ('none', 'wave', 'nod', 'shake_head', 'small_guts_pose', 'celebrate')
EMOTIONS = ('neutral', 'happy', 'excited', 'curious', 'surprised', 'concerned', 'sad', 'proud')
GAZES = ('neutral', 'user', 'content', 'away')
UUID = re.compile(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z')
PROFILE = re.compile(r'[A-Za-z0-9._-]{1,64}\Z')


class OgfBridgeError(ValueError):
    pass


def validate(session_id, action, emotion, intensity, gaze, message=None,
             ttl_ms=3000, wait_seconds=50):
    if (type(session_id) is not str or not UUID.fullmatch(session_id)
            or type(action) is not str or action not in ACTIONS
            or type(emotion) is not str or emotion not in EMOTIONS
            or type(gaze) is not str or gaze not in GAZES
            or type(intensity) not in (int, float) or not math.isfinite(intensity)
            or not 0 <= intensity <= 1
            or type(ttl_ms) is not int or not 100 <= ttl_ms <= 10000
            or type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds)
            or not 0 <= wait_seconds <= 90
            or (message is not None and (type(message) is not str or len(message) > 120
                or any(ord(c) < 32 or 127 <= ord(c) <= 159
                       or 0xD800 <= ord(c) <= 0xDFFF for c in message)))):
        raise ValueError('invalid OGF cue')


def protected_storage():
    from .owner_storage import ensure_owner_storage, _windows_api, _pin_parents, _create_private_directory
    from .stdio_main import owner_journal_path
    ensure_owner_storage()
    parent = owner_journal_path().parent
    folder = parent / 'ogf-cue'
    api = _windows_api()
    with _pin_parents(parent, api):
        _create_private_directory(folder, api)
    owner_journal_path(storage_override=folder)  # Exact protected Owner/SYSTEM/Admin DACL.
    return folder


def cleanup(folder):
    for path in folder.iterdir():
        if re.fullmatch(r'[0-9a-f]{32}\.session', path.name):
            _plain_file(path)
            if time.time() - path.stat().st_mtime > 600:
                path.unlink()


@dataclass(frozen=True)
class OgfBridge:
    inbox: str
    profile_id: str

    def startup(self):
        cleanup(protected_storage())

    def send(self, host, **arguments):
        validate(**arguments)
        try:
            folder = protected_storage()
            cleanup(folder)
            state = folder / 'sequence.sqlite3'
            if state.exists() or state.is_symlink():
                _plain_file(state)
            # SQLite serializes reservations across threads/processes. Commit
            # before review; a rejected/failed launch still consumes its number.
            connection = sqlite3.connect(state, timeout=0)
            try:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('CREATE TABLE IF NOT EXISTS sequences (session_hash TEXT PRIMARY KEY, sequence INTEGER NOT NULL)')
                key = hashlib.sha256(arguments['session_id'].lower().encode('ascii')).hexdigest()
                row = connection.execute('SELECT sequence FROM sequences WHERE session_hash=?', (key,)).fetchone()
                previous = row[0] if row else 0
                if type(previous) is not int or not 0 <= previous < 1000000:
                    return {'status': 'DENY', 'error': 'OgfSequenceExhausted'}
                sequence = previous + 1
                connection.execute('INSERT OR REPLACE INTO sequences VALUES (?, ?)', (key, sequence))
                connection.commit()
            finally:
                connection.close()
            session_file = folder / (secrets.token_hex(16) + '.session')
            with session_file.open('x', encoding='ascii') as stream:
                stream.write(arguments['session_id'])
            checkout = Path(__file__).resolve().parents[2]
            message = arguments.get('message')
            encoded = '-' if message is None else base64.b64encode(message.encode('utf-8')).decode('ascii')
            argv = ['-I', str(checkout / 'tools' / 'scn_ogf_cue.py'),
                    '--inbox', self.inbox, '--profile', self.profile_id,
                    '--sequence', str(sequence), '--ttl-ms', str(arguments.get('ttl_ms', 3000)),
                    '--action', arguments['action'], '--emotion', arguments['emotion'],
                    '--intensity', str(arguments['intensity']), '--gaze', arguments['gaze'],
                    '--message-b64', encoded, '--session-file', str(session_file)]
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            return {'status': 'DENY', 'error': 'OgfStorageUnavailable'}
        return host.run_command_wait(executable=str(Path(sys.executable).absolute()),
            argv=argv, cwd=str(checkout), timeout=15, output_limit=4096,
            wait_seconds=arguments.get('wait_seconds', 50))


def load_ogf_bridge(environ=None):
    path = tasks_file(environ)
    if path is None:
        return None
    path = path.with_name('ogf-bridge.json')
    try:
        # Same ancestor checks even for a missing configuration file.
        for part in (*reversed(path.parents), path):
            try:
                info = part.lstat()
            except FileNotFoundError:
                return None
            import stat
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError()
        _plain_file(path)
        with path.open('rb') as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            raise ValueError()
        raw = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_no_duplicates)
        if type(raw) is not dict or set(raw) != {'inbox', 'profile_id'}:
            raise ValueError()
        inbox, profile = raw['inbox'], raw['profile_id']
        if (type(inbox) is not str or '\0' in inbox or not Path(inbox).is_absolute()
                or Path(inbox).name != 'inbox.json' or Path(inbox).parent.name != 'external-brain'
                or type(profile) is not str or not PROFILE.fullmatch(profile)):
            raise ValueError()
        return OgfBridge(inbox, profile)
    except (OSError, ValueError, TaskPresetError):
        raise OgfBridgeError('invalid or unreadable configuration') from None
