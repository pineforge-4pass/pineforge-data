# Corpus fixture provenance

`corpus_ohlcv_ETH-USDT-USDT_1m_sample.csv` is the header and first 256 data
rows copied byte-for-byte from:

- repository: `pineforge-4pass/pineforge-corpus`;
- file: `data/ohlcv_ETH-USDT-USDT_1m.csv`;
- Git LFS SHA-256: `db8c1332da093008cfbd063e05db0b33fe8f7fd35d78cf058a366519eb9f6cc5`;
- sample SHA-256: `8ba4db0c669e47c11746361586cef8bce6371eeeab5af5e351107bd69451c6af`;
- timestamps: `1577836800000` through `1577852100000` (Unix milliseconds).

CI uses this bounded sample to avoid downloading the 168 MB Git LFS object on
every run. `scripts/run_database_e2e.sh` automatically uses the full sibling
corpus file when it is available, while still reading only the requested number
of rows.
