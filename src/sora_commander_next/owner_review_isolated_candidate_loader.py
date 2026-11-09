"""Fixed-sibling loader for DEV-only Owner UI presentation smoke under -I.

The active child is NOT wired to this loader. No Tk, approval, callbacks,
processes or privilege operations. It does not change sys.path. Loading
modules from non-fixed or linked paths is forbidden.
"""
import importlib.util
from pathlib import Path
import sys
import threading

_LOCK = threading.Lock()
_PACKAGE_NAME = "_sora_fixed_owner_ui_candidate"
_CANDIDATE_NAME = _PACKAGE_NAME + ".owner_review_request_binding_candidate"
_CANDIDATE_FILE = "owner_review_request_binding_candidate.py"
_FIXED_FILES = frozenset({
    "__init__.py", _CANDIDATE_FILE,
    "owner_approval_dialog.py", "owner_approval_dialog_v2.py",
    "owner_review_presentation_candidate.py", "owner_review_i18n.py",
    "owner_command_caution.py", "owner_review_renderer.py",
})
_FIXED_RESOURCES = frozenset({
    "en.json", "ja.json", "caution_en.json", "caution_ja.json",
})


def _verify_fixed_origin(folder):
    """Reject links/junctions before loading or reusing any candidate module."""
    for parent in (folder, *folder.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("linked ancestor in Owner candidate path")
    for filename in _FIXED_FILES:
        sibling = folder / filename
        if sibling.is_symlink() or not sibling.is_file():
            raise ValueError("missing or linked Owner UI sibling")
    resource_dir = folder / "i18n"
    if (resource_dir.is_symlink() or resource_dir.is_junction()
            or not resource_dir.is_dir()):
        raise ValueError("missing or linked Owner UI translations")
    for filename in _FIXED_RESOURCES:
        item = resource_dir / filename
        if item.is_symlink() or not item.is_file():
            raise ValueError("missing or linked Owner UI translation")


def _verify_package_cache(folder, init_file):
    pkg = sys.modules.get(_PACKAGE_NAME)
    if (pkg is None or getattr(pkg, "__file__", None) != str(init_file)
            or list(getattr(pkg, "__path__", ())) != [str(folder)]):
        raise ValueError("untrusted cached Owner UI package")
    for name, module in tuple(sys.modules.items()):
        if not name.startswith(_PACKAGE_NAME + "."):
            continue
        expected_name = name[len(_PACKAGE_NAME) + 1:] + ".py"
        if (expected_name not in _FIXED_FILES
                or getattr(module, "__file__", None) != str(folder / expected_name)
                or getattr(module, "__package__", None) != _PACKAGE_NAME):
            raise ValueError("unexpected cached Owner UI module")



def load_bound_review_candidate():
    """Load the exact fixed sibling package and binding module, once.

    The package's explicit __path__ is the trusted sibling directory, never
    sys.path or a caller-selected location. No product approval takes place.
    """
    # absolute() preserves a lexical symlink path for ancestry checks.
    folder = Path(__file__).absolute().parent
    init_file = folder / "__init__.py"
    target = folder / _CANDIDATE_FILE
    with _LOCK:
        _verify_fixed_origin(folder)
        existing = sys.modules.get(_CANDIDATE_NAME)
        if existing is not None:
            _verify_package_cache(folder, init_file)
            if getattr(existing, "__file__", None) != str(target):
                raise ValueError("unexpected candidate module origin")
            return existing
        if _PACKAGE_NAME in sys.modules:
            raise ValueError("unexpected cached Owner package")
        pkg_spec = importlib.util.spec_from_file_location(
            _PACKAGE_NAME, init_file,
            submodule_search_locations=[str(folder)])
        if pkg_spec is None or pkg_spec.loader is None:
            raise RuntimeError("missing fixed Owner package")
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys.modules[_PACKAGE_NAME] = pkg
        try:
            pkg_spec.loader.exec_module(pkg)
            spec = importlib.util.spec_from_file_location(_CANDIDATE_NAME, target)
            if spec is None or spec.loader is None:
                raise RuntimeError("missing fixed Owner binding candidate")
            candidate = importlib.util.module_from_spec(spec)
            sys.modules[_CANDIDATE_NAME] = candidate
            spec.loader.exec_module(candidate)
            return candidate
        except BaseException:
            # Do not retain a half-imported display module as a valid loader.
            for name in tuple(sys.modules):
                if name == _PACKAGE_NAME or name.startswith(_PACKAGE_NAME + "."):
                    del sys.modules[name]
            raise
