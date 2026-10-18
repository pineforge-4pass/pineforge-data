"""Concurrency-safe compiled-strategy cache keyed by generated C++."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(slots=True)
class _LockState:
    lock: asyncio.Lock
    references: int = 0


class CompileCache:
    """Store `.so` artifacts atomically and deduplicate concurrent compiles."""

    def __init__(self, root: Path, *, max_entries: int, max_bytes: int) -> None:
        if max_entries <= 0 or max_bytes <= 0:
            raise ValueError("cache limits must be positive")
        self.root = root
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_guard = asyncio.Lock()
        self._files_guard = asyncio.Lock()
        self._locks: dict[str, _LockState] = {}
        self._active: defaultdict[str, int] = defaultdict(int)
        self.hits = 0
        self.misses = 0
        self.compiles = 0

    def path(self, key: str) -> Path:
        return self.root / f"{key}.so"

    def temporary_path(self, key: str) -> Path:
        return self.root / f".{key}.{uuid4().hex}.tmp"

    @asynccontextmanager
    async def compile_lock(self, key: str) -> AsyncIterator[None]:
        async with self._lock_guard:
            state = self._locks.get(key)
            if state is None:
                state = _LockState(asyncio.Lock())
                self._locks[key] = state
            state.references += 1
        await state.lock.acquire()
        try:
            yield
        finally:
            state.lock.release()
            async with self._lock_guard:
                state.references -= 1
                if state.references == 0:
                    self._locks.pop(key, None)

    @asynccontextmanager
    async def use(self, key: str) -> AsyncIterator[Path]:
        path = await self.acquire(key)
        if path is None:
            raise FileNotFoundError(self.path(key))
        try:
            yield path
        finally:
            await self.release(key)

    async def lookup(self, key: str) -> Path | None:
        path = self.path(key)
        exists = await asyncio.to_thread(path.is_file)
        async with self._files_guard:
            if exists:
                self.hits += 1
            else:
                self.misses += 1
        return path if exists else None

    async def commit(self, key: str, temporary: Path) -> Path:
        target = self.path(key)
        await asyncio.to_thread(os.replace, temporary, target)
        await asyncio.to_thread(target.chmod, 0o555)
        async with self._files_guard:
            self.compiles += 1
        return target

    async def acquire(self, key: str) -> Path | None:
        """Atomically look up and reserve an artifact against eviction."""

        async with self._files_guard:
            path = self.path(key)
            if not path.is_file():
                self.misses += 1
                return None
            self.hits += 1
            self._active[key] += 1
            await asyncio.to_thread(os.utime, path, None)
            return path

    async def commit_and_acquire(self, key: str, temporary: Path) -> Path:
        """Atomically publish and reserve a newly compiled artifact."""

        async with self._files_guard:
            target = self.path(key)
            await asyncio.to_thread(os.replace, temporary, target)
            await asyncio.to_thread(target.chmod, 0o555)
            self.compiles += 1
            self._active[key] += 1
            return target

    async def release(self, key: str) -> None:
        async with self._files_guard:
            self._active[key] -= 1
            if self._active[key] == 0:
                self._active.pop(key, None)

    async def status(self) -> dict[str, int | str]:
        async with self._files_guard:
            files, sizes = await asyncio.to_thread(self._snapshot_sync)
            return {
                "directory": str(self.root),
                "entries": len(files),
                "bytes": sizes,
                "hits": self.hits,
                "misses": self.misses,
                "compiles": self.compiles,
            }

    async def trim(self) -> None:
        async with self._files_guard:
            active = set(self._active)
            await asyncio.to_thread(self._trim_sync, active)

    def _snapshot_sync(self) -> tuple[list[Path], int]:
        files: list[Path] = []
        size = 0
        for path in self.root.glob("*.so"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            files.append(path)
            size += stat.st_size
        return files, size

    def _trim_sync(self, active: set[str]) -> None:
        entries = []
        for path in self.root.glob("*.so"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            entries.append((stat.st_mtime_ns, stat.st_size, path))
        entries.sort()
        total = sum(size for _, size, _ in entries)
        count = len(entries)
        for _, size, path in entries:
            if count <= self.max_entries and total <= self.max_bytes:
                break
            if path.stem in active:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            count -= 1
            total -= size
