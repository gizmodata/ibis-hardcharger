from ibis.backends.tests.test_client import *  # noqa: F401,F403


# Ibis's versions of these two look the backend up in a dict written inside the test, which
# has no "hardcharger" key; the assertions are Ibis's for DuckDB.
def test_list_catalogs(con):
    assert {"memory"} <= set(con.list_catalogs())


def test_list_database_contents(con):
    assert {"pg_catalog", "main", "information_schema"} <= set(con.list_databases())
