"""ibis-gizmosql's basic test: TPC-H query 1 through the Ibis dataframe API, on data the
server generates (DuckDB's tpch extension)."""

from __future__ import annotations

import ibis
import pytest
from ibis import _


@pytest.fixture
def lineitem(con):
    con.load_extension("tpch")
    con.raw_sql(f"CALL dbgen(sf = 0.01, schema = '{con.current_database}')").close()
    return con.table("lineitem")


def test_ibis_basics(con, lineitem):
    t = lineitem

    # use the Ibis dataframe API to run TPC-H query 1
    expr = (
        t.filter(_.l_shipdate.cast("date") <= ibis.date("1998-12-01") + ibis.interval(days=90))
        .mutate(discount_price=_.l_extendedprice * (1 - _.l_discount))
        .mutate(charge=_.discount_price * (1 + _.l_tax))
        .group_by([_.l_returnflag, _.l_linestatus])
        .aggregate(
            sum_qty=_.l_quantity.sum(),
            sum_base_price=_.l_extendedprice.sum(),
            sum_disc_price=_.discount_price.sum(),
            sum_charge=_.charge.sum(),
            avg_qty=_.l_quantity.mean(),
            avg_price=_.l_extendedprice.mean(),
            avg_disc=_.l_discount.mean(),
            count_order=_.count(),
        )
        .order_by([_.l_returnflag, _.l_linestatus])
    )

    results = expr.execute()
    assert results is not None

    # The same query as SQL, on the server, gives the same rows.
    expected = con.sql(
        """
        SELECT l_returnflag, l_linestatus, sum(l_quantity) AS sum_qty, count(*) AS count_order
        FROM lineitem
        WHERE l_shipdate <= DATE '1998-12-01' + INTERVAL 90 DAY
        GROUP BY ALL ORDER BY ALL
        """
    ).execute()
    assert results["l_returnflag"].tolist() == ["A", "N", "N", "R"]
    assert results["l_linestatus"].tolist() == ["F", "F", "O", "F"]
    assert results["sum_qty"].tolist() == expected["sum_qty"].tolist()
    assert results["count_order"].tolist() == expected["count_order"].tolist()
    assert results["count_order"].sum() == t.count().execute()
