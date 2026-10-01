"""FXMacroData adapter for release- and vintage-aware macro observations."""

from __future__ import annotations

import asyncio
import json
import re
import warnings
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from http.client import HTTPMessage
from math import isfinite
from typing import IO, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..models import MacroObservation
from ..requests import MacroRequest

FXMACRODATA_API_URL = "https://api.fxmacrodata.com/v1"
_PAGE_LIMIT = 100
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Snapshot entries repeat the original release time in ``epoch``; only the time
# FXMacroData observed them says when their value was available.
_SNAPSHOT_STATUSES = frozenset({"captured_snapshot", "legacy_snapshot"})
_CURRENCY = re.compile(r"[A-Z]{3}")
_INDICATOR = re.compile(r"[a-z0-9_]+")


class FxMacroDataError(RuntimeError):
    """Base error raised by the FXMacroData macro provider."""


class FxMacroDataHTTPError(FxMacroDataError):
    """FXMacroData answered with a non-success HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"FXMacroData returned HTTP {status}: {message}")
        self.status = status


class FxMacroDataDataError(FxMacroDataError):
    """An FXMacroData response cannot be normalized safely."""


class FxMacroDataAccessWarning(UserWarning):
    """The response was limited by the keyless access tier."""


@dataclass(frozen=True, slots=True)
class FxMacroDataResponse:
    """Status, decoded JSON body, and optional ``Retry-After`` delay of one response."""

    status: int
    payload: object
    retry_after_seconds: float | None = None


class FxMacroDataTransport(Protocol):
    """Async GET transport; inject one for offline tests or a custom HTTP stack."""

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
    ) -> FxMacroDataResponse: ...


def _retry_after(headers: Message | None) -> float | None:
    value = None if headers is None else headers.get("Retry-After")
    try:
        return None if value is None else max(0.0, float(value))
    except ValueError:
        return None


def _decode(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


class _RefuseRedirects(HTTPRedirectHandler):
    """Surface a 3xx as an HTTP error instead of re-sending headers elsewhere."""

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        return None


class UrllibTransport:
    """Standard-library transport: no optional dependency is required.

    Redirects are not followed, so the ``X-API-Key`` header is only ever sent to
    the configured host; a 3xx response becomes an ``FxMacroDataHTTPError``.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(_RefuseRedirects())

    def _get(self, url: str, headers: Mapping[str, str]) -> FxMacroDataResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                return FxMacroDataResponse(response.status, _decode(response.read()))
        except HTTPError as exc:
            return FxMacroDataResponse(exc.code, _decode(exc.read()), _retry_after(exc.headers))
        except (URLError, TimeoutError) as exc:
            raise FxMacroDataError(f"FXMacroData request failed: {exc}") from exc

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
    ) -> FxMacroDataResponse:
        query = urlencode({key: str(value) for key, value in params.items()})
        return await asyncio.to_thread(self._get, f"{url}?{query}", headers)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FxMacroDataDataError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_number(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FxMacroDataDataError(f"{field} must be numeric")
    normalized = float(value)
    if not isfinite(normalized):
        raise FxMacroDataDataError(f"{field} must be finite")
    return normalized


def _optional_epoch_ms(value: object, field: str, *, divisor: int) -> int | None:
    """Convert epoch seconds (``divisor=1``) or nanoseconds to Unix milliseconds."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FxMacroDataDataError(f"{field} must be a non-negative integer epoch")
    return value * 1_000 if divisor == 1 else value // divisor


def _period_end_ms(value: object) -> int:
    date_text = _text(value, "record.date")
    try:
        day = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise FxMacroDataDataError("record.date must be an ISO YYYY-MM-DD date") from exc
    return int(day.timestamp() * 1_000)


def _request_date(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC).date().isoformat()


def _available_at_ms(entry: Mapping[str, object], field: str) -> int | None:
    """When the value in ``entry`` is known to have been available.

    A ``captured_snapshot`` or ``legacy_snapshot`` repeats the original release
    time in ``epoch``, so using it would date a later revision back to the first
    release; only ``observed_at_ns`` is used for those. Other vintages are dated
    by ``publication_at_ns``, else ``epoch``.
    """

    if entry.get("vintage_status") in _SNAPSHOT_STATUSES:
        return _optional_epoch_ms(
            entry.get("observed_at_ns"), f"{field}.observed_at_ns", divisor=1_000_000
        )
    published = _optional_epoch_ms(
        entry.get("publication_at_ns"), f"{field}.publication_at_ns", divisor=1_000_000
    )
    if published is not None:
        return published
    return _optional_epoch_ms(entry.get("epoch"), f"{field}.epoch", divisor=1)


def _payload_records(payload: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    records = payload.get("data")
    if not isinstance(records, list):
        raise FxMacroDataDataError("response.data must be a list")
    normalized: list[Mapping[str, object]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise FxMacroDataDataError("response.data must contain only objects")
        normalized.append(record)
    return normalized


def _next_offset(payload: Mapping[str, object], current: int, returned: int) -> int | None:
    pagination = payload.get("pagination")
    if pagination is None:
        return None
    if not isinstance(pagination, Mapping):
        raise FxMacroDataDataError("response.pagination must be an object")
    if pagination.get("has_more") is not True:
        return None
    next_offset = pagination.get("next_offset", current + returned)
    if isinstance(next_offset, bool) or not isinstance(next_offset, int) or next_offset <= current:
        raise FxMacroDataDataError("response.pagination.next_offset must advance the cursor")
    return next_offset


def _error_message(payload: object) -> str:
    if isinstance(payload, Mapping):
        for field in ("detail", "message", "error"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, Mapping):
                nested = value.get("message")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return "no error message"


class FxMacroDataProvider:
    """Fetch FXMacroData announcements as ``MacroObservation`` vintages.

    ``request.key`` is an FXMacroData indicator slug (``inflation``,
    ``policy_rate``, ``non_farm_payrolls``) and ``request.currency`` a currency
    code. The provider asks for ``revisions=all`` and emits one observation per
    distinct value a period has had, dated by when that value became available.

    ``source`` is ``fxmacrodata:<publisher>``, for example ``fxmacrodata:BLS``.
    Records without a publication time are skipped rather than given one, and so
    are records whose time FXMacroData derived instead of captured, unless
    ``include_assumed_release_times`` is set. ``last_skipped`` counts the
    records and revisions left out of the most recent fetch, by reason.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_url: str = FXMACRODATA_API_URL,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        max_retry_after_seconds: float = 60.0,
        max_pages: int = 1_000,
        include_assumed_release_times: bool = False,
        transport: FxMacroDataTransport | None = None,
    ) -> None:
        if not api_url.strip():
            raise ValueError("api_url must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be non-negative")
        if max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds must be non-negative")
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        if api_key is not None and not api_key.strip():
            raise ValueError("api_key must not be empty when supplied")

        self.name = "fxmacrodata"
        self._api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_retry_after_seconds = max_retry_after_seconds
        self.max_pages = max_pages
        self.include_assumed_release_times = include_assumed_release_times
        self._transport: FxMacroDataTransport = transport or UrllibTransport(timeout_seconds)
        self.last_skipped: Mapping[str, int] = {}

    def __repr__(self) -> str:
        key_state = "set" if self._api_key else "unset"
        return f"FxMacroDataProvider(api_url={self.api_url!r}, api_key={key_state})"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "pineforge-data-fxmacrodata",
        }
        if self._api_key is not None:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _get_page(self, url: str, params: Mapping[str, str | int]) -> Mapping[str, object]:
        attempt = 0
        while True:
            response = await self._transport.get(url, params=params, headers=self._headers())
            if response.status in _RETRY_STATUSES and attempt < self.max_retries:
                delay = response.retry_after_seconds
                if delay is None or delay > self.max_retry_after_seconds:
                    delay = self.retry_backoff_seconds * 2**attempt
                await asyncio.sleep(delay)
                attempt += 1
                continue
            if not 200 <= response.status < 300:
                message = _error_message(response.payload)
                if self._api_key is not None:
                    message = message.replace(self._api_key, "[redacted]")
                raise FxMacroDataHTTPError(response.status, message)
            if not isinstance(response.payload, Mapping):
                raise FxMacroDataDataError("response must be a JSON object")
            return response.payload

    def _release_ms(self, record: Mapping[str, object], skipped: Counter[str]) -> int | None:
        released_at_ms = _optional_epoch_ms(
            record.get("announcement_datetime"), "record.announcement_datetime", divisor=1
        )
        if released_at_ms is None:
            skipped["unknown_release_time"] += 1
            return None
        assumed = (
            record.get("release_time_assumed") is True
            or record.get("publication_time_status") == "assumed_historical"
        )
        if assumed and not self.include_assumed_release_times:
            skipped["assumed_release_time"] += 1
            return None
        return released_at_ms

    @staticmethod
    def _vintages(
        record: Mapping[str, object], released_at_ms: int, skipped: Counter[str]
    ) -> list[tuple[int, float]]:
        revisions = record.get("revisions")
        if revisions is None or revisions == []:
            entries: list[tuple[str, Mapping[str, object]]] = [("record", record)]
        elif isinstance(revisions, list):
            entries = []
            for index, revision in enumerate(revisions):
                if not isinstance(revision, Mapping):
                    raise FxMacroDataDataError(f"record.revisions[{index}] must be an object")
                entries.append((f"record.revisions[{index}]", revision))
        else:
            raise FxMacroDataDataError("record.revisions must be a list when supplied")

        candidates: set[tuple[int, float]] = set()
        for field, entry in entries:
            value = _optional_number(entry.get("val"), f"{field}.val")
            if value is None:
                skipped["missing_value"] += 1
                continue
            if field == "record" and entry.get("vintage_status") not in _SNAPSHOT_STATUSES:
                available_at_ms: int | None = released_at_ms
            else:
                available_at_ms = _available_at_ms(entry, field)
            if available_at_ms is None:
                skipped["revision_without_vintage_time"] += 1
                continue
            candidates.add((max(released_at_ms, available_at_ms), value))

        # Keep a vintage only when the value differs from the one before it.
        vintages: list[tuple[int, float]] = []
        for vintage_at_ms, value in sorted(candidates):
            if vintages and vintages[-1][1] == value:
                continue
            vintages.append((vintage_at_ms, value))
        return vintages

    def _warn_on_access_limits(self, payload: Mapping[str, object], request: MacroRequest) -> None:
        window = payload.get("freemium_window")
        if isinstance(window, Mapping) and window.get("applied") is True:
            cutoff = window.get("cutoff_date")
            if isinstance(cutoff, str) and _request_date(request.start_ms) < cutoff:
                warnings.warn(
                    FxMacroDataAccessWarning(
                        f"keyless access returns {request.currency} {request.key} only from "
                        f"{cutoff}; supply an API key for earlier periods"
                    ),
                    stacklevel=3,
                )
        delay = payload.get("freemium_delay")
        if isinstance(delay, Mapping):
            withheld = delay.get("withheld_count")
            if isinstance(withheld, int) and not isinstance(withheld, bool) and withheld > 0:
                warnings.warn(
                    FxMacroDataAccessWarning(
                        f"{withheld} release(s) published in the last 15 minutes were "
                        "withheld by the keyless tier"
                    ),
                    stacklevel=3,
                )

    async def fetch_observations(self, request: MacroRequest) -> Sequence[MacroObservation]:
        """Fetch every vintage for periods in ``[start_ms, end_ms)``."""

        currency = request.currency.strip().upper()
        key = request.key.strip().lower()
        if not _CURRENCY.fullmatch(currency):
            raise ValueError(f"currency must be a three-letter code, got {request.currency!r}")
        if not _INDICATOR.fullmatch(key):
            raise ValueError(f"key must be an FXMacroData indicator slug, got {request.key!r}")
        url = f"{self.api_url}/announcements/{quote(currency, safe='')}/{quote(key, safe='')}"
        params: dict[str, str | int] = {
            "start_date": _request_date(request.start_ms),
            "end_date": _request_date(request.end_ms - 1),
            "limit": _PAGE_LIMIT,
            "offset": 0,
            "revisions": "all",
        }
        skipped: Counter[str] = Counter()
        observations: dict[tuple[int, int], MacroObservation] = {}
        first_page = True
        pages = 0

        while True:
            pages += 1
            if pages > self.max_pages:
                raise FxMacroDataDataError(
                    f"response.pagination did not finish within max_pages={self.max_pages}"
                )
            payload = await self._get_page(url, params)
            if first_page:
                self._warn_on_access_limits(payload, request)
                first_page = False
            records = _payload_records(payload)
            value_metadata = payload.get("value_metadata")
            page_unit = (
                value_metadata.get("source_unit") if isinstance(value_metadata, Mapping) else None
            )
            page_source = payload.get("source")

            for record in records:
                period_end_ms = _period_end_ms(record.get("date"))
                if not request.start_ms <= period_end_ms < request.end_ms:
                    continue
                released_at_ms = self._release_ms(record, skipped)
                if released_at_ms is None:
                    continue
                if released_at_ms < period_end_ms:
                    # e.g. a flash estimate published before the period closed
                    skipped["released_before_period_end"] += 1
                    continue
                record_unit = record.get("unit", page_unit)
                unit = "unknown" if record_unit is None else _text(record_unit, "unit")
                record_source = record.get("source", page_source)
                source = (
                    self.name
                    if record_source is None
                    else f"{self.name}:{_text(record_source, 'source')}"
                )
                for vintage_at_ms, value in self._vintages(record, released_at_ms, skipped):
                    observations[(period_end_ms, vintage_at_ms)] = MacroObservation(
                        key=key,
                        currency=currency,
                        period_end_ms=period_end_ms,
                        released_at_ms=released_at_ms,
                        vintage_at_ms=vintage_at_ms,
                        value=value,
                        unit=unit,
                        source=source,
                    )

            offset = int(params["offset"])
            next_offset = _next_offset(payload, offset, len(records))
            if next_offset is None or not records:
                break
            params["offset"] = next_offset

        self.last_skipped = dict(skipped)
        return sorted(
            observations.values(),
            key=lambda observation: (observation.period_end_ms, observation.vintage_at_ms),
        )

    async def close(self) -> None:
        """Release resources; the default transport holds no open connection."""

    async def __aenter__(self) -> FxMacroDataProvider:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
