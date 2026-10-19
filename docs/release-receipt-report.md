# Data consumer receipt v1 delivery report

Implemented in `/Users/haoliangwen/code/pineforge-data-wt-rc-data` on
`rc/data-adapter`. HEAD remains `63c66863272f4b362c1efa503d50dda0e34ced3c`.
All changes remain in the working tree. No commits, pushes, file deletions,
release publications or callbacks were performed locally.

The integration review's sections 1 and 3, the entire hub **Consumer receipt
contract v1** section, its reference builder and `release_receipts.py`'s
`validate_receipt`, `collect`, `profile`, `signal` and supporting checks were
reviewed. The validator is authoritative. No unresolved documentation/code
conflict was found for the data receipt fields or transport. The B4 image label
spelling follows the hub's actual code: Dockerfile line 74 consumes
`RELEASE_VERSION`, `publish.yml` passes the v-prefixed Git tag, and
`release_smoke.py` explicitly expects `v` + the bare release version.

## Files changed

- `.github/workflows/pineforge-release.yml`
- `.github/workflows/release-report.yml`
- `.github/workflows/release.yml`
- `docs/release-receipt-report.md`
- `docs/releasing.md`
- `scripts/release_adapter.py`
- `scripts/release_chain/automation.py`
- `scripts/release_chain/contract.py`
- `scripts/release_chain/publishing.py`
- `scripts/release_chain/receipts.py`
- `scripts/release_chain/reporting.py`
- `tests/fixtures/release_chain/README.md`
- `tests/fixtures/release_chain/amd64-config.json`
- `tests/fixtures/release_chain/amd64-manifest.json`
- `tests/fixtures/release_chain/arm64-config.json`
- `tests/fixtures/release_chain/arm64-manifest.json`
- `tests/fixtures/release_chain/index.json`
- `tests/fixtures/release_chain/ready-event.json`
- `tests/fixtures/release_chain/receipt-policy.json`
- `tests/fixtures/release_chain/release-manifest.json`
- `tests/fixtures/release_chain/sample-release-receipt.json`
- `tests/hub_receipt_validator.py`
- `tests/test_consumer_receipt.py`
- `tests/test_release_adapter.py`

`ci.yml`, its required aggregate, all real Docker/database E2E checks, the unit
matrix, deterministic App PR diff/head/base/tree gates, protected squash merge,
landed CI distribution checks, OIDC publisher, immutable tag/assets, and actual
PyPI byte verification are retained. `evidence.json` remains the existing CI
build provenance; it is not a second consumer receipt. Ordinary manual package
publication without a product target still uses those gates and does not emit
an invalid receipt with null event/version fields.

The old report's `manifest`, object-shaped `attempt`, `completed_at`,
`pull_request`, `verification` and `deployment` fields, per-run hub artifact
names, `consumer-receipt.json` release asset, configurable event type and
full-receipt dispatch were replaced. No alternate receipt builder or callback
format remains in the runtime code. Human-facing consumer incident issues and
supersession PR comments remain. Diagnostics and private run/PR links stay in
those issues and Actions logs.

## Artifact and producing workflow/job

- **Artifact:** `release-receipt-data` (literal, no attempt/version/run suffix).
- **Archive inventory:** exactly one root file, `release-receipt.json`.
- **Reporting workflow/policy:** `.github/workflows/pineforge-release.yml`.
- **Builder:** `reconcile`, running trusted checked-out `main` code. It reconciles
  ready requests/PR CI, or verifies a completed `Release` run via
  `release_chain.reporting.publication`.
- **Uploader and callback:** caller job `report` invokes
  `.github/workflows/release-report.yml` job `report` (Actions job display
  `report / report`). Only the coordinator passes `hub: true`; publication's
  own reusable report call keeps the default `false`.
- **Serialization:** sorted compact UTF-8 JSON plus LF. One live artifact per
  reporting run; overwrite on an Actions rerun and increase `sequence` using
  `GITHUB_RUN_ATTEMPT`. `attempt` is the coordinator's numeric `GITHUB_RUN_ID`.
- **Order:** upload succeeds, retain the same verified contract as immutable
  `release-receipt.json` on the data GitHub Release, mint a hub-only App token,
  send the four-key signal. Upload failure prevents dispatch. A transport error
  creates a local incident without overwriting an uploaded completion snapshot.

All intermediate `release-stage-<attempt>-<stage>` snapshots also serialize
exactly this same contract; none use a home-grown receipt/report shape. Only
the literal coordinator artifact is selected by the hub policy.

For `verified`, both CI/E2E reference the complete successful pre-merge PR CI
attempt, including release/ready. Main CI independently authorizes the exact
published distributions. After the `Release` run finishes, the coordinator
rechecks actual GitHub PR/head/base/merge/first parent, pre-merge completion
times, main CI artifact identity, deploy job inventory, immutable PyPI downloads,
matching GitHub distribution bytes, wheel-embedded source/manifest/facts, and
previous distribution identity. Deploy and reporting run heads must equal the
merge. If workflow_run picked up a newer main SHA, repair dispatches use the
verified immutable data package tag, never the PR head; no invalid snapshot is
uploaded while repair is queued. Previously verified targets never emit later
failure/progress/superseded snapshots; local incidents still record errors.

