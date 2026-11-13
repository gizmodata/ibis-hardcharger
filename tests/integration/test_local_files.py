"""Files on the client: read_csv, read_parquet, read_json and read_delta parse them here and
bulk-load them into temporary tables; to_parquet, to_csv, to_json and to_delta write here.

The first ten tests are ibis-gizmosql's tests/test_local_ingest.py, with its assertions.
"""

from __future__ import annotations

import functools
import http.server
import json
import threading

import ibis
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


@pytest.fixture
def sample_csv(tmp_path):
    """Create a sample CSV file."""
    path = tmp_path / "sample.csv"
    path.write_text("id,name,value\n1,alice,10.5\n2,bob,20.3\n3,carol,30.1\n")
    return str(path)


@pytest.fixture
def sample_parquet(tmp_path):
    """Create a sample Parquet file."""
    table = pa.table(
        {
            "id": [1, 2, 3],
            "name": ["alice", "bob", "carol"],
            "value": [10.5, 20.3, 30.1],
        }
    )
    path = tmp_path / "sample.parquet"
    pq.write_table(table=table, where=path)
    return str(path)


@pytest.fixture
def sample_json(tmp_path):
    """Create a sample newline-delimited JSON file."""
    path = tmp_path / "sample.json"
    rows = [
        {"id": 1, "name": "alice", "value": 10.5},
        {"id": 2, "name": "bob", "value": 20.3},
        {"id": 3, "name": "carol", "value": 30.1},
    ]
    path.write_text("\n".join(json.dumps(obj=row) for row in rows) + "\n")
    return str(path)


@pytest.fixture
def large_parquet(tmp_path):
    """Create a Parquet file with more rows than the batch size the test asks for."""
    n = 25_000
    table = pa.table(
        {
            "id": list(range(n)),
            "value": [float(i) for i in range(n)],
        }
    )
    path = tmp_path / "large.parquet"
    pq.write_table(table=table, where=path)
    return str(path)


def is_temporary(con, name: str) -> bool:
    with con._safe_raw_sql(
        "SELECT temporary FROM duckdb_tables() WHERE table_name = ?", parameters=[name]
    ) as cur:
        return cur.fetchall() == [(True,)]


# ---- ibis-gizmosql's tests ----------------------------------------------------------------------


def test_read_csv(con, sample_csv):
    """Test reading a local CSV file into Hardcharger."""
    t = con.read_csv(sample_csv, table_name="test_csv_ingest")
    result = t.execute()
    assert len(result) == 3
    assert list(result.columns) == ["id", "name", "value"]
    assert result["name"].tolist() == ["alice", "bob", "carol"]


def test_read_parquet(con, sample_parquet):
    """Test reading a local Parquet file into Hardcharger."""
    t = con.read_parquet(sample_parquet, table_name="test_parquet_ingest")
    result = t.execute()
    assert len(result) == 3
    assert list(result.columns) == ["id", "name", "value"]
    assert result["id"].tolist() == [1, 2, 3]


def test_read_json(con, sample_json):
    """Test reading a local JSON file into Hardcharger."""
    t = con.read_json(sample_json, table_name="test_json_ingest")
    result = t.execute()
    assert len(result) == 3
    assert "name" in result.columns
    assert result["name"].tolist() == ["alice", "bob", "carol"]


def test_read_parquet_batching(con, large_parquet):
    """Test that large files are correctly ingested with batching."""
    t = con.read_parquet(large_parquet, table_name="test_batch_ingest", batch_size=5_000)
    result = t.execute()
    assert len(result) == 25_000
    # Verify data integrity: first and last values
    assert result["id"].iloc[0] == 0
    assert result["id"].iloc[-1] == 24_999


def test_read_csv_custom_batch_size(con, sample_csv):
    """Test read_csv with a custom batch size."""
    t = con.read_csv(sample_csv, table_name="test_csv_batch", batch_size=2)
    result = t.execute()
    assert len(result) == 3


def test_read_parquet_auto_table_name(con, sample_parquet):
    """Test that auto-generated table names work."""
    t = con.read_parquet(sample_parquet)
    result = t.execute()
    assert len(result) == 3


def test_read_csv_auto_table_name(con, sample_csv):
    """Test that auto-generated table names work for CSV."""
    t = con.read_csv(sample_csv)
    result = t.execute()
    assert len(result) == 3


def test_read_parquet_multiple_files(con, tmp_path):
    """Test reading multiple Parquet files."""
    for i in range(3):
        table = pa.table({"id": [i * 10 + j for j in range(5)]})
        pq.write_table(table=table, where=tmp_path / f"part_{i}.parquet")

    paths = [str(tmp_path / f"part_{i}.parquet") for i in range(3)]
    t = con.read_parquet(paths, table_name="test_multi_parquet")
    result = t.execute()
    assert len(result) == 15


