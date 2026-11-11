"""Standard library, batch 2 (v0.4.0):
Database (SQLite), Http (client + server), Request, Response, Csv, Validation, Config.

Objects such as a database connection or an HTTP response are instances of
native classes, so they can be used as types: `Database db = Database.connect()`.
Their hidden Python state lives in BarqObject.native.
Only the Python standard library is used.
"""

import csv
import datetime as dt
import http.server
import io
import json
import mimetypes
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .builtins import T, call, need
from .runtime import (BarqClass, BarqObject, BarqThrow, ExitSignal, NativeFunction, NativeModule,
                      PropInfo, to_display, type_name)
from .stdlib import DATE_FORMATS, arg, module, to_json_value

# `use Barq\Http\Client` and friends: path after `Barq\` -> global name
STDLIB_ALIASES = {
    "Http\\Client": "Http",
    "Http\\Server": "Http",
    "Http\\Request": "Request",
    "Http\\Response": "Response",
    "Db": "Database",
    "Db\\Database": "Database",
    "Db\\QueryBuilder": "QueryBuilder",
    "Db\\Schema": "Schema",
    "Db\\Blueprint": "Blueprint",
    "Http\\Route": "Route",
    "Http\\Router": "Route",
    "Http\\Session": "Session",
    "Http\\Cookie": "Cookie",
    "Xml\\XmlNode": "XmlNode",
    "Mail\\Message": "Message",
    "Test\\TestClient": "TestClient",
}

STDLIB2_EXCEPTIONS = [
    ("DbException", "Exception"),
    ("HttpException", "Exception"),
    ("CsvException", "ValueException"),
    ("ValidationException", "ValueException"),
]


# ===========================================================================
# helpers for native classes
# ===========================================================================

def native_class(i, name, props=None, methods=None, statics=None, construct=None):
    """props: {name: type string or None}; methods/statics: {name: (fn, lo, hi)}.
    Instance methods get fn(i, args, node, this); statics get fn(i, args, node)."""
    cls = BarqClass(name, name, None, native=True)
    for pname, tname in (props or {}).items():
        cls.props[pname] = PropInfo(pname, T(tname) if tname else None, "public", None, cls)
    if construct is None:
        def construct(i, a, n, this, name=name):
            i.throw("TypeException", f"{name} objects are created by the standard library, not with 'new'", n)
    table = dict(methods or {})
    table["__construct"] = (construct, 0, -1)
    for mname, (fn, lo, hi) in table.items():
        f = NativeFunction(f"{name}.{mname}" if mname != "__construct" else name, fn, lo, hi)
        f.owner = cls
        cls.methods[mname] = f
    # Static functions are stored as class constants, so a class can have both
    # Response.json(data) (static) and res.json() (instance method).
    for mname, (fn, lo, hi) in (statics or {}).items():
        f = NativeFunction(f"{name}.{mname}", fn, lo, hi)
        cls.statics[mname] = PropInfo(mname, None, "public", None, cls, is_static=True, const=True)
        cls.static_values[mname] = f
    i.classes[name] = cls
    i.builtins.declare(name, cls, None, const=True)
    return cls


def new_native(i, cls_name, state=None, **fields):
    obj = BarqObject(i.classes[cls_name])
    obj.fields.update(fields)
    obj.native = state
    return obj


def throw_with(i, cls_name, message, n, **fields):
    obj = i.make_exception(cls_name, message, n)
    obj.fields.update(fields)
    raise BarqThrow(obj)


# ===========================================================================
# Database (SQLite)
# ===========================================================================

class DbState:
    def __init__(self, conn, path):
        self.conn = conn
        self.path = path
        self.depth = 0          # transaction nesting
        self.last_id = None


IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
AS_ALIAS = re.compile(r"^\s*(.+?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", re.I)
OPERATORS = {"=", "!=", "<>", "<", ">", "<=", ">=", "like", "not like"}


def qid(i, name, n, what="column"):
    """Quote an identifier: name, table.name, table.*, * or 'x as y'."""
    if not isinstance(name, str):
        i.throw("TypeException", f"a {what} name must be a string, got {type_name(name)}", n)
    m = AS_ALIAS.match(name)
    if m:
        return f'{qid(i, m.group(1), n, what)} AS "{m.group(2)}"'
    if name == "*":
        return "*"
    parts = name.strip().split(".")
    out = []
    for k, p in enumerate(parts):
        if p == "*" and k == len(parts) - 1 and k > 0:
            out.append("*")
        elif IDENT.match(p):
            out.append(f'"{p}"')
        else:
            i.throw("DbException", f'invalid {what} name "{name}" (letters, digits and _ only)', n)
    return ".".join(out)


def db_value(i, v, n, where="value"):
    if v is None or isinstance(v, (int, float, str)):
        return int(v) if isinstance(v, bool) else v
    i.throw("DbException", f"cannot store {type_name(v)} in a database column ({where}) — use Json.encode() for lists and dicts", n)


