# Releasing to PyPI

PineForge Data publishes pure-Python wheels and source distributions through
PyPI Trusted Publishing in the existing `release.yml` workflow and `pypi`
environment. Stable hub releases automatically open a dependency PR, pass this
repository's CI and runtime E2E, merge under branch protection, and publish the
landed commit's tested distributions. A dispatch acknowledgement is never a
publication receipt.

## Receiving a ready release

`pineforge-release.yml` receives `repository_dispatch` with event type
`pineforge-release` and exactly the `pineforge/release-ready/v1` payload from
the release-chain audit, section 3.2. Legacy three-field events fail closed.
The adapter fetches `release-manifest.json` from the fixed
`pineforge-4pass/pineforge-release` release `v<release_version>` and verifies the
original bytes against both `event_id` and `manifest.sha256`. The manifest has
the same immutable identity fields as the event, excluding its own hash and
`event_id`. Producer package spellings are normalized before comparison; HPO
is null before `1.6.0-rc.1`. Prereleases produce `skipped_by_policy` receipts.

The adapter verifies the hub tag's source commit, immutable facts bytes and
nonempty target release entry, the OCI index and both architecture children,
configuration digests, platform identities, component versions and hub revision.
Data consumes the image; package inventories in the manifest remain the hub's
readiness prerequisites. It does not install upstream npm/PyPI packages or
vendor a canonical facts snapshot. There is no newer local facts copy to
replace or downgrade.

A stable update changes only:

- `src/pineforge_data/release_contract.py`: Python image default;
- `docker/server.Dockerfile`: the same image default and hub source pin;
- `pyproject.toml`: an independent data package patch increment;
- `src/pineforge_data/release_lock.json`: complete received event, data version
  and previous identity;
- `src/pineforge_data/release_manifest.json`: exact verified manifest bytes.

The lock and manifest are created by the first real ready event; synthetic test
fixtures never become production defaults. Until then only the original 1.0.0
image/digest is accepted without a manifest. Both JSON files ship in the wheel.

PR branches use
`bot/pineforge-release/main/stable/<version>-<manifest12>`. A machine-readable
body marker records the full event identity, base and generated head. Duplicate
open PRs are reused. Applied events do not bump again. Older deliveries are
reported as superseded. A newer PR is created before obsolete bot PRs close;
branches, tags and assets are retained. Human or unrecorded branch commits stop
automation. Base updates add merge commits and require new CI evidence; no
force push or direct main push is used.

## Required gates and protected merge

The required aggregate check is **`release/ready`**. It verifies the actual
Actions job inventory and refuses failed, cancelled, missing or skipped jobs:

| Job | Required evidence |
|---|---|
| Python 3.11, 3.12, 3.13 | Existing Ruff, mypy and full unit suites; existing 3.13 distribution checks |
| Release contract | Synchronized defaults, original hub manifest bytes, tag/source, facts and both OCI children |
| Docker runtime | Existing real backtest, header/ctypes ABI comparison, harness parity, concurrent server/cache behavior; observed runtime and built server labels must agree with the manifest |
| Database E2E | Existing corpus-backed SQLite/MySQL/Postgres provider E2E against disposable CI resources |
| Distribution | Build wheel/sdist, strict metadata and native-feed exclusion checks; install each in an isolated environment and exercise a real Pine backtest without importing checkout source |

Dedicated Docker and database jobs check a named, nonempty JUnit inventory and
allow zero skips. Opt-in Docker/database cases may skip in the ordinary unit
matrix because the dedicated jobs run them. Installed distribution checks also
require nonzero bars/trades, the expected package version, exact default image,
observed component versions and resolved digest/source agreement.

Before merging, trusted main-branch tooling checks the App-owned PR's complete
deterministic diff, current generated head, current main base, latest desired
manifest and exact tested merge commit/tree. It checks every required job in
that CI attempt again, then calls GitHub's normal squash-merge API with the
expected head. No admin merge is used. Strict up-to-date required checks and an
App without branch bypass are essential to close the final base-movement race.
Merge queue is not enabled by this implementation.

## Publication, completion and repair

Successful PR CI wakes the reconciler through `workflow_run`. The App's merge
triggers `ci-post.yml` on main. `Release` wakes on completion of that main CI, and
accepts only a landed App-created release/version/rollback PR. Ordinary main
pushes are not publication requests. Failed main CI cannot authorize upload.

The publisher downloads that exact CI attempt's tested wheel and sdist and
compares SHA-256 inventory. It preserves the matching immutable data tag and
GitHub Release assets, rechecks desired state immediately before upload, then
uses the existing PyPI OIDC action. There is no API-token fallback. An already
published version with matching tested bytes skips upload; different existing
bytes fail. Archives use the source commit timestamp for reproducibility.

