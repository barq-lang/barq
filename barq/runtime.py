"""Runtime value types and helpers shared by the interpreter and builtins.

Barq values map onto Python values like this:
    int -> int, float -> float, string -> str, bool -> bool, null -> None,
    list -> list, dict -> dict, set -> BarqSet, range(...) -> range,
    functions -> BarqFunction / NativeFunction / BoundMethod,
    classes -> BarqClass, objects -> BarqObject, Math/Str -> NativeModule
"""

import math


# -- control-flow signals (Python exceptions used internally) ----------------

class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class ReturnSignal(Exception):
    def __init__(self, value, node=None):
        self.value = value
        self.node = node


class ExitSignal(Exception):
    def __init__(self, code):
        self.code = code


class BarqThrow(Exception):
    """A Barq exception travelling up the Python stack."""

    def __init__(self, obj):
        super().__init__(obj)
        self.obj = obj


# -- values ------------------------------------------------------------------

class BarqSet:
    """Insertion-ordered set (Python's set has no stable print order)."""

    __slots__ = ("items",)

    def __init__(self, values=()):
        self.items = {}
        for v in values:
            self.items[hash_key(v)] = v

    def add(self, v):
        self.items[hash_key(v)] = v

    def has(self, v):
        return hash_key(v) in self.items

    def remove(self, v):
        self.items.pop(hash_key(v), None)

    def values(self):
        return list(self.items.values())

    def __len__(self):
        return len(self.items)


def hash_key(v):
    """Key used for sets and dicts, so that true and 1 stay distinct."""
    if isinstance(v, bool):
        return ("bool", v)
    if isinstance(v, (int, float, str)) or v is None:
        return v
    if isinstance(v, BarqObject):
        return ("obj", id(v))
    raise TypeError(type_name(v))


class Env:
    __slots__ = ("vars", "parent", "this", "cls", "func")

    def __init__(self, parent=None, this=None, cls=None, func=None):
        self.vars = {}
        self.parent = parent
        self.this = this     # set on method call environments
        self.cls = cls       # class whose code is running (for visibility)
        self.func = func     # function name, for traces and __method__

    def find(self, name):
        env = self
        while env is not None:
            entry = env.vars.get(name)
            if entry is not None:
                return entry
            env = env.parent
        return None

    def declare(self, name, value, type_ref=None, const=False):
        self.vars[name] = [value, type_ref, const]

    def context(self):
        """Nearest (this, cls, func) from enclosing method calls."""
        env = self
        while env is not None:
            if env.cls is not None or env.func is not None:
                return env.this, env.cls, env.func
            env = env.parent
        return None, None, None


class Module:
    def __init__(self, path, source, env):
        self.path = path
        self.source = source
        self.env = env
        self.namespace = ""
        self.classes = {}
        self.functions = {}

    def qualify(self, name):
        return f"{self.namespace}\\{name}" if self.namespace else name


class BarqFunction:
    code = None      # compiled Python function (VM); None for tree-walked functions
    nparams = -1

    def __init__(self, name, params, ret, body, closure, module, is_expr=False,
                 visibility=None, is_static=False, is_abstract=False, owner=None, line=0):
        self.name = name
        self.params = params
        self.ret = ret
        self.body = body
        self.closure = closure
        self.module = module
        self.is_expr = is_expr
        self.visibility = visibility or "public"
        self.is_static = is_static
        self.is_abstract = is_abstract
        self.owner = owner        # BarqClass for methods
        self.line = line
        # simple: every parameter must be passed positionally (no defaults, no
        # *variadic), so calls can skip argument binding.
        self.simple = not params or not any(p.default is not None or p.variadic for p in params)
        self.trait = None         # trait the method was copied from
        self.trait_method = None  # its name in that trait (differs for `hello as hi`)


class NativeFunction:
    def __init__(self, name, fn, min_args=0, max_args=None):
        self.name = name
        self.fn = fn              # fn(interp, args, node) -> value
        self.min_args = min_args
        self.max_args = min_args if max_args is None else max_args
        self.visibility = "public"
        self.is_static = False
        self.is_abstract = False
        self.owner = None
        self.late_static = False   # static that receives the class it was called on
        self.arg_names = None      # parameter names: lets Barq code pass named arguments


class KwArgs:
    """Keyword-only named arguments of a variadic built-in (see Interpreter.native_kwargs)."""
    __slots__ = ("values",)

    def __init__(self, values):
        self.values = values


def split_kwargs(args):
    """-> (positional args, {name: value}) for a built-in that may get a trailing KwArgs."""
    if args and isinstance(args[-1], KwArgs):
        return list(args[:-1]), args[-1].values
    return list(args), {}


