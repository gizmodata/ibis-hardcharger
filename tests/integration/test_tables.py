"""Tables: creating, loading, inserting, replacing and dropping, catalogs and schemas, and what
the backend sends to the server for in-memory data."""

from __future__ import annotations

import adbc_driver_manager
import ibis
import ibis.common.exceptions as exc
import pandas as pd
import polars as pl
import pyarrow as pa
import pyarrow.dataset as ds
import pytest
from ibis import util

import ibis_hardcharger


def rows(con, sql: str) -> list[tuple]:
    with con._safe_raw_sql(sql) as cur:
        return cur.fetchall()


def temporary_tables(con) -> set[str]:
    return {name for (name,) in rows(con, "SELECT table_name FROM duckdb_tables() WHERE temporary")}


# ---- create_table -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: pa.table({"a": [1, 2, 3], "b": ["x", "y", None]}), id="pyarrow"),
        pytest.param(lambda: pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", None]}), id="pandas"),
        pytest.param(lambda: pl.DataFrame({"a": [1, 2, 3], "b": ["x", "y", None]}), id="polars"),
        pytest.param(
            lambda: pl.LazyFrame({"a": [1, 2, 3], "b": ["x", "y", None]}), id="polars-lazy"
        ),
        pytest.param(
            lambda: pa.table({"a": [1, 2, 3], "b": ["x", "y", None]}).to_reader(),
            id="record-batch-reader",
        ),
        pytest.param(
            lambda: ds.dataset(pa.table({"a": [1, 2, 3], "b": ["x", "y", None]})), id="dataset"
        ),
        pytest.param(lambda: ibis.memtable({"a": [1, 2, 3], "b": ["x", "y", None]}), id="memtable"),
    ],
)
def test_create_table_from_in_memory_data(con, make):
    t = con.create_table("loaded", make())
    assert t.schema() == ibis.schema({"a": "int64", "b": "string"})
    assert t.order_by("a").to_pyarrow().to_pydict() == {"a": [1, 2, 3], "b": ["x", "y", None]}
    assert "loaded" in con.list_tables()


def test_create_table_from_in_memory_data_makes_no_other_table(con):
    """The data goes straight into the table: no temporary copy is left in the session."""
    before = temporary_tables(con=con)
    con.create_table("direct", pa.table({"a": [1]}))
    con.insert("direct", pd.DataFrame({"a": [2]}))
    assert temporary_tables(con=con) == before


def test_create_table_from_an_expression(con):
    source = con.create_table("source", pa.table({"a": [1, 2, 3, 4]}))
    t = con.create_table("derived", source.filter(source.a > 2).mutate(b=source.a * 10))
    assert t.order_by("a").to_pyarrow().to_pydict() == {"a": [3, 4], "b": [30, 40]}


def test_create_table_from_a_schema(con):
    schema = ibis.schema({"id": "!int32", "name": "string", "amount": "decimal(10, 2)"})
    t = con.create_table("empty", schema=schema)
    assert t.schema() == schema
    assert t.count().execute() == 0
    assert rows(con, "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'empty' ORDER BY ordinal_position") == [
        ("id", "NO"), ("name", "YES"), ("amount", "YES")
    ]  # fmt: skip


def test_create_table_gives_the_columns_the_schemas_types(con):
    """The table is made from the Ibis schema; the Arrow data is cast into it on the server."""
    data = pa.table({"small": pa.array([1, 2], type=pa.int64()), "doc": ['{"a": 1}', "[]"]})
    schema = ibis.schema({"small": "!int8", "doc": "json"})
    t = con.create_table("typed", data, schema=schema)
    assert t.schema() == schema
    assert rows(con, "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'typed' ORDER BY ordinal_position") == [
        ("small", "TINYINT"), ("doc", "JSON")
    ]  # fmt: skip


def test_create_table_needs_data_or_a_schema(con):
    with pytest.raises(ValueError, match="Either `obj` or `schema` must be specified"):
        con.create_table("nothing")


def test_create_table_refuses_null_typed_columns(con):
    with pytest.raises(exc.IbisTypeError, match=r"NULL typed columns.*NULL columns: \('n',\)"):
        con.create_table("nulls", schema=ibis.schema({"a": "int64", "n": "null"}))
    assert "nulls" not in con.list_tables()


