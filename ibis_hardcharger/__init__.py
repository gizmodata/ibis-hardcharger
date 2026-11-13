"""Ibis backend for Hardcharger, GizmoData's DuckDB 2.0 server.

Expressions compile to DuckDB SQL with Ibis's DuckDB compiler and run on the server through
``adbc-driver-hardcharger`` (the Chargewire protocol over HTTP(S)); results come back as Arrow.

    import ibis

    con = ibis.hardcharger.connect(host="db.example.com", user="joe", password="...")
    con = ibis.connect("hardcharger://joe:...@db.example.com")
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import re
from typing import TYPE_CHECKING, Any, Literal

import ibis
import ibis.common.exceptions as exc
import ibis.expr.operations as ops
import ibis.expr.schema as sch
import ibis.expr.types as ir
import sqlglot as sg
import sqlglot.expressions as sge
from adbc_driver_hardcharger import dbapi as hardcharger
from ibis import util
from ibis.backends import (
    CanCreateDatabase,
    CanListCatalog,
    DirectExampleLoader,
    HasCurrentCatalog,
    HasCurrentDatabase,
    SupportsTempTables,
)
from ibis.backends.sql import SQLBackend
from ibis.backends.sql.compilers.base import STAR, AlterTable, C, RenameTable

from ibis_hardcharger._compiler import Hardcharger, HardchargerCompiler, compiler
from ibis_hardcharger._connect import driver_kwargs, kwargs_from_url
from ibis_hardcharger.converter import (
    HardchargerPandasData,
    HardchargerPyArrowData,
    durations_for_intervals,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence
    from pathlib import Path
    from urllib.parse import ParseResult

    import pandas as pd
    import polars as pl
    import pyarrow as pa
    from ibis.expr.schema import IntoSchema

__version__ = importlib.metadata.version("ibis-hardcharger")

__all__ = ["Backend", "Hardcharger", "HardchargerCompiler", "__version__"]

#: Rows per Arrow record batch in a bulk load. The server takes at most 64 MiB per Arrow
#: message (``max_ingest_message_bytes``), so very wide rows need a smaller ``batch_size``.
INGEST_BATCH_ROWS = 122_880

# One ordered stream per bulk load, so a table keeps the order of the data it was loaded
# from. (The driver's default sends a load of two batches or more as up to 8 parallel parts,
# whose rows land in no defined order.)
_ORDERED_INGEST = {"adbc.chargewire.ingest.parts": "1"}

# Ibis keeps state in the server session (the UTC time zone it sets, temporary tables behind
# memtables, read_*() and cache()), and a session the driver opens to replace a lost one has
# none of it. So a lost session is an error here, and Backend.reconnect() starts afresh.
_SESSION_RECONNECT = "adbc.chargewire.session.reconnect"

_SQLSTATE_UNDEFINED_TABLE = "42P01"
_SETTING_NAME = re.compile(pattern=r"[A-Za-z_][A-Za-z0-9_.]*")


class _Settings:
    """The session's DuckDB settings: ``con.settings["threads"]``."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    def __getitem__(self, key: str) -> Any:
        with self._backend._safe_raw_sql(
            "SELECT value FROM duckdb_settings() WHERE name = ?", parameters=[key]
        ) as cur:
            row = cur.fetchone()
        if row is None:
            raise KeyError(key)
        return row[0]

    def __setitem__(self, key: str, value: Any) -> None:
        if _SETTING_NAME.fullmatch(string=key) is None:
            raise ValueError(f"invalid setting name: {key!r}")
        literal = sge.convert(value=str(value)).sql(dialect="duckdb")
        with self._backend._safe_raw_sql(f"SET {key} = {literal}"):
            pass

    def __repr__(self) -> str:
        with self._backend._safe_raw_sql(
            "SELECT name, value FROM duckdb_settings() ORDER BY name"
        ) as cur:
            return repr(dict(cur.fetchall()))


