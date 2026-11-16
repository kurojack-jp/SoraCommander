"""Local Owner configuration editor; --check only reads the three settings."""
import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from config_builder.model import check_existing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="read-only validation")
    args = parser.parse_args(argv)
    if args.check:
        results = check_existing(ROOT)
        for level, name, detail in results:
            print(f"[{level}] {name}: {detail}")
        return 1 if any(level == "HOLD" for level, _, _ in results) else 0
    if sys.platform != "win32":
        print("[HOLD] Windows is required for the configuration editor.")
        return 1
    try:
        from config_builder.gui import launch
        return launch(ROOT)
    except ImportError:
        print("[HOLD] Install Python with Tcl/Tk and keep this tool beside Sora Commander src/.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
