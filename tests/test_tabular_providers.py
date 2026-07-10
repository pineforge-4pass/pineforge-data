from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from pineforge_data import (
    BarColumnMapping,
    BarRequest,
    CsvBarProvider,
    Instrument,
    MarketNotFoundError,
    SchemaMappingError,
    SqlAlchemyBarProvider,
    SqliteBarProvider,
    TabularDataError,
    TimestampUnit,
    create_provider,
)


def test_mapping_infers_common_names_and_accepts_partial_overrides() -> None:
    mapping = BarColumnMapping.infer(
        ("Bucket Start", "PX_OPEN", "high", "low", "close", "qty", "ticker", "interval"),
        {"timestamp": "Bucket Start"},
    )

    assert mapping.timestamp == "Bucket Start"
    assert mapping.open == "PX_OPEN"
    assert mapping.volume == "qty"
    assert mapping.symbol == "ticker"
    assert mapping.timeframe == "interval"


def test_mapping_rejects_ambiguous_or_missing_columns() -> None:
    with pytest.raises(SchemaMappingError, match="ambiguous mappings for timestamp"):
        BarColumnMapping.infer(("timestamp", "open_time", "open", "high", "low", "close", "volume"))

    with pytest.raises(SchemaMappingError, match="missing mappings for volume"):
        BarColumnMapping.infer(("timestamp", "open", "high", "low", "close"))


def _write_csv(path: Path) -> None:
    path.write_text(
        "Bucket Start,PX_OPEN,PX_HIGH,PX_LOW,PX_CLOSE,Total Qty,Ticker,Bar Size\n"
        "2025-07-01T00:01:00Z,11,13,10,12,6,AAPL,1m\n"
        "2025-07-01T00:00:00Z,10,12,9,11,5,AAPL,1m\n"
        "2025-07-01T00:00:00Z,20,22,19,21,8,MSFT,1m\n",
        encoding="utf-8",
    )


def _arbitrary_mapping() -> dict[str, str]:
    return {
        "timestamp": "Bucket Start",
        "open": "PX_OPEN",
        "high": "PX_HIGH",
        "low": "PX_LOW",
        "close": "PX_CLOSE",
        "volume": "Total Qty",
        "symbol": "Ticker",
        "timeframe": "Bar Size",
    }