After upload, both PyPI metadata and actual immutable downloads must match the
tested distribution inventory. Only then is the GitHub latest pointer moved
and a `verified` receipt recorded. Publication does not claim that users have
installed the new package or that a hosted data server was deployed.

The only receipt format is the hub's **Consumer receipt contract v1**,
`pineforge/consumer-receipt/v1`. The trusted `pineforge-release.yml` coordinator
runs checked-out `main` tooling. Its `report` job calls `release-report.yml`'s
`report` job, which uploads exactly **`release-receipt-data`** containing one root
file **`release-receipt.json`**. Sorted, compact JSON with a final LF is used.
Full producer reruns replace that run's one live artifact and advance `sequence` using the
Actions run attempt. `attempt` is the reporting Actions run ID. Stage artifacts
named `release-stage-<run_attempt>-<stage>` contain a receipt for a current target
or an internal `producer-outcome.json` witness for an actual targetless decision
or stored replay. The witness is private and never enters the hub artifact,
retained receipt or callback. Targetless operations produce no public receipt.
Ordinary manual package publication still uses the existing landed CI evidence
and gates. CI/PR-head code never builds the hub artifact.

The producer records the actual decision and its inputs, bound to its Actions
run, attempt and head. The reporter independently reads the current run, complete
artifact inventory and producer jobs. Ordinary main pushes, pending main proofs,
package bumps and repair handoffs can therefore finish quietly with a valid
current witness. Missing files alone never authorize that result. Missing,
expired, unregistered, invalid or wrong-attempt evidence fails collection.
Re-running only the report job cannot borrow an earlier attempt's decision.
Every downloaded current stage must agree on the target and producer decision;
a later targetless stage cannot erase an earlier target. Package publication
carries prepare's witness through GitHub Release, preflight and verification,
checking its CI run/attempt, source and version against the bundle at each step.
An absent targeted receipt cannot select the package-only path.

The reporting workflow also retains private `run-context.json` in the separate
`release-run-context-<attempt>` artifact. Before downloading stage artifacts it
captures the runner's original event, including the upstream run and attempt for
`workflow_run` or the original Release dispatch inputs, and the caller's actual
`needs` observations. Consumers bind those facts to the repository, run, attempt
and head, re-read the upstream run's exact recorded attempt, and check the
complete current job and stage inventories. A newer running attempt does not
invalidate the original completed trigger; missing or conflicting exact-attempt
data still fails. This context stays outside the public receipt, immutable
completion asset and four-key callback. It is an ordinary repository run artifact
holding the full event payload for 90 days, with the repository's artifact access.

A manual dispatch of **CI** or **Native feed** on main can complete a Release
run with every release producer skipped. A successful `claim_placeholder` run
also has no release producer. These runs finish quietly only with that complete
context and matching independent observations. They create no product receipt
or failed incident. An empty artifact list, a caller boolean, a missing context,
or an executed producer with missing evidence cannot establish this outcome.
Report-only retries cannot borrow context or stage evidence from another attempt;
use **Re-run all jobs** when evidence needs regeneration.

The sole context-free empty-publisher exception requires independent provider
proof that the exact Release run/attempt on main completed as `cancelled` before
any job executed. The selected run and independent specific-attempt record must
match repository, workflow, main branch, head, run ID and attempt. The complete attempt-jobs response
must explicitly declare integer `total_count: 0` and `jobs: []`, with no stage,
context, bundle or receipt evidence and no downloaded stage. An executed job,
including a refused authz job, incomplete inventory metadata, or a noncancelled
publisher stays on the strict evidence paths. The adapter emits only an internal
`publisher-cancelled-before-execution` witness bound to that publisher ID,
attempt and head.

Collection still requires the adapter's own authenticated
reporting context. For a `workflow_run` trigger, collection first matches the
witness's publisher ID, attempt and head to the authenticated original event,
then selects that event's exact independent API attempt and complete jobs.
A newer running or successful latest attempt cannot replace the original
cancelled attempt; this consumer path does not read the latest publisher run.
For manual `workflow_dispatch`, the original `publication_run` input still
selects the latest publisher, so an older cancelled witness cannot suppress
the current target. Publisher stage, context, bundle or receipt evidence,
including evidence from a later attempt, still refuses this quiet outcome.
It emits no public receipt or callback and makes no claim about the cause of
cancellation.

For jobs in the reporting run, the workflow's actual `needs` results establish
completion. The attempt-jobs API independently supplies names and run/source
bindings, but its status can still be queued, running or null after completion.
Conflicting terminal conclusions, missing or unexpected jobs, and mismatched
attempts or sources refuse collection. Historical runs without this context
still require terminal API proof for their existing producer artifacts; they
cannot use an empty inventory alone to establish a quiet no-target result.