class BoundMethod:
    __slots__ = ("this", "func")

    def __init__(self, this, func):
        self.this = this
        self.func = func

    @property
    def name(self):
        return self.func.name


class PropInfo:
    __slots__ = ("name", "type", "visibility", "default", "owner", "is_static", "const", "module")

    def __init__(self, name, type_ref, visibility, default, owner, is_static=False, const=False, module=None):
        self.name = name
        self.type = type_ref
        self.visibility = visibility
        self.default = default
        self.owner = owner
        self.is_static = is_static
        self.const = const
        self.module = module      # file the default belongs to, when not the owner's (traits)


class BarqClass:
    def __init__(self, name, fqn, module, parent=None, is_abstract=False, native=False):
        self.name = name
        self.fqn = fqn
        self.module = module
        self.parent = parent
        self.is_abstract = is_abstract
        self.native = native
        self.methods = {}        # name -> BarqFunction | NativeFunction
        self.props = {}          # instance properties declared here: name -> PropInfo
        self.statics = {}        # static props/consts declared here: name -> PropInfo
        self.static_values = {}  # name -> value
        self.decl = None
        self.is_enum = False
        self.backing = None      # "int" / "string" for backed enums
        self.cases = {}          # enum case name -> BarqObject (one object per case)
        self._members = {}       # VM lookup cache: name -> member entry (see vm.member_entry)
        self.kind = "class"      # "class" | "interface" | "trait" | "enum"
        self.interfaces = []     # interfaces this class implements (an interface: the ones it extends)
        self.finished = False

    @property
    def is_interface(self):
        return self.kind == "interface"

    @property
    def is_trait(self):
        return self.kind == "trait"

    def find_method(self, name):
        cls = self
        while cls is not None:
            m = cls.methods.get(name)
            if m is not None:
                return m
            cls = cls.parent
        return None

    def find_prop(self, name):
        cls = self
        while cls is not None:
            p = cls.props.get(name)
            if p is not None:
                return p
            cls = cls.parent
        return None

    def find_static(self, name):
        cls = self
        while cls is not None:
            p = cls.statics.get(name)
            if p is not None:
                return p
            for iface in cls.interfaces:      # interface constants
                p = iface.find_static(name)
                if p is not None:
                    return p
            cls = cls.parent
        return None

    def is_subclass_of(self, other):
        cls = self
        while cls is not None:
            if cls is other:
                return True
            if cls.interfaces and other.kind == "interface":
                for iface in cls.interfaces:
                    if iface.is_subclass_of(other):
                        return True
            cls = cls.parent
        return False

    def all_interfaces(self):
        """Every interface this class implements, directly or through parents
        and interfaces that extend others."""
        out = []
        todo = []
        for c in self.chain():
            todo.extend(c.interfaces)
        while todo:
            i = todo.pop(0)
            if i not in out:
                out.append(i)
                todo.extend(i.interfaces)
        return out

    def chain(self):
        """Classes from the root ancestor down to this one."""
        out = []
        cls = self
        while cls is not None:
            out.append(cls)
            cls = cls.parent
        return list(reversed(out))

    def abstract_methods(self):
        missing = {}
        for cls in self.chain():
            for name, m in cls.methods.items():
                if m.is_abstract:
                    missing[name] = cls
                else:
                    missing.pop(name, None)
        return missing


class BarqObject:
    __slots__ = ("cls", "fields", "trace", "origin", "native")

    def __init__(self, cls):
        self.cls = cls
        self.fields = {}
        self.trace = None    # only used by exception objects
        self.origin = None
        self.native = None   # hidden Python state of stdlib objects (db connection...)


class NativeModule:
    def __init__(self, name, members):
        self.name = name
        self.members = members


# -- type names & checks -----------------------------------------------------

PRIMITIVES = {"int", "float", "string", "bool"}
NON_NULLABLE = {"int", "float", "bool"}


