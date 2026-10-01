from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from pineforge_data import (
    FxMacroDataAccessWarning,
    FxMacroDataDataError,
    FxMacroDataHTTPError,
    FxMacroDataProvider,
    FxMacroDataResponse,
    MacroDataProvider,
    MacroObservation,
    MacroRequest,
)
from pineforge_data.providers.fxmacrodata import UrllibTransport

FIXTURE = Path(__file__).parent / "fixtures" / "fxmacrodata_usd_non_farm_payrolls.json"
START_MS = 1_775_001_600_000  # 2026-04-01
END_MS = 1_790_812_800_000  # 2026-10-01


def fixture_pages() -> list[object]:
    pages: list[object] = json.loads(FIXTURE.read_text(encoding="utf-8"))["pages"]
    return pages


class FakeTransport:
    def __init__(self, responses: list[FxMacroDataResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str | int], dict[str, str]]] = []

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
    ) -> FxMacroDataResponse:
        self.calls.append((url, dict(params), dict(headers)))
        return self._responses.pop(0)


def ok(payload: object) -> FxMacroDataResponse:
    return FxMacroDataResponse(200, payload)


def fetch(
    provider: FxMacroDataProvider, start_ms: int = START_MS, end_ms: int = END_MS
) -> list[MacroObservation]:
    request = MacroRequest(
        key="non_farm_payrolls", currency="usd", start_ms=start_ms, end_ms=end_ms
    )
    return list(asyncio.run(provider.fetch_observations(request)))


def test_provider_implements_macro_protocol() -> None:
    assert isinstance(FxMacroDataProvider(transport=FakeTransport([])), MacroDataProvider)


def test_fixture_pages_become_release_and_vintage_observations() -> None:
    transport = FakeTransport([ok(page) for page in fixture_pages()])
    provider = FxMacroDataProvider(transport=transport)

    observations = fetch(provider)

    assert [
        (o.period_end_ms, o.released_at_ms, o.vintage_at_ms, o.value) for o in observations
    ] == [
        # June: no revisions, one vintage at the release time
        (1_782_777_600_000, 1_783_513_800_000, 1_783_513_800_000, 158_936_000.0),
        # July: first print at release, the revised value only from when it was observed
        (1_785_456_000_000, 1_786_105_800_000, 1_786_105_800_000, 158_858_000.0),
        (1_785_456_000_000, 1_786_105_800_000, 1_789_364_950_289, 158_913_000.0),
        # August: the snapshot repeats the first print, so it adds no vintage
        (1_788_134_400_000, 1_788_525_000_000, 1_788_525_000_000, 159_075_000.0),
    ]
    assert {(o.key, o.currency, o.unit, o.source) for o in observations} == {
        ("non_farm_payrolls", "USD", "Persons", "fxmacrodata:BLS")
    }
    assert provider.last_skipped == {"unknown_release_time": 1, "assumed_release_time": 1}

    first_url, first_params, _ = transport.calls[0]
    assert first_url == "https://api.fxmacrodata.com/v1/announcements/USD/non_farm_payrolls"
    assert first_params == {
        "start_date": "2026-04-01",
        "end_date": "2026-09-30",
        "limit": 100,
        "offset": 0,
        "revisions": "all",
    }
    assert transport.calls[1][1]["offset"] == 2


def test_missing_release_time_is_never_fabricated() -> None:
    page = {
        "data": [
            {
                "date": "2026-05-31",
                "val": 1.0,
                "announcement_datetime": None,
                "official_planned_release_datetime": 1_780_000_000,
                "publication_time_status": "unknown",
            }
        ]
    }
    provider = FxMacroDataProvider(transport=FakeTransport([ok(page)]))

    assert fetch(provider) == []
    assert provider.last_skipped == {"unknown_release_time": 1}


def test_assumed_release_times_are_opt_in() -> None:
    pages = fixture_pages()
    provider = FxMacroDataProvider(
        include_assumed_release_times=True,
        transport=FakeTransport([ok(page) for page in pages]),
    )

    observations = fetch(provider)

    assert (1_777_507_200_000, 1_778_247_000_000, 158_700_000.0) in {
        (o.period_end_ms, o.released_at_ms, o.value) for o in observations
    }
    assert provider.last_skipped == {"unknown_release_time": 1}


def test_snapshot_without_observation_time_is_skipped() -> None:
    page = {
        "data": [
            {
                "date": "2026-07-31",
                "val": 2.0,
                "announcement_datetime": 1_786_105_800,
                "revisions": [
                    {"epoch": 1_786_105_800, "val": 1.0, "vintage_status": "source_vintage"},
                    {"epoch": 1_786_105_800, "val": 2.0, "vintage_status": "captured_snapshot"},
                ],
            }
        ]
    }
    provider = FxMacroDataProvider(transport=FakeTransport([ok(page)]))

    assert [o.value for o in fetch(provider)] == [1.0]
    assert provider.last_skipped == {"revision_without_vintage_time": 1}


