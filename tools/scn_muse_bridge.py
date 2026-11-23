"""Muse bridge for Sora Commander (DEV, Owner-run).

Serves the Owner's Sora Commander Host over Streamable HTTP, bound ONLY to
this PC's Tailscale address. Readonly defaults to Tailscale identity.
Exec requires node identity plus its dedicated token or one-time pairing.

Safety shape:
- In readonly, roots come from the Owner's own allowed-roots.txt; every root is
  demoted to read-only for this bridge (Policy roots=()): file tools can
  read. The optional identity-gated inbox is a separate write-only tool.
- In readonly no Owner review dispatcher is attached, so commands cannot be
  approved here; they return DENY. No approval window opens from this
  process.
- Modes requiring tokens refuse to start without one. The bridge refuses to bind to
  anything that is not a Tailscale IPv4 address (100.64.0.0/10).
- Readonly tokens come from SORA_BRIDGE_TOKEN or the token file. Exec uses
  its dedicated file and the Owner pilot dispatcher. Tokens are never logged.

Usage:
    python tools/scn_muse_bridge.py --checkout C:/SoraCommander --bind 100.x.y.z [--port 8765]
"""

import argparse
import hmac
import ipaddress
import json
import os
from pathlib import Path
import sys
import stat
import shutil
import subprocess
import threading
import time

TOKEN_HEADER = b"x-sora-bridge-token"
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
EXEC_TOKEN_NAME = "muse-bridge-exec-token.txt"
PEERS_NAME = "muse-bridge-peers.json"


def _bounded_file(path, *, optional=False):
    """Bounded ordinary-file read; refuse linked files and ancestors."""
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if optional:
                return None
            raise ValueError("exec token file missing") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked token path refused")
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("invalid token directory")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("ordinary token file required")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("token file changed")
        data = stream.read(4097)
    if len(data) > 4096:
        raise ValueError("token file too large")
    return data


