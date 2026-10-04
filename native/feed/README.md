# pineforge-feed

An independent Apache-2.0 C++17 public-market-data adapter. It produces the
native `pineforge-live` runner's JSONL protocol; it does not load strategies,
place orders, access accounts, consume fills, or accept API keys. It is not part
of the Python package or wheel. It has `warmup` (venue candles), `export`
(bars built from prints), `run` (JSONL on stdout) and `serve` (the same durable
stream to many runners over a local WebSocket).

## Venues and modes

| `--venue` `--market` | Example `--symbol` | `bars` (confirmed 1m) | Ticks |
|---|---|---|---|
| `binance spot` | `BTCUSDT` | `kline_1m`, `x=true` | `ticks`: raw `trade`, ID+1, healed by `historicalTrades` |
| `binance usdm` | `BTCUSDT` | routed `/market` `kline_1m`, `x=true` | `agg-ticks` only: aggregate prints, healed by `aggTrades` within 48 h; the bars built from them can differ from Binance klines at minute edges (use `bars` for kline-exact bars) |
| `okx spot` | `BTC-USDT` | `candle1m`, confirm `"1"` | `ticks`: `trades-all`, ID+1, healed by `history-trades` within 3 months |
| `okx swap` | `BTC-USDT-SWAP` | `candle1m`, confirm `"1"`, base volume | `ticks`: `trades-all`, base quantities |
| `bybit spot`, `bybit linear` | `BTCUSDT` | `kline.1`, `confirm=true` | refused |
| `coinbase` | | refused: deferred | refused: deferred |

Gates are checked at startup, before any network access. Bybit tick modes stop
with 23: Bybit trade IDs are not a contiguous cursor and its public REST cannot
page back through trades, so no gap can be proven healed. Bybit bars never come
from trades, because trade-built bars disagree with Bybit candles. Binance USD-M
`ticks` stops with 23: its raw trades have no public REST history. Coinbase
stops with 23 as deferred: it has no confirmed one-minute candle stream, and its
REST and WebSocket trade timestamps disagree, so lossless tick paging does not
exist. OKX inverse swaps are refused (their contracts are quote-denominated).

## Build

Requirements: CMake >= 3.20, a C++17 compiler, OpenSSL Crypto, threads, and
**libcurl >= 8.14.1 with both `ws` and `wss`**. Configure executes a linked
feature check, and every running binary checks the actually loaded libcurl.
Cross-compilation without an executable feature check is not qualified.
`vendor/json.hpp` is SHA-256 pinned; attribution and dependency pins are in
`NOTICE`. No engine linkage is required. `serve` compiles CivetWeb 1.16 (MIT),
fetched at configure time from its release archive and pinned by SHA-256; only
the serve code path links it, and `-DPINEFORGE_FEED_SERVE=OFF` builds a binary
without it (whose `serve` stops with 23). Only CivetWeb's HTTP/WebSocket core is
compiled: no TLS, CGI, Lua, Duktape, WebDAV, SSI or file serving. For an offline
build, point `-DPINEFORGE_FEED_CIVETWEB_ARCHIVE=/path/to/civetweb-1.16.tar.gz` at
a local copy of the same archive; its hash is still checked.

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
# OKX swap ticks and USD-M aggregate prints both feed the runner's tick mode:
build-feed/pineforge-feed run --venue okx --market swap --symbol BTC-USDT-SWAP \
  --mode ticks --state-dir okx-state --start 2026-10-03T03:20:00Z |
  pineforge-live run --strategy strategy.so --warmup okx-warmup.csv --mode ticks ...
build-feed/pineforge-feed run --venue binance --market usdm --symbol BTCUSDT \
  --mode agg-ticks --state-dir usdm-state --start 2026-10-03T03:20:00Z | ...
