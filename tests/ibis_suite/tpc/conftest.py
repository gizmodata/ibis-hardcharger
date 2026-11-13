"""Ibis's TPC-H and TPC-DS tests: each query written as an Ibis expression must return what
the benchmark's SQL returns. The hook below is Ibis's; it passes `backend` to the tests."""

from __future__ import annotations

import numbers

import ibis.backends.tests.tpc.conftest as ibis_tpc
import pytest
from ibis.backends.tests.tpc.conftest import pytest_pyfunc_call  # noqa: F401


def compare_tpc_results(result, expected, result_is_empty=False, abs_tol=0.001):
    """Ibis's comparison, with pytest.approx() given only numbers.

    Ibis's version passes every column to ``pytest.approx(..., nan_ok=True)``, which pytest 9
    refuses for a column of dates (TPC-H 3 and 18, TPC-DS 51): "pytest.approx() does not
    support nan_ok for datetime/timedelta comparisons". A column that isn't numeric must be
    equal, which is what approx() did with it before.
    """
    assert (result_is_empty and expected.empty) or (not result_is_empty and not expected.empty)

    assert len(expected) == len(result)
    assert result.columns.tolist() == expected.columns.tolist()
    for column in result.columns:
        left = result.loc[:, column].values.tolist()
        right = expected.loc[:, column].values.tolist()
        if all(isinstance(value, numbers.Number) for value in (*left, *right)):
            assert pytest.approx(left, nan_ok=True, abs=abs_tol) == right, column
        else:
            assert left == right, column


ibis_tpc.compare_tpc_results = compare_tpc_results
