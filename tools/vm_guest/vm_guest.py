"""Prepare and run the flat VM bundle using only the standard library."""

import argparse
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
import hashlib
from importlib.machinery import ModuleSpec
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys


VERBS = ("check", "firewall-admin", "firewall-remove-admin", "install", "start-view",
         "start-full", "update", "netcheck", "remove")
AGENT_FILES = ("vm_agent.py", "vm_protocol.py", "vm_win.py", "vm_files.py", "vm_uia.py")
GUEST_FILES = ("vm_guest.py", *(verb + ".cmd" for verb in VERBS))
CODE_FILES = AGENT_FILES + GUEST_FILES
CONFIG = "vm-guest.json"
KEY = "vm-guest.key"
MANIFEST = "vm-guest.manifest.json"
HASHED_FILES = CODE_FILES + (CONFIG,)
BUNDLE_FILES = HASHED_FILES + (MANIFEST,)
CONFIG_LIMIT = 4096
CONFIG_FIELDS = {"format", "vm_name", "guest_user", "address", "host_address", "port",
                 "max_minutes", "max_inputs", "tools"}
RULE = "SoraVmAgent"
NETCHECK_OUTSIDE = (("1.1.1.1", 443), ("8.8.8.8", 53))
HOST_PORTS = (22, 80, 135, 139, 445, 3389, 5985, 8080)


class Refused(ValueError):
    pass


@dataclass
class Changes:
    began: bool = False


def guest_user(value):
    if (type(value) is not str or not 1 <= len(value) <= 20
            or not value.isprintable() or value != value.strip() or value.endswith(".")
            or not any(char != "." for char in value)
            or any(char in '\\/:*?"<>|[];=,+&%^!`()' for char in value)):
        raise Refused("invalid local account name")
    return value


def plain(path, *, directory, single_link=True):
    if (not path.is_absolute() or path.anchor.startswith(("\\\\", "//"))
            or any(part == ".." or ":" in part for part in path.parts[1:])):
        raise Refused("an ordinary absolute path is required")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        expected = stat.S_ISDIR if part != path or directory else stat.S_ISREG
        if (not expected(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or (part == path and not directory and single_link and info.st_nlink != 1)):
            raise Refused(f"not an ordinary {'directory' if directory else 'file'}: {part}")
    return info


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise Refused("duplicate configuration field")
    return result


def _constant(_):
    raise Refused("non-JSON constant")


def load_config(folder, protocol, *, data=None):
    path = folder / CONFIG
    try:
        if data is None:
            plain(path, directory=False)
            with path.open("rb") as stream:
                data = stream.read(CONFIG_LIMIT + 1)
        protocol.require(len(data) <= CONFIG_LIMIT)
        raw = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique, parse_constant=_constant)
        protocol.require(type(raw) is dict and raw.keys() == CONFIG_FIELDS)
        protocol.integer(raw["format"], 1, 1)
        protocol.vm_name(raw["vm_name"])
        guest_user(raw["guest_user"])
        address = protocol.address(raw["address"])
        host = protocol.address(raw["host_address"])
        protocol.require(address != host)
        protocol.integer(raw["port"], 1024, 65535)
        protocol.integer(raw["max_minutes"], 1, 30)
        protocol.integer(raw["max_inputs"], 1, 200)
        tools = raw["tools"]
        protocol.require(type(tools) is list and len(tools) <= len(protocol.TOOLS)
                         and all(type(tool) is str and tool in protocol.TOOLS for tool in tools))
        protocol.selected_tools(",".join(tools))
        return raw
    except (OSError, ValueError, RecursionError, protocol.ProtocolError):
        raise Refused("invalid or unreadable guest configuration") from None


