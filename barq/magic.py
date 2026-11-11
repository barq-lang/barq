"""Barq Magic Helper Library — global functions always available without import.

Type checks (is_*), string utilities (str_*), regex (rx_*), collection
helpers (list_* / dict_*), object reflection, and value coalescing.
"""

import re as _re
import math as _math
import copy as _copy

from . import nodes as _nodes
from .runtime import (
    BarqClass, BarqFunction, BarqObject, BarqSet,
    NativeFunction, to_display, type_name, values_equal,
)

VARIADIC = -1

# ---------------------------------------------------------------------------
# shared micro-helpers (avoid importing builtins to stay import-safe)
# ---------------------------------------------------------------------------

def _need(i, value, kinds, fname, pos, node):
    tn = type_name(value)
    if tn in kinds:
        return value
    if "number" in kinds and tn in ("int", "float"):
        return value
    want = " or ".join(kinds)
    i.throw("TypeException", f"{fname}() argument {pos} must be {want}, got {tn}", node)


def _need_bool(i, value, fname, node):
    if value is True or value is False:
        return value
    i.throw("TypeException", f"the function passed to {fname}() must return bool, got {type_name(value)}", node)


_FN_TYPES = None

def _fn_types():
    global _FN_TYPES
    if _FN_TYPES is None:
        from .runtime import BarqFunction, NativeFunction, BoundMethod
        _FN_TYPES = (BarqFunction, NativeFunction, BoundMethod)
    return _FN_TYPES


def _call(i, fn, args, node, fname):
    if not isinstance(fn, _fn_types()):
        i.throw("TypeException", f"{fname}() needs a function, got {type_name(fn)}", node)
    return i.call_function(fn, args, node)


def _slugify(s):
    s = _re.sub(r"[^\w\s-]", "", s.lower(), flags=_re.UNICODE)
    return _re.sub(r"[\s_-]+", "-", s).strip("-")


def _barq_pattern(pattern, fname, i, node):
    """Convert /pattern/flags string to (pat, flags_int)."""
    if pattern.startswith("/"):
        end = pattern.rfind("/")
        if end > 0:
            flags_str = pattern[end + 1:]
            pat = pattern[1:end]
            flags = 0
            if "i" in flags_str:
                flags |= _re.IGNORECASE
            if "m" in flags_str:
                flags |= _re.MULTILINE
            if "s" in flags_str:
                flags |= _re.DOTALL
            return pat, flags
    return pattern, 0


# ---------------------------------------------------------------------------
# type checks — is_*
# ---------------------------------------------------------------------------

def g_is_null(i, a, n):
    return a[0] is None


def g_is_str(i, a, n):
    return isinstance(a[0], str)


def g_is_int(i, a, n):
    v = a[0]
    return isinstance(v, int) and not isinstance(v, bool)


def g_is_float(i, a, n):
    return isinstance(a[0], float)


def g_is_num(i, a, n):
    v = a[0]
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def g_is_bool(i, a, n):
    return isinstance(a[0], bool)


def g_is_list(i, a, n):
    return isinstance(a[0], list)


def g_is_dict(i, a, n):
    return isinstance(a[0], dict)


def g_is_fn(i, a, n):
    return isinstance(a[0], _fn_types())


def g_is_obj(i, a, n):
    return isinstance(a[0], BarqObject)


def g_is_set(i, a, n):
    return isinstance(a[0], BarqSet)


def g_is_numeric(i, a, n):
    """True if value is a number OR a string that parses as one."""
    v = a[0]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return True
    if isinstance(v, str):
        try:
            float(v.strip())
            return True
        except ValueError:
            return False
    return False


def g_is_empty(i, a, n):
    """True if value is null, 0, 0.0, "", [], {}, or false."""
    v = a[0]
    if v is None or v is False:
        return True
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v == 0
    if isinstance(v, (str, list, dict)):
        return len(v) == 0
    if isinstance(v, BarqSet):
        return len(v) == 0
    return False


def g_not_null(i, a, n):
    """not_null(v) — True if value is not null."""
    return a[0] is not None


# ---------------------------------------------------------------------------
# number helpers
# ---------------------------------------------------------------------------

def g_is_odd(i, a, n):
    return _need(i, a[0], ("int",), "is_odd", 1, n) % 2 != 0


def g_is_even(i, a, n):
    return _need(i, a[0], ("int",), "is_even", 1, n) % 2 == 0