def from_db(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def db_state(i, this, n):
    st = this.native
    if st is None or st.conn is None:
        i.throw("DbException", "the database connection is closed", n)
    return st


def db_run(i, st, sql, params, n):
    try:
        cur = st.conn.execute(sql, params)
    except sqlite3.Error as e:
        i.throw("DbException", f"{e} — in: {sql}", n)
    if cur.lastrowid:
        st.last_id = cur.lastrowid
    return cur


def db_rows(cur):
    if cur.description is None:
        return []
    names = [d[0] for d in cur.description]
    return [{k: from_db(v) for k, v in zip(names, row)} for row in cur.fetchall()]


def db_params(i, a, k, fname, n):
    params = arg(i, a, k, ("list",), fname, n, [])
    return [db_value(i, v, n, f"parameter {j + 1}") for j, v in enumerate(params)]


def resolve_connection(i, a, n):
    """Find driver + settings for Database.connect(name?, settings?)."""
    name = arg(i, a, 0, ("string", "null"), "Database.connect", n, None)
    settings = arg(i, a, 1, ("dict",), "Database.connect", n, None)
    if name is None:
        name = config_lookup(i, "database.default") or os.environ.get("DB_DRIVER") \
            or os.environ.get("DB_CONNECTION") or "sqlite"
    if settings is None:
        settings = config_lookup(i, f"database.connections.{name}")
        if not isinstance(settings, dict):
            settings = {}
    driver = settings.get("driver", name)
    return name, driver, settings


def database_connect(i, a, n):
    name, driver, settings = resolve_connection(i, a, n)
    if driver != "sqlite":
        i.throw("DbException", f'driver "{driver}" is not available in the reference interpreter yet — only "sqlite" is. '
                "MySQL, Postgres and MongoDB come with the native runtime", n)
    path = settings.get("path") or settings.get("database") or settings.get("name") \
        or os.environ.get("DB_PATH") or os.environ.get("DB_DATABASE") or "storage/database.db"
    if not isinstance(path, str):
        i.throw("TypeException", f"sqlite path must be a string, got {type_name(path)}", n)
    if path != ":memory:":
        if not os.path.isabs(path):
            path = os.path.join(i.root, path)
        folder = os.path.dirname(path)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            i.throw("DbException", f"cannot create folder {folder}: {e.strerror or e}", n)
    try:
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error as e:
        i.throw("DbException", f"cannot open sqlite database {path}: {e}", n)
    return new_native(i, "Database", DbState(conn, path), driver="sqlite", name=name)


def db_table(i, a, n, this):
    db_state(i, this, n)
    table = need(i, a[0], ("string",), "Database.table", 1, n)
    return new_query(i, this, qid(i, table, n, "table"))


def db_raw(i, a, n, this):
    st = db_state(i, this, n)
    sql = need(i, a[0], ("string",), "Database.raw", 1, n)
    return db_rows(db_run(i, st, sql, db_params(i, a, 1, "Database.raw", n), n))


def db_raw_exec(i, a, n, this):
    st = db_state(i, this, n)
    sql = need(i, a[0], ("string",), "Database.rawExec", 1, n)
    params = db_params(i, a, 1, "Database.rawExec", n)
    if not params and ";" in sql.strip().rstrip(";"):
        try:
            st.conn.executescript(sql)
        except sqlite3.Error as e:
            i.throw("DbException", f"{e} — in: {sql}", n)
        return 0
    return max(db_run(i, st, sql, params, n).rowcount, 0)


def tx_begin(i, st, n):
    db_run(i, st, "BEGIN" if st.depth == 0 else f"SAVEPOINT barq_{st.depth}", [], n)
    st.depth += 1


def tx_end(i, st, n, ok):
    if st.depth == 0:
        i.throw("DbException", "no transaction is open — call beginTransaction() first", n)
    st.depth -= 1
    if st.depth == 0:
        db_run(i, st, "COMMIT" if ok else "ROLLBACK", [], n)
    else:
        sp = f"barq_{st.depth}"
        db_run(i, st, f"RELEASE {sp}" if ok else f"ROLLBACK TO {sp}", [], n)
        if not ok:
            db_run(i, st, f"RELEASE {sp}", [], n)


def db_transaction(i, a, n, this):
    st = db_state(i, this, n)
    tx_begin(i, st, n)
    try:
        result = call(i, a[0], [], n, "Database.transaction")
    except BaseException:
        if st.conn is not None and st.depth > 0:
            tx_end(i, st, n, False)
        raise
    tx_end(i, st, n, True)
    return result


def db_tables(i, st, n):
    cur = db_run(i, st, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name", [], n)
    return [r[0] for r in cur.fetchall()]


def db_close(i, a, n, this):
    st = this.native
    if st is not None and st.conn is not None:
        st.conn.close()
        st.conn = None
    return None


DATABASE_METHODS = {
    "table": (db_table, 1, 1),
    "raw": (db_raw, 1, 2),
    "rawExec": (db_raw_exec, 1, 2),
    "transaction": (db_transaction, 1, 1),
    "beginTransaction": (lambda i, a, n, this: tx_begin(i, db_state(i, this, n), n), 0, 0),
    "commit": (lambda i, a, n, this: tx_end(i, db_state(i, this, n), n, True), 0, 0),
    "rollback": (lambda i, a, n, this: tx_end(i, db_state(i, this, n), n, False), 0, 0),
    "inTransaction": (lambda i, a, n, this: db_state(i, this, n).depth > 0, 0, 0),
    "lastInsertId": (lambda i, a, n, this: db_state(i, this, n).last_id, 0, 0),
    "tables": (lambda i, a, n, this: db_tables(i, db_state(i, this, n), n), 0, 0),
    "hasTable": (lambda i, a, n, this: need(i, a[0], ("string",), "Database.hasTable", 1, n)
                 in db_tables(i, db_state(i, this, n), n), 1, 1),
    "schema": (lambda i, a, n, this: (db_state(i, this, n), new_native(i, "Schema", this))[1], 0, 0),
    "close": (db_close, 0, 0),
}


# ---- query builder ---------------------------------------------------------

class Query:
    def __init__(self, db, table):
        self.db = db                # Database BarqObject
        self.table = table          # quoted
        self.columns = []           # quoted
        self.distinct = False
        self.wheres = []            # (bool_op, sql, params)
        self.joins = []             # (sql)
        self.groups = []
        self.orders = []
        self.limit = None
        self.offset = None


def new_query(i, db, table):
    return new_native(i, "QueryBuilder", Query(db, table))


def where_sql(q):
    if not q.wheres:
        return "", []
    parts, params = [], []
    for k, (op, sql, ps) in enumerate(q.wheres):
        parts.append(sql if k == 0 else f"{op} {sql}")
        params.extend(ps)
    return " WHERE " + " ".join(parts), params


def from_sql(q):
    return f" FROM {q.table}" + "".join(q.joins)


def select_sql(q, columns=None, with_order=True):
    cols = columns or (", ".join(q.columns) if q.columns else "*")
    where, params = where_sql(q)
    sql = f"SELECT {'DISTINCT ' if q.distinct else ''}{cols}{from_sql(q)}{where}"
    if q.groups:
        sql += " GROUP BY " + ", ".join(q.groups)
    if with_order:
        if q.orders:
            sql += " ORDER BY " + ", ".join(q.orders)
        if q.limit is not None or q.offset is not None:
            sql += " LIMIT ?"
            params.append(q.limit if q.limit is not None else -1)
            if q.offset is not None:
                sql += " OFFSET ?"
                params.append(q.offset)
    return sql, params


def qb(this):
    return this.native


def qb_run(i, this, sql, params, n):
    q = qb(this)
    return db_run(i, db_state(i, q.db, n), sql, params, n)


def check_op(i, op, n):
    if not isinstance(op, str) or op.lower() not in OPERATORS:
        i.throw("DbException", f"unknown operator {to_display(op, i, True)} (use {', '.join(sorted(OPERATORS))})", n)
    return op.upper() if op.lower() in ("like", "not like") else op


def add_where(i, a, n, this, bool_op, fname):
    q = qb(this)
    first = a[0]
    if type_name(first) == "karo":
        sub = new_query(i, q.db, q.table)
        call(i, first, [sub], n, fname)
        sq = qb(sub)
        if sq.wheres:
            sql, params = where_sql(sq)
            q.wheres.append((bool_op, f"({sql[7:]})", params))
        return this
    if isinstance(first, dict):
        if len(a) != 1:
            i.throw("TypeException", f"{fname}(dict) takes just the dict", n)
        for k, v in first.items():
            add_where(i, [k, v], n, this, "AND" if q.wheres or bool_op == "AND" else bool_op, fname)
        return this
    col = qid(i, need(i, first, ("string",), fname, 1, n), n)
    if len(a) == 2:
        op, value = "=", a[1]
    elif len(a) == 3:
        op, value = check_op(i, a[1], n), a[2]
    else:
        i.throw("TypeException", f"{fname}() expects (column, value) or (column, operator, value)", n)
    if value is None:
        if op in ("=", "!=", "<>"):
            q.wheres.append((bool_op, f"{col} IS {'NOT ' if op != '=' else ''}NULL", []))
            return this
        i.throw("DbException", f"cannot compare with null using '{op}'", n)
    q.wheres.append((bool_op, f"{col} {op} ?", [db_value(i, value, n)]))
    return this


def where_in(negate, bool_op="AND"):
    def fn(i, a, n, this):
        fname = f"QueryBuilder.{'or' if bool_op == 'OR' else ''}{'whereNotIn' if negate else 'whereIn'}"
        col = qid(i, need(i, a[0], ("string",), fname, 1, n), n)
        values = need(i, a[1], ("list", "set"), fname, 2, n)
        values = values if isinstance(values, list) else values.values()
        if not values:
            qb(this).wheres.append((bool_op, "1 = 1" if negate else "0 = 1", []))
        else:
            marks = ", ".join("?" for _ in values)
            qb(this).wheres.append((bool_op, f"{col} {'NOT ' if negate else ''}IN ({marks})",
                                    [db_value(i, v, n) for v in values]))
        return this
    return fn


def where_null(negate, bool_op="AND"):
    def fn(i, a, n, this):
        col = qid(i, need(i, a[0], ("string",), "QueryBuilder.whereNull", 1, n), n)
        qb(this).wheres.append((bool_op, f"{col} IS {'NOT ' if negate else ''}NULL", []))
        return this
    return fn


def where_between(i, a, n, this):
    col = qid(i, need(i, a[0], ("string",), "QueryBuilder.whereBetween", 1, n), n)
    pair = need(i, a[1], ("list",), "QueryBuilder.whereBetween", 2, n)
    if len(pair) != 2:
        i.throw("ValueException", "QueryBuilder.whereBetween() needs a list of 2 values, e.g. #[18, 65]", n)
    qb(this).wheres.append(("AND", f"{col} BETWEEN ? AND ?", [db_value(i, v, n) for v in pair]))
    return this


def qb_select(i, a, n, this):
    cols = a[0] if len(a) == 1 and isinstance(a[0], list) else a
    qb(this).columns = [qid(i, c, n) for c in cols]
    return this


def qb_distinct(i, a, n, this):
    q = qb(this)
    q.distinct = True
    if a:
        qb_select(i, a, n, this)
    return this


def qb_join(kind):
    def fn(i, a, n, this):
        fname = f"QueryBuilder.{'leftJoin' if kind == 'LEFT' else 'join'}"
        if len(a) == 3:
            table, first, op, second = a[0], a[1], "=", a[2]
        else:
            table, first, op, second = a
        op = check_op(i, op, n)
        need(i, table, ("string",), fname, 1, n)
        qb(this).joins.append(f" {kind} JOIN {qid(i, table, n, 'table')} ON {qid(i, first, n)} {op} {qid(i, second, n)}")
        return this
    return fn


def qb_order(i, a, n, this):
    col = qid(i, need(i, a[0], ("string",), "QueryBuilder.orderBy", 1, n), n)
    direction = arg(i, a, 1, ("string",), "QueryBuilder.orderBy", n, "asc").lower()
    if direction not in ("asc", "desc"):
        i.throw("DbException", f'orderBy direction must be "asc" or "desc", got "{direction}"', n)
    qb(this).orders.append(f"{col} {direction.upper()}")
    return this


def qb_set(field, fname):
    def fn(i, a, n, this):
        v = need(i, a[0], ("int",), fname, 1, n)
        if v < 0:
            i.throw("ValueException", f"{fname}() cannot be negative", n)
        setattr(qb(this), field, v)
        return this
    return fn


def qb_group(i, a, n, this):
    cols = a[0] if len(a) == 1 and isinstance(a[0], list) else a
    qb(this).groups.extend(qid(i, c, n) for c in cols)
    return this


def qb_get(i, a, n, this):
    sql, params = select_sql(qb(this))
    return db_rows(qb_run(i, this, sql, params, n))


def qb_first(i, a, n, this):
    q = qb(this)
    saved = q.limit
    q.limit = 1
    try:
        sql, params = select_sql(q)
    finally:
        q.limit = saved
    rows = db_rows(qb_run(i, this, sql, params, n))
    return rows[0] if rows else None


def qb_find(i, a, n, this):
    col = arg(i, a, 1, ("string",), "QueryBuilder.find", n, "id")
    add_where(i, [col, a[0]], n, this, "AND", "QueryBuilder.find")
    return qb_first(i, [], n, this)


def qb_value(i, a, n, this):
    col = need(i, a[0], ("string",), "QueryBuilder.value", 1, n)
    qb_select(i, [col], n, this)
    row = qb_first(i, [], n, this)
    return None if row is None else next(iter(row.values()))


def qb_pluck(i, a, n, this):
    col = need(i, a[0], ("string",), "QueryBuilder.pluck", 1, n)
    key = arg(i, a, 1, ("string",), "QueryBuilder.pluck", n, None)
    qb_select(i, [col] + ([key] if key else []), n, this)
    sql, params = select_sql(qb(this))
    rows = qb_run(i, this, sql, params, n).fetchall()
    if key:
        return {from_db(r[1]): from_db(r[0]) for r in rows}
    return [from_db(r[0]) for r in rows]


def aggregate(func):
    def fn(i, a, n, this):
        q = qb(this)
        col = "*" if func == "COUNT" and not a else qid(i, need(i, a[0], ("string",), f"QueryBuilder.{func.lower()}", 1, n), n)
        if q.distinct or q.groups or q.limit is not None or q.offset is not None:
            inner, params = select_sql(q)
            sql = f"SELECT {func}({'*' if func == 'COUNT' else col}) FROM ({inner})"
        else:
            where, params = where_sql(q)
            sql = f"SELECT {func}({col}){from_sql(q)}{where}"
        value = qb_run(i, this, sql, params, n).fetchone()[0]
        if func == "COUNT":
            return value or 0
        if func == "AVG" and value is not None:
            return float(value)
        return value
    return fn


def qb_exists(i, a, n, this):
    inner, params = select_sql(qb(this))
    return bool(qb_run(i, this, f"SELECT EXISTS({inner})", params, n).fetchone()[0])


def qb_paginate(i, a, n, this):
    page = need(i, a[0], ("int",), "QueryBuilder.paginate", 1, n)
    per = arg(i, a, 1, ("int",), "QueryBuilder.paginate", n, 15)
    if page < 1 or per < 1:
        i.throw("ValueException", "QueryBuilder.paginate() needs page >= 1 and perPage >= 1", n)
    total = aggregate("COUNT")(i, [], n, this)
    q = qb(this)
    q.limit, q.offset = per, (page - 1) * per
    data = qb_get(i, [], n, this)
    return {"data": data, "total": total, "page": page, "perPage": per,
            "lastPage": max(1, -(-total // per))}


def insert_rows(i, this, rows, n, fname):
    q = qb(this)
    if not rows:
        return 0
    for r in rows:
        if not isinstance(r, dict) or not r:
            i.throw("TypeException", f"{fname}() needs non-empty dicts of column: value", n)
    keys = list(rows[0].keys())
    for r in rows[1:]:
        if list(r.keys()) != keys and set(r.keys()) != set(keys):
            i.throw("DbException", f"{fname}(): every row must have the same columns", n)
    cols = ", ".join(qid(i, k, n) for k in keys)
    marks = ", ".join("?" for _ in keys)
    sql = f"INSERT INTO {q.table} ({cols}) VALUES ({marks})"
    st = db_state(i, q.db, n)
    data = [[db_value(i, r[k], n, f"column {k}") for k in keys] for r in rows]
    if len(data) == 1:
        db_run(i, st, sql, data[0], n)
        return 1
    try:
        st.conn.executemany(sql, data)
    except sqlite3.Error as e:
        i.throw("DbException", f"{e} — in: {sql}", n)
    return len(data)


def qb_insert(i, a, n, this):
    v = need(i, a[0], ("dict", "list"), "QueryBuilder.insert", 1, n)
    return insert_rows(i, this, v if isinstance(v, list) else [v], n, "QueryBuilder.insert")


def qb_insert_get_id(i, a, n, this):
    v = need(i, a[0], ("dict",), "QueryBuilder.insertGetId", 1, n)
    insert_rows(i, this, [v], n, "QueryBuilder.insertGetId")
    return db_state(i, qb(this).db, n).last_id


def no_joins(i, q, n, what):
    if q.joins or q.limit is not None or q.orders:
        i.throw("DbException", f"{what}() cannot be combined with join/limit/orderBy on SQLite", n)


def qb_update(i, a, n, this):
    q = qb(this)
    values = need(i, a[0], ("dict",), "QueryBuilder.update", 1, n)
    if not values:
        return 0
    no_joins(i, q, n, "update")
    sets = ", ".join(f"{qid(i, k, n)} = ?" for k in values)
    where, wparams = where_sql(q)
    params = [db_value(i, v, n, f"column {k}") for k, v in values.items()] + wparams
    return qb_run(i, this, f"UPDATE {q.table} SET {sets}{where}", params, n).rowcount


def qb_step(sign):
    def fn(i, a, n, this):
        fname = "QueryBuilder.increment" if sign > 0 else "QueryBuilder.decrement"
        q = qb(this)
        no_joins(i, q, n, fname[13:])
        col = qid(i, need(i, a[0], ("string",), fname, 1, n), n)
        by = arg(i, a, 1, ("int", "float"), fname, n, 1)
        extra = arg(i, a, 2, ("dict",), fname, n, {})
        sets = [f"{col} = {col} {'+' if sign > 0 else '-'} ?"] + [f"{qid(i, k, n)} = ?" for k in extra]
        where, wparams = where_sql(q)
        params = [by] + [db_value(i, v, n) for v in extra.values()] + wparams
        return qb_run(i, this, f"UPDATE {q.table} SET {', '.join(sets)}{where}", params, n).rowcount
    return fn


def qb_delete(i, a, n, this):
    q = qb(this)
    no_joins(i, q, n, "delete")
    where, params = where_sql(q)
    return qb_run(i, this, f"DELETE FROM {q.table}{where}", params, n).rowcount


def qb_to_sql(i, a, n, this):
    return select_sql(qb(this))[0]


def qb_bindings(i, a, n, this):
    return select_sql(qb(this))[1]


QUERY_METHODS = {
    "select": (qb_select, 1, -1),
    "distinct": (qb_distinct, 0, -1),
    "where": (lambda i, a, n, this: add_where(i, a, n, this, "AND", "QueryBuilder.where"), 1, 3),
    "orWhere": (lambda i, a, n, this: add_where(i, a, n, this, "OR", "QueryBuilder.orWhere"), 1, 3),
    "whereIn": (where_in(False), 2, 2),
    "whereNotIn": (where_in(True), 2, 2),
    "orWhereIn": (where_in(False, "OR"), 2, 2),
    "whereNull": (where_null(False), 1, 1),
    "whereNotNull": (where_null(True), 1, 1),
    "orWhereNull": (where_null(False, "OR"), 1, 1),
    "whereBetween": (where_between, 2, 2),
    "join": (qb_join("INNER"), 3, 4),
    "leftJoin": (qb_join("LEFT"), 3, 4),
    "orderBy": (qb_order, 1, 2),
    "orderByDesc": (lambda i, a, n, this: qb_order(i, [a[0], "desc"], n, this), 1, 1),
    "latest": (lambda i, a, n, this: qb_order(i, [a[0] if a else "created_at", "desc"], n, this), 0, 1),
    "oldest": (lambda i, a, n, this: qb_order(i, [a[0] if a else "created_at", "asc"], n, this), 0, 1),
    "groupBy": (qb_group, 1, -1),
    "limit": (qb_set("limit", "QueryBuilder.limit"), 1, 1),
    "take": (qb_set("limit", "QueryBuilder.take"), 1, 1),
    "offset": (qb_set("offset", "QueryBuilder.offset"), 1, 1),
    "skip": (qb_set("offset", "QueryBuilder.skip"), 1, 1),
    "get": (qb_get, 0, 0),
    "first": (qb_first, 0, 0),
    "find": (qb_find, 1, 2),
    "value": (qb_value, 1, 1),
    "pluck": (qb_pluck, 1, 2),
    "count": (aggregate("COUNT"), 0, 1),
    "sum": (aggregate("SUM"), 1, 1),
    "avg": (aggregate("AVG"), 1, 1),
    "min": (aggregate("MIN"), 1, 1),
    "max": (aggregate("MAX"), 1, 1),
    "exists": (qb_exists, 0, 0),
    "paginate": (qb_paginate, 1, 2),
    "insert": (qb_insert, 1, 1),
    "insertMany": (lambda i, a, n, this: insert_rows(i, this, need(i, a[0], ("list",), "QueryBuilder.insertMany", 1, n),
                                                     n, "QueryBuilder.insertMany"), 1, 1),
    "insertGetId": (qb_insert_get_id, 1, 1),
    "update": (qb_update, 1, 1),
    "increment": (qb_step(1), 1, 3),
    "decrement": (qb_step(-1), 1, 3),
    "delete": (qb_delete, 0, 0),
    "toSql": (qb_to_sql, 0, 0),
    "bindings": (qb_bindings, 0, 0),
}


# ---- schema builder ---------------------------------------------------------

class ColumnSpec:
    def __init__(self, name, sql_type):
        self.name = name
        self.type = sql_type
        self.nullable = False
        self.default = None
        self.has_default = False
        self.unique = False
        self.primary = False
        self.index = False
        self.extra = ""             # e.g. PRIMARY KEY AUTOINCREMENT
        self.ref = None             # (table, column)
        self.on_delete = None

    def sql(self, i, n):
        out = f'"{self.name}" {self.type}{self.extra}'
        if self.primary and not self.extra:
            out += " PRIMARY KEY"
        if not self.nullable and not self.extra:
            out += " NOT NULL"
        if self.unique:
            out += " UNIQUE"
        if self.has_default:
            out += " DEFAULT " + sql_literal(i, self.default, n)
        if self.ref:
            out += f' REFERENCES "{self.ref[0]}"("{self.ref[1]}")'
            if self.on_delete:
                out += f" ON DELETE {self.on_delete}"
        return out


def sql_literal(i, v, n):
    v = db_value(i, v, n, "default")
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(v)


class BlueprintState:
    def __init__(self, table, altering):
        self.table = table
        self.altering = altering
        self.columns = []           # ColumnSpec
        self.after = []             # extra statements (indexes, drops, renames)


def plain_ident(i, name, n, what="column"):
    if not isinstance(name, str) or not IDENT.match(name):
        i.throw("DbException", f"invalid {what} name {to_display(name, i, True)} (letters, digits and _ only)", n)
    return name


def col_maker(sql_type, default_name=None, extra="", nullable=False):
    def fn(i, a, n, this):
        name = a[0] if a else default_name
        name = plain_ident(i, name, n)
        t = sql_type
        if sql_type == "VARCHAR":
            length = arg(i, a, 1, ("int",), "Blueprint.string", n, 255)
            t = f"VARCHAR({length})"
        spec = ColumnSpec(name, t)
        spec.extra = extra
        spec.nullable = nullable
        this.native.columns.append(spec)
        return new_native(i, "ColumnDef", spec)
    return fn


def bp_morphs(i, a, n, this):
    """t.morphs("tokenable"): tokenable_type (string) + tokenable_id (int), indexed together."""
    name = plain_ident(i, need(i, a[0], ("string",), "Blueprint.morphs", 1, n), n)
    for col, t in ((name + "_type", "VARCHAR(255)"), (name + "_id", "INTEGER")):
        this.native.columns.append(ColumnSpec(col, t))
    bp_index(False)(i, [[name + "_type", name + "_id"]], n, this)
    return None


def bp_timestamps(i, a, n, this):
    for name in ("created_at", "updated_at"):
        spec = ColumnSpec(name, "INTEGER")
        spec.nullable = True
        this.native.columns.append(spec)
    return None


def bp_index(unique):
    def fn(i, a, n, this):
        cols = a[0] if isinstance(a[0], list) else [a[0]]
        cols = [plain_ident(i, c, n) for c in cols]
        st = this.native
        name = f"{st.table}_{'_'.join(cols)}_{'unique' if unique else 'index'}"
        quoted = ", ".join(f'"{c}"' for c in cols)
        st.after.append(f'CREATE {"UNIQUE " if unique else ""}INDEX "{name}" ON "{st.table}" ({quoted})')
        return None
    return fn


def bp_alter_only(what, build):
    def fn(i, a, n, this):
        st = this.native
        if not st.altering:
            i.throw("DbException", f"Blueprint.{what}() only works inside schema.alter()", n)
        st.after.append(build(i, a, n, st))
        return None
    return fn


def drop_column_sql(i, a, n, st):
    return f'ALTER TABLE "{st.table}" DROP COLUMN "{plain_ident(i, a[0], n)}"'


def rename_column_sql(i, a, n, st):
    return f'ALTER TABLE "{st.table}" RENAME COLUMN "{plain_ident(i, a[0], n)}" TO "{plain_ident(i, a[1], n)}"'


BLUEPRINT_METHODS = {
    "id": (col_maker("INTEGER", "id", " PRIMARY KEY AUTOINCREMENT"), 0, 1),
    "increments": (col_maker("INTEGER", None, " PRIMARY KEY AUTOINCREMENT"), 1, 1),
    "string": (col_maker("VARCHAR"), 1, 2),
    "text": (col_maker("TEXT"), 1, 1),
    "int": (col_maker("INTEGER"), 1, 1),
    "integer": (col_maker("INTEGER"), 1, 1),
    "bigInt": (col_maker("INTEGER"), 1, 1),
    "foreignId": (col_maker("INTEGER"), 1, 1),
    "float": (col_maker("REAL"), 1, 1),
    "decimal": (col_maker("NUMERIC"), 1, 1),
    "bool": (col_maker("INTEGER"), 1, 1),
    "boolean": (col_maker("INTEGER"), 1, 1),
    "json": (col_maker("TEXT"), 1, 1),
    "uuid": (col_maker("TEXT"), 1, 1),
    "date": (col_maker("INTEGER"), 1, 1),
    "datetime": (col_maker("INTEGER"), 1, 1),
    "timestamp": (col_maker("INTEGER"), 1, 1),
    "timestamps": (bp_timestamps, 0, 0),
    "softDeletes": (col_maker("INTEGER", "deleted_at", nullable=True), 0, 0),
    "rememberToken": (col_maker("VARCHAR", "remember_token", nullable=True), 0, 0),
    "morphs": (bp_morphs, 1, 1),
    "unique": (bp_index(True), 1, 1),
    "index": (bp_index(False), 1, 1),
    "dropColumn": (bp_alter_only("dropColumn", drop_column_sql), 1, 1),
    "renameColumn": (bp_alter_only("renameColumn", rename_column_sql), 2, 2),
}


def cd_flag(field, value=True):
    def fn(i, a, n, this):
        setattr(this.native, field, a[0] if a else value)
        return this
    return fn


def cd_default(i, a, n, this):
    db_value(i, a[0], n, "default")
    this.native.default = a[0]
    this.native.has_default = True
    return this


def cd_references(i, a, n, this):
    spec = this.native
    col = plain_ident(i, need(i, a[0], ("string",), "ColumnDef.references", 1, n), n)
    spec.ref = (spec.ref[0] if spec.ref else "", col)
    return this


def cd_on(i, a, n, this):
    spec = this.native
    table = plain_ident(i, need(i, a[0], ("string",), "ColumnDef.on", 1, n), n, "table")
    spec.ref = (table, spec.ref[1] if spec.ref else "id")
    return this


def cd_constrained(i, a, n, this):
    spec = this.native
    table = arg(i, a, 0, ("string",), "ColumnDef.constrained", n, None)
    if table is None:
        if not spec.name.endswith("_id"):
            i.throw("DbException", f"cannot guess the table for '{spec.name}' — pass it: constrained(\"users\")", n)
        table = spec.name[:-3] + "s"
    spec.ref = (plain_ident(i, table, n, "table"), arg(i, a, 1, ("string",), "ColumnDef.constrained", n, "id"))
    return this


def cd_on_delete(i, a, n, this):
    action = need(i, a[0], ("string",), "ColumnDef.onDelete", 1, n).lower()
    actions = {"cascade": "CASCADE", "restrict": "RESTRICT", "set null": "SET NULL", "null": "SET NULL",
               "no action": "NO ACTION"}
    if action not in actions:
        i.throw("DbException", f'onDelete must be one of: {", ".join(actions)}', n)
    this.native.on_delete = actions[action]
    return this


COLUMN_METHODS = {
    "nullable": (lambda i, a, n, this: cd_flag("nullable")(i, [need(i, a[0], ("bool",), "ColumnDef.nullable", 1, n)] if a else [], n, this), 0, 1),
    "default": (cd_default, 1, 1),
    "unique": (cd_flag("unique"), 0, 0),
    "primary": (cd_flag("primary"), 0, 0),
    "index": (cd_flag("index"), 0, 0),
    "unsigned": (lambda i, a, n, this: this, 0, 0),
    "references": (cd_references, 1, 1),
    "on": (cd_on, 1, 1),
    "constrained": (cd_constrained, 0, 2),
    "onDelete": (cd_on_delete, 1, 1),
}


def schema_db(i, this, n):
    return db_state(i, this.native, n)


def schema_build(i, a, n, this, altering):
    fname = "Schema.alter" if altering else "Schema.create"
    st = schema_db(i, this, n)
    table = plain_ident(i, need(i, a[0], ("string",), fname, 1, n), n, "table")
    bp = BlueprintState(table, altering)
    call(i, a[1], [new_native(i, "Blueprint", bp)], n, fname)
    stmts = []
    if altering:
        for c in bp.columns:
            if not c.nullable and not c.has_default and not c.extra:
                i.throw("DbException", f"new column '{c.name}' needs .nullable() or .default(...) because the table may already have rows", n)
            stmts.append(f'ALTER TABLE "{table}" ADD COLUMN {c.sql(i, n)}')
    else:
        if not bp.columns:
            i.throw("DbException", f"Schema.create(\"{table}\") defines no columns", n)
        if_not = "IF NOT EXISTS " if (len(a) > 2 and a[2] is True) else ""
        stmts.append(f'CREATE TABLE {if_not}"{table}" (' + ", ".join(c.sql(i, n) for c in bp.columns) + ")")
    for c in bp.columns:
        if c.index:
            stmts.append(f'CREATE INDEX "{table}_{c.name}_index" ON "{table}" ("{c.name}")')
    stmts.extend(bp.after)
    tx_begin(i, st, n)
    try:
        for sql in stmts:
            db_run(i, st, sql, [], n)
    except BaseException:
        tx_end(i, st, n, False)
        raise
    tx_end(i, st, n, True)
    return None


def schema_drop(if_exists):
    def fn(i, a, n, this):
        table = plain_ident(i, need(i, a[0], ("string",), "Schema.drop", 1, n), n, "table")
        db_run(i, schema_db(i, this, n), f'DROP TABLE {"IF EXISTS " if if_exists else ""}"{table}"', [], n)
        return None
    return fn


def schema_columns(i, a, n, this):
    table = plain_ident(i, need(i, a[0], ("string",), "Schema.columns", 1, n), n, "table")
    cur = db_run(i, schema_db(i, this, n), f'PRAGMA table_info("{table}")', [], n)
    return [r[1] for r in cur.fetchall()]


def schema_rename(i, a, n, this):
    old = plain_ident(i, need(i, a[0], ("string",), "Schema.rename", 1, n), n, "table")
    new = plain_ident(i, need(i, a[1], ("string",), "Schema.rename", 2, n), n, "table")
    db_run(i, schema_db(i, this, n), f'ALTER TABLE "{old}" RENAME TO "{new}"', [], n)
    return None


SCHEMA_METHODS = {
    "create": (lambda i, a, n, this: schema_build(i, a, n, this, False), 2, 2),
    "createIfNotExists": (lambda i, a, n, this: schema_build(i, [a[0], a[1], True], n, this, False), 2, 2),
    "alter": (lambda i, a, n, this: schema_build(i, a, n, this, True), 2, 2),
    "table": (lambda i, a, n, this: schema_build(i, a, n, this, True), 2, 2),
    "drop": (schema_drop(False), 1, 1),
    "dropIfExists": (schema_drop(True), 1, 1),
    "rename": (schema_rename, 2, 2),
    "hasTable": (lambda i, a, n, this: need(i, a[0], ("string",), "Schema.hasTable", 1, n)
                 in db_tables(i, schema_db(i, this, n), n), 1, 1),
    "columns": (schema_columns, 1, 1),
    "hasColumn": (lambda i, a, n, this: need(i, a[1], ("string",), "Schema.hasColumn", 2, n)
                  in schema_columns(i, a[:1], n, this), 2, 2),
}


# ===========================================================================
# Http — Response, Request, client, server
# ===========================================================================

STATUS_TEXT = {code: msg for code, (msg, _) in http.server.BaseHTTPRequestHandler.responses.items()}


def make_response(i, body="", status=200, headers=None):
    return new_native(i, "Response", None, status=status, body=body, headers=dict(headers or {}))


def header_lookup(headers, name, default=None):
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v
    return default


def check_status(i, status, n):
    if not 100 <= status <= 599:
        i.throw("ValueException", f"HTTP status must be 100-599, got {status}", n)
    return status


def check_headers(i, headers, n, fname):
    for k, v in headers.items():
        if not isinstance(k, str) or not isinstance(v, str):
            i.throw("TypeException", f"{fname}(): headers must be dict<string, string>", n)
        if "\n" in k or "\r" in k or "\n" in v or "\r" in v:
            i.throw("ValueException", f"{fname}(): header {k!r} contains a line break", n)
    return headers


def resp_make(i, a, n):
    body = arg(i, a, 0, ("string",), "Response.make", n, "")
    status = check_status(i, arg(i, a, 1, ("int",), "Response.make", n, 200), n)
    headers = check_headers(i, arg(i, a, 2, ("dict",), "Response.make", n, {}), n, "Response.make")
    if header_lookup(headers, "Content-Type") is None:
        headers = {"Content-Type": "text/html; charset=utf-8", **headers}
    return make_response(i, body, status, headers)


def resp_typed(ctype, fname):
    def fn(i, a, n):
        body = need(i, a[0], ("string",), fname, 1, n)
        status = check_status(i, arg(i, a, 1, ("int",), fname, n, 200), n)
        return make_response(i, body, status, {"Content-Type": ctype})
    return fn


def resp_json(i, a, n):
    status = check_status(i, arg(i, a, 1, ("int",), "Response.json", n, 200), n)
    body = json.dumps(to_json_value(i, a[0], n), ensure_ascii=False, separators=(",", ":"))
    return make_response(i, body, status, {"Content-Type": "application/json"})


def resp_redirect(i, a, n):
    url = need(i, a[0], ("string",), "Response.redirect", 1, n)
    status = arg(i, a, 1, ("int",), "Response.redirect", n, 302)
    if status not in (301, 302, 303, 307, 308):
        i.throw("ValueException", f"Response.redirect() status must be 301, 302, 303, 307 or 308, got {status}", n)
    check_headers(i, {"Location": url}, n, "Response.redirect")
    return make_response(i, "", status, {"Location": url})


def resp_construct(i, a, n, this):
    r = resp_make(i, a, n)
    this.fields.update(r.fields)
    return None


def resp_decode_json(i, a, n, this):
    try:
        return json.loads(this.fields["body"])
    except ValueError as e:
        i.throw("JsonException", f"response body is not valid JSON: {e}", n)


def resp_with_header(i, a, n, this):
    name = need(i, a[0], ("string",), "Response.withHeader", 1, n)
    value = need(i, a[1], ("string",), "Response.withHeader", 2, n)
    check_headers(i, {name: value}, n, "Response.withHeader")
    this.fields["headers"][name] = value
    return this


def resp_with_status(i, a, n, this):
    this.fields["status"] = check_status(i, need(i, a[0], ("int",), "Response.withStatus", 1, n), n)
    return this


RESPONSE_METHODS = {
    "json": (resp_decode_json, 0, 0),
    "ok": (lambda i, a, n, this: 200 <= this.fields["status"] < 300, 0, 0),
    "failed": (lambda i, a, n, this: this.fields["status"] >= 400, 0, 0),
    "header": (lambda i, a, n, this: header_lookup(this.fields["headers"], need(i, a[0], ("string",), "Response.header", 1, n),
                                                   a[1] if len(a) > 1 else None), 1, 2),
    "withHeader": (resp_with_header, 2, 2),
    "withStatus": (resp_with_status, 1, 1),
}

RESPONSE_STATICS = {
    "make": (resp_make, 0, 3),
    "json": (resp_json, 1, 2),
    "html": (resp_typed("text/html; charset=utf-8", "Response.html"), 1, 2),
    "text": (resp_typed("text/plain; charset=utf-8", "Response.text"), 1, 2),
    "redirect": (resp_redirect, 1, 2),
    "noContent": (lambda i, a, n: make_response(i, "", 204), 0, 0),
}


# ---- Request ----------------------------------------------------------------

class RequestState:
    def __init__(self, query, form, ip):
        self.query = query
        self.form = form        # dict from urlencoded / JSON body, or None
        self.ip = ip
        self.params = {}        # route parameters, filled by the router (web.py)
        self.route = None       # the matched route
        self.session = None     # Session object, created on first req.session()
        self.cookies = None     # parsed Cookie header, on first use
        self.queued = []        # Set-Cookie headers added outside the response (remember-me)
        self.auth = {}          # guard name -> user (None: guest), filled on first use (barq-auth)
        self.guard = None       # default guard for this request (set by the auth:<guard> middleware)
        self.skip_csrf = False  # TestClient requests skip the "csrf" middleware unless withCsrfChecks()
        self.files = {}         # name -> UploadedFile (or a list for name[]), from multipart bodies (upload.py)


def req_json(i, a, n, this):
    body = this.fields["body"]
    if body == "":
        return None
    try:
        return json.loads(body)
    except ValueError as e:
        i.throw("JsonException", f"request body is not valid JSON: {e}", n)


def req_all(i, a, n, this):
    st = this.native
    out = dict(st.query)
    if isinstance(st.form, dict):
        out.update(st.form)
    out.update(st.files)
    out.pop("_token", None)     # the CSRF field and method spoofing are not input
    out.pop("_method", None)
    return out


def req_input(i, a, n, this):
    key = need(i, a[0], ("string",), "Request.input", 1, n)
    data = req_all(i, [], n, this)
    return data.get(key, a[1] if len(a) > 1 else None)


def req_query(i, a, n, this):
    q = this.native.query
    if not a:
        return dict(q)
    return q.get(need(i, a[0], ("string",), "Request.query", 1, n), a[1] if len(a) > 1 else None)


def req_only(i, a, n, this):
    keys = need(i, a[0], ("list",), "Request.only", 1, n)
    data = req_all(i, [], n, this)
    return {k: data[k] for k in keys if k in data}


REQUEST_METHODS = {
    "all": (req_all, 0, 0),
    "input": (req_input, 1, 2),
    "query": (req_query, 0, 2),
    "only": (req_only, 1, 1),
    "has": (lambda i, a, n, this: need(i, a[0], ("string",), "Request.has", 1, n) in req_all(i, [], n, this), 1, 1),
    "json": (req_json, 0, 0),
    "header": (lambda i, a, n, this: header_lookup(this.fields["headers"], need(i, a[0], ("string",), "Request.header", 1, n),
                                                   a[1] if len(a) > 1 else None), 1, 2),
    "ip": (lambda i, a, n, this: this.native.ip, 0, 0),
    "isJson": (lambda i, a, n, this: "json" in (header_lookup(this.fields["headers"], "Content-Type") or ""), 0, 0),
}


def flat_query(qs):
    return {k: v[-1] for k, v in urllib.parse.parse_qs(qs, keep_blank_values=True).items()}


# ---- client -----------------------------------------------------------------

def http_options(i, opts, n, fname):
    for k in opts:
        if k not in ("headers", "timeout", "query", "form", "token", "json", "body"):
            i.throw("ValueException", f'{fname}(): unknown option "{k}" (use headers, timeout, query, form, token)', n)
    return opts


def http_send(i, method, url, data, opts, n, fname):
    if not re.match(r"^https?://", url):
        i.throw("HttpException", f'{fname}(): URL must start with http:// or https://, got "{url}"', n)
    headers = check_headers(i, dict(opts.get("headers") or {}), n, fname)
    query = opts.get("query")
    if query:
        need(i, query, ("dict",), fname + " option query", 1, n)
        sep = "&" if "?" in url else "?"
        url += sep + urllib.parse.urlencode({k: to_display(v) for k, v in query.items()})
    if opts.get("token"):
        headers.setdefault("Authorization", "Bearer " + need(i, opts["token"], ("string",), fname + " option token", 1, n))
    body = None
    if data is not None:
        if isinstance(data, str):
            body = data.encode("utf-8")
            if header_lookup(headers, "Content-Type") is None:
                headers["Content-Type"] = "text/plain; charset=utf-8"
        elif opts.get("form"):
            need(i, data, ("dict",), fname + " (form)", 2, n)
            body = urllib.parse.urlencode({k: to_display(v) for k, v in data.items()}).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = json.dumps(to_json_value(i, data, n), ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
    headers.setdefault("User-Agent", "Barq-Http/0.6")
    headers.setdefault("Accept", "application/json, text/html;q=0.9, */*;q=0.8")
    timeout = opts.get("timeout", 30)
    need(i, timeout, ("int", "float"), fname + " option timeout", 1, n)
    if i.__dict__.get("_http_fake") is not None:
        return fake_send(i, method, url, headers, body, n, fname)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            status, raw, hdrs = res.status, res.read(), res.headers
    except urllib.error.HTTPError as e:
        status, raw, hdrs = e.code, e.read(), e.headers
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, socket.timeout):
            i.throw("HttpException", f"{method} {url} timed out after {timeout}s", n)
        i.throw("HttpException", f"{method} {url} failed: {reason}", n)
    except (socket.timeout, TimeoutError):
        i.throw("HttpException", f"{method} {url} timed out after {timeout}s", n)
    except (OSError, ValueError) as e:
        i.throw("HttpException", f"{method} {url} failed: {e}", n)
    charset = hdrs.get_content_charset()
    ctype = (hdrs.get_content_type() or "").lower()
    if charset is None and not (ctype.startswith("text/") or "json" in ctype or "xml" in ctype
                                or "javascript" in ctype or "x-www-form-urlencoded" in ctype):
        # binary (images, pdf, zip...): keep every byte, the same way Storage does
        text = raw.decode("utf-8", "surrogateescape")
    else:
        try:
            text = raw.decode(charset or "utf-8", "replace")
        except LookupError:
            text = raw.decode("utf-8", "replace")
    return make_response(i, text, status, {k: v for k, v in hdrs.items()})


# ---- Http.fake(): canned responses for tests, no network --------------------

class HttpFake:
    def __init__(self, routes):
        self.routes = routes        # [(pattern, value)]: a value, or a function(request dict)
        self.log = []               # every request sent while faked


def url_matches(pattern, url):
    """"*" matches anything; a pattern without a scheme matches http and https;
    the query string only counts when the pattern has one."""
    import fnmatch
    base = url.split("?", 1)[0]
    if pattern == "*":
        return True
    if not re.match(r"^https?://", pattern):
        pattern = "http*://" + pattern
    return fnmatch.fnmatchcase(url if "?" in pattern else base, pattern)


def fake_value(i, v, n):
    """What a faked URL answers with: a Response, a dict/list (JSON), a string (body) or an int (status)."""
    if isinstance(v, BarqObject) and v.cls.name == "Response":
        return v
    if v is None:
        return make_response(i, "", 200)
    if type(v) is int:
        return make_response(i, "", v)
    if isinstance(v, str):
        return make_response(i, v, 200, {"Content-Type": "text/plain; charset=utf-8"})
    if isinstance(v, (dict, list)):
        return make_response(i, json.dumps(to_json_value(i, v, n), ensure_ascii=False), 200,
                             {"Content-Type": "application/json"})
    i.throw("TypeException", f"Http.fake(): a faked URL answers with a Response, dict, list, string, int or a "
            f"function returning one, got {type_name(v)}", n)


def fake_send(i, method, url, headers, body, n, fname):
    st = i._http_fake
    text = body.decode("utf-8", "replace") if body is not None else ""
    ctype = header_lookup(headers, "Content-Type") or ""
    data = None
    if text and "json" in ctype:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    elif text and "x-www-form-urlencoded" in ctype:
        data = flat_query(text)
    parts = urllib.parse.urlsplit(url)
    record = {"method": method, "url": url, "headers": dict(headers), "body": text, "data": data,
              "query": flat_query(parts.query)}
    st.log.append(record)
    for pattern, value in st.routes:
        if url_matches(pattern, url):
            if type_name(value) == "karo":
                value = i.call_function(value, [dict(record)], n)
            res = fake_value(i, value, n)
            out = make_response(i, res.fields.get("body", ""), res.fields.get("status", 200),
                                res.fields.get("headers") or {})
            return out
    i.throw("HttpException", f"{fname}(): Http.fake() has no response for {method} {url} "
            '(add its URL, a pattern like "api.example.com/*", or "*")', n)


def http_fake(i, a, n):
    routes = arg(i, a, 0, ("dict",), "Http.fake", n, None)
    if routes is None:
        routes = {"*": None}
    for k in routes:
        need(i, k, ("string",), "Http.fake (URL pattern)", 1, n)
    i._http_fake = HttpFake(list(routes.items()))
    return None


def http_recorded(i, a, n):
    st = i.__dict__.get("_http_fake")
    return [dict(r) for r in st.log] if st is not None else []


def http_restore(i, a, n):
    i._http_fake = None
    return None


def http_method(method, has_body):
    fname = f"Http.{method.lower()}"

    def fn(i, a, n):
        url = need(i, a[0], ("string",), fname, 1, n)
        if has_body:
            data = a[1] if len(a) > 1 else None
            opts = arg(i, a, 2, ("dict",), fname, n, {})
        else:
            data = None
            opts = arg(i, a, 1, ("dict",), fname, n, {})
        return http_send(i, method, url, data, http_options(i, opts, n, fname), n, fname)
    return fn


def http_request(i, a, n):
    method = need(i, a[0], ("string",), "Http.request", 1, n).upper()
    url = need(i, a[1], ("string",), "Http.request", 2, n)
    opts = http_options(i, arg(i, a, 2, ("dict",), "Http.request", n, {}), n, "Http.request")
    data = opts.get("json", opts.get("body"))
    return http_send(i, method, url, data, opts, n, "Http.request")


# ---- server -----------------------------------------------------------------

def to_response(i, result, n):
    if isinstance(result, BarqObject) and result.cls.name == "Response":
        return result
    if result is None:
        return make_response(i, "", 204)
    if isinstance(result, str):
        return make_response(i, result, 200, {"Content-Type": "text/html; charset=utf-8"})
    if isinstance(result, (dict, list)):
        return resp_json(i, [result], n)
    if isinstance(result, BarqObject) and result.cls.find_method("toArray") is not None:
        from . import hooks
        return resp_json(i, [result, hooks.response_status(i, result)], n)
    i.throw("TypeException", f"a request handler must return Response, string, dict, list or null — got {type_name(result)}", n)


def hooks_rules():
    from .hooks import VALIDATION_RULES
    return VALIDATION_RULES


def serve_static(folder, path):
    """Return (bytes, content type) for a file under folder, or None."""
    rel = urllib.parse.unquote(path).lstrip("/")
    full = os.path.realpath(os.path.join(folder, rel))
    if not full.startswith(os.path.realpath(folder) + os.sep) or not os.path.isfile(full):
        return None
    with open(full, "rb") as f:
        data = f.read()
    return data, mimetypes.guess_type(full)[0] or "application/octet-stream"


def http_serve(i, a, n):
    handler = a[0]
    if type_name(handler) != "karo":
        i.throw("TypeException", f"Http.serve() needs a handler function, got {type_name(handler)}", n)
    opts = arg(i, a, 1, ("dict",), "Http.serve", n, {})
    for k in opts:
        if k not in ("port", "host", "static", "max", "quiet"):
            i.throw("ValueException", f'Http.serve(): unknown option "{k}" (use port, host, static, max, quiet)', n)
    port = need(i, opts.get("port", 8000), ("int",), "Http.serve option port", 1, n)
    host = need(i, opts.get("host", "127.0.0.1"), ("string",), "Http.serve option host", 1, n)
    static = opts.get("static")
    if static is not None:
        need(i, static, ("string",), "Http.serve option static", 1, n)
        static = static if os.path.isabs(static) else os.path.join(i.root, static)
    limit = opts.get("max")
    quiet = opts.get("quiet") is True
    state = {"exit": None, "count": 0}

    def log(msg):
        if not quiet:
            print(msg, file=sys.stderr, flush=True)

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "Barq/0.6"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def handle_any(self):
            start = time.perf_counter()
            parts = urllib.parse.urlsplit(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            from .upload import settings as upload_settings
            if length < 0 or length > upload_settings(i)[1] * 1024:
                body = b"Payload Too Large" if length > 0 else b"Bad Request"
                self.send_response(413 if length > 0 else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                self.close_connection = True
                log(f"{self.command} {self.path} -> {413 if length > 0 else 400} (body too large)")
                return
            raw = self.rfile.read(length) if length > 0 else b""
            out_status, out_headers, out_body = 500, {"Content-Type": "text/plain; charset=utf-8"}, b"Internal Server Error"
            out_cookies = []
            hit = serve_static(static, parts.path) if static and self.command in ("GET", "HEAD") else None
            if hit is not None:
                out_status, out_headers, out_body = 200, {"Content-Type": hit[1]}, hit[0]
            else:
                body = raw.decode("utf-8", "replace")
                ctype = self.headers.get("Content-Type") or ""
                form = None
                files = {}
                if "application/x-www-form-urlencoded" in ctype:
                    form = flat_query(body)
                elif "multipart/form-data" in ctype.lower():
                    from .upload import parse_multipart
                    form, files = parse_multipart(i, raw, ctype)
                elif "json" in ctype and body.strip():
                    try:
                        form = json.loads(body)
                    except ValueError:
                        form = None
                req = new_native(i, "Request", RequestState(flat_query(parts.query), form, self.client_address[0]),
                                 method=self.command, path=parts.path, url=self.path, body=body,
                                 headers={k: v for k, v in self.headers.items()})
                req.native.files = files
                try:
                    from .web import current
                    with current(i, req):
                        res = to_response(i, i.call_function(handler, [req], n), n)
                    from .web import finish_request
                    out_cookies = finish_request(i, req, res, n)
                    out_status = res.fields["status"]
                    out_headers = dict(res.fields["headers"])
                    out_body = res.fields["body"].encode("utf-8", "surrogateescape")
                except BarqThrow as e:
                    if not quiet:
                        from .cli import report_uncaught
                        report_uncaught(e)
                except ExitSignal as e:
                    state["exit"] = e
                    out_status, out_body = 503, b"Server stopping"
            out_headers["Content-Length"] = str(len(out_body))
            out_headers.setdefault("Connection", "close")
            self.close_connection = True
            self.send_response(out_status, STATUS_TEXT.get(out_status, ""))
            for k, v in out_headers.items():
                self.send_header(k, v)
            for c in out_cookies:
                self.send_header("Set-Cookie", c)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(out_body)
            ms = (time.perf_counter() - start) * 1000
            log(f"  {self.command} {self.path} -> {out_status} ({ms:.1f} ms)")

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any

    try:
        server = http.server.HTTPServer((host, port), Handler)
    except OSError as e:
        i.throw("HttpException", f"cannot listen on {host}:{port}: {e.strerror or e}", n)
    log(f"Barq server running at http://{host}:{server.server_address[1]}  (Ctrl+C to stop)")
    try:
        while state["exit"] is None and (limit is None or state["count"] < limit):
            server.handle_request()
            state["count"] += 1
    except KeyboardInterrupt:
        log("\nServer stopped.")
    finally:
        server.server_close()
    if state["exit"] is not None:
        raise state["exit"]
    return None


HTTP = {
    "get": (http_method("GET", False), 1, 2),
    "delete": (http_method("DELETE", False), 1, 2),
    "head": (http_method("HEAD", False), 1, 2),
    "post": (http_method("POST", True), 1, 3),
    "put": (http_method("PUT", True), 1, 3),
    "patch": (http_method("PATCH", True), 1, 3),
    "request": (http_request, 2, 3),
    "serve": (http_serve, 1, 2),
    "fake": (http_fake, 0, 1),
    "recorded": (http_recorded, 0, 0),
    "restore": (http_restore, 0, 0),
}


# ===========================================================================
# Csv
# ===========================================================================

def csv_delim(i, a, k, fname, n):
    d = arg(i, a, k, ("string",), fname, n, ",")
    if len(d) != 1:
        i.throw("ValueException", f"{fname}(): delimiter must be one character", n)
    return d


def csv_parse_text(i, text, header, delim, n, fname):
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
    except csv.Error as e:
        i.throw("CsvException", f"{fname}(): {e}", n)
    rows = [r for r in rows if r]
    if not header:
        return rows
    if not rows:
        return []
    head = rows[0]
    if len(set(head)) != len(head):
        i.throw("CsvException", f"{fname}(): the header row has duplicate column names", n)
    out = []
    for line, r in enumerate(rows[1:], start=2):
        if len(r) != len(head):
            i.throw("CsvException", f"{fname}(): row {line} has {len(r)} fields, the header has {len(head)}", n)
        out.append(dict(zip(head, r)))
    return out


def csv_parse(i, a, n):
    text = need(i, a[0], ("string",), "Csv.parse", 1, n)
    header = arg(i, a, 1, ("bool",), "Csv.parse", n, True)
    return csv_parse_text(i, text, header, csv_delim(i, a, 2, "Csv.parse", n), n, "Csv.parse")


def csv_read(i, a, n):
    from .stdlib import io_call, _read
    path = need(i, a[0], ("string",), "Csv.read", 1, n)
    text = io_call(i, n, "Csv.read", path, lambda: _read(path))
    if text.startswith("﻿"):
        text = text[1:]
    header = arg(i, a, 1, ("bool",), "Csv.read", n, True)
    return csv_parse_text(i, text, header, csv_delim(i, a, 2, "Csv.read", n), n, "Csv.read")


def csv_cell(i, v, n):
    if isinstance(v, (list, dict)):
        i.throw("CsvException", f"a CSV cell cannot hold a {type_name(v)} — use Json.encode() first", n)
    return "" if v is None else to_display(v, i)


def csv_encode_rows(i, rows, delim, n, fname):
    need(i, rows, ("list",), fname, 1, n)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delim, lineterminator="\n")
    if rows and all(isinstance(r, dict) for r in rows):
        head = []
        for r in rows:
            for k in r:
                if k not in head:
                    head.append(k)
        w.writerow([csv_cell(i, h, n) for h in head])
        for r in rows:
            w.writerow([csv_cell(i, r.get(h), n) for h in head])
    else:
        for k, r in enumerate(rows):
            if not isinstance(r, list):
                i.throw("CsvException", f"{fname}(): rows must all be lists or all be dicts (row {k + 1} is {type_name(r)})", n)
            w.writerow([csv_cell(i, v, n) for v in r])
    return buf.getvalue()


def csv_encode(i, a, n):
    return csv_encode_rows(i, a[0], csv_delim(i, a, 1, "Csv.encode", n), n, "Csv.encode")


def csv_write(i, a, n):
    from .stdlib import io_call, _write
    path = need(i, a[0], ("string",), "Csv.write", 1, n)
    text = csv_encode_rows(i, a[1], csv_delim(i, a, 2, "Csv.write", n), n, "Csv.write")
    io_call(i, n, "Csv.write", path, lambda: _write(path, text, "w"))
    return len(a[1])


CSV = {
    "parse": (csv_parse, 1, 3),
    "read": (csv_read, 1, 3),
    "encode": (csv_encode, 1, 2),
    "write": (csv_write, 2, 3),
}


# ===========================================================================
# Validation
# ===========================================================================

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.I)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
INT_RE = re.compile(r"^[+-]?\d+$")
NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")

# canonical rule names; lookup ignores case and underscores (alpha_num == alphaNum)
RULES = ["required", "nullable", "sometimes", "string", "int", "integer", "numeric", "float", "bool", "boolean",
         "email", "url", "uuid", "alpha", "alphaNum", "alphaDash", "min", "max", "between", "size", "in", "notIn",
         "regex", "same", "different", "confirmed", "date", "list", "array", "dict", "accepted", "digits",
         "startsWith", "endsWith", "json", "unique", "exists",
         "file", "image", "mimes", "mimetypes", "extensions", "dimensions"]
FILE_RULES = {"file", "image", "mimes", "mimetypes", "extensions", "dimensions"}
RULE_KEYS = {r.lower(): r for r in RULES}
NUMERIC_RULES = {"int", "integer", "numeric", "float"}

MESSAGES = {
    "required": "The :field field is required.",
    "string": "The :field field must be a string.",
    "int": "The :field field must be an integer.",
    "numeric": "The :field field must be a number.",
    "bool": "The :field field must be true or false.",
    "email": "The :field field must be a valid email address.",
    "url": "The :field field must be a valid URL.",
    "uuid": "The :field field must be a valid UUID.",
    "alpha": "The :field field must only contain letters.",
    "alphaNum": "The :field field must only contain letters and numbers.",
    "alphaDash": "The :field field must only contain letters, numbers, dashes and underscores.",
    "min.number": "The :field field must be at least :min.",
    "min.string": "The :field field must be at least :min characters.",
    "min.list": "The :field field must have at least :min items.",
    "max.number": "The :field field must not be greater than :max.",
    "max.string": "The :field field must not be longer than :max characters.",
    "max.list": "The :field field must not have more than :max items.",
    "between.number": "The :field field must be between :min and :max.",
    "between.string": "The :field field must be between :min and :max characters.",
    "between.list": "The :field field must have between :min and :max items.",
    "size.number": "The :field field must be :size.",
    "size.string": "The :field field must be :size characters.",
    "size.list": "The :field field must contain :size items.",
    "in": "The selected :field is invalid.",
    "notIn": "The selected :field is invalid.",
    "regex": "The :field field format is invalid.",
    "same": "The :field field must match :other.",
    "different": "The :field field must be different from :other.",
    "confirmed": "The :field field confirmation does not match.",
    "date": "The :field field must be a valid date.",
    "list": "The :field field must be a list.",
    "dict": "The :field field must be a dict.",
    "accepted": "The :field field must be accepted.",
    "digits": "The :field field must be :digits digits.",
    "startsWith": "The :field field must start with :values.",
    "endsWith": "The :field field must end with :values.",
    "json": "The :field field must be valid JSON.",
    "unique": "The :field has already been taken.",
    "exists": "The selected :field is invalid.",
}
from .upload import MESSAGES as UPLOAD_MESSAGES  # noqa: E402
MESSAGES.update(UPLOAD_MESSAGES)


def split_rules(i, spec, n):
    """'required|min:2|regex:/a|b/' -> [(name, [params])]. A regex rule takes the rest of the string."""
    if isinstance(spec, list):
        parts = spec
    elif isinstance(spec, str):
        parts = []
        rest = spec
        while rest:
            if rest.lower().startswith("regex:"):
                parts.append(rest)
                break
            head, sep, rest = rest.partition("|")
            parts.append(head)
            if not sep:
                break
    else:
        i.throw("TypeException", f"validation rules must be a string like \"required|email\" or a list, got {type_name(spec)}", n)
    out = []
    for p in parts:
        if not isinstance(p, str):
            i.throw("TypeException", f"each validation rule must be a string, got {type_name(p)}", n)
        p = p.strip()
        if not p:
            continue
        name, _, params = p.partition(":")
        key = name.replace("_", "").lower()
        if key not in RULE_KEYS:
            i.throw("ValueException", f'unknown validation rule "{name}"', n)
        canon = RULE_KEYS[key]
        canon = {"integer": "int", "boolean": "bool", "array": "list", "float": "numeric"}.get(canon, canon)
        if canon == "regex":
            args = [params]
        else:
            args = [x.strip() for x in params.split(",")] if params else []
        if canon in ("min", "max", "size", "digits") and (len(args) != 1 or not NUM_RE.match(args[0])):
            i.throw("ValueException", f'validation rule "{name}" needs a number, e.g. {name}:3', n)
        if canon == "between" and (len(args) != 2 or not all(NUM_RE.match(x) for x in args)):
            i.throw("ValueException", 'validation rule "between" needs two numbers, e.g. between:1,10', n)
        if canon in ("same", "different", "in", "notIn", "startsWith", "endsWith", "regex", "unique", "exists",
                     "mimes", "mimetypes", "extensions", "dimensions") \
                and not (args and args[0]):
            i.throw("ValueException", f'validation rule "{name}" needs a value, e.g. {name}:x', n)
        if canon == "dimensions":
            from .upload import check_dimension_args
            check_dimension_args(i, args, n)
        out.append((canon, args))
    return out


def lookup_path(data, key):
    cur = data
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def num(x):
    return float(x) if not isinstance(x, (int, float)) else x


def measure(value, numeric):
    """(kind, size) used by min/max/between/size. Files are measured in kilobytes."""
    from .upload import is_upload
    if is_upload(value):
        return "file", len(value.native.data) / 1024
    if numeric:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return "number", value
        if isinstance(value, str) and NUM_RE.match(value.strip()):
            return "number", float(value)
    if isinstance(value, str):
        return "string", len(value)
    if isinstance(value, (list, dict)):
        return "list", len(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number", value
    return None, None


def is_date(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if not isinstance(value, str):
        return False
    s = value.strip()
    for fmt in DATE_FORMATS:
        try:
            dt.datetime.strptime(s, fmt)
            return True
        except ValueError:
            pass
    try:
        dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def is_empty(value):
    return value is None or (isinstance(value, str) and value.strip() == "") or \
        (isinstance(value, (list, dict)) and len(value) == 0)


def check_rule(i, rule, args, value, data, field, numeric, n):
    """Return None if the rule passes, else (message key, placeholders)."""
    ph = {}
    ok = True
    s = value if isinstance(value, str) else None
    if rule in FILE_RULES:
        from .upload import check_rule as check_file_rule
        return check_file_rule(rule, args, value)
    if rule == "string":
        ok = isinstance(value, str)
    elif rule == "int":
        ok = (isinstance(value, int) and not isinstance(value, bool)) or (s is not None and bool(INT_RE.match(s.strip())))
    elif rule == "numeric":
        ok = (isinstance(value, (int, float)) and not isinstance(value, bool)) or (s is not None and bool(NUM_RE.match(s.strip())))
    elif rule == "bool":
        ok = isinstance(value, bool) or value in (0, 1) and not isinstance(value, float) or \
            (s is not None and s.lower() in ("true", "false", "0", "1"))
    elif rule == "accepted":
        ok = value is True or value == 1 and not isinstance(value, bool) or (s is not None and s.lower() in ("yes", "on", "1", "true"))
    elif rule == "email":
        ok = s is not None and bool(EMAIL_RE.match(s))
    elif rule == "url":
        ok = s is not None and bool(URL_RE.match(s))
    elif rule == "uuid":
        ok = s is not None and bool(UUID_RE.match(s))
    elif rule == "alpha":
        ok = s is not None and s.isalpha()
    elif rule == "alphaNum":
        ok = s is not None and s.isalnum()
    elif rule == "alphaDash":
        ok = s is not None and bool(re.match(r"^[\w-]+$", s))
    elif rule == "json":
        ok = s is not None
        if ok:
            try:
                json.loads(s)
            except ValueError:
                ok = False
    elif rule == "date":
        ok = is_date(value)
    elif rule == "list":
        ok = isinstance(value, list)
    elif rule == "dict":
        ok = isinstance(value, dict)
    elif rule == "digits":
        ok = s is not None and s.isdigit() and len(s) == int(float(args[0])) or \
            (isinstance(value, int) and not isinstance(value, bool) and value >= 0 and len(str(value)) == int(float(args[0])))
        ph["digits"] = args[0]
    elif rule in ("min", "max", "size", "between"):
        kind, size = measure(value, numeric)
        if kind is None:
            ok = False
            kind = "string"
        elif rule == "min":
            ok = size >= num(args[0])
        elif rule == "max":
            ok = size <= num(args[0])
        elif rule == "size":
            ok = size == num(args[0])
        else:
            ok = num(args[0]) <= size <= num(args[1])
        key = rule if rule != "between" else "between"
        ph.update({"min": args[0], "max": args[-1], "size": args[0]})
        if not ok:
            return f"{key}.{kind}", ph
        return None
    elif rule in ("in", "notIn"):
        found = to_display(value, i) in args if isinstance(value, (str, int, float)) and not isinstance(value, bool) else False
        ok = found if rule == "in" else not found
        ph["values"] = ", ".join(args)
    elif rule in ("startsWith", "endsWith"):
        ok = s is not None and any(s.startswith(x) if rule == "startsWith" else s.endswith(x) for x in args)
        ph["values"] = ", ".join(args)
    elif rule == "regex":
        from .stdlib import compile_regex
        ok = s is not None and compile_regex(i, args[0], n, "Validation").search(s) is not None
    elif rule in ("same", "different"):
        _, other = lookup_path(data, args[0])
        from .runtime import values_equal
        ok = values_equal(value, other) if rule == "same" else not values_equal(value, other)
        ph["other"] = args[0].replace("_", " ")
    elif rule in hooks_rules():
        ok = hooks_rules()[rule](i, rule, args, value, field, n)
    elif rule == "confirmed":
        _, other = lookup_path(data, field + "_confirmation")
        from .runtime import values_equal
        ok = values_equal(value, other)
    if ok:
        return None
    return rule, ph


def run_validation(i, data, rules, messages, n):
    need(i, data, ("dict",), "Validation.make", 1, n)
    need(i, rules, ("dict",), "Validation.make", 2, n)
    errors = {}
    validated = {}
    for field, spec in rules.items():
        if not isinstance(field, str):
            i.throw("TypeException", "validation field names must be strings", n)
        parsed = split_rules(i, spec, n)
        names = {r for r, _ in parsed}
        present, value = lookup_path(data, field)
        label = field.replace("_", " ").replace(".", " ")
        fails = []

        def fail(key, ph, rule):
            custom = messages.get(f"{field}.{rule}") or messages.get(rule)
            text = custom if isinstance(custom, str) else MESSAGES[key]
            for k, v in {"field": label, **ph}.items():
                text = text.replace(":" + k, v)
            fails.append(text)

        if "sometimes" in names and not present:
            continue
        if is_empty(value):
            if "required" in names:
                fail("required", {}, "required")
            elif "accepted" in names:
                fail("accepted", {}, "accepted")
            if fails:
                errors[field] = fails
            elif present:
                validated[field] = value
            continue
        numeric = bool(names & NUMERIC_RULES)
        for rule, args in parsed:
            if rule in ("required", "nullable", "sometimes"):
                continue
            res = check_rule(i, rule, args, value, data, field, numeric, n)
            if res is not None:
                fail(res[0], res[1], rule)
        if fails:
            errors[field] = fails
        else:
            validated[field] = value
    return errors, validated


class ValidatorState:
    def __init__(self, errors, validated):
        self.errors = errors
        self.validated = validated


def validation_make(i, a, n):
    messages = arg(i, a, 2, ("dict",), "Validation.make", n, {})
    errors, validated = run_validation(i, a[0], a[1], messages, n)
    return new_native(i, "Validator", ValidatorState(errors, validated))


def validation_validate(i, a, n):
    messages = arg(i, a, 2, ("dict",), "Validation.validate", n, {})
    errors, validated = run_validation(i, a[0], a[1], messages, n)
    if errors:
        first = next(iter(errors.values()))[0]
        more = sum(len(v) for v in errors.values()) - 1
        msg = first + (f" (and {more} more error{'s' if more != 1 else ''})" if more else "")
        throw_with(i, "ValidationException", msg, n, errors=errors)
    return validated


def validation_check(i, a, n):
    errors, _ = run_validation(i, {"value": a[0]}, {"value": a[1]}, {}, n)
    return not errors


def v_first(i, a, n, this):
    errs = this.native.errors
    if a:
        msgs = errs.get(need(i, a[0], ("string",), "Validator.first", 1, n))
        return msgs[0] if msgs else None
    for msgs in errs.values():
        return msgs[0]
    return None


VALIDATOR_METHODS = {
    "fails": (lambda i, a, n, this: bool(this.native.errors), 0, 0),
    "passes": (lambda i, a, n, this: not this.native.errors, 0, 0),
    "errors": (lambda i, a, n, this: {k: list(v) for k, v in this.native.errors.items()}, 0, 0),
    "messages": (lambda i, a, n, this: [m for v in this.native.errors.values() for m in v], 0, 0),
    "first": (v_first, 0, 1),
    "has": (lambda i, a, n, this: need(i, a[0], ("string",), "Validator.has", 1, n) in this.native.errors, 1, 1),
    "validated": (lambda i, a, n, this: dict(this.native.validated), 0, 0),
}

VALIDATION = {
    "make": (validation_make, 2, 3),
    "validate": (validation_validate, 2, 3),
    "check": (validation_check, 2, 2),
}


# ===========================================================================
# Config — reads config/*.bq and config/*.json under the project root
# ===========================================================================

class ConfigState:
    data = None         # dict, loaded lazily
    loaded_from = None


def config_load_dir(i, folder, n):
    data = {}
    if not os.path.isdir(folder):
        return data
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        stem, ext = os.path.splitext(name)
        if ext == ".json":
            try:
                with open(path, encoding="utf-8") as f:
                    data[stem] = json.load(f)
            except ValueError as e:
                i.throw("JsonException", f"config/{name}: invalid JSON: {e}", n)
        elif ext == ".bq":
            mod = i.load_module(path)
            env = mod.env.vars
            if stem in env:
                data[stem] = env[stem][0]
            else:
                values = {k: v[0] for k, v in env.items()
                          if k not in mod.functions and k not in mod.classes}
                data[stem] = values
    return data


def config_data(i, n=None):
    if ConfigState.data is None:
        folder = os.path.join(i.root, "config")
        ConfigState.data = {}
        ConfigState.data = config_load_dir(i, folder, n)
        ConfigState.loaded_from = folder
    return ConfigState.data


def config_lookup(i, key, n=None):
    found, value = lookup_path(config_data(i, n), key)
    return value if found else None


def config_get(i, a, n):
    key = need(i, a[0], ("string",), "Config.get", 1, n)
    found, value = lookup_path(config_data(i, n), key)
    return value if found else (a[1] if len(a) > 1 else None)


def config_set(i, a, n):
    key = need(i, a[0], ("string",), "Config.set", 1, n)
    cur = config_data(i, n)
    parts = key.split(".")
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = a[1]
    return None


def config_load(i, a, n):
    folder = arg(i, a, 0, ("string",), "Config.load", n, "config")
    if not os.path.isabs(folder):
        folder = os.path.join(i.root, folder)
    if not os.path.isdir(folder):
        i.throw("IOException", f"Config.load: no such folder: {folder}", n)
    config_data(i, n).update(config_load_dir(i, folder, n))
    return None


CONFIG = {
    "get": (config_get, 1, 2),
    "set": (config_set, 2, 2),
    "has": (lambda i, a, n: lookup_path(config_data(i, n), need(i, a[0], ("string",), "Config.has", 1, n))[0], 1, 1),
    "all": (lambda i, a, n: config_data(i, n), 0, 0),
    "load": (config_load, 0, 1),
}


# ===========================================================================
# install
# ===========================================================================

MODULES2 = {"Http": HTTP, "Csv": CSV, "Validation": VALIDATION, "Config": CONFIG}


def install(i):
    for name, parent in STDLIB2_EXCEPTIONS:
        cls = BarqClass(name, name, None, parent=i.classes[parent], native=True)
        if name == "ValidationException":
            cls.props["errors"] = PropInfo("errors", T("dict"), "public", None, cls)
        i.classes[name] = cls
        i.builtins.declare(name, cls, None, const=True)
    native_class(i, "Database", {"driver": "string", "name": "string"}, DATABASE_METHODS,
                 {"connect": (database_connect, 0, 2)})
    native_class(i, "QueryBuilder", None, QUERY_METHODS)
    native_class(i, "Schema", None, SCHEMA_METHODS)
    native_class(i, "Blueprint", None, BLUEPRINT_METHODS)
    native_class(i, "ColumnDef", None, COLUMN_METHODS)
    native_class(i, "Response", {"status": "int", "body": "string", "headers": "dict"}, RESPONSE_METHODS,
                 RESPONSE_STATICS, construct=resp_construct)
    native_class(i, "Request", {"method": "string", "path": "string", "url": "string", "body": "string",
                                "headers": "dict"}, REQUEST_METHODS)
    native_class(i, "Validator", None, VALIDATOR_METHODS)
    for name, table in MODULES2.items():
        i.builtins.declare(name, module(name, table), None, const=True)
