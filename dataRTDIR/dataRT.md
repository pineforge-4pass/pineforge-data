# CR-171 data runner transition

Completed 2026-10-18 in repository `data`, branch `core/cr171-ci-spot`, from base `50f282d`.

All six workflows were reviewed. The 19 jobs that select Linux x64 runners now use the supplied public fragment and the exact job-level `PF_CI_LABELS` string. The native-feed release matrix retains its three OS values; only its Linux x64 leg gets an additional runner field. Its ARM and macOS legs retain their original hosted runner labels.

Source of truth: `~/.local/state/pineforge-super-top/ci-spot-labels-1018.txt` and `git -C ~/code/pf-ci-spot show origin/main:workflows/public.yml`.

`public` in the tables below means this expression, copied verbatim with only its surrounding YAML indentation adjusted:

```yaml
runs-on: >-
  ${{ github.event_name != 'pull_request_target' &&
      contains(fromJSON('["push","pull_request","workflow_dispatch","schedule","release"]'), github.event_name) &&
      (github.event_name != 'pull_request' ||
       (github.event.pull_request.head.repo.full_name == github.repository && github.event.pull_request.head.repo.fork == false)) &&
      fromJSON('["self-hosted","pf-ci","pf-ci-x64"]') || 'ubuntu-latest' }}
env:
  PF_CI_LABELS: '["self-hosted","pf-ci","pf-ci-x64"]'
```

The expression selects `["self-hosted","pf-ci","pf-ci-x64"]` for its supported event/repository cases and `ubuntu-latest` otherwise. The dispatcher actor allowlist still applies: `luisleo526` and `pineforge-app[bot]`, with `github-actions[bot]` allowed only for chain triggers. No dispatcher policy or actor list was changed. The fragment itself has no actor condition.

No Dependabot configuration or Dependabot-specific job/actor condition exists under `.github`; no workflow has a `pull_request_target` trigger. Hosted fallbacks for fork PRs and excluded events remain those in the supplied fragment.

Workflow `.github/workflows/ci.yml` (triggers: main push, pull request):

| Job | Before runs-on | After runs-on | Hosted cases and reason |
| --- | --- | --- | --- |
| `test` (Python 3.11, 3.12, 3.13) | `ubuntu-latest` | `public` for every existing Python matrix leg | Fork PRs use `ubuntu-latest` because the public fragment excludes fork PRs. |
| `docker` | `ubuntu-latest` | `public` | Fork PRs use the public hosted fallback. |
| `database-e2e` | `ubuntu-latest` | `public` | Fork PRs use the public hosted fallback. Existing job env was merged, retaining every database setting. |
| `contract` | `ubuntu-latest` | `public` | Fork PRs use the public hosted fallback. |
| `distribution` | `ubuntu-latest` | `public` | Fork PRs use the public hosted fallback. |
| `ready` | `ubuntu-latest` | `public` | Fork PRs use the public hosted fallback. |

Workflow `.github/workflows/docs.yml` (triggers: main push, pull request, workflow dispatch):

| Job | Before runs-on | After runs-on | Hosted cases and reason |
| --- | --- | --- | --- |
| `build` | `ubuntu-latest` | `public` | Fork PRs use `ubuntu-latest` because the public fragment excludes fork PRs. |
| `deploy` | `ubuntu-latest` | `public` | Its existing condition skips PRs. Configured push/manual invocations select ci-spot; the fragment retains its hosted fallback for excluded events. |

Workflow `.github/workflows/native-feed.yml` (triggers: path-filtered push/pull request, workflow dispatch):

| Job / matrix leg | Before runs-on | After runs-on | Hosted cases and reason |
| --- | --- | --- | --- |
| `release`, `os=ubuntu-24.04` (Linux x64) | `matrix.os` → `ubuntu-24.04` | `public` through `matrix.runner` | Fork PRs use the fragment's `ubuntu-latest` fallback. |
| `release`, `os=ubuntu-24.04-arm` | `matrix.os` → `ubuntu-24.04-arm` | `ubuntu-24.04-arm`, unchanged | ARM remains hosted because the ci-spot ARM class is disabled. |
| `release`, `os=macos-14` | `matrix.os` → `macos-14` | `macos-14`, unchanged | macOS remains hosted as instructed. |
| `asan-ubsan` | `ubuntu-24.04` | `public` | Fork PRs use the fragment's `ubuntu-latest` fallback. |
| `tsan` | `ubuntu-24.04` | `public` | Fork PRs use the fragment's `ubuntu-latest` fallback. |