def g_between(i, a, n):
    """between(value, lo, hi) — True if lo <= value <= hi."""
    v  = _need(i, a[0], ("number",), "between", 1, n)
    lo = _need(i, a[1], ("number",), "between", 2, n)
    hi = _need(i, a[2], ("number",), "between", 3, n)
    return lo <= v <= hi


def g_clamp(i, a, n):
    """clamp(value, lo, hi) — constrain value to [lo, hi]."""
    v  = _need(i, a[0], ("number",), "clamp", 1, n)
    lo = _need(i, a[1], ("number",), "clamp", 2, n)
    hi = _need(i, a[2], ("number",), "clamp", 3, n)
    return max(lo, min(hi, v))


def g_sign(i, a, n):
    """sign(n) — returns -1, 0, or 1."""
    v = _need(i, a[0], ("number",), "sign", 1, n)
    if v < 0:
        return -1
    if v > 0:
        return 1
    return 0


# ---------------------------------------------------------------------------
# collection helpers — list_* / dict_*
# ---------------------------------------------------------------------------

def g_in_list(i, a, n):
    """in_list(value, list) — True if value exists in list."""
    lst = _need(i, a[1], ("list",), "in_list", 2, n)
    return any(values_equal(a[0], x) for x in lst)


def g_has_key(i, a, n):
    """has_key(dict, key) — True if key exists in dict."""
    d = _need(i, a[0], ("dict",), "has_key", 1, n)
    try:
        return a[1] in d
    except TypeError:
        return False


def g_list_search(i, a, n):
    """list_search(list, value) — first index of value, or null."""
    lst = _need(i, a[0], ("list",), "list_search", 1, n)
    for k, x in enumerate(lst):
        if values_equal(x, a[1]):
            return k
    return None


def g_list_map(i, a, n):
    lst = _need(i, a[0], ("list",), "list_map", 1, n)
    return [_call(i, a[1], [x], n, "list_map") for x in list(lst)]


def g_list_filter(i, a, n):
    lst = _need(i, a[0], ("list",), "list_filter", 1, n)
    return [x for x in list(lst) if _need_bool(i, _call(i, a[1], [x], n, "list_filter"), "list_filter", n)]


def g_list_reduce(i, a, n):
    lst = _need(i, a[0], ("list",), "list_reduce", 1, n)
    acc = a[2]
    for x in list(lst):
        acc = _call(i, a[1], [acc, x], n, "list_reduce")
    return acc


def g_list_each(i, a, n):
    lst = _need(i, a[0], ("list",), "list_each", 1, n)
    for x in list(lst):
        _call(i, a[1], [x], n, "list_each")
    return None


def g_list_flatten(i, a, n):
    lst = _need(i, a[0], ("list",), "list_flatten", 1, n)
    result = []
    def _flat(x):
        if isinstance(x, list):
            for item in x:
                _flat(item)
        else:
            result.append(x)
    _flat(lst)
    return result


def g_list_unique(i, a, n):
    lst = _need(i, a[0], ("list",), "list_unique", 1, n)
    seen, out = [], []
    for x in lst:
        if not any(values_equal(x, s) for s in seen):
            seen.append(x)
            out.append(x)
    return out


def g_list_chunk(i, a, n):
    lst  = _need(i, a[0], ("list",), "list_chunk", 1, n)
    size = _need(i, a[1], ("int",),  "list_chunk", 2, n)
    if size <= 0:
        i.throw("ValueException", "list_chunk() size must be > 0", n)
    return [lst[k:k + size] for k in range(0, len(lst), size)]


def g_list_zip(i, a, n):
    la = _need(i, a[0], ("list",), "list_zip", 1, n)
    lb = _need(i, a[1], ("list",), "list_zip", 2, n)
    return [[x, y] for x, y in zip(la, lb)]


def g_list_sum(i, a, n):
    lst = _need(i, a[0], ("list",), "list_sum", 1, n)
    if not lst:
        return 0
    total = lst[0]
    _need(i, total, ("number",), "list_sum", 1, n)
    for x in lst[1:]:
        total = i.binop("+", total, x, n)
    return total


def g_list_count(i, a, n):
    return len(_need(i, a[0], ("list",), "list_count", 1, n))


