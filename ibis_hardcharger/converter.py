"""Convert Hardcharger's Arrow results to what an Ibis expression's schema promises."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pyarrow as pa
from ibis.formats.pandas import PandasData
from ibis.formats.pyarrow import PyArrowData, PyArrowType

if TYPE_CHECKING:
    import ibis.expr.datatypes as dt
    import ibis.expr.schema as sch

# Arrow's month_day_nano_interval, as it lies in memory.
_MONTH_DAY_NANO = np.dtype([("months", "<i4"), ("days", "<i4"), ("nanoseconds", "<i8")])
_NANOSECONDS_PER_DAY = 86_400_000_000_000
_NANOSECONDS_PER = {"s": 1_000_000_000, "ms": 1_000_000, "us": 1_000, "ns": 1}


class MonthsInIntervalError(ValueError):
    """An INTERVAL with months has no fixed length, so it is not a duration."""


def interval_to_duration(column: pa.ChunkedArray | pa.Array, unit: str) -> pa.ChunkedArray:
    """An INTERVAL column (Arrow ``month_day_nano_interval``, as the server sends it) as the
    Arrow ``duration`` Ibis's interval type stands for. A day is 24 hours, as in DuckDB.

    Raises :class:`MonthsInIntervalError` when a value has months: their length depends on the
    date they are added to.
    """
    chunks = column.chunks if isinstance(column, pa.ChunkedArray) else [column]
    per_unit = _NANOSECONDS_PER[unit]
    converted = []
    for chunk in chunks:
        values = np.frombuffer(chunk.buffers()[1], dtype=_MONTH_DAY_NANO)[
            chunk.offset : chunk.offset + len(chunk)
        ]
        valid = chunk.is_valid().to_numpy(zero_copy_only=False)
        if np.any(values["months"][valid] != 0):
            raise MonthsInIntervalError(
                "an INTERVAL value with months has no fixed length, so it can't be returned as "
                "a duration: cast it to a string, or take its parts (months, days, ...)"
            )
        nanoseconds = values["days"].astype(np.int64) * _NANOSECONDS_PER_DAY + values["nanoseconds"]
        converted.append(pa.array(nanoseconds // per_unit, mask=~valid, type=pa.duration(unit)))
    return pa.chunked_array(converted, type=pa.duration(unit))


def durations_for_intervals(table: pa.Table, schema: sch.Schema) -> pa.Table:
    """`table` with each INTERVAL column that is a fixed length of time as a duration; a
    column with months in it is left as the server sent it."""
    for index, (name, dtype) in enumerate(schema.items()):
        column = table.column(index)
        if dtype.is_interval() and pa.types.is_interval(column.type):
            desired = PyArrowType.from_ibis(dtype)
            if pa.types.is_duration(desired):
                try:
                    column = interval_to_duration(column=column, unit=desired.unit)
                except MonthsInIntervalError:
                    continue
                table = table.set_column(index, pa.field(name, column.type), column)
    return table


class HardchargerPandasData(PandasData):
    @staticmethod
    def convert_Array(s, dtype, pandas_type):
        return s.replace(float("nan"), None)


class HardchargerPyArrowData(PyArrowData):
    @classmethod
    def convert_scalar(cls, scalar: pa.Scalar, dtype: dt.DataType) -> pa.Scalar:
        if dtype.is_null():
            return pa.scalar(None)
        if dtype.is_interval() and pa.types.is_interval(scalar.type):
            desired = PyArrowType.from_ibis(dtype)
            if pa.types.is_duration(desired):
                return interval_to_duration(
                    column=pa.array([scalar.as_py()], type=scalar.type), unit=desired.unit
                )[0]
        return super().convert_scalar(scalar, dtype)

    @classmethod
    def convert_column(cls, column: pa.Array, dtype: dt.DataType) -> pa.Array:
        if dtype.is_null():
            return pa.nulls(len(column))
        if dtype.is_interval() and pa.types.is_interval(column.type):
            desired = PyArrowType.from_ibis(dtype)
            if pa.types.is_duration(desired):
                return interval_to_duration(column=column, unit=desired.unit)
        return super().convert_column(column, dtype)
