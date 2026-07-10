"""SQLAlchemy Core provider for reflected user-owned database tables."""

from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from threading import Lock
from types import ModuleType
from typing import Any, cast

from ..models import Instrument
from ..requests import BarRequest
from .tabular import (
    BarColumnMapping,
    ColumnMappingInput,
    SourceColumn,
    TabularBarProvider,
    TabularRow,
    TabularSchema,
    TimestampUnit,
    numeric_source_bounds,
)


class SqlAlchemyDependencyError(RuntimeError):
    """SQLAlchemy support was requested without the optional dependency."""


def _load_sqlalchemy() -> ModuleType:
    try:
        return import_module("sqlalchemy")
    except ModuleNotFoundError as exc:
        if exc.name == "sqlalchemy" or (exc.name and exc.name.startswith("sqlalchemy.")):
            raise SqlAlchemyDependencyError(
                "SQLAlchemy is not installed; install pineforge-data[database]"
            ) from exc
        raise


class SqlAlchemyBarProvider(TabularBarProvider):
    """Reflect and query one table or view through a synchronous SQLAlchemy engine."""

    def __init__(
        self,
        url: str,
        table: str,
        *,
        venue: str = "database",
        schema: str | None = None,
        mapping: ColumnMappingInput = None,
        timestamp_unit: TimestampUnit | str = TimestampUnit.MILLISECONDS,
        timestamp_timezone: str = "UTC",
        instrument: Instrument | None = None,
        timeframe: str | None = None,
        engine_options: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            venue=venue,
            mapping=mapping,
            timestamp_unit=timestamp_unit,
            timestamp_timezone=timestamp_timezone,
            instrument=instrument,
            timeframe=timeframe,
        )
        if not url.strip():
            raise ValueError("SQLAlchemy URL must not be empty")
        if not table:
            raise ValueError("table must not be empty")
        if schema is not None and not schema:
            raise ValueError("schema must be None or a non-empty string")

        self._sa = _load_sqlalchemy()
        options = dict(engine_options or {})
        options.setdefault("hide_parameters", True)
        self._engine: Any = self._sa.create_engine(url, **options)
        self._table: Any | None = None
        self._reflection_lock = Lock()
        self.table = table
        self.schema_name = schema
        self.name = f"sqlalchemy:{venue}"

    def _reflected_table(self) -> Any:
        with self._reflection_lock:
            if self._table is None:
                metadata = self._sa.MetaData()
                self._table = self._sa.Table(
                    self.table,
                    metadata,
                    schema=self.schema_name,
                    autoload_with=self._engine,
                )
            return self._table

    def _inspect_schema_sync(self) -> TabularSchema:
        table = self._reflected_table()
        qualified_table = f"{self.schema_name}.{self.table}" if self.schema_name else self.table
        return TabularSchema(
            source=f"sqlalchemy:{self.venue}#{qualified_table}",
            columns=tuple(
                SourceColumn(
                    name=str(column.name),
                    data_type=str(column.type),
                    nullable=bool(column.nullable),
                )
                for column in table.columns
            ),
        )

    def _read_rows_sync(
        self, mapping: BarColumnMapping, request: BarRequest
    ) -> tuple[TabularRow, ...]:
        table = self._reflected_table()
        columns = [table.c[column] for column in mapping.columns]
        statement = self._sa.select(*columns)
        if mapping.symbol is not None:
            statement = statement.where(table.c[mapping.symbol] == request.instrument.symbol)
        if mapping.timeframe is not None:
            statement = statement.where(table.c[mapping.timeframe] == request.timeframe)
        bounds = numeric_source_bounds(request.start_ms, request.end_ms, self.timestamp_unit)
        if bounds is not None:
            statement = statement.where(table.c[mapping.timestamp] >= bounds[0])
            statement = statement.where(table.c[mapping.timestamp] < bounds[1])
        statement = statement.order_by(table.c[mapping.timestamp])

        with self._engine.connect() as connection:
            result = connection.execute(statement).mappings().all()
        return tuple(
            cast(TabularRow, {str(key): value for key, value in row.items()}) for row in result
        )

    def _distinct_values_sync(self, column: str) -> tuple[object, ...]:
        table = self._reflected_table()
        statement = self._sa.select(table.c[column]).distinct().order_by(table.c[column])
        with self._engine.connect() as connection:
            return tuple(connection.execute(statement).scalars().all())

    def _close_sync(self) -> None:
        self._engine.dispose()