The internal witness has `schema`, `run_id`, `run_attempt`, `head_sha`, `outcome`,
`reason`, `inputs` and `receipt_sha256`. Actual no-target decisions use a null
receipt digest; replay binds the unchanged original receipt's canonical bytes.
The decision comes from producer execution, with no caller override. The
reporter also checks the relevant PR, input run or completed publisher facts.
Pending proof inputs record the observed wait; a later counterpart completion
does not turn that earlier valid decision into an incident.

Stored verified replay keeps the original receipt's `attempt`, `sequence`,
timestamp, event and all pins. The current adapter envelope is checked separately.
The reporter compares the snapshot with the immutable stored receipt, reads the
installed lock at the producer's recorded source revision and rechecks the served
package proof. Replay produces no new hub artifact, retained asset, callback or
incident. A stale targeted receipt without this replay proof still fails.

CI and E2E both reference the complete successful **pre-merge** `ci.yml` run,
including `release/ready`. The landed `ci-post.yml` run authorizes the exact
wheel/sdist bytes used by the existing publisher. Preview records the candidate
head/image/facts and a SHA-256 of its tested distribution identity. After the
`Release` run finishes, the coordinator rechecks the actual PR, tested base and
merge first parent, pre-merge checks, landed CI evidence, publication jobs,
served PyPI bytes, matching GitHub assets and previous/rollback identity. Only
then can it emit `verified`, with a timestamp after the completed deploy checks.
The deploy inventory is `prepare`, `build`, `github-release`, `publish`, `verify`;
only `publish` may skip when the exact distributions are already on PyPI.

The deploy and reporting Actions runs must both have the recorded merge SHA.
If a `workflow_run` starts after unrelated main changes, the coordinator repairs
that mismatch using the immutable, verified data-package tag `v<package_version>`:
first replay `Release` with the original landed `ci_run` if needed (matching
PyPI bytes skip upload), then replay `pineforge-release.yml` with
`publication_run`. Tooling is still checked out from trusted `main`; source and
artifacts are read as data. The mismatched run emits no invalid snapshot.

Receipts contain only contract fields: numeric PR/run IDs, public check names,
commit/image/facts identities and SHA-256 deployment identities. Package file
inventories, raw distribution identifiers, URLs, secrets and diagnostics never
enter a receipt. `deployment_id_sha256` hashes compact canonical JSON containing
`package: "pineforge-data"`, the data package `version`, and the verified
`distributions` filename-to-SHA-256 map. The same contract is retained as the
immutable GitHub Release asset `release-receipt.json`, after artifact upload.
Rollback/replay checks recompute that identity from actual PyPI downloads,
matching GitHub distribution assets and the unchanged package tag/source.

A previous publication must have verifiable tagged-source manifest/facts
provenance, wheel and sdist bytes and matching immutable GitHub assets. A package
from before manifest adoption cannot supply the contract's previous facts
identity. Establish a manifest-based baseline before enabling the `data` role;
missing previous provenance fails before automatic publication. The adapter
never fills in synthetic commits/hashes or treats unknown rollback state as
verified. Automatic rollback still requires a stored **verified** receipt.

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

The configured owner receives one reused GitHub incident per event, with private
run and PR links. Full diagnostics stay in the consumer's Actions log. After
successful artifact upload, a separate hub-only App token POSTs
`repos/pineforge-4pass/pineforge-release/dispatches` with exactly:

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

`run_id` identifies the coordinator run containing the artifact, never the CI
or publication run. The event type is fixed by the hub; no configurable legacy
callback format remains. Upload failure prevents callback. Callback failures
alert locally; hub polling can still collect the immutable artifact. Reporting
errors use the diagnostic-only `incident` command, which cannot write a receipt
or enable transport. Upstream failures retain their actual stage; delivery
failures use `receipt-transport`. A previously verified target never emits a later
failed/superseded/progress receipt; local incidents still record those errors.
Delivery diagnostics keep the collected event and PR when the evidence supports
them. API or collection errors instead reach a run-scoped incident with the
collection error recorded in the Actions summary. Failure to deliver the incident
itself remains an error.

Offline CLI controls cover manual upstream workflows, placeholder claims,
ordinary main pushes, pending proof pairs, package bumps/publication, repair
handoffs, stored replay, delayed job status and evidence refusals. GitHub,
PyPI, GHCR and git observations use fixtures. Such controls demonstrate only
the validators they actually execute; they do not establish hosted workflow,
native, provider, publication or callback acceptance.

