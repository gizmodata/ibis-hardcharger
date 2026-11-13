"""Run Ibis's own backend test suite (``ibis.backends.tests``) against Hardcharger.

Each ``test_*.py`` here star-imports the Ibis module of the same name, as Ibis wrote it; this
file supplies what Ibis's ``ibis/backends/conftest.py`` supplies for its in-tree backends,
with Hardcharger as the one backend:

- ``TestConf``: how to connect and load the suite's data (the Ibis testing data, see
  scripts/fetch_testing_data.sh, and ci/schema/hardcharger.sql);
- the fixtures of ``ibis/backends/conftest.py``;
- Ibis's ``notimpl`` / ``notyet`` / ``never`` markers. Hardcharger runs DuckDB SQL from Ibis's
  DuckDB compiler, so a test Ibis marks as failing on ``duckdb`` is expected to fail here the
  same way, with the server's error (the ADBC driver's exception) where Ibis names DuckDB's
  in-process exception;
- the expected failures that are Hardcharger's own (``HARDCHARGER_XFAILS``), each with its
  reason and exception. They are strict: one that starts passing fails the run.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any

import adbc_driver_manager
import ibis
import ibis.backends.sql.compilers as sc
import ibis.common.exceptions as exc
import pytest
import sqlglot as sg
from ibis import util
from ibis.backends.conftest import TEST_TABLES
from ibis.backends.conftest import pytest_runtest_call as _ibis_pytest_runtest_call
from ibis.backends.tests.base import BackendTest

import ibis_hardcharger

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ibis.backends import BaseBackend

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(
    os.environ.get("IBIS_HARDCHARGER_TEST_DATA_DIR") or PROJECT_ROOT / "ci" / "ibis-testing-data"
)

BACKEND = "hardcharger"

# TestConf.connect() is a static method with a fixed signature, so it can't take the server
# fixture; the `backend` fixture fills this in before any connection is opened.
_SERVER: dict[str, Any] = {}

# ``ibis.to_sql(expr, dialect="hardcharger")`` and the suite's per-backend lookups go through
# the compiler registry by backend name.
if not hasattr(sc, BACKEND):
    sc.hardcharger = types.SimpleNamespace(compiler=ibis_hardcharger.Backend.compiler)
# The suite imports a backend as ibis.backends.<name> (test_has_operation_no_builtins), and
# its TestConf from ibis.backends.<name>.tests.conftest (test_stateful_data_is_loaded_once).
sys.modules.setdefault(f"ibis.backends.{BACKEND}", ibis_hardcharger)
sys.modules.setdefault(f"ibis.backends.{BACKEND}.tests", types.ModuleType("tests"))
sys.modules.setdefault(f"ibis.backends.{BACKEND}.tests.conftest", sys.modules[__name__])


class TestConf(BackendTest):
    # As Ibis's DuckDB TestConf: Hardcharger is DuckDB to the suite.
    supports_map = True
    supports_tpch = True
    supports_tpcds = True
    driver_supports_multiple_statements = True
    # One server holds the data for every pytest-xdist worker: load it once.
    stateful = True
    # Nothing here may be skipped for a missing package: without the driver the import of
    # ibis_hardcharger fails, and so does the run.
    deps = ()

    @classmethod
    def name(cls) -> str:
        return BACKEND

    @staticmethod
    def connect(*, tmpdir, worker_id, **kw) -> BaseBackend:
        return ibis.hardcharger.connect(
            host=_SERVER["host"],
            port=_SERVER["port"],
            user=_SERVER["username"],
            password=_SERVER["password"],
            use_encryption=False,
            **kw,
        )

    def _load_data(self, **_: Any) -> None:
        """The suite's tables: the parquet files, bulk-loaded from this machine, then the
        special tables of ci/schema/hardcharger.sql in one multi-statement request."""
        con = self.connection
        parquet_dir = self.data_dir / "parquet"
        for table in TEST_TABLES:
            con.create_table(
                table, con.read_parquet(parquet_dir / f"{table}.parquet"), overwrite=True
            )
        super()._load_data()

    def _load_tpc(self, *, suite, scale_factor) -> None:
        con = self.connection
        schema = f"tpc{suite}"
        con.create_database(schema, force=True)
        parquet_dir = self.data_dir.joinpath(schema, f"sf={scale_factor}", "parquet")
        assert parquet_dir.exists(), parquet_dir
        for path in sorted(parquet_dir.glob("*.parquet")):
            con.create_table(
                path.with_suffix("").name,
                # DuckDB infers the sf= directory as a hive partition: disable it.
                con.read_parquet(path, hive_partitioning=False),
                database=schema,
                overwrite=True,
            )

    def _transform_tpc_sql(self, parsed, *, suite, leaves):
        def add_catalog_and_schema(node):
            if isinstance(node, sg.exp.Table) and node.name in leaves:
                return node.__class__(
                    catalog=f"tpc{suite}",
                    **{k: v for k, v in node.args.items() if k != "catalog"},
                )
            return node

        return parsed.transform(add_catalog_and_schema)


# ---- the fixtures of ibis/backends/conftest.py ------------------------------------------------


@pytest.fixture(scope="session")
def data_dir() -> Path:
    if not (DATA_DIR / "parquet" / "functional_alltypes.parquet").is_file():
        pytest.fail(
            f"the Ibis testing data is not in {DATA_DIR}: run scripts/fetch_testing_data.sh",
            pytrace=False,
        )
    return DATA_DIR


# Parametrized with the one backend, as Ibis parametrizes it with all of its own: test ids and
# snapshot paths then carry the backend's name where Ibis's do.
@pytest.fixture(params=[pytest.param(BACKEND, id=BACKEND)], scope="session")
def backend(hardcharger_server, data_dir, tmp_path_factory, worker_id) -> Iterator[TestConf]:
    """The TestConf instance, with the suite's data loaded."""
    _SERVER.update(hardcharger_server)
    with TestConf.load_data(data_dir, tmp_path_factory, worker_id) as inst:
        yield inst


