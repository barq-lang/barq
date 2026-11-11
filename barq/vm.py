"""The Barq VM: runs files compiled by compiler.py on CPython's bytecode VM.

`VM` is a drop-in replacement for the tree-walking `Interpreter`: the standard
library, builtins and error reporting all talk to it through the same methods.
What changes is how code runs:

* Each file is compiled once to a Python code object and cached as a `.bqc`
  file under `<project>/.barq/cache/`. The next run skips parsing, checking and
  compiling when the source has not changed.
* Barq functions get a `code` attribute: the compiled Python function. Calls
  between compiled functions are plain Python calls.
* Nothing tracks the current line while code runs. When an error is raised,
  the VM looks at the Python frames that belong to Barq code and reads the
  source position of the instruction each one is running. That gives the
  error location and the call trace for free.
"""

import functools
import hashlib
import importlib.util
import marshal
import math
import os
import pickle
import sys
import warnings

from . import __version__
from . import nodes as N
from .builtins import METHOD_TABLES, cast as builtin_cast
from .compiler import COMPILER_VERSION, CompiledModule, Compiler
from .errors import CheckError, SourceFile
from .interpreter import IMPORT_PENDING, Interpreter
from .runtime import (
    BarqClass, BarqFunction, BarqObject, BarqSet, BarqThrow, BoundMethod, Env, Module,
    NativeFunction, NativeModule, hash_key, to_display, type_name, values_equal,
)

CACHE_DIR = os.path.join(".barq", "cache")
CACHE_MAGIC = f"barq-{__version__}-c{COMPILER_VERSION}-".encode() + importlib.util.MAGIC_NUMBER
MAX_DEPTH = 5000


class SwitchBreak(Exception):
    """`break` inside a switch case (but not at its end)."""


class TracebackFrame:
    """A frame seen through a traceback entry (its instruction position is
    the one that raised, not wherever the frame is now)."""

    __slots__ = ("frame", "f_code", "pos")

    def __init__(self, vm, tb):
        self.frame = tb.tb_frame
        self.f_code = tb.tb_frame.f_code
        code = self.f_code
        table = vm._positions.get(code)
        if table is None:
            table = vm._positions[code] = list(code.co_positions())
        i = tb.tb_lasti // 2
        line, col = tb.tb_lineno or 0, 0
        if 0 <= i < len(table) and table[i][0] is not None:
            line, col = table[i][0], table[i][2] or 0
        self.pos = (line, col)


class CtxEnv:
    """Stands in for an Env when the tree-walker's member-access code needs the
    calling context (for visibility checks and parent::method())."""

    __slots__ = ("this", "cls")

    def __init__(self, cls, this):
        self.cls = cls
        self.this = this

    def context(self):
        return self.this, self.cls, None


def can_access(visibility, owner, ctx):
    if visibility == "public":
        return True
    if ctx is None:
        return False
    if visibility == "private":
        return ctx is owner
    return ctx.is_subclass_of(owner) or owner.is_subclass_of(ctx)


PY_TYPES = {"int": int, "float": float, "bool": bool, "string": str}


def member_entry(cls, name):
    """Look up obj.name once per class and remember the answer:
    (0, PropInfo, public, python type, nullable, untyped) for a property,
    (1, method, public) for a method, (2, None, False) when there is neither."""
    p = cls.find_prop(name)
    if p is not None:
        t = p.type
        simple = t is not None and not t.args
        hit = (0, p, p.visibility == "public", PY_TYPES.get(t.name) if simple else None,
               t is None or t.name not in ("int", "float", "bool"), t is None or t.name == "any")
    else:
        m = cls.find_method(name)
        hit = (1, m, m.visibility == "public") if m is not None else (2, None, False)
    cls._members[name] = hit
    return hit


def forget_lookups(classes):
    """Drop cached lookups once classes are finished (they may have been
    looked at half-built, e.g. by a static default that runs during setup)."""
    for cls in classes:
        cls._members.clear()
        cls.__dict__.pop("_plan", None)


class GlobalCache(dict):
    """Names that are not local variables: module functions, classes, imports
    and builtins. They never change once found, so each is resolved once."""

    def __init__(self, vm, module):
        super().__init__()
        self.vm = vm
        self.module = module

    def __missing__(self, name):
        entry = self.module.env.find(name)
        if entry is not None and entry[0] is not IMPORT_PENDING:
            value = entry[0]
        else:
            value = self.vm.resolve_name(name, self.module, None)
            if value is None:
                self.vm.throw("NameException", f"'{name}' is not defined")
        self[name] = value
        return value