```

Each venue's warmup takes the same flags and writes its own venue's candles:
use a warmup from the venue and market that the live feed reads. A tick-mode
deployment (raw ticks or `agg-ticks`) builds its bars from prints, so its
matching backtest history comes from `export` (below), not from candles.

Times are minute-aligned Unix milliseconds or strict UTC RFC3339 seconds.
Warmup fetches contiguous REST 1m klines only behind an observed `x=true` WS
watermark. Its end is **exclusive** and must be the live start. It writes the
runner CSV columns `timestamp,open,high,low,close,volume` and an atomic
`warmup.csv.manifest.json` containing source, symbol, units, start, exclusive
cut, confirmation watermark, row count, quantity multiplier, and the CSV
SHA-256. It refuses to overwrite either output. Keep both files together; an interrupted two-file
publication without its manifest is not a qualified warmup.

## Prints-built history: `export`

```sh
build-feed/pineforge-feed export --venue binance --market usdm --symbol BTCUSDT \
  --mode agg-ticks --start 2026-10-03T00:00:00Z --end 2026-10-03T03:20:00Z --output agg-bars.csv
# or from a local copy of the venue's public daily aggregate-trade archive:
build-feed/pineforge-feed export --venue binance --market usdm --symbol BTCUSDT \
  --mode agg-ticks --start 2026-10-02T00:10:00Z --end 2026-10-02T23:50:00Z --output agg-bars.csv \
  --archive BTCUSDT-aggTrades-2026-10-02.zip --checksum BTCUSDT-aggTrades-2026-10-02.zip.CHECKSUM
