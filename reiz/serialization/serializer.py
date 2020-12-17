import ast
import functools
import tokenize
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from reiz.db.schema import (
    ATOMIC_TYPES,
    ENUM_TYPES,
    MODULE_ANNOTATED_TYPES,
    protected_name,
)
from reiz.edgeql import (
    EdgeQLCall,
    EdgeQLCast,
    EdgeQLComparisonOperator,
    EdgeQLFilter,
    EdgeQLFilterKey,
    EdgeQLInsert,
    EdgeQLReference,
    EdgeQLReizCustomList,
    EdgeQLSelect,
    EdgeQLSet,
    EdgeQLUpdate,
    EdgeQLVariable,
    as_edgeql,
    make_filter,
)
from reiz.serialization.transformers import (
    QLAst,
    Sentinel,
    infer_base,
    iter_attributes,
)
from reiz.utilities import guarded, logger


@dataclass(unsafe_hash=True)
class QLState:
    fields: Dict[str, Any] = field(default_factory=dict)
    from_parent: Optional[ast.AST] = None
    reference_pool: List[str] = field(default_factory=list)


@functools.singledispatch
def serialize(obj, ql_state, connection):
    if type(obj) is int:
        return obj
    else:
        message = f"Unexpected object: {obj!r}"
        if ql_state.from_parent is not None:
            message += f" flowing from {ql_state.from_parent}"
        raise ValueError(message + ".")


def serialize_sum(obj, ql_state, connection):
    obj_type = type(obj)
    enum_type = obj_type.__base__
    return EdgeQLCast(
        protected_name(enum_type.__name__, prefix=True),
        repr(obj_type.__name__),
    )


@serialize.register(ast.AST)
def serialize_ast(obj, ql_state, connection):
    if isinstance(obj, ENUM_TYPES):
        return serialize_sum(obj, ql_state, connection)

    db_obj = insert(connection, ql_state, obj)
    ql_state.reference_pool.append(db_obj.id)
    return EdgeQLSelect(
        infer_base(obj).__name__,
        filters=make_filter(id=EdgeQLReference(db_obj)),
        limit=1,
    )


@serialize.register(list)
def serialize_list(obj, ql_state, connection):
    qlset = EdgeQLSet(
        [serialize(value, ql_state, connection) for value in obj]
    )
    if all(isinstance(value, ENUM_TYPES + ATOMIC_TYPES) for value in obj):
        return qlset
    else:
        return EdgeQLReizCustomList(qlset)


@serialize.register(str)
def serialize_string(obj, ql_state, connection):
    return repr(obj)


@serialize.register(type(None))
def serialize_sentinel(obj, ql_state, connection):
    return serialize(Sentinel(), ql_state, connection)


def insert(connection, ql_state, node):
    node_type = type(node).__name__
    insertions = {}
    ql_state.from_parent = node
    for field, value in (*ast.iter_fields(node), *iter_attributes(node)):
        if value is None:
            continue
        elif field in ql_state.fields:
            insertions[field] = ql_state.fields[field]
        else:
            insertions[field] = serialize(value, ql_state, connection)

    query = as_edgeql(EdgeQLInsert(node_type, insertions))
    logger.trace("Running query: %r", query)
    return connection.query_one(query)


def insert_project_metadata(connection, instance):
    ql_state = QLState()
    project = ast.project(
        instance.name, instance.git_source, instance.git_revision
    )
    return serialize(project, ql_state, connection)


@guarded
def insert_file(connection, file, filename, project_ref):
    with tokenize.open(file) as file_p:
        source = file_p.read()

    tree = QLAst.visit(ast.parse(source))
    tree.filename = filename
    tree.project = ...

    ql_state = QLState(fields={"project": project_ref})
    with connection.transaction():
        module = insert(connection, ql_state, tree)
        module_select = EdgeQLSelect(
            name=type(tree).__name__,
            filters=make_filter(id=EdgeQLReference(module)),
            limit=1,
        )

        update_filter = EdgeQLFilter(
            EdgeQLFilterKey("id"),
            EdgeQLCall(
                "array_unpack",
                [EdgeQLCast("array<uuid>", EdgeQLVariable("ids"))],
            ),
            operator=EdgeQLComparisonOperator.CONTAINS,
        )
        for base in MODULE_ANNOTATED_TYPES:
            update = as_edgeql(
                EdgeQLUpdate(
                    base.__name__,
                    filters=update_filter,
                    assigns={"_module": module_select},
                ),
            )
            logger.trace("Running post-insert query: %r", update)
            connection.query(update, ids=ql_state.reference_pool)

    return True