The release matrix still has `os: [ubuntu-24.04, ubuntu-24.04-arm, macos-14]`. A single `include` entry augments `os=ubuntu-24.04` with `runner: <public expression>`. The job's selector becomes `${{ matrix.runner || matrix.os }}`. The other two legs have no `matrix.runner`, so they retain `matrix.os`. The required `PF_CI_LABELS` is job-level and therefore also present on the hosted matrix legs; it does not change their runner selection.

Workflow `.github/workflows/pineforge-release.yml` (PineForge release adapter; triggers: repository dispatch, workflow run, workflow dispatch):

| Job | Before runs-on | After runs-on | Hosted cases and reason |
| --- | --- | --- | --- |
| `reconcile` | `ubuntu-latest` | `public` | `repository_dispatch` and `workflow_run` remain on `ubuntu-latest` because neither event is accepted by the supplied fragment. `workflow_dispatch` selects ci-spot. |
| `report` | No runs-on; calls `./.github/workflows/release-report.yml` | Same reusable-workflow call, unchanged | Runner selection is delegated to `release-report.yml` and uses the caller's event context: repository-dispatch/workflow-run calls stay hosted; manual calls select ci-spot. |

The adapter has no schedule after #122 (base commit `50f282d`). Its existing triggers, conditions, and explanatory comments remain unchanged. Its remaining triggers are trusted as stated in the brief; the exact fragment still excludes the two automatic event types above.

Workflow `.github/workflows/release-report.yml` (trigger: workflow call):

| Job | Before runs-on | After runs-on | Hosted cases and reason |
| --- | --- | --- | --- |
| `report` | `ubuntu-latest` | `public`, evaluated using the caller's GitHub context | Current callers triggered by `workflow_run` or `repository_dispatch` use `ubuntu-latest`; callers triggered by `workflow_dispatch` select ci-spot. The reusable workflow's `workflow_call` declaration does not replace the caller's event context. |

Workflow `.github/workflows/release.yml` (triggers: workflow dispatch, completed main CI workflow run):

| Job | Before runs-on | After runs-on | Hosted cases and reason |
| --- | --- | --- | --- |
| `manual-pr` | `ubuntu-latest` | `public` | Its existing condition allows only manual invocations, which select ci-spot. |
| `prepare` | `ubuntu-latest` | `public` | Automatic `workflow_run` invocations remain on `ubuntu-latest` because the fragment excludes that event. Manual replay selects ci-spot. |
| `build` | `ubuntu-latest` | `public` | Automatic workflow-run invocations remain hosted; eligible manual replay selects ci-spot. |
| `github-release` | `ubuntu-latest` | `public` | Automatic workflow-run invocations remain hosted; eligible manual replay selects ci-spot. |
| `publish` | `ubuntu-latest` | `public` | Automatic workflow-run invocations remain hosted; eligible manual replay selects ci-spot. |
| `verify` | `ubuntu-latest` | `public` | Automatic workflow-run invocations remain hosted; eligible manual replay selects ci-spot. |
| `report` | No runs-on; calls `./.github/workflows/release-report.yml` | Same reusable-workflow call, unchanged | Runner selection is delegated to `release-report.yml`: workflow-run calls stay hosted; manual calls select ci-spot. |

Static verification:

- Compared all 19 embedded expressions against the source fragment, preserving the expression text and relative indentation.
- Confirmed each runner job has exactly one job-level env block and one exact `PF_CI_LABELS` value. The existing `database-e2e` env was merged; release workflow-level `PYTHONPATH` and all step-level env blocks are unchanged.
- Compared every job against the base source. Only the runner expressions, required job env additions, and native Linux x64 matrix runner field changed. Steps, triggers, timeouts, job conditions, permissions, services, dependencies, and pre-existing matrix values are unchanged. Both reusable-workflow caller jobs are unchanged.
- `git diff --check` passed for the workflow changes.
- No builds, tests, repository scripts, or workflows were executed. Git commit hooks were disabled for these local commits. No push was performed.
- Every pre-existing file was retained. Additions are limited to this report and `dataRTDIR/DONE`.

One local commit was created per workflow file:

| Workflow | Commit |
| --- | --- |
| `.github/workflows/ci.yml` | `2a2e19f` |
| `.github/workflows/docs.yml` | `1ab66a4` |
| `.github/workflows/native-feed.yml` | `4e14299` |
| `.github/workflows/pineforge-release.yml` | `6b631f5` |
| `.github/workflows/release-report.yml` | `453194e` |
| `.github/workflows/release.yml` | `1b73060` |

The report and completion marker are recorded in a separate documentation commit.
