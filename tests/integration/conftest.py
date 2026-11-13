"""Fixtures of the integration tests: connections to the session's Hardcharger server."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

import ibis
import pytest
from ibis import util

import ibis_hardcharger


@pytest.fixture(scope="session")
def connect(hardcharger_server) -> Callable[..., ibis_hardcharger.Backend]:
    """``connect(**kwargs)``: a new connection to the session's server (plain HTTP)."""

    def _connect(**kwargs: Any) -> ibis_hardcharger.Backend:
        kwargs.setdefault("user", hardcharger_server["username"])
        kwargs.setdefault("password", hardcharger_server["password"])
        return ibis.hardcharger.connect(
            host=hardcharger_server["host"],
            port=hardcharger_server["port"],
            use_encryption=False,
            **kwargs,
        )

    return _connect


@pytest.fixture
def con(connect) -> Iterator[ibis_hardcharger.Backend]:
    """A connection whose current schema is this test's own, dropped afterwards.

    The server is shared by every test and pytest-xdist worker, so each test works in a
    schema of its own.
    """
    schema = util.gen_name("it")
    admin = connect()
    admin.create_database(schema)
    backend = connect(schema=schema)
    try:
        yield backend
    finally:
        with contextlib.suppress(Exception):
            backend.disconnect()
        admin.raw_sql(f'DROP SCHEMA "{schema}" CASCADE').close()
        admin.disconnect()
