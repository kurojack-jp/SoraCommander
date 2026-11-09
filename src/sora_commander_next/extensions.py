"""Explicit trusted local plugins only. This is fault isolation, not a sandbox."""

from importlib import import_module
from typing import Protocol


class Extension(Protocol):
    def invoke(self, operation: str, payload: dict) -> dict: ...


class Registry:
    def __init__(self):
        self._extensions: dict[str, Extension] = {}

    def register(self, name: str, extension: Extension):
        if name in self._extensions or not callable(getattr(extension, "invoke", None)):
            raise ValueError("duplicate name or invalid extension")
        self._extensions[name] = extension

    def load_local(self, name: str, module: str) -> dict:
        try:
            self.register(name, import_module(module).create_extension())
            return {"status": "OK"}
        except Exception as exc:
            return {"status": "ERROR", "error": type(exc).__name__}

    def invoke(self, name: str, operation: str, payload: dict) -> dict:
        try:
            result = self._extensions[name].invoke(operation, payload)
            if not isinstance(result, dict):
                raise TypeError("extension must return a dict")
            return result
        except Exception as exc:
            return {"status": "ERROR", "error": type(exc).__name__}
