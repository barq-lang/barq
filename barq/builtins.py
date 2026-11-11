"""Global functions, the Math/Str modules, methods on built-in types,
casts, and the built-in exception classes."""

import math
import random
import re
import sys

from . import nodes as N
from .runtime import (
    BarqClass, BarqFunction, BarqObject, BarqSet, BarqThrow, BoundMethod, ExitSignal, NativeFunction, NativeModule, PropInfo,
    dump_value, hash_key, to_display, type_name, values_equal,
)

VARIADIC = -1

EXCEPTION_CLASSES = [
    # (name, parent)
    ("Exception", None),
    ("RuntimeException", "Exception"),
    ("TypeException", "Exception"),
    ("CastException", "TypeException"),
    ("NameException", "Exception"),
    ("ValueException", "Exception"),
    ("IndexException", "Exception"),
    ("KeyException", "Exception"),
    ("NullException", "Exception"),
    ("DivisionByZeroException", "Exception"),
    ("IOException", "Exception"),
]


def T(name, *args):
    return N.TypeRef(name, list(args))


# ---------------------------------------------------------------------------
# helpers for argument checking inside native functions
# ---------------------------------------------------------------------------

def need(i, value, kinds, fname, pos, node):
    """Check a native argument's type. kinds: tuple of Barq type names."""
    tn = type_name(value)
    if tn in kinds:
        return value
    if "number" in kinds and tn in ("int", "float"):
        return value
    if "callable" in kinds and tn == "karo":
        return value
    want = " or ".join(k for k in kinds)
    i.throw("TypeException", f"{fname}() argument {pos} must be {want}, got {tn}", node)


FUNCTION_TYPES = (BarqFunction, NativeFunction, BoundMethod)


def call(i, fn, args, node, fname):
    if type(fn) not in FUNCTION_TYPES:
        i.throw("TypeException", f"{fname}() needs a function, got {type_name(fn)}", node)
    return i.call_function(fn, args, node)


def need_bool(i, value, fname, node):
    if value is True or value is False:
        return value
    i.throw("TypeException", f"the function passed to {fname}() must return bool, got {type_name(value)}", node)


def sort_key(i, node, fname):
    def key(v):
        if type(v) in (int, float):
            return (0, v)
        if type(v) is str:
            return (1, v)
        i.throw("TypeException", f"{fname}() can only sort numbers or strings, found {type_name(v)}", node)
    return key


def check_sortable(i, values, node, fname):
    kinds = {("num" if type(v) in (int, float) else type_name(v)) for v in values}
    if len(kinds) > 1:
        i.throw("TypeException", f"{fname}() cannot compare {' and '.join(sorted(kinds))}", node)


# ---------------------------------------------------------------------------
# global functions
# ---------------------------------------------------------------------------

def g_print(i, args, node):
    i.stdout.write(" ".join(to_display(a, i) for a in args) + "\n")
    return None


def g_write(i, args, node):
    i.stdout.write("".join(to_display(a, i) for a in args))
    return None


def g_dump(i, args, node):
    for a in args:
        i.stdout.write(dump_value(a, i) + "\n")
    return None


def g_dd(i, args, node):
    g_dump(i, args, node)
    i.stdout.flush()
    raise ExitSignal(1)


def g_exit(i, args, node):
    code = args[0] if args else 0
    need(i, code, ("int",), "exit", 1, node)
    raise ExitSignal(code)


def g_die(i, args, node):
    if args:
        i.stdout.write(to_display(args[0], i) + "\n")
    raise ExitSignal(1)


def g_range(i, args, node):
    for n, a in enumerate(args, 1):
        need(i, a, ("int",), "range", n, node)
    if len(args) == 3 and args[2] == 0:
        i.throw("ValueException", "range() step cannot be 0", node)
    return range(*args)


def g_len(i, args, node):
    v = args[0]
    if isinstance(v, (str, list, dict, BarqSet, range)):
        return len(v)
    i.throw("TypeException", f"len() needs a string or collection, got {type_name(v)}", node)


def g_type(i, args, node):
    return type_name(args[0])


def g_input(i, args, node):
    if args:
        i.stdout.write(to_display(args[0], i))
        i.stdout.flush()
    line = i.stdin.readline()
    if not line:
        return None
    return line.rstrip("\n").rstrip("\r")


def g_args(i, args, node):
    return list(i.argv)


