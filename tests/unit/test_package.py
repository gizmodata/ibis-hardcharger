"""What installing the package registers, and what it compiles, without a server."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

import ibis
import ibis.common.exceptions
import pytest
import sqlglot

import ibis_hardcharger

ROOT = Path(__file__).resolve().parent.parent.parent


def test_version_is_the_installed_metadata_and_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert ibis_hardcharger.__version__ == project["version"]
    assert ibis_hardcharger.__version__ == importlib.metadata.version("ibis-hardcharger")


def test_the_backend_entry_point():
    (entry_point,) = (
        ep
        for ep in importlib.metadata.entry_points(group="ibis.backends")
        if ep.name == "hardcharger"
    )
    assert entry_point.value == "ibis_hardcharger"
    assert entry_point.load().Backend is ibis_hardcharger.Backend


def test_ibis_finds_the_backend_by_name():
    assert ibis.hardcharger.name == "hardcharger"


def test_a_server_that_is_not_hardcharger_is_refused(mocker):
    con = mocker.Mock()
    con.adbc_get_info.return_value = {"vendor_name": "SomethingElse"}
    with pytest.raises(ibis.common.exceptions.IbisError, match="needs a Hardcharger server"):
        ibis_hardcharger.Backend.from_connection(con)
    con.cursor.assert_not_called()


def test_the_sqlglot_dialect_is_registered():
    dialect = sqlglot.Dialect.get_or_raise("hardcharger")
    assert isinstance(dialect, ibis_hardcharger.Hardcharger)
    assert sqlglot.transpile(sql="SELECT 1::INT", read="hardcharger", write="duckdb") == [
        "SELECT CAST(1 AS INT)"
    ]


def test_compiles_duckdb_sql():
    t = ibis.table(schema={"a": "int64", "b": "string"}, name="t")
    sql = ibis.hardcharger.compile(t.filter(t.a > 1).select("b"))
    assert 'FROM "t"' in sql
    assert '"a" > 1' in sql


def test_lambdas_use_the_form_duckdb_2_takes():
    """DuckDB 2.0 refuses `x -> x + 1`; Ibis 12's DuckDB compiler alone writes that."""
    t = ibis.table(schema={"xs": "array<int64>"}, name="t")
    expr = t.xs.map(lambda x: x + 1)
    sql = ibis.hardcharger.compile(expr)
    assert "LAMBDA" in sql
    assert "->" not in sql
    # The stock compiler, for contrast: the reason the Hardcharger compiler exists.
    assert "->" in ibis.to_sql(expr, dialect="duckdb")


def test_connecting_without_a_server_address_is_refused():
    with pytest.raises(ValueError, match=r"needs host= \(or uri=, or profile=\)"):
        ibis.hardcharger.connect(user="joe", password="pw")


def test_do_connect_hands_the_driver_its_arguments(mocker):
    """The driver's connect() gets the mapped arguments, with session replacement off."""

    class Stop(Exception):
        pass

    connect = mocker.patch.object(
        target=ibis_hardcharger.hardcharger, attribute="connect", side_effect=Stop
    )
    with pytest.raises(Stop):
        ibis.hardcharger.connect(
            host="db.example.com",
            user="joe",
            password="pw",
            database="lake",
            disable_certificate_verification=True,
            tls_server_name="db.internal",
            db_kwargs={"adbc.chargewire.ipc_compression": "zstd"},
        )
    connect.assert_called_once_with(
        uri="hardcharger://db.example.com",
        auth_type="password",
        username="joe",
        password="pw",
        catalog="lake",
        tls_skip_verify=True,
        tls_server_name="db.internal",
        db_kwargs={
            "adbc.chargewire.ipc_compression": "zstd",
            "adbc.chargewire.session.reconnect": "false",
        },
    )


def test_session_replacement_can_be_turned_back_on(mocker):
    class Stop(Exception):
        pass

    connect = mocker.patch.object(
        target=ibis_hardcharger.hardcharger, attribute="connect", side_effect=Stop
    )
    with pytest.raises(Stop):
        ibis.hardcharger.connect(
            host="db.example.com", db_kwargs={"adbc.chargewire.session.reconnect": "true"}
        )
    assert connect.call_args.kwargs["db_kwargs"] == {"adbc.chargewire.session.reconnect": "true"}