## Synthetic sample receipt

This is the complete sample used by the new CI test (synthetic run IDs 70 for
pre-merge CI/E2E, 80 for landed build provenance, 200 for publication, and 300
for the coordinator). It is not a receipt from a live production run. Actual
receipts hash the real package-version/distribution inventory as the deployment
identity, and use observed commits, digests, run results and UTC completion time.

```json
{
  "attempt": 300,
  "ci": {
    "approved_skips": [],
    "expected": [
      "Python 3.11",
      "Python 3.12",
      "Python 3.13",
      "Release contract",
      "Docker runtime",
      "Database E2E",
      "Distribution",
      "release/ready"
    ],
    "results": {
      "Database E2E": "success",
      "Distribution": "success",
      "Docker runtime": "success",
      "Python 3.11": "success",
      "Python 3.12": "success",
      "Python 3.13": "success",
      "Release contract": "success",
      "release/ready": "success"
    },
    "run_attempt": 2,
    "run_id": 70,
    "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "consumer": "data",
  "deploy": {
    "approved_skips": [
      "publish"
    ],
    "expected": [
      "prepare",
      "build",
      "github-release",
      "publish",
      "verify"
    ],
    "results": {
      "build": "success",
      "github-release": "success",
      "prepare": "success",
      "publish": "success",
      "verify": "success"
    },
    "run_attempt": 1,
    "run_id": 200,
    "sha": "cccccccccccccccccccccccccccccccccccccccc"
  },
  "deployed": {
    "deployment_id_sha256": "fda9cda3a5ee2e0490aa51926b814488b288052e537910fdf0aa1cfd03a3fc9a",
    "facts_commit": "5555555555555555555555555555555555555555",
    "facts_sha256": "c4cbcff4e067a80515a05616a3d4dd4e672b4237d8d384250de2d5071b2070ee",
    "image_digest": "sha256:cd06c078d3fc66f79d7b9815b1a68cdce51f0b0e68812466a051e436ed2ff3cc",
    "source": "cccccccccccccccccccccccccccccccccccccccc"
  },
  "e2e": {
    "approved_skips": [],
    "expected": [
      "Python 3.11",
      "Python 3.12",
      "Python 3.13",
      "Release contract",
      "Docker runtime",
      "Database E2E",
      "Distribution",
      "release/ready"
    ],
    "results": {
      "Database E2E": "success",
      "Distribution": "success",
      "Docker runtime": "success",
      "Python 3.11": "success",
      "Python 3.12": "success",
      "Python 3.13": "success",
      "Release contract": "success",
      "release/ready": "success"
    },
    "run_attempt": 2,
    "run_id": 70,
    "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "event_id": "sha256:8b23d0dd906cd9e5fc8f9750f486b9ca141466d4abdb021f7ba2ebdcf2e3cf11",
  "pr": {
    "base": "main",
    "base_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "merge": "cccccccccccccccccccccccccccccccccccccccc",
    "number": 7
  },
  "preview": {
    "deployment_id_sha256": "a64240825d1189c77374a67057257d5877c4cdadeeab6f8d08a3a3df53206800",
    "facts_commit": "5555555555555555555555555555555555555555",
    "facts_sha256": "c4cbcff4e067a80515a05616a3d4dd4e672b4237d8d384250de2d5071b2070ee",
    "image_digest": "sha256:cd06c078d3fc66f79d7b9815b1a68cdce51f0b0e68812466a051e436ed2ff3cc",
    "source": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "previous": {
    "deployment_id_sha256": "a2ddfbc368cb7ea8af71abdd57c9a34b4df4d48827c82bd28d43f385b7805849",
    "facts_commit": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "facts_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "image_digest": "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "source": "dddddddddddddddddddddddddddddddddddddddd"
  },
  "release_version": "1.6.0",
  "schema": "pineforge/consumer-receipt/v1",
  "sequence": 1,
  "state": "verified",
  "timestamp": "2020-01-01T00:07:00Z"
}
```

## Exact callback

The App token is separately scoped only to the hub, with Contents write. POST
`repos/pineforge-4pass/pineforge-release/dispatches` only after artifact upload.
`client_payload` contains exactly these four keys. `run_id` is the reporting
run containing `release-receipt-data`, not the publication or CI run.

```json
{
  "event_type": "pineforge-release-status",
  "client_payload": {
    "schema": "pineforge/consumer-signal/v1",
    "consumer": "data",
    "release_version": "1.6.0",
    "run_id": 300
  }
}
```

`RELEASE_RECEIPT_EVENT_TYPE` is no longer required or used. The contract fixes
`pineforge-release-status`; a legacy/custom callback cannot be selected.

## Hub RELEASE_RECEIPT_POLICY entry

