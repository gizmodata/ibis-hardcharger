"""The SQL Ibis sends to Hardcharger: Ibis's DuckDB compiler, adjusted for DuckDB 2.0."""

from __future__ import annotations

from typing import Any

import sqlglot.expressions as sge
from ibis.backends.sql.compilers.duckdb import DuckDBCompiler
from sqlglot.dialects.duckdb import DuckDB


class Hardcharger(DuckDB):
    """The ``hardcharger`` sqlglot dialect: Hardcharger speaks DuckDB SQL.

    Defining it registers the name, so ``con.sql(query, dialect="hardcharger")`` and
    ``sqlglot.transpile(..., read="hardcharger")`` work.
    """


class HardchargerCompiler(DuckDBCompiler):
    __slots__ = ()

    def to_sqlglot(self, *args: Any, **kwargs: Any) -> sge.Query:
        sql = super().to_sqlglot(*args, **kwargs)
        # DuckDB 2.0 refuses the arrow form of a lambda (`x -> x + 1`: "Deprecated lambda
        # arrow (->) detected"), which is what Ibis 12 writes. `lambda x: x + 1` is the form
        # every DuckDB since 1.3 takes; sqlglot writes it for a Lambda marked `colon`.
        for node in sql.find_all(sge.Lambda):
            node.set("colon", True)
        return sql


compiler = HardchargerCompiler()
