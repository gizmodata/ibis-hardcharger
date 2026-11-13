"""Every type the backend supports, in a nullable column, a NOT NULL column and an empty table.

For each: the Ibis type the backend reports, the Arrow type and values of ``to_pyarrow()``,
the pandas values of ``execute()``, and a round trip of the Arrow result back into a new
table through the bulk load (``create_table``) and through an in-memory table.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from dataclasses import dataclass
from typing import Any

import adbc_driver_manager
import ibis
import ibis.expr.datatypes as dt
import pandas as pd
import pyarrow as pa
import pytest

from ibis_hardcharger.converter import MonthsInIntervalError

UTC = datetime.UTC
_SAME = object()


@dataclass(frozen=True)
class Case:
    sql_type: str  # the DuckDB column type
    literal: str  # a value of it, as SQL
    ibis_type: str  # the Ibis type the backend reports for the column
    arrow_type: pa.DataType  # the Arrow type of to_pyarrow()
    arrow_value: Any  # the value in to_pyarrow().to_pylist()
    pandas_value: Any = _SAME  # the value in execute(), where it differs

    @property
    def expected_pandas(self) -> Any:
        return self.arrow_value if self.pandas_value is _SAME else self.pandas_value


CASES = {
    "boolean": Case("BOOLEAN", "TRUE", "boolean", pa.bool_(), True),
    "int8": Case("TINYINT", "-128", "int8", pa.int8(), -128),
    "int16": Case("SMALLINT", "-32768", "int16", pa.int16(), -32768),
    "int32": Case("INTEGER", "-2147483648", "int32", pa.int32(), -2147483648),
    "int64": Case("BIGINT", "-4611686018427387904", "int64", pa.int64(), -(2**62)),
    "uint8": Case("UTINYINT", "255", "uint8", pa.uint8(), 255),
    "uint16": Case("USMALLINT", "65535", "uint16", pa.uint16(), 65535),
    "uint32": Case("UINTEGER", "4294967295", "uint32", pa.uint32(), 4294967295),
    # Above the largest BIGINT, and a float64 holds it exactly (pandas shows a nullable
    # integer column that has a NULL as float64).
    "uint64": Case("UBIGINT", "13835058055282163712", "uint64", pa.uint64(), 3 * 2**62),
    "hugeint": Case(
        "HUGEINT",
        "1000000000000000000000000000000",
        "decimal(38, 0)",
        pa.decimal128(38, 0),
        decimal.Decimal(10**30),
    ),
    "float32": Case("FLOAT", "1.5", "float32", pa.float32(), 1.5),
    "float64": Case("DOUBLE", "-2.25", "float64", pa.float64(), -2.25),
    "decimal": Case(
        "DECIMAL(18, 3)",
        "12345.678",
        "decimal(18, 3)",
        pa.decimal128(18, 3),
        decimal.Decimal("12345.678"),
    ),
    "decimal38": Case(
        "DECIMAL(38, 10)",
        "1234567890123456789012345678.0123456789",
        "decimal(38, 10)",
        pa.decimal128(38, 10),
        decimal.Decimal("1234567890123456789012345678.0123456789"),
    ),
    "string": Case("VARCHAR", "'héllo, wörld'", "string", pa.string(), "héllo, wörld"),
    "binary": Case("BLOB", r"'\x00\x01\xFF'::BLOB", "binary", pa.binary(), b"\x00\x01\xff"),
    "date": Case(
        "DATE",
        "DATE '2024-02-29'",
        "date",
        pa.date32(),
        datetime.date(2024, 2, 29),
        pd.Timestamp("2024-02-29"),
    ),
    "time": Case(
        "TIME",
        "TIME '12:34:56.789012'",
        "time",
        pa.time64("ns"),
        datetime.time(12, 34, 56, 789012),
    ),
    "timestamp": Case(
        "TIMESTAMP",
        "TIMESTAMP '2024-02-29 12:34:56.789012'",
        "timestamp(6)",
        pa.timestamp("us"),
        datetime.datetime(2024, 2, 29, 12, 34, 56, 789012),
    ),
    "timestamp_s": Case(
        "TIMESTAMP_S",
        "TIMESTAMP_S '2024-02-29 12:34:56'",
        "timestamp(0)",
        pa.timestamp("s"),
        datetime.datetime(2024, 2, 29, 12, 34, 56),
    ),
    "timestamp_ms": Case(
        "TIMESTAMP_MS",
        "TIMESTAMP_MS '2024-02-29 12:34:56.789'",
        "timestamp(3)",
        pa.timestamp("ms"),
        datetime.datetime(2024, 2, 29, 12, 34, 56, 789000),
    ),
    "timestamp_ns": Case(
        "TIMESTAMP_NS",
        "TIMESTAMP_NS '2024-02-29 12:34:56.789012345'",
        "timestamp(9)",
        pa.timestamp("ns"),
        pd.Timestamp("2024-02-29 12:34:56.789012345"),
    ),
    "timestamptz": Case(
        "TIMESTAMP WITH TIME ZONE",
        # 18:04:56 in the server's own zone, Asia/Kolkata: the backend's session is in UTC.
        "TIMESTAMPTZ '2024-02-29 18:04:56.789012+05:30'",
        "timestamp('UTC', 6)",
        pa.timestamp("us", tz="UTC"),
        datetime.datetime(2024, 2, 29, 12, 34, 56, 789012, tzinfo=UTC),
    ),
    "interval": Case(
        "INTERVAL",
        "INTERVAL '3 days 4 hours 5 microseconds'",
        "interval('us')",
        pa.duration("us"),
        datetime.timedelta(days=3, hours=4, microseconds=5),
    ),
    "uuid": Case(
        "UUID",
        "'0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0'::UUID",
        "uuid",
        pa.string(),
        "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
        uuid.UUID("0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"),
    ),
    "json": Case(
        "JSON",
        """'{"a": [1, 2], "b": null}'::JSON""",
        "json",
        pa.string(),
        '{"a": [1, 2], "b": null}',
        {"a": [1, 2], "b": None},
    ),
    "array": Case("BIGINT[]", "[1, NULL, 3]", "array<int64>", pa.list_(pa.int64()), [1, None, 3]),
    "fixed_array": Case(
        "DOUBLE[3]",
        "[1.5, 2.5, 3.5]",
        "array<float64, 3>",
        pa.list_(pa.float64(), 3),
        [1.5, 2.5, 3.5],
    ),
    "struct": Case(
        "STRUCT(a BIGINT, b VARCHAR)",
        "{'a': 1, 'b': 'x'}",
        "struct<a: int64, b: string>",
        pa.struct([("a", pa.int64()), ("b", pa.string())]),
        {"a": 1, "b": "x"},
    ),
    "map": Case(
        "MAP(VARCHAR, BIGINT)",
        "MAP {'k': 1, 'l': 2}",
        "map<string, int64>",
        pa.map_(pa.string(), pa.int64()),
        [("k", 1), ("l", 2)],
        {"k": 1, "l": 2},
    ),
    "nested": Case(
        "STRUCT(xs DOUBLE[], m MAP(VARCHAR, DATE), deep VARCHAR[][])",
        "{'xs': [1.5], 'm': MAP {'d': DATE '2024-01-01'}, 'deep': [['a'], []]}",
        "struct<xs: array<float64>, m: map<string, date>, deep: array<array<string>>>",
        pa.struct(
            [
                ("xs", pa.list_(pa.float64())),
                ("m", pa.map_(pa.string(), pa.date32())),
                ("deep", pa.list_(pa.list_(pa.string()))),
            ]
        ),
        {"xs": [1.5], "m": [("d", datetime.date(2024, 1, 1))], "deep": [["a"], []]},
        {"xs": [1.5], "m": {"d": datetime.date(2024, 1, 1)}, "deep": [["a"], []]},
    ),
    # Types DuckDB has and Ibis reads as one of its own.
    "enum": Case("mood", "'happy'", "string", pa.string(), "happy"),
    "timetz": Case(
        "TIME WITH TIME ZONE",
        "TIMETZ '12:34:56+02'",
        "time",
        pa.time64("ns"),
        datetime.time(12, 34, 56),
    ),
}

STATES = ("nullable", "not_null", "empty")


def arrow_types_equal(actual: pa.DataType, expected: pa.DataType) -> bool:
    """Equal Arrow types, whatever the child fields of lists and maps are called."""
    if pa.types.is_fixed_size_list(expected):
        return (
            pa.types.is_fixed_size_list(actual)
            and actual.list_size == expected.list_size
            and arrow_types_equal(actual=actual.value_type, expected=expected.value_type)
        )
    if pa.types.is_list(expected):
        return pa.types.is_list(actual) and arrow_types_equal(
            actual=actual.value_type, expected=expected.value_type
        )
    if pa.types.is_map(expected):
        return (
            pa.types.is_map(actual)
            and arrow_types_equal(actual=actual.key_type, expected=expected.key_type)
            and arrow_types_equal(actual=actual.item_type, expected=expected.item_type)
        )
    if pa.types.is_struct(expected):
        return (
            pa.types.is_struct(actual)
            and [f.name for f in actual] == [f.name for f in expected]
            and all(
                arrow_types_equal(actual=a.type, expected=e.type)
                for a, e in zip(actual, expected, strict=True)
            )
        )
    return actual == expected


@pytest.fixture
def typed_table(con, request):
    """make(case, state): a one-column table ``c`` of the case's type in that state."""

    def make(case: Case, state: str):
        if case.sql_type == "mood":
            con.raw_sql("CREATE TYPE mood AS ENUM ('sad', 'ok', 'happy')").close()
        not_null = " NOT NULL" if state == "not_null" else ""
        con.raw_sql(f"CREATE TABLE typed (c {case.sql_type}{not_null})").close()
        if state == "nullable":
            con.raw_sql(f"INSERT INTO typed VALUES ({case.literal}), (NULL)").close()
        elif state == "not_null":
            con.raw_sql(f"INSERT INTO typed VALUES ({case.literal})").close()
        return con.table("typed")

    return make


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("name", list(CASES))
def test_type(con, typed_table, name, state):
    case = CASES[name]
    t = typed_table(case=case, state=state)
    expected_values = {
        "nullable": [case.arrow_value, None],
        "not_null": [case.arrow_value],
        "empty": [],
    }[state]

    # The Ibis type, with the column's nullability.
    expected_dtype = dt.dtype(case.ibis_type, nullable=state != "not_null")
    assert t.schema() == ibis.schema({"c": expected_dtype})

    # Arrow.
    arrow = t.to_pyarrow()
    assert arrow_types_equal(actual=arrow.schema.field("c").type, expected=case.arrow_type), (
        arrow.schema.field("c").type
    )
    assert arrow["c"].to_pylist() == expected_values

    # pandas.
    df = t.execute()
    assert len(df) == len(expected_values)
    if state != "empty":
        assert df["c"].iloc[0] == case.expected_pandas
    if state == "nullable":
        assert pd.isna(df["c"].iloc[1])

    # A stream of record batches has the same schema and values.
    with con.to_pyarrow_batches(t) as reader:
        streamed = reader.read_all()
    assert streamed.schema == arrow.schema
    assert streamed["c"].to_pylist() == expected_values

    # Back in through the bulk load, into a table made from the same Ibis schema.
    loaded = con.create_table("loaded", arrow, schema=t.schema())
    assert loaded.schema() == t.schema()
    assert loaded.to_pyarrow().equals(arrow)

    # And as an in-memory table.
    memtable = ibis.memtable(arrow, schema=t.schema())
    assert con.to_pyarrow(memtable).equals(arrow)


