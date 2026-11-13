#!/usr/bin/env bash
# Fetch the Ibis project's shared test data (https://github.com/ibis-project/testing-data) at a
# pinned commit into ci/ibis-testing-data, the directory Ibis's backend test suite reads.
#
# Only the directories the Hardcharger suite loads are checked out (parquet, csv, tpch, tpcds:
# about 190 MB of the repository's 490 MB). git verifies every object against the pinned commit,
# so the data can't change under the tests. Re-running is a no-op once the commit is checked out.
set -euo pipefail

REPO_URL="https://github.com/ibis-project/testing-data.git"
# master as of 2024-08-01, the data Ibis 12.0.0's own CI runs against.
COMMIT="b26bd40cf29004372319df620c4bbe41420bb6f8"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${1:-$root/ci/ibis-testing-data}"

if [ -d "$dest/.git" ] && [ "$(git -C "$dest" rev-parse HEAD 2>/dev/null)" = "$COMMIT" ] \
  && [ -f "$dest/parquet/functional_alltypes.parquet" ]; then
  echo "ibis testing data already at $COMMIT in $dest"
  exit 0
fi

rm -rf "$dest"
mkdir -p "$dest"
git -C "$dest" init --quiet
git -C "$dest" remote add origin "$REPO_URL"
git -C "$dest" config core.sparseCheckout true
git -C "$dest" sparse-checkout set --no-cone /parquet/ /csv/ /tpch/ /tpcds/
git -C "$dest" fetch --quiet --depth 1 --filter=blob:none origin "$COMMIT"
git -C "$dest" checkout --quiet --detach FETCH_HEAD

if [ "$(git -C "$dest" rev-parse HEAD)" != "$COMMIT" ]; then
  echo "ibis testing data: expected $COMMIT, got $(git -C "$dest" rev-parse HEAD)" >&2
  exit 1
fi
if [ ! -f "$dest/parquet/functional_alltypes.parquet" ]; then
  echo "ibis testing data: parquet/functional_alltypes.parquet is missing in $dest" >&2
  exit 1
fi
echo "ibis testing data at $COMMIT in $dest"
