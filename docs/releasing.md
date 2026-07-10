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

1. Update `project.version` in `pyproject.toml` using a valid Python package
   version.
2. Update user-facing documentation and merge the release changes to `main`.
3. Run the complete local checks:

   ```bash
   python -m pip install -e '.[dev,ccxt,database,server,docs,release]'
   ruff check .
   mypy src
   pytest
   mkdocs build --strict
   python -m build
   python -m twine check dist/*
   ```

4. Create a GitHub Release whose tag is exactly `v<project.version>`, such as
   `v0.1.0`.
5. Review the generated wheel and source-distribution artifact, then approve
   the `pypi` environment deployment.
6. Confirm the version and project links on PyPI and install it into a clean
   environment.

## Workflow safeguards

The release workflow runs only for a published GitHub Release. Before upload,
it verifies that the tag exactly matches `v` plus `project.version`, builds the
wheel and source archive once, validates their long-description metadata, and
installs the wheel in a clean virtual environment. The publish job receives
only those validated artifacts and has only `id-token: write` permission.

PyPI versions are immutable. If a release is wrong, increment the package
version and publish a correction; do not attempt to replace an existing file.
