"""What installing the package registers, and what it compiles, without a server."""

from __future__ import annotations

import contextlib
import importlib.metadata
import io
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
            db_kwargs={"hardcharger.ipc.compression": "zstd"},
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
            "hardcharger.ipc.compression": "zstd",
            "hardcharger.session.reconnect": "false",
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
            host="db.example.com", db_kwargs={"hardcharger.session.reconnect": "true"}
        )
    assert connect.call_args.kwargs["db_kwargs"] == {"hardcharger.session.reconnect": "true"}


@pytest.mark.parametrize(
    "name",
    [
        "hardcharger.session.reconnect",
        "chargewire.session.reconnect",
        "adbc.chargewire.session.reconnect",
        "adbc.warp.session.reconnect",
    ],
)
@pytest.mark.parametrize("value", ["true", "false"])
def test_the_callers_session_replacement_choice_is_sent_under_one_name_only(mocker, name, value):
    """Under any name the driver reads: the default is never added beside it under another
    name, where the driver would take one of the two values, not necessarily the caller's."""

    class Stop(Exception):
        pass

    connect = mocker.patch.object(
        target=ibis_hardcharger.hardcharger, attribute="connect", side_effect=Stop
    )
    with pytest.raises(Stop):
        ibis.hardcharger.connect(host="db.example.com", db_kwargs={name: value})
    assert connect.call_args.kwargs["db_kwargs"] == {name: value}


def test_the_option_names_are_the_drivers_current_ones():
    """adbc-driver-hardcharger 2.7.0 names: no deprecation line from the driver."""
    from adbc_driver_hardcharger import _legacy

    for name in (
        *ibis_hardcharger._ORDERED_INGEST,
        ibis_hardcharger._SESSION_RECONNECT,
    ):
        assert _legacy.current_key(key=name) == (name, 0)
    # every spelling in the list is one the driver reads as that option
    with contextlib.redirect_stderr(new_target=io.StringIO()):
        for name in ibis_hardcharger._SESSION_RECONNECT_NAMES:
            assert _legacy.current_key(key=name)[0] == "hardcharger.session.reconnect"
    assert ibis_hardcharger._ORDERED_INGEST == {"hardcharger.ingest.parts": "1"}
    assert ibis_hardcharger._SESSION_RECONNECT == "hardcharger.session.reconnect"