def test_create_table_that_exists_is_refused(con):
    con.create_table("once", pa.table({"a": [1]}))
    with pytest.raises(adbc_driver_manager.Error, match='Table with name "once" already exists'):
        con.create_table("once", pa.table({"a": [2]}))
    assert con.table("once").to_pyarrow().to_pydict() == {"a": [1]}


def test_a_load_keeps_the_order_of_its_rows(con):
    """A bulk load is one ordered stream. (The driver's default, up to 8 parallel parts for a
    load of two batches or more, lands rows in no defined order.)"""
    n = 600_000  # five batches of ibis_hardcharger.INGEST_BATCH_ROWS
    assert n > 4 * ibis_hardcharger.INGEST_BATCH_ROWS
    t = con.create_table("ordered", pa.table({"i": pa.array(range(n), type=pa.int64())}))
    assert t.to_pyarrow()["i"].to_pylist() == list(range(n))


def test_a_failed_load_leaves_no_table(con):
    data = pa.table({"a": pa.array([1, None], type=pa.int64())})
    with pytest.raises(adbc_driver_manager.IntegrityError, match="NOT NULL constraint failed"):
        con.create_table("half_made", data, schema=ibis.schema({"a": "!int64"}))
    assert "half_made" not in con.list_tables()


# ---- overwrite ----------------------------------------------------------------------------------


def test_overwrite_replaces_the_table(con):
    con.create_table("replaced", pa.table({"a": [1, 2]}))
    t = con.create_table("replaced", pa.table({"b": ["x"]}), overwrite=True)
    assert t.schema() == ibis.schema({"b": "string"})
    assert t.to_pyarrow().to_pydict() == {"b": ["x"]}
    assert con.list_tables(like="hardcharger_table") == []  # no stand-in table is left


def test_overwrite_from_the_table_itself(con):
    t = con.create_table("self_ref", pa.table({"a": [1, 2, 3, 4]}))
    t = con.create_table("self_ref", t.filter(t.a % 2 == 0), overwrite=True)
    assert t.order_by("a").to_pyarrow().to_pydict() == {"a": [2, 4]}


def test_overwrite_without_an_existing_table(con):
    t = con.create_table("fresh", pa.table({"a": [1]}), overwrite=True)
    assert t.to_pyarrow().to_pydict() == {"a": [1]}


def test_a_failed_overwrite_keeps_the_old_table(con):
    con.create_table("kept", pa.table({"a": [1, 2]}))
    bad = pa.table({"a": pa.array([1, None], type=pa.int64())})
    with pytest.raises(adbc_driver_manager.IntegrityError, match="NOT NULL constraint failed"):
        con.create_table("kept", bad, schema=ibis.schema({"a": "!int64"}), overwrite=True)
    assert con.table("kept").order_by("a").to_pyarrow().to_pydict() == {"a": [1, 2]}
    assert con.list_tables(like="hardcharger_table") == []


def test_overwrite_inside_the_callers_transaction(con):
    """The swap joins a transaction opened with raw_sql("BEGIN"), and rolls back with it."""
    con.create_table("in_txn", pa.table({"a": [1]}))
    con.raw_sql("BEGIN TRANSACTION").close()
    try:
        con.create_table("in_txn", pa.table({"a": [2]}), overwrite=True)
        assert con.table("in_txn").to_pyarrow().to_pydict() == {"a": [2]}
    finally:
        con.raw_sql("ROLLBACK").close()
    assert con.table("in_txn").to_pyarrow().to_pydict() == {"a": [1]}


# ---- temporary tables ---------------------------------------------------------------------------


def test_temporary_table(con, connect):
    t = con.create_table("scratch", pa.table({"a": [1, 2]}), temp=True)
    assert t.a.sum().execute() == 3
    assert "scratch" in temporary_tables(con=con)
    assert "scratch" in con.list_tables()
    other = connect(schema=con.current_database)
    try:
        assert "scratch" not in other.list_tables()  # it belongs to the session
    finally:
        other.disconnect()


def test_temporary_table_overwrite(con):
    con.create_table("scratch", pa.table({"a": [1]}), temp=True)
    t = con.create_table("scratch", pa.table({"a": [5, 6]}), temp=True, overwrite=True)
    assert t.a.sum().execute() == 11
    assert "scratch" in temporary_tables(con=con)