def test_csv_provider_inspects_arbitrary_columns_and_normalizes_rows(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "research bars.csv"
        _write_csv(path)
        provider = CsvBarProvider(
            path,
            venue="research",
            mapping=_arbitrary_mapping(),
            timestamp_unit="iso8601",
        )

        schema = await provider.inspect_schema()
        assert schema.column_names[0] == "Bucket Start"
        assert [item.instrument.symbol for item in await provider.list_markets()] == [
            "AAPL",
            "MSFT",
        ]
        listing = await provider.resolve_market("AAPL")
        bars = await provider.fetch_bars(
            BarRequest(
                listing.instrument,
                "1m",
                1_751_328_000_000,
                1_751_328_120_000,
            )
        )

        assert [bar.timestamp_ms for bar in bars] == [
            1_751_328_000_000,
            1_751_328_060_000,
        ]
        assert [bar.close for bar in bars] == [11.0, 12.0]
        assert all(bar.source == "csv:research" for bar in bars)
        with pytest.raises(MarketNotFoundError, match="no exact symbol"):
            await provider.resolve_market("NVDA")

    asyncio.run(run())


def test_csv_single_instrument_dataset_binds_symbol_at_resolution(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "single.csv"
        path.write_text(
            "timestamp,open,high,low,close,volume\n0,1,2,1,2,10\n",
            encoding="utf-8",
        )
        provider = CsvBarProvider(path, venue="research")

        assert await provider.list_markets() == []
        listing = await provider.resolve_market("PRIVATE_SERIES")
        bars = await provider.fetch_bars(BarRequest(listing.instrument, "1m", 0, 60_000))

        assert listing.instrument.symbol == "PRIVATE_SERIES"
        assert len(bars) == 1

    asyncio.run(run())


def _write_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE TABLE "price candles" ('
            '"epoch seconds" INTEGER, "first px" REAL, "top px" REAL, '
            '"bottom px" REAL, "last px" REAL, "traded qty" REAL, '
            '"security code" TEXT, "bar interval" TEXT)'
        )
        connection.executemany(
            'INSERT INTO "price candles" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [
                (1_751_328_000, 10, 12, 9, 11, 5, "AAPL", "1m"),
                (1_751_328_060, 11, 13, 10, 12, 6, "AAPL", "1m"),
                (1_751_328_000, 20, 22, 19, 21, 8, "MSFT", "1m"),
            ],
        )


def _sqlite_mapping() -> BarColumnMapping:
    return BarColumnMapping(
        timestamp="epoch seconds",
        open="first px",
        high="top px",
        low="bottom px",
        close="last px",
        volume="traded qty",
        symbol="security code",
        timeframe="bar interval",
    )


def test_sqlite_provider_reflects_and_queries_arbitrary_ddl(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "market.sqlite3"
        _write_sqlite(path)
        provider = SqliteBarProvider(
            path,
            "price candles",
            venue="warehouse",
            mapping=_sqlite_mapping(),
            timestamp_unit=TimestampUnit.SECONDS,
        )

        schema = await provider.inspect_schema()
        assert schema.column_names == (
            "epoch seconds",
            "first px",
            "top px",
            "bottom px",
            "last px",
            "traded qty",
            "security code",
            "bar interval",
        )
        listing = await provider.resolve_market("AAPL")
        bars = await provider.fetch_bars(
            BarRequest(
                listing.instrument,
                "1m",
                1_751_328_030_000,
                1_751_328_120_000,
            )
        )
        assert [bar.timestamp_ms for bar in bars] == [1_751_328_060_000]

    asyncio.run(run())


def test_sqlalchemy_provider_uses_reflection_and_bound_filters(tmp_path: Path) -> None:
    pytest.importorskip("sqlalchemy")

    async def run() -> None:
        path = tmp_path / "sqlalchemy.sqlite3"
        _write_sqlite(path)
        provider = SqlAlchemyBarProvider(
            f"sqlite:///{path}",
            "price candles",
            venue="analytics",
            mapping=_sqlite_mapping(),
            timestamp_unit="seconds",
        )
        try:
            schema = await provider.inspect_schema()
            assert "first px" in schema.column_names
            listing = await provider.resolve_market("MSFT")
            bars = await provider.fetch_bars(
                BarRequest(
                    listing.instrument,
                    "1m",
                    1_751_328_000_000,
                    1_751_328_060_000,
                )
            )
            assert len(bars) == 1
            assert bars[0].close == 21.0
            assert bars[0].source == "sqlalchemy:analytics"
        finally:
            await provider.close()

    asyncio.run(run())


def test_registry_builds_csv_provider_from_runtime_json_shape(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    _write_csv(path)

    provider = create_provider(
        "csv",
        "research",
        config={
            "path": str(path),
            "timestamp_unit": "iso8601",
            "columns": _arbitrary_mapping(),
        },
    )

    assert isinstance(provider, CsvBarProvider)
    assert provider.name == "csv:research"


def test_registry_reads_sqlalchemy_url_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "database.sqlite3"
    _write_sqlite(path)
    monkeypatch.setenv("TEST_PINEFORGE_DATABASE_URL", f"sqlite:///{path}")

    provider = create_provider(
        "sqlalchemy",
        "analytics",
        config={
            "url_env": "TEST_PINEFORGE_DATABASE_URL",
            "table": "price candles",
            "timestamp_unit": "seconds",
            "columns": {
                "timestamp": "epoch seconds",
                "open": "first px",
                "high": "top px",
                "low": "bottom px",
                "close": "last px",
                "volume": "traded qty",
                "symbol": "security code",
                "timeframe": "bar interval",
            },
        },
    )

    assert isinstance(provider, SqlAlchemyBarProvider)
    assert "TEST_PINEFORGE_DATABASE_URL" in os.environ
    asyncio.run(provider.close())


def test_duplicate_timestamp_is_rejected_instead_of_silently_overwritten(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "duplicates.csv"
        path.write_text(
            "timestamp,open,high,low,close,volume\n0,1,2,1,2,10\n0,2,3,1,2,20\n",
            encoding="utf-8",
        )
        provider = CsvBarProvider(path)
        listing = await provider.resolve_market("TEST")

        with pytest.raises(TabularDataError, match="duplicate bar timestamp"):
            await provider.fetch_bars(BarRequest(listing.instrument, "1m", 0, 60_000))

    asyncio.run(run())


def test_fixed_timeframe_is_validated_when_no_timeframe_column(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "bars.csv"
        path.write_text(
            "timestamp,open,high,low,close,volume\n0,1,2,1,2,10\n",
            encoding="utf-8",
        )
        provider = CsvBarProvider(
            path,
            instrument=Instrument("TEST"),
            timeframe="1m",
        )
        listing = await provider.resolve_market("TEST")

        with pytest.raises(ValueError, match="source timeframe"):
            await provider.fetch_bars(BarRequest(listing.instrument, "5m", 0, 60_000))

    asyncio.run(run())