def test_release_before_period_end_is_skipped_not_reordered() -> None:
    page = {"data": [{"date": "2026-07-31", "val": 50.1, "announcement_datetime": 1_785_000_000}]}
    provider = FxMacroDataProvider(transport=FakeTransport([ok(page)]))

    assert fetch(provider) == []
    assert provider.last_skipped == {"released_before_period_end": 1}


def test_periods_outside_the_request_are_dropped() -> None:
    pages = fixture_pages()
    provider = FxMacroDataProvider(transport=FakeTransport([ok(page) for page in pages]))

    observations = fetch(provider, start_ms=1_785_456_000_000, end_ms=1_788_134_400_000)

    assert {o.period_end_ms for o in observations} == {1_785_456_000_000}


def test_api_key_is_sent_only_in_header() -> None:
    transport = FakeTransport([ok({"data": []})])
    provider = FxMacroDataProvider(api_key="placeholder-key", transport=transport)

    fetch(provider)

    url, params, headers = transport.calls[0]
    assert headers["X-API-Key"] == "placeholder-key"
    assert "placeholder-key" not in url
    assert "placeholder-key" not in json.dumps(params)
    assert "placeholder-key" not in repr(provider)


def test_keyless_requests_send_no_key_header() -> None:
    transport = FakeTransport([ok({"data": []})])

    fetch(FxMacroDataProvider(transport=transport))

    assert "X-API-Key" not in transport.calls[0][2]


def test_keyless_history_window_warns() -> None:
    page = {
        "freemium_window": {"applied": True, "max_days": 90, "cutoff_date": "2026-07-03"},
        "data": [],
    }
    provider = FxMacroDataProvider(transport=FakeTransport([ok(page)]))

    with pytest.warns(FxMacroDataAccessWarning, match="only from 2026-07-03"):
        fetch(provider)


def test_retries_rate_limits_then_raises_http_errors() -> None:
    transport = FakeTransport(
        [
            FxMacroDataResponse(429, {"detail": "slow down"}, retry_after_seconds=0.0),
            ok({"data": []}),
        ]
    )
    assert fetch(FxMacroDataProvider(transport=transport)) == []
    assert len(transport.calls) == 2

    denied = FakeTransport([FxMacroDataResponse(403, {"detail": "subscription required"})])
    with pytest.raises(FxMacroDataHTTPError, match="HTTP 403: subscription required") as exc:
        fetch(FxMacroDataProvider(api_key="placeholder-key", transport=denied))
    assert exc.value.status == 403
    assert "placeholder-key" not in str(exc.value)

    exhausted = FakeTransport(
        [FxMacroDataResponse(503, None, retry_after_seconds=0.0) for _ in range(2)]
    )
    with pytest.raises(FxMacroDataHTTPError, match="HTTP 503"):
        fetch(FxMacroDataProvider(max_retries=1, transport=exhausted))


def test_pagination_must_advance() -> None:
    page = {
        "pagination": {"has_more": True, "next_offset": 0},
        "data": [{"date": "2026-07-31", "val": 1.0, "announcement_datetime": 1_786_105_800}],
    }
    provider = FxMacroDataProvider(transport=FakeTransport([ok(page)]))

    with pytest.raises(FxMacroDataDataError, match="advance"):
        fetch(provider)


def test_malformed_records_raise() -> None:
    page = {"data": [{"date": "July 2026", "val": 1.0, "announcement_datetime": 1_786_105_800}]}
    provider = FxMacroDataProvider(transport=FakeTransport([ok(page)]))

    with pytest.raises(FxMacroDataDataError, match=r"record\.date"):
        fetch(provider)


def test_constructor_validation() -> None:
    with pytest.raises(ValueError, match="api_key"):
        FxMacroDataProvider(api_key=" ")
    with pytest.raises(ValueError, match="timeout_seconds"):
        FxMacroDataProvider(timeout_seconds=0)
    with pytest.raises(ValueError, match="max_retries"):
        FxMacroDataProvider(max_retries=-1)


def test_empty_revisions_fall_back_to_the_record_value() -> None:
    page = {
        "data": [
            {
                "date": "2026-07-31",
                "val": 4.1,
                "announcement_datetime": 1_786_105_800,
                "revisions": [],
            }
        ]
    }
    provider = FxMacroDataProvider(transport=FakeTransport([ok(page)]))

    [observation] = fetch(provider)

    assert (observation.vintage_at_ms, observation.value) == (1_786_105_800_000, 4.1)
    assert provider.last_skipped == {}


def test_legacy_snapshot_is_not_dated_by_its_epoch() -> None:
    page = {
        "data": [
            {
                "date": "2026-07-31",
                "val": 2.0,
                "announcement_datetime": 1_786_105_800,
                "revisions": [
                    {"epoch": 1_786_105_800, "val": 1.0},
                    {"epoch": 1_786_105_800, "val": 2.0, "vintage_status": "legacy_snapshot"},
                    {
                        "epoch": 1_786_105_800,
                        "val": 3.0,
                        "vintage_status": "legacy_snapshot",
                        "observed_at_ns": 1_789_000_000_000_000_000,
                    },
                ],
            }
        ]
    }
    provider = FxMacroDataProvider(transport=FakeTransport([ok(page)]))

    assert [(o.vintage_at_ms, o.value) for o in fetch(provider)] == [
        (1_786_105_800_000, 1.0),
        (1_789_000_000_000, 3.0),
    ]
    assert provider.last_skipped == {"revision_without_vintage_time": 1}