def test_temporary_table_in_a_catalog_is_refused(con):
    with pytest.raises(
        exc.UnsupportedArgumentError, match="temporary tables in the `temp` catalog"
    ):
        con.create_table("t", pa.table({"a": [1]}), database=("memory", "main"), temp=True)


def test_cache(con):
    source = con.create_table("to_cache", pa.table({"a": [1, 2, 3]}))
    cached = source.filter(source.a > 1).cache()
    assert cached.a.sum().execute() == 5
    assert cached.op().name in temporary_tables(con=con)


# ---- other schemas and catalogs -----------------------------------------------------------------


def test_create_and_drop_database(con):
    name = util.gen_name("db")
    con.create_database(name)
    try:
        assert name in con.list_databases()
        assert con.list_databases(like=f"^{name}$") == [name]
        t = con.create_table("there", pa.table({"a": [1]}), database=name)
        assert t.to_pyarrow().to_pydict() == {"a": [1]}
        assert con.list_tables(database=name) == ["there"]
        con.insert("there", pd.DataFrame({"a": [2]}), database=name)
        assert con.table("there", database=name).a.sum().execute() == 3
        con.truncate_table("there", database=name)
        assert con.table("there", database=name).count().execute() == 0
        con.drop_table("there", database=name)
        assert con.list_tables(database=name) == []
    finally:
        con.drop_database(name, force=True)
    assert name not in con.list_databases()


def test_create_database_in_another_catalog_is_refused(con):
    with pytest.raises(exc.UnsupportedOperationError, match="cannot create a database in another"):
        con.create_database("x", catalog="system")
    with pytest.raises(exc.UnsupportedOperationError, match="cannot drop a database in another"):
        con.drop_database("x", catalog="system")


def test_attach_and_detach_a_catalog(con):
    name = util.gen_name("attached")
    con.attach(":memory:", name=name)
    try:
        assert name in con.list_catalogs()
        assert con.list_databases(catalog=name) == ["main"]
        t = con.create_table("elsewhere", pa.table({"a": [1, 2]}), database=(name, "main"))
        assert t.a.sum().execute() == 3
        assert con.list_tables(database=(name, "main")) == ["elsewhere"]
        assert con.list_tables(database=f"{name}.main") == ["elsewhere"]
        con.insert("elsewhere", pa.table({"a": [10]}), database=(name, "main"))
        assert con.table("elsewhere", database=(name, "main")).a.sum().execute() == 13
    finally:
        con.detach(name)
    assert name not in con.list_catalogs()


def test_list_catalogs(con):
    catalogs = con.list_catalogs()
    assert {"memory", "temp", "system"} <= set(catalogs)
    assert con.list_catalogs(like="^mem") == ["memory"]


# ---- insert -------------------------------------------------------------------------------------


@pytest.fixture
def target(con):
    con.raw_sql(
        "CREATE TABLE target (id INTEGER NOT NULL, name VARCHAR, flag VARCHAR DEFAULT 'none')"
    ).close()
    return con.table("target")


def test_insert_matches_columns_by_name(con, target):
    """Reordered columns keep their values, and a column left out gets its default."""
    con.insert("target", pd.DataFrame({"name": ["ann", "bob"], "id": [1, 2]}))
    assert target.order_by("id").to_pyarrow().to_pylist() == [
        {"id": 1, "name": "ann", "flag": "none"},
        {"id": 2, "name": "bob", "flag": "none"},
    ]


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: pa.table({"id": [7], "name": ["x"], "flag": ["f"]}), id="pyarrow"),
        pytest.param(lambda: pd.DataFrame({"id": [7], "name": ["x"], "flag": ["f"]}), id="pandas"),
        pytest.param(lambda: pl.DataFrame({"id": [7], "name": ["x"], "flag": ["f"]}), id="polars"),
        pytest.param(lambda: {"id": [7], "name": ["x"], "flag": ["f"]}, id="dict"),
        pytest.param(lambda: [{"id": 7, "name": "x", "flag": "f"}], id="list-of-dicts"),
    ],
)
def test_insert_in_memory_data(con, target, make):
    con.insert("target", make())
    assert target.to_pyarrow().to_pylist() == [{"id": 7, "name": "x", "flag": "f"}]


def test_insert_with_other_column_names_goes_by_position(con, target):
    """Ibis's rule for data whose names are not the table's columns."""
    con.insert("target", pd.DataFrame({"c0": [1], "c1": ["ann"], "c2": ["f"]}))
    assert target.to_pyarrow().to_pylist() == [{"id": 1, "name": "ann", "flag": "f"}]