def g_list_any(i, a, n):
    lst = _need(i, a[0], ("list",), "list_any", 1, n)
    return any(_need_bool(i, _call(i, a[1], [x], n, "list_any"), "list_any", n) for x in list(lst))


def g_list_all(i, a, n):
    lst = _need(i, a[0], ("list",), "list_all", 1, n)
    return all(_need_bool(i, _call(i, a[1], [x], n, "list_all"), "list_all", n) for x in list(lst))


def g_list_find(i, a, n):
    lst = _need(i, a[0], ("list",), "list_find", 1, n)
    for x in list(lst):
        if _need_bool(i, _call(i, a[1], [x], n, "list_find"), "list_find", n):
            return x
    return None


def g_list_first(i, a, n):
    lst = _need(i, a[0], ("list",), "list_first", 1, n)
    return lst[0] if lst else None


def g_list_last(i, a, n):
    lst = _need(i, a[0], ("list",), "list_last", 1, n)
    return lst[-1] if lst else None


def g_list_slice(i, a, n):
    lst   = _need(i, a[0], ("list",), "list_slice", 1, n)
    start = _need(i, a[1], ("int",),  "list_slice", 2, n)
    end   = _need(i, a[2], ("int",),  "list_slice", 3, n) if len(a) > 2 else None
    return lst[start:end]


def g_list_merge(i, a, n):
    la = _need(i, a[0], ("list",), "list_merge", 1, n)
    lb = _need(i, a[1], ("list",), "list_merge", 2, n)
    return la + lb


def g_list_reverse(i, a, n):
    lst = _need(i, a[0], ("list",), "list_reverse", 1, n)
    return list(reversed(lst))


def g_list_sort(i, a, n):
    lst = _need(i, a[0], ("list",), "list_sort", 1, n)
    out = list(lst)
    if len(a) > 1:
        keys = [_call(i, a[1], [x], n, "list_sort") for x in out]
        order = sorted(range(len(out)), key=lambda k: (0 if isinstance(keys[k], (int, float)) else 1, keys[k]))
        return [out[k] for k in order]
    out.sort(key=lambda v: (0 if isinstance(v, (int, float)) else 1, v))
    return out


def g_list_contains(i, a, n):
    lst = _need(i, a[0], ("list",), "list_contains", 1, n)
    return any(values_equal(a[1], x) for x in lst)


def g_dict_merge(i, a, n):
    da = _need(i, a[0], ("dict",), "dict_merge", 1, n)
    db = _need(i, a[1], ("dict",), "dict_merge", 2, n)
    out = dict(da)
    out.update(db)
    return out


def g_dict_keys(i, a, n):
    return list(_need(i, a[0], ("dict",), "dict_keys", 1, n).keys())


def g_dict_values(i, a, n):
    return list(_need(i, a[0], ("dict",), "dict_values", 1, n).values())


def g_dict_has(i, a, n):
    d = _need(i, a[0], ("dict",), "dict_has", 1, n)
    try:
        return a[1] in d
    except TypeError:
        return False


def g_dict_get(i, a, n):
    """dict_get(dict, key, default?) — get key or return default."""
    d = _need(i, a[0], ("dict",), "dict_get", 1, n)
    default = a[2] if len(a) > 2 else None
    try:
        return d.get(a[1], default)
    except TypeError:
        return default


def g_dict_pick(i, a, n):
    """dict_pick(dict, [keys]) — return new dict with only specified keys."""
    d    = _need(i, a[0], ("dict",),  "dict_pick", 1, n)
    keys = _need(i, a[1], ("list",),  "dict_pick", 2, n)
    return {k: d[k] for k in keys if k in d}


def g_dict_omit(i, a, n):
    """dict_omit(dict, [keys]) — return new dict without specified keys."""
    d    = _need(i, a[0], ("dict",), "dict_omit", 1, n)
    keys = _need(i, a[1], ("list",), "dict_omit", 2, n)
    skip = set(keys)
    return {k: v for k, v in d.items() if k not in skip}


# ---------------------------------------------------------------------------
# string helpers — str_*
# ---------------------------------------------------------------------------

def g_str_len(i, a, n):
    return len(_need(i, a[0], ("string",), "str_len", 1, n))


def g_str_upper(i, a, n):
    return _need(i, a[0], ("string",), "str_upper", 1, n).upper()


def g_str_lower(i, a, n):
    return _need(i, a[0], ("string",), "str_lower", 1, n).lower()