class ClassCache(dict):
    def __init__(self, vm, module):
        super().__init__()
        self.vm = vm
        self.module = module

    def __missing__(self, name):
        cls = self.vm.resolve_class(name, self.module, None)
        self[name] = cls
        return cls


# ---------------------------------------------------------------------------
# runtime helpers shared by every module (closed over one VM)
# ---------------------------------------------------------------------------

def make_helpers(vm):
    throw = vm.throw
    binop = vm.binop
    call_function = vm.call_function
    H = vm.handling

    def call(f, args):
        t = type(f)
        if t is BarqFunction:
            code = f.code
            if code is not None and len(args) == f.nparams and (f.owner is None or f.is_static) and not f.is_abstract:
                return code(*args)
        elif t is NativeFunction:
            n = len(args)
            if n >= f.min_args and (f.max_args < 0 or n <= f.max_args):
                return f.fn(vm, args, None)
        return call_function(f, args, None)

    def callm(obj, name, args, ctx, this):
        t = type(obj)
        if t is BarqObject:
            hit = obj.cls._members.get(name) or member_entry(obj.cls, name)
            kind, m = hit[0], hit[1]
            if kind == 1:
                if m.visibility != "public" and not can_access(m.visibility, m.owner, ctx):
                    member(obj, name, ctx, this)   # raises the visibility error at the method name
                if m.is_static:
                    return call(m, args)
                if type(m) is BarqFunction:
                    code = m.code
                    if code is not None and len(args) == m.nparams and not m.is_abstract:
                        return code(obj, *args)
                    return call_function(BoundMethod(obj, m), args, None)
                n = len(args)
                if n >= m.min_args and (m.max_args < 0 or n <= m.max_args):
                    return m.fn(vm, args, None, obj)
                return call_function(BoundMethod(obj, m), args, None)
        else:
            table = METHOD_TABLES.get(t)
            if table is not None:
                entry = table.get(name)
                if entry is not None:
                    impl, lo, hi = entry
                    n = len(args)
                    if n >= lo and (hi is None or hi < 0 or n <= hi):
                        return impl(vm, obj, args, None)
            elif t is NativeModule:
                f = obj.members.get(name)
                if f is not None:
                    return call(f, args)
        return call_function(member(obj, name, ctx, this), args, None)

    def callk(f, args, kw):
        return call_function(f, args, None, kw)

    def callmk(obj, name, args, kw, ctx, this):
        return call_function(member(obj, name, ctx, this), args, None, kw)

    def newk(cls, args, kw, ctx, this):
        return vm.instantiate(cls, args, None, CtxEnv(ctx, this), kw)

    def spread(value):
        return vm.spread(value, None)

    def member(obj, name, ctx, this):
        """obj.name looked up for a method call; errors point at the name."""
        vm.at_name = True
        try:
            return vm.get_attr(obj, name, None, CtxEnv(ctx, this), call=True)
        finally:
            vm.at_name = False

    def getattr_(obj, name, ctx, this):
        if type(obj) is BarqObject:
            hit = obj.cls._members.get(name) or member_entry(obj.cls, name)
            if hit[0] == 0 and (hit[2] or can_access(hit[1].visibility, hit[1].owner, ctx)):
                return obj.fields[name]
        elif type(obj) is NativeModule:
            v = obj.members.get(name)
            if v is not None:
                return v
        return vm.get_attr(obj, name, None, CtxEnv(ctx, this))

    def sattr(obj, name, ctx, this):
        if obj is None:
            return None
        return getattr_(obj, name, ctx, this)

    def setattr_(obj, name, value, ctx, this):
        if type(obj) is BarqObject and not obj.cls.is_enum:
            hit = obj.cls._members.get(name) or member_entry(obj.cls, name)
            if hit[0] == 0 and (hit[2] or can_access(hit[1].visibility, hit[1].owner, ctx)):
                if not (hit[5] or type(value) is hit[3] or (value is None and hit[4])):
                    p = hit[1]
                    if not vm.matches(value, p.type, p.owner.module):
                        vm.check_assign(value, p.type, f"property '{obj.cls.name}.{name}'", None, p.owner.module)
                obj.fields[name] = value
                return
        vm.set_attr(obj, name, value, None, CtxEnv(ctx, this))

    def classof(value):
        if isinstance(value, (BarqClass, NativeModule)):
            return value
        throw("TypeException", f"'::' needs a class on the left, got {type_name(value)}")

    def parentof(cls):
        if cls.parent is None:
            throw("NameException", f"{cls.name} has no parent class")
        return cls.parent

    def static(cls, name, ctx, this, via_parent):
        if type(cls) is NativeModule:
            return vm.get_attr(cls, name, None, CtxEnv(ctx, this))
        return vm.get_static(cls, name, None, CtxEnv(ctx, this), via_parent)

    def setstatic(cls, name, value, ctx, this):
        if type(cls) is NativeModule:
            throw("TypeException", f"cannot set '{name}' on module {cls.name}")
        vm.set_static(cls, name, value, None, CtxEnv(ctx, this))

    def index(c, k):
        t = type(c)
        if t is list:
            if type(k) is int:
                try:
                    return c[k]
                except IndexError:
                    pass
        elif t is dict:
            try:
                return c[k]
            except (KeyError, TypeError):
                pass
        return vm.index(c, k, None)

    def setindex(c, k, v):
        t = type(c)
        if t is list:
            if type(k) is int and -len(c) <= k < len(c):
                c[k] = v
                return
        elif t is dict:
            tk = type(k)
            if tk is str or tk is int:
                c[k] = v
                return
        vm.set_index(c, k, v, None)

    def add(a, b):
        ta = type(a)
        if ta is type(b) and (ta is int or ta is float or ta is str or ta is list):
            return a + b
        return binop("+", a, b, None)

    def sub(a, b):
        ta = type(a)
        if ta is type(b) and (ta is int or ta is float):
            return a - b
        return binop("-", a, b, None)

    def mul(a, b):
        ta = type(a)
        if ta is type(b) and (ta is int or ta is float):
            return a * b
        return binop("*", a, b, None)

    def div(a, b):
        return binop("/", a, b, None)

    def mod(a, b):
        return binop("%", a, b, None)

    def pow_(a, b):
        return binop("**", a, b, None)

    def idiv(a, b):
        if b == 0:
            throw("DivisionByZeroException", "division by zero")
        if a >= 0 and b > 0:
            return a // b
        q = abs(a) // abs(b)
        return q if (a >= 0) == (b >= 0) else -q

    def fdiv(a, b):
        if b == 0:
            throw("DivisionByZeroException", "division by zero")
        return a / b

    def imod(a, b):
        if b == 0:
            throw("DivisionByZeroException", "modulo by zero")
        if a >= 0 and b > 0:
            return a % b
        r = abs(a) % abs(b)
        return r if a >= 0 else -r

    def fmod(a, b):
        if b == 0:
            throw("DivisionByZeroException", "modulo by zero")
        return math.fmod(a, b)

    def compare(op):
        def cmp(a, b):
            ta, tb = type(a), type(b)
            if (ta in (int, float) and tb in (int, float)) or (ta is str and tb is str):
                if op == "<":
                    return a < b
                if op == ">":
                    return a > b
                if op == "<=":
                    return a <= b
                return a >= b
            throw("TypeException", f"cannot compare {type_name(a)} {op} {type_name(b)}")
        return cmp

    def ne(a, b):
        return not values_equal(a, b)

    def contains(container, item):
        return vm.contains(container, item, None)

    def neg(x):
        if type(x) in (int, float):
            return -x
        throw("TypeException", f"unary '-' needs a number, got {type_name(x)}")

    def pos(x):
        if type(x) in (int, float):
            return x
        throw("TypeException", f"unary '+' needs a number, got {type_name(x)}")

    def not_(x):
        if x is True or x is False:
            return not x
        throw("TypeException", f"'not' needs a bool, got {type_name(x)}")

    def cond(value, what):
        if value is True or value is False:
            return value
        throw("TypeException",
              f"{what} must be bool, got {type_name(value)} — compare explicitly, e.g. "
              + ("'x != null'" if value is None else "'x != 0'" if isinstance(value, (int, float)) else "'s != \"\"'"
                 if isinstance(value, str) else "'items.len() > 0'" if isinstance(value, (list, dict, BarqSet)) else "'x != null'"))

    def disp(v):
        if type(v) is str:
            return v
        return to_display(v, vm)

    def dictlit(flat):
        out = {}
        for i in range(0, len(flat), 2):
            k = flat[i]
            try:
                hash_key(k)
            except TypeError:
                throw("TypeException", f"{type_name(k)} cannot be used as a dict key")
            out[k] = flat[i + 1]
        return out

    def setlit(items):
        s = BarqSet()
        for x in items:
            try:
                s.add(x)
            except TypeError:
                throw("TypeException", f"{type_name(x)} cannot be used as a set item")
        return s

    def iter_(v):
        t = type(v)
        if t is range:
            return v
        if t is list:
            return list(v)
        if t is dict:
            return list(v.keys())
        if t is BarqSet:
            return v.values()
        if t is str:
            return list(v)
        return vm.iterate(v, None, False)

    def iterp(v):
        return vm.iterate(v, None, True)

    def new(cls, args, ctx, this):
        if not cls.is_abstract and not cls.is_enum:
            hit = cls._members.get("__construct") or member_entry(cls, "__construct")
            if hit[0] == 2:
                if not args:
                    return vm.instantiate_bare(cls)
            elif hit[0] == 1 and hit[2]:
                m = hit[1]
                if type(m) is BarqFunction:
                    if m.code is not None and len(args) == m.nparams and not m.is_abstract:
                        obj = vm.instantiate_bare(cls)
                        m.code(obj, *args)
                        return obj
                else:
                    n = len(args)
                    if n >= m.min_args and (m.max_args < 0 or n <= m.max_args):
                        obj = vm.instantiate_bare(cls)
                        m.fn(vm, args, None, obj)
                        return obj
        return vm.instantiate(cls, args, None, CtxEnv(ctx, this))

    def cast(t, value):
        return builtin_cast(vm, t, value, None)

    def voidret(name, value):
        throw("TypeException", f"{name}() is #[void] but returned {type_name(value)}")

    def noret(name, ret):
        from .runtime import type_str
        throw("TypeException", f"{name}() must return {type_str(ret)} but reached the end without 'return'")

    def raise_(value):
        if not (isinstance(value, BarqObject) and value.cls.is_subclass_of(vm.classes["Exception"])):
            throw("TypeException", f"only Exception objects can be raised, got {type_name(value)}")
        vm.stamp(value)
        raise BarqThrow(value)

    def reraise():
        if not H:
            throw("RuntimeException", "'raise' with no value can only be used inside 'except'")
        raise BarqThrow(H[-1])

    def thrown(e):
        if type(e) is BarqThrow:
            return e.obj
        return vm.python_exception(e)

    def nothis():
        throw("NameException", "'this' can only be used inside a (non-static) method")

    def compile_error(message, line, col):
        m = vm.current_module()
        raise CheckError(message, m.source if m else None, line, col)

    return {
        "call": call, "callm": callm, "callk": callk, "callmk": callmk, "newk": newk, "spread": spread, "getattr": getattr_, "sattr": sattr, "setattr": setattr_,
        "classof": classof, "parentof": parentof, "static": static, "setstatic": setstatic,
        "index": index, "setindex": setindex,
        "add": add, "sub": sub, "mul": mul, "div": div, "mod": mod, "pow": pow_,
        "idiv": idiv, "fdiv": fdiv, "imod": imod, "fmod": fmod,
        "lt": compare("<"), "gt": compare(">"), "le": compare("<="), "ge": compare(">="),
        "eq": values_equal, "ne": ne, "contains": contains,
        "neg": neg, "pos": pos, "not_": not_, "cond": cond, "disp": disp,
        "dictlit": dictlit, "setlit": setlit, "Set": BarqSet, "iter": iter_, "iterp": iterp,
        "new": new, "cast": cast, "voidret": voidret, "noret": noret,
        "raise_": raise_, "reraise": reraise, "thrown": thrown, "nothis": nothis,
        "throw": throw, "compile_error": compile_error,
        "H": H, "Throw": BarqThrow, "Obj": BarqObject, "SwitchBreak": SwitchBreak, "CATCH": (BarqThrow, RecursionError, NameError),
    }


