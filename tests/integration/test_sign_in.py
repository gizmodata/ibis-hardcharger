"""TLS, client-certificate (mutual TLS) sign-in and browser (OAuth) sign-in, each against a
server of its own. Every refusal asserts why it was refused, next to a twin that succeeds."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import adbc_driver_manager
import ibis
import pyarrow as pa
import pytest

from tests.support.certificates import ADMIN_CERT_IDENTITY, make_certificates
from tests.support.server import SERVER_PASSWORD, SERVER_USERNAME, free_port, start_server

SUPPORT_DIR = Path(__file__).resolve().parent.parent / "support"
AUDIENCE = "ibis-hardcharger-test"
CLIENT_ID = "ibis-hardcharger-client"
CLIENT_SECRET = "ibis-hardcharger-secret"
IDP_EMAIL = "analyst@example.com"


def current_user(con) -> str:
    return con.con.current_user()


def auth_method(con) -> str:
    with con._safe_raw_sql(
        "SELECT auth_method FROM hardcharger_sessions() WHERE is_current"
    ) as cur:
        return cur.fetchone()[0]


# ---- TLS and client certificates ----------------------------------------------------------------


@pytest.fixture(scope="module")
def certs(tmp_path_factory) -> dict[str, Path]:
    return make_certificates(out_dir=tmp_path_factory.mktemp("certs"))


@pytest.fixture(scope="module")
def tls_server(certs):
    """A server that serves TLS, and offers sign-in by client certificate."""
    server = start_server(
        tls_cert=certs["cert"],
        tls_key=certs["key"],
        extra_args=[
            "--mtls-ca-cert",
            str(certs["ca"]),
            "--mtls-require-client-cert",
            "false",
            "--mtls-certificate-sign-in",
            "--mtls-admin-identities",
            ADMIN_CERT_IDENTITY,
        ],
    )
    yield server
    server.stop()


def test_password_over_verified_tls(tls_server, certs):
    con = ibis.hardcharger.connect(
        host=tls_server.host,
        port=tls_server.port,
        user=SERVER_USERNAME,
        password=SERVER_PASSWORD,
        tls_root_certs=str(certs["ca"]),
    )
    try:
        assert current_user(con=con) == SERVER_USERNAME
        assert auth_method(con=con) == "password"
        assert con.execute(ibis.literal(1) + 1) == 2
    finally:
        con.disconnect()


def test_an_unknown_ca_is_refused(tls_server):
    """Certificates are verified by default."""
    with pytest.raises(adbc_driver_manager.Error) as excinfo:
        ibis.hardcharger.connect(
            host=tls_server.host,
            port=tls_server.port,
            user=SERVER_USERNAME,
            password=SERVER_PASSWORD,
        )
    # Go's wording on Linux (CI) and on macOS.
    reasons = ("certificate signed by unknown authority", "certificate is not trusted")
    assert any(reason in str(excinfo.value) for reason in reasons), str(excinfo.value)


def test_disable_certificate_verification(tls_server):
    con = ibis.hardcharger.connect(
        host=tls_server.host,
        port=tls_server.port,
        user=SERVER_USERNAME,
        password=SERVER_PASSWORD,
        disable_certificate_verification=True,
    )
    try:
        assert current_user(con=con) == SERVER_USERNAME
    finally:
        con.disconnect()


def test_tls_options_in_a_url(tls_server, certs):
    url = f"hardcharger://{SERVER_USERNAME}:{SERVER_PASSWORD}@{tls_server.host}:{tls_server.port}"
    con = ibis.connect(f"{url}?disable_certificate_verification=true")
    try:
        assert current_user(con=con) == SERVER_USERNAME
    finally:
        con.disconnect()
    con = ibis.connect(url, tls_root_certs=str(certs["ca"]))
    try:
        assert current_user(con=con) == SERVER_USERNAME
    finally:
        con.disconnect()


def test_tls_server_name(tls_server, certs):
    """The certificate is checked against tls_server_name when it is given."""
    common = dict(
        host=tls_server.host,
        port=tls_server.port,
        user=SERVER_USERNAME,
        password=SERVER_PASSWORD,
        tls_root_certs=str(certs["ca"]),
    )
    con = ibis.hardcharger.connect(**common, tls_server_name="localhost")
    try:
        assert current_user(con=con) == SERVER_USERNAME
    finally:
        con.disconnect()
    with pytest.raises(adbc_driver_manager.Error, match="not-the-server.example.com"):
        ibis.hardcharger.connect(**common, tls_server_name="not-the-server.example.com")


def test_client_certificate_sign_in(tls_server, certs):
    con = ibis.hardcharger.connect(
        host=tls_server.host,
        port=tls_server.port,
        auth_type="client_certificate",
        tls_root_certs=str(certs["ca"]),
        tls_client_cert=str(certs["admin"]),
        tls_client_key=str(certs["admin-key"]),
    )
    try:
        assert current_user(con=con).lower() == ADMIN_CERT_IDENTITY
        assert auth_method(con=con) == "client_certificate"
        # A bulk load presents the certificate the token is bound to.
        t = con.create_table("certificate_load", pa.table({"k": list(range(50_000))}), temp=True)
        assert t.count().execute() == 50_000
    finally:
        con.disconnect()


def test_an_unknown_certificate_identity_is_refused(tls_server, certs):
    with pytest.raises(adbc_driver_manager.Error, match="unknown_certificate_identity"):
        ibis.hardcharger.connect(
            host=tls_server.host,
            port=tls_server.port,
            auth_type="mtls",
            tls_root_certs=str(certs["ca"]),
            tls_client_cert=str(certs["stranger"]),
            tls_client_key=str(certs["stranger-key"]),
        )


# ---- browser sign-in ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def idp_issuer():
    """The mock identity provider, in its own process (see tests/support/mock_idp.py)."""
    process = subprocess.Popen(
        args=[sys.executable, str(SUPPORT_DIR / "mock_idp.py"), AUDIENCE, CLIENT_ID, CLIENT_SECRET],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    issuer = process.stdout.readline().strip()
    if not issuer.startswith("http://127.0.0.1:"):
        process.kill()
        pytest.fail(reason=f"the mock identity provider did not start: {issuer!r}")
    yield issuer
    process.kill()
    process.wait()


@pytest.fixture(scope="module")
def oauth_server(idp_issuer):
    """A server that offers browser sign-in through the mock identity provider."""
    port = free_port()
    server = start_server(
        port=port,
        extra_args=[
            "--token-allowed-issuer",
            idp_issuer,
            "--token-allowed-audience",
            AUDIENCE,
            "--token-default-role",
            "admin",
            "--token-authorized-emails",
            "*@example.com",
            "--oauth-client-id",
            CLIENT_ID,
            "--oauth-client-secret",
            CLIENT_SECRET,
            # The redirect URI names 127.0.0.1, where the tests dial the server.
            "--oauth-base-url",
            f"http://127.0.0.1:{port}",
        ],
    )
    yield server
    server.stop()


def fake_browser(*, code: str) -> str:
    """The command the driver opens the sign-in URL with: the identity provider sends it back
    to the server's callback with `code`."""
    return f"{sys.executable} {SUPPORT_DIR / 'fake_browser.py'} {code}"


