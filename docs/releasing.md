# Releasing to PyPI

PineForge Data publishes pure-Python wheels and source distributions through
PyPI Trusted Publishing. GitHub exchanges a short-lived OpenID Connect token
for upload permission; the repository stores no PyPI API token.

## One-time setup

Create the `pypi` GitHub environment and require a maintainer's approval before
deployment. Then configure a PyPI Trusted Publisher with these exact values:

| Field | Value |
|---|---|
| PyPI project | `pineforge-data` |
| GitHub owner | `pineforge-4pass` |
| Repository | `pineforge-data` |
| Workflow | `release.yml` |
| Environment | `pypi` |

For the first release, register a pending publisher from the
[PyPI publishing settings](https://pypi.org/manage/account/publishing/). For an
existing project, add it from that project's publishing settings. The workflow
and PyPI configuration must match exactly.

Enable GitHub Pages with **Source: GitHub Actions** in the repository Pages
settings. The documentation workflow builds pull requests strictly and deploys
the `main` branch to `https://pineforge-4pass.github.io/pineforge-data/`.

## Release checklist

1. Merge every intended code and documentation change to `main`.
2. Run the complete local checks when preparing substantial release changes:

   ```bash
   python -m pip install -e '.[dev,ccxt,database,server,docs,release]'
   ruff check .
   mypy src
   pytest
   mkdocs build --strict
   python -m build
   python -m twine check dist/*
   ```

3. Open the repository's **Actions** tab, select **Release**, and choose
   **Run workflow** from `main`.
4. Select the semantic version component:

   | Choice | Example |
   |---|---|
   | `patch` | `1.2.3` → `1.2.4` |
   | `minor` | `1.2.3` → `1.3.0` |
   | `major` | `1.2.3` → `2.0.0` |

5. The workflow creates and squash-merges a version-only PR, validates the
   merged source, and creates the matching `v<version>` GitHub Release.
6. Review the generated wheel, source distribution, and release notes, then
   approve the protected `pypi` environment deployment.
7. Confirm the version and project links on PyPI and install it into a clean
   environment.

## Workflow safeguards

The release workflow is manually dispatched and serialized so two version
bumps cannot race. It refuses dispatches from branches other than `main`. Since
the organization requires changes to enter `main` through a pull request, the
workflow creates a dedicated release PR instead of pushing directly.

The generated notes include every merged PR since the previous GitHub Release.
Closed-but-unmerged PRs are intentionally excluded because their changes were
not shipped. The mechanical version-bump PR carries `skip-changelog` and is
also excluded. Labels group breaking changes, features, fixes, documentation,
and all other merged work.

Before upload, the workflow verifies that the prospective tag exactly matches
`v` plus `project.version`, runs static checks, unit tests, and Docker
integration tests, builds the wheel and source archive once, validates their
long-description metadata, and installs the wheel in a clean virtual
environment. Only then does it create the GitHub Release and request approval
for PyPI. The publish job receives the validated artifacts and has only
`id-token: write` permission.

Reruns reuse an already merged bump or tag. If the version is already present
on PyPI, build and publication jobs are skipped instead of attempting a
duplicate immutable upload.

PyPI versions are immutable. If a release is wrong, increment the package
version and publish a correction; do not attempt to replace an existing file.
