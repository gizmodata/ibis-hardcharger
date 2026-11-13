import ibis
from ibis.backends.tests.tpc.conftest import add_date, tpc_test
from ibis.backends.tests.tpc.h.test_queries import *  # noqa: F401,F403


# Ibis 12.0.0 decorates its test_10 with a bare `@tpc_test` (no suite name), which turns it
# into a function that asks for a fixture called `test` and never runs. This is Ibis's query,
# decorated like its other TPC-H tests.
@tpc_test("h")
def test_10(customer, orders, lineitem, nation):
    """Returned Item Reporting Query (Q10)"""
    DATE = "1993-10-01"

    q = customer
    q = q.join(orders, customer.c_custkey == orders.o_custkey)
    q = q.join(lineitem, lineitem.l_orderkey == orders.o_orderkey)
    q = q.join(nation, customer.c_nationkey == nation.n_nationkey)

    q = q.filter(
        (q.o_orderdate >= ibis.date(DATE)) & (q.o_orderdate < add_date(DATE, dm=3)),
        q.l_returnflag == "R",
    )

    gq = q.group_by(
        [
            q.c_custkey,
            q.c_name,
            q.c_acctbal,
            q.n_name,
            q.c_address,
            q.c_phone,
            q.c_comment,
        ]
    )
    q = gq.agg(revenue=(q.l_extendedprice * (1 - q.l_discount)).sum()).relocate(
        "revenue", after="c_name"
    )

    q = q.order_by(ibis.desc(q.revenue))
    return q.limit(20)
