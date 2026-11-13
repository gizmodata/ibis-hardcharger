"""Connecting: keyword arguments, URLs, password and token sign-in, session settings.

Every refusal asserts why it was refused, next to a twin that succeeds.
"""

from __future__ import annotations

import adbc_driver_manager
import ibis
import pytest
from adbc_driver_hardcharger import dbapi

import ibis_hardcharger
from tests.support.server import SERVER_TIME_ZONE


def current_user(con) -> str:
    return con.con.current_user()


def auth_method(con) -> str:
    with con._safe_raw_sql(
        "SELECT auth_method FROM hardcharger_sessions() WHERE is_current"
    ) as cur:
        return cur.fetchone()[0]


def test_connect_with_keyword_arguments(connect, hardcharger_server):
    con = connect()
    try:
        assert isinstance(con, ibis_hardcharger.Backend)
        assert current_user(con=con) == hardcharger_server["username"]
        assert auth_method(con=con) == "password"
        assert con.current_catalog == "memory"
        assert con.current_database == "main"
    finally:
        con.disconnect()


def test_version_is_the_servers(connect):
    con = connect()
    try:
        with con._safe_raw_sql("SELECT HARDCHARGER_VERSION()") as cur:
            [(version,)] = cur.fetchall()
        assert con.version == version
        major = int(version.split(".")[0])
        assert major >= 2
    finally:
        con.disconnect()


def test_connect_with_a_url(hardcharger_server):
    s = hardcharger_server
    con = ibis.connect(
        f"hardcharger://{s['username']}:{s['password']}@{s['host']}:{s['port']}?tls=false"
    )
    try:
        assert isinstance(con, ibis_hardcharger.Backend)
        assert current_user(con=con) == s["username"]
        assert con.execute(ibis.literal(1) + 1) == 2
    finally:
        con.disconnect()


def test_url_names_the_catalog_and_schema(connect, hardcharger_server):
    s = hardcharger_server
    admin = connect()
    admin.create_database("url_schema", force=True)
    try:
        con = ibis.connect(
            f"hardcharger://{s['username']}:{s['password']}@{s['host']}:{s['port']}"
            "/memory/url_schema?tls=false"
        )
        try:
            assert con.current_catalog == "memory"
            assert con.current_database == "url_schema"
        finally:
            con.disconnect()
    finally:
        admin.drop_database("url_schema", force=True)
        admin.disconnect()


def test_url_with_ibis_gizmosql_parameter_names(hardcharger_server):
    s = hardcharger_server
    con = ibis.connect(
        f"hardcharger://{s['username']}:{s['password']}@{s['host']}:{s['port']}"
        "?useEncryption=false&authType=password"
    )
    try:
        assert current_user(con=con) == s["username"]
    finally:
        con.disconnect()


def test_uri_argument(hardcharger_server):
    s = hardcharger_server
    con = ibis.hardcharger.connect(
        uri=f"hardcharger://{s['host']}:{s['port']}?tls=false",
        user=s["username"],
        password=s["password"],
    )
    try:
        assert current_user(con=con) == s["username"]
    finally:
        con.disconnect()


def test_set_backend_with_a_url(hardcharger_server, monkeypatch):
    s = hardcharger_server
    monkeypatch.setattr(ibis.options, "default_backend", None)
    ibis.set_backend(
        f"hardcharger://{s['username']}:{s['password']}@{s['host']}:{s['port']}?tls=false"
    )
    con = ibis.get_backend()
    try:
        assert con.name == "hardcharger"
        assert ibis.literal("x").upper().execute() == "X"
    finally:
        con.disconnect()


def test_database_and_schema_arguments(connect):
    admin = connect()
    admin.create_database("start_here", force=True)
    try:
        con = connect(database="memory", schema="start_here")
        try:
            assert con.current_catalog == "memory"
            assert con.current_database == "start_here"
        finally:
            con.disconnect()
    finally:
        admin.drop_database("start_here", force=True)
        admin.disconnect()


def test_catalog_and_schema_in_another_catalog(connect):
    """Both together: the session starts in that catalog's schema, not in its `main`."""
    admin = connect()
    admin.attach(":memory:", name="start_catalog")
    try:
        admin.raw_sql('CREATE SCHEMA "start_catalog"."inner"').close()
        # Many connects, not one: a driver before adbc-driver-hardcharger 2.5.0 applied the two
        # options in a random order, so one connect in five or so found the schema by luck.
        for _ in range(40):
            con = connect(database="start_catalog", schema="inner")
            try:
                assert con.current_catalog == "start_catalog"
                assert con.current_database == "inner"
            finally:
                con.disconnect()
        only_catalog = connect(database="start_catalog")
        try:
            assert only_catalog.current_catalog == "start_catalog"
            assert only_catalog.current_database == "main"
        finally:
            only_catalog.disconnect()
    finally:
        admin.detach("start_catalog")
        admin.disconnect()