def g_assert(i, args, node):
    cond = args[0]
    if cond is not True and cond is not False:
        i.throw("TypeException", f"assert() needs a bool, got {type_name(cond)}", node)
    if not cond:
        msg = to_display(args[1], i) if len(args) > 1 else "assertion failed"
        i.throw("RuntimeException", msg, node)
    return None


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

def make_math():
    def num(fname, pos=1):
        return ("number",)

    def unary_float(name, fn):
        def f(i, args, node):
            x = need(i, args[0], ("number",), f"Math.{name}", 1, node)
            try:
                return float(fn(x))
            except (ValueError, OverflowError):
                i.throw("ValueException", f"Math.{name}({to_display(x)}) is not defined", node)
        return NativeFunction(f"Math.{name}", f, 1)

    def m_abs(i, a, n):
        return abs(need(i, a[0], ("number",), "Math.abs", 1, n))

    def m_round(i, a, n):
        x = need(i, a[0], ("number",), "Math.round", 1, n)
        if len(a) > 1:
            digits = need(i, a[1], ("int",), "Math.round", 2, n)
            # Round half away from zero, like PHP.
            factor = 10 ** digits
            return math.floor(abs(x) * factor + 0.5) / factor * (1 if x >= 0 else -1)
        return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)

    def m_floor(i, a, n):
        return math.floor(need(i, a[0], ("number",), "Math.floor", 1, n))

    def m_ceil(i, a, n):
        return math.ceil(need(i, a[0], ("number",), "Math.ceil", 1, n))

    def m_pow(i, a, n):
        return i.binop("**", need(i, a[0], ("number",), "Math.pow", 1, n), need(i, a[1], ("number",), "Math.pow", 2, n), n)

    def minmax(fname, fn):
        def f(i, a, n):
            values = a[0] if len(a) == 1 and isinstance(a[0], list) else a
            if not values:
                i.throw("ValueException", f"Math.{fname}() needs at least one value", n)
            for k, v in enumerate(values, 1):
                need(i, v, ("number",), f"Math.{fname}", k, n)
            return fn(values)
        return NativeFunction(f"Math.{fname}", f, 1, VARIADIC)

    def m_random(i, a, n):
        if not a:
            return random.random()
        lo = need(i, a[0], ("int",), "Math.random", 1, n)
        hi = need(i, a[1], ("int",), "Math.random", 2, n) if len(a) > 1 else None
        if hi is None:
            i.throw("TypeException", "Math.random() takes 0 or 2 arguments (min, max)", n)
        return random.randint(lo, hi)

    def m_clamp(i, a, n):
        x, lo, hi = (need(i, v, ("number",), "Math.clamp", k + 1, n) for k, v in enumerate(a))
        return max(lo, min(hi, x))

    members = {
        "PI": math.pi, "E": math.e, "INF": math.inf,
        "abs": NativeFunction("Math.abs", m_abs, 1),
        "round": NativeFunction("Math.round", m_round, 1, 2),
        "floor": NativeFunction("Math.floor", m_floor, 1),
        "ceil": NativeFunction("Math.ceil", m_ceil, 1),
        "pow": NativeFunction("Math.pow", m_pow, 2),
        "min": minmax("min", min),
        "max": minmax("max", max),
        "random": NativeFunction("Math.random", m_random, 0, 2),
        "clamp": NativeFunction("Math.clamp", m_clamp, 3),
        "sqrt": unary_float("sqrt", math.sqrt),
        "sin": unary_float("sin", math.sin),
        "cos": unary_float("cos", math.cos),
        "tan": unary_float("tan", math.tan),
        "log": unary_float("log", math.log),
        "log10": unary_float("log10", math.log10),
        "exp": unary_float("exp", math.exp),
    }
    return NativeModule("Math", members)


# ---------------------------------------------------------------------------
# string methods (used both as "s".upper() and Str.upper("s"))
# ---------------------------------------------------------------------------

def slugify(s):
    s = re.sub(r"[^\w\s-]", "", s.lower(), flags=re.UNICODE)
    return re.sub(r"[\s_-]+", "-", s).strip("-")


def s_split(i, s, a, n):
    sep = need(i, a[0], ("string",), "split", 1, n) if a else None
    if sep == "":
        return list(s)
    return s.split(sep) if sep is not None else s.split()


def s_replace(i, s, a, n):
    return s.replace(need(i, a[0], ("string",), "replace", 1, n), need(i, a[1], ("string",), "replace", 2, n))


