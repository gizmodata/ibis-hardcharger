<p align="center">
  <a href="https://github.com/gizmodata/hardcharger">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/gizmodata/ibis-hardcharger/main/docs/assets/hardcharger-banner-dark.png">
      <img src="https://raw.githubusercontent.com/gizmodata/ibis-hardcharger/main/docs/assets/hardcharger-banner-light.png" alt="Hardcharger™" width="400">
    </picture>
  </a>
</p>

<h1 align="center">Ibis Hardcharger™ Backend</h1>

<p align="center">
  <a href="https://github.com/gizmodata/ibis-hardcharger/actions/workflows/ci.yml"><img src="https://github.com/gizmodata/ibis-hardcharger/actions/workflows/ci.yml/badge.svg" alt="CI: full suite on Python 3.11–3.15"></a>
  <a href="https://pypi.org/project/ibis-hardcharger/"><img src="https://img.shields.io/pypi/v/ibis-hardcharger?logo=pypi" alt="PyPI version"></a>
  <a href="https://pypi.org/project/ibis-hardcharger/"><img src="https://img.shields.io/badge/Python-3.11–3.15-3776AB?logo=python&amp;logoColor=white" alt="Python 3.11–3.15"></a>
  <a href="https://pypi.org/project/ibis-hardcharger/"><img src="https://img.shields.io/pepy/dt/ibis-hardcharger.svg" alt="PyPI downloads"></a>
  <a href="https://ibis-project.org"><img src="https://img.shields.io/badge/Ibis-12.0-31a8db.svg" alt="Ibis 12.0"></a>
  <a href="https://github.com/gizmodata/hardcharger-adbc"><img src="https://img.shields.io/badge/driver-Hardcharger%20ADBC-f15a24.svg" alt="Hardcharger ADBC driver"></a>
  <a href="https://github.com/gizmodata/chargewire"><img src="https://img.shields.io/badge/protocol-Chargewire-31a8db.svg" alt="Chargewire protocol"></a>
  <a href="https://github.com/gizmodata/ibis-hardcharger/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-brightgreen.svg" alt="Apache 2.0 license"></a>
  <a href="https://gizmodata.com"><img src="https://img.shields.io/badge/by-GizmoData%E2%84%A2-f15a24.svg" alt="GizmoData™"></a>
</p>