def g_str_trim(i, a, n):
    s = _need(i, a[0], ("string",), "str_trim", 1, n)
    chars = _need(i, a[1], ("string",), "str_trim", 2, n) if len(a) > 1 else None
    return s.strip(chars) if chars else s.strip()


def g_str_ltrim(i, a, n):
    s = _need(i, a[0], ("string",), "str_ltrim", 1, n)
    chars = _need(i, a[1], ("string",), "str_ltrim", 2, n) if len(a) > 1 else None
    return s.lstrip(chars) if chars else s.lstrip()


def g_str_rtrim(i, a, n):
    s = _need(i, a[0], ("string",), "str_rtrim", 1, n)
    chars = _need(i, a[1], ("string",), "str_rtrim", 2, n) if len(a) > 1 else None
    return s.rstrip(chars) if chars else s.rstrip()


def g_str_replace(i, a, n):
    """str_replace(search, replace, text) — single or list-of-pairs substitution."""
    text   = _need(i, a[2], ("string",), "str_replace", 3, n)
    search = a[0]
    repl   = a[1]
    if isinstance(search, list) and isinstance(repl, list):
        for s, r in zip(search, repl):
            _need(i, s, ("string",), "str_replace", 1, n)
            _need(i, r, ("string",), "str_replace", 2, n)
            text = text.replace(s, r)
        return text
    _need(i, search, ("string",), "str_replace", 1, n)
    _need(i, repl, ("string",), "str_replace", 2, n)
    return text.replace(search, repl)


def g_str_contains(i, a, n):
    s      = _need(i, a[0], ("string",), "str_contains", 1, n)
    needle = _need(i, a[1], ("string",), "str_contains", 2, n)
    return needle in s


def g_str_starts(i, a, n):
    s      = _need(i, a[0], ("string",), "str_starts", 1, n)
    prefix = _need(i, a[1], ("string",), "str_starts", 2, n)
    return s.startswith(prefix)


def g_str_ends(i, a, n):
    s      = _need(i, a[0], ("string",), "str_ends", 1, n)
    suffix = _need(i, a[1], ("string",), "str_ends", 2, n)
    return s.endswith(suffix)


def g_str_split(i, a, n):
    s   = _need(i, a[0], ("string",), "str_split", 1, n)
    sep = _need(i, a[1], ("string",), "str_split", 2, n) if len(a) > 1 else None
    if sep == "":
        return list(s)
    return s.split(sep) if sep is not None else s.split()


def g_str_join(i, a, n):
    lst  = _need(i, a[0], ("list",),   "str_join", 1, n)
    glue = _need(i, a[1], ("string",), "str_join", 2, n) if len(a) > 1 else ""
    return glue.join(to_display(x, i) for x in lst)


def g_str_pad(i, a, n):
    s     = _need(i, a[0], ("string",), "str_pad", 1, n)
    width = _need(i, a[1], ("int",),    "str_pad", 2, n)
    fill  = _need(i, a[2], ("string",), "str_pad", 3, n) if len(a) > 2 else " "
    side  = _need(i, a[3], ("string",), "str_pad", 4, n) if len(a) > 3 else "right"
    if len(fill) != 1:
        i.throw("ValueException", "str_pad() fill must be a single character", n)
    if side == "left":
        return s.rjust(width, fill)
    if side == "both":
        return s.center(width, fill)
    return s.ljust(width, fill)


def g_str_repeat(i, a, n):
    s = _need(i, a[0], ("string",), "str_repeat", 1, n)
    c = _need(i, a[1], ("int",),    "str_repeat", 2, n)
    if c < 0:
        i.throw("ValueException", "str_repeat() count cannot be negative", n)
    return s * c


def g_str_slug(i, a, n):
    return _slugify(_need(i, a[0], ("string",), "str_slug", 1, n))


def g_str_reverse(i, a, n):
    return _need(i, a[0], ("string",), "str_reverse", 1, n)[::-1]


def g_str_substr(i, a, n):
    s     = _need(i, a[0], ("string",), "str_substr", 1, n)
    start = _need(i, a[1], ("int",),    "str_substr", 2, n)
    if len(a) > 2:
        length = _need(i, a[2], ("int",), "str_substr", 3, n)
        return s[start:start + length] if start >= 0 else s[start:len(s) + start + length]
    return s[start:]