def s_substr(i, s, a, n):
    start = need(i, a[0], ("int",), "substr", 1, n)
    if len(a) > 1:
        length = need(i, a[1], ("int",), "substr", 2, n)
        return s[start:start + length] if start >= 0 else s[start:len(s) + start + length]
    return s[start:]


def s_pad(i, s, a, n):
    width = need(i, a[0], ("int",), "pad", 1, n)
    fill = need(i, a[1], ("string",), "pad", 2, n) if len(a) > 1 else " "
    side = need(i, a[2], ("string",), "pad", 3, n) if len(a) > 2 else "right"
    if len(fill) != 1:
        i.throw("ValueException", "pad() fill must be a single character", n)
    if side == "left":
        return s.rjust(width, fill)
    if side == "both":
        return s.center(width, fill)
    if side == "right":
        return s.ljust(width, fill)
    i.throw("ValueException", 'pad() side must be "left", "right" or "both"', n)


def s_repeat(i, s, a, n):
    count = need(i, a[0], ("int",), "repeat", 1, n)
    if count < 0:
        i.throw("ValueException", "repeat() count cannot be negative", n)
    return s * count


def s_index_of(i, s, a, n):
    return s.find(need(i, a[0], ("string",), "indexOf", 1, n))


STRING_METHODS = {
    # name: (impl(i, s, args, node), min_args, max_args)
    "len": (lambda i, s, a, n: len(s), 0, 0),
    "length": (lambda i, s, a, n: len(s), 0, 0),
    "upper": (lambda i, s, a, n: s.upper(), 0, 0),
    "lower": (lambda i, s, a, n: s.lower(), 0, 0),
    "trim": (lambda i, s, a, n: s.strip(), 0, 0),
    "trimStart": (lambda i, s, a, n: s.lstrip(), 0, 0),
    "trimEnd": (lambda i, s, a, n: s.rstrip(), 0, 0),
    "contains": (lambda i, s, a, n: need(i, a[0], ("string",), "contains", 1, n) in s, 1, 1),
    "startsWith": (lambda i, s, a, n: s.startswith(need(i, a[0], ("string",), "startsWith", 1, n)), 1, 1),
    "endsWith": (lambda i, s, a, n: s.endswith(need(i, a[0], ("string",), "endsWith", 1, n)), 1, 1),
    "indexOf": (s_index_of, 1, 1),
    "split": (s_split, 0, 1),
    "replace": (s_replace, 2, 2),
    "substr": (s_substr, 1, 2),
    "pad": (s_pad, 1, 3),
    "repeat": (s_repeat, 1, 1),
    "reverse": (lambda i, s, a, n: s[::-1], 0, 0),
    "slug": (lambda i, s, a, n: slugify(s), 0, 0),
    "capitalize": (lambda i, s, a, n: s[:1].upper() + s[1:], 0, 0),
    "isEmpty": (lambda i, s, a, n: len(s) == 0, 0, 0),
    "chars": (lambda i, s, a, n: list(s), 0, 0),
}


def make_str():
    members = {}
    for name, (impl, lo, hi) in STRING_METHODS.items():
        def f(i, a, n, impl=impl, name=name):
            s = need(i, a[0], ("string",), f"Str.{name}", 1, n)
            return impl(i, s, a[1:], n)
        members[name] = NativeFunction(f"Str.{name}", f, lo + 1, hi + 1)

    def join(i, a, n):
        items = need(i, a[0], ("list",), "Str.join", 1, n)
        sep = need(i, a[1], ("string",), "Str.join", 2, n) if len(a) > 1 else ""
        return sep.join(to_display(x, i) for x in items)
    members["join"] = NativeFunction("Str.join", join, 1, 2)
    return NativeModule("Str", members)


# ---------------------------------------------------------------------------
# list / dict / set methods
# ---------------------------------------------------------------------------

def l_push(i, l, a, n):
    l.extend(a)
    return None


def l_pop(i, l, a, n):
    if not l:
        i.throw("IndexException", "pop() on an empty list", n)
    return l.pop()


def l_insert(i, l, a, n):
    idx = need(i, a[0], ("int",), "insert", 1, n)
    if not 0 <= idx <= len(l):
        i.throw("IndexException", f"insert() index {idx} is out of range (length {len(l)})", n)
    l.insert(idx, a[1])
    return None


