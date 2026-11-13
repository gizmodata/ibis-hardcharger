#!/usr/bin/env python3
"""Run every Python example in README.md against live Hardcharger servers, as written.

The README's examples connect to what a reader following it has, so this script provides it:

- ``127.0.0.1:31337``, plain HTTP, user ``hardcharger`` with password ``change-me``: started
  with the very command the README gives (``README_SERVER_COMMAND``; the script fails if the
  README no longer shows it);
- ``localhost:31338``, TLS with a certificate from a private CA, whose certificate is
  ``ca.pem`` in the directory the examples run in;
- ``127.0.0.1:31339``, with browser sign-in through a mock identity provider; the "browser"
  the driver opens (``HARDCHARGER_BROWSER``) is a script that follows the sign-in redirect;
- ``HARDCHARGER_TOKEN``: an access token for the first server.

Each ```python block runs in a fresh interpreter, in one scratch directory. A ```text block
right after one must appear in its output (whitespace-normalized). Any failure, a port that
is already in use, or a README with no Python examples, exits non-zero.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adbc_driver_hardcharger import dbapi  # noqa: E402

from tests.support.certificates import make_certificates  # noqa: E402
from tests.support.server import clean_environment, start_server  # noqa: E402

SUPPORT_DIR = ROOT / "tests" / "support"
USERNAME = "hardcharger"
PASSWORD = "change-me"
PLAIN_PORT, TLS_PORT, OAUTH_PORT = 31337, 31338, 31339
#: The line of README.md that starts the server its examples connect to.
README_SERVER_COMMAND = "HARDCHARGER_ADMIN_PASSWORD='change-me' hardcharger-server --host 127.0.0.1"
AUDIENCE = "ibis-hardcharger-readme"
CLIENT_ID = "ibis-hardcharger-readme-client"
CLIENT_SECRET = "ibis-hardcharger-readme-secret"
FENCE = re.compile(pattern=r"^```(\w*)\n(.*?)^```", flags=re.MULTILINE | re.DOTALL)


def examples(*, readme: str) -> list[tuple[str, str | None]]:
    """(code, expected output or None) for each ```python block."""
    blocks = [(match.group(1), match.group(2)) for match in FENCE.finditer(string=readme)]
    found = []
    for index, (language, body) in enumerate(blocks):
        if language != "python":
            continue
        expected = None
        if index + 1 < len(blocks) and blocks[index + 1][0] == "text":
            expected = blocks[index + 1][1]
        found.append((body, expected))
    return found


def normalized(*, text: str) -> str:
    return " ".join(text.split())


def port_in_use(*, port: int) -> bool:
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with socket.socket(family=family, type=socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            try:
                if sock.connect_ex((address, port)) == 0:
                    return True
            except OSError:
                pass
    return False


def start_readme_server(*, log: pathlib.Path) -> subprocess.Popen:
    """The server of the README's "start a server" command, run as written there."""
    with log.open(mode="wb") as out:
        process = subprocess.Popen(
            args=[sys.executable, "-m", "hardcharger", "server", "--host", "127.0.0.1"],
            env={**clean_environment(), "HARDCHARGER_ADMIN_PASSWORD": PASSWORD},
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if port_in_use(port=PLAIN_PORT):
            return process
        time.sleep(0.1)
    process.kill()
    raise RuntimeError(f"the README's server did not start:\n{log.read_text(errors='replace')}")


def main() -> int:
    readme = (ROOT / "README.md").read_text()
    blocks = examples(readme=readme)
    if not blocks:
        print("README.md has no Python examples", file=sys.stderr)
        return 1
    if README_SERVER_COMMAND not in readme:
        print(f"README.md no longer shows: {README_SERVER_COMMAND}", file=sys.stderr)
        return 1
    busy = [port for port in (PLAIN_PORT, TLS_PORT, OAUTH_PORT) if port_in_use(port=port)]
    if busy:
        print(
            f"ports in use (the examples would reach someone else's server): {busy}",
            file=sys.stderr,
        )
        return 1

    work = pathlib.Path(tempfile.mkdtemp(prefix="readme-examples-"))
    run_dir = work / "run"
    run_dir.mkdir()
    certs = make_certificates(out_dir=work / "certs")
    shutil.copyfile(src=certs["ca"], dst=run_dir / "ca.pem")

    idp = subprocess.Popen(
        args=[sys.executable, str(SUPPORT_DIR / "mock_idp.py"), AUDIENCE, CLIENT_ID, CLIENT_SECRET],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert idp.stdout is not None
    issuer = idp.stdout.readline().strip()
    servers = []
    plain = None
    failures = 0
    try:
        if not issuer.startswith("http://127.0.0.1:"):
            raise RuntimeError(f"the mock identity provider did not start: {issuer!r}")
        plain = start_readme_server(log=work / "server.log")
        servers.append(
            start_server(
                username=USERNAME,
                password=PASSWORD,
                port=TLS_PORT,
                tls_cert=certs["cert"],
                tls_key=certs["key"],
            )
        )
        servers.append(
            start_server(
                username=USERNAME,
                password=PASSWORD,
                port=OAUTH_PORT,
                extra_args=[
                    "--token-allowed-issuer", issuer,
                    "--token-allowed-audience", AUDIENCE,
                    "--token-default-role", "admin",
                    "--token-authorized-emails", "*@example.com",
                    "--oauth-client-id", CLIENT_ID,
                    "--oauth-client-secret", CLIENT_SECRET,
                    "--oauth-base-url", f"http://127.0.0.1:{OAUTH_PORT}",
                ],
            )
        )  # fmt: skip
        with dbapi.connect(
            uri=f"hardcharger://127.0.0.1:{PLAIN_PORT}?tls=false",
            username=USERNAME,
            password=PASSWORD,
        ) as conn:
            token = conn.access_token

        env = clean_environment()
        env["HARDCHARGER_TOKEN"] = token
        env["HARDCHARGER_BROWSER"] = f"{sys.executable} {SUPPORT_DIR / 'fake_browser.py'} code-ok"
        for number, (code, expected) in enumerate(blocks, start=1):
            first_line = code.strip().splitlines()[0]
            print(f"--- example {number}: {first_line}", flush=True)
            result = subprocess.run(
                args=[sys.executable, "-c", code],
                cwd=run_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            print(result.stdout, end="")
            if result.returncode != 0:
                print(result.stderr, file=sys.stderr)
                print(f"FAILED: example {number} exited {result.returncode}", file=sys.stderr)
                failures += 1
            elif expected is not None and normalized(text=expected) not in normalized(
                text=result.stdout
            ):
                print(
                    f"FAILED: example {number}'s output doesn't contain:\n{expected}",
                    file=sys.stderr,
                )
                failures += 1
    finally:
        for server in servers:
            server.stop()
        if plain is not None:
            plain.terminate()
            plain.wait(timeout=30)
        idp.kill()
        idp.wait()
        shutil.rmtree(path=work, ignore_errors=True)
    print(f"{len(blocks) - failures} of {len(blocks)} README examples passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
