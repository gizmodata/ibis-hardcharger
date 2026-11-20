# Changelog

All notable changes to ibis-hardcharger are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Requires `adbc-driver-hardcharger` 2.7.0 or newer (was 2.5.0), and uses its option names, which
  follow the ADBC driver style guide: `hardcharger.ingest.parts` for ordered bulk loads and
  `hardcharger.session.reconnect` (was `adbc.chargewire.*`), taken from the driver's
  `StatementOptions` and `DatabaseOptions`. The driver prints no deprecation line for them.

### Fixed

- `connect(db_kwargs=...)` with session replacement under any name the driver reads
  (`hardcharger.session.reconnect`, `chargewire.session.reconnect`,
  `adbc.chargewire.session.reconnect`, `adbc.warp.session.reconnect`) is sent as given, without
  Ibis's default `"false"` added beside it under another name, where the driver would have taken
  one of the two values.

## [1.0.1] - 2026-11-13

### Changed

- Requires `adbc-driver-hardcharger` 2.5.0 or newer (was 2.4.9). That driver applies a catalog and
  a schema given together at connect in that order; earlier ones usually applied the schema first,
  in the server's default catalog. `connect(database=..., schema=...)` and a `/catalog/schema` URL
  now pass both to the driver, where 1.0.0 connected with neither and sent `USE` afterwards. The
  session starts in the same place as before.

## [1.0.0] - 2026-11-13

The first release: the [Ibis](https://ibis-project.org) backend for
[Hardcharger](https://github.com/gizmodata/hardcharger), GizmoData's DuckDB 2.0 server, ported
from [ibis-gizmosql](https://github.com/gizmodata/ibis-gizmosql) 1.1.1 with its tests.

### Added

- `ibis.hardcharger.connect(...)` and `ibis.connect("hardcharger://...")`, on
  `adbc-driver-hardcharger` 2.4.9 (the Chargewire protocol over HTTP(S)). TLS is on by default;
  sign in with a password, an access token, a client certificate (mutual TLS) or the browser
  (`auth_type="external"`). Every other argument of the driver's `connect()` passes through
  (`tls_root_certs`, `settings`, `session_tag`, ...). A URL's query can set the connection
  options only: it can't name a program to run (`open_browser` is true or false there), adopt
  a session or pass driver options through; those are keyword arguments.
- Expressions compile with Ibis 12.0's DuckDB compiler. Lambdas (`array.map()`,
  `array.filter()`) are written as `lambda x: ...`, the form DuckDB 2.0 takes.
- In-memory data (pandas, Polars, PyArrow tables, record batch readers and datasets) is
  bulk-loaded as Arrow by `create_table()`, `insert()` and `ibis.memtable()`: one transaction
  per load, rows in their order, into a table with the Ibis schema's column types. `insert()`
  matches columns by name.
- `create_table(..., overwrite=True)` replaces the table in one transaction.
- `read_csv()`, `read_parquet()`, `read_json()` and `read_delta()` read files on the client and
  load them into temporary tables; URLs (`s3://`, `https://`, ...) are read by the server through
  a temporary view. `to_parquet()`, `to_csv()`, `to_json()` and `to_delta()` write on the client.
- Results as Arrow tables, record batch streams, pandas and Polars. `INTERVAL` results of a
  fixed length come back as durations.
- `attach()` / `detach()` for catalogs, `create_database()` / `drop_database()` for schemas,
  `load_extension()`, and `con.settings` for the session's DuckDB settings.
- The `hardcharger` sqlglot dialect.
- Tests against a live Hardcharger server (2.12.0): Ibis 12.0.0's own backend suite with its
  TPC-H and TPC-DS queries, every supported type (nullable, `NOT NULL` and empty), sign-in over
  TLS, by client certificate and through the browser, and the README's examples. Python 3.11
  to 3.15.

### Changed

Differences from ibis-gizmosql:

- TLS is on unless `use_encryption=False` (ibis-gizmosql: off unless `use_encryption=True`).
- `read_csv()`, `read_parquet()`, `read_json()` and `ibis.memtable()` make temporary tables of
  the session, not tables in the database.
- A session the server ends is an error (`session_not_found`) until `con.reconnect()`, instead
  of a new session without the UTC time zone and temporary tables Ibis set up.
- `con.version` is Hardcharger's version, not DuckDB's.
- `oauth_port` is gone: browser sign-in runs on the server's own port.
- Not carried over: `read_postgres`, `read_mysql`, `read_sqlite`, `attach_sqlite`, `read_xlsx`,
  `to_xlsx`, `read_geo` and `register_filesystem`, which act on the engine's own file system or
  network (or need an in-process DuckDB); run the SQL with `con.sql()` or `con.raw_sql()`.
- sqlglot is held at 30.17: Ibis 12.0.0's `drop_table()` and `drop_view()` send a `DROP` with no
  name from sqlglot 30.18 on.
