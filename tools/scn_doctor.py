"""Read-only setup checks. Never starts services or opens tunnel secrets."""
import argparse
import importlib
import os
from pathlib import Path
import re
import stat
import sys

# A diagnostic must not create bytecode, even when run without -B.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MESSAGES = {
    "en": {
        "runtime": "Windows and Python",
        "packages": "Packages",
        "location": "Where this copy lives",
        "roots": "allowed-roots.txt",
        "language": "language.txt",
        "tasks": "tasks.json",
        "bridge": "ogf-bridge.json",
        "tunnel": "Tunnel program, settings and key (existence only)",
        "broker": "Broker running now",
        "runtime_fix": "see INSTALL section 1",
        "packages_fix": "see INSTALL section 1",
        "location_fix": "avoid folders named temp or tmp, or starting with a dot; see INSTALL section 1",
        "roots_fix": "see INSTALL: Allow your project folders",
        "language_fix": "see INSTALL: Choose the window language",
        "tasks_fix": "see INSTALL: Task presets",
        "bridge_fix": "see INSTALL: Optional companion cue",
        "tunnel_fix": "see INSTALL sections 2 to 4",
        "broker_fix": "start it as in INSTALL section 5; restart the Broker and its client after any settings change",
        "off": "off", "ok": "OK", "default": "no file, default folder only",
        "present": "yes", "absent": "not found", "unavailable": "could not check",
        "runtime_bad": "Windows and Python {required} or newer required",
    },
    "ja": {
        "runtime": "WindowsとPython",
        "packages": "パッケージ",
        "location": "このコピーの置き場所",
        "roots": "allowed-roots.txt",
        "language": "language.txt",
        "tasks": "tasks.json",
        "bridge": "ogf-bridge.json",
        "tunnel": "トンネルのプログラム・設定・キー（あるかどうかだけ確認）",
        "broker": "Brokerが今動いているか",
        "runtime_fix": "INSTALLの手順1を見てください",
        "packages_fix": "INSTALLの手順1を見てください",
        "location_fix": "temp・tmpという名前や、ドットで始まるフォルダは避けてください。INSTALLの手順1を見てください",
        "roots_fix": "INSTALLの「自分のプロジェクトのフォルダを許可する」を見てください",
        "language_fix": "INSTALLの「承認窓の言語を選ぶ」を見てください",
        "tasks_fix": "INSTALLの「Ownerのタスクプリセット」を見てください",
        "bridge_fix": "INSTALLの「任意の連携cue」を見てください",
        "tunnel_fix": "INSTALLの手順2〜4を見てください",
        "broker_fix": "INSTALLの手順5のとおり起動してください。設定を変えたあとはBrokerとクライアントを再起動してください",
        "off": "オフ", "ok": "OK", "default": "ファイルなし（既定のフォルダだけ）",
        "present": "はい", "absent": "見つかりません", "unavailable": "確認できませんでした",
        "runtime_bad": "WindowsとPython {required} 以上が必要です",
    },
}


def text(key, lang):
    return MESSAGES.get(lang, {}).get(key, MESSAGES["en"][key])


