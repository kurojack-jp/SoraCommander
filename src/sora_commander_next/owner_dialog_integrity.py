"""Startup pins for isolated Owner windows and their transitive fixed siblings."""
import ast
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType


DIALOG_NAMES = frozenset({"owner_command_dialog_localized.py",
                          "owner_file_dialog_localized.py",
                          "owner_approval_dialog_v2.py"})


def sibling_names(data):
    """Extract both isolated sibling loaders and fixed Path.with_name imports."""
    names = set()
    for node in ast.walk(ast.parse(data)):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.id if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute) else "")
        if name not in {"_load_sibling", "with_name"}:
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            # _load_sibling itself forwards its literal-checked caller's name.
            if name == "with_name":
                continue
            raise ValueError("Owner sibling must have a literal filename")
        value = node.args[0].value
        if not isinstance(value, str) or Path(value).name != value:
            raise ValueError("invalid Owner sibling filename")
        if value.endswith(".py"):
            names.add(value)
    return names


class DialogIntegrity:
    def __init__(self, folder):
        self.folder = Path(folder)
        hashes = {}
        try:
            pending = set(DIALOG_NAMES)
            while pending:
                name = pending.pop()
                if name in hashes:
                    continue
                data = (self.folder / name).read_bytes()
                hashes[name] = sha256(data).digest()
                pending.update(sibling_names(data) - hashes.keys())
            self.hashes = MappingProxyType(hashes)
        except (OSError, ValueError, SyntaxError):
            self.hashes = None

    def verify(self):
        try:
            if self.hashes is None or any(
                    sha256((self.folder / name).read_bytes()).digest() != digest
                    for name, digest in self.hashes.items()):
                raise PermissionError("Owner dialog integrity unavailable")
        except OSError as exc:
            raise PermissionError("Owner dialog integrity unavailable") from exc


# One snapshot when the review component starts; never refresh before a review.
DIALOG_INTEGRITY = DialogIntegrity(Path(__file__).resolve().parent)