def test_insert_overwrite(con, target):
    con.insert("target", pa.table({"id": [1, 2], "name": ["a", "b"]}))
    con.insert("target", pa.table({"id": [3], "name": ["c"]}), overwrite=True)
    assert target.to_pyarrow().to_pylist() == [{"id": 3, "name": "c", "flag": "none"}]


def test_insert_an_expression(con, target):
    source = con.create_table("staged", pa.table({"id": [1, 2], "name": ["a", "b"]}))
    con.insert("target", source.filter(source.id > 1))
    assert target.to_pyarrow().to_pylist() == [{"id": 2, "name": "b", "flag": "none"}]


def test_insert_that_breaks_a_constraint_inserts_nothing(con, target):
    data = pa.table({"id": pa.array([1, None, 3], type=pa.int32()), "name": ["a", "b", "c"]})
    with pytest.raises(adbc_driver_manager.IntegrityError, match="NOT NULL constraint failed"):
        con.insert("target", data)
    assert target.count().execute() == 0  # the load is one transaction


def test_insert_into_a_temporary_table(con):
    con.create_table("scratch", schema=ibis.schema({"a": "int64"}), temp=True)
    con.insert("scratch", pa.table({"a": [1, 2]}))
    assert con.table("scratch").a.sum().execute() == 3


def test_insert_into_a_missing_table(con):
    with pytest.raises(exc.TableNotFound, match="no_such_table"):
        con.insert("no_such_table", pa.table({"a": [1]}))


def test_upsert(con):
    con.raw_sql("CREATE TABLE people (id INTEGER PRIMARY KEY, name VARCHAR)").close()
    con.insert("people", pa.table({"id": [1, 2], "name": ["ann", "bob"]}))
    con.upsert("people", pd.DataFrame({"id": [2, 3], "name": ["bobby", "cy"]}), on="id")
    assert con.table("people").order_by("id").to_pyarrow().to_pylist() == [
        {"id": 1, "name": "ann"},
        {"id": 2, "name": "bobby"},
        {"id": 3, "name": "cy"},
    ]


# ---- in-memory tables in expressions ------------------------------------------------------------


def test_memtable_is_a_temporary_table_with_the_schemas_types(con):
    memtable = ibis.memtable(
        {"id": [1, 2], "doc": ['{"a": 1}', "[]"]}, schema={"id": "int16", "doc": "json"}
    )
    assert con.execute(memtable.id.sum()) == 3
    name = memtable.op().name
    assert name in temporary_tables(con=con)
    assert rows(con, f'SELECT column_name, column_type FROM (DESCRIBE "{name}")') == [
        ("id", "SMALLINT"),
        ("doc", "JSON"),
    ]


def test_memtable_joined_with_a_table(con):
    t = con.create_table("facts", pa.table({"k": [1, 2, 2], "v": [10, 20, 30]}))
    names = ibis.memtable({"k": [1, 2], "name": ["one", "two"]})
    expr = t.join(names, "k").group_by("name").agg(total=t.v.sum()).order_by("name")
    assert con.to_pyarrow(expr).to_pylist() == [
        {"name": "one", "total": 10},
        {"name": "two", "total": 50},
    ]


def test_memtable_with_an_all_null_column(con):
    memtable = ibis.memtable(pa.table({"a": [1, 2], "n": pa.nulls(2)}))
    assert memtable.schema()["n"].is_null()
    result = con.to_pyarrow(memtable.order_by("a"))
    assert result.to_pydict() == {"a": [1, 2], "n": [None, None]}
    assert result.schema.field("n").type == pa.null()


def test_memtable_from_a_dataset_is_streamed(con):
    memtable = ibis.memtable(ds.dataset(pa.table({"a": list(range(1000))})))
    assert con.execute(memtable.a.sum()) == sum(range(1000))


def test_empty_memtable(con):
    memtable = ibis.memtable(
        pa.table({"a": pa.array([], type=pa.int32()), "b": pa.array([], type=pa.string())})
    )
    result = con.to_pyarrow(memtable)
    assert result.num_rows == 0
    assert result.schema.types == [pa.int32(), pa.string()]


# ---- schemas, dropping and renaming -------------------------------------------------------------