Merge this `data` entry into the hub's existing policy object; preserve other
roles. The same entry is checked into `receipt-policy.json` for CI. CI and E2E
both use the full, ordered eight-job inventory with zero job-level skips.
Deploy includes previous identity recording in prepare and served-byte checking
in verify. Only publish can skip, after proving matching bytes already exist.

```json
{
  "data": {
    "artifact": "release-receipt-data",
    "base": "main",
    "ci": {
      "approved_skips": [],
      "expected": [
        "Python 3.11",
        "Python 3.12",
        "Python 3.13",
        "Release contract",
        "Docker runtime",
        "Database E2E",
        "Distribution",
        "release/ready"
      ],
      "workflow": "ci.yml"
    },
    "deploy": {
      "approved_skips": [
        "publish"
      ],
      "expected": [
        "prepare",
        "build",
        "github-release",
        "publish",
        "verify"
      ],
      "workflow": "release.yml"
    },
    "e2e": {
      "approved_skips": [],
      "expected": [
        "Python 3.11",
        "Python 3.12",
        "Python 3.13",
        "Release contract",
        "Docker runtime",
        "Database E2E",
        "Distribution",
        "release/ready"
      ],
      "workflow": "ci.yml"
    },
    "workflow": "pineforge-release.yml"
  }
}
```

## Validation and remaining rollout requirement

`tests/test_consumer_receipt.py` builds the sample through the actual completed
publication coordinator, validates it with the hub's own validate_receipt /
check_run / identity logic, checks the artifact archive and exact signal with
the hub checks, rejects schema/gate/source drift, and covers immutable-source
repair, approved publication replay skips and verified-state non-regression.
The sibling checkout cannot be imported in standalone consumer CI, so the
minimal validator/check/helper snapshot is vendored in
`tests/hub_receipt_validator.py`, not used by production. Its field-check
function ASTs were compared statically with the current hub: all eight match
exactly. Its signal version helper inlines the same bare semver subset to avoid
importing the rest of the hub. The upstream function bodies are preserved,
including their formatting, rather than rewritten to this repo's lint style.

Upstream `scripts/release_receipts.py` SHA-256:
`6fe7b0afcb7a34de9e28bce9d8372e5a4eaa6695398536697f59ed705b0cc23a`.

Static checks only were run locally, per the owner rule. No pytest, Docker,
database, distribution exercise, publication or live callback was run. Both the
new hub-validator test and all existing suites remain CI work.

```text
$ ruff check .
All checks passed!
exit 0

$ mypy src
Success: no issues found in 21 source files
exit 0

$ actionlint
(no output)
exit 0

$ python3 -m py_compile scripts/release_adapter.py scripts/release_chain/__init__.py scripts/release_chain/automation.py scripts/release_chain/contract.py scripts/release_chain/evidence.py scripts/release_chain/publishing.py scripts/release_chain/receipts.py scripts/release_chain/reporting.py tests/test_release_adapter.py tests/test_consumer_receipt.py tests/hub_receipt_validator.py
(no output)
exit 0

$ git diff --check
(no output)
exit 0

node --check: not applicable; no .js, .mjs or .cjs sources in this repository.

Static AST comparison: all 8 copied hub validator functions match the current hub exactly.

$ git rev-parse HEAD
63c66863272f4b362c1efa503d50dda0e34ced3c

$ git diff --name-only --diff-filter=D
(no output; no tracked files deleted)
```

**Baseline requirement before enabling data:** the hub contract requires a real
previous/rollback identity with source, image and consumed facts provenance.
A pre-manifest package cannot truthfully supply its previous facts fields. The
adapter verifies the previous wheel/sdist downloads, matching GitHub assets,
unchanged tag/source, and wheel-embedded manifest/facts. If that provenance is
absent, prepare fails before OIDC publication and reports the failure; it does
not invent an identity or claim verified. Establish a manifest-based published
baseline in CI before setting the hub's data readiness flag. Automatic rollback
also continues to require a stored verified contract. This rollout prerequisite
is explicit in `docs/releasing.md`; it was not bypassed to fabricate a receipt.

**TEMPORARY first-adopter exception (TX-DATA-BASELINE2, L6 10-19):** for
pineforge-data 0.3.0 → 0.3.1 only, the previous identity uses
`provenance: "pre-manifest-bootstrap"` with `facts_commit` and `facts_sha256`
keys **ABSENT**, including no null placeholders. The producer verifies actual
PyPI wheel/sdist bytes against recorded hashes, matching immutable release
assets and the recorded tag commit. This form is for one use; a receipt carries
no data package version, so the hub cannot refuse the form in a later data
receipt. The data producer's 0.3.1 adopter-version gate and removal enforce the
one-time limit. Remove the data exemption and the hub form together, only after
0.3.1 is verified AND every retained `release-receipt-data` artifact carrying the
bootstrap `previous` has expired (90-day retention), because the hub's scheduled
collector re-validates retained receipts and `recover_applied` replays the stored
receipt.
