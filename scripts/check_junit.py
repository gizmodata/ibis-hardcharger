#!/usr/bin/env python3
"""Check a pytest JUnit report: enough tests passed, and none failed or errored.

pytest's exit code already fails a run with failures, errors or no tests at all; this adds a
floor, so a run that quietly collected a fraction of the suite (a directory not found, a
conftest that deselected too much) fails too.

    python scripts/check_junit.py report.xml --min-passed 2000
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report")
    parser.add_argument("--min-passed", type=int, required=True)
    args = parser.parse_args()

    root = ET.parse(source=args.report).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = {
        name: sum(int(suite.get(name, "0")) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }
    # pytest counts an expected failure (xfail) as skipped in JUnit.
    passed = total["tests"] - total["failures"] - total["errors"] - total["skipped"]
    print(
        f"{passed} passed, {total['failures']} failed, {total['errors']} errors, {total['skipped']} skipped or expected to fail"
    )
    problems = []
    if total["failures"] or total["errors"]:
        problems.append("there are failures or errors")
    if passed < args.min_passed:
        problems.append(f"only {passed} tests passed, expected at least {args.min_passed}")
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
