# Offline release-chain fixtures

These are synthetic identities, never production artifacts. `ready-event.json`
uses exactly audit section 3.2. Its event ID hashes the original bytes of
`release-manifest.json`, which excludes its own hash. The facts and OCI config,
child-manifest and index bytes have real SHA-256 links so tests can distinguish
corrupt transport bytes from validly hashed but contradictory provenance.

Network calls are replaced in the unit tests. CI Docker and distribution gates
use the committed production default, never these synthetic digests.