The [Ibis](https://ibis-project.org) backend for [Hardcharger™](https://github.com/gizmodata/hardcharger),
GizmoData™'s DuckDB 2.0 server. Write dataframe expressions in Python; they compile to DuckDB SQL
and run on the server, over the [Chargewire](https://github.com/gizmodata/chargewire) protocol
(HTTP(S)) through the native [Hardcharger ADBC driver](https://github.com/gizmodata/hardcharger-adbc).
Results come back as Arrow.

- **The Ibis API**: tables, expressions, joins, windows, arrays, structs, maps and JSON, compiled by
  Ibis's DuckDB compiler. Ibis's own backend test suite, with its TPC-H and TPC-DS queries, runs
  against a live Hardcharger server in CI.
- **Arrow in, Arrow out**: pandas, Polars and PyArrow data is bulk-loaded as Arrow, in one
  transaction; results are Arrow tables, record batch streams, pandas or Polars.
- **Files on your machine**: `read_csv()`, `read_parquet()` and `read_json()` parse local files
  on the client and load them; the server needs no access to your file system.
- **Connections**: `hardcharger://` URLs, TLS on by default, password, access token, client
  certificate (mutual TLS) and browser (OAuth/SSO) sign-in.

## Install

```bash
pip install ibis-hardcharger
```

Python 3.11 to 3.15. It installs Ibis 12.0, `adbc-driver-hardcharger` 2.4.9 or newer, PyArrow,
pandas and DuckDB (which reads client-side files). sqlglot stays at 30.17: Ibis 12.0.0 does not
work with a newer one (its `drop_table()` sends `DROP TABLE` with no name from sqlglot 30.18 on).

## Quick start

`pip install hardcharger` gets the server for Python: it downloads `hardcharger-server` and runs
it on a free local port, which is all this example needs.

```python
import hardcharger
import ibis

with hardcharger.Server() as server:  # a throwaway server on this machine
    con = ibis.hardcharger.connect(
        host=server.host,
        port=server.port,
        user=server.username,
        password=server.password,
        use_encryption=False,  # the throwaway server speaks plain HTTP
    )
    trips = con.create_table(
        "trips",
        ibis.memtable(
            {"city": ["Boston", "Denver", "Boston", "Austin"], "miles": [12.5, 30.0, 4.0, 8.5]}
        ),
    )
    by_city = (
        trips.group_by("city")
        .agg(trips=trips.count(), miles=trips.miles.sum())
        .order_by(ibis.desc("miles"))
    )
    print(by_city.to_pandas())
    con.disconnect()
```

```text
     city  trips  miles
0  Denver      1   30.0
1  Boston      2   16.5
2  Austin      1    8.5
```

## Connecting

The rest of this page talks to a server you start yourself. For one on your own machine, on
plain HTTP ([other ways to run it](https://docs.hardcharger.gizmodata.com/get-started/quickstart/)):

```bash
pip install hardcharger
HARDCHARGER_ADMIN_PASSWORD='change-me' hardcharger-server --host 127.0.0.1
```

```python
import ibis

con = ibis.hardcharger.connect(
    host="127.0.0.1",
    user="hardcharger",
    password="change-me",
    use_encryption=False,
)
print(con.current_catalog, con.current_database)
print(con.execute(ibis.literal(6) * 7))
```

```text
memory main
42
```

Or with a URL, `hardcharger://[user[:password]@]host[:port][/catalog[/schema]][?option=value&...]`:

```python
import ibis

con = ibis.connect("hardcharger://hardcharger:change-me@127.0.0.1:31337/memory/main?tls=false")
print(con.list_catalogs())
```

```text
['_hardcharger_system', 'memory', 'system', 'temp']
```

**TLS is on unless you turn it off**, and the server's certificate is verified. The port defaults
to 31337 (443 for a GizmoData Cloud host). `use_encryption=False`, or `?tls=false` in a URL, is
for a server on your own machine.

| Argument | Default | Meaning |
| --- | --- | --- |
| `host` | | Server host name or address |
| `port` | 31337 | Server port (443 for a GizmoData Cloud host) |
| `user`, `password` | | Password sign-in |
| `access_token` | | A Hardcharger access token, instead of a user name and password |
| `auth_type` | `"password"` | `"external"`: browser sign-in. `"client_certificate"` (`"mtls"`): sign in with a TLS client certificate |
| `database` | the server's | Catalog to start in |
| `schema` | the server's | Schema to start in |
| `use_encryption` | `True` | TLS (HTTPS); `False` is plain HTTP |
| `disable_certificate_verification` | `False` | Don't verify the server's certificate (development only) |
| `uri` | | The driver's own URI instead of `host`, `port` and `use_encryption`: `hardcharger://host[:port][/prefix][?tls=false]` |
| `extensions` | | DuckDB extensions to install and load on the server for the session |

Every other keyword argument goes to the driver's
[`connect()`](https://github.com/gizmodata/hardcharger-adbc/tree/main/python) as it is:
`tls_root_certs`, `tls_client_cert`, `tls_client_key`, `tls_server_name`, `subject_token`,
`oauth_timeout`, `open_browser`, `share_sign_in`, `resume_timeout`, `settings`, `session_tag`,
`query_tag`, `profile`, `db_kwargs` and `conn_kwargs`.

In a URL, the connection options can be query parameters (`?auth_type=external`);
percent-encode special characters in the user name and password. A URL is often configuration
that someone other than the application's author supplies, so it can't do everything a keyword
argument can: `open_browser` is `true` or `false` there (as a keyword argument it may name the
command that opens the browser), and `session_id`, `settings`, `db_kwargs`, `conn_kwargs` and
`uri` are keyword arguments only.

A server whose certificate comes from your own certificate authority:

```python
import ibis

con = ibis.hardcharger.connect(
    host="localhost",
    port=31338,
    user="hardcharger",
    password="change-me",
    tls_root_certs="ca.pem",  # the CA's certificate: a file path, or the PEM text
)
print(con.con.current_user())
```

```text
hardcharger
```

An access token instead of a password:

```python
import os

import ibis

con = ibis.hardcharger.connect(
    host="127.0.0.1",
    access_token=os.environ["HARDCHARGER_TOKEN"],
    use_encryption=False,
)
print(con.con.current_user())
```

```text
hardcharger
```

Browser sign-in, on a server set up with an identity provider
([SSO](https://docs.hardcharger.gizmodata.com/guides/sso/)): the driver opens your browser, you
sign in, and the connection is yours. No user name or password is given.

```python
import ibis

con = ibis.hardcharger.connect(
    host="127.0.0.1",
    port=31339,
    auth_type="external",
    use_encryption=False,
)
print(con.execute(ibis.literal("signed in").upper()))
```

```text
SIGNED IN
```

## Loading data

In-memory data (pandas, Polars, PyArrow tables, record batch readers and datasets) is
bulk-loaded as Arrow: no `INSERT` statements, one transaction per load, rows in the order you
gave them. `create_table()` makes the table with the column types of the Ibis schema, so
`NOT NULL`, `JSON`, `UUID` and the like are what you asked for.

```python
import ibis
import pandas as pd
import pyarrow as pa

con = ibis.hardcharger.connect(
    host="127.0.0.1", user="hardcharger", password="change-me", use_encryption=False
)

orders = con.create_table(
    "orders",
    pd.DataFrame(
        {"id": [1, 2, 3], "customer": ["ann", "bob", "ann"], "total": [20.0, 35.5, 12.25]}
    ),
    overwrite=True,
)
# Columns are matched by name; one left out gets its default.
con.insert("orders", pa.table({"total": [99.0], "id": [4]}))

scratch = con.create_table("scratch", orders.filter(orders.total > 15), temp=True, overwrite=True)

print(orders.order_by("id").to_pyarrow().to_pylist()[-1])
print(scratch.count().execute())
```

```text
{'id': 4, 'customer': None, 'total': 99.0}
3
```

`ibis.memtable(...)` used in an expression is loaded into a temporary table of the session the
first time the expression runs.

### Files

`read_csv()`, `read_parquet()` and `read_json()` take paths on **your** machine: an in-process
DuckDB parses them (with DuckDB's reader options) and the rows are bulk-loaded into a temporary
table. Keep one with `create_table()`. `read_delta()` reads a local Delta Lake table the same way
(`pip install 'ibis-hardcharger[delta]'`).

```python
import ibis

with open("people.csv", "w") as f:
    f.write("id;name;joined\n1;Ann;2024-03-01\n2;Bob;2025-11-12\n")

con = ibis.hardcharger.connect(
    host="127.0.0.1", user="hardcharger", password="change-me", use_encryption=False
)
people = con.read_csv("people.csv", delim=";")
print(people.schema())
con.create_table("people", people, overwrite=True)
print(con.table("people").joined.year().max().execute())
```

```text
ibis.Schema {
  id      int64
  name    string
  joined  date
}
2025
```

A URL (`s3://`, `https://`, `gs://`, ...) is the server's to read, with its own credentials and
network: `read_parquet("s3://bucket/path/*.parquet")` makes a temporary view over it on the
server, and nothing passes through your machine.

`to_parquet()`, `to_csv()`, `to_json()` and `to_delta()` write on your machine too.

## Results

```python
import ibis

con = ibis.hardcharger.connect(
    host="127.0.0.1", user="hardcharger", password="change-me", use_encryption=False
)
numbers = con.sql("SELECT range AS n, range % 3 AS bucket FROM range(1000000)")
expr = numbers.group_by("bucket").agg(total=numbers.n.sum()).order_by("bucket")

print(expr.to_pyarrow())  # a pyarrow.Table
print(expr.to_polars())  # a polars.DataFrame
print(expr.total.max().execute())  # a scalar

with numbers.to_pyarrow_batches() as reader:  # streamed: nothing is held in memory
    print(sum(batch.num_rows for batch in reader))

print(ibis.to_sql(expr))  # the DuckDB SQL the server runs
```

```text
pyarrow.Table
bucket: int64
total: int64
----
bucket: [[0,1,2]]
total: [[166666833333,166666166667,166666500000]]
shape: (3, 2)
┌────────┬──────────────┐
│ bucket ┆ total        │
│ ---    ┆ ---          │
│ i64    ┆ i64          │
╞════════╪══════════════╡
│ 0      ┆ 166666833333 │
│ 1      ┆ 166666166667 │
│ 2      ┆ 166666500000 │
└────────┴──────────────┘
166666833333
1000000
SELECT
  *
FROM (
  SELECT
    "t0"."bucket",
    SUM("t0"."n") AS "total"
  FROM (
    SELECT
      range AS n,
      range % 3 AS bucket
    FROM RANGE(0, 1000000)
  ) AS "t0"
  GROUP BY
    1
) AS "t1"
ORDER BY
  "t1"."bucket" ASC
```

`con.sql()` takes DuckDB SQL, and `con.raw_sql()` runs any statement and returns the driver's
cursor.

## Good to know

- **The session is in UTC.** The backend sets the session's `TimeZone` to UTC when it connects,
  as Ibis's DuckDB backend does, so `TIMESTAMP WITH TIME ZONE` values come back in UTC.
- **A lost session is an error.** Ibis keeps state in the server session: that time zone, and the
  temporary tables behind `memtable()`, `read_*()` and `cache()`. When the server ends the
  session (idle eviction, a restart), the next statement fails with `session_not_found` instead
  of running in a new session without them; `con.reconnect()` starts afresh.
- **Bulk loads keep row order**: each is one ordered stream. (The driver alone would send a large
  load in parallel parts, which is faster and lands rows in no defined order.)
- **`create_table(..., overwrite=True)` swaps tables in one transaction**: other sessions see the
  old table or the new one, and a failed load leaves the old one in place.
- **Lambdas** (`array.map()`, `array.filter()`) are written as `lambda x: ...`, the form DuckDB
  2.0 takes; it refuses the `x -> ...` form Ibis 12 writes for DuckDB.
- **`INTERVAL` results** of a fixed length come back as durations (pandas `Timedelta`). One with
  months has no fixed length: `to_pyarrow()` refuses it; cast it to a string or take its parts.
- **Catalogs** are attached with `con.attach(path, name=...)` and removed with `con.detach(name)`.
  The path is on the server (`":memory:"` works too).

### Not supported

- **Python, pandas and PyArrow UDFs**: they run inside the client's process, and Hardcharger runs
  queries on the server. Builtin UDFs (`@ibis.udf.scalar.builtin`) call any function the server
  has.
- **DuckDB types Ibis 12 has no type for** (`BIT`, `BIGNUM`, `UHUGEINT`, `UNION`) are reported as
  `unknown`; cast them in SQL. A `HUGEINT` beyond 38 digits is refused by the server (it travels
  as `DECIMAL(38, 0)`).
- **The DuckDB backend's extras** that act on the engine's own file system or network
  (`read_postgres`, `read_mysql`, `read_sqlite`, `attach_sqlite`, `read_xlsx`, `to_xlsx`,
  `read_geo`, `register_filesystem`): run the SQL with `con.sql()` or `con.raw_sql()` instead.

### Coming from ibis-gizmosql

| | ibis-gizmosql | ibis-hardcharger |
| --- | --- | --- |
| Connect | `ibis.gizmosql.connect(...)`, `gizmosql://` | `ibis.hardcharger.connect(...)`, `hardcharger://` |
| TLS | off unless `use_encryption=True` | **on** unless `use_encryption=False` |
| URL parameters | `useEncryption`, `disableCertificateVerification`, `authType`, ... | the same names still work, as do the keyword arguments' own (`use_encryption`, ...) and the driver's `tls` |
| `oauth_port` | local callback port | gone: the sign-in runs on the server's own port |
| `read_csv()`, `read_parquet()`, `read_json()` | a table left in the database | a **temporary** table; `create_table()` keeps it |
| `ibis.memtable()` | a table in the current schema, dropped when the process exits | a temporary table of the session |
| `con.version` | DuckDB's version | Hardcharger's version |

## Development and testing

```bash
uv venv && source .venv/bin/activate && uv pip install -e ".[dev,delta]"
scripts/fetch_testing_data.sh       # the Ibis project's test data, at a pinned commit
ruff check . && ruff format --check . && mypy
pytest tests/unit                   # no server
pytest -n auto --dist loadgroup     # everything, against a live server
python scripts/run_readme_examples.py
```

The `hardcharger` development dependency downloads the released server binary on first use and
the tests start it on a free local port: no Docker. Tests that need the server **fail** when it
can't start; nothing is skipped for that, and a skip for any reason not listed in
`tests/conftest.py` fails the run.

- `tests/unit`: connection arguments and URLs, the entry point, compiled SQL.
- `tests/integration`: sign-in (password, token, TLS, client certificate, browser sign-in
  against a mock identity provider), sessions, tables, every supported type (nullable, `NOT
  NULL` and empty), client-side files. ibis-gizmosql's tests are carried over.
- `tests/ibis_suite`: [Ibis 12.0.0's own backend tests](https://github.com/ibis-project/ibis/tree/12.0.0/ibis/backends/tests),
  as Ibis wrote them, with its TPC-H and TPC-DS queries. Tests Ibis expects to fail on DuckDB are
  expected to fail here the same way; the handful that differ are listed, with reasons, in
  `tests/ibis_suite/conftest.py`.
- `scripts/run_readme_examples.py` runs every Python example on this page, as written.

CI runs all of it on Python 3.11 to 3.15, once more with the lowest dependency versions
`pyproject.toml` allows, and publishes from version tags with PyPI Trusted Publishing. See
[releasing](https://github.com/gizmodata/ibis-hardcharger/blob/main/docs/releasing.md).

## License

Apache License 2.0; see [LICENSE](https://github.com/gizmodata/ibis-hardcharger/blob/main/LICENSE).
Adapted from [ibis-gizmosql](https://github.com/gizmodata/ibis-gizmosql) and the DuckDB backend of
[Ibis](https://github.com/ibis-project/ibis) (Apache-2.0).

---

<p align="center"><sub>Hardcharger™ and GizmoData™ are trademarks of GizmoData LLC.</sub></p>