class ModuleRuntime:
    """The `_R` object a compiled module receives: shared helpers plus the
    parts that belong to this one file."""

    def __init__(self, vm, module, compiled):
        self.__dict__.update(vm.helpers)
        self.vm = vm
        self.module = module
        self.compiled = compiled
        self.T = compiled.consts
        self.G = GlobalCache(vm, module)
        self.CL = ClassCache(vm, module)
        self.K = module.classes
        self.path = module.path

        def m(value, t):
            return vm.matches(value, t, module)

        def is_(value, t):
            return value is not None and vm.matches(value, t, module)

        def tfail(value, t, what):
            vm.check_assign(value, t, what, None, module)

        def argfail(value, t, what, index):
            # Report a bad argument at the call site, not inside the function.
            vm.skip_frames = 1
            vm.skip_arg = index
            try:
                vm.check_assign(value, t, what, None, module)
            finally:
                vm.skip_frames = 0

        def mkfn(pyfn, decl):
            name = decl.name if isinstance(decl, N.FuncDecl) else "<lambda>"
            fn = BarqFunction(name, decl.params, decl.ret, decl.body, None, module,
                              is_expr=getattr(decl, "is_expr", False), line=decl.line)
            fn.code = pyfn
            fn.nparams = len(decl.params) if fn.simple else -1
            return fn

        self.m, self.is_, self.tfail, self.argfail, self.mkfn = m, is_, tfail, argfail, mkfn

    def attach(self, table):
        """Connect compiled functions to the Barq function and class objects
        made by setup, then finish the classes (as the tree-walker does)."""
        module = self.module
        for name, fn in module.functions.items():
            fn.nparams = len(fn.params) if fn.simple else -1
            fn.code = table.get(("f", name))
        for cls in module.classes.values():
            cls._compiled = table
        for cls in list(module.classes.values()):
            self.vm.finish_class(cls)
        forget_lookups(self.vm.classes.values())

    def export(self, local_vars):
        """Copy top-level variables into the module's Env (used by Config and
        anything else that reads a file's variables after it ran)."""
        env = self.module.env
        for name, pyname, t, const in self.compiled.exports:
            if pyname in local_vars and name not in env.vars:
                env.declare(name, local_vars[pyname], t, const=const)


