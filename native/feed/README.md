# pineforge-feed

An independent Apache-2.0 C++17 public-market-data adapter. It produces the
native `pineforge-live` runner's JSONL protocol; it does not load strategies,
place orders, access accounts, consume fills, or accept API keys. It is not part
of the Python package or wheel. This first release supports **Binance spot**,
`warmup` and `run`, with `bars` and raw `ticks`. There is no `serve` subcommand.

## Build

Requirements: CMake >= 3.20, a C++17 compiler, OpenSSL Crypto, threads, and
**libcurl >= 8.14.1 with both `ws` and `wss`**. Configure executes a linked
feature check, and every running binary checks the actually loaded libcurl.
Cross-compilation without an executable feature check is not qualified.
`vendor/json.hpp` is SHA-256 pinned; attribution and dependency pins are in
`NOTICE`. No engine linkage or additional C++ dependency is required.

```sh
cmake -S native/feed -B build-feed -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCURL_DIR=/path/to/qualified-curl/lib/cmake/CURL
cmake --build build-feed -j4
ctest --test-dir build-feed --output-on-failure
```

`tests/build_ci_curl.sh PREFIX` builds the CI-pinned curl with WebSockets and
OpenSSL. The isolated `.github/workflows/native-feed.yml` defines Linux and
macOS Release builds, ASan+UBSan, and a separate Linux TSan build. Python and
the existing Python workflow are unaffected. Python 3.9+ is used only for the
synthetic mock tests; public qualification uses Python 3.10+.

## A verified warmup cut

```sh
build-feed/pineforge-feed warmup --venue binance --market spot --symbol BTCUSDT \
  --start 2026-10-03T00:00:00Z --end 2026-10-03T03:20:00Z --output warmup.csv
build-feed/pineforge-feed run --venue binance --market spot --symbol BTCUSDT \
  --mode bars --state-dir feed-state --start 2026-10-03T03:20:00Z |
  pineforge-live run --strategy strategy.so --warmup warmup.csv --mode bars \
    --script-tf 1 --feed - --ledger runner.sqlite3 --symbol BINANCE:BTCUSDT \
    --webhook-url https://receiver.example/actions
```

Times are minute-aligned Unix milliseconds or strict UTC RFC3339 seconds.
Warmup fetches contiguous REST 1m klines only behind an observed `x=true` WS
watermark. Its end is **exclusive** and must be the live start. It writes the
runner CSV columns `timestamp,open,high,low,close,volume` and an atomic
`warmup.csv.manifest.json` containing source, symbol, units, start, exclusive
cut, confirmation watermark, row count, and the CSV SHA-256. It refuses to
overwrite either output. Keep both files together; an interrupted two-file
publication without its manifest is not a qualified warmup.

## Protocol and proof policy

- Bars are `{"type":"bar","bar":{"ts_open":...,"o":...,"h":...,"l":...,"c":...,"v":...}}`.
- Raw ticks are `{"type":"tick","ts":...,"seq":...,"price":...,"qty":...}`.
- Completeness is `{"type":"time","ts":...}`: complete **strictly before**
  that timestamp. It is never a heartbeat or a wall-clock assertion.
- Every message contains one event. No batches, control messages, diagnostics,
  or status objects are sent on stdout. These immutable boundaries are the
  runner's `--from-input` counting unit.

Venue decimal string tokens become JSON number tokens unchanged, including
trailing zeros. Fixed-point string arithmetic reconciles OHLCV; no price or
quantity enters a binary float. Bounds are 96 token characters, 32 fractional
digits, and 128 arithmetic digits. Invalid/zero tick quantities fail closed.

The combined stream uses raw `t` as the positive sequence and matched `T` as
time, not dispatch `E`. The real predecessor fetched from REST proves the
initial cut; aggregates locate a raw start ID but are never emitted or expanded.
Each new raw ID must be exactly its predecessor plus one, with nonregressing
matched time. Holes/reordering heal through inclusive `historicalTrades`
`fromId` pages (maximum 1000); identical duplicates are discarded and conflicting
duplicates stop. There is no assumed all-time retention window.

For the observed minute, `x=true`, the complete `f..L` range, `n`, and exact
OHLCV must all reconcile before `time = open + 60000`. Later-minute prints stay
buffered until that proof. A missed historical minute can be closed only behind
a later observed `x=true` watermark, with a fetched contiguous raw prefix, an
independently fetched next-minute raw-ID fence, and exact REST `n`/OHLCV. This
uses the approved historical-watermark policy, not a fabricated WS confirmation.
Empty/ambiguous tick minutes deliberately stop rather than invent a fence.
Bars heal missing minutes from REST only behind the verified closed watermark.
Any discovered already-emitted bar revision stops; history is never rewritten.