def write_new(path, data, made):
    fd = os.open(path, os.O_WRONLY | os.O_EXCL | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    made[path] = None
    with os.fdopen(fd, "wb") as stream:
        made[path] = os.fstat(stream.fileno())
        if stream.write(data) != len(data):
            raise OSError("incomplete write")


def identity(info):
    # On Windows fstat's ctime may differ from lstat for the same file.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def recheck(path, expected, *, directory=False):
    current = plain(path, directory=directory)
    if ((not os.path.samestat(current, expected)) if directory else identity(current) != identity(expected)):
        raise Refused(f"file or directory changed: {path}")


def snapshot(path, *, limit=None):
    before = plain(path, directory=False)
    with path.open("rb") as stream:
        data = stream.read() if limit is None else stream.read(limit + 1)
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise Refused(f"file changed while reading: {path}")
    recheck(path, before)
    if limit is not None and len(data) > limit:
        raise Refused(f"file is too large: {path}")
    return data, before


def rollback(made, folder=None):
    left = []
    for path, identity in reversed(tuple(made.items())):
        try:
            if identity is None or not os.path.samestat(plain(path, directory=False), identity):
                raise Refused("file changed")
            path.unlink()
        except FileNotFoundError:
            pass
        except (OSError, Refused):
            left.append(path)
    if folder is not None:
        path, identity = folder
        try:
            if not os.path.samestat(plain(path, directory=True), identity):
                raise Refused("directory changed")
            path.rmdir()
        except FileNotFoundError:
            pass
        except (OSError, Refused):
            left.append(path)
    if left:
        print("Left in place:", *left, sep="\n")
    return bool(left)


def _system(name, *args, capture=False, changes=None):
    exe = _system_directory() / (name + ".exe")
    plain(exe, directory=False, single_link=False)
    if changes is not None:
        changes.began = True
    result = subprocess.run([str(exe), *args], capture_output=capture, timeout=30)
    if capture:
        result.stdout, result.stderr = _decode(result.stdout), _decode(result.stderr)
    return result


def _decode(data):
    # Windows 11 tools may write UTF-8 even when the console code page is 932 (seen 2026-11-26),
    # so try UTF-8 first and fall back to the Windows code page.
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        import locale
        return data.decode(locale.getencoding(), errors="replace")


def _system_directory():
    buffer = ctypes.create_unicode_buffer(260)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise Refused("cannot determine the Windows system directory")
    return Path(buffer.value)


def _user():
    buffer = ctypes.create_unicode_buffer(257)
    size = ctypes.c_ulong(len(buffer))
    if not ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
        raise Refused("cannot determine the signed-in user")
    return buffer.value


def _admin_group():
    groups = _system("whoami", "/groups", "/fo", "csv", "/nh", capture=True)
    sids = set(re.findall(r'"(S-1-[0-9-]+)"', groups.stdout))
    if groups.returncode or "S-1-1-0" not in sids:
        raise Refused("cannot determine Administrators membership")
    return "S-1-5-32-544" in sids


def _elevated():
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _check(config, admin, agent, win):
    print("[1] VM name in the registry. The agent needs the first one.")
    for registry in (agent.GUEST, agent.GUEST.removesuffix("s")):
        try:
            name = win.read_registry(registry, "VirtualMachineName")
        except (OSError, win.WindowsError):
            name = "(unavailable)"
        print(registry, name)
    print("[2] Administrators group. Want: OK")
    print("NG: administrator" if admin else "OK: standard user")
    print("[3] Python and TLS-PSK. Want: 3.13 and True")
    print(sys.version.split()[0], ssl.OPENSSL_VERSION, ssl.HAS_PSK)
    print(f"[4] Address. Want: {config['address']} and no default gateway", flush=True)
    addresses = _system("ipconfig", capture=True)
    print("\n".join(line for line in addresses.stdout.splitlines() if "IPv4" in line), flush=True)
    route = _system("route", "print", "-4", "0.0.0.0")
    return int(bool(admin or addresses.returncode or route.returncode))


def _firewall_rows(result):
    error = "the inbound firewall rules for python.exe could not be read"
    # Real netsh output (checked on Windows 11, 2026-11-26) has rules without a program, descriptions
    # that run over several lines, and names the console code page cannot show. Only the program and
    # action of each rule matter here, so other lines are skipped; a program that cannot be read is refused.
    if result.returncode or result.stderr.strip():
        raise Refused(f"{error} (netsh exit code {result.returncode})")
    lines = result.stdout.strip().splitlines()
    if not lines or lines[-1].strip().casefold() not in ("ok.", "ok"):
        raise Refused(f"{error} (no final OK line)")
    body = "\n".join(lines[:-1]).strip()
    rows_out = []
    if not body:
        return rows_out
    recognized = 0
    # A rule starts with a name line and a line of dashes; field names may be English or Japanese.
    for rule in re.split(r"\n[ \t]*\n", body):
        rows = rule.splitlines()
        if len(rows) < 2:
            continue
        heading, separator, *fields = rows
        _, colon, name = heading.partition(":")
        if not colon or not re.fullmatch(r"-+", separator.strip()):
            continue  # Part of a long description, not the start of a rule.
        recognized += 1
        values = {}
        for field in fields:
            key, colon, value = field.partition(":")
            key = key.strip().casefold()
            if colon and key and key not in values:
                values[key] = value.strip()
        program = values.get("program") or values.get("プログラム") or ""
        if not program:
            continue  # Service and app rules name no program, so they cannot be python.exe.
        if "\ufffd" in program:
            raise Refused(f"{error} (a program path has characters that cannot be shown)")
        action = (values.get("action") or values.get("操作") or "").casefold()
        if action not in ("allow", "block", "許可", "ブロック"):
            raise Refused(f"{error} (unknown action {action[:20]!r})")
        if program.startswith('"') != program.endswith('"'):
            raise Refused(f"{error} (unbalanced quotes in a program path)")
        program = program.removeprefix('"').removesuffix('"')
        if not program or '"' in program:
            raise Refused(error)
        rows_out.append((name.strip(), program, action in ("block", "ブロック"), values, rule))
    if not recognized:
        raise Refused(f"{error} (no rule found)")
    return rows_out


def _firewall_list(*filters):
    try:
        result = _system("netsh", "advfirewall", "firewall", "show", "rule", *filters, "verbose", capture=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise Refused("the inbound firewall rules for python.exe could not be read") from error
    if result.returncode == 1 and not result.stderr.strip() and result.stdout.strip() in (
            "No rules match the specified criteria.", "指定された条件に一致する規則はありません。"):
        return []
    return _firewall_rows(result)


def _rule_matches(row, config):
    _, program, blocked, values, _ = row

    def fields(*names):
        return next((values[name].casefold() for name in names if name in values), "")

    return (
        not blocked and os.path.normcase(program) == os.path.normcase(sys.executable)
        and fields("direction", "方向") in ("in", "受信", "入力")  # Windows 11 Japanese shows 入力 (2026-11-26)
        and fields("enabled", "有効") in ("yes", "はい")
        and fields("protocol", "プロトコル") == "tcp"
        and fields("localip", "local ip", "ローカル ip", "ローカルip").removesuffix("/32") == config["address"]
        and fields("remoteip", "remote ip", "リモート ip", "リモートip").removesuffix("/32") == config["host_address"]
        and fields("localport", "local port", "ローカル ポート", "ローカルポート") == str(config["port"])
        and fields("remoteport", "remote port", "リモート ポート", "リモートポート") in ("any", "任意", "すべて")
        and fields("profiles", "プロファイル").replace(" ", "") in (
            "domain,private,public", "any", "すべて", "ドメイン,プライベート,パブリック")
        and fields("edgetraversal", "edge traversal", "エッジ トラバーサル", "エッジトラバーサル") in ("no", "いいえ")
    )


def _firewall(config, remove, changes):
    from secrets import token_hex

    prefix = ("advfirewall", "firewall")
    managed_name = re.compile(RULE + r"(?:-[a-f0-9]{32})?", re.I | re.ASCII)
    rows = _firewall_list("name=all", "dir=in")
    managed = [row for row in rows if managed_name.fullmatch(row[0])]
    if bool(managed) != remove:
        raise Refused("the firewall rule already exists" if managed else "the firewall rule does not exist")
    rule_name = managed[0][0] if remove else RULE + "-" + token_hex(16)
    name = "name=" + rule_name
    scope = ("dir=in", "protocol=TCP", f"localip={config['address']}",
             f"localport={config['port']}", f"remoteip={config['host_address']}")

    def checked_rule():
        rules = _firewall_list(name)
        if len(rules) != 1 or rules[0][0] != rule_name or not _rule_matches(rules[0], config):
            raise Refused(f"cannot verify the unique firewall rule {rule_name} with the exact configured scope")
        return rules[0]

    def delete_rule():
        if _system("netsh", *prefix, "delete", "rule", name, *scope, changes=changes).returncode:
            raise Refused("firewall delete failed")
        if _firewall_list(name):
            raise Refused(f"firewall rule {rule_name} still exists after deletion")

    def check_blocks(listing):
        for _, program, block, _, rule in listing:
            if re.search(r'(?:^|[\\/])python\.exe$', program, re.I):
                print(rule, end="\n\n")
                if block:
                    raise Refused("an inbound Block rule for python.exe was found")

    if remove:
        if len(managed) != 1 or not _rule_matches(managed[0], config):
            raise Refused("the existing firewall rule does not uniquely match the old configuration")
        checked_rule()
        delete_rule()
        print(f"OK: removed firewall rule {rule_name}")
        return 0
    if any(block and re.search(r'(?:^|[\\/])python\.exe$', program, re.I)
           for _, program, block, _, _ in rows):
        check_blocks(rows)
    action = ("add", "rule", name, *scope, "action=allow", f"program={sys.executable}", "profile=any", "edge=no")
    try:
        added = _system("netsh", *prefix, *action, changes=changes)
    except (OSError, subprocess.SubprocessError):
        print(f"Check whether firewall rule {rule_name} was added before retrying.")
        raise
    if added.returncode:
        raise Refused(f"firewall add failed. Check rule {rule_name} before retrying")
    try:
        verified = checked_rule()
        all_rules = _firewall_list("name=all", "dir=in")
        if [row for row in all_rules if managed_name.fullmatch(row[0])] != [verified]:
            raise Refused("the inbound listing does not confirm one managed firewall rule")
        check_blocks(all_rules)
    except (OSError, Refused, subprocess.SubprocessError):
        try:
            checked_rule()
            delete_rule()
        except (OSError, Refused, subprocess.SubprocessError):
            print(f"Could not safely confirm removal of {rule_name}. Inspect that exact rule before retrying.")
        else:
            changes.began = False
            print(f"Rolled back firewall rule {rule_name}; verified absent.")
        raise
    print(f"OK: verified firewall rule {rule_name} and no inbound Block rule for python.exe")
    return 0


def _probe(address, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.settimeout(3)
        try:
            stream.connect((address, port))
        except OSError as error:
            code = getattr(error, "winerror", None) or error.errno
            if isinstance(error, TimeoutError) or code in (errno.ETIMEDOUT, 10060):
                return "timed out"
            if code in (errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065):
                return "unreachable"
            if code in (errno.ECONNREFUSED, 10061):
                return "connection refused"
            return f"unknown socket error ({code})"
        return "connected"


def _dns_resolves(name, seconds=5):
    import threading

    answers = []

    def look():
        try:
            answers.append(bool(socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)))
        except OSError:
            answers.append(False)

    thread = threading.Thread(target=look, daemon=True)
    thread.start()
    thread.join(seconds)
    return bool(answers and answers[0])  # No answer in time counts as a failed lookup.


def _netcheck(config):
    print("Want: timed out or unreachable. Connected or connection refused means a packet got through.", flush=True)
    results = []
    for address, port in (*NETCHECK_OUTSIDE, *((config["host_address"], port) for port in HOST_PORTS)):
        status = _probe(address, port)
        results.append(status)
        print(f"{address}:{port} {status}", flush=True)
    print("--- DNS. Verify a lookup failure or timeout before proceeding ---", flush=True)
    _system("nslookup", "-timeout=2", "example.com")
    # nslookup on Windows 11 can exit 0 even when the lookup fails (seen 2026-11-26), so judge DNS by
    # trying the lookup here; it must fail or time out.
    if _dns_resolves("example.com") or any(status not in ("timed out", "unreachable") for status in results):
        raise Refused("the isolation acceptance check did not establish blocked connections and failed DNS")
    print("TCP probes timed out or were unreachable; confirm the DNS output before acceptance.")
    return 0


def _bundle(folder, *, update=False, installed=False):
    plain(folder, directory=True)
    expected = set(BUNDLE_FILES)
    actual = expected | {KEY} if installed else {path.name for path in folder.iterdir()}
    if actual != expected | {KEY} and not (update and actual == expected):
        raise Refused("unexpected bundle file set")
    if KEY in actual:
        plain(folder / KEY, directory=False)
    manifest, _ = snapshot(folder / MANIFEST, limit=8192)
    try:
        raw = json.loads(manifest.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
        if (type(raw) is not dict or raw.keys() != {"format", "vm_name", "sha256"}
                or type(raw["format"]) is not int or raw["format"] != 1
                or type(raw["vm_name"]) is not str or type(raw["sha256"]) is not dict
                or raw["sha256"].keys() != set(HASHED_FILES)):
            raise ValueError
    except (ValueError, RecursionError):
        raise Refused("invalid bundle manifest") from None
    files = {MANIFEST: manifest}
    for name in HASHED_FILES:
        data, _ = snapshot(folder / name, limit=CONFIG_LIMIT if name == CONFIG else 4 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != raw["sha256"][name]:
            raise Refused(f"bundle hash mismatch: {name}")
        files[name] = data
    try:
        config = json.loads(files[CONFIG].decode("utf-8-sig"), object_pairs_hook=_unique,
                            parse_constant=_constant)
        if type(config) is not dict or config.get("vm_name") != raw["vm_name"]:
            raise ValueError
    except (ValueError, RecursionError):
        raise Refused("bundle manifest VM name differs from the guest configuration") from None
    return files


def _install(source, target, protocol, win, changes, files):
    key = win.read_key_bytes(source / KEY, protocol.KEY_FILE_MAX)
    protocol.decode_key(key)
    plain(target.parent, directory=True)
    made = {}
    target.mkdir()
    changes.began = True
    folder = (target, target.lstat())
    try:
        for name in (*BUNDLE_FILES, KEY):
            data = key if name == KEY else files[name]
            output = target / name
            write_new(output, data, made)
            copied = (win.read_key_bytes(output, protocol.KEY_FILE_MAX) if name == KEY else output.read_bytes())
            if copied != data:
                raise Refused(f"copy differs: {name}")
        print("--- Who can read the key. Want: the guest user, SYSTEM, Administrators only ---", flush=True)
        if _system("icacls", str(target / KEY)).returncode:
            raise Refused("cannot show key ACL")
    except BaseException:
        changes.began = rollback(made, folder)
        raise
    print(f"OK: copied to {target}")
    return 0


def _installed(target, protocol):
    config = load_config(target, protocol)
    for name in (*HASHED_FILES, KEY):
        plain(target / name, directory=False)
    try:
        (target / MANIFEST).lstat()
    except FileNotFoundError:
        pass
    else:
        _bundle(target, installed=True)
    return config


@contextmanager
def _stopped(config):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        try:
            stream.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            stream.bind((config["address"], config["port"]))
        except OSError:
            raise Refused("stop the agent and ensure its configured port is available") from None
        yield


@contextmanager
def _operation(target):
    import msvcrt

    plain(target.parent, directory=True)
    lock = target.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        expected = plain(lock, directory=False)
        fd = os.open(lock, os.O_RDWR)
    else:
        expected = os.fstat(fd)
    with os.fdopen(fd, "rb+") as stream:
        recheck(lock, expected)
        if not os.path.samestat(os.fstat(stream.fileno()), expected):
            raise Refused("the agent operation lock changed")
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise Refused("another agent operation is in progress") from error
        yield


def _update(source, target, changes, files):
    if source.resolve() == target.resolve():
        raise Refused("run update from the incoming bundle")
    if snapshot(target / CONFIG, limit=CONFIG_LIMIT)[0] != files[CONFIG]:
        raise Refused("the configuration bytes differ. Recreate the bundle using the old settings")
    incoming = {name: files[name] for name in (*CODE_FILES, MANIFEST)}
    previous = {}
    for name in incoming:
        try:
            previous[name] = snapshot(target / name)
        except FileNotFoundError:
            if name != MANIFEST:
                raise
    from secrets import token_hex

    # Prepare and compare every replacement before changing the installed code.
    made = {}
    replaced = []
    staged = {}
    try:
        for name, data in incoming.items():
            new = target / (".update-" + token_hex(16))
            write_new(new, data, made)
            checked, new_identity = snapshot(new)
            if checked != data:
                raise Refused(f"copy differs: {name}")
            backup = None
            if name in previous:
                old = target / (".update-" + token_hex(16))
                write_new(old, previous[name][0], made)
                checked, old_identity = snapshot(old)
                if checked != previous[name][0]:
                    raise Refused(f"backup differs: {name}")
                backup = old, old_identity
            staged[name] = new, new_identity, backup
        for name, (new, new_identity, _) in staged.items():
            destination = target / name
            recheck(new, new_identity)
            if name in previous:
                recheck(destination, previous[name][1])
                os.replace(new, destination)
            else:
                os.rename(new, destination)
            replaced.append(name)
    except BaseException:
        changes.began = bool(made)
        for name in reversed(replaced):
            destination = target / name
            _, new_identity, backup = staged[name]
            try:
                recheck(destination, new_identity)
                if backup is None:
                    destination.unlink()
                else:
                    recheck(*backup)
                    os.replace(backup[0], destination)
            except (OSError, Refused):
                if backup is not None:
                    del made[backup[0]]
                    print(f"Original {name} retained at {backup[0]}")
                print(f"Check {destination} with the original bundle before starting the agent.")
        raise
    finally:
        rollback(made)
    print(f"OK: updated {target}")
    for name, data in incoming.items():
        print(hashlib.sha256(data).hexdigest(), name)
    return 0


def _remove(target, changes):
    folder = plain(target, directory=True)
    existing = {}
    for name in (*CODE_FILES, MANIFEST, KEY, CONFIG):
        try:
            existing[target / name] = plain(target / name, directory=False)
        except FileNotFoundError:
            pass
    try:
        for path, info in existing.items():
            recheck(target, folder, directory=True)
            recheck(path, info)
            path.unlink()
            changes.began = True
    except (OSError, Refused):
        print("Left in place:", *(path for path in existing if path.exists()), sep="\n")
        print("Keep the old configuration. Use remove.cmd from the matching incoming bundle to continue.")
        raise
    remaining = sorted(path.name for path in target.iterdir())
    if remaining:
        print("Left in place:", *remaining, sep="\n")
    else:
        os.chdir(target.parent)
        try:
            recheck(target, folder, directory=True)
            target.rmdir()
        except FileNotFoundError:
            pass
        except OSError as error:
            print(f"The installed files were removed. The empty folder stays: {target}. {error}")
            print("Check that the folder is still empty, then remove it before reinstalling.")
    print("SoraVmFiles, the firewall rule, and the host key stay in place.")
    print(f"Operation lock file retained: {target.with_suffix('.lock')}")
    return 0


def _incoming_root():
    return Path(_system_directory().anchor) / "SoraFromHost"


def _admin_bundle(source):
    root = _incoming_root()
    if (source.name != "vm-guest" or source.parent.parent != root
            or not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{32}", source.parent.name, re.ASCII)):
        raise Refused("run the administrator verb from an incoming SoraFromHost bundle")
    return _bundle(source, update=True)


class _BundleImports:
    def __init__(self, files, source):
        self.files = files
        self.source = source

    def find_spec(self, fullname, path=None, target=None):
        name = fullname + ".py"
        if name in AGENT_FILES:
            return ModuleSpec(fullname, self, origin=str(self.source / name))

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = module.__spec__.origin
        module.__file__ = path
        exec(compile(self.files[Path(path).name], path, "exec"), module.__dict__)


def _start(verb, target, config, agent, changes):
    tools = ("screenshot", "wait") if verb == "start-view" else config["tools"]
    args = ["--vm-name", config["vm_name"], "--bind", config["address"],
            "--allow-peer", config["host_address"], "--port", str(config["port"]),
            "--key-file", str(target / KEY), "--max-minutes", str(config["max_minutes"]),
            "--max-input-ops", str(config["max_inputs"]), "--tools", ",".join(tools)]
    if verb == "start-full":
        print("This start lets the host click, type, read control names, and send or fetch files.")
        if {"file_put", "file_get"}.intersection(tools):
            files = Path(os.environ["USERPROFILE"]) / "SoraVmFiles"
            print(f"Files: only {files / 'inbox'} (from the host) and outbox (to the host).")
            for folder in (files, files / "inbox", files / "outbox"):
                plain(folder.parent, directory=True)
                try:
                    folder.mkdir()
                except FileExistsError:
                    pass
                else:
                    changes.began = True
                plain(folder, directory=True)
            args.extend(("--files-dir", str(files)))
        print(f"{config['max_inputs']} inputs, {config['max_minutes']} minutes.", flush=True)
    return agent.main(args)


def main(argv=None, *, source=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verb", choices=VERBS)
    args = parser.parse_args(argv)
    verb = args.verb
    source = source if source is not None else Path(os.path.abspath(__file__)).parent
    administrative = verb in ("firewall-admin", "firewall-remove-admin")
    files = None
    try:
        if administrative:
            if not _elevated():
                raise Refused('use "Run as administrator"')
            files = _admin_bundle(source)
    except (OSError, Refused) as error:
        print(f"NG: {error}. Nothing was done.")
        return 1

    loader = _BundleImports(files, source) if administrative else None
    if loader is not None:
        sys.meta_path.insert(0, loader)
    try:
        import vm_agent as agent
        import vm_protocol as protocol
        import vm_win as win
    finally:
        if loader is not None:
            sys.meta_path.remove(loader)

    changes = Changes()
    try:
        config = load_config(source, protocol, data=files[CONFIG] if files else None)
        if verb in ("install", "update"):
            files = _bundle(source, update=verb == "update")
            config = load_config(source, protocol, data=files[CONFIG])
        if not agent._guest(win.read_registry, config["vm_name"]):
            raise Refused("this is not the configured Hyper-V guest")
        if administrative:
            return _firewall(config, verb == "firewall-remove-admin", changes)
        if _user().casefold() != config["guest_user"].casefold():
            raise Refused("sign in as the configured guest user")
        admin = _admin_group()
        if verb == "check":
            return _check(config, admin, agent, win)
        if admin:
            raise Refused("a standard user is required")
        if verb == "netcheck":
            return _netcheck(config)
        target = Path(os.environ["LOCALAPPDATA"]) / RULE
        with _operation(target):
            if verb == "install":
                return _install(source, target, protocol, win, changes, files)
            if verb == "remove":
                if source != target:
                    _bundle(source, update=True)
                installed = load_config(target, protocol)
            else:
                installed = _installed(target, protocol)
            if installed != config:
                raise Refused("the bundle configuration differs from the installed configuration")
            if verb in ("update", "remove"):
                with _stopped(installed):
                    return _update(source, target, changes, files) if verb == "update" else _remove(target, changes)
            if source.resolve() != target.resolve():
                raise Refused("run the start command from the installed folder")
            return _start(verb, target, installed, agent, changes)
    except (OSError, KeyError, Refused, win.WindowsError, protocol.ProtocolError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, (Refused, OSError)) else "guest operation could not be verified"
        suffix = ("Some changes may have been made. Run check.cmd and look at the folder before trying again."
                  if changes.began else "Nothing was done.")
        print(f"NG: {reason}. {suffix}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
