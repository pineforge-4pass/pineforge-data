from __future__ import annotations

import asyncio

from pineforge_data import CompileCache


def test_compile_cache_commits_and_reports_hits(tmp_path) -> None:
    async def run() -> None:
        cache = CompileCache(tmp_path, max_entries=2, max_bytes=1_000)
        assert await cache.lookup("abc") is None
        temporary = cache.temporary_path("abc")
        temporary.write_bytes(b"compiled")
        target = await cache.commit("abc", temporary)

        assert target.read_bytes() == b"compiled"
        assert await cache.lookup("abc") == target
        status = await cache.status()
        assert status["hits"] == 1
        assert status["misses"] == 1
        assert status["compiles"] == 1

    asyncio.run(run())


def test_compile_lock_deduplicates_same_key(tmp_path) -> None:
    async def run() -> None:
        cache = CompileCache(tmp_path, max_entries=10, max_bytes=10_000)
        active = 0
        maximum = 0

        async def worker() -> None:
            nonlocal active, maximum
            async with cache.compile_lock("same"):
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(worker() for _ in range(4)))
        assert maximum == 1

    asyncio.run(run())


def test_trim_preserves_reserved_artifacts(tmp_path) -> None:
    async def run() -> None:
        cache = CompileCache(tmp_path, max_entries=1, max_bytes=1_000)
        first_temp = cache.temporary_path("first")
        first_temp.write_bytes(b"first")
        first = await cache.commit_and_acquire("first", first_temp)
        second_temp = cache.temporary_path("second")
        second_temp.write_bytes(b"second")
        await cache.commit("second", second_temp)

        await cache.trim()

        assert first.is_file()
        assert not cache.path("second").exists()
        await cache.release("first")

    asyncio.run(run())