def g_str_index(i, a, n):
    """str_index(text, needle) — first position of needle, or null."""
    s      = _need(i, a[0], ("string",), "str_index", 1, n)
    needle = _need(i, a[1], ("string",), "str_index", 2, n)
    pos = s.find(needle)
    return pos if pos != -1 else None


def g_str_last_index(i, a, n):
    """str_last_index(text, needle) — last position of needle, or null."""
    s      = _need(i, a[0], ("string",), "str_last_index", 1, n)
    needle = _need(i, a[1], ("string",), "str_last_index", 2, n)
    pos = s.rfind(needle)
    return pos if pos != -1 else None


def g_str_count(i, a, n):
    """str_count(text, needle) — number of non-overlapping occurrences."""
    s      = _need(i, a[0], ("string",), "str_count", 1, n)
    needle = _need(i, a[1], ("string",), "str_count", 2, n)
    return s.count(needle)


def g_str_words(i, a, n):
    """str_words(text) — split on whitespace, return list of words."""
    return _need(i, a[0], ("string",), "str_words", 1, n).split()


def g_str_word_count(i, a, n):
    return len(_need(i, a[0], ("string",), "str_word_count", 1, n).split())


def g_str_capitalize(i, a, n):
    s = _need(i, a[0], ("string",), "str_capitalize", 1, n)
    return s[:1].upper() + s[1:]


def g_str_title(i, a, n):
    return _need(i, a[0], ("string",), "str_title", 1, n).title()


def g_str_format(i, a, n):
    """str_format(template, ...values) — replace %s placeholders in order."""
    tmpl = _need(i, a[0], ("string",), "str_format", 1, n)
    result = tmpl
    for v in a[1:]:
        result = result.replace("%s", to_display(v, i), 1)
    return result


def g_str_is_empty(i, a, n):
    return len(_need(i, a[0], ("string",), "str_is_empty", 1, n)) == 0


def g_str_to_list(i, a, n):
    """str_to_list(text) — split string into list of characters."""
    return list(_need(i, a[0], ("string",), "str_to_list", 1, n))


def g_str_lines(i, a, n):
    """str_lines(text) — split text into lines."""
    return _need(i, a[0], ("string",), "str_lines", 1, n).splitlines()


def g_str_before(i, a, n):
    """str_before(text, delimiter) — substring before first occurrence of delimiter."""
    s = _need(i, a[0], ("string",), "str_before", 1, n)
    d = _need(i, a[1], ("string",), "str_before", 2, n)
    pos = s.find(d)
    return s[:pos] if pos != -1 else s


def g_str_after(i, a, n):
    """str_after(text, delimiter) — substring after first occurrence of delimiter."""
    s = _need(i, a[0], ("string",), "str_after", 1, n)
    d = _need(i, a[1], ("string",), "str_after", 2, n)
    pos = s.find(d)
    return s[pos + len(d):] if pos != -1 else ""


def g_str_between(i, a, n):
    """str_between(text, start, end) — substring between two delimiters."""
    s  = _need(i, a[0], ("string",), "str_between", 1, n)
    st = _need(i, a[1], ("string",), "str_between", 2, n)
    en = _need(i, a[2], ("string",), "str_between", 3, n)
    p1 = s.find(st)
    if p1 == -1:
        return ""
    p1 += len(st)
    p2 = s.find(en, p1)
    return s[p1:p2] if p2 != -1 else ""


# ---------------------------------------------------------------------------
# regex helpers — rx_*
# ---------------------------------------------------------------------------

def g_rx_match(i, a, n):
    """rx_match(pattern, text) — True if pattern matches anywhere in text."""
    pat  = _need(i, a[0], ("string",), "rx_match", 1, n)
    text = _need(i, a[1], ("string",), "rx_match", 2, n)
    try:
        p, f = _barq_pattern(pat, "rx_match", i, n)
        return bool(_re.search(p, text, f))
    except _re.error as e:
        i.throw("ValueException", f"rx_match() invalid pattern: {e}", n)


def g_rx_test(i, a, n):
    """rx_test(pattern, text) — True if entire text matches pattern."""
    pat  = _need(i, a[0], ("string",), "rx_test", 1, n)
    text = _need(i, a[1], ("string",), "rx_test", 2, n)
    try:
        p, f = _barq_pattern(pat, "rx_test", i, n)
        return bool(_re.fullmatch(p, text, f))
    except _re.error as e:
        i.throw("ValueException", f"rx_test() invalid pattern: {e}", n)


