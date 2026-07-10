from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pineforge_data import Bar, BarRequest, HistoricalBarProvider, Instrument


class ExampleBarProvider:
    name = "example"

    async def fetch_bars(self, request: BarRequest) -> Sequence[Bar]:
        return [
            Bar(
                request.instrument,
                request.start_ms,
                10.0,
                12.0,
                9.0,
                11.0,
                5.0,
                self.name,
            )
        ]


def test_structural_provider_protocol_needs_no_base_class() -> None:
    provider = ExampleBarProvider()
    request = BarRequest(Instrument("BTCUSD"), "1m", 1_000, 61_000)

    assert isinstance(provider, HistoricalBarProvider)
    assert asyncio.run(provider.fetch_bars(request))[0].source == "example"
