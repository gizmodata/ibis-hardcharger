# Releasing ibis-hardcharger

Releases follow [semantic versioning](https://semver.org) and [CHANGELOG.md](../CHANGELOG.md).
A `vX.Y.Z` tag runs the whole of `.github/workflows/ci.yml` on the tagged commit (lint, unit
tests, the live suites on Python 3.11 to 3.15, the lowest dependency versions, the README's
examples), builds the sdist and wheel, publishes them to PyPI with Trusted Publishing and
attestations, and creates the GitHub Release with the tag's CHANGELOG section as its notes.
Nothing is published unless every job passed on that commit.

## One-time setup

### PyPI

At https://pypi.org/manage/account/publishing/ add a **pending GitHub publisher**:

| Field | Exact value |
| --- | --- |
| PyPI project name | `ibis-hardcharger` |
| GitHub owner | `gizmodata` |
| Repository name | `ibis-hardcharger` |
| Workflow filename | `ci.yml` |
| Environment name | `release` |

Enter only the workflow filename, not `.github/workflows/ci.yml`. No API token is needed. A
pending publisher creates the PyPI project on the first successful upload; it does not reserve
the name before that. After the first publication, manage it at
https://pypi.org/manage/project/ibis-hardcharger/settings/publishing/.

References: https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/ and
https://docs.pypi.org/trusted-publishers/adding-a-publisher/.

### GitHub

- An environment named `release` (Settings → Environments), limited to `v*` tags, with a
  required reviewer if a person should approve each publish.
- The tests download `hardcharger-server` from the releases of `gizmodata/hardcharger` with the
  workflow's own token (`HARDCHARGER_INSTALL_TOKEN: ${{ github.token }}`). While that
  repository is internal, it must allow this repository's workflows to read it.

## Before the first tag

The publish and release jobs run only on tags, so a first run is their first test. Run the
workflow once from the Actions tab (`workflow_dispatch`) on the commit to be tagged: every job
up to `build` must pass, and `build` uploads the sdist and wheel as an artifact to look at.

## Release

1. Move the entries under `## [Unreleased]` in `CHANGELOG.md` into `## [X.Y.Z] - YYYY-MM-DD`,
   and set `version` in `pyproject.toml` (the only place the version lives).
   `python scripts/check_version.py --tag vX.Y.Z` checks the two agree.
2. Commit, push `main`, and wait for CI to pass on that commit.
3. Confirm the trusted publisher above is configured.
4. Tag that commit and push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. When the run finishes, check that https://pypi.org/project/ibis-hardcharger/ shows the new
   version, and that `pip install ibis-hardcharger==X.Y.Z` works in a fresh environment.

A failed upload can be retried by re-running the failed job. Never move a tag whose version was
published; a fix ships as the next version. A tag whose run failed before anything was published
can be moved to the fixed commit.
