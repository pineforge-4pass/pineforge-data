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
OpenSSL. The isolated `.github/workflows/native-feed.yml` defines Linux amd64,
Linux arm64 and macOS Release builds, ASan+UBSan, and a separate Linux TSan
build. The Python sdist excludes `native/` and the Python CI asserts it; the
wheel is unchanged. Python 3.9+ is used only for the synthetic mock tests;
public qualification uses Python 3.10+.

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

Bars mode subscribes to `kline_1m` only; ticks mode adds the raw `trade`
stream. Raw `t` is the positive sequence and matched `T` is time, not dispatch
`E`. The real predecessor fetched from REST proves the initial cut; aggregates
locate a raw start ID but are never emitted or expanded. Each new raw ID must be
exactly its predecessor plus one; a matched time that goes backwards along that
chain stops with 23. Holes/reordering heal through inclusive `historicalTrades`
`fromId` pages (maximum 1000); identical duplicates are discarded and
conflicting duplicates stop. There is no assumed all-time retention window.

For the observed minute, `x=true`, the complete `f..L` range, `n`, and exact
OHLCV must all reconcile before `time = open + 60000`. Later-minute prints stay
buffered until that proof. A tick minute whose own `x=true` kline was missed
(it fell inside a reconnect or a catch-up) closes only on this proof:

1. a later observed `x=true` watermark;
2. the contiguous raw-ID chain from the proven predecessor;
3. the first print of a later minute, fetched by ID, with ID exactly the
   minute's last ID plus one (the fence);
4. the REST kline's exact `n` and OHLCV.

A REST kline has no `x=true` and no `f..L`, and the feed does not fill them in.
Empty/ambiguous tick minutes deliberately stop rather than invent a fence.
Bars heal missing minutes from REST only behind the verified closed watermark.
Any discovered already-emitted bar revision stops; history is never rewritten.

The subscribed data streams are strict: another event type on them, a
malformed trade or kline, or an unknown shape stops with 23. An event outside
the data streams with an unknown type (a new venue notice) is logged as a
structured `unknown_stream_event` warning and ignored; `serverShutdown`
reconnects.

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
from `exchangeInfo`, and requests are spaced to spend at most half of each
published ceiling (about 50% headroom; 20 ms per weight at 6000 per minute, so
500 ms per 1000-print page). The venue's `X-MBX-USED-WEIGHT-1M` header counts
every client on the IP: once it shows half of the 1-minute ceiling, the feed
waits for the next window. `429` honours `Retry-After` as delta-seconds or an
IMF-fixdate HTTP-date; a wait above 24 hours stops with 22 and an unparseable
value with 23. A ban/access rejection is not retried as anonymous trading
access. TLS verification is always enabled; redirects, URL credentials, and
non-origin overrides are refused. Explicit insecure overrides are
**loopback-only**, for tests.

## Durability and restart

One producer owns a state directory via `flock`. `events.jsonl` is append-only.
Messages are group-committed: every event made ready by the source messages
already queued (up to 1024 source messages, or 4096 events) is appended in one
write and fsynced once, then `cursor.json` is atomically replaced once and its
directory fsynced, and only **then** are those lines written to stdout, one
event per line as before. The cursor binds source origins, symbol/mode/units,
random stream epoch, initial/verified cut, venue ID, emitted sequence, message
index, byte count, predecessor, closed-minute proofs, SHA-256 prefix chain,
last-message hash, and fixed message partition. Its canonical document is
itself hashed. Resume verifies the retained committed prefix and fsyncs
truncation of an uncommitted crash tail.

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
intake, discards staged messages that were never committed or published, and
exits 0. A directory holding only an empty `events.jsonl` (a crash before the
first cursor write) is taken over by a fresh `--start`; other existing state
needs `--resume`.

Stdout is nonblocking during `run` for a five-second stop drain, and its
original flags are restored on exit (a SIGKILL cannot restore them); a consumer
that has not acknowledged a committed message must replay it. Stderr's flags
are never changed: one writer thread writes whole JSON records of at most
`PIPE_BUF` bytes, so a pipe write is atomic and a record is never cut. A full
sink drops whole records, the next record reports `dropped_records`, and exit
waits at most 2 seconds for queued records.

| Exit | Meaning |
|---|---|
| 0 | Requested clean stop / successful command |
| 20 | Unhealable history gap, ambiguous fence, exhausted source retries |
| 21 | Changed overlap, conflicting duplicate, revised bar, changed cursor/prefix |
| 22 | Replay/disk/queue/allocation budget or durable I/O failure |
| 23 | Unsupported input/source, permanent access, protocol/decimal/dependency failure, matched-time regression |