def test_a_schema_that_does_not_exist_is_refused(connect):
    with pytest.raises(adbc_driver_manager.Error, match='"memory.no_such_schema" found'):
        connect(schema="no_such_schema")
    with pytest.raises(adbc_driver_manager.Error, match="no_such_schema"):
        connect(database="memory", schema="no_such_schema")


def test_wrong_password_is_refused(connect):
    with pytest.raises(adbc_driver_manager.Error, match="invalid_credentials"):
        connect(password="not-the-password")


def test_access_token(connect, hardcharger_server):
    signed_in = connect()
    try:
        token = signed_in.con.access_token
        con = connect(user=None, password=None, access_token=token)
        try:
            assert current_user(con=con) == hardcharger_server["username"]
            assert con.execute(ibis.literal(20) + 22) == 42
        finally:
            con.disconnect()
    finally:
        signed_in.disconnect()


def test_forged_access_token_is_refused(connect):
    with pytest.raises(adbc_driver_manager.Error, match="invalid_token"):
        connect(user=None, password=None, access_token="not-a-token")


def test_browser_sign_in_takes_no_password(connect):
    """The driver's own refusal, before anything is sent."""
    with pytest.raises(ValueError, match="auth_type='external' signs in through the browser"):
        connect(auth_type="external")


def test_tls_to_a_plain_http_server_is_refused(hardcharger_server):
    """TLS is on unless turned off: a plain-HTTP server can't be reached by accident."""
    s = hardcharger_server
    with pytest.raises(adbc_driver_manager.Error, match="speaks plain HTTP, not TLS"):
        ibis.hardcharger.connect(
            host=s["host"], port=s["port"], user=s["username"], password=s["password"]
        )


def test_session_time_zone_is_utc(connect, hardcharger_server):
    """Ibis works in UTC: the backend puts its session there, whatever the server's zone."""
    s = hardcharger_server
    with (
        dbapi.connect(
            uri=f"hardcharger://{s['host']}:{s['port']}?tls=false",
            username=s["username"],
            password=s["password"],
        ) as raw,
        raw.cursor() as cur,
    ):
        cur.execute(operation="SELECT current_setting('TimeZone')")
        assert cur.fetchone() == (SERVER_TIME_ZONE,)  # a session as the server starts it

    con = connect()
    try:
        assert con.settings["TimeZone"] == "UTC"
        noon = ibis.timestamp("2024-07-01 12:00:00+00:00").cast("timestamp('UTC')")
        assert con.execute(noon.hour()) == 12
        (value,) = con.to_pyarrow(noon.name("t").as_table())["t"].to_pylist()
        assert value.hour == 12
        assert str(value.tzinfo) == "UTC"
    finally:
        con.disconnect()


def test_settings(con):
    con.settings["threads"] = 2
    assert con.settings["threads"] == "2"
    assert "threads" in repr(con.settings)
    with pytest.raises(KeyError, match="no_such_setting"):
        con.settings["no_such_setting"]
    with pytest.raises(ValueError, match="invalid setting name"):
        con.settings["threads = 1; DROP TABLE t; --"] = 1


def test_driver_arguments_pass_through(connect):
    con = connect(settings={"query_timeout": 600}, session_tag={"app": "ibis-hardcharger-tests"})
    try:
        assert con.con.get_setting(name="query_timeout") == "600"
    finally:
        con.disconnect()


def test_an_unknown_argument_is_refused(connect):
    with pytest.raises(TypeError, match="unexpected keyword argument 'no_such_option'"):
        connect(no_such_option=1)


def test_from_connection(hardcharger_server):
    s = hardcharger_server
    raw = dbapi.connect(
        uri=f"hardcharger://{s['host']}:{s['port']}?tls=false",
        username=s["username"],
        password=s["password"],
    )
    con = ibis.hardcharger.from_connection(raw)
    try:
        assert con.con is raw
        assert con.settings["TimeZone"] == "UTC"
        assert con.execute(ibis.literal(1) + 1) == 2
        with pytest.raises(ibis.common.exceptions.IbisError, match="Cannot reconnect"):
            con.reconnect()
    finally:
        con.disconnect()


def test_disconnect_and_reconnect(connect):
    con = connect()
    assert con.execute(ibis.literal(1)) == 1
    con.disconnect()
    with pytest.raises(adbc_driver_manager.Error, match="INVALID_STATE"):
        con.execute(ibis.literal(1))
    con.reconnect()
    try:
        assert con.execute(ibis.literal(1)) == 1
        assert con.settings["TimeZone"] == "UTC"
    finally:
        con.disconnect()


def test_extensions_are_loaded_on_connect(connect):
    con = connect(extensions=["inet"])
    try:
        with con._safe_raw_sql(
            "SELECT loaded FROM duckdb_extensions() WHERE extension_name = 'inet'"
        ) as cur:
            assert cur.fetchall() == [(True,)]
        assert con.sql("SELECT host('10.1.2.3/24'::INET) AS h").execute()["h"][0] == "10.1.2.3"
        con.load_extension("inet")  # already loaded: nothing to do
    finally:
        con.disconnect()