def type_name(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    if isinstance(v, BarqSet):
        return "set"
    if isinstance(v, range):
        return "range"
    if isinstance(v, BarqObject):
        return v.cls.name
    if isinstance(v, BarqClass):
        return "class"
    if isinstance(v, (BarqFunction, NativeFunction, BoundMethod)):
        return "function"
    if isinstance(v, NativeModule):
        return "module"
    return type(v).__name__


def type_str(t):
    if t is None:
        return "any"
    q = "?" if t.nullable else ""
    if t.args:
        return f"{q}{t.name}<{', '.join(type_str(a) for a in t.args)}>"
    return q + t.name


# -- formatting --------------------------------------------------------------

def format_float(f):
    if math.isnan(f):
        return "NAN"
    if math.isinf(f):
        return "INF" if f > 0 else "-INF"
    if f == int(f) and abs(f) < 1e16:
        return f"{int(f)}.0"
    return repr(f)


def to_display(v, interp=None, quote=False):
    """String form used by print, interpolation and ~string()."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return format_float(v)
    if isinstance(v, str):
        if quote:
            escaped = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
            return f'"{escaped}"'
        return v
    if isinstance(v, list):
        return "#[" + ", ".join(to_display(x, interp, True) for x in v) + "]"
    if isinstance(v, dict):
        return "#{" + ", ".join(f"{to_display(k, interp, True)}: {to_display(x, interp, True)}" for k, x in v.items()) + "}"
    if isinstance(v, BarqSet):
        return "#(" + ", ".join(to_display(x, interp, True) for x in v.values()) + ")"
    if isinstance(v, range):
        if v.step == 1:
            return f"range({v.start}, {v.stop})"
        return f"range({v.start}, {v.stop}, {v.step})"
    if isinstance(v, BarqObject):
        if interp is not None:
            method = v.cls.find_method("__toString")
            if method is not None:
                result = interp.call_function(BoundMethod(v, method), [], None)
                if not isinstance(result, str):
                    interp.throw("TypeException", f"{v.cls.name}.__toString() must return string, got {type_name(result)}")
                return result
        if v.cls.is_enum:
            return f"{v.cls.name}::{v.fields['name']}"
        return f"<{v.cls.name} object>"
    if isinstance(v, BarqClass):
        return f"<class {v.fqn}>"
    if isinstance(v, (BarqFunction, NativeFunction, BoundMethod)):
        return f"<function {v.name}>"
    if isinstance(v, NativeModule):
        return f"<module {v.name}>"
    return str(v)


def dump_value(v, interp=None, indent=0):
    """Detailed, multi-line form used by dump() and dd()."""
    pad = "    " * indent
    inner = "    " * (indent + 1)
    if isinstance(v, list) and v:
        rows = [f"{inner}{i}: {dump_value(x, interp, indent + 1)}" for i, x in enumerate(v)]
        return f"list({len(v)}) #[\n" + "\n".join(rows) + f"\n{pad}]"
    if isinstance(v, dict) and v:
        rows = [f"{inner}{to_display(k, interp, True)}: {dump_value(x, interp, indent + 1)}" for k, x in v.items()]
        return f"dict({len(v)}) #{{\n" + "\n".join(rows) + f"\n{pad}}}"
    if isinstance(v, BarqObject) and v.cls.is_enum:
        if v.cls.backing:
            return f"enum({v.cls.name}::{v.fields['name']}: {to_display(v.fields['value'], interp, True)})"
        return f"enum({v.cls.name}::{v.fields['name']})"
    if isinstance(v, BarqObject):
        rows = []
        extra = getattr(v.native, "attrs", None)
        if isinstance(extra, dict):   # model columns and loaded relations
            for name, x in list(extra.items()) + list(getattr(v.native, "relations", {}).items()):
                rows.append(f"{inner}{name}: {dump_value(x, interp, indent + 1)}")
        for cls in v.cls.chain():
            for name, p in cls.props.items():
                mark = {"public": "+", "protected": "#", "private": "-"}[p.visibility]
                rows.append(f"{inner}{mark}{name}: {dump_value(v.fields.get(name), interp, indent + 1)}")
        if not rows:
            return f"{v.cls.name} {{}}"
        return f"{v.cls.name} {{\n" + "\n".join(rows) + f"\n{pad}}}"
    if isinstance(v, str):
        return f"string({len(v)}) {to_display(v, interp, True)}"
    if isinstance(v, bool) or v is None:
        return to_display(v)
    if isinstance(v, (int, float)):
        return f"{type_name(v)}({to_display(v)})"
    return to_display(v, interp, True)


# -- equality ----------------------------------------------------------------

def values_equal(a, b):
    """Strict equality: values of different types are never equal, except
    that int and float compare by numeric value."""
    if a is None or b is None:
        return a is None and b is None
    ta, tb = type(a), type(b)
    if ta is bool or tb is bool:
        return ta is tb and a == b
    if ta in (int, float) and tb in (int, float):
        return a == b
    if ta is not tb:
        if isinstance(a, BarqObject) and isinstance(b, BarqObject):
            return a is b
        return False
    if ta is list:
        return len(a) == len(b) and all(values_equal(x, y) for x, y in zip(a, b))
    if ta is dict:
        if len(a) != len(b):
            return False
        for k, x in a.items():
            if k not in b or not values_equal(x, b[k]):
                return False
        return True
    if ta is BarqSet:
        return len(a) == len(b) and all(k in b.items for k in a.items)
    if ta is BarqObject:
        return a is b
    return a == b