def test_every_case_was_a_distinct_column_type():
    assert len({case.sql_type for case in CASES.values()}) == len(CASES)


def test_null_literal_column(con):
    """A column of NULLs with no type: Arrow's null type, as Ibis's schema says."""
    t = ibis.memtable({"a": [1, 2]}).mutate(n=ibis.null())
    result = con.to_pyarrow(t)
    assert result.schema.field("n").type == pa.null()
    assert result["n"].to_pylist() == [None, None]
    assert con.to_pyarrow(ibis.null()).as_py() is None


def test_scalar_results(con):
    assert con.execute(ibis.literal(decimal.Decimal("1.25"))) == decimal.Decimal("1.25")
    value = con.to_pyarrow(ibis.literal(uuid.UUID(int=7)))
    assert value.as_py() == str(uuid.UUID(int=7))
    assert con.execute(ibis.literal(uuid.UUID(int=7))) == uuid.UUID(int=7)
    assert con.execute(ibis.literal(datetime.date(2024, 2, 29))) == pd.Timestamp("2024-02-29")
    assert con.execute(ibis.literal({"a": 1})) == {"a": 1}
    assert con.execute(ibis.literal([1, 2])) == [1, 2]
    assert con.execute(ibis.literal(b"\x00\xff")) == b"\x00\xff"