def test_get_schema_of_a_missing_table(con):
    with pytest.raises(exc.TableNotFound, match="no_such_table"):
        con.table("no_such_table")
    with pytest.raises(exc.TableNotFound, match="no_such_table"):
        con.get_schema("no_such_table", catalog="memory", database="main")


def test_an_error_that_is_not_a_missing_table_stays_the_drivers(con):
    with pytest.raises(adbc_driver_manager.ProgrammingError, match="syntax error"):
        con.sql("SELEC 1")


def test_drop_table(con):
    con.create_table("gone", pa.table({"a": [1]}))
    con.drop_table("gone")
    assert "gone" not in con.list_tables()
    con.drop_table("gone", force=True)  # nothing to drop: not an error with force
    with pytest.raises(adbc_driver_manager.Error, match="Table with name gone does not exist"):
        con.drop_table("gone")


def test_views(con):
    t = con.create_table("base", pa.table({"a": [1, 2, 3]}))
    v = con.create_view("evens", t.filter(t.a % 2 == 0))
    assert v.to_pyarrow().to_pydict() == {"a": [2]}
    assert "evens" in con.list_tables()
    con.create_view("evens", t.filter(t.a > 100), overwrite=True)
    assert con.table("evens").count().execute() == 0
    con.drop_view("evens")
    assert "evens" not in con.list_tables()


def test_list_tables_like(con):
    con.create_table("alpha_one", pa.table({"a": [1]}))
    con.create_table("alpha_two", pa.table({"a": [1]}))
    con.create_table("beta", pa.table({"a": [1]}))
    assert sorted(con.list_tables(like="^alpha_")) == ["alpha_one", "alpha_two"]
    assert sorted(con.tables) == ["alpha_one", "alpha_two", "beta"]
    assert con.tables.beta.count().execute() == 1


# ---- SQL ----------------------------------------------------------------------------------------


def test_raw_sql_with_parameters(con):
    cur = con.raw_sql("SELECT ? + ? AS total, ? AS word", parameters=[40, 2, "x"])
    try:
        assert cur.fetchall() == [(42, "x")]
    finally:
        cur.close()


def test_raw_sql_runs_several_statements(con):
    cur = con.raw_sql(
        "CREATE TABLE multi (a INTEGER); INSERT INTO multi VALUES (1), (2); "
        "SELECT sum(a) FROM multi"
    )
    try:
        assert cur.fetchall() == [(3,)]
    finally:
        cur.close()


def test_raw_sql_takes_a_sqlglot_expression(con):
    import sqlglot as sg

    cur = con.raw_sql(sg.select(sg.exp.Literal.number(7).as_("n")))
    try:
        assert cur.fetchall() == [(7,)]
    finally:
        cur.close()


def test_sql_expression(con):
    con.create_table("nums", pa.table({"a": [1, 2, 3]}))
    expr = con.sql("SELECT a, a * 2 AS doubled FROM nums")
    assert expr.schema() == ibis.schema({"a": "int64", "doubled": "int64"})
    assert expr.filter(expr.doubled > 2).order_by("a").to_pyarrow().to_pydict() == {
        "a": [2, 3],
        "doubled": [4, 6],
    }
    t = con.table("nums")
    assert t.sql("SELECT max(a) AS m FROM nums").to_pyarrow().to_pydict() == {"m": [3]}


def test_lambdas_run_on_duckdb_2(con):
    t = con.create_table("lists", pa.table({"xs": [[1, 2, 3], [4]]}))
    expr = t.select(doubled=t.xs.map(lambda x: x * 2), big=t.xs.filter(lambda x: x > 1))
    assert con.to_pyarrow(expr).to_pylist() == [
        {"doubled": [2, 4, 6], "big": [2, 3]},
        {"doubled": [8], "big": [4]},
    ]


# ---- user-defined functions -----------------------------------------------------------------------


def test_builtin_udf_calls_a_server_function(con):
    @ibis.udf.scalar.builtin
    def hamming(a: str, b: str) -> int: ...

    assert con.execute(hamming("duck", "luck")) == 1


@pytest.mark.parametrize("kind", ["python", "pyarrow", "pandas"])
def test_python_udfs_are_refused(con, kind):
    @getattr(ibis.udf.scalar, kind)
    def add_one(x: int) -> int:
        return x + 1

    with pytest.raises(exc.UnsupportedOperationError, match="UDFs run inside the client's process"):
        con.execute(add_one(ibis.literal(1)))