def test_page_ceiling_raises() -> None:
    page = {
        "pagination": {"has_more": True},
        "data": [{"date": "2026-07-31", "val": 1.0, "announcement_datetime": 1_786_105_800}],
    }
    pages = [ok({**page, "pagination": {"has_more": True, "next_offset": n}}) for n in (1, 2, 3)]
    provider = FxMacroDataProvider(max_pages=2, transport=FakeTransport(pages))

    with pytest.raises(FxMacroDataDataError, match="max_pages=2"):
        fetch(provider)


def test_long_retry_after_falls_back_to_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    transport = FakeTransport(
        [
            FxMacroDataResponse(429, None, retry_after_seconds=86_400.0),
            FxMacroDataResponse(503, None, retry_after_seconds=5.0),
            ok({"data": []}),
        ]
    )

    fetch(FxMacroDataProvider(retry_backoff_seconds=0.5, transport=transport))

    assert delays == [0.5, 5.0]


def test_key_is_redacted_from_server_error_text() -> None:
    transport = FakeTransport([FxMacroDataResponse(401, {"detail": "invalid key placeholder-key"})])

    with pytest.raises(FxMacroDataHTTPError) as exc:
        fetch(FxMacroDataProvider(api_key="placeholder-key", transport=transport))

    assert "placeholder-key" not in str(exc.value)
    assert "[redacted]" in str(exc.value)


@pytest.mark.parametrize(
    ("currency", "key"),
    [("US/D", "inflation"), ("USD", "../latest"), ("USD", "a/b"), ("USDX", "inflation")],
)
def test_currency_and_key_must_be_plain_identifiers(currency: str, key: str) -> None:
    transport = FakeTransport([])
    provider = FxMacroDataProvider(transport=transport)
    request = MacroRequest(key=key, currency=currency, start_ms=START_MS, end_ms=END_MS)

    with pytest.raises(ValueError):
        asyncio.run(provider.fetch_observations(request))
    assert transport.calls == []


def test_withheld_releases_warn() -> None:
    page = {"freemium_delay": {"applied": True, "withheld_count": 2}, "data": []}
    provider = FxMacroDataProvider(transport=FakeTransport([ok(page)]))

    with pytest.warns(FxMacroDataAccessWarning, match="2 release"):
        fetch(provider)


class _Handler(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, dict[str, str]]]] = []

    def log_message(self, format: str, *args: object) -> None:
        return None

    def _send(self, status: int, body: bytes, headers: Mapping[str, str]) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        _Handler.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        if self.path.startswith("/v1/ok"):
            self._send(200, b'{"data": [1]}', {"Content-Type": "application/json"})
        elif self.path.startswith("/v1/moved"):
            self._send(302, b"", {"Location": "/elsewhere"})
        elif self.path.startswith("/v1/busy"):
            self._send(429, b'{"detail": "busy"}', {"Retry-After": "7"})
        elif self.path.startswith("/v1/text"):
            self._send(200, b"not json", {"Content-Type": "text/plain"})
        else:
            self._send(404, b"{}", {})


@pytest.fixture
def local_server() -> Iterator[str]:
    _Handler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def test_urllib_transport_sends_headers_and_decodes_json(local_server: str) -> None:
    transport = UrllibTransport(timeout_seconds=5)

    response = asyncio.run(
        transport.get(
            f"{local_server}/ok",
            params={"limit": 100, "offset": 0},
            headers={"X-API-Key": "placeholder-key", "Accept": "application/json"},
        )
    )

    assert (response.status, response.payload) == (200, {"data": [1]})
    path, headers = _Handler.seen[0]
    assert path == "/v1/ok?limit=100&offset=0"
    assert headers["x-api-key"] == "placeholder-key"


def test_urllib_transport_refuses_redirects(local_server: str) -> None:
    provider = FxMacroDataProvider(api_key="placeholder-key", api_url=f"{local_server}/moved")
    request = MacroRequest(key="inflation", currency="USD", start_ms=START_MS, end_ms=END_MS)

    with pytest.raises(FxMacroDataHTTPError, match="HTTP 302"):
        asyncio.run(provider.fetch_observations(request))
    assert [path.split("?")[0] for path, _ in _Handler.seen] == [
        "/v1/moved/announcements/USD/inflation"
    ]


def test_urllib_transport_reads_retry_after_and_tolerates_non_json(local_server: str) -> None:
    transport = UrllibTransport(timeout_seconds=5)

    busy = asyncio.run(transport.get(f"{local_server}/busy", params={}, headers={}))
    text = asyncio.run(transport.get(f"{local_server}/text", params={}, headers={}))

    assert (busy.status, busy.payload, busy.retry_after_seconds) == (429, {"detail": "busy"}, 7.0)
    assert (text.status, text.payload) == (200, None)
