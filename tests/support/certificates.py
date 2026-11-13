"""A throwaway certificate authority for the TLS and certificate sign-in tests.

Adapted from gizmodata/sqlframe-hardcharger tests/hardcharger_server.py (Apache-2.0).
"""

from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path

#: The e-mail identity of the client certificate that signs in as the administrator.
ADMIN_CERT_IDENTITY = "ibis-admin@example.com"
#: A client certificate the server knows nothing about.
STRANGER_CERT_IDENTITY = "stranger@example.com"


def make_certificates(*, out_dir: Path) -> dict[str, Path]:
    """A CA (``ca``); a server certificate for 127.0.0.1 and localhost it signed (``cert``,
    ``key``); and client certificates ``admin`` (:data:`ADMIN_CERT_IDENTITY`) and ``stranger``
    (:data:`STRANGER_CERT_IDENTITY`), each with its key at ``paths[name + "-key"]``."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(tz=datetime.UTC)

    def name(common_name: str) -> x509.Name:
        return x509.Name(
            attributes=[x509.NameAttribute(oid=NameOID.COMMON_NAME, value=common_name)]
        )

    def pem_key(key) -> bytes:
        return key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    ca_key = ec.generate_private_key(curve=ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(name=name(common_name="ibis-hardcharger test CA"))
        .issuer_name(name=name(common_name="ibis-hardcharger test CA"))
        .public_key(key=ca_key.public_key())
        .serial_number(number=x509.random_serial_number())
        .not_valid_before(time=now - datetime.timedelta(minutes=5))
        .not_valid_after(time=now + datetime.timedelta(days=1))
        .add_extension(extval=x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            extval=x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            extval=x509.SubjectKeyIdentifier.from_public_key(public_key=ca_key.public_key()),
            critical=False,
        )
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )

    def leaf(*, common_name: str, san: list, usage) -> tuple:
        key = ec.generate_private_key(curve=ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(name=name(common_name=common_name))
            .issuer_name(name=ca_cert.subject)
            .public_key(key=key.public_key())
            .serial_number(number=x509.random_serial_number())
            .not_valid_before(time=now - datetime.timedelta(minutes=5))
            .not_valid_after(time=now + datetime.timedelta(days=1))
            .add_extension(extval=x509.SubjectAlternativeName(general_names=san), critical=False)
            .add_extension(extval=x509.ExtendedKeyUsage(usages=[usage]), critical=False)
            .add_extension(
                extval=x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    public_key=ca_key.public_key()
                ),
                critical=False,
            )
            .sign(private_key=ca_key, algorithm=hashes.SHA256())
        )
        return cert, key

    paths = {"ca": out_dir / "ca.pem"}
    paths["ca"].write_bytes(data=ca_cert.public_bytes(encoding=serialization.Encoding.PEM))
    leaves = {
        "cert": leaf(
            common_name="localhost",
            san=[
                x509.DNSName(value="localhost"),
                x509.IPAddress(value=ipaddress.ip_address(address="127.0.0.1")),
            ],
            usage=ExtendedKeyUsageOID.SERVER_AUTH,
        ),
        "admin": leaf(
            common_name="admin",
            san=[x509.RFC822Name(value=ADMIN_CERT_IDENTITY)],
            usage=ExtendedKeyUsageOID.CLIENT_AUTH,
        ),
        "stranger": leaf(
            common_name="stranger",
            san=[x509.RFC822Name(value=STRANGER_CERT_IDENTITY)],
            usage=ExtendedKeyUsageOID.CLIENT_AUTH,
        ),
    }
    for label, (cert, key) in leaves.items():
        key_label = "key" if label == "cert" else f"{label}-key"
        cert_path = out_dir / ("server.pem" if label == "cert" else f"{label}.pem")
        key_path = out_dir / ("server-key.pem" if label == "cert" else f"{label}-key.pem")
        cert_path.write_bytes(data=cert.public_bytes(encoding=serialization.Encoding.PEM))
        key_path.write_bytes(data=pem_key(key=key))
        paths[label] = cert_path
        paths[key_label] = key_path
    return paths