```

In tick modes the runner builds its 1m bars from the prints, so a backtest that
must equal the forward run needs bars built the same way, not venue candles (see
"Binance USD-M" for how far `agg-ticks` bars and klines can differ). `export`
writes exactly those bars in the warmup CSV format, by the runner's tick-built
bar rule: a print belongs to minute `floor(ts / 60000) * 60000`; open is the first
price, high the maximum, low the minimum, close the last price, and volume the
exact decimal sum of the quantities; a minute without a print repeats the
previous close (for the first minute, the last print before `--start`) with
volume `0`, as the runner's carry-forward bar does. Price tokens are the venue's
own; the volume is the canonical exact sum.

Completeness is proven, never assumed: the prints must form a contiguous ID chain
from the last print strictly before `--start` through the fence, the first print
at or after `--end`. A hole, a missing fence (the window has not closed yet), or
a start outside the venue's REST history (USD-M 48 hours) stops with 20 and
writes nothing; time running backwards along the chain stops with 23. From REST
the chain is paged by ID (Binance 1000, OKX 100 per page); a USD-M start is found
by searching hour windows forward from `--start` up to the present (within the
48-hour history), so a quiet first hour is not a stop.

`--archive` reads the Binance USD-M daily `aggTrades` archive as published (the
`.zip`, inflated in-process and CRC-checked, or the extracted `.csv`, with or
without its header line), streamed rather than loaded. A `transact_time` above
10^14 is microseconds and becomes `floor(us / 1000)` milliseconds. `--checksum`
takes the published `<sha256>  <file name>` file and must match the archive's
SHA-256 and base name (21 otherwise); without it the export logs
`archive_not_checksum_verified`. The window, its predecessor and its fence must
all lie inside the one archive (else 20). Archives are never fetched
automatically.

An existing output or manifest is refused. The CSV and then
`<output>.manifest.json` are written atomically; the manifest records the source
(REST origin, or archive name, SHA-256 and whether the checksum was verified),
the window, the predecessor and fence prints, the first and last IDs, the print,
bar and quiet-minute counts, the rule, and the CSV's SHA-256. Limits: 100000
minutes and 64 MiB of CSV (22 beyond).

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
Overlap, duplicate and revision checks compare exact decimal **values**, not
lexemes: OKX can render one candle price as `100.0` on its WebSocket and as
`100` over REST, which is not a revision; any change of value still is.
A message carries the token of the source that delivered it, so a bar healed
from REST can differ in bytes from the same bar received on the WebSocket, and
two feeds of one instrument can differ in bytes. The values are equal, a
journal replay is byte-identical, and the feed never canonicalises a token.

### Units

Prices are quote per base, times are Unix milliseconds, and every quantity and
volume is in **base** units. OKX swap `sz` and candle `vol` are contracts: the
feed multiplies them exactly (decimal arithmetic, never float) by the
instrument's `ctVal` x `ctMult` from the public instruments endpoint, which must
be a live linear swap with `ctValCcy` equal to the base currency. For
`BTC-USDT-SWAP`, 2.75 contracts are emitted as `0.0275`. A converted quantity is
the canonical product (no trailing fractional zeros); every other token is the
venue's own. The multiplier is bound into the cursor and the warmup manifest;
a resume with a changed multiplier stops with 21.

### Binance spot ticks

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

### Next-print fence: OKX ticks and USD-M aggregate prints

OKX candles and USD-M klines name no print-ID range (a USD-M kline's `f..L`
and `n` count raw trades, not aggregates), so a minute `M` closes with
`time = M + 60000` only on this proof:

1. a contiguous ID chain (`tradeId` or aggregate `a`, each exactly its
   predecessor plus one) from the REST-proven predecessor, the last print
   strictly before `--start`. It is proven on both sides before it is saved:
   OKX walks forward by trade ID from the time lookup until the next print is
   at or after the start (REST can trail the newest prints, and prints of one
   millisecond need not come back in ID order); USD-M takes the first
   aggregate at or after the start within the next hour and the one before it.
   Nothing is anchored, and no minute closes, until both sides are visible. A
   quiet start waits for the first print (each newly closed minute costs one
   look-ahead: OKX a time lookup plus an ID page, USD-M one aggregate search
   per unsettled hour window, searched hour by hour up to the newest venue time
   and at most 48 hours past the start, after which it stops with 20); once a
   WebSocket print exists, REST must show it within about 15 seconds, or the
   feed stops with 20;
2. the fence: the next print in that chain has a matched time at or after
   `M + 60000`, so no further print of `M` can exist (matched time never goes
   backwards along the chain; if it does, the feed stops with 23);
3. the minute's closed candle: a confirmed WebSocket candle, or, only behind a
   later confirmed WebSocket candle, the REST candle (OKX additionally requires
   its per-row confirm `"1"`);
4. for OKX raw prints, exact OHLC and volume equality between the minute's
   prints and that candle. USD-M aggregate prints skip this step: an aggregate
   is dated by its first fill and never split, and one can hold a fill that
   Binance's kline counts in the next minute, so the kline is not their exact
   sum (see "Binance USD-M"). Their completeness rests on conditions 1 and 2,
   and the kline only marks the minute closed.

A candle without the fence never closes a minute, and the fence without the
candle waits. A quiet OKX minute closes on the same fence when its candle shows
zero volume; nonzero volume there stops with 21. Prints of later minutes stay
buffered until the proof. Holes heal by ID through REST: OKX `history-trades`
pages of 100 with exclusive `after`/`before` trade-ID bounds, USD-M `aggTrades`
`fromId` pages of 1000. A hole that starts more than the venue's documented
window before the newest venue time on the WebSocket (OKX 89 days, inside its
3-month window; USD-M 48 hours) stops with 20 without asking REST, and a USD-M
`-4166` "recent 2 days" rejection also stops with 20.

### OKX

`candle1m` and `trades-all` come from the business endpoint `/ws/v5/business`,
subscribed by message after every handshake. Only confirm `"1"` candles are
bars; a forming candle is never one, and a REST row without confirm `"1"` ends
the healed prefix. Ticks never come from the aggregated `trades` channel: a
push on it, or any print carrying `count`, stops with 23. The feed sends text
`ping` every `--keepalive-seconds` (default 20, at most 25: OKX closes a
connection after 30 seconds without traffic) and treats `pong` as control,
never data; a `notice` event (service upgrade) reconnects with overlap. Every
connection re-reads the instrument and re-runs every instrument check: a changed
`ctVal x ctMult` stops with 21, since quantities already emitted were converted
with the old one, and an instrument that is no longer `live` (a suspension)
stops with 23.

### Bybit

Bars only, from `kline.1.<SYMBOL>` on `/v5/public/spot` or `/v5/public/linear`.
A push can carry the minute that just closed (`confirm=true`) and the next
forming one; only the confirmed row is a bar. REST klines arrive newest first
and a range wider than `limit` keeps only its newest rows, so each request asks
for exactly the rows it can hold and the reversed rows must be contiguous. The
feed sends `{"op":"ping"}` every `--keepalive-seconds`. Bybit answers HTTP 403
for its IP rate ban (about ten minutes) and for a restricted region; the feed
stops with 20 there, so a supervised restart retries; give that restart a backoff
above ten minutes, since a request inside the ban window extends it.

### Binance USD-M

Both modes use the routed `/market/stream` endpoint: since 2026-10-02 the
unrouted origin no longer serves `aggTrade` or `kline` streams, and a raw
`trade` event stops with 23. `agg-ticks` maps each aggregate `a` to exactly one
print and never expands `f..l` into imaginary raw trades. **Aggregate prints
are not raw trades**: one print is the sum of same-price, same-side executions,
so a strategy sees fewer, larger intrabar prints than in raw tick mode. The
print quantity is `q`, the aggregate's full executed quantity including RPI
(retail price improvement) executions; `nq` excludes those and would drop real
executions from the print stream. (Every aggregate seen live had `q = nq`.)
USD-M `exchangeInfo` exceeds the 1 MiB JSON
bound, so only its top-level `rateLimits` member is extracted (4 MiB body cap)
and parsed; requests then spend at most half of the published weight ceiling.

**Bars built from `agg-ticks` are not Binance klines.** An aggregate is dated
by its first fill and never split, so one of its fills can belong to the next
minute's kline. The runner builds its bars from the prints, so at those minute
edges its bars, and the fills and indicators that depend on them, can differ
from Binance klines, from a backtest on klines, and from `--mode bars`. Use
`--mode bars` when bars must equal Binance klines. In 120 consecutive
BTCUSDT minutes (2026-10-03, every aggregate re-read from `aggTrades`), 6
minutes (5%) differed, always in pairs where one fill moves into the next
minute: that minute's open is off by one tick (0.10) and both volumes by the
fill (0.002-0.011 BTC); close, high and low never differed, and no print lay
outside its own minute's kline range. In one live run, a short entry signalled
at a minute's close filled at the next minute's first print, one tick (0.10)
below that minute's kline open: the kline opened at the price of the fill that
the aggregate dated in the previous minute, so a backtest on klines fills one
tick away. A backtest on the bars built from the same prints equals the runner.
The matching backtest for an `agg-ticks` deployment is therefore a backtest on
prints-built 1m bars, which `export` writes from REST or the daily archive.

The subscribed data streams are strict: another event type on them, a
malformed trade or kline, or an unknown shape stops with 23 (on every venue). An event outside
the data streams with an unknown type (a new venue notice) is logged as a
structured `unknown_stream_event` warning and ignored; `serverShutdown`
reconnects.

Curl answers server PING with matching-payload PONG; OKX and Bybit get their
text keepalives. Keepalive replies are control messages: they never count as
data. After `--silence-seconds` (default 75) without data, OKX and Bybit
reconnect; Binance instead sends a `LIST_SUBSCRIPTIONS` probe, and a reply that
lists every stream of the connection proves the connection and its streams
alive, so a quiet symbol no longer pays a reconnect and its REST overlap every
75 seconds (an error reply, a list missing a stream, or no reply within 10
seconds reconnects; it is never taken for market data). This is safe because
completeness never rests on the connection: every print is chained by ID, every
minute behind a confirmed watermark, and every reconnect re-verifies its overlap.
Reconnect also occurs on `serverShutdown` or an OKX `notice`, transport loss, or
at 23h55 (before the 24-hour Binance limit). The reader buffers during REST
healing; if a long outage keeps the session healing until the reader's bounded
queue (`--max-queue-bytes`) is full, the reader drops its connection instead of
stopping, waits until the queue has drained to a quarter, and reconnects: what
the venue sent meanwhile is healed by ID or behind the watermark from REST after
the reconnect's overlap check, exactly as after a restart. Only a single message
larger than the whole queue still stops with 22. Every new
connection re-fetches up to 1000 raw prints and 32 closed-bar proofs, requires
exact value/ID/time overlap, then continues the same stream epoch and sequence.
A venue's REST row for the minute that just closed can trail its WebSocket
close by a second or more (observed on USD-M at a restart): a disagreement must
survive three spaced re-reads (about 7 seconds) before it stops with 21, and
warmup re-reads its newest rows the same way. An altered duplicate discovered
outside that window is checked against the full retained normalized journal.
Older, undiscovered revisions are not claimed to be continuously polled.

REST routes are allowlisted per venue: Binance spot `exchangeInfo`,
`historicalTrades`, `aggTrades`, `klines`; USD-M `exchangeInfo`, `aggTrades`,
`klines`; OKX `public/instruments`, `market/history-trades`,
`market/history-candles` (one request per 200 ms, half of 20 per 2 s); Bybit
`market/kline`, `market/instruments-info` (one request per 100 ms, OKX `50011`
and Bybit `10006` rate-limit bodies wait and retry like `429`; OKX `50001`,
`50004`, `50013`, `50026` and Bybit `10000`, `10016` mean "try again" and are
retried with backoff like HTTP 5xx, within the same four attempts). Binance's
public weight and raw-request limits are read from `exchangeInfo`. The one-minute
weight ceiling is spent by the venue's own count: every response's
`X-MBX-USED-WEIGHT-1M` header (which counts every client on the IP) is kept, and
a request whose weight would take the current window past 90% of the ceiling
waits for the next window (`rest_quota_wait`); otherwise requests are not spaced.
Other published ceilings are paced evenly across their interval. `429` honours `Retry-After` as delta-seconds or an
IMF-fixdate HTTP-date; a wait above 24 hours stops with 22 and an unparseable
value with 23. A ban/access rejection is not retried as anonymous trading
access. TLS verification is always enabled; redirects, URL credentials, and
non-origin overrides are refused. Explicit insecure overrides are
**loopback-only**, for tests.

## Durability and restart

One producer owns a state directory via `flock`. The normalized log is a
segmented journal under `journal/`: each segment file `<first index>.jsonl` holds
consecutive messages and is append-only, and beside it a hashed checkpoint
`<first index>.checkpoint.json` records the verified state before its first
message (prefix hash, last-message hash, message index and byte offset, cut,
sequence, last tick time, last bar, predecessor). Messages are group-committed:
every event made ready by the source messages already queued (up to 1024 source
messages, or 4096 events) is appended to the open segment in one write and
fsynced once, then `cursor.json` is atomically replaced once and its directory
fsynced, and only **then** are those lines published (stdout for `run`, clients
for `serve`), one event per line. The cursor binds source origins,
symbol/mode/units, random stream epoch, initial/verified cut, venue ID, emitted
sequence, message index, byte count, the open segment, predecessor,
closed-minute proofs, SHA-256 prefix chain, last-message hash, and fixed message
partition. Its canonical document is itself hashed.

After a commit leaves the open segment at or above `--segment-bytes` (default
16 MiB), it is sealed: the successor's checkpoint is written atomically, then the
empty successor is created and synced, and only then does the cursor name it. A
crash between those steps leaves files that resume removes, and the old segment
stays open. Sealed segments then expire oldest first while the retained journal
exceeds `--replay-bytes` (default 256 MiB, at least twice `--segment-bytes`) or,
with `--replay-age-seconds`, while a segment's last minute is older than that in
venue time; a segment file goes before its checkpoint, so a crash in between
leaves an orphan checkpoint that resume removes. The open segment and every
segment that can hold a message of the protected window (the open minute and the
32 closed-minute proofs the reconnect overlap re-verifies) never expire, nor, in
tick modes, the segment holding the newest print (a reconnect re-reads recent
prints from the journal, and a quiet next-print-fence stream keeps closing
minutes without any); when they alone exceed the budget the feed keeps them
and logs `replay_budget_held_by_protected_window`. Resume verifies the retained journal
from its oldest checkpoint, checks every segment boundary against its
checkpoint and the end against the cursor, and only then fsyncs truncation of an
uncommitted crash tail. It removes only what a crash can leave (the empty
successor of an interrupted seal, the oldest checkpoint of an interrupted
expiry); any other unexpected segment or checkpoint stops with 21 and is left in
place. Resume reads the whole retained window within `--max-replay-seconds`, so
a very large `--replay-bytes` needs a larger budget there. Every crash point of a commit, a seal and an expiry is
exercised by the test suite (`PINEFORGE_FEED_CRASH_AT`, a test hook that kills
the process at a named step) and by random kills.

A duplicate print or candle older than the retained journal can no longer be
compared with what was emitted: it is logged as `expired_duplicate_unverified`
and ignored. Inside the retained journal every duplicate is still checked.

A pipe write is not a consumer acknowledgment. If the runner has committed N
messages:

```sh
build-feed/pineforge-feed run --venue binance --market spot --symbol BTCUSDT \
  --mode bars --state-dir feed-state --resume --output-from N |
  pineforge-live run --strategy strategy.so --warmup warmup.csv --mode bars \
    --script-tf 1 --feed - --ledger runner.sqlite3 --from-input N \
    --symbol BINANCE:BTCUSDT --webhook-url https://receiver.example/actions
