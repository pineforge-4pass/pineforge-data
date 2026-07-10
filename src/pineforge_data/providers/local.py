"""CSV and SQLite providers for user-owned historical bars."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import TextIO

from ..models import Instrument
from ..requests import BarRequest
from .tabular import (
    BarColumnMapping,
    ColumnMappingInput,
    SourceColumn,
    TabularBarProvider,
    TabularDataError,
    TabularRow,
    TabularSchema,
    TimestampUnit,
    numeric_source_bounds,
)


class CsvBarProvider(TabularBarProvider):
    """Read historical bars from a local CSV file with a runtime column mapping."""

    def __init__(
        self,
        path: str | Path,
        *,
        venue: str = "local",
        mapping: ColumnMappingInput = None,
        timestamp_unit: TimestampUnit | str = TimestampUnit.MILLISECONDS,
        timestamp_timezone: str = "UTC",
        instrument: Instrument | None = None,
        timeframe: str | None = None,
        encoding: str = "utf-8-sig",
        delimiter: str = ",",
    ) -> None:
        super().__init__(
            venue=venue,
            mapping=mapping,
            timestamp_unit=timestamp_unit,
            timestamp_timezone=timestamp_timezone,
            instrument=instrument,
            timeframe=timeframe,
        )
        if not encoding:
            raise ValueError("encoding must not be empty")
        if len(delimiter) != 1:
            raise ValueError("delimiter must be exactly one character")
        self.path = Path(path).expanduser().resolve()
        self.encoding = encoding
        self.delimiter = delimiter
        self.name = f"csv:{venue}"

    def _reader(self) -> tuple[csv.DictReader[str], TextIO]:
        if not self.path.is_file():
            raise FileNotFoundError(f"CSV file not found: {self.path}")
        handle = self.path.open("r", encoding=self.encoding, newline="")
        reader = csv.DictReader(handle, delimiter=self.delimiter)
        return reader, handle

    def _inspect_schema_sync(self) -> TabularSchema:
        reader, handle = self._reader()
        try:
            fieldnames = reader.fieldnames
            if not fieldnames:
                raise TabularDataError(f"CSV file has no header: {self.path}")
            if any(not fieldname for fieldname in fieldnames):
                raise TabularDataError("CSV header contains an empty column name")
            return TabularSchema(
                source=str(self.path),
                columns=tuple(SourceColumn(fieldname, "text", None) for fieldname in fieldnames),
            )
        finally:
            handle.close()

    def _all_rows_sync(self) -> tuple[TabularRow, ...]:
        reader, handle = self._reader()
        try:
            rows: list[TabularRow] = []
            for line_number, row in enumerate(reader, start=2):
                if None in row:
                    raise TabularDataError(f"CSV row {line_number} has more values than the header")
                rows.append(dict(row))
            return tuple(rows)
        finally:
            handle.close()

    def _read_rows_sync(
        self, mapping: BarColumnMapping, request: BarRequest
    ) -> tuple[TabularRow, ...]:
        del mapping, request
        return self._all_rows_sync()

    def _distinct_values_sync(self, column: str) -> tuple[object, ...]:
        return tuple(row[column] for row in self._all_rows_sync())


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class SqliteBarProvider(TabularBarProvider):
    """Read historical bars from one SQLite table or view in read-only mode."""

    def __init__(
        self,
        path: str | Path,
        table: str,
        *,
        venue: str = "local",
        mapping: ColumnMappingInput = None,
        timestamp_unit: TimestampUnit | str = TimestampUnit.MILLISECONDS,
        timestamp_timezone: str = "UTC",
        instrument: Instrument | None = None,
        timeframe: str | None = None,
    ) -> None:
        super().__init__(
            venue=venue,
            mapping=mapping,
            timestamp_unit=timestamp_unit,
            timestamp_timezone=timestamp_timezone,
            instrument=instrument,
            timeframe=timeframe,
        )
        if not table:
            raise ValueError("table must not be empty")
        self.path = Path(path).expanduser().resolve()
        self.table = table
        self.name = f"sqlite:{venue}"

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(f"SQLite database not found: {self.path}")
        connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _require_table(self, connection: sqlite3.Connection) -> None:
        found = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type IN ('table', 'view') AND name = ? LIMIT 1",
            (self.table,),
        ).fetchone()
        if found is None:
            raise TabularDataError(f"SQLite table or view not found: {self.table}")

    def _inspect_schema_sync(self) -> TabularSchema:
        with self._connect() as connection:
            self._require_table(connection)
            rows = connection.execute(
                f"PRAGMA table_info({_quote_identifier(self.table)})"
            ).fetchall()
        if not rows:
            raise TabularDataError(f"SQLite source has no columns: {self.table}")
        return TabularSchema(
            source=f"{self.path}#{self.table}",
            columns=tuple(
                SourceColumn(
                    name=str(row[1]),
                    data_type=str(row[2] or ""),
                    nullable=not bool(row[3]) and not bool(row[5]),
                )
                for row in rows
            ),
        )

    def _read_rows_sync(
        self, mapping: BarColumnMapping, request: BarRequest
    ) -> tuple[TabularRow, ...]:
        selected = ", ".join(_quote_identifier(column) for column in mapping.columns)
        predicates: list[str] = []
        parameters: list[object] = []
        if mapping.symbol is not None:
            predicates.append(f"{_quote_identifier(mapping.symbol)} = ?")
            parameters.append(request.instrument.symbol)
        if mapping.timeframe is not None:
            predicates.append(f"{_quote_identifier(mapping.timeframe)} = ?")
            parameters.append(request.timeframe)
        bounds = numeric_source_bounds(request.start_ms, request.end_ms, self.timestamp_unit)
        if bounds is not None:
            predicates.append(f"{_quote_identifier(mapping.timestamp)} >= ?")
            parameters.append(bounds[0])
            predicates.append(f"{_quote_identifier(mapping.timestamp)} < ?")
            parameters.append(bounds[1])
        where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
        statement = (
            f"SELECT {selected} FROM {_quote_identifier(self.table)}{where} "
            f"ORDER BY {_quote_identifier(mapping.timestamp)}"
        )
        with self._connect() as connection:
            self._require_table(connection)
            rows = connection.execute(statement, parameters).fetchall()
        return tuple(dict(row) for row in rows)

    def _distinct_values_sync(self, column: str) -> tuple[object, ...]:
        statement = (
            f"SELECT DISTINCT {_quote_identifier(column)} "
            f"FROM {_quote_identifier(self.table)} "
            f"ORDER BY {_quote_identifier(column)}"
        )
        with self._connect() as connection:
            self._require_table(connection)
            rows = connection.execute(statement).fetchall()
        return tuple(row[0] for row in rows)