def g_rx_find(i, a, n):
    """rx_find(pattern, text) — capture groups of first match, or null."""
    pat  = _need(i, a[0], ("string",), "rx_find", 1, n)
    text = _need(i, a[1], ("string",), "rx_find", 2, n)
    try:
        p, f = _barq_pattern(pat, "rx_find", i, n)
        m = _re.search(p, text, f)
        if m is None:
            return None
        groups = list(m.groups())
        return groups if groups else [m.group(0)]
    except _re.error as e:
        i.throw("ValueException", f"rx_find() invalid pattern: {e}", n)


def g_rx_find_all(i, a, n):
    """rx_find_all(pattern, text) — list of all matches (or capture groups)."""
    pat  = _need(i, a[0], ("string",), "rx_find_all", 1, n)
    text = _need(i, a[1], ("string",), "rx_find_all", 2, n)
    try:
        p, f = _barq_pattern(pat, "rx_find_all", i, n)
        matches = _re.findall(p, text, f)
        return [list(m) if isinstance(m, tuple) else m for m in matches]
    except _re.error as e:
        i.throw("ValueException", f"rx_find_all() invalid pattern: {e}", n)


def g_rx_replace(i, a, n):
    """rx_replace(pattern, replacement, text) — replace all matches."""
    pat  = _need(i, a[0], ("string",), "rx_replace", 1, n)
    repl = _need(i, a[1], ("string",), "rx_replace", 2, n)
    text = _need(i, a[2], ("string",), "rx_replace", 3, n)
    try:
        p, f = _barq_pattern(pat, "rx_replace", i, n)
        return _re.sub(p, repl, text, flags=f)
    except _re.error as e:
        i.throw("ValueException", f"rx_replace() invalid pattern: {e}", n)


def g_rx_split(i, a, n):
    """rx_split(pattern, text) — split text by regex."""
    pat  = _need(i, a[0], ("string",), "rx_split", 1, n)
    text = _need(i, a[1], ("string",), "rx_split", 2, n)
    try:
        p, f = _barq_pattern(pat, "rx_split", i, n)
        return _re.split(p, text, flags=f)
    except _re.error as e:
        i.throw("ValueException", f"rx_split() invalid pattern: {e}", n)


def g_rx_count(i, a, n):
    """rx_count(pattern, text) — number of non-overlapping matches."""
    pat  = _need(i, a[0], ("string",), "rx_count", 1, n)
    text = _need(i, a[1], ("string",), "rx_count", 2, n)
    try:
        p, f = _barq_pattern(pat, "rx_count", i, n)
        return len(_re.findall(p, text, f))
    except _re.error as e:
        i.throw("ValueException", f"rx_count() invalid pattern: {e}", n)


# ---------------------------------------------------------------------------
# object / class reflection
# ---------------------------------------------------------------------------

def g_has_method(i, a, n):
    """has_method(obj, name) — True if object has that method."""
    obj  = a[0]
    name = _need(i, a[1], ("string",), "has_method", 2, n)
    if isinstance(obj, BarqObject):
        return obj.cls.find_method(name) is not None
    return False


def g_has_prop(i, a, n):
    """has_prop(obj, name) — True if object has that property."""
    obj  = a[0]
    name = _need(i, a[1], ("string",), "has_prop", 2, n)
    if isinstance(obj, BarqObject):
        return obj.cls.find_prop(name) is not None
    return False


def g_has_class(i, a, n):
    """has_class(name) — True if a class with that name is defined."""
    name = _need(i, a[0], ("string",), "has_class", 1, n)
    return name in i.classes


def g_class_of(i, a, n):
    """class_of(value) — class name of an object, or built-in type name."""
    v = a[0]
    if isinstance(v, BarqObject):
        return v.cls.name
    return type_name(v)


def g_instance_of(i, a, n):
    """instance_of(obj, ClassName) — True if obj is an instance of that class."""
    obj  = a[0]
    name = _need(i, a[1], ("string",), "instance_of", 2, n)
    if not isinstance(obj, BarqObject):
        return False
    cls = i.classes.get(name)
    if cls is None:
        return False
    return obj.cls.is_subclass_of(cls)