Default replay disk budget: 256 MiB (`--max-log-bytes`); each prefix traversal:
60 seconds (`--max-replay-seconds`, maximum 3600); source and unconfirmed-print
queues: 16 MiB each (`--max-queue-bytes`); WS text: 1 MiB; REST body: 1 MiB.
Warmup: 100000 rows / 16 MiB. Exhaustion is a cursor-preserving stop, not silent
compaction or dropped events. Raising limits is an explicit operator choice.
`--max-messages` permits bounded qualification. `--reconnect-seconds` can shorten
the 23h55 rotation for synthetic tests, never extend it beyond that limit.

### Throughput

Storage sets the ceiling, so measure on the target disk. On a shared 16-vCPU
Linux test machine (ext4 root), committing each message separately (three
fsyncs per print) sustained about 120 messages/s. With group commit, the
loopback bench (synthetic contiguous prints through the real binary and the
mock venue, wall time including startup) ran 30,000 prints at 5,811, 11,212 and
10,842 messages/s in three runs on the same machine. The busiest BTCUSDT minute
of a recent 89-day sample averaged 655 prints/s, about one ninth of the slowest
run.

### Run-time horizon per mode

`--max-log-bytes` is a hard stop (22): this release neither rotates nor
checkpoints the journal. Journal segmentation and a checkpoint (prefix hash,
cut, sequence, current-minute aggregate, the 32 proofs) are planned together
with bounded replay windows. Until then the horizon is the budget divided by
the journal's growth:

| Mode | Bytes per message | Messages | Horizon at the default 256 MiB |
|---|---|---|---|
| bars | about 138 | 1 per minute | about 3.7 years |
| ticks, BTCUSDT median day | about 92 | 2.92 M per day | about 24 hours |
| ticks, BTCUSDT busiest day | about 92 | 8.93 M per day | about 8 hours |

Message sizes come from a public BTCUSDT capture; the daily print counts are
REST kline trade counts over 89 days.

Raising the budget lengthens every whole-journal scan: at startup, on every
tick-mode reconnect, on a duplicate closed kline and on an old duplicate print,
each bounded by `--max-replay-seconds`.

### Tick mode needs a liquid symbol

75 seconds without any text frame forces a reconnect, and each reconnect costs
a weight-27 overlap check (one `historicalTrades` page and one `klines` call).
A minute without a print cannot be fenced, so tick mode stops with 20 there,
and again on every resume. Use bars mode for symbols that can go quiet for a
minute.

### Restart policy

Retry budgets are short by design: about 2 minutes of failed WebSocket
connects, or about 7 seconds of failed REST attempts, end with 20 and the
verified cursor retained. Run the feed under a supervisor (a systemd unit with
`Restart=on-failure`, or a container restart policy) that restarts on 20 with
backoff, as `--resume --output-from N` with the runner's committed count.
Exits 21 and 23 need an operator (changed history, protocol or access
failure): an automatic restart repeats the stop. 22 needs a larger budget, more
disk, or a consumer that reads stdout again.

## Qualification and next adapters

`tests/unit.cpp` and `tests/mock_venue.py` contain **synthetic** documented
Binance message shapes only: holes, reordering, duplicates, revisions, access,
reconnect overlap, fragmented text, PING/PONG, retirement, budgets, and recovery.
Never commit public exchange captures. `tests/public_e2e.py` explicitly opts
into public BTCUSDT capture, local receiver delivery, a hard-kill restart,
exact REST verification, bar batch action parity through `run_backtest_full`,
and tick same-print replay determinism. Tick actions are compared with the
batch under the predeclared R-B2 mapping: each action timestamp becomes
`floor(ts / script_tf) * script_tf` on both sides, because a tick fill carries
its print's time while the OHLC batch carries its modeled segment's clock.
Every other field stays exact, bar actions compare raw timestamps, and any
difference fails the run. Its batch observer is linked separately from the
read-only engine's `tests/native_live_equivalence_observer.cpp`; it is not a
feed dependency.

`src/venue.hpp` is the adapter boundary. Later slices add OKX raw ticks/bars
(contract-unit conversion where needed), Bybit confirmed bars **without ticks**,
and USD-M confirmed bars / separately opted-in aggregate-print ticks. Coinbase
remains deferred. `serve` will add approved MIT CivetWeb only to its server
target, bounded client replay/queues, and cursor-bound fan-out; no server library
is linked in this slice. The engine executable remains `pineforge-live`.