def _file_token(path, *, optional=False):
    data = _bounded_file(path, optional=optional)
    if data is None:
        return None
    lines = [line.strip() for line in data.decode("utf-8-sig").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    if not lines:
        raise ValueError("token file empty")
    return lines


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def example_peer_ids(checkout):
    """StableIDs in the shipped example file; a copied placeholder must be replaced."""
    try:
        sample = json.loads(_bounded_file(Path(checkout) / "docs" / "muse-bridge-peers.example.json")
                            .decode("utf-8-sig"))
        return frozenset(value for values in sample.values() for value in values
                         if _stable_id(value))
    except (OSError, ValueError, AttributeError, TypeError):
        return frozenset()


def _stable_id(value):
    return isinstance(value, str) and bool(value.strip()) and value.isprintable()


def load_peers(path):
    peers = json.loads(_bounded_file(path).decode("utf-8-sig"), object_pairs_hook=_unique_object)
    if not isinstance(peers, dict) or set(peers) != {"readonly", "exec"}:
        raise ValueError("readonly and exec lists required; unknown keys refused")
    for values in peers.values():
        if not isinstance(values, list) or any(not _stable_id(value) for value in values):
            raise ValueError("StableID strings required")
        if len(set(values)) != len(values) or any("*" in value for value in values):
            raise ValueError("duplicate or wildcard StableID refused")
    return peers


def find_tailscale():
    cli = shutil.which("tailscale")
    if cli:
        return cli
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidate = Path(program_files) / "Tailscale" / "tailscale.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _cli_json(arguments, *, runner, finder):
    cli = finder()
    if not cli:
        raise ValueError("CLI unavailable")
    result = runner([cli, *arguments], shell=False, timeout=3, capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise ValueError("CLI failed")
    if len(result.stdout) > 64 * 1024:
        raise ValueError("CLI response too large")
    payload = json.loads(result.stdout, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise ValueError("invalid CLI response")
    return payload


class PeerIdentity:
    """Process-local cache with one lookup at a time per IP."""

    def __init__(self, allowed, *, runner=subprocess.run, finder=find_tailscale,
                 clock=time.monotonic):
        self.allowed = frozenset(allowed)
        self.runner, self.finder, self.clock = runner, finder, clock
        self._lock = threading.Lock()
        self._ip_locks = {}
        self._cache = {}
        self._announced = set()

    def accepts(self, ip):
        return self.lookup(ip) is not None

    def lookup(self, ip) -> str | None:
        with self._lock:
            ip_lock = self._ip_locks.get(ip)
            if ip_lock is None:
                ip_lock = self._ip_locks[ip] = threading.Lock()
        with ip_lock:
            cached = self._cache.get(ip)
            if cached is not None and self.clock() < cached[0]:
                return cached[1]
            try:
                payload = _cli_json(["whois", "--json", ip], runner=self.runner, finder=self.finder)
                node = payload.get("Node")
                if not isinstance(node, dict) or not _stable_id(node.get("StableID")):
                    raise ValueError("invalid identity")
                addresses = node.get("Addresses")
                if not isinstance(addresses, list) or f"{ip}/32" not in addresses:
                    raise ValueError("address mismatch")
                stable_id = node["StableID"]
            except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
                accepted, reason = False, "lookup failed"
            else:
                accepted = stable_id in self.allowed
                reason = "not in allow-list"
                if accepted:
                    with self._lock:
                        first_accept = stable_id not in self._announced
                        self._announced.add(stable_id)
                    if first_accept:
                        print(f"[bridge] peer allowed: {stable_id}")
            if not accepted:
                print(f"[bridge] peer refused: {reason}")
            allowed_id = stable_id if accepted else None
            self._cache[ip] = (self.clock() + (60 if accepted else 5), allowed_id)
            return allowed_id


def transport_lists(bind, port, *, allow_magicdns=False, runner=subprocess.run,
                    finder=find_tailscale):
    hosts, origins = [f"{bind}:{port}", bind], [f"http://{bind}:{port}"]
    if allow_magicdns:
        try:
            payload = _cli_json(["status", "--json"], runner=runner, finder=finder)
            own = payload.get("Self")
            name = own.get("DNSName") if isinstance(own, dict) else None
            if isinstance(name, str) and name.endswith(".ts.net."):
                name = name[:-1]
                # Only a DNS name can be added, never a wildcard, URL or port.
                if len(name) <= 253 and all(
                    0 < len(label) <= 63 and label[0].isalnum() and label[-1].isalnum()
                    and all(c.isascii() and (c.isalnum() or c == "-") for c in label)
                    for label in name.split(".")
                ):
                    hosts.extend([name, f"{name}:{port}"])
                    origins.append(f"http://{name}:{port}")
        except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
            pass
    return hosts, origins


def load_exec_token(path, environ):
    """Never use SORA_BRIDGE_TOKEN as an exec credential or reuse readonly's token."""
    home = Path(environ.get("USERPROFILE", ""))
    expected = home / ".config" / "sora-commander" / EXEC_TOKEN_NAME
    if not home.is_absolute() or path != expected:
        raise ValueError("exec mode requires its dedicated Owner token file")
    lines = _file_token(path)
    if len(lines) != 1 or len(lines[0]) < 32 or any(not 33 <= ord(c) <= 126 for c in lines[0]):
        raise ValueError("exec token must be one ASCII value of at least 32 characters")
    token = lines[0]
    readonly = _file_token(expected.with_name("muse-bridge-token.txt"), optional=True)
    candidates = [environ.get("SORA_BRIDGE_TOKEN", "").strip()]
    if readonly:
        candidates.append(readonly[0])
    if any(hmac.compare_digest(token.encode(), value.encode()) for value in candidates if value):
        raise ValueError("exec and readonly tokens must differ")
    return token


def build_exec_host(checkout, *, enable_edit=False, enable_create=False):
    if os.environ.get("SORA_NEXT_PRODUCT_BROKER") == "1":
        raise ValueError("product Broker and exec pilot are mutually exclusive")
    if enable_create and not enable_edit:
        raise ValueError("create pilot requires edit pilot")
    from sora_commander_next.stdio_main import build_owner_pilot_host
    host = build_owner_pilot_host(checkout, environ=os.environ, profile="development",
                                 journal_enabled=True, result_enabled=True,
                                 edit_enabled=enable_edit, create_enabled=enable_create)
    if host._owner_review_dispatcher is None:
        raise RuntimeError("Owner dispatcher required")
    return host


def load_token(token_file: Path) -> str:
    token = os.environ.get("SORA_BRIDGE_TOKEN", "").strip()
    if not token:
        # Same bounded, no-link, ordinary-file read as the exec token file.
        try:
            lines = _file_token(Path(token_file).absolute(), optional=True)
        except (OSError, ValueError, UnicodeDecodeError):
            raise SystemExit("Bridge token file refused: linked, not an ordinary file, "
                             "too large or unreadable.") from None
        token = lines[0] if lines else ""
    if len(token) < 32:
        raise SystemExit(
            "Bridge token missing or too short. Set SORA_BRIDGE_TOKEN or "
            f"put a token of 32+ characters in {token_file}"
        )
    return token


class TokenGate:
    """ASGI middleware enforcing the selected token and/or node identity gate."""

    def __init__(self, app, token: str, *, auth="token", identity=None):
        self.app = app
        self.token = token.encode()
        if auth not in {"token", "tailscale", "both", "pairing"} or (auth != "token" and identity is None):
            raise ValueError("invalid authentication configuration")
        self.auth, self.identity = auth, identity

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope_type not in ("http", "lifespan"):
            return
        if scope_type == "http":
            headers = dict(scope.get("headers") or [])
            presented = headers.get(TOKEN_HEADER, b"")
            accepted = self.auth in {"tailscale", "pairing"} or hmac.compare_digest(presented, self.token)
            if not accepted:
                print("[bridge] peer refused: token required")
            if accepted and self.auth != "token":
                client = scope.get("client")
                try:
                    ip = ipaddress.IPv4Address(client[0]) if client else None
                    valid_ip = ip is not None and ip in TAILNET_V4
                except (ValueError, TypeError, IndexError):
                    valid_ip = False
                if valid_ip:
                    import anyio
                    stable_id = await anyio.to_thread.run_sync(self.identity.lookup, str(ip))
                    accepted = stable_id is not None
                    if accepted:
                        scope.setdefault("state", {})["muse_stable_id"] = stable_id
                else:
                    accepted = False
                    print("[bridge] peer refused: invalid source")
            if not accepted:
                from starlette.responses import JSONResponse

                await JSONResponse({"error": "unauthorized"}, status_code=401)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", default=r"C:\SoraCommander")
    parser.add_argument("--bind", required=True,
                        help="This PC's Tailscale IPv4 address (100.64.0.0/10 only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", choices=("readonly", "exec"), default="readonly")
    parser.add_argument("--auth", choices=("token", "tailscale", "both", "pairing"), default=None)
    parser.add_argument("--inbox", action="store_true",
                        help="Enable the write-only inbox in identity-authenticated readonly mode")
    parser.add_argument("--allow-magicdns", action="store_true")
    parser.add_argument("--enable-dev-writes", action="store_true")
    parser.add_argument("--enable-edit-pilot", action="store_true")
    parser.add_argument("--enable-create-pilot", action="store_true")
    parser.add_argument(
        "--token-file",
        default=None,
    )
    args = parser.parse_args(argv)
    if args.mode == "exec" and args.auth not in (None, "both", "pairing"):
        parser.error("exec mode requires --auth both or pairing")
    if args.mode == "readonly" and args.auth == "pairing":
        parser.error("--auth pairing requires --mode exec")
    auth = args.auth or ("both" if args.mode == "exec" else "tailscale")
    if args.inbox and (args.mode != "readonly" or auth == "token"):
        parser.error("--inbox requires --mode readonly and --auth tailscale or both")
    if args.mode == "readonly" and any((args.enable_dev_writes, args.enable_edit_pilot, args.enable_create_pilot)):
        parser.error("write opt-ins require --mode exec")
    if args.mode == "exec" and args.port != 8765:
        parser.error("exec mode uses port 8765 only")

    try:
        bind_ip = ipaddress.IPv4Address(args.bind)
    except ValueError:
        raise SystemExit(f"--bind is not an IPv4 address: {args.bind}")
    if bind_ip not in TAILNET_V4:
        raise SystemExit(
            f"Refusing to bind {args.bind}: only Tailscale addresses "
            "(100.64.0.0/10) are allowed for this bridge."
        )

    try:
        checkout = Path(args.checkout).resolve(strict=True)
    except (OSError, RuntimeError):
        print("[HOLD] The --checkout folder was not found. Bridge not started.", file=sys.stderr)
        return 2
    sys.path.insert(0, str(checkout / "src"))

    identity = None
    if auth != "token":
        try:
            home = Path(os.environ.get("USERPROFILE", ""))
            if not home.is_absolute():
                raise ValueError("Owner profile unavailable")
            peers = load_peers(home / ".config" / "sora-commander" / PEERS_NAME)
            if example_peer_ids(checkout).intersection(
                    value for values in peers.values() for value in values):
                print(f"[HOLD] {PEERS_NAME} still has the example ID. "
                      "Replace it with your agent's StableID.", file=sys.stderr)
                return 2
            if not peers[args.mode]:
                raise ValueError("empty mode allow-list")
            identity = PeerIdentity(peers[args.mode])
        except (OSError, ValueError):
            print("[HOLD] Peer allow-list missing, invalid or empty for this mode.", file=sys.stderr)
            if args.mode == "readonly" and args.auth is None:
                print("[HOLD] v0.3.0 defaults to Tailscale identity. To keep using a token, start with --auth token.",
                      file=sys.stderr)
            return 2

    token = ""
    if args.mode == "readonly":
        if auth != "tailscale":
            if args.token_file is None:
                home = Path(os.environ.get("USERPROFILE", ""))
                if not home.is_absolute():
                    print("[HOLD] Owner profile unavailable; bridge not started.", file=sys.stderr)
                    return 2
                readonly_file = home / ".config" / "sora-commander" / "muse-bridge-token.txt"
            else:
                readonly_file = Path(args.token_file)
            token = load_token(readonly_file)
    elif auth == "both":
        token_file = (Path(os.environ.get("USERPROFILE", "")) / ".config" / "sora-commander" / EXEC_TOKEN_NAME
                      if args.token_file is None else Path(args.token_file))
        try:
            token = load_exec_token(token_file, os.environ)
        except (OSError, ValueError):
            print("[HOLD] Exec token unavailable, invalid or shared with readonly.", file=sys.stderr)
            return 2

    from sora_commander_next.owner_journal_lease import (
        OwnerJournalLeaseError, owner_journal_lease)
    try:
        with owner_journal_lease(args.mode == "exec"):
            return _serve_bridge(args, checkout, token, auth, identity)
    except OwnerJournalLeaseError as error:
        print(f"[HOLD] {error}; bridge not started.", file=sys.stderr)
        return 2


def _serve_bridge(args, checkout, token, auth, identity):
    from sora_commander_next.allowed_roots import (
        AllowedRootsError, _locations, startup_allowed_roots)
    from sora_commander_next.host import Host
    from sora_commander_next.mcp_adapter import build_server
    from sora_commander_next.policy import Policy

    host = None
    pairing = None
    inbox_enabled = getattr(args, "inbox", False)
    try:
        if args.mode == "readonly":
            try:
                _, config, _, _ = _locations(os.environ)
                config.lstat()
            except FileNotFoundError:
                print("[HOLD] allowed-roots.txt is missing; the read-only bridge serves only folders you list. Bridge not started.",
                      file=sys.stderr)
                return 2
            except Exception:
                print("[HOLD] allowed-roots.txt could not be checked. Bridge not started.", file=sys.stderr)
                return 2
            try:
                roots = startup_allowed_roots(str(checkout), "development", os.environ)
            except AllowedRootsError as error:
                # Its fixed message holds a line number and a reason, never a path.
                print(f"[HOLD] allowed-roots.txt: {error}. Bridge not started.", file=sys.stderr)
                return 2
            read_all = tuple(roots.read)
            if not read_all:
                raise SystemExit("allowed-roots.txt configured no folders; nothing to serve.")
            # Every allowed root is demoted to read-only here: Policy roots stay empty.
            host = Host(policy=Policy(roots=(), read_only_roots=read_all))
            server = build_server(host, dev_writes_enabled=False, dev_tools_enabled=False,
                                  command_tools_enabled=False)
            if __package__:
                from .scn_muse_digest import register_digest
            else:
                from scn_muse_digest import register_digest
            register_digest(server, host, checkout=checkout, auth=auth, inbox=inbox_enabled)
            if inbox_enabled:
                if __package__:
                    from .scn_muse_inbox import InboxUnavailable, register_inbox
                else:
                    from scn_muse_inbox import InboxUnavailable, register_inbox
                try:
                    register_inbox(server, host._activity_store, os.environ.get("USERPROFILE", ""))
                except (InboxUnavailable, OSError) as error:
                    print(f"[HOLD] Inbox unavailable: {error}", file=sys.stderr)
                    return 2
        else:
            try:
                host = build_exec_host(checkout, enable_edit=args.enable_edit_pilot,
                                       enable_create=args.enable_create_pilot)
                server = build_server(host, dev_writes_enabled=args.enable_dev_writes,
                                      dev_write_tools_enabled=args.enable_dev_writes)
                if auth == "pairing":
                    if __package__:
                        from .scn_muse_pairing import Pairing, register_pairing
                    else:
                        from scn_muse_pairing import Pairing, register_pairing
                    pairing = Pairing()
                    register_pairing(server, pairing)
            except (Exception, SystemExit):
                print("[HOLD] Exec Owner pilot construction failed; bridge not started.", file=sys.stderr)
                return 2

        from mcp.server.transport_security import TransportSecuritySettings

        allowed_hosts, allowed_origins = transport_lists(args.bind, args.port,
                                                        allow_magicdns=args.allow_magicdns)
        server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )

        app = TokenGate(server.streamable_http_app(), token, auth=auth, identity=identity)

        if args.mode == "readonly":
            print(f"[bridge] Sora Commander read-only bridge for Muse")
            if inbox_enabled:
                print("[bridge] read-only, plus one write-only inbox folder.")
        elif pairing is not None:
            print("[bridge] mode: EXEC with one-time pairing (approval window still required for every command)")
            print(f"[bridge] pairing code: {pairing.display()} (valid 10 minutes, one use; never share it except with your agent)")
        else:
            print("[bridge] mode: EXEC (approval window required for every command)")
        print(f"[bridge] listening on http://{args.bind}:{args.port}/mcp (Tailscale only)")
        if args.mode == "readonly":
            print(f"[bridge] read-only folders: {len(read_all)} (from allowed-roots.txt, demoted)")
            print("[bridge] commands are denied in this process; no approval window opens here.")
        else:
            print(f"[bridge] allowed folders: {len(host.policy.roots)} full, {len(host.policy.read_only_roots)} read-only")
            print("[bridge] observations and stop follow native Host behavior; stop has no new approval window.")
            print(f"[bridge] DEV file writes: {args.enable_dev_writes}; edit pilot: {args.enable_edit_pilot}; create pilot: {args.enable_create_pilot}")
            print("[bridge] send_input is not covered by these flags; it stays limited to approved DEV Python sessions with its own approval window.")
        print("[bridge] Ctrl+C to stop.")

        import uvicorn

        try:
            uvicorn.run(app, host=args.bind, port=args.port, log_level="info",
                        access_log=False, proxy_headers=False, ws="none")
        finally:
            if args.mode == "exec":
                print("[bridge] EXEC listener stopped. Reconcile existing jobs; never replay unknown outcomes.")
        return 0
    finally:
        if host is not None:
            host.close()


if __name__ == "__main__":
    raise SystemExit(main())
