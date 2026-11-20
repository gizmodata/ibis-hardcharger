"""How Ibis-style connection arguments and URLs become the driver's arguments (no server)."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlparse

import pytest

from ibis_hardcharger._connect import build_uri, driver_kwargs, kwargs_from_url


def from_url(url: str, **explicit):
    """What ``ibis.connect(url, **explicit)`` hands to ``Backend._from_url``."""
    parsed = urlparse(url=url)
    kwargs = dict(parse_qsl(qs=parsed.query))
    kwargs.update(explicit)
    return kwargs_from_url(url=parsed, kwargs=kwargs)


class TestBuildUri:
    def test_tls_is_on_by_default_and_the_port_is_the_drivers(self):
        assert build_uri(host="db.example.com") == "hardcharger://db.example.com"

    def test_port(self):
        assert build_uri(host="db.example.com", port=8443) == "hardcharger://db.example.com:8443"

    def test_plain_http(self):
        assert (
            build_uri(host="127.0.0.1", port=31337, use_encryption=False)
            == "hardcharger://127.0.0.1:31337?tls=false"
        )

    def test_ipv6_host_is_bracketed(self):
        assert build_uri(host="::1", port=31337) == "hardcharger://[::1]:31337"
        assert build_uri(host="[::1]", port=31337) == "hardcharger://[::1]:31337"

    def test_empty_host_is_refused(self):
        with pytest.raises(ValueError, match="host can't be empty"):
            build_uri(host="")

    @pytest.mark.parametrize("port", [0, 65536, -1, "31337", True, 1.5])
    def test_bad_port_is_refused(self, port):
        with pytest.raises(ValueError, match="port must be an integer from 1 to 65535"):
            build_uri(host="db.example.com", port=port)


class TestDriverKwargs:
    def test_password_sign_in(self):
        assert driver_kwargs(
            host="db.example.com", user="joe", password="s3cret", database="lake", schema="sales"
        ) == {
            "uri": "hardcharger://db.example.com",
            "auth_type": "password",
            "username": "joe",
            "password": "s3cret",
            "catalog": "lake",
            "db_schema": "sales",
        }

    def test_access_token(self):
        assert driver_kwargs(host="db.example.com", access_token="tok") == {
            "uri": "hardcharger://db.example.com",
            "auth_type": "password",
            "access_token": "tok",
        }

    def test_browser_sign_in_sends_no_credentials(self):
        assert driver_kwargs(host="db.example.com", auth_type="external") == {
            "uri": "hardcharger://db.example.com",
            "auth_type": "external",
        }

    def test_certificate_verification_is_on_unless_disabled(self):
        assert "tls_skip_verify" not in driver_kwargs(host="db.example.com")
        assert (
            driver_kwargs(host="db.example.com", disable_certificate_verification=True)[
                "tls_skip_verify"
            ]
            is True
        )

    def test_driver_arguments_pass_through(self):
        kwargs = driver_kwargs(
            host="db.example.com",
            extra={
                "tls_root_certs": "ca.pem",
                "settings": {"query_timeout": 60},
                "session_tag": {"app": "etl"},
                "db_kwargs": {"hardcharger.ipc.compression": "zstd"},
            },
        )
        assert kwargs["tls_root_certs"] == "ca.pem"
        assert kwargs["settings"] == {"query_timeout": 60}
        assert kwargs["session_tag"] == {"app": "etl"}
        assert kwargs["db_kwargs"] == {"hardcharger.ipc.compression": "zstd"}

    def test_the_drivers_own_names_are_accepted(self):
        kwargs = driver_kwargs(
            host="db.example.com",
            extra={
                "username": "joe",
                "catalog": "lake",
                "db_schema": "sales",
                "tls_skip_verify": True,
            },
        )
        assert kwargs["username"] == "joe"
        assert kwargs["catalog"] == "lake"
        assert kwargs["db_schema"] == "sales"
        assert kwargs["tls_skip_verify"] is True

    @pytest.mark.parametrize(
        ("ibis_name", "driver_name"),
        [("user", "username"), ("database", "catalog"), ("schema", "db_schema")],
    )
    def test_both_spellings_of_one_argument_are_refused(self, ibis_name, driver_name):
        with pytest.raises(ValueError, match=f"give {ibis_name}= or {driver_name}=, not both"):
            driver_kwargs(host="db.example.com", **{ibis_name: "a"}, extra={driver_name: "b"})

    def test_uri_instead_of_host(self):
        kwargs = driver_kwargs(uri="hardcharger://db.example.com:443/prefix", user="joe")
        assert kwargs["uri"] == "hardcharger://db.example.com:443/prefix"

    def test_profile_needs_no_host(self):
        kwargs = driver_kwargs(extra={"profile": "prod"})
        assert kwargs["profile"] == "prod"
        assert "uri" not in kwargs

    def test_host_and_uri_together_are_refused(self):
        with pytest.raises(ValueError, match="give host=.* or uri=, not both"):
            driver_kwargs(host="db.example.com", uri="hardcharger://db.example.com")

    def test_no_server_is_refused(self):
        with pytest.raises(ValueError, match=r"needs host= \(or uri=, or profile=\)"):
            driver_kwargs(user="joe", password="s3cret")

    def test_port_with_uri_is_refused(self):
        with pytest.raises(ValueError, match="port= goes with host="):
            driver_kwargs(uri="hardcharger://db.example.com", port=31337)

    def test_plain_http_with_uri_is_refused(self):
        with pytest.raises(ValueError, match=r"add \?tls=false to the uri"):
            driver_kwargs(uri="hardcharger://db.example.com", use_encryption=False)

    def test_turning_autocommit_off_is_refused(self):
        with pytest.raises(ValueError, match="needs the driver's autocommit"):
            driver_kwargs(host="db.example.com", extra={"autocommit": False})


class TestUrl:
    def test_full_url(self):
        assert from_url("hardcharger://joe:s3cret@db.example.com:8443/lake/sales") == {
            "host": "db.example.com",
            "port": 8443,
            "user": "joe",
            "password": "s3cret",
            "database": "lake",
            "schema": "sales",
        }

    def test_host_only(self):
        assert from_url("hardcharger://db.example.com") == {"host": "db.example.com"}

    def test_catalog_only(self):
        assert from_url("hardcharger://db.example.com/lake")["database"] == "lake"
        assert "schema" not in from_url("hardcharger://db.example.com/lake")

    def test_the_drivers_tls_parameter(self):
        assert from_url("hardcharger://127.0.0.1:31337?tls=false") == {
            "host": "127.0.0.1",
            "port": 31337,
            "use_encryption": False,
        }
        assert from_url("hardcharger://db.example.com?tls=true")["use_encryption"] is True

    def test_keyword_arguments_as_query_parameters(self):
        kwargs = from_url(
            "hardcharger://db.example.com?auth_type=external&oauth_timeout=30"
            "&open_browser=false&disable_certificate_verification=true&tls_server_name=db"
        )
        assert kwargs == {
            "host": "db.example.com",
            "auth_type": "external",
            "oauth_timeout": 30.0,
            "open_browser": False,
            "disable_certificate_verification": True,
            "tls_server_name": "db",
        }

    def test_a_url_cannot_name_a_program_to_run(self):
        """The driver runs open_browser's text as a command during browser sign-in, and URLs
        are often configuration someone else supplies."""
        with pytest.raises(ValueError, match="open_browser must be true or false"):
            from_url("hardcharger://db.example.com?auth_type=external&open_browser=firefox")
        with pytest.raises(ValueError, match="open_browser must be true or false"):
            from_url("hardcharger://db.example.com?openBrowser=touch+/tmp/marker")
        assert from_url("hardcharger://db.example.com?open_browser=false")["open_browser"] is False

    def test_a_command_is_still_fine_as_a_keyword_argument(self):
        kwargs = from_url("hardcharger://db.example.com?auth_type=external", open_browser="firefox")
        assert kwargs["open_browser"] == "firefox"

    @pytest.mark.parametrize(
        "option", ["session_id", "db_kwargs", "conn_kwargs", "settings", "autocommit", "uri", "x"]
    )
    def test_a_url_sets_only_the_listed_options(self, option):
        with pytest.raises(ValueError, match=f"{option} can't be set in a URL"):
            from_url(f"hardcharger://db.example.com?{option}=anything")

    def test_an_option_error_never_repeats_the_value(self):
        for url in (
            "hardcharger://db.example.com?tls=tok_SECRET",
            "hardcharger://db.example.com?open_browser=tok_SECRET",
            "hardcharger://db.example.com?oauth_timeout=tok_SECRET",
            "hardcharger://db.example.com?session_id=tok_SECRET",
        ):
            with pytest.raises(ValueError) as caught:
                from_url(url)
            assert "tok_SECRET" not in str(caught.value)

    def test_ibis_gizmosql_parameter_names_carry_over(self):
        kwargs = from_url(
            "hardcharger://joe:s3cret@db.example.com:31337"
            "?useEncryption=True&disableCertificateVerification=True&authType=password"
            "&oauthTimeout=45&openBrowser=false"
        )
        assert kwargs["use_encryption"] is True
        assert kwargs["disable_certificate_verification"] is True
        assert kwargs["auth_type"] == "password"
        assert kwargs["oauth_timeout"] == 45.0
        assert kwargs["open_browser"] is False

    def test_user_and_password_are_percent_decoded(self):
        kwargs = from_url("hardcharger://jo%40example.com:p%3Ass%2Fw%2Brd%20x@db.example.com")
        assert kwargs["user"] == "jo@example.com"
        assert kwargs["password"] == "p:ss/w+rd x"

    def test_a_plus_in_a_password_stays_a_plus(self):
        assert from_url("hardcharger://joe:a+b@db.example.com")["password"] == "a+b"

    def test_explicit_arguments_join_the_urls(self):
        kwargs = from_url("hardcharger://db.example.com?tls=false", user="joe", password="pw")
        assert kwargs == {
            "host": "db.example.com",
            "use_encryption": False,
            "user": "joe",
            "password": "pw",
        }

    def test_an_argument_given_twice_is_refused(self):
        with pytest.raises(ValueError, match="user is in the URL and also given as an argument"):
            from_url("hardcharger://joe@db.example.com", user="ann")
        with pytest.raises(ValueError, match="use_encryption is given more than once"):
            from_url("hardcharger://db.example.com?tls=false&useEncryption=true")

    def test_a_url_without_a_host_is_refused(self):
        with pytest.raises(ValueError, match="a hardcharger:// URL needs a host"):
            from_url("hardcharger://")

    def test_a_path_deeper_than_catalog_and_schema_is_refused(self):
        with pytest.raises(ValueError, match="the URL path is /catalog or /catalog/schema"):
            from_url("hardcharger://db.example.com/a/b/c")

    @pytest.mark.parametrize("value", ["maybe", "2", ""])
    def test_a_bad_boolean_is_refused(self, value):
        with pytest.raises(ValueError, match="use_encryption must be true or false"):
            from_url(f"hardcharger://db.example.com?tls={value or '%20'}")

    def test_a_bad_timeout_is_refused(self):
        with pytest.raises(ValueError, match="oauth_timeout must be a number of seconds"):
            from_url("hardcharger://db.example.com?oauth_timeout=soon")