```

Replace N with the runner's actual committed count; never use the producer's
possibly-ahead count. `--resume` without `--output-from` replays from index 0.
Bind warmup identity and stream epoch in orchestration. An unavailable cursor or
changed identity is refused. A cursor older than the retained journal stops with
22 and says how to recover: a consumer behind the first retained index cannot
resume from this stream; rebuild it from a warmup that ends inside the retained
window, or run with a longer retention. SIGTERM/SIGINT stops intake, discards
staged messages that were never committed or published, and exits 0. A
directory whose journal holds only empty segments and no cursor (a crash before
the first cursor write) is taken over by a fresh `--start`; other existing state
needs `--resume`. A state directory from a release before the segmented journal
(`events.jsonl`) is refused; start a new one.

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
| 21 | Changed overlap, conflicting duplicate, revised bar, changed cursor/prefix/checkpoint |
| 22 | Expired output cursor, queue/allocation budget or durable I/O failure |
| 23 | Unsupported input/source, permanent access, protocol/decimal/dependency failure, matched-time regression |

Retained journal: `--segment-bytes` (16 MiB), `--replay-bytes` (256 MiB),
`--replay-age-seconds` (off). Each prefix traversal: 60 seconds
(`--max-replay-seconds`, maximum 3600); source and unconfirmed-print queues:
16 MiB each (`--max-queue-bytes`); WS text: 1 MiB; REST body: 1 MiB. Warmup and
export: 100000 rows / 16 MiB and 64 MiB. Raising limits is an explicit operator
choice. `--max-messages` permits bounded qualification. `--reconnect-seconds` can
shorten the 23h55 rotation for synthetic tests, never extend it beyond that
limit, and `--silence-seconds` shortens the data-silence threshold.

### Throughput

Storage sets the ceiling, so measure on the target disk. On a shared 16-vCPU
Linux test machine (ext4 root), committing each message separately (three
fsyncs per print) sustained about 120 messages/s. With group commit, the
loopback bench (synthetic contiguous prints through the real binary and the
mock venue, wall time including startup) ran 30,000 prints at 5,811, 11,212 and
10,842 messages/s in three runs on the same machine under load; with the
segmented journal, on an idle machine, 30,000 prints ran at about 42,000
messages/s, and through `serve` one client received a 30,000-print burst at about
19,000 messages/s (eight clients at once, about 17,000 each). The busiest BTCUSDT
minute of a recent 89-day sample averaged 655 prints/s, about one ninth of the
slowest run.

### Retention per mode

The journal no longer has a terminal budget: it keeps the newest
`--replay-bytes` and expires older segments. The retained replay window is the
budget divided by the journal's growth:

| Mode | Bytes per message | Messages | Window at the default 256 MiB |
|---|---|---|---|
| bars | about 138 | 1 per minute | about 3.7 years |
| ticks, BTCUSDT median day | about 92 | 2.92 M per day | about 24 hours |
| ticks, BTCUSDT busiest day | about 92 | 8.93 M per day | about 8 hours |

Message sizes come from a public BTCUSDT capture; the daily print counts are
REST kline trade counts over 89 days. A consumer must resume inside that window.
Startup verifies the whole retained window, and the journal scans for the open
minute, a duplicate closed candle or an old duplicate print start at the
segment that holds the minute or sequence, each bounded by
`--max-replay-seconds`.

### Tick mode needs a liquid symbol

On OKX and Bybit 75 seconds without any data message forces a reconnect, and
each reconnect costs an overlap check; Binance proves a quiet connection with a
probe instead (see the reconnect paragraph above). On Binance spot a minute
without a print cannot be fenced, so
tick mode stops with 20 there, and again on every resume. OKX closes a quiet
minute on the next-print fence and its zero-volume candle, USD-M on the fence
alone; the runner then sees a `time` event with no print in that minute. Use bars mode for
symbols that can go quiet for a minute.

### Restart policy

Retry budgets are short by design: about 2 minutes of failed WebSocket
connects (a connection the venue accepts but closes before any frame counts as
failed), or about 7 seconds of failed REST attempts, end with 20 and the
verified cursor retained. Run the feed under a supervisor (a systemd unit with
`Restart=on-failure`, or a container restart policy) that restarts on 20 with
backoff, as `--resume --output-from N` with the runner's committed count.
Exits 21 and 23 need an operator (changed history, protocol or access
failure): an automatic restart repeats the stop. 22 needs a larger budget, more
disk, a consumer that reads stdout again, or (an expired cursor) a consumer
rebuilt inside the retained window.

## Serve: one producer, many runners

```sh
build-feed/pineforge-feed serve --venue binance --market spot --symbol BTCUSDT \
  --mode bars --state-dir feed-state --start 2026-10-03T03:20:00Z --listen 127.0.0.1:8787
