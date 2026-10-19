# Offline release-chain fixtures

Except for the documented `bootstrap/` archive, these are synthetic identities,
never production artifacts. `ready-event.json`
uses exactly audit section 3.2. Its event ID hashes the original bytes of
`release-manifest.json`, which excludes its own hash. The facts and OCI config,
child-manifest and index bytes have real SHA-256 links so tests can distinguish
corrupt transport bytes from validly hashed but contradictory provenance.

Network calls are replaced in the unit tests. CI Docker and distribution gates
use the committed production default, never these synthetic digests.

The OCI version labels use the hub's exact `v1.6.0` spelling. Config, child,
index and manifest digests have been recalculated together; no provenance check
is bypassed to accommodate the prefix.

`receipt-policy.json` is the literal owner policy entry for the data lane.
`sample-release-receipt.json` is a complete synthetic verified receipt, built by
the coordinator in `tests/test_consumer_receipt.py` and validated using the hub's
own checks copied into `tests/hub_receipt_validator.py` for standalone CI. The
hub validation and all other suites run in CI, not on the maintainer's Mac.
