"""Start ``hardcharger-server`` for the tests: the released binary, as a subprocess on a free
loopback port, from the ``hardcharger`` PyPI package. It raises (never skips) when the server
can't start."""

from __future__ import annotations

import os
import socket
from typing import Any

import hardcharger
import pytest

SERVER_USERNAME = "ibis"
SERVER_PASSWORD = "ibis_password"
#: The time zone the test servers give a new session.
SERVER_TIME_ZONE = "Asia/Kolkata"

# HARDCHARGER_* variables the ``hardcharger`` package's installer reads; all others are
# server options, and are removed from the server's environment.
_INSTALLER_VARIABLES = frozenset(
    {
        "HARDCHARGER_INSTALL_TOKEN",
        "HARDCHARGER_CACHE_DIR",
        "HARDCHARGER_RELEASE_API_URL",
        "HARDCHARGER_RELEASE_REPO",
    }
)


def free_port() -> int:
    with socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def clean_environment() -> dict[str, str]:
    """This process's environment without server options: only the installer's
    ``HARDCHARGER_*`` variables stay."""
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("HARDCHARGER_") or name in _INSTALLER_VARIABLES
    }


def start_server(
    *, username: str = SERVER_USERNAME, password: str = SERVER_PASSWORD, **kwargs: Any
) -> hardcharger.Server:
    """Start a Hardcharger server (with the test credentials unless others are given); the
    caller stops it.

    Raises (it never skips) when the server can't start.
    """
    binary = os.environ.get("IBIS_HARDCHARGER_TEST_SERVER_BINARY") or None
    version = os.environ.get("IBIS_HARDCHARGER_TEST_SERVER_VERSION") or None
    extra_args = [
        # Sessions start in a zone that is not UTC on any machine, so the tests show that the
        # backend's sessions are in UTC because it puts them there (CI runners are in UTC).
        "--init-sql-commands",
        f"SET GLOBAL TimeZone = '{SERVER_TIME_ZONE}'",
        *kwargs.pop("extra_args", ()),
    ]
    server = hardcharger.Server(
        username=username,
        password=password,
        binary=binary,
        version=version,
        extra_args=extra_args,
        **kwargs,
    )
    with pytest.MonkeyPatch.context() as patch:
        for name in list(os.environ):
            if name.startswith("HARDCHARGER_") and name not in _INSTALLER_VARIABLES:
                patch.delenv(name=name)
        return server.start()