curl -s http://127.0.0.1:8787/v1/status        # epoch, source, retained range, last seq/time, clients
pineforge-live run --strategy strategy.so --warmup warmup.csv --mode bars --script-tf 1 \
  --feed-url 'ws://127.0.0.1:8787/v1/stream?epoch=E&from=0' --allow-insecure-http \
  --ledger runner.sqlite3 --symbol BINANCE:BTCUSDT --webhook-url https://receiver.example/actions
```

`serve` is `run` with its stdout replaced by a local HTTP/WebSocket server: one
producer per venue, market, symbol and mode owns the same durable journal (same
proofs, group commit, persist-before-publish, `--resume`, retention) and any
number of runners read it, instead of each opening its own venue connection.
Every WebSocket text message is exactly one journal message, so message
boundaries, and therefore the runner's `--from-input` count, are the journal's.

- `GET /v1/status`: `epoch`, `source` (venue, market, symbol, mode, origins,
  start, quantity multiplier), `retained` (`first_index`, `next_index`), `last`
  (sequence, verified cut, last tick time, last bar), `clients` and the limits.
- `GET /v1/snapshot`: the complete committed prefix from index 0 as JSONL, the
  only HTTP shape the runner accepts (it starts every snapshot at index 0 and
  refuses more than 4 MiB). Once that prefix exceeds 4 MiB the endpoint answers
  413, and once index 0 has expired 410, with the reason in the body: use the
  WebSocket stream.
- `ws://HOST:PORT/v1/stream?epoch=E&from=I`: every message from index `I` on,
  first from the journal, then live. Cursor negotiation is HTTP, during the
  upgrade, never an event in the runner's input: a malformed request answers
  400, a wrong epoch 409, a cursor beyond the committed log 416, an expired
  cursor 410, a full server 503, each with a JSON reason. The runner reports any
  of them as a failed handshake.