The required hub `RELEASE_RECEIPT_POLICY.data` entry is checked into
[`receipt-policy.json`](https://github.com/pineforge-4pass/pineforge-data/blob/main/tests/fixtures/release_chain/receipt-policy.json).
Both CI and E2E inventories include Python 3.11/3.12/3.13, Release contract,
Docker runtime, Database E2E, Distribution and release/ready, with zero approved
skips. The reporting workflow is `pineforge-release.yml`, artifact
`release-receipt-data`, base `main`; deploy uses `release.yml` and the inventory
above. A complete synthetic verified receipt is in
[`sample-release-receipt.json`](https://github.com/pineforge-4pass/pineforge-data/blob/main/tests/fixtures/release_chain/sample-release-receipt.json).
`tests/test_consumer_receipt.py` builds it through the coordinator with fixed
candidate, main, local-lock, OCI, served and previous proof boundaries. That
fixture tests orchestration and the hub's field, GitHub-check, identity, artifact
and signal checks vendored in `tests/hub_receipt_validator.py`; it cannot prove
the replaced publication gates. The sibling hub repo is unavailable in
standalone CI, so the snapshot records its source hash. Controls that stop at a
candidate boundary likewise prove only the path up to that boundary.

The event-driven or manually dispatched reconciler repairs dropped ready events and missing publication
transitions, reuses verified receipts without republishing, and refuses to
replace the tested dependency with a moving image alias. Actions write access
is used only on this consumer to replay its existing `Release` workflow after
a complete successful landed CI run. It never reruns producer bump logic.

For repair, dispatch `PineForge release adapter` to reconcile. To replay a
publication explicitly, dispatch `Release` from `main` with `ci_run` set to the
successful landed main CI run ID. Leave the other optional input empty. To
publish an ordinary data change, leave `ci_run` empty and choose a semantic
`bump`; the App opens a version PR under the same gates. Use **Re-run all jobs**
for a CI or publication retry: evidence from earlier attempts cannot fill a
missing job in the current attempt.
The periodic schedule remains disabled in `pineforge-release.yml`.

## Rollback

Dispatch `Release` from `main` with `rollback_package` set to a previously
verified data package version and `ci_run` empty. The target must have a stored
verified receipt, matching PyPI bytes, an unchanged source tag, and a verified
hub manifest. The adapter opens a new data patch PR restoring that manifest and
both image defaults. The ordinary full gates, protected merge and OIDC publisher
run again. Existing releases, artifacts and tags remain intact.

The new lock records the rejected event identity; replays cannot reapply it.
A later, distinct ready release can advance normally. A package predating
manifest/receipt adoption is not a verified automatic rollback target: establish
a known-good manifest-based package during rollout. Database state and user
installations are outside this package rollback.

## Supervisor setup

Create or expose these names to this repository; never put their values in code:

| Name | Scope/type | Purpose |
|---|---|---|
| `RELEASE_APP_ID` | Repository variable (or selected org variable) | Existing GitHub App identifier |
| `RELEASE_APP_PRIVATE_KEY` | Repository secret (or selected org secret) | Mint short-lived, single-target installation tokens |
| `RELEASE_ALERT_OWNER` | Repository variable | Assignable GitHub login for incident/failure and verified-publication notices |

Install the App on this consumer and the hub. Its consumer permissions need
Contents write, Pull requests write and Actions write; the reconciliation token
is restricted to this repository. Publisher preparation requests only read
permissions. The separate hub token requests only hub Contents write for status
dispatch. Local incident reporting uses the job's default token with Issues
write, not the cross-repository App token. The App must have no main branch
ruleset/protection bypass.

Protect `main` with PR-only changes, required **`release/ready`** from GitHub
Actions, strict up-to-date checks, and all existing required checks/reviews.
Allow squash merge; preserve branches. If configured review requirements cannot
be fulfilled automatically, the merge correctly remains blocked and alerts.

Keep the PyPI Trusted Publisher configured exactly as follows:

| Field | Value |
|---|---|
| PyPI project | `pineforge-data` |
| GitHub owner | `pineforge-4pass` |
| Repository | `pineforge-data` |
| Workflow | `release.yml` |
| Environment | `pypi` |

For a new publisher use [PyPI publishing settings](https://pypi.org/manage/account/publishing/).
The supervisor must remove required human reviewers/wait approvals on the
`pypi` environment for the owner's fully automatic policy, while restricting
publication to trusted main-branch workflows. No environment settings are
changed by this code. Retain the existing GitHub Pages **Source: GitHub Actions**
setting and documentation/native-feed workflows.

The hub lane must publish the immutable manifest and matching image/facts
identities, include data in fan-out/receipt monitoring, and configure the exact
`RELEASE_RECEIPT_POLICY.data` entry before live acceptance. Hub readiness owns upstream package
artifact availability. There is no hosted data deployment target to configure.
