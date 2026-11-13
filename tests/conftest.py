"""Shared test configuration: the Hardcharger server every live test runs against.

The server is the released ``hardcharger-server`` binary, started as a subprocess on a free
loopback port by the ``hardcharger`` PyPI package (no Docker). Tests that need it FAIL when
it can't start; nothing is skipped for lack of a server.

The tests read only these environment variables:

``IBIS_HARDCHARGER_TEST_SERVER_BINARY``
    A ``hardcharger-server`` binary to run instead of the downloaded release.
``IBIS_HARDCHARGER_TEST_SERVER_VERSION``
    The server release to download and run (default: the one the ``hardcharger`` package pins).
``IBIS_HARDCHARGER_TEST_DATA_DIR``
    The Ibis testing data (default: ``ci/ibis-testing-data``, see scripts/fetch_testing_data.sh).

(The ``hardcharger`` package itself reads ``HARDCHARGER_INSTALL_TOKEN`` / ``GH_TOKEN`` to
download the binary; every other ``HARDCHARGER_*`` variable is kept away from the server so a
developer's shell can't reconfigure it.)
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import hardcharger
import pytest
from filelock import FileLock

from tests.support.server import start_server

if TYPE_CHECKING:
    from collections.abc import Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Why a test may be skipped. A skip for any other reason fails the run: a skipped test is an
# unverified one, so each reason is listed here with what it means.
ALLOWED_SKIP_REASONS = (
    # Ibis's suite skips tests of an interface the backend's class doesn't have: catalogs
    # are attached with attach(), not created (CanCreateCatalog).
    r"^hardcharger backend cannot create databases$",
    r"^hardcharger backend cannot create both database and schemas$",
    r"^Method (create|drop)_catalog not present in <class 'ibis_hardcharger.Backend'>",
    # Ibis's suite skips tests that need a package this project doesn't depend on.
    r"^could not import '(torch|geopandas)'",
    r"^could not import 'ibis.expr.visualize': No module named 'graphviz'$",
    # Tests Ibis itself skips for every backend (@pytest.mark.skip in its source).
    r"^unconditional skip$",  # test_close_connection
    r"^risingwave$",  # test_semi_join_topk
)


@pytest.fixture(scope="session")
def hardcharger_server(tmp_path_factory, worker_id) -> Iterator[dict[str, Any]]:
    """The session's Hardcharger server: ``host``, ``port``, ``username``, ``password``.

    Under pytest-xdist every worker is its own session, and the Ibis suite loads its data
    once for all of them, so the workers share one server: the first to get here starts it
    and writes its address to a file in the run's shared temporary directory, the others read
    the file, and the one that started it stops it when every worker is done.
    """
    if worker_id == "master":
        server = start_server()
        try:
            yield _address(server=server)
        finally:
            server.stop()
        return

    shared = tmp_path_factory.getbasetemp().parent
    info_path = shared / "hardcharger_server.json"
    count_path = shared / "hardcharger_server.users"
    lock = FileLock(str(shared / "hardcharger_server.lock"))

    def users() -> int:
        try:
            return int(count_path.read_text())
        except FileNotFoundError:
            return 0

    server = None
    with lock:
        if not info_path.is_file():
            server = start_server()
            info_path.write_text(json.dumps(_address(server=server)))
        count_path.write_text(str(users() + 1))
    try:
        yield json.loads(info_path.read_text())
    finally:
        with lock:
            count_path.write_text(str(users() - 1))
        if server is not None:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                with lock:
                    if users() <= 0:
                        break
                time.sleep(0.2)
            server.stop()
            with contextlib.suppress(FileNotFoundError):
                info_path.unlink()


def _address(*, server: hardcharger.Server) -> dict[str, Any]:
    return {
        "host": server.host,
        "port": server.port,
        "username": server.username,
        "password": server.password,
    }


def pytest_configure(config) -> None:
    """Ibis's suite marks tests with the name of every backend Ibis knows; --strict-markers
    wants each registered."""
    from ibis.backends import _get_backend_names

    for name in sorted(_get_backend_names()):
        config.addinivalue_line("markers", f"{name}: Ibis's marker for tests of its {name} backend")


def pytest_collection_modifyitems(config, items) -> None:
    """Everything outside tests/unit runs against the live server."""
    unit = PROJECT_ROOT / "tests" / "unit"
    for item in items:
        if unit not in Path(item.path).parents:
            item.add_marker(pytest.mark.integration)


# ---- skips are failures unless their reason is listed above ---------------------------------

_SKIPS: list[str] = []


def pytest_runtest_logreport(report) -> None:
    if not report.skipped or hasattr(report, "wasxfail"):
        return
    longrepr = report.longrepr
    reason = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr)
    reason = reason.removeprefix("Skipped: ")
    if not any(re.search(pattern=pattern, string=reason) for pattern in ALLOWED_SKIP_REASONS):
        _SKIPS.append(f"{report.nodeid}: {reason}")


def pytest_terminal_summary(terminalreporter) -> None:
    if _SKIPS:
        terminalreporter.section("skips that are not allowed (tests/conftest.py)", red=True)
        for line in _SKIPS:
            terminalreporter.line(line)


def pytest_sessionfinish(session, exitstatus) -> None:
    if _SKIPS and exitstatus == 0:
        session.exitstatus = 1