Curl answers server PING with matching-payload PONG. Reconnect occurs on
`serverShutdown`, transport loss/75-second source silence, or at 23h55 (before
the venue's 24-hour limit). The reader buffers during REST healing. Every new
connection re-fetches up to 1000 raw prints and 32 closed-bar proofs, requires
exact token/ID/time overlap, then continues the same stream epoch and sequence.
An altered duplicate discovered outside that window is checked against the
full retained normalized journal. Older, undiscovered revisions are not claimed
to be continuously polled.

REST routes are restricted to public `exchangeInfo`, `historicalTrades`,
`aggTrades`, and `klines`. Current public weight/raw-request limits are read
from `exchangeInfo`, combined with conservative pacing, observed used-weight
headers, shared-quota pauses, and `429 Retry-After`. A ban/access rejection is
not retried as anonymous trading access. TLS verification is always enabled;
redirects, URL credentials, and non-origin overrides are refused. Explicit
insecure overrides are **loopback-only**, for tests.

## Durability and restart

One producer owns a state directory via `flock`. `events.jsonl` is append-only;
each message is fsynced before `cursor.json` is atomically replaced and its
directory fsynced, **before** stdout publication. The cursor binds source
origins, symbol/mode/units, random stream epoch, initial/verified cut, venue ID,
emitted sequence, message index, byte count, predecessor, closed-minute proofs,
SHA-256 prefix chain, last-message hash, and fixed message partition. Its
canonical document is itself hashed. Resume verifies the retained committed
prefix and fsyncs truncation of an uncommitted crash tail.

A pipe write is not a consumer acknowledgment. By default `--resume` replays the
whole verified origin prefix. If the runner has committed N messages:

```sh
build-feed/pineforge-feed run --venue binance --market spot --symbol BTCUSDT \
  --mode bars --state-dir feed-state --resume --output-from N |
  pineforge-live run --strategy strategy.so --warmup warmup.csv --mode bars \
    --script-tf 1 --feed - --ledger runner.sqlite3 --from-input N \
    --symbol BINANCE:BTCUSDT --webhook-url https://receiver.example/actions
```

Replace N with the runner's actual committed count; never use the producer's
possibly-ahead count. Bind warmup identity and stream epoch in orchestration.
An unavailable cursor or changed identity is refused. SIGTERM/SIGINT stops
intake, finishes a durable current message or leaves a rollback tail, and exits
0. Nonblocking stdout has a five-second stop drain; a consumer that has not
acknowledged a committed message must replay it. Stderr is structured JSON and
nonblocking; a stalled diagnostics sink can lose logs, not stall shutdown.

| Exit | Meaning |
|---|---|
| 0 | Requested clean stop / successful command |
| 20 | Unhealable history gap, ambiguous fence, exhausted source retries |
| 21 | Changed overlap, conflicting duplicate, revised bar, changed cursor/prefix |
| 22 | Replay/disk/queue/allocation budget or durable I/O failure |
| 23 | Unsupported input/source, permanent access, protocol/decimal/dependency failure |

Default replay disk budget: 256 MiB (`--max-log-bytes`); each prefix traversal:
60 seconds (`--max-replay-seconds`, maximum 3600); source and unconfirmed-print
queues: 16 MiB each (`--max-queue-bytes`); WS text: 1 MiB; REST body: 4 MiB.
Warmup: 100000 rows / 16 MiB. Exhaustion is a cursor-preserving stop, not silent
compaction or dropped events. Raising limits is an explicit operator choice.
`--max-messages` permits bounded qualification. `--reconnect-seconds` can shorten
the 23h55 rotation for synthetic tests, never extend it beyond that limit.

## Qualification and next adapters

`tests/unit.cpp` and `tests/mock_venue.py` contain **synthetic** documented
Binance message shapes only: holes, reordering, duplicates, revisions, access,
reconnect overlap, fragmented text, PING/PONG, retirement, budgets, and recovery.
Never commit public exchange captures. `tests/public_e2e.py` explicitly opts
into public BTCUSDT capture, local receiver delivery, a hard-kill restart,
exact REST verification, bar batch action parity through `run_backtest_full`,
and tick same-print replay determinism. Tick-vs-OHLC-only fills are not assumed
equivalent: the harness exits nonzero for that comparison unless the operator
explicitly passes `--allow-tick-ohlc-difference`; even then it retains the FAIL
receipt and false parity result. Its batch observer is linked separately from the read-only engine's
`tests/native_live_equivalence_observer.cpp`; it is not a feed dependency.

`src/venue.hpp` is the adapter boundary. Later slices add OKX raw ticks/bars
(contract-unit conversion where needed), Bybit confirmed bars **without ticks**,
and USD-M confirmed bars / separately opted-in aggregate-print ticks. Coinbase
remains deferred. `serve` will add approved MIT CivetWeb only to its server
target, bounded client replay/queues, and cursor-bound fan-out; no server library
is linked in this slice. The engine executable remains `pineforge-live`.