def l_remove_at(i, l, a, n):
    idx = need(i, a[0], ("int",), "removeAt", 1, n)
    if not -len(l) <= idx < len(l):
        i.throw("IndexException", f"removeAt() index {idx} is out of range (length {len(l)})", n)
    return l.pop(idx)


def l_remove(i, l, a, n):
    for k, x in enumerate(l):
        if values_equal(x, a[0]):
            del l[k]
            return True
    return False


def l_index_of(i, l, a, n):
    for k, x in enumerate(l):
        if values_equal(x, a[0]):
            return k
    return -1


def l_join(i, l, a, n):
    sep = need(i, a[0], ("string",), "join", 1, n) if a else ""
    return sep.join(to_display(x, i) for x in l)


def l_map(i, l, a, n):
    return [call(i, a[0], [x], n, "map") for x in list(l)]


def l_filter(i, l, a, n):
    return [x for x in list(l) if need_bool(i, call(i, a[0], [x], n, "filter"), "filter", n)]


def l_reduce(i, l, a, n):
    acc = a[1]
    for x in list(l):
        acc = call(i, a[0], [acc, x], n, "reduce")
    return acc


def l_each(i, l, a, n):
    for x in list(l):
        call(i, a[0], [x], n, "each")
    return None


def l_any(i, l, a, n):
    return any(need_bool(i, call(i, a[0], [x], n, "any"), "any", n) for x in list(l))


def l_all(i, l, a, n):
    return all(need_bool(i, call(i, a[0], [x], n, "all"), "all", n) for x in list(l))


def l_find(i, l, a, n):
    for x in list(l):
        if need_bool(i, call(i, a[0], [x], n, "find"), "find", n):
            return x
    return None


def l_sort(i, l, a, n):
    if a:
        keys = [call(i, a[0], [x], n, "sort") for x in l]
        check_sortable(i, keys, n, "sort")
        k = sort_key(i, n, "sort")
        order = sorted(range(len(l)), key=lambda idx: k(keys[idx]))
        l[:] = [l[idx] for idx in order]
    else:
        check_sortable(i, l, n, "sort")
        l.sort(key=sort_key(i, n, "sort"))
    return l


def l_sum(i, l, a, n):
    if not l:
        return 0
    total = l[0]
    need(i, total, ("number",), "sum", 1, n)
    for x in l[1:]:
        total = i.binop("+", total, x, n)
    return total


def l_first(i, l, a, n):
    return l[0] if l else None


def l_last(i, l, a, n):
    return l[-1] if l else None


def l_slice(i, l, a, n):
    start = need(i, a[0], ("int",), "slice", 1, n)
    end = need(i, a[1], ("int",), "slice", 2, n) if len(a) > 1 else None
    return l[start:end]


LIST_METHODS = {
    "len": (lambda i, l, a, n: len(l), 0, 0),
    "push": (l_push, 1, VARIADIC),
    "pop": (l_pop, 0, 0),
    "insert": (l_insert, 2, 2),
    "removeAt": (l_remove_at, 1, 1),
    "remove": (l_remove, 1, 1),
    "contains": (lambda i, l, a, n: any(values_equal(a[0], x) for x in l), 1, 1),
    "indexOf": (l_index_of, 1, 1),
    "join": (l_join, 0, 1),
    "map": (l_map, 1, 1),
    "filter": (l_filter, 1, 1),
    "reduce": (l_reduce, 2, 2),
    "each": (l_each, 1, 1),
    "any": (l_any, 1, 1),
    "all": (l_all, 1, 1),
    "find": (l_find, 1, 1),
    "sort": (l_sort, 0, 1),
    "reverse": (lambda i, l, a, n: (l.reverse(), l)[1], 0, 0),
    "sum": (l_sum, 0, 0),
    "first": (l_first, 0, 0),
    "last": (l_last, 0, 0),
    "slice": (l_slice, 1, 2),
    "copy": (lambda i, l, a, n: list(l), 0, 0),
    "isEmpty": (lambda i, l, a, n: len(l) == 0, 0, 0),
    "clear": (lambda i, l, a, n: l.clear(), 0, 0),
}


def d_get(i, d, a, n):
    try:
        return d.get(a[0], a[1] if len(a) > 1 else None)
    except TypeError:
        return a[1] if len(a) > 1 else None