def test_browser_sign_in(oauth_server):
    con = ibis.hardcharger.connect(
        host=oauth_server.host,
        port=oauth_server.port,
        use_encryption=False,
        auth_type="external",
        open_browser=fake_browser(code="code-ok"),
        oauth_timeout=30,
    )
    try:
        assert current_user(con=con) == IDP_EMAIL
        assert con.execute(ibis.literal(20) + 22) == 42
        t = con.create_table("signed_in", pa.table({"a": [1, 2, 3]}), temp=True)
        assert t.a.sum().execute() == 6
    finally:
        con.disconnect()


def test_browser_sign_in_from_a_url(oauth_server):
    con = ibis.connect(
        f"hardcharger://{oauth_server.host}:{oauth_server.port}"
        "?tls=false&auth_type=external&oauth_timeout=30",
        open_browser=fake_browser(code="code-ok"),
    )
    try:
        assert current_user(con=con) == IDP_EMAIL
    finally:
        con.disconnect()


def test_browser_sign_in_with_a_rejected_code(oauth_server):
    with pytest.raises(adbc_driver_manager.Error, match="invalid_grant"):
        ibis.hardcharger.connect(
            host=oauth_server.host,
            port=oauth_server.port,
            use_encryption=False,
            auth_type="external",
            open_browser=fake_browser(code="code-bad"),
            oauth_timeout=30,
        )


def test_browser_sign_in_on_a_server_without_it_is_refused(hardcharger_server):
    """The session's shared server has no identity provider configured."""
    with pytest.raises(adbc_driver_manager.Error) as excinfo:
        ibis.hardcharger.connect(
            host=hardcharger_server["host"],
            port=hardcharger_server["port"],
            use_encryption=False,
            auth_type="external",
            open_browser=False,
            oauth_timeout=5,
        )
    assert "oauth" in str(excinfo.value).lower(), str(excinfo.value)