def test_read_csv_ibis_operations(con, sample_csv):
    """Test that ibis operations work on ingested CSV data."""
    t = con.read_csv(sample_csv, table_name="test_csv_ops")
    # Filter
    filtered = t.filter(t.value > 15).execute()
    assert len(filtered) == 2
    # Aggregate
    total = t.value.sum().execute()
    assert abs(total - 60.9) < 0.01


def test_read_parquet_ibis_operations(con, sample_parquet):
    """Test that ibis operations work on ingested Parquet data."""
    t = con.read_parquet(sample_parquet, table_name="test_parquet_ops")
    # Select specific columns
    result = t.select("name", "value").execute()
    assert list(result.columns) == ["name", "value"]
    # Sort
    result = t.order_by("id").execute()
    assert result["id"].tolist() == [1, 2, 3]


# ---- what is new here -----------------------------------------------------------------------------


def test_read_tables_are_temporary(con, sample_csv, sample_parquet, sample_json):
    """A file read is the session's own, as in Ibis's DuckDB backend, not a table left in
    the database. create_table() keeps it."""
    con.read_csv(sample_csv, table_name="from_csv")
    con.read_parquet(sample_parquet, table_name="from_parquet")
    con.read_json(sample_json, table_name="from_json")
    for name in ("from_csv", "from_parquet", "from_json"):
        assert is_temporary(con=con, name=name)
    kept = con.create_table("kept", con.table("from_parquet"))
    assert not is_temporary(con=con, name="kept")
    assert kept.count().execute() == 3


def test_read_again_under_the_same_name_replaces(con, sample_csv, sample_parquet):
    con.read_csv(sample_csv, table_name="reread")
    t = con.read_parquet(sample_parquet, table_name="reread")
    assert t.count().execute() == 3


def test_read_csv_options(con, tmp_path):
    path = tmp_path / "semi.csv"
    path.write_text("7;x;2024-01-02\n8;y;2024-01-03\n")
    t = con.read_csv(
        path,
        table_name="with_options",
        header=False,
        delim=";",
        columns={"n": "SMALLINT", "s": "VARCHAR", "d": "DATE"},
    )
    assert t.schema() == ibis.schema({"n": "int16", "s": "string", "d": "date"})
    assert t.n.sum().execute() == 15

    typed = con.read_csv(
        tmp_path / "semi.csv", header=False, delim=";", types={"column0": "BIGINT"}
    )
    assert typed.schema()["column0"] == ibis.dtype("int64")


def test_read_csv_directory_glob(con, tmp_path):
    for i in range(3):
        (tmp_path / f"part{i}.csv").write_text(f"a\n{i}\n")
    t = con.read_csv(str(tmp_path / "*.csv"))
    assert sorted(t.a.to_pyarrow().to_pylist()) == [0, 1, 2]


def test_read_json_columns(con, sample_json):
    t = con.read_json(sample_json, columns={"id": "SMALLINT", "name": "VARCHAR"})
    assert t.schema() == ibis.schema({"id": "int16", "name": "string"})


def test_read_parquet_keeps_types_and_order(con, tmp_path):
    table = pa.table(
        {
            "i": pa.array(range(300_000), type=pa.int32()),
            "when": pa.array([pd.Timestamp("2024-01-01")] * 300_000, type=pa.timestamp("us")),
            "tags": pa.array([["a", "b"]] * 300_000, type=pa.list_(pa.string())),
        }
    )
    path = tmp_path / "typed.parquet"
    pq.write_table(table=table, where=path)
    t = con.read_parquet(path)
    assert t.schema() == ibis.schema(
        {"i": "int32", "when": "timestamp(6)", "tags": "array<string>"}
    )
    assert t.i.to_pyarrow().to_pylist() == list(range(300_000))


def test_read_a_missing_file(con, tmp_path):
    with pytest.raises(Exception, match="No files found that match the pattern"):
        con.read_parquet(tmp_path / "missing.parquet")


