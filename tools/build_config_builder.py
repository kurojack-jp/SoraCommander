"""Assemble an offline Config Builder ZIP from this checkout; publish nothing."""
import hashlib
import json
from pathlib import Path
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "LICENSE", "NOTICE.md",
    "tools/scn_config_builder.py", "tools/config_builder/__init__.py",
    "tools/config_builder/model.py", "tools/config_builder/gui.py",
    "src/sora_commander_next/__init__.py", "src/sora_commander_next/allowed_roots.py",
    "src/sora_commander_next/task_presets.py", "src/sora_commander_next/owner_ui_language.py",
    "src/sora_commander_next/commands.py", "src/sora_commander_next/file_reads.py",
    "docs/config-builder.md", "docs/config-builder.ja.md",
)
QUICKSTART = {
    "QUICKSTART.md": """# Sora Commander Config Builder — quick start

Windows + Python 3.13 with Tcl/Tk. Extract the entire ZIP before starting.
From the extracted folder, run `py -3.13 -B tools\\scn_config_builder.py`.

1. Add folders. New entries are read-only; changing to full access asks for confirmation.
2. Select Windows default, English or Japanese for the approval windows.
3. Add npm/pytest task templates, adjust the project folder and arguments, then apply the form to JSON.
4. Review all three files, acknowledge the read-access warning and confirm WRITE/DELETE.
5. Check the result and restart both the Broker and client yourself.

ChatGPT can read allowed files without approval. Do not allow secrets.
Read-only file settings cannot prevent writes by approved commands.
No command is executed by this editor. There is no network connection.

Check only: `py -3.13 -B tools\\scn_config_builder.py --check`.
Exit 0 means no HOLD; exit 1 means HOLD. No configuration is written in check mode.

Backups use .bak-YYYYMMDD-HHMMSS. Atomic replacement is per file.
On failure, inspect completed files, backups and retained temporary files before further saving.
See docs/config-builder.md for recovery and limits.
The package carries matching validators. With no roots file, its extracted folder is its default scope.
Set explicit allowed folders and recheck with your actual Sora Commander installation.
""",
    "QUICKSTART.ja.md": """# Sora Commander Config Builder — クイックスタート

WindowsとTcl/Tk付きPython 3.13を使います。ZIPを全て展開してください。
展開先で `py -3.13 -B tools\\scn_config_builder.py` を実行します。

1. フォルダを追加します。既定は読むだけです。フルへの変更時は警告を確認します。
2. 承認窓の言語をWindows既定・English・日本語から選びます。
3. npm／pytestのタスクを追加し、作業場所と引数を調整してフォームをJSONに反映します。
4. 3ファイルの全文を確認し、読み取りの警告にチェックしてWRITE／DELETEを確認します。
5. 結果を確認し、Brokerとクライアントを手動で再起動します。

許可フォルダは承認窓なしでChatGPTから読まれます。秘密情報のある場所は許可しないでください。
読むだけの設定でも、承認したコマンドによる書き込みは防げません。
このエディターはコマンドを実行せず、ネットワークにも接続しません。

検証だけなら `py -3.13 -B tools\\scn_config_builder.py --check` を使います。
終了0はHOLDなし、終了1はHOLDありです。検証モードでは設定を書き込みません。

バックアップ名は .bak-YYYYMMDD-HHMMSS です。atomic置換は1ファイル単位です。
失敗時は保存済みファイル・バックアップ・残った一時ファイルを確認してから次の保存を判断してください。
復旧手順と制限は docs/config-builder.ja.md にあります。
検証モジュールを同梱しています。設定がない場合の既定範囲は展開先です。
許可フォルダを明示して、実際に使うSora Commanderでも再検証してください。
""",
}


def ordinary(path, *, directory):
    info = path.lstat()
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not expected or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or (not directory and info.st_nlink != 1)):
        raise ValueError("Linked or non-ordinary build path refused")


def build(root=ROOT):
    root = Path(root)
    for parent in (*reversed(root.parents), root):
        ordinary(parent, directory=True)
    contents = {}
    for name in FILES:
        source = root / name
        if name in ("LICENSE", "NOTICE.md") and (root / "docs/public_release").is_dir():
            source = root / "docs/public_release" / name
        for parent in source.relative_to(root).parents:
            ordinary(root / parent, directory=True)
        ordinary(source, directory=False)
        contents[name] = source.read_bytes()
    contents.update({name: text.encode("utf-8") for name, text in QUICKSTART.items()})
    manifest = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in contents.items()]
    artifacts = root / "artifacts"
    try:
        ordinary(artifacts, directory=True)
    except FileNotFoundError:
        artifacts.mkdir()
    output = artifacts / "config-builder"
    output.mkdir()  # Existing outputs are never replaced or removed.
    temporary = output / "config-builder.zip.tmp"
    with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
        archive.writestr("MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP verification failed")
        for name, data in contents.items():
            if archive.read(name) != data:
                raise ValueError("ZIP content verification failed")
    destination = output / "config-builder.zip"
    temporary.rename(destination)
    return destination


if __name__ == "__main__":
    try:
        result = build()
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"[HOLD] Build stopped: {type(error).__name__}")
        raise SystemExit(1)
    print(f"[PASS] Built {result.name} in artifacts/config-builder. Review before distribution.")