def d_remove(i, d, a, n):
    try:
        return d.pop(a[0], None)
    except TypeError:
        return None


def d_merge(i, d, a, n):
    other = need(i, a[0], ("dict",), "merge", 1, n)
    out = dict(d)
    out.update(other)
    return out


DICT_METHODS = {
    "len": (lambda i, d, a, n: len(d), 0, 0),
    "keys": (lambda i, d, a, n: list(d.keys()), 0, 0),
    "values": (lambda i, d, a, n: list(d.values()), 0, 0),
    "items": (lambda i, d, a, n: [[k, v] for k, v in d.items()], 0, 0),
    "has": (lambda i, d, a, n: i.contains(d, a[0], n), 1, 1),
    "get": (d_get, 1, 2),
    "remove": (d_remove, 1, 1),
    "merge": (d_merge, 1, 1),
    "copy": (lambda i, d, a, n: dict(d), 0, 0),
    "isEmpty": (lambda i, d, a, n: len(d) == 0, 0, 0),
    "clear": (lambda i, d, a, n: d.clear(), 0, 0),
}


def set_add(i, s, a, n):
    for x in a:
        s.add(i.key_of(x, n, "set item"))
    return None


def set_union(i, s, a, n):
    other = need(i, a[0], ("set",), "union", 1, n)
    return BarqSet(s.values() + other.values())


def set_intersect(i, s, a, n):
    other = need(i, a[0], ("set",), "intersect", 1, n)
    return BarqSet(x for x in s.values() if other.has(x))


def set_diff(i, s, a, n):
    other = need(i, a[0], ("set",), "diff", 1, n)
    return BarqSet(x for x in s.values() if not other.has(x))


def set_has(i, s, a, n):
    try:
        return s.has(a[0])
    except TypeError:
        return False


def set_remove(i, s, a, n):
    try:
        existed = s.has(a[0])
        s.remove(a[0])
        return existed
    except TypeError:
        return False


SET_METHODS = {
    "len": (lambda i, s, a, n: len(s), 0, 0),
    "add": (set_add, 1, VARIADIC),
    "has": (set_has, 1, 1),
    "remove": (set_remove, 1, 1),
    "toList": (lambda i, s, a, n: s.values(), 0, 0),
    "union": (set_union, 1, 1),
    "intersect": (set_intersect, 1, 1),
    "diff": (set_diff, 1, 1),
    "isEmpty": (lambda i, s, a, n: len(s) == 0, 0, 0),
}

RANGE_METHODS = {
    "len": (lambda i, r, a, n: len(r), 0, 0),
    "toList": (lambda i, r, a, n: list(r), 0, 0),
    "contains": (lambda i, r, a, n: a[0] in r if type(a[0]) is int else False, 1, 1),
}

METHOD_TABLES = {str: STRING_METHODS, list: LIST_METHODS, dict: DICT_METHODS, BarqSet: SET_METHODS, range: RANGE_METHODS}


def builtin_method(i, obj, name, node):
    table = METHOD_TABLES.get(type(obj))
    if table is None or name not in table:
        return None
    impl, lo, hi = table[name]
    tname = type_name(obj)
    return NativeFunction(f"{tname}.{name}", lambda i, a, n, obj=obj: impl(i, obj, a, n), lo, hi)


# ---------------------------------------------------------------------------
# casts: ~type(value)
# ---------------------------------------------------------------------------

