import ast
import tokenize
from functools import lru_cache

from reiz.db.connection import connect
from reiz.db.schema import protected_name
from reiz.edgeql import (
    EdgeQLCall,
    EdgeQLSelect,
    EdgeQLSelector,
    EdgeQLUnion,
    as_edgeql,
)
from reiz.reizql import ReizQLSyntaxError, compile_edgeql, parse_query
from reiz.utilities import get_db_settings, logger

DEFAULT_LIMIT = 10
DEFAULT_NODES = ("Module", "AST", "stmt", "expr")


class LocationNode(ast.AST):
    _attributes = ("lineno", "col_offset", "end_lineno", "end_col_offset")


@lru_cache(8)
def get_stats(nodes=DEFAULT_NODES):
    query = as_edgeql(
        EdgeQLSelect(
            EdgeQLUnion.from_seq(
                EdgeQLCall("count", [protected_name(node, prefix=True)])
                for node in nodes
            )
        ),
    )

    with connect(**get_db_settings()) as conn:
        stats = tuple(conn.query(query))

    return dict(zip(nodes, stats))


def fetch(filename, **loc_data):
    with tokenize.open(filename) as file:
        source = file.read()
    if loc_data:
        loc_node = LocationNode(**loc_data)
        return ast.get_source_segment(source, loc_node)
    else:
        return source


def run_query(reiz_ql, stats=False, limit=DEFAULT_LIMIT):
    tree = parse_query(reiz_ql)
    logger.info("ReizQL Tree: %r", tree)

    selection = compile_edgeql(tree)
    if stats:
        selection = EdgeQLSelect(EdgeQLCall("count", [selection]))
    else:
        selection.limit = limit
        if tree.positional:
            selection.selections.extend(
                (
                    EdgeQLSelector("lineno"),
                    EdgeQLSelector("col_offset"),
                    EdgeQLSelector("end_lineno"),
                    EdgeQLSelector("end_col_offset"),
                )
            )
            # FIX-IN(schema-change)
            module_matcher = EdgeQLSelector(
                "_module", [EdgeQLSelector("filename")]
            )
            if tree.name == "arg":
                if "annotation" not in tree.filters:
                    raise ReizQLSyntaxError(
                        "Matching arg() without a valid annotation is not possible right now"
                    )
                selection.selections.append(
                    EdgeQLSelector("annotation", [module_matcher])
                )
            else:
                selection.selections.append(module_matcher)
        elif tree.name == "Module":
            selection.selections.append(EdgeQLSelector("filename"))
        else:
            raise ReizQLSyntaxError(f"Unexpected root matcher: {tree.name}")

    query = as_edgeql(selection)
    logger.info("EdgeQL query: %r", query)

    results = []
    with connect(**get_db_settings()) as conn:
        if stats:
            return conn.query_one(query)

        query_set = conn.query(query)

        for result in query_set:
            loc_data = {}
            if tree.positional:
                if tree.name == "arg":
                    filename = result.annotation._module.filename
                else:
                    filename = result._module.filename
                loc_data.update(
                    {
                        "filename": filename,
                        "lineno": result.lineno,
                        "col_offset": result.col_offset,
                        "end_lineno": result.end_lineno,
                        "end_col_offset": result.end_col_offset,
                    }
                )
            elif tree.name == "Module":
                loc_data.update({"filename": result.filename})

            try:
                source = fetch(**loc_data)
            except Exception:
                source = None

            results.append(
                {
                    "source": source,
                    "filename": loc_data["filename"],
                }
            )

    return results
