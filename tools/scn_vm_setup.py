"""Build a flat bundle for the configured Hyper-V guest."""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys

from sora_commander_next import vm_client, vm_protocol
from sora_commander_next.vm_control import VmConfigurationError, load_vm_control
if __package__:
    from .vm_guest import vm_guest as guest
else:
    from vm_guest import vm_guest as guest


CHECKOUT = Path(__file__).resolve().parents[1]
GUEST_SOURCE = Path(__file__).resolve().with_name("vm_guest")
AGENT_SOURCE = CHECKOUT / "src" / "sora_commander_next"


def _local_drive(root):
    # Removable, fixed and RAM drives are local. Network and unknown drives are refused.
    return ctypes.windll.kernel32.GetDriveTypeW(root) in (2, 3, 6)


def _absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise guest.Refused(f"already exists: {path}")


def _out_folder(path):
    if (not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":"
            or not path.drive[0].isascii() or not path.drive[0].isalpha()
            or not _local_drive(path.anchor) or path.name != "vm-guest"
            or any(part.endswith((" ", ".")) or ":" in part for part in path.parts[1:])):
        raise guest.Refused("use an absolute local drive path ending in vm-guest")
    guest.plain(path.parent, directory=True)
    if path.parent.resolve(strict=True).is_relative_to(CHECKOUT):
        raise guest.Refused("the output must be outside this checkout")
    _absent(path)


def _guest_config(config, user):
    return {"format": 1, "vm_name": config.vm_name, "guest_user": user,
            "address": config.address, "host_address": config.bind_local,
            "port": config.port, "max_minutes": config.max_minutes,
            "max_inputs": config.max_inputs, "tools": list(config.tools)}


def _rotation_paths(key):
    return tuple(key.with_name(key.name + suffix)
                 for suffix in (".pending.key", ".rotation.json", ".previous.key"))


def _rotation_config(config, user, previous, previous_key):
    current = _guest_config(config, user)
    if (previous_key != config.key_file or previous is None or previous.name != guest.CONFIG
            or guest.load_config(previous.parent, vm_protocol) != current):
        raise guest.Refused("key replacement requires the matching old vm-guest.json and --previous-host-key. "
                            "Rotate the key with the old settings first. For a settings change, stop and remove "
                            "the old guest and its firewall rule using the old configuration, then change settings, "
                            "reinstall the guest and restart the host. If the VM name changed, run "
                            "Install-SendToVm.ps1 -VmName <new VM name>")
    return current


def _activate_key(args, config):
    user = guest.guest_user(args.guest_user)
    current = _rotation_config(config, user, args.previous_guest_config, args.previous_host_key)
    key = config.key_file
    pending, receipt, backup = _rotation_paths(key)
    previous, old_identity = guest.snapshot(key, limit=vm_protocol.KEY_FILE_MAX)
    new, new_identity = guest.snapshot(pending, limit=vm_protocol.KEY_FILE_MAX)
    data, receipt_identity = guest.snapshot(receipt, limit=guest.CONFIG_LIMIT)
    if json.loads(data) != {"config": current, "original": list(guest.identity(old_identity)),
                            "prepared": list(guest.identity(new_identity))}:
        raise guest.Refused("the host key or settings changed after preparation. Keep the pending key and inspect the guest before proceeding")
    vm_protocol.decode_key(new)
    _absent(backup)
    made = {}
    try:
        guest.write_new(backup, previous, made)
        copied, backup_identity = guest.snapshot(backup, limit=vm_protocol.KEY_FILE_MAX)
        if copied != previous:
            raise guest.Refused("the old host key backup differs")
        guest.recheck(receipt, receipt_identity)
        guest.recheck(backup, backup_identity)
        guest.recheck(pending, new_identity)
        guest.recheck(key, old_identity)
        os.replace(pending, key)
    except (OSError, guest.Refused):
        print(f"Inspect host key: {key}\nPrepared key: {pending}\nOld key backup: {backup}")
        print("Keep the guest stopped. Check which files remain before switching or restoring either key.")
        raise
    print(f"Activated host key: {key}\nOld host key retained: {backup}\nRotation record: {receipt}")
    print("Start the guest and verify the connection. After verification, remove the old host key backup, "
          "the rotation record and the incoming copies of the new key.")


