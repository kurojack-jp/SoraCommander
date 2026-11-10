"""One approved, local OGF semantic cue. Standard library only; no session output."""
import argparse
import base64
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import uuid

ACTIONS = ('none', 'wave', 'nod', 'shake_head', 'small_guts_pose', 'celebrate')
EMOTIONS = ('neutral', 'happy', 'excited', 'curious', 'surprised', 'concerned', 'sad', 'proud')
GAZES = ('neutral', 'user', 'content', 'away')


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('invalid arguments')


def ordinary_folder(path):
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('linked or non-directory path')


def send(argv):
    # Find and consume the session before parsing any other argument. argparse
    # errors may echo input, so neither its parser nor its errors reach output.
    try:
        if argv.count('--session-file') != 1:
            return 3
        session_path = Path(argv[argv.index('--session-file') + 1])
        with session_path.open('rb') as stream:
            data = stream.read(37)
        session_path.unlink()
    except (OSError, ValueError, IndexError):
        return 3
    try:
        session = data.decode('ascii')
        if not re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', session):
            raise ValueError()
        parser = QuietParser(exit_on_error=False, add_help=False, allow_abbrev=False)
        if any(argv.count('--' + key) != 1 for key in (
                'inbox', 'profile', 'sequence', 'ttl-ms', 'action', 'emotion',
                'intensity', 'gaze', 'message-b64', 'session-file')):
            raise ValueError()
        for key in ('inbox', 'profile', 'sequence', 'ttl-ms', 'action', 'emotion',
                    'intensity', 'gaze', 'message-b64', 'session-file'):
            parser.add_argument('--' + key, required=True)
        # parse_known_args avoids argparse's stderr/exit path for unknown flags.
        args, extra = parser.parse_known_args(argv)
        if extra:
            raise ValueError()
        sequence, ttl = int(args.sequence), int(args.ttl_ms)
        intensity = float(args.intensity)
        if (not 1 <= sequence <= 1000000 or not 100 <= ttl <= 10000
                or not math.isfinite(intensity) or not 0 <= intensity <= 1
                or args.action not in ACTIONS or args.emotion not in EMOTIONS
                or args.gaze not in GAZES
                or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', args.profile)):
            raise ValueError()
        message = (None if args.message_b64 == '-' else
                   base64.b64decode(args.message_b64, validate=True).decode('utf-8'))
        if message is not None and (len(message) > 120 or any(
                ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in message)):
            raise ValueError()
        inbox = Path(args.inbox)
        if not inbox.is_absolute() or inbox.name != 'inbox.json' or inbox.parent.name != 'external-brain':
            raise ValueError()
        ordinary_folder(inbox.parent)
        if os.path.lexists(inbox):
            print('one cue already waiting', file=sys.stderr)
            return 4
        body = {'version': 1, 'type': 'companion.intent', 'session': session,
                'profileId': args.profile, 'sequence': sequence, 'ttlMs': ttl,
                'control': {'emotion': args.emotion, 'intensity': intensity,
                            'action': args.action, 'gaze': args.gaze, 'message': message}}
        payload = json.dumps(body, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(payload) > 4096:
            raise ValueError()
    except (ValueError, OSError, UnicodeError, argparse.ArgumentError):
        return 2
    temporary = inbox.parent / ('.scn-cue-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows rename never replaces an existing destination. Refuse other
        # platforms rather than silently adopting POSIX's replacing rename.
        if os.name != 'nt':
            raise ValueError()
        temporary.rename(inbox)
    except FileExistsError:
        return 4
    except (OSError, ValueError):
        return 2
    finally:
        if temporary.exists():
            temporary.unlink()
    print(json.dumps({'status': 'SENT', 'sequence': sequence, 'action': args.action,
                      'emotion': args.emotion, 'gaze': args.gaze, 'bytes': len(payload)}))
    return 0


def main():
    try:
        return send(sys.argv[1:])
    except BaseException:
        # Never print paths, argument values or an exception containing a UUID.
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