@pytest.fixture(scope="session")
def con(backend):
    return backend.connection


# Ibis's hook refuses a test that gets two different backend fixtures; here they are the same
# object, since there is one backend and one server.
@pytest.fixture(scope="session")
def ddl_backend(backend):
    return backend


@pytest.fixture(scope="session")
def ddl_con(ddl_backend):
    return ddl_backend.connection


@pytest.fixture(scope="session")
def backend_no_data(backend):
    return backend


@pytest.fixture(scope="session")
def con_no_data(con):
    return con


@pytest.fixture(scope="session")
def backend_name() -> str:
    return BACKEND


@pytest.fixture(scope="session")
def backend_cls():
    return ibis_hardcharger.Backend


@pytest.fixture(scope="session")
def con_create_catalog(con):
    from ibis.backends import CanCreateCatalog

    if isinstance(con, CanCreateCatalog):
        return con
    pytest.skip(f"{con.name} backend cannot create databases")


@pytest.fixture(scope="session")
def con_create_database(con):
    from ibis.backends import CanCreateDatabase

    if isinstance(con, CanCreateDatabase):
        return con
    pytest.skip(f"{con.name} backend cannot create schemas")


@pytest.fixture(scope="session")
def con_create_catalog_database(con):
    from ibis.backends import CanCreateCatalog, CanCreateDatabase

    if isinstance(con, CanCreateCatalog) and isinstance(con, CanCreateDatabase):
        return con
    pytest.skip(f"{con.name} backend cannot create both database and schemas")


@pytest.fixture(scope="session")
def alltypes(backend):
    return backend.functional_alltypes


@pytest.fixture(scope="session")
def json_t(backend):
    return backend.json_t


@pytest.fixture(scope="session")
def struct(backend):
    return backend.struct


@pytest.fixture(scope="session")
def batting(backend):
    return backend.batting


@pytest.fixture(scope="session")
def awards_players(backend):
    return backend.awards_players


@pytest.fixture(scope="session")
def df(alltypes):
    return alltypes.execute()