def _make_guest(args, config):
    user = guest.guest_user(args.guest_user)
    output = args.out
    _out_folder(output)
    host_key = config.key_file
    original = None
    data = _guest_config(config, user)
    if not args.update:
        guest.plain(host_key.parent, directory=True)
        if args.replace_key:
            data = _rotation_config(config, user, args.previous_guest_config, args.previous_host_key)
            original = guest.plain(host_key, directory=False)
            pending, receipt, backup = _rotation_paths(host_key)
            for path in (pending, receipt, backup):
                _absent(path)
        else:
            _absent(host_key)

    if {path.name for path in GUEST_SOURCE.iterdir()} != set(guest.GUEST_FILES):
        raise guest.Refused("unexpected guest source file set")
    written = {}
    folder = None
    try:
        output.mkdir()
        folder = (output, output.lstat())
        for name in guest.CODE_FILES:
            source = (AGENT_SOURCE if name in guest.AGENT_FILES else GUEST_SOURCE) / name
            guest.plain(source, directory=False)
            guest.write_new(output / name, source.read_bytes(), written)
        guest.write_new(output / guest.CONFIG,
                        (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), written)
        guest.load_config(output, vm_protocol)
        digests = [(name, hashlib.sha256((output / name).read_bytes()).hexdigest())
                   for name in guest.HASHED_FILES]
        manifest = (json.dumps({"format": 1, "vm_name": config.vm_name, "sha256": dict(digests)},
                               ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        guest.write_new(output / guest.MANIFEST, manifest, written)
        digests.append((guest.MANIFEST, hashlib.sha256(manifest).hexdigest()))

        if not args.update:
            def write_key(path, key):
                guest.write_new(path, key, written)
                if original is None:
                    guest.write_new(host_key, key, written)
                    return
                guest.write_new(pending, key, written)

            key_args = argparse.Namespace(command="make-key", out=output / guest.KEY)
            if vm_client.run(key_args, writer=write_key, output=lambda _: None):
                raise guest.Refused("cannot create the key files")
            if original is not None:
                guest.recheck(host_key, original)
                prepared = guest.snapshot(pending, limit=vm_protocol.KEY_FILE_MAX)[1]
                receipt_bytes = json.dumps({"config": data, "original": list(guest.identity(original)),
                                            "prepared": list(guest.identity(prepared))}).encode("utf-8")
                guest.write_new(receipt, receipt_bytes, written)
    except BaseException:
        guest.rollback(written, folder)
        raise

    print(f"Guest bundle: {output}")
    for name, digest in digests:
        print(digest, name)
    if not args.update:
        print(f"Guest key: {output / guest.KEY}")
        print(f"Host key: {host_key}")
    print("Send the vm-guest folder to the VM and run check.cmd there.")
    if args.update:
        print("Stop the agent and run update.cmd in the incoming folder.")
    elif args.replace_key:
        print(f"Prepared host key: {pending}. The current host key is still active.")
        print("Keep a copy of the old vm-guest.json. Stop the guest, retain its old key securely for recovery, "
              "run remove.cmd from a matching bundle with the old configuration, then run install.cmd from the new bundle.")
        print("After guest installation succeeds, run activate-key with the same --guest-user and "
              "--previous-guest-config and --previous-host-key. If guest installation fails, keep the old host key and restore "
              "the guest with the old configuration and old guest key.")
        print("The existing firewall rule can stay in place if its settings still match.")
    else:
        print("Run firewall-admin.cmd as administrator from the incoming folder. "
              "It checks the bundle against the manifest automatically. "
              "Then run install.cmd as the guest user.")
    print("Retain a copy of vm-guest.json without the key for future setup changes.")
    if not args.update:
        print(r"After setup, delete the vm-guest copy under C:\SoraFromHost in the VM because it holds a key.")
    print("Delete the vm-guest folder on the host after setup"
          + ("." if args.update else " because it holds a copy of the key."))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make_guest = commands.add_parser("make-guest")
    make_guest.add_argument("--guest-user", required=True)
    make_guest.add_argument("--out", required=True, type=Path)
    make_guest.add_argument("--previous-guest-config", type=Path)
    make_guest.add_argument("--previous-host-key", type=Path)
    mode = make_guest.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true")
    mode.add_argument("--replace-key", action="store_true")
    activate = commands.add_parser("activate-key")
    activate.add_argument("--guest-user", required=True)
    activate.add_argument("--previous-guest-config", required=True, type=Path)
    activate.add_argument("--previous-host-key", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if Path(os.environ.get("USERPROFILE", "")).anchor.startswith(("\\\\", "//")):
            raise guest.Refused("the host profile must be on a local drive")
        config = load_vm_control()
        if config is None:
            raise guest.Refused("vm-control.json is missing")
        if args.command == "activate-key":
            _activate_key(args, config)
        else:
            _make_guest(args, config)
    except (KeyError, OSError, ValueError, VmConfigurationError, vm_protocol.ProtocolError) as error:
        print(f"[HOLD] {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