def g_prop_names(i, a, n):
    """prop_names(obj) — list of public property names."""
    obj = a[0]
    if not isinstance(obj, BarqObject):
        i.throw("TypeException", f"prop_names() needs an object, got {type_name(obj)}", n)
    return [name for name, p in obj.cls.props.items() if p.visibility == "public"]


def g_method_names(i, a, n):
    """method_names(obj) — list of method names defined on the class."""
    obj = a[0]
    if not isinstance(obj, BarqObject):
        i.throw("TypeException", f"method_names() needs an object, got {type_name(obj)}", n)
    cls = obj.cls
    names = set()
    while cls is not None:
        names.update(cls.methods.keys())
        cls = cls.parent
    return sorted(names)


def g_to_dict(i, a, n):
    """to_dict(obj) — convert object public properties to a dict."""
    obj = a[0]
    if isinstance(obj, BarqObject):
        return {k: v for k, v in obj.fields.items()
                if obj.cls.find_prop(k) and obj.cls.find_prop(k).visibility == "public"}
    if isinstance(obj, dict):
        return dict(obj)
    i.throw("TypeException", f"to_dict() needs an object or dict, got {type_name(obj)}", n)


# ---------------------------------------------------------------------------
# value helpers
# ---------------------------------------------------------------------------

def g_coalesce(i, a, n):
    """coalesce(...values) — return first non-null value."""
    for v in a:
        if v is not None:
            return v
    return None


def g_val(i, a, n):
    """val(value, default) — return value if not null, else default."""
    return a[0] if a[0] is not None else (a[1] if len(a) > 1 else None)


def g_clone(i, a, n):
    """clone(value) — deep copy of a list, dict, or object."""
    v = a[0]
    if isinstance(v, (list, dict)):
        return _copy.deepcopy(v)
    if isinstance(v, BarqObject):
        from .runtime import BarqObject as _BO
        new_obj = _BO(v.cls)
        new_obj.fields = _copy.deepcopy(v.fields)
        return new_obj
    return v


def g_to_str(i, a, n):
    return to_display(a[0], i)


def g_to_int(i, a, n):
    from .builtins import cast
    return cast(i, _nodes.TypeRef("int", []), a[0], n)


def g_to_float(i, a, n):
    from .builtins import cast
    return cast(i, _nodes.TypeRef("float", []), a[0], n)


def g_to_bool(i, a, n):
    from .builtins import cast
    return cast(i, _nodes.TypeRef("bool", []), a[0], n)


def g_to_list_fn(i, a, n):
    from .builtins import cast
    return cast(i, _nodes.TypeRef("list", []), a[0], n)


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