def test_interval_with_months_is_refused_as_a_duration(con):
    """A month has no fixed length. pandas gets the value as the server sent it."""
    con.raw_sql(
        "CREATE TABLE spans (c INTERVAL); INSERT INTO spans VALUES (INTERVAL '1 month 2 days')"
    ).close()
    t = con.table("spans")
    with pytest.raises(MonthsInIntervalError, match="an INTERVAL value with months"):
        t.to_pyarrow()
    assert t.execute()["c"].iloc[0] == pd.DateOffset(months=1, days=2, nanoseconds=0)
    # Its parts, and its text, are there to be asked for.
    assert con.execute(t.c.cast("string").max()) == "1 month 2 days"


def test_hugeint_beyond_38_digits_is_refused_by_the_server(con):
    """HUGEINT travels as DECIMAL(38, 0), which is smaller than its own largest values."""
    con.raw_sql(
        "CREATE TABLE huge (c HUGEINT); "
        "INSERT INTO huge VALUES (170141183460469231731687303715884105727)"
    ).close()
    with pytest.raises(adbc_driver_manager.ProgrammingError, match="cannot send the HUGEINT value"):
        con.table("huge").to_pyarrow()
    assert con.execute(con.table("huge").c.cast("string").max()) == (
        "170141183460469231731687303715884105727"
    )