def cast(i, t, value, node):
    name = t.name

    def fail(extra=""):
        i.throw("CastException", f"cannot cast {type_name(value)} {to_display(value, i, True)} to {name}{extra}", node)

    if name == "int":
        if type(value) is int:
            return value
        if type(value) is bool:
            return 1 if value else 0
        if type(value) is float:
            if math.isnan(value) or math.isinf(value):
                fail()
            return int(value)
        if type(value) is str:
            text = value.strip().replace("_", "")
            try:
                return int(text, 0) if text[:2].lower() in ("0x", "0b", "0o") else int(text)
            except ValueError:
                fail()
        fail()
    if name == "float":
        if type(value) is float:
            return value
        if type(value) is int:
            return float(value)
        if type(value) is bool:
            return 1.0 if value else 0.0
        if type(value) is str:
            try:
                return float(value.strip())
            except ValueError:
                fail()
        fail()
    if name == "string":
        return to_display(value, i)
    if name == "bool":
        if type(value) is bool:
            return value
        if type(value) in (int, float):
            return value != 0
        if type(value) is str:
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off", ""):
                return False
            fail(' (accepted: "true"/"false", "1"/"0", "yes"/"no", "on"/"off")')
        if value is None:
            return False
        fail()
    if name == "list":
        if isinstance(value, list):
            return list(value)
        if isinstance(value, (range, str)):
            return list(value)
        if isinstance(value, BarqSet):
            return value.values()
        if isinstance(value, dict):
            return list(value.keys())
        fail()
    if name == "set":
        if isinstance(value, (list, range, str)):
            return BarqSet(i.key_of(x, node, "set item") for x in value)
        if isinstance(value, BarqSet):
            return BarqSet(value.values())
        fail()
    if name == "dict":
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, BarqObject):
            return {k: v for k, v in value.fields.items()
                    if value.cls.find_prop(k) and value.cls.find_prop(k).visibility == "public"}
        fail()
    if name in ("any",):
        return value
    if name in ("void", "null", "range"):
        i.throw("TypeException", f"cannot cast to {name}", node)
    cls = i.type_class(t, i.module)
    if isinstance(value, BarqObject):
        if value.cls.is_subclass_of(cls):
            return value
        fail()
    if isinstance(value, dict):
        # ~User(#{"name": "Alice"}) builds an object from a dict without
        # calling the constructor. Every key must be a declared property.
        if cls.is_abstract:
            i.throw("CastException", f"cannot cast to abstract class {cls.name}", node)
        obj = i.instantiate_bare(cls)
        for k, v in value.items():
            p = cls.find_prop(k) if isinstance(k, str) else None
            if p is None:
                i.throw("CastException", f"{cls.name} has no property {to_display(k, i, True)}", node)
            if not i.matches(v, p.type):
                i.throw("CastException", f"{cls.name}.{k} expects {p.type.name}, got {type_name(v)}", node)
            obj.fields[k] = v
        return obj
    fail()


# ---------------------------------------------------------------------------
# exceptions
# ---------------------------------------------------------------------------

def make_exception_classes(i):
    def construct(i, a, n, this):
        if a:
            this.fields["message"] = need(i, a[0], ("string",), "Exception.__construct", 1, n)
        if len(a) > 1:
            this.fields["code"] = need(i, a[1], ("int",), "Exception.__construct", 2, n)
        return None

    for name, parent in EXCEPTION_CLASSES:
        cls = BarqClass(name, name, None, parent=i.classes.get(parent), native=True)
        if parent is None:
            cls.props["message"] = PropInfo("message", T("string"), "public", None, cls)
            cls.props["code"] = PropInfo("code", T("int"), "public", None, cls)
            cls.methods["__construct"] = NativeFunction("__construct", construct, 0, 2)
            cls.methods["getMessage"] = NativeFunction("getMessage", lambda i, a, n, this: this.fields["message"], 0)
            cls.methods["getCode"] = NativeFunction("getCode", lambda i, a, n, this: this.fields["code"], 0)
            for m in cls.methods.values():
                m.owner = cls
        i.classes[name] = cls
        i.builtins.declare(name, cls, None, const=True)


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

def install(i):
    g = i.builtins
    functions = [
        ("print", g_print, 0, VARIADIC),
        ("println", g_print, 0, VARIADIC),
        ("write", g_write, 0, VARIADIC),
        ("dump", g_dump, 1, VARIADIC),
        ("dd", g_dd, 1, VARIADIC),
        ("exit", g_exit, 0, 1),
        ("die", g_die, 0, 1),
        ("range", g_range, 1, 3),
        ("len", g_len, 1, 1),
        ("type", g_type, 1, 1),
        ("input", g_input, 0, 1),
        ("args", g_args, 0, 0),
        ("assert", g_assert, 1, 2),
    ]
    for name, fn, lo, hi in functions:
        g.declare(name, NativeFunction(name, fn, lo, hi), None, const=True)
    g.declare("Math", make_math(), None, const=True)
    g.declare("Str", make_str(), None, const=True)
    make_exception_classes(i)
    from . import stdlib, stdlib2, stdlib3, web, magic, view, storage, upload, plugins
    stdlib.install(i)
    stdlib2.install(i)
    web.install(i)
    storage.install(i)
    upload.install(i)
    stdlib3.install(i)
    magic.install(i)
    view.install(i)
    plugins.install(i)      # barq-framework, barq-auth, ... when installed
