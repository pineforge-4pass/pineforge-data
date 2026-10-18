"""Deprecated import alias for :mod:`tunaxis_data` (shipped by pineforge-data)."""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import warnings
from types import CodeType, ModuleType
from typing import Any


class _AliasLoader(importlib.abc.Loader):
    """Reuse the canonical module without changing its import metadata."""

    def __init__(self, canonical: str) -> None:
        self.canonical = canonical
        self.spec: importlib.machinery.ModuleSpec | None = None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        module = importlib.import_module(self.canonical)
        self.spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        # Import machinery temporarily assigns the alias spec to the shared object.
        # Preserve canonical metadata for reload(), resources and later imports.
        module.__spec__ = self.spec

    def get_code(self, fullname: str) -> CodeType | None:
        # runpy uses get_code rather than exec_module for `python -m old.module`.
        spec = importlib.util.find_spec(self.canonical)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot find {self.canonical}")
        loader: Any = spec.loader
        return loader.get_code(self.canonical)  # type: ignore[no-any-return]


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith("pineforge_data."):
            return None
        canonical = "tunaxis_data" + fullname[len("pineforge_data") :]
        spec = importlib.util.find_spec(canonical)
        if spec is None:
            return None
        return importlib.util.spec_from_loader(
            fullname,
            _AliasLoader(canonical),
            is_package=spec.submodule_search_locations is not None,
        )


warnings.warn(
    "pineforge_data is deprecated; import tunaxis_data and install tunaxis-data instead.",
    DeprecationWarning,
    stacklevel=2,
)
sys.meta_path.insert(0, _AliasFinder())
sys.modules[__name__] = importlib.import_module("tunaxis_data")