@pytest.mark.parametrize(
    ("sql_type", "literal"),
    [
        ("BIT", "'101'::BIT"),
        ("BIGNUM", "123::BIGNUM"),
        ("UHUGEINT", "5"),
        ("UNION(i INTEGER, s VARCHAR)", "union_value(i := 1)"),
    ],
)
def test_duckdb_types_ibis_does_not_know(con, sql_type, literal):
    """Ibis 12 has no type for these, so the backend reports them as `unknown`. When Ibis
    learns one, this test fails and the type moves up into CASES."""
    con.raw_sql(f"CREATE TABLE odd (c {sql_type}); INSERT INTO odd VALUES ({literal})").close()
    assert con.table("odd").schema()["c"].is_unknown()
    # Cast to a type Ibis knows, they are readable.
    assert con.sql("SELECT CAST(c AS VARCHAR) AS c FROM odd").to_pyarrow()["c"].to_pylist() != [
        None
    ]


def test_arrow_types_the_server_refuses_to_load(con):
    """Nothing is stored as something else behind the caller's back."""
    with pytest.raises(adbc_driver_manager.Error, match="halffloat cannot be ingested"):
        con.con.cursor().adbc_ingest(
            table_name="half",
            data=pa.table({"c": pa.array([1.5], type=pa.float16())}),
            mode="create",
        )
    wide = ibis.schema({"c": "decimal(50, 2)"})
    with pytest.raises(adbc_driver_manager.Error, match=r"(?i)decimal.*38"):
        con.create_table("wide", schema=wide)
