"""Map Ibis-style connection arguments onto ``adbc_driver_hardcharger.dbapi.connect()``.

Pure functions, so they are tested without a server (``tests/unit``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

if TYPE_CHECKING:
    from collections.abc import Mapping
    from urllib.parse import ParseResult

# Ibis-style argument -> the driver's name for it.
_ALIASES = {
    "user": "username",
    "database": "catalog",
    "schema": "db_schema",
    "disable_certificate_verification": "tls_skip_verify",
}

# URL query spellings accepted besides the keyword arguments themselves: the driver's own
# ``?tls=false``, and ibis-gizmosql's camelCase parameters, so its URLs carry over.
_URL_ALIASES = {
    "tls": "use_encryption",
    "useEncryption": "use_encryption",
    "disableCertificateVerification": "disable_certificate_verification",
    "authType": "auth_type",
    "oauthTimeout": "oauth_timeout",
    "openBrowser": "open_browser",
}

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def _to_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false, not {value!r}")


def _to_bool_or_text(name: str, value: Any) -> bool | str:
    """A URL value for an option that is a flag or a name (``open_browser``, ``share_sign_in``)."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return str(value)


def _to_seconds(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number of seconds, not {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number of seconds, not {value!r}") from None


# How each argument that can arrive as URL text is converted.
_URL_CONVERTERS = {
    "use_encryption": _to_bool,
    "disable_certificate_verification": _to_bool,
    "tls_skip_verify": _to_bool,
    "open_browser": _to_bool_or_text,
    "share_sign_in": _to_bool_or_text,
    "oauth_timeout": _to_seconds,
    "resume_timeout": _to_seconds,
}


def build_uri(*, host: str, port: int | None = None, use_encryption: bool = True) -> str:
    """``hardcharger://host[:port][?tls=false]``, the driver's URI for a server.

    TLS is on unless ``use_encryption`` is false. Without a port the driver picks its
    default: 31337, or 443 for a GizmoData Cloud host.
    """
    if not host:
        raise ValueError("host can't be empty")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # an IPv6 address
    uri = f"hardcharger://{host}"
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"port must be an integer from 1 to 65535, not {port!r}")
        uri += f":{port}"
    if not use_encryption:
        uri += "?tls=false"
    return uri


def driver_kwargs(
    *,
    host: str | None = None,
    user: str | None = None,
    password: str | None = None,
    port: int | None = None,
    database: str | None = None,
    schema: str | None = None,
    uri: str | None = None,
    use_encryption: bool = True,
    disable_certificate_verification: bool = False,
    auth_type: str = "password",
    access_token: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The keyword arguments for ``adbc_driver_hardcharger.dbapi.connect()``.

    ``extra`` holds the driver's own arguments, passed through as they are; the driver's
    spelling of an Ibis-style argument (``username`` for ``user``, ``catalog`` for
    ``database``, ``db_schema`` for ``schema``, ``tls_skip_verify`` for
    ``disable_certificate_verification``) is accepted there too, but not together with it.
    """
    extra = dict(extra or {})
    given: dict[str, Any] = {
        "user": user,
        "database": database,
        "schema": schema,
        "disable_certificate_verification": disable_certificate_verification or None,
    }
    for ibis_name, driver_name in _ALIASES.items():
        if driver_name in extra:
            if given[ibis_name] is not None:
                raise ValueError(f"give {ibis_name}= or {driver_name}=, not both")
            given[ibis_name] = extra.pop(driver_name)

    if extra.get("autocommit", True) is not True:
        raise ValueError(
            "the Hardcharger backend needs the driver's autocommit: without it nothing Ibis "
            "writes is committed, and the server ends a session left idle in a transaction"
        )

    profile = extra.get("profile")
    if uri is not None and host is not None:
        raise ValueError("give host= (with port= and use_encryption=) or uri=, not both")
    if uri is None and host is None and profile is None:
        raise ValueError("a Hardcharger connection needs host= (or uri=, or profile=)")
    if host is not None:
        uri = build_uri(host=host, port=port, use_encryption=use_encryption)
    else:
        if port is not None:
            raise ValueError("port= goes with host=: put the port in the uri")
        if not use_encryption:
            raise ValueError("use_encryption=False goes with host=: add ?tls=false to the uri")

    kwargs: dict[str, Any] = dict(extra)
    if uri is not None:
        kwargs["uri"] = uri
    kwargs["auth_type"] = auth_type
    for name, value in (
        ("username", given["user"]),
        ("password", password),
        ("access_token", access_token),
        ("catalog", given["database"]),
        ("db_schema", given["schema"]),
    ):
        if value is not None:
            kwargs[name] = value
    if given["disable_certificate_verification"]:
        kwargs["tls_skip_verify"] = True
    return kwargs


def kwargs_from_url(url: ParseResult, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """``Backend.connect()`` arguments for ``ibis.connect("hardcharger://...")``.

    ``hardcharger://[user[:password]@]host[:port][/catalog[/schema]][?option=value&...]``.
    ``kwargs`` are the URL's query parameters merged with ``ibis.connect()``'s own keyword
    arguments, as Ibis passes them. User name and password are percent-decoded.
    """
    if not url.hostname:
        raise ValueError(
            "a hardcharger:// URL needs a host: hardcharger://[user[:password]@]host[:port]"
        )
    out: dict[str, Any] = {}
    for name, value in kwargs.items():
        name = _URL_ALIASES.get(name, name)
        if name in out:
            raise ValueError(f"{name} is given more than once")
        converter = _URL_CONVERTERS.get(name)
        out[name] = converter(name, value) if converter is not None else value

    path = [unquote(part) for part in url.path.strip("/").split("/")] if url.path else []
    path = [part for part in path if part]
    if len(path) > 2:
        raise ValueError("the URL path is /catalog or /catalog/schema")
    from_url: dict[str, Any] = {
        "host": url.hostname,
        "port": url.port,
        "user": unquote(url.username) if url.username is not None else None,
        "password": unquote(url.password) if url.password is not None else None,
        "database": path[0] if path else None,
        "schema": path[1] if len(path) > 1 else None,
    }
    for name, value in from_url.items():
        if value is None:
            continue
        if name in out or _ALIASES.get(name) in out:
            raise ValueError(f"{name} is in the URL and also given as an argument")
        out[name] = value
    return out