def test_read_delta(con, tmp_path):
    from deltalake import write_deltalake

    path = tmp_path / "delta"
    write_deltalake(path, pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"]}))
    write_deltalake(path, pa.table({"id": [4], "name": ["d"]}), mode="append")
    t = con.read_delta(path, table_name="from_delta")
    assert is_temporary(con=con, name="from_delta")
    assert t.order_by("id").to_pyarrow().to_pydict() == {
        "id": [1, 2, 3, 4],
        "name": ["a", "b", "c", "d"],
    }
    first = con.read_delta(path, version=0)
    assert first.count().execute() == 3


def test_example_loader_reads_local_files(con, sample_csv, sample_parquet):
    """What ibis.examples.<name>.fetch(backend=con) calls, with files that are already here."""
    csv = con._load_example(path=sample_csv, table_name="example_csv")
    parquet = con._load_example(path=sample_parquet, table_name="example_parquet")
    assert csv.count().execute() == 3
    assert parquet.columns == ("id", "name", "value")


# ---- URLs are the server's to read ----------------------------------------------------------------


@pytest.fixture
def file_server(tmp_path):
    """Serve a directory over HTTP on 127.0.0.1: files the Hardcharger server can fetch by URL
    (with DuckDB's httpfs), and that this test process never hands to a reader as a path."""
    directory = tmp_path / "served"
    directory.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield directory, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_a_url_is_read_by_the_server(con, file_server):
    directory, base_url = file_server
    pq.write_table(
        table=pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"]}),
        where=directory / "remote.parquet",
    )
    (directory / "remote.csv").write_text("id,name\n4,d\n5,e\n")

    t = con.read_parquet(f"{base_url}/remote.parquet", table_name="remote_parquet")
    assert t.order_by("id").to_pyarrow().to_pydict() == {"id": [1, 2, 3], "name": ["a", "b", "c"]}
    # A view on the server over the URL, not a table loaded from this machine.
    with con._safe_raw_sql(
        "SELECT sql FROM duckdb_views() WHERE view_name = 'remote_parquet'"
    ) as cur:
        [(definition,)] = cur.fetchall()
    assert f"{base_url}/remote.parquet" in definition
    assert not is_temporary(con=con, name="remote_parquet")  # no table of that name

    csv = con.read_csv(f"{base_url}/remote.csv")
    assert csv.order_by("id").to_pyarrow().to_pydict() == {"id": [4, 5], "name": ["d", "e"]}


def test_files_and_urls_together_are_refused(con, sample_parquet):
    with pytest.raises(ValueError, match="files on this machine and URLs can't be read together"):
        con.read_parquet([sample_parquet, "https://example.com/data.parquet"])


# ---- writing --------------------------------------------------------------------------------------


@pytest.fixture
def people(con):
    return con.create_table(
        "people",
        pa.table(
            {
                "id": [1, 2, 3],
                "name": ["alice", "bob", None],
                "born": pa.array(
                    [pd.Timestamp("1990-01-02"), pd.Timestamp("1985-06-07"), None],
                    type=pa.date32(),
                ),
            }
        ),
    )


def test_to_parquet(con, people, tmp_path):
    path = tmp_path / "out.parquet"
    people.order_by("id").to_parquet(path)
    assert pq.read_table(path).to_pydict() == people.order_by("id").to_pyarrow().to_pydict()
    assert con.read_parquet(path).count().execute() == 3


def test_to_parquet_dir_partitions(people, tmp_path):
    out = tmp_path / "partitioned"
    people.to_parquet_dir(out, partitioning=["id"], partitioning_flavor="hive")
    assert sorted(child.name for child in out.iterdir()) == ["id=1", "id=2", "id=3"]


def test_to_csv(people, tmp_path):
    path = tmp_path / "out.csv"
    people.order_by("id").to_csv(path)
    assert pd.read_csv(path)["name"].tolist()[:2] == ["alice", "bob"]


def test_to_json(people, tmp_path):
    path = tmp_path / "out.json"
    people.order_by("id").to_json(path)
    assert json.loads(s=path.read_text()) == [
        {"id": 1, "name": "alice", "born": "1990-01-02"},
        {"id": 2, "name": "bob", "born": "1985-06-07"},
        {"id": 3, "name": None, "born": None},
    ]


def test_to_json_options(people, tmp_path):
    path = tmp_path / "out.json.gz"
    people.order_by("id").to_json(path, compression="gzip", dateformat="%d/%m/%Y")
    assert pd.read_json(path, compression="gzip")["born"].tolist()[0] == "02/01/1990"


def test_to_delta(con, people, tmp_path):
    path = tmp_path / "out_delta"
    people.to_delta(path)
    assert con.read_delta(path).count().execute() == 3


def test_to_pandas_batches_and_polars(people):
    batches = list(people.order_by("id").to_pandas_batches())
    assert sum(len(batch) for batch in batches) == 3
    assert people.order_by("id").to_polars()["id"].to_list() == [1, 2, 3]


def test_to_pyarrow_batches_chunk_size(con):
    t = con.create_table("many", pa.table({"i": list(range(10_000))}))
    with con.to_pyarrow_batches(t, chunk_size=3_000) as reader:
        sizes = [batch.num_rows for batch in reader]
    assert sum(sizes) == 10_000
    assert max(sizes) <= 3_000