@pytest.fixture(scope="session")
def struct_df(struct):
    return struct.execute()


@pytest.fixture(scope="session")
def sorted_df(df):
    return df.sort_values("id").reset_index(drop=True)


@pytest.fixture(scope="session")
def batting_df(batting):
    return batting.execute(limit=None)


@pytest.fixture(scope="session")
def awards_players_df(awards_players):
    return awards_players.execute(limit=None)


@pytest.fixture
def temp_table(con):
    name = util.gen_name("temp_table")
    yield name
    with contextlib.suppress(NotImplementedError):
        con.drop_table(name, force=True)


@pytest.fixture
def temp_table2(con):
    name = util.gen_name("temp_table2")
    yield name
    with contextlib.suppress(NotImplementedError):
        con.drop_table(name, force=True)


@pytest.fixture
def temp_table_orig(con, temp_table):
    name = f"{temp_table}_orig"
    yield name
    with contextlib.suppress(NotImplementedError):
        con.drop_table(name, force=True)


@pytest.fixture
def temp_view(ddl_con):
    name = util.gen_name("view")
    yield name
    with contextlib.suppress(NotImplementedError):
        ddl_con.drop_view(name, force=True)


@pytest.fixture
def assert_sql(con, snapshot):
    def checker(expr, file_name="out.sql"):
        sql = con.compile(expr, pretty=True)
        snapshot.assert_match(sql, file_name)

    return checker


# ---- Ibis's per-backend lookup tables: Hardcharger's entry is DuckDB's ------------------------


def _mirror_duckdb(mapping: Any) -> None:
    if isinstance(mapping, dict) and "duckdb" in mapping and BACKEND not in mapping:
        mapping[BACKEND] = mapping["duckdb"]


def _mirror_module_tables() -> None:
    """In each Ibis test module this suite runs (one per test_*.py here)."""
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        module = importlib.import_module(f"ibis.backends.tests.{path.stem}")
        for value in vars(module).values():
            _mirror_duckdb(mapping=value)


_mirror_module_tables()


# ---- markers ------------------------------------------------------------------------------

# Every other backend Ibis knows: tests parametrized for one of them are not ours to run.
_OTHER_BACKENDS = frozenset(ibis.backends._get_backend_names()) - {BACKEND}

_MARKERS = ("notimpl", "notyet", "never")


def _is_duckdb_exception(cls: Any) -> bool:
    return getattr(cls, "__module__", "").split(".")[0] in ("duckdb", "_duckdb")


def _for_hardcharger(marker: pytest.Mark) -> pytest.MarkDecorator:
    """Ibis's marker for ``duckdb``, as one for ``hardcharger``.

    Where it expects one of DuckDB's in-process exceptions, the same failure reaches the
    client here as the ADBC driver's error.
    """
    kwargs = dict(marker.kwargs)
    raises = kwargs.get("raises")
    if raises is not None:
        classes = raises if isinstance(raises, tuple) else (raises,)
        kwargs["raises"] = tuple(
            adbc_driver_manager.Error if cls is not None and _is_duckdb_exception(cls) else cls
            for cls in classes
        )
    return getattr(pytest.mark, marker.name)([BACKEND], **kwargs)


# Tests Ibis expects to fail on DuckDB that pass here.
NOT_INHERITED = {
    # to_parquet() and to_csv() write on the client with PyArrow, which takes writer options;
    # Ibis's DuckDB backend writes with COPY, which can't.
    "test_table_to_parquet_writer_kwargs",
    "test_table_to_csv_writer_kwargs",
    # create_table() streams a PyArrow RecordBatchReader into the table (the [pyarrow_rbr]
    # case; Ibis's DuckDB backend refuses a reader).
    "test_create_table_in_memory",
    # An INTERVAL result of fixed length (no months) comes back as a duration / pandas
    # Timedelta, as its Ibis type says; Ibis's DuckDB backend returns DateOffsets.
    "test_interval_literal",
    "test_temporal_binop",
}

