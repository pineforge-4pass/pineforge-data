from __future__ import annotations

import asyncio
import csv
import hashlib
import os
from dataclasses import dataclass
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import pytest

from pineforge_data import BarRequest, TabularBarProvider, create_provider

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = ROOT / "tests/fixtures/corpus_ohlcv_ETH-USDT-USDT_1m_sample.csv"
SAMPLE_SHA256 = "8ba4db0c669e47c11746361586cef8bce6371eeeab5af5e351107bd69451c6af"
TABLE_NAME = "corpus bars"
SYMBOL = "ETH/USDT:USDT"
TIMEFRAME = "1m"
MAPPING = {
    "timestamp": "bucket ms",
    "open": "first px",
    "high": "top px",
    "low": "bottom px",
    "close": "last px",
    "volume": "traded qty",
    "symbol": "security code",
    "timeframe": "bar interval",
}
SOURCE_COLUMNS = (*MAPPING.values(), "ignored note")


@dataclass(frozen=True, slots=True)
class CorpusBar:
    timestamp_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def normalized(self) -> tuple[int, float, float, float, float, float]:
        return (
            self.timestamp_ms,
            float(self.open),
            float(self.high),
            float(self.low),
            float(self.close),
            float(self.volume),
        )


def _load_corpus_rows(path: Path, limit: int) -> list[CorpusBar]:
    rows: list[CorpusBar] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["timestamp", "open", "high", "low", "close", "volume"]
        for raw in reader:
            rows.append(
                CorpusBar(
                    timestamp_ms=int(raw["timestamp"]),
                    open=Decimal(raw["open"]),
                    high=Decimal(raw["high"]),
                    low=Decimal(raw["low"]),
                    close=Decimal(raw["close"]),
                    volume=Decimal(raw["volume"]),
                )
            )
            if len(rows) == limit:
                break
    assert len(rows) == limit, f"{path} contains fewer than {limit} corpus bars"
    return rows


def _seed_database(url: str, rows: list[CorpusBar]) -> None:
    sqlalchemy = cast(Any, import_module("sqlalchemy"))
    metadata = sqlalchemy.MetaData()
    table = sqlalchemy.Table(
        TABLE_NAME,
        metadata,
        sqlalchemy.Column(MAPPING["timestamp"], sqlalchemy.BigInteger, nullable=False),
        sqlalchemy.Column(MAPPING["open"], sqlalchemy.Numeric(24, 8), nullable=False),
        sqlalchemy.Column(MAPPING["high"], sqlalchemy.Numeric(24, 8), nullable=False),
        sqlalchemy.Column(MAPPING["low"], sqlalchemy.Numeric(24, 8), nullable=False),
        sqlalchemy.Column(MAPPING["close"], sqlalchemy.Numeric(24, 8), nullable=False),
        sqlalchemy.Column(MAPPING["volume"], sqlalchemy.Numeric(30, 8), nullable=False),
        sqlalchemy.Column(MAPPING["symbol"], sqlalchemy.String(64), nullable=False),
        sqlalchemy.Column(MAPPING["timeframe"], sqlalchemy.String(16), nullable=False),
        sqlalchemy.Column("ignored note", sqlalchemy.String(64), nullable=True),
        sqlalchemy.PrimaryKeyConstraint(
            MAPPING["timestamp"], MAPPING["symbol"], MAPPING["timeframe"]
        ),
    )
    engine = sqlalchemy.create_engine(url, hide_parameters=True)
    try:
        metadata.drop_all(engine, tables=[table], checkfirst=True)
        metadata.create_all(engine, tables=[table])
        payload: list[dict[str, object]] = [
            {
                MAPPING["timestamp"]: row.timestamp_ms,
                MAPPING["open"]: row.open,
                MAPPING["high"]: row.high,
                MAPPING["low"]: row.low,
                MAPPING["close"]: row.close,
                MAPPING["volume"]: row.volume,
                MAPPING["symbol"]: SYMBOL,
                MAPPING["timeframe"]: TIMEFRAME,
                "ignored note": "pineforge-corpus",
            }
            for row in rows
        ]
        first = payload[0]
        payload.extend(
            [
                {
                    **first,
                    MAPPING["symbol"]: "BTC/USDT:USDT",
                    "ignored note": "symbol filter sentinel",
                },
                {
                    **first,
                    MAPPING["timeframe"]: "5m",
                    "ignored note": "timeframe filter sentinel",
                },
            ]
        )
        with engine.begin() as connection:
            for offset in range(0, len(payload), 100):
                connection.execute(table.insert(), payload[offset : offset + 100])
    finally:
        engine.dispose()