# ---------------------------------------------------------------------------
# the VM
# ---------------------------------------------------------------------------

class VM(Interpreter):
    def __init__(self, *args, cache=True, **kwargs):
        self._module = None
        self.code_files = {}       # code filename -> Module
        self.code_names = {}       # code filename -> {python function name: display name}
        self.code_calls = {}       # code filename -> {call position: argument positions}
        self.code_vars = {}        # code filename -> {python variable name: Barq name}
        self.skip_arg = 0
        self.at_name = False
        self.at_arg = False        # bind_args() error: point at argument skip_arg of the call
        self._positions = {}
        self.skip_frames = 0
        self.use_cache = cache and not os.environ.get("BARQ_NO_CACHE")
        super().__init__(*args, **kwargs)
        self.helpers = make_helpers(self)

    # The module whose code is running: found from the Python frames, so the
    # compiled code never has to keep it up to date.
    @property
    def module(self):
        m = self.current_module(sys._getframe(1))
        return m if m is not None else self._module

    @module.setter
    def module(self, value):
        self._module = value

    def current_module(self, frame=None):
        f = frame or sys._getframe(1)
        files = self.code_files
        while f is not None:
            m = files.get(f.f_code.co_filename)
            if m is not None:
                return m
            f = f.f_back
        return self._module

    # -- locations and traces --------------------------------------------------

    def barq_frames(self):
        out = []
        f = sys._getframe(1)
        files = self.code_files
        while f is not None:
            m = files.get(f.f_code.co_filename)
            if m is not None:
                out.append((f, m))
            f = f.f_back
        return out

    def position(self, frame):
        if type(frame) is TracebackFrame:
            return frame.pos
        code = frame.f_code
        table = self._positions.get(code)
        if table is None:
            table = self._positions[code] = list(code.co_positions())
        i = frame.f_lasti // 2
        if 0 <= i < len(table):
            line, _, col, _ = table[i]
            if line is not None:
                return line, col or 0
        return frame.f_lineno or 0, 0

    def stamp(self, obj, node=None, frames=None):
        if obj.origin is not None:
            return
        if frames is None:
            frames = self.barq_frames()
        skip = self.skip_frames
        if skip and len(frames) > skip:
            frames = frames[skip:]
        tpl = getattr(node, "tpl_src", None)
        if tpl is not None:   # raised inside a template expression
            obj.origin = (tpl, node.line, node.col)
        elif node is not None and getattr(node, "line", 0):
            m = frames[0][1] if frames else self._module
            obj.origin = (m.source if m else None, node.line, node.col)
        elif frames:
            f, m = frames[0]
            line, col = self.position(f)
            if skip or self.at_name or self.at_arg:
                site = self.code_calls.get(f.f_code.co_filename, {}).get((line, col))
                if site is not None:
                    args, name_pos = site
                    if self.at_name and name_pos is not None:
                        line, col = name_pos
                    elif (skip or self.at_arg) and self.skip_arg < len(args):
                        line, col = args[self.skip_arg]
            obj.origin = (m.source, line, col)
        else:
            obj.origin = (self._module.source if self._module else None, self.line, 0)
        trace = []
        for i, (f, m) in enumerate(frames):
            name = self.code_names.get(f.f_code.co_filename, {}).get(f.f_code.co_name)
            if name is None or i + 1 >= len(frames):
                continue
            caller, cmod = frames[i + 1]
            trace.append((name, cmod, self.position(caller)[0]))
        trace.reverse()
        obj.trace = trace

    def python_exception(self, error):
        """Turn a Python error raised by compiled code into a Barq exception
        object, or re-raise it if it is not one Barq knows about.

        RecursionError: the call stack is too deep.
        NameError: a function read a top-level variable before its declaration
        ran (the tree-walker reports this as "'x' is not defined")."""
        if isinstance(error, RecursionError):
            # The limit counts Python frames (1-3 per Barq call), so no exact number.
            return self.exception_from(error, "RuntimeException", "maximum call depth exceeded — infinite recursion?")
        tb = error.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        filename = tb.tb_frame.f_code.co_filename if tb is not None else None
        name = self.code_vars.get(filename, {}).get(getattr(error, "name", None))
        if name is None:
            raise error
        return self.exception_from(error, "NameException", f"'{name}' is not defined")

    def exception_from(self, error, cls_name, message):
        """A Barq exception for a Python error. The frames that raised it may
        be gone, so the location comes from the traceback."""
        obj = BarqObject(self.classes[cls_name])
        obj.fields["message"] = message
        obj.fields["code"] = 0
        inner = []
        tb = error.__traceback__ if error is not None else None
        while tb is not None:
            m = self.code_files.get(tb.tb_frame.f_code.co_filename)
            if m is not None:
                inner.append((TracebackFrame(self, tb), m))
            tb = tb.tb_next
        inner.reverse()
        outer = self.barq_frames()
        if inner and outer and inner[-1][0].frame is outer[0][0]:
            inner.pop()
        self.stamp(obj, frames=inner + outer)
        return obj

    # -- loading ---------------------------------------------------------------

    def run_source(self, text, path="<string>"):
        source = SourceFile(path, text)
        program = self.parse_and_check(source)
        module = Module(path, source, Env(self.builtins))
        self.modules[path] = module
        self.run_module(module, program, None)
        return module

    def load_module(self, path, entry=False):
        path = os.path.abspath(path)
        if path in self.modules:
            return self.modules[path]
        with open(path, encoding="utf-8") as f:
            text = f.read()
        display = os.path.relpath(path, self.root) if path.startswith(self.root) else path
        source = SourceFile(display, text)
        cache_path = self.cache_path(path)
        cached = self.read_cache(cache_path, text) if cache_path else None
        if cached is not None:
            program, compiled = cached
        else:
            program = self.parse_and_check(source)
            compiled = None
        module = Module(path, source, Env(self.builtins))
        self.modules[path] = module
        prev = self._module
        self._module = module
        try:
            self.run_module(module, program, compiled, cache_path, text)
        finally:
            self._module = prev
        return module

    def compile_file(self, path, emit=False):
        """Parse, check and compile one file without running it. Writes the
        cache (or, with emit, returns the generated Python syntax tree)."""
        path = os.path.abspath(path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        display = os.path.relpath(path, self.root) if path.startswith(self.root) else path
        source = SourceFile(display, text)
        program = self.parse_and_check(source)
        module = Module(path, source, Env(self.builtins))
        self._module = module
        self.declare_module(module, program.body)
        compiled, tree = self.compile_module(module, program)
        if emit:
            return tree
        cache_path = self.cache_path(path)
        if cache_path:
            self.write_cache(cache_path, text, program, compiled)
        return compiled

    def declare_module(self, module, body):
        """setup_module() without finishing classes: that happens in attach(),
        once the compiled methods exist."""
        module.imports = {}
        for i, stmt in enumerate(body):
            if isinstance(stmt, N.Namespace):
                if i != 0:
                    self.compile_error("'namespace' must be the first statement in the file", stmt)
                module.namespace = stmt.name
            elif isinstance(stmt, N.Use):
                for fqn, alias in stmt.imports:
                    short = alias or fqn.split("\\")[-1]
                    if short in module.imports:
                        self.compile_error(f"'{short}' is imported twice", stmt)
                    module.imports[short] = (fqn, stmt)
                    builtin = self.builtins.vars.get(short)
                    if builtin is not None and isinstance(builtin[0], NativeModule):
                        module.env.declare(short, IMPORT_PENDING, None, const=True)
        for stmt in body:
            if isinstance(stmt, N.FuncDecl):
                self.declare_function(module, stmt)
            elif isinstance(stmt, N.ClassDecl):
                self.declare_class(module, stmt)

    def compile_module(self, module, program):
        compiler = Compiler(program, module, module.path)
        tree, exports = compiler.compile()
        filename = "<barq>" + module.path
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            code = compile(tree, filename, "exec", dont_inherit=True)
        return CompiledModule(code, compiler.consts, compiler.names, exports, module.path,
                              compiler.calls, compiler.varnames), tree

    def run_module(self, module, program, compiled, cache_path=None, text=None):
        self.declare_module(module, program.body)
        if compiled is None:
            compiled, _ = self.compile_module(module, program)
            if cache_path:
                self.write_cache(cache_path, text, program, compiled)
        filename = compiled.code.co_filename
        if filename != "<barq>" + module.path:
            # The cache was made for a file at another path: recompile.
            compiled, _ = self.compile_module(module, program)
            filename = compiled.code.co_filename
        self.code_files[filename] = module
        self.code_names[filename] = compiled.names
        self.code_calls[filename] = compiled.calls
        self.code_vars[filename] = compiled.varnames
        namespace = {"__builtins__": __builtins__}
        exec(compiled.code, namespace)
        runtime = ModuleRuntime(self, module, compiled)
        try:
            namespace["__barq__"](runtime)
        except (RecursionError, NameError) as e:
            raise BarqThrow(self.python_exception(e)) from None

    # -- classes ---------------------------------------------------------------

    def finish_class(self, cls):
        if getattr(cls, "_finished", False):
            return
        cls._finished = True
        super().finish_class(cls)
        table = cls.__dict__.get("_compiled") or {}
        for name, m in cls.methods.items():
            if type(m) is BarqFunction:
                m.nparams = len(m.params) if m.simple else -1   # -1: arguments go through bind_args()
                if m.trait is not None:
                    # compiled once in the trait's file, told which class it runs for
                    code = (m.trait.__dict__.get("_compiled") or {}).get(("m", m.trait.name, m.trait_method or name))
                    m.code = functools.partial(code, _cx=cls) if code is not None else None
                else:
                    m.code = table.get(("m", cls.name, name))
        cls._defaults = {name: fn for key, fn in table.items()
                         if key[0] == "d" and key[1] == cls.name for name in (key[2],)}

    def instantiate_bare(self, cls):
        if cls.is_enum:
            return super().instantiate_bare(cls)
        plan = cls.__dict__.get("_plan")
        if plan is None:
            plan = cls._plan = self.default_plan(cls)
        obj = BarqObject(cls)
        fields = obj.fields
        for name, kind, payload, t, owner, home in plan:
            if kind == 0:
                fields[name] = payload
                continue
            if kind == 1:
                value = payload()
            elif kind == 2:
                value = self.zero_value(t)
            else:
                prev = self._module
                self._module = home
                try:
                    value = self.eval(payload, home.env)
                finally:
                    self._module = prev
            if t is not None and not self.matches(value, t, home):
                self.check_assign(value, t, f"default of property '{owner.name}.{name}'", None, home)
            fields[name] = value
        return obj

    def default_plan(self, cls):
        """How to fill each property of a new object: (name, kind, payload, type, owner, module).
        kind 0: a constant already checked, 1: compiled default, 2: zero value, 3: tree-walk.
        module is the file the default is written in (a trait's, for trait properties)."""
        plan = []
        for c in cls.chain():
            defaults = c.__dict__.get("_defaults") or {}
            for p in c.props.values():
                d = p.default
                home = p.module or c.module
                if d is None:
                    if p.type is not None and p.type.name in ("list", "dict", "set") and not p.type.nullable:
                        plan.append((p.name, 2, None, p.type, c, home))
                    else:
                        plan.append((p.name, 0, self.zero_value(p.type), None, c, home))
                elif isinstance(d, N.Literal) and type(d.value) in (int, float, str, bool, type(None)) \
                        and self.matches(d.value, p.type, home):
                    plan.append((p.name, 0, d.value, None, c, home))
                elif p.module is None and p.name in defaults and defaults[p.name] is not None:
                    plan.append((p.name, 1, defaults[p.name], p.type, c, home))
                else:
                    plan.append((p.name, 3, d, p.type, c, home))
        return plan

    # -- calls -----------------------------------------------------------------

    def call_function(self, func, args, node=None, kwargs=None):
        t = type(func)
        if t is BarqFunction and func.owner is None and func.code is not None and len(args) == func.nparams \
                and kwargs is None:
            return func.code(*args)
        if t is BoundMethod:
            inner = func.func
            if type(inner) is BarqFunction and inner.code is not None:
                return self.call_compiled(inner, args, node, func.this, kwargs)
        elif t is BarqFunction and func.code is not None:
            if func.owner is not None and not func.is_static:
                self.throw("TypeException", f"method '{func.name}' needs an object; call it as obj.{func.name}()", node)
            return self.call_compiled(func, args, node, None, kwargs)
        return super().call_function(func, args, node, kwargs)

    def throw_at_arg(self, cls_name, message, node, index):
        if node is not None:
            return super().throw_at_arg(cls_name, message, node, index)
        self.at_arg, self.skip_arg = index is not None, index or 0   # stamp() finds the argument's position
        try:
            self.throw(cls_name, message, None)
        finally:
            self.at_arg, self.skip_arg = False, 0

    def arg_type_error(self, value, t, what, node, index, module):
        if node is not None:
            return super().arg_type_error(value, t, what, node, index, module)
        self.at_arg, self.skip_arg = index is not None, index or 0
        try:
            self.check_assign(value, t, what, None, module)
        finally:
            self.at_arg, self.skip_arg = False, 0

    def call_compiled(self, fn, args, node, this, kwargs=None):
        if fn.is_abstract:
            self.throw("TypeException", f"cannot call abstract method {self.display_name(fn)}()", node)
        n = fn.nparams
        if kwargs is not None or n < 0:
            args = self.bind_args(fn, args, kwargs, node)
        elif len(args) != n:
            self.throw("TypeException",
                       f"{self.display_name(fn)}() expects {n} argument{'s' if n != 1 else ''}, got {len(args)}", node)
        if fn.owner is not None and not fn.is_static:
            return fn.code(this, *args)
        return fn.code(*args)

    # -- the .bqc cache --------------------------------------------------------

    def cache_path(self, path):
        if not self.use_cache:
            return None
        root = self.root
        if not path.startswith(root + os.sep):
            return None
        rel = os.path.relpath(path, root)
        return os.path.join(root, CACHE_DIR, rel[:-3] + ".bqc" if rel.endswith(".bq") else rel + ".bqc")

    def read_cache(self, cache_path, text):
        try:
            with open(cache_path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        if not data.startswith(CACHE_MAGIC):
            return None
        try:
            header_len = len(CACHE_MAGIC)
            digest = data[header_len:header_len + 32]
            if digest != hashlib.sha256(text.encode("utf-8")).digest():
                return None
            payload = pickle.loads(data[header_len + 32:])
            program = payload["program"]
            compiled = CompiledModule(marshal.loads(payload["code"]), payload["consts"], payload["names"],
                                      payload["exports"], payload["path"], payload["calls"], payload["varnames"])
            return program, compiled
        except Exception:
            return None

    def write_cache(self, cache_path, text, program, compiled):
        try:
            payload = pickle.dumps({
                "program": program, "code": marshal.dumps(compiled.code), "consts": compiled.consts,
                "names": compiled.names, "exports": compiled.exports, "path": compiled.path,
                "calls": compiled.calls, "varnames": compiled.varnames,
            }, protocol=pickle.HIGHEST_PROTOCOL)
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            tmp = cache_path + f".{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(CACHE_MAGIC + hashlib.sha256(text.encode("utf-8")).digest() + payload)
            os.replace(tmp, cache_path)
        except (OSError, pickle.PicklingError, RecursionError, TypeError, ValueError):
            pass