def check_runtime(environ, root, *, platform=sys.platform, version=sys.version_info):
    try:
        metadata = (root / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', metadata)
        if match is None:
            return "HOLD", "runtime", "unavailable"
        minimum = tuple(map(int, match.groups()))
        if platform != "win32" or tuple(version[:2]) < minimum:
            return "HOLD", "runtime", ">=" + ".".join(map(str, minimum))
        return "PASS", "runtime", "ok"
    except OSError:
        return "HOLD", "runtime", "unavailable"


def check_packages(environ, root, *, importer=importlib.import_module):
    missing = []
    for name in ("sora_commander_next", "win32api", "mcp"):
        try:
            importer(name)
        except (ImportError, OSError):
            missing.append(name)
    return ("HOLD" if missing else "PASS", "packages", ", ".join(missing) or "ok")


def check_location(environ, root):
    suspect = any(p.casefold() in ("temp", "tmp") or p.startswith(".")
                  for p in root.parts[1:])
    return ("WARN", "location", "") if suspect else ("PASS", "location", "ok")


def check_roots(environ, root):
    from sora_commander_next.allowed_roots import startup_allowed_roots, AllowedRootsError
    try:
        roots = startup_allowed_roots(root, "development", environ)
        config = Path(environ["USERPROFILE"]) / ".config/sora-commander/allowed-roots.txt"
        if not config.exists():
            return "WARN", "roots", "default"
        if any(not Path(folder).is_dir() for folder in roots.read):
            return "WARN", "roots", "absent"
        return "PASS", "roots", "ok"
    except AllowedRootsError as error:
        return "HOLD", "roots", str(error)


def check_language(environ, root):
    from sora_commander_next.owner_ui_language import resolve_owner_ui_language, OwnerLanguageError
    try:
        lang, source = resolve_owner_ui_language(environ)
        return "PASS", "language", f"{lang} ({source})"
    except OwnerLanguageError as error:
        return "HOLD", "language", str(error)


def check_tasks(environ, root):
    from sora_commander_next.task_presets import load_task_presets, TaskPresetError
    try:
        return "PASS", "tasks", "ok" if load_task_presets(environ) else "off"
    except TaskPresetError as error:
        return "HOLD", "tasks", str(error)


def check_bridge(environ, root):
    from sora_commander_next.ogf_cue import load_ogf_bridge, OgfBridgeError
    try:
        return "PASS", "bridge", "off" if load_ogf_bridge(environ) is None else "ok"
    except OgfBridgeError as error:
        return "HOLD", "bridge", str(error)


def tunnel_paths(environ, root):
    # Tracked start-sora-commander-broker-dev.cmd / environment launcher paths.
    config = Path(environ["USERPROFILE"]) / ".config/tunnel-client"
    return (root / "tools/tunnel-client/v0.0.16/tunnel-client.exe",
            config / "sora-next.yaml", config / "sora-next.key")


def check_tunnel(environ, root, *, paths=None):
    try:
        missing = []
        for path in tunnel_paths(environ, root) if paths is None else paths:
            try:
                info = path.lstat()
                ordinary = (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                            and not getattr(info, "st_file_attributes", 0) & 0x400)
            except FileNotFoundError:
                ordinary = False
            if not ordinary:
                missing.append(path.name)
        return "WARN" if missing else "PASS", "tunnel", ", ".join(missing) or "ok"
    except (OSError, KeyError):
        return "WARN", "tunnel", "unavailable"


def pipe_exists():
    from sora_commander_next.windows_owner import current_windows_owner
    from sora_commander_next.owner_broker_pipe import broker_pipe_name
    owner = current_windows_owner()
    directory, _, name = broker_pipe_name(owner.sid, owner.session_id).rpartition("\\")
    # List the pipe namespace, as the launcher does. Opening or stat-ing the
    # pipe itself would connect to the Broker as a client.
    return name.casefold() in {entry.casefold() for entry in os.listdir(directory + "\\")}


def check_broker(environ, root, *, exists=pipe_exists):
    try:
        present = exists()
        return "PASS" if present else "WARN", "broker", "present" if present else "absent"
    except (OSError, ImportError, ValueError):
        return "WARN", "broker", "unavailable"


CHECKS = (check_runtime, check_packages, check_location, check_roots,
          check_language, check_tasks, check_bridge, check_tunnel, check_broker)


def main(argv=None, *, environ=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", choices=("en", "ja"))
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    language_result = None
    try:
        from sora_commander_next.owner_ui_language import resolve_owner_ui_language, OwnerLanguageError
        try:
            lang = resolve_owner_ui_language(environ)[0]
        except OwnerLanguageError as error:
            lang = "en"
            language_result = ("HOLD", "language", str(error))
    except (ImportError, OSError):
        lang = "en"
    lang = args.lang or lang
    held = False
    for check in CHECKS:
        try:
            result = language_result if check is check_language and language_result else check(environ, root)
        except (ImportError, OSError):
            result = ("HOLD", check.__name__.removeprefix("check_"), "unavailable")
        level, key, detail = result
        if key == "runtime" and detail.startswith(">="):
            detail = text("runtime_bad", lang).format(required=detail[2:])
        elif detail in MESSAGES["en"]:
            detail = text(detail, lang)
        line = f"[{level}] {text(key, lang)}" + (f": {detail}" if detail else "")
        if level != "PASS":
            # The fix hint appears only when there is something to fix.
            line += " -- " + text(key + "_fix", lang)
        print(line)
        held |= level == "HOLD"
    return int(held)


if __name__ == "__main__":
    raise SystemExit(main())