def _provider_config(backend: str, sqlite_path: Path | None = None) -> tuple[str, dict[str, Any]]:
    common: dict[str, Any] = {
        "table": TABLE_NAME,
        "timestamp_unit": "milliseconds",
        "columns": MAPPING,
    }
    if backend == "sqlite":
        assert sqlite_path is not None
        return "sqlite", {**common, "path": str(sqlite_path)}
    environment_name = {
        "mysql": "PINEFORGE_MYSQL_URL",
        "postgres": "PINEFORGE_POSTGRES_URL",
    }[backend]
    return "sqlalchemy", {**common, "url_env": environment_name}


async def _exercise_public_api(
    backend: str, rows: list[CorpusBar], sqlite_path: Path | None = None
) -> list[tuple[int, float, float, float, float, float]]:
    provider_name, config = _provider_config(backend, sqlite_path)
    provider = cast(
        TabularBarProvider,
        create_provider(provider_name, f"corpus-{backend}", config=config),
    )
    try:
        schema = await provider.inspect_schema()
        assert schema.column_names == SOURCE_COLUMNS
        assert "://" not in schema.source

        listings = await provider.list_markets()
        assert [listing.instrument.symbol for listing in listings] == [
            "BTC/USDT:USDT",
            SYMBOL,
        ]
        listing = await provider.resolve_market(SYMBOL)

        start_index = 17
        requested = rows[start_index : start_index + 100]
        bars = await provider.fetch_bars(
            BarRequest(
                instrument=listing.instrument,
                timeframe=TIMEFRAME,
                start_ms=requested[0].timestamp_ms,
                end_ms=rows[start_index + 150].timestamp_ms,
                limit=100,
            )
        )
        normalized = [
            (bar.timestamp_ms, bar.open, bar.high, bar.low, bar.close, bar.volume) for bar in bars
        ]
        assert normalized == [row.normalized() for row in requested]
        assert all(bar.instrument == listing.instrument for bar in bars)
        assert all(bar.source == provider.name for bar in bars)

        timeframe_sentinel = await provider.fetch_bars(
            BarRequest(
                instrument=listing.instrument,
                timeframe="5m",
                start_ms=rows[0].timestamp_ms,
                end_ms=rows[1].timestamp_ms,
            )
        )
        assert len(timeframe_sentinel) == 1
        return normalized
    finally:
        await provider.close()


def test_bundled_corpus_sample_has_pinned_provenance() -> None:
    assert hashlib.sha256(SAMPLE_PATH.read_bytes()).hexdigest() == SAMPLE_SHA256
    rows = _load_corpus_rows(SAMPLE_PATH, 256)
    assert rows[0].timestamp_ms == 1_577_836_800_000
    assert rows[-1].timestamp_ms == 1_577_852_100_000


@pytest.mark.skipif(
    os.environ.get("PINEFORGE_DATABASE_E2E") != "1",
    reason="set PINEFORGE_DATABASE_E2E=1 and database URLs to run corpus database E2E",
)
def test_public_database_provider_api_normalizes_corpus_across_backends(
    tmp_path: Path,
) -> None:
    corpus_path = Path(os.environ.get("PINEFORGE_CORPUS_OHLCV", str(SAMPLE_PATH)))
    limit = int(os.environ.get("PINEFORGE_DATABASE_E2E_ROWS", "256"))
    assert limit >= 168, "database E2E requires at least 168 source bars"
    rows = _load_corpus_rows(corpus_path, limit)

    mysql_url = os.environ["PINEFORGE_MYSQL_URL"]
    postgres_url = os.environ["PINEFORGE_POSTGRES_URL"]
    sqlite_path = tmp_path / "corpus.sqlite3"
    backends = {
        "sqlite": f"sqlite:///{sqlite_path}",
        "mysql": mysql_url,
        "postgres": postgres_url,
    }
    for url in backends.values():
        _seed_database(url, rows)

    normalized: dict[str, list[tuple[int, float, float, float, float, float]]] = {}
    for backend in backends:
        normalized[backend] = asyncio.run(
            _exercise_public_api(
                backend,
                rows,
                sqlite_path=sqlite_path if backend == "sqlite" else None,
            )
        )

    assert normalized["sqlite"] == normalized["mysql"] == normalized["postgres"]