Resume: bind each runner to the stream's epoch and the index `S` of its first
message (0 when its warmup ends at the producer's start). With N messages in its
ledger, a restarted runner connects with `from=S+N` and `--from-input N` and
receives exactly the remaining messages. A producer restart (`serve --resume`)
keeps the epoch, so runners reconnect the same way; the runner stops when the
producer goes away and is restarted with its own committed count.

Bounds: each client reads the journal until it reaches the head and then drains
its own queue of released messages, at most `--client-queue-bytes` (default
1 MiB, which must hold one burst: a group commit releases up to 4096 messages at
once); `--max-clients` (default 64). A client whose queue overflows (it reads
slower than the stream, beyond the kernel's socket buffers) is closed with code
1008 and the reason `slow consumer`; a client whose cursor expires while it
catches up is closed with 4410 as soon as it reaches the expired segment.
Neither ever blocks the producer or another client. A client holds a journal
segment open only while it catches up. At start `serve` raises its open-file
limit to the hard limit and refuses to start (23) when that is below
`2 x --max-clients + 64`. A client that stops reading entirely is dropped when its blocked write
times out (30 seconds). On SIGTERM the producer drains each live client's queue
and closes every client with 1001 (a client still catching up from the journal
is closed at once and resumes from its own count).

Quiet streams: the runner's WebSocket idle deadline is 15 seconds and libcurl
answers PINGs without telling it, so `serve` sends an unsolicited PONG to a
client after 5 seconds without a message; a bar stream (one message a minute)
stays connected.

Listening: `--listen` defaults to `127.0.0.1:8787`; port 0 picks a free port,
logged as `serve_listening`. **`serve` has no TLS and no authentication.** A
non-loopback address needs `--allow-remote-listen`; expose it only behind a
reverse proxy that terminates TLS and authenticates clients (for example nginx
or Caddy proxying `wss://` to the loopback listener), and never on a public
interface directly.

## Qualification and next adapters

`tests/unit.cpp` and `tests/mock_venue.py` contain **synthetic** documented
message shapes only (Binance spot, USD-M, OKX, Bybit): holes, reordering,
duplicates, revisions, access, reconnect overlap, fragmented text, PING/PONG
and text keepalives, retirement, budgets, recovery, and every venue quirk above.
Never commit public exchange captures. `tests/public_e2e.py --venue V --market
M --symbol S --modes bars,ticks|agg-ticks` explicitly opts into a public
capture of one venue, local receiver delivery, a hard-kill restart, exact REST
verification (OKX candle values; lexemes elsewhere), bar batch action parity
through `run_backtest_full`, and tick same-print replay determinism. For tick
modes the batch replays 1m bars built from the feed's own prints (the tape),
not venue candles; for `agg-ticks` the receipt also reports how many minutes
differ from Binance klines (`kline_differs`) and how a batch over those klines
compares with the runner. Tick actions are compared with the
batch under the predeclared R-B2 mapping: each action timestamp becomes
`floor(ts / script_tf) * script_tf` on both sides, because a tick fill carries
its print's time while the OHLC batch carries its modeled segment's clock.
Every other field stays exact, bar actions compare raw timestamps, and any
difference fails the run. Its batch observer is linked separately from the
read-only engine's `tests/native_live_equivalence_observer.cpp`; it is not a
feed dependency.

`src/venue.hpp` is the adapter boundary: a venue supplies its WebSocket
connection (path, subscription frames, keepalive, frame classifier), decoding,
REST history and candles, its tick proof and its print history window.
`src/session.cpp` holds the venue-neutral sessions, `src/state.cpp` the
segmented journal, `src/serve.cpp` the fan-out and `src/export.cpp` the
prints-built history. Coinbase remains deferred. The engine executable remains
`pineforge-live`.

`tests/serve_e2e.py` qualifies `serve` against the public venue and the real
runner: one producer, runners on `--feed-url`, a runner restart with
`--from-input`, a producer SIGKILL and `--resume`, each runner's ledger equal to
the journal, no idle-deadline stop, and the same REST and batch checks.
`public_e2e.py --export` also checks `export` over a tick window: its bars must
equal the bars built from the tape's prints, and the batch over them the runner.
