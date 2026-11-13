"""A session the server ends: the backend reports it, and reconnect() starts afresh.

Ibis keeps state in the server session (the UTC time zone, the temporary tables behind
memtables, read_*() and cache()), so the backend turns off the driver's replacement of a lost
session. These tests run against a server that evicts a session idle for one second.
"""

from __future__ import annotations

import time

import adbc_driver_manager
import ibis
import pytest
from adbc_driver_hardcharger import dbapi

from tests.support.server import SERVER_PASSWORD, SERVER_TIME_ZONE, SERVER_USERNAME, start_server


@pytest.fixture(scope="module")
def evicting_server():
    server = start_server(extra_args=["--session-idle-timeout", "1"])
    yield server
    server.stop()


@pytest.fixture
def watcher(evicting_server):
    """wait_until_evicted(session_id): block until the server no longer lists the session.

    It asks through a connection of its own, on the server's view of its sessions, because a
    request on the watched connection would keep its session alive.
    """
    raw = dbapi.connect(uri=evicting_server.uri, username=SERVER_USERNAME, password=SERVER_PASSWORD)

    def wait_until_evicted(session_id: str) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            with raw.cursor() as cur:
                cur.execute(
                    operation="SELECT count(*) FROM hardcharger_sessions() "
                    "WHERE CAST(session_id AS VARCHAR) = ?",
                    parameters=[session_id],
                )
                (count,) = cur.fetchone()
            if count == 0:
                return
            time.sleep(0.25)
        raise AssertionError(f"session {session_id} was not evicted within 60 s")

    yield wait_until_evicted
    raw.close()


def connect(server, **kwargs):
    return ibis.hardcharger.connect(
        host=server.host,
        port=server.port,
        user=SERVER_USERNAME,
        password=SERVER_PASSWORD,
        use_encryption=False,
        **kwargs,
    )


def test_a_lost_session_is_an_error_until_reconnect(evicting_server, watcher):
    con = connect(evicting_server)
    try:
        memtable = ibis.memtable({"a": [1, 2, 3]})
        assert con.execute(memtable.a.sum()) == 6
        watcher(con.con.session_id)

        # Every request fails, and says what to do: nothing runs in a session without the
        # time zone and temporary tables Ibis set up.
        for _ in range(2):
            with pytest.raises(adbc_driver_manager.OperationalError) as excinfo:
                con.execute(ibis.literal(1))
            assert "session_not_found" in str(excinfo.value)
            assert "the connection must be reopened" in str(excinfo.value)

        con.reconnect()
        assert con.settings["TimeZone"] == "UTC"
        # The in-memory table is loaded again into the new session.
        assert con.execute(memtable.a.sum()) == 6
    finally:
        con.disconnect()


def test_why_the_drivers_session_replacement_is_off(evicting_server, watcher):
    """With the driver's default, the statement after the failed one runs in a new session
    that has neither the UTC time zone nor the temporary tables: wrong, and silently."""
    con = connect(evicting_server, db_kwargs={"adbc.chargewire.session.reconnect": "true"})
    try:
        con.raw_sql("CREATE TEMP TABLE kept AS SELECT 1 AS a").close()
        assert con.settings["TimeZone"] == "UTC"
        watcher(con.con.session_id)

        with pytest.raises(adbc_driver_manager.OperationalError, match="session_not_found"):
            con.raw_sql("SELECT a FROM kept")
        assert con.settings["TimeZone"] == SERVER_TIME_ZONE
        with pytest.raises(adbc_driver_manager.ProgrammingError, match="kept does not exist"):
            con.raw_sql("SELECT a FROM kept")
    finally:
        con.disconnect()