class Backend(
    SupportsTempTables,
    SQLBackend,
    CanListCatalog,
    CanCreateDatabase,
    HasCurrentCatalog,
    HasCurrentDatabase,
    DirectExampleLoader,
):
    name = "hardcharger"
    compiler = compiler
    supports_temporary_tables = True
    supports_python_udfs = False

    con: hardcharger.Connection

    # ---- connecting -------------------------------------------------------------------

    def _from_url(self, url: ParseResult, **kwargs: Any) -> Backend:
        """Connect with ``ibis.connect("hardcharger://...")``.

        ``hardcharger://[user[:password]@]host[:port][/catalog[/schema]][?option=value&...]``.
        TLS is on unless the URL says ``?tls=false``. Every ``connect()`` argument can be a
        query parameter (``?auth_type=external``, ``?disable_certificate_verification=true``);
        percent-encode special characters in the user name and password.
        """
        return self.connect(**kwargs_from_url(url=url, kwargs=kwargs))

    def do_connect(
        self,
        host: str | None = None,
        user: str | None = None,
        password: str | None = None,
        port: int | None = None,
        database: str | None = None,
        schema: str | None = None,
        *,
        uri: str | None = None,
        use_encryption: bool = True,
        disable_certificate_verification: bool = False,
        auth_type: Literal["password", "external", "client_certificate", "mtls"] = "password",
        access_token: str | None = None,
        extensions: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Create an Ibis client connected to a Hardcharger server.

        Parameters
        ----------
        host
            The server's host name or address.
        user
            User name, for password sign-in.
        password
            Password, for password sign-in.
        port
            The server's port. Default: 31337, or 443 for a GizmoData Cloud host
            (``gizmodata.com`` or a name under it).
        database
            The catalog to start in. Default: the server's.
        schema
            The schema to start in. Default: the server's.
        uri
            The driver's own URI, instead of `host`, `port` and `use_encryption`:
            ``hardcharger://host[:port][/prefix][?tls=false]``.
        use_encryption
            Connect over TLS (HTTPS). On by default; ``False`` is plain HTTP, for a server on
            your own machine.
        disable_certificate_verification
            Don't verify the server's TLS certificate (development only).
        auth_type
            ``"password"`` (the default, also for `access_token`), ``"external"`` to sign in
            through a browser with the server's identity provider (no user name or password),
            or ``"client_certificate"`` (``"mtls"``) to sign in with a TLS client certificate.
        access_token
            A Hardcharger access token obtained elsewhere, instead of a user name and password.
        extensions
            DuckDB extensions to install and load on the server for this session.
        kwargs
            More arguments for ``adbc_driver_hardcharger.dbapi.connect()``, passed as they
            are: ``tls_root_certs``, ``tls_client_cert``, ``tls_client_key``,
            ``tls_server_name``, ``subject_token``, ``oauth_timeout``, ``open_browser``,
            ``share_sign_in``, ``resume_timeout``, ``settings``, ``session_tag``,
            ``query_tag``, ``profile``, ``db_kwargs`` and ``conn_kwargs``.

        Examples
        --------
        >>> import ibis
        >>> con = ibis.hardcharger.connect(  # doctest: +SKIP
        ...     host="db.example.com", user="joe", password="..."
        ... )
        >>> con = ibis.hardcharger.connect(  # doctest: +SKIP
        ...     host="db.example.com", auth_type="external"
        ... )
        """
        connect_kwargs = driver_kwargs(
            host=host,
            user=user,
            password=password,
            port=port,
            database=database,
            schema=schema,
            uri=uri,
            use_encryption=use_encryption,
            disable_certificate_verification=disable_certificate_verification,
            auth_type=auth_type,
            access_token=access_token,
            extra=kwargs,
        )
        db_kwargs = dict(connect_kwargs.get("db_kwargs") or {})
        db_kwargs.setdefault(_SESSION_RECONNECT, "false")
        connect_kwargs["db_kwargs"] = db_kwargs
        if (previous := getattr(self, "con", None)) is not None:
            # reconnect(): the old connection may be one whose session the server lost.
            with contextlib.suppress(Exception):
                previous.close()
        # The temporary tables behind in-memory tables belonged to the old session.
        self._memtables.clear()
        # Given both a catalog and a schema, adbc-driver-hardcharger 2.4.9 (against
        # Hardcharger 2.12.0) sets the schema first, in the server's default catalog, and
        # then the catalog, which puts the session in that catalog's `main`: the schema is
        # lost, or refused when the default catalog has none of that name. Each alone works,
        # so the pair is set here, with USE.
        start_in = None
        if "catalog" in connect_kwargs and "db_schema" in connect_kwargs:
            start_in = sg.table(
                connect_kwargs.pop("db_schema"), db=connect_kwargs.pop("catalog"), quoted=True
            )
        self.con = hardcharger.connect(**connect_kwargs)
        try:
            if start_in is not None:
                self._run(f"USE {start_in.sql(dialect=self.dialect)}")
            self._post_connect(extensions=extensions)
        except BaseException:
            self.con.close()
            raise

    @util.experimental
    @classmethod
    def from_connection(
        cls,
        con: hardcharger.Connection,
        /,
        *,
        extensions: Sequence[str] | None = None,
    ) -> Backend:
        """Create an Ibis client from an ``adbc_driver_hardcharger`` connection.

        The connection must be in autocommit mode (the driver's default). Its session's
        time zone is set to UTC. Open it with
        ``db_kwargs={"adbc.chargewire.session.reconnect": "false"}`` so that a session the
        server loses is an error instead of a new session without Ibis's temporary tables.

        Parameters
        ----------
        con
            An open ``adbc_driver_hardcharger.dbapi.Connection``.
        extensions
            DuckDB extensions to install and load on the server for this session.
        """
        new_backend = cls(extensions=extensions)
        new_backend._can_reconnect = False
        new_backend.con = con
        new_backend._post_connect(extensions=extensions)
        return new_backend

    def _post_connect(self, extensions: Sequence[str] | None = None) -> None:
        vendor = self.con.adbc_get_info().get("vendor_name")
        if vendor != "Hardcharger":
            raise exc.IbisError(
                f"the hardcharger backend needs a Hardcharger server, not {vendor!r}"
            )
        if extensions is not None:
            self._load_extensions(extensions=list(extensions))
        # As Ibis's DuckDB backend does: TIMESTAMP WITH TIME ZONE values come back in the
        # session's time zone, and Ibis works in UTC.
        self.settings["TimeZone"] = "UTC"

    def disconnect(self) -> None:
        """Close the connection, which ends its session on the server."""
        self.con.close()

    # ---- session ------------------------------------------------------------------------

    @property
    def settings(self) -> _Settings:
        """The session's DuckDB settings, read and set by name."""
        return _Settings(backend=self)

    @property
    def version(self) -> str:
        """The Hardcharger server's version, e.g. ``2.12.0``."""
        return self.con.server_version()

    @property
    def current_catalog(self) -> str:
        with self._safe_raw_sql(sg.select(self.compiler.f.current_database())) as cur:
            [(catalog,)] = cur.fetchall()
        return catalog

    @property
    def current_database(self) -> str:
        with self._safe_raw_sql(sg.select(self.compiler.f.current_schema())) as cur:
            [(database,)] = cur.fetchall()
        return database

    # ---- running SQL --------------------------------------------------------------------

    def raw_sql(self, query: str | sge.Expression, **kwargs: Any) -> hardcharger.Cursor:
        """Run a statement and return its open cursor; close it when done.

        Parameters
        ----------
        query
            DuckDB SQL (several statements may be separated by ``;``), or a sqlglot expression.
        kwargs
            ``parameters=[...]`` binds values to the statement's ``?`` placeholders.
        """
        if isinstance(query, sge.Expression):
            query = query.sql(dialect=self.dialect)
        cur = self.con.cursor()
        try:
            cur.execute(operation=query, **kwargs)
        except BaseException:
            cur.close()
            raise
        return cur

    @contextlib.contextmanager
    def _safe_raw_sql(
        self, query: str | sge.Expression, **kwargs: Any
    ) -> Iterator[hardcharger.Cursor]:
        cur = self.raw_sql(query, **kwargs)
        try:
            yield cur
        finally:
            cur.close()

    def _run(self, query: str | sge.Expression) -> None:
        """Run a statement that returns no rows."""
        with self._safe_raw_sql(query):
            pass

    def _ingest(
        self,
        *,
        table_name: str,
        data: pa.Table | pa.RecordBatchReader,
        mode: Literal["create", "append", "replace", "create_append"],
        temporary: bool = False,
        catalog: str | None = None,
        database: str | None = None,
    ) -> int:
        """Bulk-load Arrow data into a table, as one ordered stream in one transaction.

        A table is sent in batches of at most `INGEST_BATCH_ROWS` rows. Each load gets its
        own cursor: a cursor keeps the ingest options of the load before.
        """
        import pyarrow as pa

        if isinstance(data, pa.Table):
            data = pa.RecordBatchReader.from_batches(
                schema=data.schema, batches=data.to_batches(max_chunksize=INGEST_BATCH_ROWS)
            )
        target: dict[str, Any] = {}
        if temporary:
            target["temporary"] = True
        else:
            if catalog is not None:
                target["catalog_name"] = catalog
            if database is not None:
                target["db_schema_name"] = database
        with self.con.cursor(adbc_stmt_kwargs=_ORDERED_INGEST) as cur:
            return cur.adbc_ingest(table_name=table_name, data=data, mode=mode, **target)

    # ---- catalogs, databases and tables ---------------------------------------------------

    def list_catalogs(self, *, like: str | None = None) -> list[str]:
        col = "catalog_name"
        query = sg.select(sge.Distinct(expressions=[sg.column(col)])).from_(
            sg.table("schemata", db="information_schema")
        )
        with self._safe_raw_sql(query) as cur:
            result = cur.fetch_arrow_table()
        return self._filter_with_like(result[col].to_pylist(), like)

    def list_databases(self, *, like: str | None = None, catalog: str | None = None) -> list[str]:
        col = "schema_name"
        query = sg.select(sge.Distinct(expressions=[sg.column(col)])).from_(
            sg.table("schemata", db="information_schema")
        )
        if catalog is not None:
            query = query.where(sg.column("catalog_name").eq(sge.convert(value=catalog)))
        with self._safe_raw_sql(query) as cur:
            out = cur.fetch_arrow_table()
        return self._filter_with_like(out[col].to_pylist(), like)

    def list_tables(
        self, *, like: str | None = None, database: tuple[str, str] | str | None = None
    ) -> list[str]:
        table_loc = self._to_sqlglot_table(database)

        catalog = table_loc.catalog or self.current_catalog
        schema = table_loc.db or self.current_database

        # As Ibis's DuckDB backend: the tables and views of the schema, with the session's
        # temporary ones of the same schema name (they all live in `temp.main`).
        where = sg.and_(
            C.table_catalog.isin(sge.convert(value=catalog), sge.convert(value="temp")),
            C.table_schema.eq(sge.convert(value=schema)),
        )
        if database is None:
            # Asked about "here": the session's temporary tables are here, whatever the
            # current schema is called.
            where = sg.or_(where, C.table_catalog.eq(sge.convert(value="temp")))

        col = "table_name"
        sql = (
            sg.select(col)
            .from_(sg.table("tables", db="information_schema"))
            .distinct()
            .where(where)
            .sql(self.dialect)
        )
        with self._safe_raw_sql(sql) as cur:
            out = cur.fetch_arrow_table()
        return self._filter_with_like(out[col].to_pylist(), like)

    def create_database(
        self, name: str, /, *, catalog: str | None = None, force: bool = False
    ) -> None:
        if catalog is not None:
            raise exc.UnsupportedOperationError(
                "Hardcharger cannot create a database in another catalog."
            )
        this = sg.table(name, catalog=catalog, quoted=self.compiler.quoted)
        self._run(sge.Create(this=this, kind="SCHEMA", replace=force))

    def drop_database(
        self, name: str, /, *, catalog: str | None = None, force: bool = False
    ) -> None:
        if catalog is not None and catalog != self.current_catalog:
            raise exc.UnsupportedOperationError(
                "Hardcharger cannot drop a database in another catalog."
            )
        this = sg.table(name, catalog=catalog, quoted=self.compiler.quoted)
        self._run(sge.Drop(this=this, kind="SCHEMA", replace=force))

    def attach(self, path: str | Path, name: str | None = None, read_only: bool = False) -> None:
        """Attach another DuckDB database to the server, as a catalog.

        Parameters
        ----------
        path
            The database file, **on the server's file system**, or ``":memory:"``.
        name
            The catalog's name. Defaults to the basename of `path`.
        read_only
            Whether to attach the database as read-only.
        """
        code = f"ATTACH {sge.convert(value=str(path)).sql(dialect=self.dialect)}"
        if name is not None:
            code += f" AS {sg.to_identifier(name, quoted=True).sql(dialect=self.dialect)}"
        if read_only:
            code += " (READ_ONLY)"
        self._run(code)

    def detach(self, name: str) -> None:
        """Detach a catalog from the server.

        Parameters
        ----------
        name
            The catalog's name.
        """
        self._run(f"DETACH {sg.to_identifier(name, quoted=True).sql(dialect=self.dialect)}")

    def _load_extensions(self, extensions: list[str], force_install: bool = False) -> None:
        f = self.compiler.f
        query = (
            sg.select(
                f.anon.unnest(
                    f.list_intersect(
                        f.list_append(C.aliases, C.extension_name),
                        f.list_value(*extensions),
                    )
                ),
                C.installed,
                C.loaded,
            )
            .from_(f.duckdb_extensions())
            .where(sg.not_(C.installed & C.loaded))
        )
        with self._safe_raw_sql(query) as cur:
            not_installed_or_loaded = cur.fetchall()
        commands = [
            "FORCE " * force_install + f"INSTALL {sge.convert(value=extension).sql()}"
            for extension, installed, _ in not_installed_or_loaded
            if not installed
        ]
        commands.extend(
            f"LOAD {sge.convert(value=extension).sql()}"
            for extension, _, loaded in not_installed_or_loaded
            if not loaded
        )
        if commands:
            self._run("; ".join(commands))

    def load_extension(self, extension: str, force_install: bool = False) -> None:
        """Install and load a DuckDB extension on the server, by name.

        Parameters
        ----------
        extension
            The extension name.
        force_install
            Force reinstallation of the extension.
        """
        self._load_extensions(extensions=[extension], force_install=force_install)

    def table(self, name: str, /, *, database: str | tuple[str, str] | None = None) -> ir.Table:
        table_loc = self._to_sqlglot_table(database)

        catalog = table_loc.catalog or None
        database = table_loc.db or None

        table_schema = self.get_schema(name, catalog=catalog, database=database)
        return ops.DatabaseTable(
            name,
            schema=table_schema,
            source=self,
            namespace=ops.Namespace(catalog=catalog, database=database),
        ).to_expr()

    def _describe(self, query: str) -> sch.Schema:
        with self._safe_raw_sql(query) as cur:
            meta = cur.fetch_arrow_table().to_pydict()
        type_mapper = self.compiler.type_mapper
        return sch.Schema(
            {
                name: type_mapper.from_string(typ, nullable=null == "YES")
                for name, typ, null in zip(
                    meta["column_name"], meta["column_type"], meta["null"], strict=True
                )
            }
        )

    def get_schema(
        self,
        table_name: str,
        *,
        catalog: str | None = None,
        database: str | None = None,
    ) -> sch.Schema:
        """Compute the schema of a table.

        Parameters
        ----------
        table_name
            May **not** be fully qualified. Use `database` if you want to
            qualify the identifier.
        catalog
            Catalog name
        database
            Database name

        Returns
        -------
        sch.Schema
            Ibis schema
        """
        query = sge.Describe(
            this=sg.table(table_name, db=database, catalog=catalog, quoted=self.compiler.quoted)
        ).sql(self.dialect)
        try:
            return self._describe(query=query)
        except hardcharger.Error as e:
            if e.sqlstate == _SQLSTATE_UNDEFINED_TABLE:
                raise exc.TableNotFound(table_name) from e
            raise

    def _get_schema_using_query(self, query: str) -> sch.Schema:
        return self._describe(query=f"DESCRIBE {query}")

    def create_table(
        self,
        name: str,
        /,
        obj: ir.Table | pd.DataFrame | pa.Table | pl.DataFrame | pl.LazyFrame | None = None,
        *,
        schema: IntoSchema | None = None,
        database: str | None = None,
        temp: bool = False,
        overwrite: bool = False,
    ) -> ir.Table:
        """Create a table in Hardcharger.

        In-memory data (a pandas or Polars DataFrame, a PyArrow table, record batch reader
        or dataset, ...) is bulk-loaded as Arrow; an Ibis expression runs on the server.

        Parameters
        ----------
        name
            Name of the table to create
        obj
            The data with which to populate the table; optional, but at least
            one of `obj` or `schema` must be specified
        schema
            The schema of the table to create; optional, but at least one of
            `obj` or `schema` must be specified
        database
            The name of the database in which to create the table; if not
            passed, the current database is used.

            For multi-level table hierarchies, you can pass in a dotted string
            path like `"catalog.database"` or a tuple of strings like
            `("catalog", "database")`.
        temp
            Create a temporary table
        overwrite
            If `True`, replace the table if it already exists, otherwise fail
            if the table exists
        """
        import pyarrow as pa

        table_loc = self._to_sqlglot_table(database)

        if getattr(table_loc, "catalog", False) and temp:
            raise exc.UnsupportedArgumentError(
                "Hardcharger can only create temporary tables in the `temp` catalog. "
                "Don't specify a catalog to enable temp table creation."
            )

        catalog = table_loc.catalog or self.current_catalog
        database = table_loc.db or self.current_database

        if obj is None and schema is None:
            raise ValueError("Either `obj` or `schema` must be specified")

        quoted = self.compiler.quoted
        dialect = self.dialect

        properties = []
        if temp:
            properties.append(sge.TemporaryProperty())
            catalog = "temp"
            database = "main"

        table: ir.Table | None = None
        query: sge.Query | None = None
        reader: pa.RecordBatchReader | None = None
        if isinstance(obj, pa.RecordBatchReader):
            reader = obj
            if schema is None:
                schema = sch.Schema.from_pyarrow(obj.schema)
        elif obj is not None and not isinstance(obj, ir.Expr):
            table = ibis.memtable(obj)
            reader = self._memtable_reader(op=table.op())
        elif obj is not None:
            table = obj
            self._run_pre_execute_hooks(table)
            query = self.compiler.to_sqlglot(table)

        if schema is None:
            assert table is not None
            schema = table.schema()
        else:
            schema = ibis.schema(schema)

        if null_fields := schema.null_fields:
            raise exc.IbisTypeError(
                "Hardcharger does not support creating tables with NULL typed columns. "
                "Ensure that every column has non-NULL type. "
                f"NULL columns: {null_fields}"
            )

        initial_name = util.gen_name("hardcharger_table") if overwrite else name
        initial_table = sg.table(initial_name, catalog=catalog, db=database, quoted=quoted)
        final_table = sg.table(name, catalog=catalog, db=database, quoted=quoted)

        self._run(
            sge.Create(
                kind="TABLE",
                this=sge.Schema(
                    this=initial_table, expressions=schema.to_sqlglot_column_defs(dialect)
                ),
                properties=sge.Properties(expressions=properties),
            )
        )
        try:
            if query is not None:
                assert table is not None
                columns = [sge.to_identifier(col, quoted=quoted) for col in table.columns]
                self._run(sge.insert(query, into=initial_table, columns=columns))
            elif reader is not None:
                self._ingest(
                    table_name=initial_name,
                    data=reader,
                    mode="append",
                    temporary=temp,
                    catalog=catalog,
                    database=database,
                )
            if overwrite:
                self._swap_tables(new=initial_table, old=final_table)
        except BaseException:
            # Leave no half-made table behind: the new one, or its stand-in under overwrite.
            with contextlib.suppress(Exception):
                self._run(sge.Drop(kind="TABLE", this=initial_table, exists=True))
            raise

        return self.table(name, database=(catalog, database))

    def _in_transaction(self) -> bool:
        """Whether the caller has opened a transaction in this session (``raw_sql("BEGIN")``).

        Inside one, every statement has the same transaction id; in autocommit each statement
        gets its own. (Asking by trying ``BEGIN`` would abort the caller's transaction.)
        """
        ids = []
        for _ in range(2):
            with self._safe_raw_sql("SELECT txid_current()") as cur:
                ids.append(cur.fetchone()[0])
        return ids[0] == ids[1]

    def _swap_tables(self, *, new: sge.Table, old: sge.Table) -> None:
        """Replace table `old` with table `new`, in one transaction.

        Other sessions see the old table or the new one, and a failure leaves the old one in
        place. (Two statements in one request are not one transaction by themselves.) Inside a
        transaction the caller opened, the two statements join it.
        """
        dialect = self.dialect
        statements = [
            sge.Drop(kind="TABLE", this=old, exists=True).sql(dialect),
            AlterTable(this=new, actions=[RenameTable(this=old)]).sql(dialect),
        ]
        if self._in_transaction():
            self._run("; ".join(statements))
            return
        try:
            self._run("; ".join(["BEGIN TRANSACTION", *statements, "COMMIT"]))
        except BaseException:
            with contextlib.suppress(Exception):
                self._run("ROLLBACK")
            raise

    def insert(
        self,
        name: str,
        /,
        obj: pd.DataFrame | ir.Table | list | dict,
        *,
        database: str | None = None,
        overwrite: bool = False,
    ) -> None:
        """Insert data into a table.

        In-memory data whose column names are all columns of the table is bulk-loaded as
        Arrow and matched to the table's columns **by name** (columns it leaves out get
        their defaults). An Ibis expression, or data with other column names (matched by
        position), is inserted with SQL.

        Parameters
        ----------
        name
            The name of the table to which data will be inserted
        obj
            The source data or expression to insert
        database
            Name of the attached database that the table is located in.

            For multi-level table hierarchies, you can pass in a dotted string path like
            `"catalog.database"` or a tuple of strings like `("catalog", "database")`.
        overwrite
            If `True` then replace existing contents of table
        """
        if isinstance(obj, ir.Table):
            return super().insert(name, obj, database=database, overwrite=overwrite)

        table_loc = self._to_sqlglot_table(database)
        catalog, db = self._to_catalog_db_tuple(table_loc)

        source = ibis.memtable(obj)
        target_schema = self.get_schema(name, catalog=catalog, database=db)
        if not source.schema().keys() <= target_schema.keys():
            return super().insert(name, source, database=database, overwrite=overwrite)

        if overwrite:
            self.truncate_table(name, database=database)
        # Without a catalog or schema the server resolves the name as SQL does, so a
        # temporary table is found first.
        self._ingest(
            table_name=name,
            data=self._memtable_reader(op=source.op()),
            mode="append",
            catalog=catalog,
            database=db,
        )
        return None

    # ---- in-memory tables -----------------------------------------------------------------

    def _memtable_reader(self, *, op: ops.InMemoryTable) -> pa.RecordBatchReader:
        """The data of an in-memory table as a stream of Arrow record batches."""
        import pyarrow as pa

        data = op.data
        try:
            table = data.to_pyarrow(op.schema)
        except exc.UnsupportedOperationError:
            # A PyArrow dataset: scanned batch by batch, never held in memory whole.
            return data.to_pyarrow_dataset(op.schema).scanner().to_reader()
        return pa.RecordBatchReader.from_batches(
            schema=table.schema, batches=table.to_batches(max_chunksize=INGEST_BATCH_ROWS)
        )

    def _register_in_memory_table(self, op: ops.InMemoryTable) -> None:
        """Bulk-load an in-memory table into a temporary table of the session."""
        reader = self._memtable_reader(op=op)
        schema = op.schema
        if schema.null_fields:
            # DuckDB has no column type for "always NULL": let the load pick the types.
            self._ingest(table_name=op.name, data=reader, mode="replace", temporary=True)
            return
        this = sg.table(op.name, quoted=self.compiler.quoted)
        self._run(
            sge.Create(
                kind="TABLE",
                this=sge.Schema(this=this, expressions=schema.to_sqlglot_column_defs(self.dialect)),
                replace=True,
                properties=sge.Properties(expressions=[sge.TemporaryProperty()]),
            )
        )
        self._ingest(table_name=op.name, data=reader, mode="append", temporary=True)

    # ---- user-defined functions -------------------------------------------------------------

    def _register_udfs(self, expr: ir.Expr) -> None:
        for udf_node in expr.op().find(ops.ScalarUDF):
            getattr(self, f"_register_{udf_node.__input_type__.name.lower()}_udf")(udf_node)

    def _no_python_udfs(self, udf_node: ops.ScalarUDF) -> None:
        raise exc.UnsupportedOperationError(
            f"{type(udf_node).__name__}: Python, pandas and PyArrow UDFs run inside the "
            "client's process, and Hardcharger runs queries on the server. Use a builtin UDF "
            "(@ibis.udf.scalar.builtin) to call a function the server has."
        )

    _register_python_udf = _no_python_udfs
    _register_pyarrow_udf = _no_python_udfs
    _register_pandas_udf = _no_python_udfs

    # ---- results ----------------------------------------------------------------------------

    def _to_arrow_table(
        self,
        expr: ir.Expr,
        *,
        params: Mapping[ir.Scalar, Any] | None = None,
        limit: int | str | None = None,
        **kwargs: Any,
    ) -> pa.Table:
        self._run_pre_execute_hooks(expr)
        sql = self.compile(expr.as_table(), limit=limit, params=params, **kwargs)
        with self._safe_raw_sql(sql) as cur:
            return cur.fetch_arrow_table()

    def to_pyarrow(
        self,
        expr: ir.Expr,
        /,
        *,
        params: Mapping[ir.Scalar, Any] | None = None,
        limit: int | str | None = None,
        **kwargs: Any,
    ) -> pa.Table:
        table = self._to_arrow_table(expr, params=params, limit=limit, **kwargs)
        return expr.__pyarrow_result__(table, data_mapper=HardchargerPyArrowData)

    def to_pyarrow_batches(
        self,
        expr: ir.Expr,
        /,
        *,
        params: Mapping[ir.Scalar, Any] | None = None,
        limit: int | str | None = None,
        chunk_size: int = 1_000_000,
        **_: Any,
    ) -> pa.ipc.RecordBatchReader:
        """Return a stream of record batches.

        The result is streamed from the server as it is read. The statement's cursor is
        closed when the stream is exhausted or closed.

        Parameters
        ----------
        expr
            Ibis expression
        params
            Bound parameters
        limit
            Limit the result to this number of rows
        chunk_size
            The most rows per record batch (the server's batches are split, never merged)
        """
        import pyarrow as pa

        self._run_pre_execute_hooks(expr)
        table_expr = expr.as_table()
        sql = self.compile(table_expr, limit=limit, params=params)
        schema = table_expr.schema()
        target = schema.to_pyarrow()

        cur = self.raw_sql(sql)

        def batches() -> Iterator[pa.RecordBatch]:
            try:
                for batch in cur.fetch_record_batch():
                    table = HardchargerPyArrowData.convert_table(
                        pa.Table.from_batches(batches=[batch]), schema
                    )
                    yield from table.to_batches(max_chunksize=chunk_size)
            finally:
                cur.close()

        return pa.RecordBatchReader.from_batches(schema=target, batches=batches())

    def execute(
        self,
        expr: ir.Expr,
        /,
        *,
        params: Mapping[ir.Scalar, Any] | None = None,
        limit: int | str | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame | pd.Series | Any:
        """Execute an expression."""
        import pandas as pd
        import pyarrow.types as pat

        table = self._to_arrow_table(expr, params=params, limit=limit, **kwargs)
        table = durations_for_intervals(table=table, schema=expr.as_table().schema())

        df = pd.DataFrame(
            {
                name: (
                    col.to_pylist()
                    if (
                        pat.is_nested(col.type)
                        or pat.is_dictionary(col.type)
                        # a column of NULL literals has a type, but its values are None
                        or col.null_count
                    )
                    else col.to_pandas()
                )
                for name, col in zip(table.column_names, table.columns, strict=True)
            }
        )
        df = HardchargerPandasData.convert_table(df, expr.as_table().schema())
        return expr.__pandas_result__(df)

    @util.experimental
    def to_json(
        self,
        expr: ir.Table,
        /,
        path: str | Path,
        *,
        compression: Literal["auto", "none", "gzip", "zstd"] = "auto",
        dateformat: str | None = None,
        timestampformat: str | None = None,
    ) -> None:
        """Write the results of `expr` to a JSON file of an array of objects, on this machine.

        The result is streamed from the server and written here, by an in-process DuckDB.

        Parameters
        ----------
        expr
            The ibis expression to execute and persist to JSON.
        path
            The data source. A string or Path to the JSON file.
        compression
            Compression codec to use. One of "auto", "none", "gzip", "zstd".
        dateformat
            Date format string.
        timestampformat
            Timestamp format string.
        """
        import duckdb

        options = ["FORMAT JSON", "ARRAY TRUE", f"COMPRESSION {compression.upper()}"]
        if dateformat:
            options.append(f"DATEFORMAT {sge.convert(value=dateformat).sql(dialect='duckdb')}")
        if timestampformat:
            options.append(
                f"TIMESTAMPFORMAT {sge.convert(value=timestampformat).sql(dialect='duckdb')}"
            )
        target = sge.convert(value=str(path)).sql(dialect="duckdb")
        local = duckdb.connect()
        try:
            with self.to_pyarrow_batches(expr) as reader:
                local.register("result", reader)
                local.execute(f"COPY (SELECT * FROM result) TO {target} ({', '.join(options)})")
        finally:
            local.close()

    # ---- files on the client ------------------------------------------------------------------

    def _read_local(self, *, table_name: str, source: sge.Select, batch_size: int) -> ir.Table:
        """Read files on this machine and bulk-load them into a temporary table.

        The files are parsed here, by an in-process DuckDB (the server needs no access to
        them), and streamed to the server as Arrow record batches of `batch_size` rows.
        """
        import duckdb

        local = duckdb.connect()
        try:
            reader = local.sql(source.sql(dialect="duckdb")).to_arrow_reader(batch_size=batch_size)
            self._ingest(table_name=table_name, data=reader, mode="replace", temporary=True)
        finally:
            local.close()
        return self.table(table_name)

    def read_csv(
        self,
        paths: str | list[str] | tuple[str],
        /,
        *,
        table_name: str | None = None,
        columns: Mapping[str, str] | None = None,
        types: Mapping[str, str] | None = None,
        batch_size: int = INGEST_BATCH_ROWS,
        **kwargs: Any,
    ) -> ir.Table:
        """Load CSV files on this machine into a temporary table.

        The files are read here, by an in-process DuckDB, and bulk-loaded into the server.

        Parameters
        ----------
        paths
            The data source(s). May be a path to a file or directory of CSV
            files, or an iterable of CSV files.
        table_name
            An optional name to use for the created table. This defaults to a
            sequentially generated name.
        columns
            An optional mapping of **all** column names to their DuckDB types.
        types
            An optional mapping of a **subset** of column names to their DuckDB types.
        batch_size
            Rows per Arrow record batch sent to the server.
        **kwargs
            Additional keyword arguments passed to DuckDB's ``read_csv``. See
            https://duckdb.org/docs/data/csv for more information.

        Returns
        -------
        ir.Table
            The just-loaded table
        """
        paths = util.normalize_filenames(paths)

        if not table_name:
            table_name = util.gen_name("read_csv")

        # auto_detect and columns collide, so we set auto_detect=True
        # unless COLUMNS has been specified
        kwargs.setdefault("header", True)
        kwargs["auto_detect"] = kwargs.pop("auto_detect", columns is None)
        options = [C[key].eq(sge.convert(value=val)) for key, val in kwargs.items()]

        def make_struct_argument(obj: Mapping[str, str]) -> sge.Struct:
            expressions = []
            for name, typ in obj.items():
                sgtype = sg.parse_one(str(typ), read="duckdb", into=sge.DataType)
                expressions.append(
                    sge.PropertyEQ(
                        this=sge.to_identifier(name),
                        expression=sge.convert(value=sgtype.sql(dialect="duckdb")),
                    )
                )
            return sge.Struct(expressions=expressions)

        if columns is not None:
            options.append(C.columns.eq(make_struct_argument(obj=columns)))
        if types is not None:
            options.append(C.types.eq(make_struct_argument(obj=types)))

        source = sg.select(STAR).from_(self.compiler.f.read_csv(paths, *options))
        return self._read_local(table_name=table_name, source=source, batch_size=batch_size)

    def read_parquet(
        self,
        paths: str | Path | Iterable[str | Path],
        /,
        *,
        table_name: str | None = None,
        batch_size: int = INGEST_BATCH_ROWS,
        **kwargs: Any,
    ) -> ir.Table:
        """Load Parquet files on this machine into a temporary table.

        The files are read here, by an in-process DuckDB, and bulk-loaded into the server.

        Parameters
        ----------
        paths
            The data source(s). May be a path to a file, an iterable of files,
            or directory of parquet files.
        table_name
            An optional name to use for the created table. This defaults to
            a sequentially generated name.
        batch_size
            Rows per Arrow record batch sent to the server.
        **kwargs
            Additional keyword arguments passed to DuckDB's ``read_parquet``.
            See https://duckdb.org/docs/data/parquet for more information.

        Returns
        -------
        ir.Table
            The just-loaded table
        """
        paths = util.normalize_filenames(paths)
        table_name = table_name or util.gen_name("read_parquet")
        options = [sg.to_identifier(key).eq(sge.convert(value=val)) for key, val in kwargs.items()]
        source = sg.select(STAR).from_(self.compiler.f.read_parquet(paths, *options))
        return self._read_local(table_name=table_name, source=source, batch_size=batch_size)

    @util.experimental
    def read_json(
        self,
        paths: str | list[str] | tuple[str],
        /,
        *,
        table_name: str | None = None,
        columns: Mapping[str, str] | None = None,
        batch_size: int = INGEST_BATCH_ROWS,
        **kwargs: Any,
    ) -> ir.Table:
        """Load newline-delimited JSON files on this machine into a temporary table.

        The files are read here, by an in-process DuckDB, and bulk-loaded into the server.

        Parameters
        ----------
        paths
            File or list of files
        table_name
            Optional table name
        columns
            Optional mapping from string column name to DuckDB type string.
        batch_size
            Rows per Arrow record batch sent to the server.
        **kwargs
            Additional keyword arguments passed to DuckDB's ``read_json_auto``. See
            https://duckdb.org/docs/data/json/overview.html#json-loading

        Returns
        -------
        Table
            An ibis table expression
        """
        if not table_name:
            table_name = util.gen_name("read_json")

        options = [sg.to_identifier(key).eq(sge.convert(value=val)) for key, val in kwargs.items()]
        if columns:
            options.append(
                sg.to_identifier("columns").eq(
                    sge.Struct.from_arg_list(
                        [
                            sge.PropertyEQ(
                                this=sg.to_identifier(key),
                                expression=sge.convert(value=value),
                            )
                            for key, value in columns.items()
                        ]
                    )
                )
            )

        source = sg.select(STAR).from_(
            self.compiler.f.read_json_auto(util.normalize_filenames(paths), *options)
        )
        return self._read_local(table_name=table_name, source=source, batch_size=batch_size)

    def read_delta(
        self,
        path: str | Path,
        /,
        *,
        table_name: str | None = None,
        **kwargs: Any,
    ) -> ir.Table:
        """Load a Delta Lake table on this machine into a temporary table.

        The table is read here, with the ``deltalake`` package
        (``pip install 'ibis-hardcharger[delta]'``), and bulk-loaded into the server.

        Parameters
        ----------
        path
            The data source. Must be a directory containing a Delta Lake table.
        table_name
            An optional name to use for the created table. This defaults to
            a sequentially generated name.
        kwargs
            Additional keyword arguments passed to ``deltalake.DeltaTable``.

        Returns
        -------
        ir.Table
            The just-loaded table.
        """
        try:
            from deltalake import DeltaTable
        except ImportError:
            raise ImportError(
                "The deltalake extra is required to use the read_delta method. "
                "You can install it using pip:\n\npip install 'ibis-hardcharger[delta]'\n"
            ) from None

        (path,) = util.normalize_filenames(path)
        table_name = table_name or util.gen_name("read_delta")
        reader = DeltaTable(path, **kwargs).to_pyarrow_dataset().scanner().to_reader()
        self._ingest(table_name=table_name, data=reader, mode="replace", temporary=True)
        return self.table(table_name)

    # ---- temporary views ----------------------------------------------------------------------

    def _get_temp_view_definition(self, name: str, definition: str) -> sge.Create:
        return sge.Create(
            this=sg.to_identifier(name, quoted=self.compiler.quoted),
            kind="VIEW",
            expression=definition,
            replace=True,
            properties=sge.Properties(expressions=[sge.TemporaryProperty()]),
        )

    def _create_temp_view(self, table_name: str, source: Any) -> None:
        self._run(self._get_temp_view_definition(name=table_name, definition=source))