MAGIC_FUNCTIONS = [
    # type checks
    ("is_null",       g_is_null,       1, 1),
    ("is_str",        g_is_str,        1, 1),
    ("is_int",        g_is_int,        1, 1),
    ("is_float",      g_is_float,      1, 1),
    ("is_num",        g_is_num,        1, 1),
    ("is_bool",       g_is_bool,       1, 1),
    ("is_list",       g_is_list,       1, 1),
    ("is_dict",       g_is_dict,       1, 1),
    ("is_fn",         g_is_fn,         1, 1),
    ("is_obj",        g_is_obj,        1, 1),
    ("is_set",        g_is_set,        1, 1),
    ("is_numeric",    g_is_numeric,    1, 1),
    ("is_empty",      g_is_empty,      1, 1),
    ("not_null",      g_not_null,      1, 1),
    # number helpers
    ("is_odd",        g_is_odd,        1, 1),
    ("is_even",       g_is_even,       1, 1),
    ("between",       g_between,       3, 3),
    ("clamp",         g_clamp,         3, 3),
    ("sign",          g_sign,          1, 1),
    # collection — list
    ("in_list",       g_in_list,       2, 2),
    ("has_key",       g_has_key,       2, 2),
    ("list_search",   g_list_search,   2, 2),
    ("list_map",      g_list_map,      2, 2),
    ("list_filter",   g_list_filter,   2, 2),
    ("list_reduce",   g_list_reduce,   3, 3),
    ("list_each",     g_list_each,     2, 2),
    ("list_flatten",  g_list_flatten,  1, 1),
    ("list_unique",   g_list_unique,   1, 1),
    ("list_chunk",    g_list_chunk,    2, 2),
    ("list_zip",      g_list_zip,      2, 2),
    ("list_sum",      g_list_sum,      1, 1),
    ("list_count",    g_list_count,    1, 1),
    ("list_any",      g_list_any,      2, 2),
    ("list_all",      g_list_all,      2, 2),
    ("list_find",     g_list_find,     2, 2),
    ("list_first",    g_list_first,    1, 1),
    ("list_last",     g_list_last,     1, 1),
    ("list_slice",    g_list_slice,    2, 3),
    ("list_merge",    g_list_merge,    2, 2),
    ("list_reverse",  g_list_reverse,  1, 1),
    ("list_sort",     g_list_sort,     1, 2),
    ("list_contains", g_list_contains, 2, 2),
    # collection — dict
    ("dict_merge",    g_dict_merge,    2, 2),
    ("dict_keys",     g_dict_keys,     1, 1),
    ("dict_values",   g_dict_values,   1, 1),
    ("dict_has",      g_dict_has,      2, 2),
    ("dict_get",      g_dict_get,      2, 3),
    ("dict_pick",     g_dict_pick,     2, 2),
    ("dict_omit",     g_dict_omit,     2, 2),
    # string helpers
    ("str_len",       g_str_len,       1, 1),
    ("str_upper",     g_str_upper,     1, 1),
    ("str_lower",     g_str_lower,     1, 1),
    ("str_trim",      g_str_trim,      1, 2),
    ("str_ltrim",     g_str_ltrim,     1, 2),
    ("str_rtrim",     g_str_rtrim,     1, 2),
    ("str_replace",   g_str_replace,   3, 3),
    ("str_contains",  g_str_contains,  2, 2),
    ("str_starts",    g_str_starts,    2, 2),
    ("str_ends",      g_str_ends,      2, 2),
    ("str_split",     g_str_split,     1, 2),
    ("str_join",      g_str_join,      1, 2),
    ("str_pad",       g_str_pad,       2, 4),
    ("str_repeat",    g_str_repeat,    2, 2),
    ("str_slug",      g_str_slug,      1, 1),
    ("str_reverse",   g_str_reverse,   1, 1),
    ("str_substr",    g_str_substr,    2, 3),
    ("str_index",     g_str_index,     2, 2),
    ("str_last_index",g_str_last_index,2, 2),
    ("str_count",     g_str_count,     2, 2),
    ("str_words",     g_str_words,     1, 1),
    ("str_word_count",g_str_word_count,1, 1),
    ("str_capitalize",g_str_capitalize,1, 1),
    ("str_title",     g_str_title,     1, 1),
    ("str_format",    g_str_format,    1, VARIADIC),
    ("str_is_empty",  g_str_is_empty,  1, 1),
    ("str_to_list",   g_str_to_list,   1, 1),
    ("str_lines",     g_str_lines,     1, 1),
    ("str_before",    g_str_before,    2, 2),
    ("str_after",     g_str_after,     2, 2),
    ("str_between",   g_str_between,   3, 3),
    # regex
    ("rx_match",      g_rx_match,      2, 2),
    ("rx_test",       g_rx_test,       2, 2),
    ("rx_find",       g_rx_find,       2, 2),
    ("rx_find_all",   g_rx_find_all,   2, 2),
    ("rx_replace",    g_rx_replace,    3, 3),
    ("rx_split",      g_rx_split,      2, 2),
    ("rx_count",      g_rx_count,      2, 2),
    # reflection
    ("has_method",    g_has_method,    2, 2),
    ("has_prop",      g_has_prop,      2, 2),
    ("has_class",     g_has_class,     1, 1),
    ("class_of",      g_class_of,      1, 1),
    ("instance_of",   g_instance_of,   2, 2),
    ("prop_names",    g_prop_names,    1, 1),
    ("method_names",  g_method_names,  1, 1),
    ("to_dict",       g_to_dict,       1, 1),
    # value helpers
    ("coalesce",      g_coalesce,      1, VARIADIC),
    ("val",           g_val,           1, 2),
    ("clone",         g_clone,         1, 1),
    ("to_str",        g_to_str,        1, 1),
    ("to_int",        g_to_int,        1, 1),
    ("to_float",      g_to_float,      1, 1),
    ("to_bool",       g_to_bool,       1, 1),
    ("to_list",       g_to_list_fn,    1, 1),
]


def install(i):
    g = i.builtins
    for name, fn, lo, hi in MAGIC_FUNCTIONS:
        g.declare(name, NativeFunction(name, fn, lo, hi), None, const=True)