_NO_PYTHON_UDFS = (
    exc.UnsupportedOperationError,
    "Python, pandas and PyArrow UDFs run in the client's process; Hardcharger runs queries "
    "on the server (the backend refuses them with this error)",
)

# Expected failures that are Hardcharger's own: test name (with its parameter id where only
# one case fails) -> (exception, reason).
HARDCHARGER_XFAILS: dict[str, tuple[type[BaseException] | tuple[type[BaseException], ...], str]] = {
    "test_udf": _NO_PYTHON_UDFS,
    "test_map_udf": _NO_PYTHON_UDFS,
    "test_map_merge_udf": _NO_PYTHON_UDFS,
    "test_vectorized_udf": _NO_PYTHON_UDFS,
    "test_impure_correlated[hardcharger-udf]": _NO_PYTHON_UDFS,
    "test_chained_selections[hardcharger-udf]": _NO_PYTHON_UDFS,
    "test_impure_uncorrelated_different_id[hardcharger-udf]": _NO_PYTHON_UDFS,
    "test_impure_uncorrelated_same_id[hardcharger-udf]": _NO_PYTHON_UDFS,
    "test_empty_array_string_join": (
        AssertionError,
        "DuckDB 2.0's ARRAY_TO_STRING gives '' for an empty list, and Ibis's test expects "
        "NULL: Ibis's own DuckDB backend returns the same on duckdb 2.0.0",
    ),
    "test_roundtrip_partitioned_parquet": (
        TypeError,
        "to_parquet() writes on the client with PyArrow's ParquetWriter, which has no "
        "partition_by (Ibis's DuckDB backend writes with COPY); to_parquet_dir() partitions",
    ),
}


def _in_suite(*, item) -> bool:
    return Path(__file__).parent in Path(item.path).parents


def pytest_collection_modifyitems(config, items) -> None:
    selected = []
    deselected = []
    for item in items:
        if not _in_suite(item=item):
            selected.append(item)
            continue

        # A test or case Ibis marks for another backend (test_connect_url[postgres], ...)
        # is not ours to run. Ibis selects its own runs the same way, with `-m <backend>`.
        if _OTHER_BACKENDS.intersection(marker.name for marker in item.iter_markers()):
            deselected.append(item)
            continue
        callspec = getattr(item, "callspec", None)
        if callspec is not None:
            # A case parametrized by another backend's name, without its marker.
            if callspec.id.split("-")[0] in _OTHER_BACKENDS or any(
                isinstance(value, str) and value in _OTHER_BACKENDS
                for value in callspec.params.values()
            ):
                deselected.append(item)
                continue
            # Ibis parametrizes some tests with a dict keyed by backend name.
            for value in callspec.params.values():
                _mirror_duckdb(mapping=value)

        item.add_marker(pytest.mark.hardcharger)
        for name in _MARKERS:
            for marker in list(item.iter_markers(name=name)):
                backends = util.promote_list(marker.args[0])
                if (
                    "duckdb" in backends
                    and BACKEND not in backends
                    and item.originalname not in NOT_INHERITED
                ):
                    item.add_marker(_for_hardcharger(marker=marker))

        expected = HARDCHARGER_XFAILS.get(item.name) or HARDCHARGER_XFAILS.get(item.originalname)
        if expected is not None:
            raises, reason = expected
            item.add_marker(pytest.mark.xfail(raises=raises, reason=reason, strict=True))
        selected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


def pytest_runtest_call(item) -> None:
    """Ibis's hook, which turns notimpl/notyet/never markers for the backend under test into
    xfails. It refuses a test that received more than one backend fixture; ours are all the
    same object, so it gets to see one of them."""
    if not _in_suite(item=item):
        return
    keys = [
        key for key in item.funcargs if key.endswith(("backend", "backend_name", "backend_no_data"))
    ]
    stashed = {key: item.funcargs.pop(key) for key in keys[1:]}
    try:
        _ibis_pytest_runtest_call(item)
    finally:
        item.funcargs.update(stashed)


__all__ = ["TestConf", "exc"]
