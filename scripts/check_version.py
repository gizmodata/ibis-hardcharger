#!/usr/bin/env python3
"""The release's version, from the one place it lives (pyproject.toml), checked against
everything that must agree with it. Exits non-zero, saying why, when something doesn't.

- CHANGELOG.md has a ``## [X.Y.Z] - YYYY-MM-DD`` section for it, which is not empty;
- on a tag (``GITHUB_REF=refs/tags/...``, or ``--tag vX.Y.Z``), the tag is ``vX.Y.Z``;
- with ``--dist DIR``, the sdist and wheel in DIR are that version's;
- with ``--release-notes FILE``, the CHANGELOG section is written to FILE.

Prints the version.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def changelog_section(*, changelog: str, version: str) -> str | None:
    """The body of the ``## [version] - date`` section, or None when there is none."""
    heading = re.compile(
        pattern=rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}[ \t]*$",
        flags=re.MULTILINE,
    )
    match = heading.search(string=changelog)
    if match is None:
        return None
    rest = changelog[match.end() :]
    following = re.search(pattern=r"^## \[", string=rest, flags=re.MULTILINE)
    return rest[: following.start()] if following else rest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", help="the release tag (default: GITHUB_REF when it is a tag)")
    parser.add_argument(
        "--dist", type=pathlib.Path, help="directory holding the built sdist and wheel"
    )
    parser.add_argument(
        "--release-notes", type=pathlib.Path, help="write the CHANGELOG section here"
    )
    args = parser.parse_args()

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    errors = []

    if re.fullmatch(pattern=r"\d+\.\d+\.\d+", string=version) is None:
        errors.append(f"pyproject.toml's version {version!r} is not X.Y.Z")

    tag = args.tag
    ref = os.environ.get("GITHUB_REF", "")
    if tag is None and ref.startswith("refs/tags/"):
        tag = ref.removeprefix("refs/tags/")
    if tag is not None and tag != f"v{version}":
        errors.append(f"the tag is {tag}, but pyproject.toml says {version} (expected v{version})")

    section = changelog_section(changelog=(ROOT / "CHANGELOG.md").read_text(), version=version)
    if section is None:
        errors.append(f"CHANGELOG.md has no '## [{version}] - YYYY-MM-DD' section")
    elif not section.strip():
        errors.append(f"CHANGELOG.md's section for {version} is empty")
    elif args.release_notes is not None:
        args.release_notes.write_text(section.strip() + "\n")

    if args.dist is not None:
        expected = {
            f"ibis_hardcharger-{version}.tar.gz",
            f"ibis_hardcharger-{version}-py3-none-any.whl",
        }
        found = {path.name for path in args.dist.iterdir()} if args.dist.is_dir() else set()
        if found != expected:
            errors.append(f"{args.dist} holds {sorted(found)}, expected {sorted(expected)}")

    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
