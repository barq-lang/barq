"""Barq compiler: turns a checked Barq syntax tree into Python bytecode.

The tree-walker (interpreter.py) visits every node each time it runs. This
compiler instead translates a whole file once into a Python syntax tree, which
CPython compiles to bytecode and runs on its own virtual machine. The result
behaves exactly like the tree-walker — same type checks, same error messages —
but runs many times faster.

How Barq maps onto the generated code:

* A file becomes one Python function, `__barq__(_R)`. Top-level variables are
  its locals; every Barq function and method is a nested `def` inside it, so
  they reach top-level variables as closure cells.
* Barq variables become Python locals named `v_<name>` (with a number added
  when a name is declared more than once), so block scoping keeps working.
* Operations whose types are known at compile time (an `int` plus an `int`)
  become plain Python operations. Everything else calls a small runtime helper
  (see vm.py) that does the full Barq checks.
* Every generated node carries the Barq line and column it came from. When an
  error happens, the VM reads the position of the running bytecode
  instruction, so errors point at the right place without any per-statement
  bookkeeping at run time.
"""

import ast as A

from . import nodes as N

COMPILER_VERSION = 3
END = 10 ** 9   # scope end of top-level variables (the whole file)

# Static types the compiler tracks. "str" is a string that cannot be null;
# "str?" is a `string` variable, which may hold null.
PRIM = {"int": "int", "float": "float", "bool": "bool", "string": "str?"}
NUM = ("int", "float")
LITERAL_TYPES = (int, float, str, bool, type(None))

CMP_OPS = {"<": A.Lt, ">": A.Gt, "<=": A.LtE, ">=": A.GtE, "==": A.Eq, "!=": A.NotEq}
ARITH_OPS = {"+": A.Add, "-": A.Sub, "*": A.Mult}
BIN_HELPERS = {"+": "add", "-": "sub", "*": "mul", "/": "div", "%": "mod", "**": "pow"}
CMP_HELPERS = {"<": "lt", ">": "gt", "<=": "le", ">=": "ge"}
SPECIAL_NAMES = {"__line__", "__file__", "__class__", "__method__", "__namespace__"}
CAST_TYPES = {"int": "int", "float": "float", "bool": "bool", "string": "str"}
BUILTIN_RESULT = {"len": "int"}


BUILTIN_TYPE_NAMES = {"int", "float", "string", "bool", "void", "any", "null", "list", "dict", "set", "range", "karo"}


def stype_of(t):
    """Static type of a value stored under type `t`. Objects are "@Name"
    (exactly that class) or "@Name?" (that class, a subclass, or null)."""
    if t is None:
        return None
    if t.name not in BUILTIN_TYPE_NAMES and not t.args:
        return "@" + t.name + "?"
    if t.nullable and t.name != "string":
        return None             # `?int` may be null, so no int fast paths
    return PRIM.get(t.name)


# ---------------------------------------------------------------------------
# small builders for Python syntax nodes
# ---------------------------------------------------------------------------

def L(name):
    return A.Name(id=name, ctx=A.Load())


def S(name):
    return A.Name(id=name, ctx=A.Store())


def C(value):
    return A.Constant(value=value)


def call(func, *args):
    return A.Call(func=L(func) if isinstance(func, str) else func, args=list(args), keywords=[])


def lst(items):
    return A.List(elts=list(items), ctx=A.Load())


def sub(value, index):
    return A.Subscript(value=value, slice=index if isinstance(index, A.AST) else C(index), ctx=A.Load())


def sub_store(value, index):
    return A.Subscript(value=value, slice=index if isinstance(index, A.AST) else C(index), ctx=A.Store())


def assign(target, value):
    return A.Assign(targets=[target], value=value)


def expr_stmt(value):
    return A.Expr(value=value)


def if_(test, body, orelse=None):
    return A.If(test=test, body=body or [A.Pass()], orelse=orelse or [])


def type_is(value, pytype):
    return A.Compare(left=call("type", value), ops=[A.Is()], comparators=[L(pytype)])


def type_is_not(value, pytype):
    return A.Compare(left=call("type", value), ops=[A.IsNot()], comparators=[L(pytype)])


def is_none(value):
    return A.Compare(left=value, ops=[A.Is()], comparators=[C(None)])


def is_not_none(value):
    return A.Compare(left=value, ops=[A.IsNot()], comparators=[C(None)])


def func_def(name, params, body, kwonly=(), required_kw=()):
    args = A.arguments(
        posonlyargs=[], args=[A.arg(arg=p) for p in params], vararg=None,
        kwonlyargs=[A.arg(arg=k) for k in tuple(required_kw) + tuple(kwonly)],
        kw_defaults=[None] * len(required_kw) + [L(k) for k in kwonly],
        kwarg=None, defaults=[])
    return A.FunctionDef(name=name, args=args, body=body or [A.Pass()], decorator_list=[],
                         returns=None, type_comment=None, type_params=[])


def at(py, node):
    """Give a generated node (and children without a position) a Barq position."""
    line = getattr(node, "line", 0) or 1
    col = getattr(node, "col", 0) or 0
    py.lineno = line
    py.col_offset = col
    py.end_lineno = line
    py.end_col_offset = col + 1
    return py


# ---------------------------------------------------------------------------
# scopes
# ---------------------------------------------------------------------------

class Var:
    __slots__ = ("name", "pyname", "type", "const", "boxed", "fn", "stype")

    def __init__(self, name, pyname, type_ref, const, boxed, fn, stype):
        self.name = name
        self.pyname = pyname
        self.type = type_ref      # TypeRef or None (untyped: loop variables, consts)
        self.const = const
        self.boxed = boxed        # held in a one-item list (captured inside a loop)
        self.fn = fn              # FnCtx that owns the Python variable
        self.stype = stype


class Scope:
    def __init__(self, fn, parent=None, end=None):
        self.fn = fn
        self.parent = parent
        self.vars = {}
        # Last line the scope's variables can be seen on (for the debugger).
        self.end = end if end is not None else (parent.end if parent is not None else END)

    def find(self, name):
        s = self
        while s is not None:
            v = s.vars.get(name)
            if v is not None:
                return v
            s = s.parent
        return None


def last_line(x):
    """Highest source line inside a node or a list of nodes."""
    best = 0
    stack = [x]
    while stack:
        y = stack.pop()
        if isinstance(y, (list, tuple)):
            stack.extend(y)
        elif isinstance(y, N.Node):
            line = getattr(y, "line", 0)
            if isinstance(line, int) and line > best:
                best = line
            for f in y._fields:
                stack.append(getattr(y, f, None))
    return best


class FnCtx:
    """One Python function being generated (module body, function, method or lambda)."""

    def __init__(self, kind, parent, cls_var=None, cls_name=None, func_name=None,
                 has_this=False, display=None, ret=None):
        self.kind = kind              # "main" | "func" | "method" | "lambda" | "thunk"
        self.parent = parent
        self.cls_var = cls_var        # Python name of the class used for visibility checks
        self.cls_name = cls_name
        self.func_name = func_name    # value of __method__
        self.has_this = has_this
        self.display = display        # name used in error messages, e.g. "User.save"
        self.ret = ret                # declared return TypeRef
        self.nonlocals = set()
        self.box_caps = []            # boxed outer variables captured as keyword defaults
        self.loop_captured = frozenset()
        self.targets = []             # break/continue targets: "loop" or ["switch", used]
        self.tmp = 0
        self.trait = False            # a trait method: its class arrives as the _cx argument

    def temp(self, prefix="_t"):
        self.tmp += 1
        return f"{prefix}{self.tmp}"


def captured_names(stmts):
    """Names used inside any function or lambda nested in these statements."""
    out = set()

    def walk(x, inside):
        if isinstance(x, (list, tuple)):
            for y in x:
                walk(y, inside)
            return
        if not isinstance(x, N.Node):
            return
        if isinstance(x, (N.Lambda, N.FuncDecl)):
            inside = True
        if inside and isinstance(x, N.Name):
            out.add(x.id)
        for f in x._fields:
            walk(getattr(x, f), inside)

    walk(stmts, False)
    return out


# ---------------------------------------------------------------------------
# the compiler
# ---------------------------------------------------------------------------

class CompiledModule:
    """What the compiler produces; this is what a .bqc cache file stores."""

    def __init__(self, code, consts, names, exports, path, calls=None, varnames=None, debug=None):
        self.debug = debug or {}  # python variable name -> (Barq name, boxed, first line, last line, type, const)
        self.calls = calls or {}  # call position -> argument positions (for argument errors)
        self.varnames = varnames or {}   # python variable name -> Barq name
        self.code = code          # Python code object of the module
        self.consts = consts      # objects the code refers to as _T[i] (types, nodes)
        self.names = names        # Python function name -> Barq display name (for traces)
        self.exports = exports    # top-level variables: (barq name, python name, type, const)
        self.path = path


class Compiler:
    def __init__(self, program, module, filename):
        self.program = program
        self.module = module          # runtime Module (namespace, functions, classes)
        self.filename = filename
        self.consts = []
        self._const_ids = {}
        self.names = {}               # pyname -> display name
        self.counter = {}
        self.varnames = {}            # python variable name -> Barq name (for error messages)
        self.debug = {}               # python variable name -> (name, boxed, line, end, type, const) for the debugger
        self.helpers = set()
        self.attach = []              # (key tuple, pyname)
        self.class_vars = {}          # class short name -> python var holding the class
        self.module_funcs = {}        # barq name -> (pyname, FuncDecl)
        self.fn_seq = 0
        self.pending_defs = []
        self.calls = {}               # (line, col) of a call -> [(line, col) of each argument]

    # -- naming --------------------------------------------------------------

    def h(self, name):
        """Reference to a runtime helper, bound as a local of the module function."""
        self.helpers.add(name)
        return L("_" + name)

    def const(self, obj):
        k = self._const_ids.get(id(obj))
        if k is None:
            k = len(self.consts)
            self.consts.append(obj)
            self._const_ids[id(obj)] = k
        return sub(self.h("T"), k)

    def pyname(self, name):
        n = self.counter.get(name, 0)
        self.counter[name] = n + 1
        py = f"v_{name}" if n == 0 else f"v_{name}_{n}"
        self.varnames[py] = name
        return py

    def fn_name(self, prefix, display):
        self.fn_seq += 1
        name = f"{prefix}{self.fn_seq}"
        if display is not None:
            self.names[name] = display
        return name

    # -- entry point -----------------------------------------------------------

    def compile(self):
        body = self.program.body
        main = FnCtx("main", None)
        scope = Scope(main)
        self.main_scope = scope
        self.declared_top = set()
        # Top-level variables are visible to every function of the file, even
        # ones written above the declaration, so declare them all up front.
        for stmt in body:
            if isinstance(stmt, (N.VarDecl, N.ConstDecl)) and stmt.name not in scope.vars:
                t = stmt.type if isinstance(stmt, N.VarDecl) else None
                st = stype_of(t) if t is not None else self.literal_stype(stmt.value)
                v = scope.vars[stmt.name] = Var(stmt.name, self.pyname(stmt.name), t,
                                                isinstance(stmt, N.ConstDecl), False, main, st)
                self.debug[v.pyname] = (v.name, False, stmt.line, END, t, v.const)
        for stmt in body:
            if isinstance(stmt, N.FuncDecl) and not stmt.is_abstract:
                self.module_funcs[stmt.name] = (self.fn_name("_f", stmt.name), stmt)
        for stmt in body:
            if isinstance(stmt, N.ClassDecl):
                self.class_vars[stmt.name] = f"_c{len(self.class_vars)}"

        defs = []
        for stmt in body:
            if isinstance(stmt, N.FuncDecl) and not stmt.is_abstract:
                pyname = self.module_funcs[stmt.name][0]
                fn = FnCtx("func", main, func_name=stmt.name, display=stmt.name, ret=stmt.ret)
                defs.append(self.function(stmt, fn, pyname, scope, this=False))
                self.attach.append((("f", stmt.name), pyname))
            elif isinstance(stmt, N.ClassDecl):
                defs.extend(self.class_defs(stmt, main, scope))

        stmts = []
        for stmt in body:
            if isinstance(stmt, (N.FuncDecl, N.ClassDecl, N.Namespace, N.Use)):
                continue
            stmts.extend(self.stmt(stmt, scope))

        exports = [(v.name, v.pyname, v.type, v.const) for v in scope.vars.values()]
        prologue = []
        for cname, var in self.class_vars.items():
            prologue.append(assign(S(var), sub(L("_K"), C(cname))))
        attach = A.Dict(keys=[C(k) for k, _ in self.attach], values=[L(p) for _, p in self.attach])
        tail = [expr_stmt(call(self.h("attach"), attach))]
        # The export runs after the last statement: give it a line past the end
        # of the file, so a debugger stepping off the end does not land on line 1.
        export = at(expr_stmt(call(self.h("export"), call("locals"))), N.Node(line=last_line(body) + 1, col=0))
        run = A.Try(body=stmts or [A.Pass()], handlers=[], orelse=[], finalbody=[export])
        helpers = [assign(S("_" + name), A.Attribute(value=L("_R"), attr=name, ctx=A.Load()))
                   for name in sorted(self.helpers | {"K"})]
        main_def = func_def("__barq__", ["_R"], helpers + prologue + defs + tail + [run])
        # Line 0, not 1: CPython reports no line event for the line a function
        # starts on, and a debugger must be able to stop on a statement on line 1.
        main_def.lineno = main_def.end_lineno = 0
        mod = A.Module(body=[main_def], type_ignores=[])
        A.fix_missing_locations(mod)
        return mod, exports

    def literal_stype(self, value):
        if isinstance(value, N.Literal):
            v = value.value
            if type(v) is bool:
                return "bool"
            if type(v) is int:
                return "int"
            if type(v) is float:
                return "float"
            if type(v) is str:
                return "str"
        if isinstance(value, N.Interp):
            return "str"
        return None

    # -- functions -------------------------------------------------------------

    def function(self, decl, fn, pyname, parent_scope, this):
        """Generate `def pyname(...)` for a function, method or lambda."""
        end = last_line(decl.body) or decl.line
        scope = Scope(fn, parent_scope, end=end)
        params = ["this"] if this else []
        checks = []
        for i, p in enumerate(decl.params):
            v = self.declare(scope, p.name, p.type, False, p)   # never boxed: fn has no loops yet
            params.append(v.pyname)
            checks.extend(self.check(L(v.pyname), None, p.type, f"argument '{p.name}' of {fn.display}()",
                                     p, helper="argfail", extra=(C(i),)))
        saved = self.pending_defs
        self.pending_defs = []
        try:
            if getattr(decl, "is_expr", False):
                value, st = self.expr(decl.body, scope)
                body = self.pending_defs + self.return_value(value, st, fn, decl.body)
            else:
                # Parameters and the body share one scope, as in the tree-walker.
                body = self.block(decl.body, scope, new_scope=False)
                ret = fn.ret
                if ret is not None and ret.name not in ("void", "any"):
                    body.append(at(expr_stmt(call(self.h("noret"), C(fn.display), self.const(ret))), ret))
        finally:
            self.pending_defs = saved
        head = []
        if fn.nonlocals:
            head.append(A.Nonlocal(names=sorted(fn.nonlocals)))
        d = func_def(pyname, params, head + checks + body, kwonly=fn.box_caps,
                     required_kw=("_cx",) if fn.trait else ())
        return at(d, decl)

    def return_value(self, value, st, fn, node):
        """Statements that return `value` after checking it against the return type."""
        ret = fn.ret
        if ret is None or ret.name == "any":
            return [at(A.Return(value=value), node)]
        if ret.name == "void":
            if isinstance(value, A.Constant) and value.value is None:
                return [at(A.Return(value=C(None)), node)]
            tmp = "_rv"
            return [at(assign(S(tmp), value), node),
                    at(if_(is_not_none(L(tmp)), [expr_stmt(call(self.h("voidret"), C(fn.display), L(tmp)))]), node),
                    at(A.Return(value=C(None)), node)]
        tmp = "_rv"
        checks = self.check(L(tmp), st, ret, f"return value of {fn.display}()", node)
        if not checks:
            return [at(A.Return(value=value), node)]
        return [at(assign(S(tmp), value), node)] + checks + [at(A.Return(value=L(tmp)), node)]

    def class_defs(self, decl, main, scope):
        defs = []
        trait = decl.kind == "trait"
        # A trait method runs as a method of each class that uses it, so that
        # class (for visibility, self:: and __class__) is passed in as _cx.
        cls_var = "_cx" if trait else self.class_vars[decl.name]
        for m in decl.members:
            if isinstance(m, N.FuncDecl) and not m.is_abstract:
                sep = "::" if m.is_static else "."
                display = f"{decl.name}{sep}{m.name}"
                pyname = self.fn_name("_m", display)
                fn = FnCtx("method", main, cls_var=cls_var, cls_name=decl.name, func_name=m.name,
                           has_this=not m.is_static, display=display, ret=m.ret)
                fn.trait = trait
                defs.append(self.function(m, fn, pyname, scope, this=not m.is_static))
                self.attach.append((("m", decl.name, m.name), pyname))
            elif isinstance(m, N.Property) and not m.is_static and m.value is not None \
                    and not (isinstance(m.value, N.Literal) and type(m.value.value) in LITERAL_TYPES):
                pyname = self.fn_name("_d", None)
                fn = FnCtx("thunk", main)
                saved = self.pending_defs
                self.pending_defs = []
                try:
                    value, _ = self.expr(m.value, Scope(fn, scope))
                    inner = self.pending_defs + [at(A.Return(value=value), m.value)]
                finally:
                    self.pending_defs = saved
                d = func_def(pyname, [], inner, kwonly=fn.box_caps)
                if fn.nonlocals:
                    d.body.insert(0, A.Nonlocal(names=sorted(fn.nonlocals)))
                defs.append(at(d, m))
                self.attach.append((("d", decl.name, m.name), pyname))
        return defs

    # -- variables -------------------------------------------------------------

    def declare(self, scope, name, type_ref, const, node, stype=None):
        fn = scope.fn
        boxed = name in fn.loop_captured
        st = stype if stype is not None else stype_of(type_ref)
        v = Var(name, self.pyname(name), type_ref, const, boxed, fn, st)
        scope.vars[name] = v
        self.debug[v.pyname] = (name, boxed, getattr(node, "line", 0) or 0, scope.end, type_ref, const)
        return v

    def capture(self, v, scope):
        """Make an outer variable reachable from the current function."""
        fn = scope.fn
        if v.fn is fn or not v.boxed:
            return
        f = fn
        while f is not None and f is not v.fn:
            if v.pyname not in f.box_caps:
                f.box_caps.append(v.pyname)
            f = f.parent

    def read_var(self, v, scope):
        self.capture(v, scope)
        if v.boxed:
            return sub(L(v.pyname), 0)
        return L(v.pyname)

    def store_var(self, v, scope, value, node):
        self.capture(v, scope)
        if v.boxed:
            return at(assign(sub_store(L(v.pyname), 0), value), node)
        if v.fn is not scope.fn:
            scope.fn.nonlocals.add(v.pyname)
        return at(assign(S(v.pyname), value), node)

    def init_var(self, v, value, node):
        """First binding of a declared variable in its own function."""
        if v.boxed:
            return at(assign(S(v.pyname), lst([value])), node)
        return at(assign(S(v.pyname), value), node)

    # -- type checks -------------------------------------------------------------

    def check(self, value, st, t, what, node, helper="tfail", extra=()):
        """Statements that raise a TypeException unless `value` fits type `t`.
        `value` must be a cheap expression (a name), since it is evaluated twice."""
        if t is None or t.name == "any":
            return []
        n = t.name
        if st is not None:
            if (n == "int" and st == "int") or (n == "float" and st == "float") or \
                    (n == "bool" and st == "bool") or (n == "string" and st in ("str", "str?")):
                return []
            if st[:1] == "@" and st.rstrip("?")[1:] == n and not t.args:
                return []
        if n in ("int", "float", "bool") and not t.args:
            test = type_is_not(value, {"int": "int", "float": "float", "bool": "bool"}[n])
            if t.nullable:
                test = A.BoolOp(op=A.And(), values=[is_not_none(value), test])
        elif n == "string":
            test = A.BoolOp(op=A.And(), values=[is_not_none(value), type_is_not(value, "str")])
        elif n not in BUILTIN_TYPE_NAMES and not t.args:
            # Fast path: null, or an object of exactly that class. Anything else
            # (a subclass, or a wrong value) goes through the full check.
            exact = A.BoolOp(op=A.Or(), values=[
                A.Compare(left=call("type", value), ops=[A.IsNot()], comparators=[self.h("Obj")]),
                A.Compare(left=A.Attribute(value=value, attr="cls", ctx=A.Load()), ops=[A.IsNot()],
                          comparators=[sub(self.h("CL"), C(n))])])
            full = A.UnaryOp(op=A.Not(), operand=call(self.h("m"), value, self.const(t)))
            test = A.BoolOp(op=A.And(), values=[is_not_none(value), exact, full])
        else:
            test = A.UnaryOp(op=A.Not(), operand=call(self.h("m"), value, self.const(t)))
        fail = expr_stmt(call(self.h(helper), value, self.const(t), C(what), *extra))
        return [at(if_(test, [fail]), node)]

    # -- statements ------------------------------------------------------------

    def block(self, stmts, scope, new_scope=True):
        inner = Scope(scope.fn, scope, end=last_line(stmts) or None) if new_scope else scope
        out = []
        for s in stmts or []:
            out.extend(self.stmt(s, inner))
        return out

    def stmt(self, s, scope):
        # Lambdas are expressions in Barq but `def` is a statement in Python:
        # each statement's lambdas are defined right before it.
        saved = self.pending_defs
        self.pending_defs = []
        try:
            out = getattr(self, "s_" + type(s).__name__)(s, scope)
            return self.pending_defs + out
        finally:
            self.pending_defs = saved

    def throw_stmt(self, cls_name, message, node):
        return [at(expr_stmt(call(self.h("throw"), C(cls_name), C(message))), node)]

    def compile_error_stmt(self, message, node):
        return [at(expr_stmt(call(self.h("compile_error"), C(message), C(node.line), C(node.col))), node)]

    def s_Namespace(self, s, scope):
        return self.compile_error_stmt("'namespace' is only allowed at the top level of a file", s)

    def s_Use(self, s, scope):
        return self.compile_error_stmt("'use' is only allowed at the top level of a file", s)

    def s_ClassDecl(self, s, scope):
        return self.compile_error_stmt("'class' is only allowed at the top level of a file", s)

    def s_FuncDecl(self, s, scope):
        # A function declared inside a block: a value bound when the line runs.
        if s.name in scope.vars:
            return self.compile_error_stmt(f"'{s.name}' is already declared in this scope", s)
        v = self.declare(scope, s.name, None, True, s)
        pyname = self.fn_name("_n", s.name)
        fn = FnCtx("func", scope.fn, func_name=s.name, display=s.name, ret=s.ret)
        d = self.function(s, fn, pyname, scope, this=False)
        make = call(self.h("mkfn"), L(pyname), self.const(s))
        return [d, self.init_var(v, make, s)]

    def s_VarDecl(self, s, scope):
        out = []
        top = scope is self.main_scope
        if s.name in scope.vars and (not top or s.name in self.declared_top):
            return self.throw_stmt("NameException", f"variable '{s.name}' is already declared in this scope", s)
        if top and (s.name in self.module.functions or s.name in self.module.classes):
            return self.throw_stmt("NameException", f"variable '{s.name}' is already declared in this scope", s)
        t = s.type
        if t.name == "void":
            return self.throw_stmt("TypeException", "variables cannot have type void", s)
        if s.value is not None:
            value, st = self.expr(s.value, scope)
        else:
            value, st = self.zero(t), stype_of(t)
            if st == "str?" and not t.nullable:
                st = "str"
        if top:
            self.declared_top.add(s.name)
            v = scope.vars[s.name]
        else:
            v = self.declare(scope, s.name, t, False, s)
        out.append(self.init_var(v, value, s.value or s))
        out.extend(self.check(self.read_var(v, scope), st, t, f"variable '{s.name}'", s.value or s))
        return out

    def zero(self, t):
        n = t.name
        if t.nullable:
            return C(None)
        if n in ("int", "float", "string", "bool"):
            return C({"int": 0, "float": 0.0, "string": "", "bool": False}[n])
        if n == "list":
            return lst([])
        if n == "dict":
            return A.Dict(keys=[], values=[])
        if n == "set":
            return call(self.h("Set"))
        return C(None)

    def s_ConstDecl(self, s, scope):
        top = scope is self.main_scope
        if s.name in scope.vars and (not top or s.name in self.declared_top):
            return self.throw_stmt("NameException", f"'{s.name}' is already declared in this scope", s)
        value, st = self.expr(s.value, scope)
        if top:
            self.declared_top.add(s.name)
            v = scope.vars[s.name]
            v.stype = st
        else:
            v = self.declare(scope, s.name, None, True, s, stype=st)
        return [self.init_var(v, value, s)]

    def s_Assign(self, s, scope):
        target = s.target
        op = s.op
        if isinstance(target, N.Name):
            v = scope.find(target.id)
            if v is None:
                if op == "=":
                    hint = self.guess_type(s.value)
                    return self.throw_stmt("NameException",
                                           f"'{target.id}' is not declared — give it a type, e.g. "
                                           f"'{hint} {target.id} = ...'", target)
                return self.throw_stmt("NameException", f"'{target.id}' is not declared", target)
            if v.const:
                return self.throw_stmt("TypeException", f"cannot reassign constant '{target.id}'", target)
            value, st = self.expr(s.value, scope)
            if op != "=":
                value, st = self.binop(op[:-1], self.read_var(v, scope), v.stype, value, st, s)
            checks = self.check(L("_v"), st, v.type, f"variable '{target.id}'", s.value)
            if not checks:
                return [self.store_var(v, scope, value, s)]
            return [at(assign(S("_v"), value), s)] + checks + [self.store_var(v, scope, L("_v"), s)]
        fn = scope.fn
        ctx = self.ctx_ref(fn)
        this = self.this_ref(fn)
        if isinstance(target, N.Attr):
            obj, _ = self.expr(target.obj, scope)
            value, st = self.expr(s.value, scope)
            if op == "=":
                return [at(expr_stmt(call(self.h("setattr"), obj, C(target.name), value, ctx, this)), target)]
            o, x = fn.temp(), fn.temp()
            cur = at(call(self.h("getattr"), L(o), C(target.name), ctx, this), target)
            new, _ = self.binop(op[:-1], cur, None, L(x), st, s)
            return [at(assign(S(o), obj), s), at(assign(S(x), value), s),
                    at(expr_stmt(call(self.h("setattr"), L(o), C(target.name), new, ctx, this)), target)]
        if isinstance(target, N.Index):
            cont, _ = self.expr(target.obj, scope)
            key, _ = self.expr(target.index, scope)
            value, st = self.expr(s.value, scope)
            if op == "=":
                return [at(expr_stmt(call(self.h("setindex"), cont, key, value)), target)]
            c, k, x = fn.temp(), fn.temp(), fn.temp()
            cur = at(call(self.h("index"), L(c), L(k)), target)
            new, _ = self.binop(op[:-1], cur, None, L(x), st, s)
            return [at(assign(S(c), cont), s), at(assign(S(k), key), s), at(assign(S(x), value), s),
                    at(expr_stmt(call(self.h("setindex"), L(c), L(k), new)), target)]
        if isinstance(target, N.StaticRef):
            cls = self.static_target(target, scope)
            value, st = self.expr(s.value, scope)
            c = fn.temp()
            out = [at(assign(S(c), cls), target)]
            if op != "=":
                x = fn.temp()
                out.append(at(assign(S(x), value), s))
                cur = at(call(self.h("static"), L(c), C(target.name), ctx, this, C(False)), target)
                value, _ = self.binop(op[:-1], cur, None, L(x), st, s)
            out.append(at(expr_stmt(call(self.h("setstatic"), L(c), C(target.name), value, ctx, this)), target))
            return out
        return self.throw_stmt("TypeException", "cannot assign to this expression", s)

    def guess_type(self, expr):
        if isinstance(expr, N.Literal):
            v = expr.value
            if v is None:
                return "any"
            return {bool: "bool", int: "int", float: "float", str: "string"}.get(type(v), "any")
        if isinstance(expr, N.Interp):
            return "string"
        if isinstance(expr, N.ListLit):
            return "list<...>"
        if isinstance(expr, N.DictLit):
            return "dict<..., ...>"
        if isinstance(expr, N.New):
            return expr.cls.split("\\")[-1]
        return "any"

    def s_ExprStmt(self, s, scope):
        value, _ = self.expr(s.expr, scope)
        return [at(expr_stmt(value), s)]

    def cond(self, node, scope, what="condition"):
        value, st = self.expr(node, scope)
        if st == "bool":
            return value
        return at(call(self.h("cond"), value, C(what)), node)

    def s_If(self, s, scope):
        orelse = self.block(s.other, scope) if s.other is not None else []
        for cond, body in reversed(s.branches):
            test = self.cond(cond, scope)
            orelse = [at(if_(test, self.block(body, scope), orelse), cond)]
        return orelse

    def s_For(self, s, scope):
        fn = scope.fn
        prev = fn.loop_captured
        iterable, _ = self.expr(s.iterable, scope)
        fn.loop_captured = prev | captured_names(s.body)
        fn.targets.append("loop")
        try:
            inner = Scope(fn, scope, end=last_line(s.body) or None)
            range_call = self.is_builtin_call(s.iterable, "range", scope)
            pre = []
            if s.key is not None:
                kv = self.declare(inner, s.key, None, False, s, stype="int" if range_call else None)
                vv = self.declare(inner, s.var, None, False, s)
                kt, vt = fn.temp("_k"), fn.temp("_x")
                target = A.Tuple(elts=[S(kt), S(vt)], ctx=A.Store())
                pre = [self.init_var(kv, L(kt), s), self.init_var(vv, L(vt), s)]
                it = at(call(self.h("iterp"), iterable), s.iterable)
            else:
                vv = self.declare(inner, s.var, None, False, s, stype="int" if range_call else None)
                if vv.boxed:
                    xt = fn.temp("_x")
                    target = S(xt)
                    pre = [self.init_var(vv, L(xt), s)]
                else:
                    target = S(vv.pyname)
                it = at(call(self.h("iter"), iterable), s.iterable)
            body = pre + self.block(s.body, inner, new_scope=False)
        finally:
            fn.targets.pop()
            fn.loop_captured = prev
        return [at(A.For(target=target, iter=it, body=body or [A.Pass()], orelse=[]), s)]

    def is_builtin_call(self, node, name, scope):
        return (isinstance(node, N.Call) and isinstance(node.func, N.Name) and node.func.id == name
                and scope.find(name) is None and name not in self.module_funcs
                and name not in getattr(self.module, "imports", {}))

    def s_While(self, s, scope):
        fn = scope.fn
        prev = fn.loop_captured
        test = self.cond(s.cond, scope, "while condition")
        fn.loop_captured = prev | captured_names(s.body)
        fn.targets.append("loop")
        try:
            body = self.block(s.body, scope)
        finally:
            fn.targets.pop()
            fn.loop_captured = prev
        return [at(A.While(test=test, body=body or [A.Pass()], orelse=[]), s)]

    def s_DoWhile(self, s, scope):
        fn = scope.fn
        prev = fn.loop_captured
        first = fn.temp("_first")
        fn.loop_captured = prev | captured_names(s.body)
        fn.targets.append("loop")
        try:
            body = self.block(s.body, scope)
        finally:
            fn.targets.pop()
            fn.loop_captured = prev
        test = A.BoolOp(op=A.Or(), values=[L(first), self.cond(s.cond, scope, "while condition")])
        return [at(assign(S(first), C(True)), s),
                at(A.While(test=test, body=[at(assign(S(first), C(False)), s)] + body, orelse=[]), s)]

    def s_Break(self, s, scope):
        targets = scope.fn.targets
        if not targets:
            return self.throw_stmt("RuntimeException", "'break' or 'continue' used outside of a loop", s)
        top = targets[-1]
        if top == "loop":
            return [at(A.Break(), s)]
        top[1] = True
        return [at(A.Raise(exc=self.h("SwitchBreak"), cause=None), s)]

    def s_Continue(self, s, scope):
        if "loop" not in scope.fn.targets:
            return self.throw_stmt("RuntimeException", "'break' or 'continue' used outside of a loop", s)
        return [at(A.Continue(), s)]

    def s_Return(self, s, scope):
        fn = scope.fn
        if fn.kind == "main":
            if s.value is not None:
                value, _ = self.expr(s.value, scope)
                return [at(expr_stmt(value), s), at(A.Return(value=None), s)]
            return [at(A.Return(value=None), s)]
        if s.value is None:
            return self.return_value(C(None), None, fn, s)
        value, st = self.expr(s.value, scope)
        return self.return_value(value, st, fn, s.value)

    def s_Raise(self, s, scope):
        if s.value is None:
            return [at(expr_stmt(call(self.h("reraise"))), s)]
        value, _ = self.expr(s.value, scope)
        return [at(expr_stmt(call(self.h("raise_"), value)), s)]

    def s_Switch(self, s, scope):
        fn = scope.fn
        subject, sst = self.expr(s.subject, scope)
        tmp = fn.temp("_sw")
        target = ["switch", False]
        fn.targets.append(target)
        try:
            def case_body(body):
                stmts = list(body or [])
                if stmts and isinstance(stmts[-1], N.Break):
                    stmts = stmts[:-1]   # a trailing break just ends the case
                return self.block(stmts, scope)

            orelse = case_body(s.default) if s.default is not None else []
            for labels, body in reversed(s.cases):
                tests = []
                for label in labels:
                    lv, lst_ = self.expr(label, scope)
                    tests.append(at(self.equals(L(tmp), sst, lv, lst_, label), label))
                test = tests[0] if len(tests) == 1 else A.BoolOp(op=A.Or(), values=tests)
                orelse = [at(if_(test, case_body(body), orelse), labels[0])]
        finally:
            fn.targets.pop()
        out = [at(assign(S(tmp), subject), s)]
        if target[1]:
            handler = A.ExceptHandler(type=self.h("SwitchBreak"), name=None, body=[A.Pass()])
            out.append(at(A.Try(body=orelse or [A.Pass()], handlers=[handler], orelse=[], finalbody=[]), s))
        else:
            out.extend(orelse)
        return out

    def s_Try(self, s, scope):
        fn = scope.fn
        body = self.block(s.body, scope)
        handlers = []
        if s.handlers:
            e, o = fn.temp("_e"), fn.temp("_o")
            chain = [at(A.Raise(exc=call(self.h("Throw"), L(o)), cause=None), s)]
            for types, name, hbody, pos in reversed(s.handlers):
                hnode = N.Node(line=pos[0], col=pos[1])
                hscope = Scope(fn, scope, end=last_line(hbody) or None)
                inner = []
                if name:
                    v = self.declare(hscope, name, None, False, hnode)
                    inner.append(self.init_var(v, L(o), hnode))
                inner.append(at(expr_stmt(call(A.Attribute(value=self.h("H"), attr="append", ctx=A.Load()), L(o))), hnode))
                handled = A.Try(body=self.block(hbody, hscope, new_scope=False) or [A.Pass()], handlers=[],
                                orelse=[], finalbody=[expr_stmt(call(A.Attribute(value=self.h("H"), attr="pop", ctx=A.Load())))])
                inner.append(at(handled, hnode))
                if types:
                    tests = [call(A.Attribute(value=A.Attribute(value=L(o), attr="cls", ctx=A.Load()),
                                              attr="is_subclass_of", ctx=A.Load()),
                                  sub(self.h("CL"), C(t))) for t in types]
                    test = tests[0] if len(tests) == 1 else A.BoolOp(op=A.Or(), values=tests)
                    chain = [at(if_(test, inner, chain), hnode)]
                else:
                    chain = inner
            first = at(assign(S(o), call(self.h("thrown"), L(e))), s)
            handlers.append(at(A.ExceptHandler(type=self.h("CATCH"), name=e, body=[first] + chain), s))
        final = self.block(s.final, scope) if s.final is not None else []
        return [at(A.Try(body=body or [A.Pass()], handlers=handlers, orelse=[], finalbody=final), s)]

    # -- expressions -----------------------------------------------------------

    def expr(self, node, scope):
        method = getattr(self, "e_" + type(node).__name__)
        py, st = method(node, scope)
        if not hasattr(py, "lineno"):
            at(py, node)
        return py, st

    def e_Literal(self, node, scope):
        v = node.value
        if type(v) is bool:
            return C(v), "bool"
        if type(v) is int:
            return C(v), "int"
        if type(v) is float:
            return C(v), "float"
        if type(v) is str:
            return C(v), "str"
        return C(v), None

    def e_Interp(self, node, scope):
        parts = []
        for part in node.parts:
            if isinstance(part, str):
                if part:
                    parts.append(C(part))
                continue
            value, st = self.expr(part, scope)
            if st == "str":
                parts.append(value)
            elif st == "int":
                parts.append(at(call("str", value), part))
            else:
                parts.append(at(call(self.h("disp"), value), part))
        if not parts:
            return C(""), "str"
        out = parts[0]
        for p in parts[1:]:
            out = A.BinOp(left=out, op=A.Add(), right=p)
        return out, "str"

    def e_Name(self, node, scope):
        name = node.id
        v = scope.find(name)
        if v is not None:
            return self.read_var(v, scope), v.stype
        if name in SPECIAL_NAMES:
            fn = scope.fn
            if name == "__line__":
                return C(node.line), "int"
            if name == "__file__":
                return self.h("path"), "str"
            if name == "__namespace__":
                return C(self.module.namespace), "str"
            ctx = self.context(fn)
            if name == "__class__":
                if ctx is not None and ctx.trait:
                    return A.Attribute(value=L("_cx"), attr="name", ctx=A.Load()), "str"
                return C(ctx.cls_name if ctx and ctx.cls_name else ""), "str"
            return C(ctx.func_name if ctx and ctx.func_name else ""), "str"
        return sub(self.h("G"), C(name)), None

    def context(self, fn):
        """The function whose (this, class, name) context applies, like Env.context()."""
        f = fn
        while f is not None:
            if f.kind in ("func", "method"):
                return f
            if f.kind in ("main", "thunk"):
                return None
            f = f.parent
        return None

    def ctx_ref(self, fn):
        c = self.context(fn)
        return L(c.cls_var) if c is not None and c.cls_var else C(None)

    def this_ref(self, fn):
        c = self.context(fn)
        return L("this") if c is not None and c.has_this else C(None)

    def e_This(self, node, scope):
        c = self.context(scope.fn)
        if c is None or not c.has_this:
            return call(self.h("nothis")), None
        return L("this"), (None if c.trait else "@" + c.cls_name)

    def e_ListLit(self, node, scope):
        return lst(self.expr(x, scope)[0] for x in node.items), None

    def e_DictLit(self, node, scope):
        if all(isinstance(k, N.Literal) and type(k.value) in LITERAL_TYPES for k, _ in node.pairs):
            keys = [C(k.value) for k, _ in node.pairs]
            values = [self.expr(v, scope)[0] for _, v in node.pairs]
            return A.Dict(keys=keys, values=values), None
        flat = []
        for k, v in node.pairs:
            flat.append(self.expr(k, scope)[0])
            flat.append(self.expr(v, scope)[0])
        return call(self.h("dictlit"), lst(flat)), None

    def e_SetLit(self, node, scope):
        return call(self.h("setlit"), lst(self.expr(x, scope)[0] for x in node.items)), None

    def e_Unary(self, node, scope):
        value, st = self.expr(node.operand, scope)
        if node.op == "not":
            if st == "bool":
                return A.UnaryOp(op=A.Not(), operand=value), "bool"
            return call(self.h("not_"), value), "bool"
        if st in NUM:
            if node.op == "-":
                return A.UnaryOp(op=A.USub(), operand=value), st
            return value, st
        return call(self.h("neg" if node.op == "-" else "pos"), value), None

    def e_Binary(self, node, scope):
        left, ls = self.expr(node.left, scope)
        right, rs = self.expr(node.right, scope)
        return self.binop(node.op, left, ls, right, rs, node)

    def binop(self, op, left, ls, right, rs, node):
        if ls == rs and ls in NUM:
            if op in ARITH_OPS:
                return at(A.BinOp(left=left, op=ARITH_OPS[op](), right=right), node), ls
            if op == "/":
                return at(call(self.h("idiv" if ls == "int" else "fdiv"), left, right), node), ls
            if op == "%":
                return at(call(self.h("imod" if ls == "int" else "fmod"), left, right), node), ls
            if op == "**" and ls == "float":
                return at(call(self.h("pow"), left, right), node), "float"
        if op == "+" and ls == "str" and rs == "str":
            return at(A.BinOp(left=left, op=A.Add(), right=right), node), "str"
        result = None
        if op == "+" and ls in ("str", "str?") and rs in ("str", "str?"):
            result = "str"
        elif op != "**" and ls == rs and ls in NUM:
            result = ls
        return at(call(self.h(BIN_HELPERS[op]), left, right), node), result

    def e_Logical(self, node, scope):
        if node.op == "??":
            left, ls = self.expr(node.left, scope)
            right, rs = self.expr(node.right, scope)
            tmp = scope.fn.temp("_n")
            test = is_not_none(A.NamedExpr(target=S(tmp), value=left))
            st = rs if rs == ls else ("str" if ls in ("str", "str?") and rs == "str" else None)
            return A.IfExp(test=test, body=L(tmp), orelse=right), st
        left = self.cond(node.left, scope, f"left side of '{node.op}'")
        right = self.cond(node.right, scope, f"right side of '{node.op}'")
        op = A.And() if node.op == "and" else A.Or()
        return A.BoolOp(op=op, values=[left, right]), "bool"

    def equals(self, left, ls, right, rs, node, negate=False):
        fast = (ls in NUM and rs in NUM) or (ls == rs and ls in ("bool", "str"))
        if fast:
            return A.Compare(left=left, ops=[A.NotEq() if negate else A.Eq()], comparators=[right])
        return call(self.h("ne" if negate else "eq"), left, right)

    def e_Compare(self, node, scope):
        left, ls = self.expr(node.left, scope)
        right, rs = self.expr(node.right, scope)
        op = node.op
        if op in ("==", "!="):
            return self.equals(left, ls, right, rs, node, negate=op == "!="), "bool"
        if op in ("in", "not in"):
            r = call(self.h("contains"), right, left)
            if op == "not in":
                r = A.UnaryOp(op=A.Not(), operand=r)
            return r, "bool"
        if (ls in NUM and rs in NUM) or (ls == "str" and rs == "str"):
            return A.Compare(left=left, ops=[CMP_OPS[op]()], comparators=[right]), "bool"
        return call(self.h(CMP_HELPERS[op]), left, right), "bool"

    def e_IsType(self, node, scope):
        value, _ = self.expr(node.expr, scope)
        t = node.type
        if t.name == "null":
            return is_none(value), "bool"
        if t.nullable:
            return call(self.h("m"), value, self.const(t)), "bool"
        if t.name in ("int", "float", "bool") and not t.args:
            return type_is(value, t.name), "bool"
        if t.name == "string":
            return type_is(value, "str"), "bool"
        if t.name == "any":
            return is_not_none(value), "bool"
        return call(self.h("is_"), value, self.const(t)), "bool"

    def e_Ternary(self, node, scope):
        test = self.cond(node.cond, scope, "ternary condition")
        a, sa = self.expr(node.then, scope)
        b, sb = self.expr(node.other, scope)
        return A.IfExp(test=test, body=a, orelse=b), (sa if sa == sb else None)

    def e_Cast(self, node, scope):
        value, vs = self.expr(node.expr, scope)
        target = node.type.name if not node.type.args else None
        st = CAST_TYPES.get(target)
        # Casts whose result is certain from the static type need no helper.
        if (vs, target) in (("int", "int"), ("float", "float"), ("bool", "bool"), ("str", "string")):
            return value, st
        if vs == "int" and target == "float":
            return call("float", value), st
        if vs == "int" and target == "string":
            return call("str", value), st
        return call(self.h("cast"), self.const(node.type), value), st

    def note_call(self, node, args, attr=None):
        """Remember where a call's arguments (and method name) are, so errors
        can point at them: (line, col) of the call -> (argument positions, name position)."""
        self.calls[(node.line, node.col)] = ([(a.line, a.col) for a in args],
                                             (attr.line, attr.col) if attr is not None else None)

    def call_args(self, nodes, scope):
        """Positional argument expressions (with *spread as Starred) and a dict
        of named ones, or None when there are none."""
        pos, keys, values = [], [], []
        for a in nodes:
            t = type(a)
            if t is N.NamedArg:
                keys.append(C(a.name))
                values.append(self.expr(a.value, scope)[0])
            elif t is N.Spread:
                value = at(call(self.h("spread"), self.expr(a.value, scope)[0]), a)
                pos.append(A.Starred(value=value, ctx=A.Load()))
            else:
                pos.append(self.expr(a, scope)[0])
        return pos, (A.Dict(keys=keys, values=values) if keys else None)

    def e_Call(self, node, scope):
        func = node.func
        fn = scope.fn
        self.note_call(node, node.args)
        args, kw = self.call_args(node.args, scope)
        plain = kw is None and not any(type(a) is N.Spread for a in node.args)
        if isinstance(func, N.Name):
            name = func.id
            v = scope.find(name)
            if v is None and name in self.module_funcs and plain:
                pyname, decl = self.module_funcs[name]
                simple = not any(p.default is not None or p.variadic for p in decl.params)
                if simple and len(decl.params) == len(args):
                    return call(pyname, *args), stype_of(decl.ret)
            if v is None and name in BUILTIN_RESULT and kw is None and self.is_builtin_call(node, name, scope):
                return call(self.h("call"), self.expr(func, scope)[0], lst(args)), BUILTIN_RESULT[name]
        if isinstance(func, N.Attr) and not func.safe:
            obj, _ = self.expr(func.obj, scope)
            self.note_call(node, node.args, attr=func)
            if kw is not None:
                return call(self.h("callmk"), obj, C(func.name), lst(args), kw, self.ctx_ref(fn), self.this_ref(fn)), None
            return call(self.h("callm"), obj, C(func.name), lst(args), self.ctx_ref(fn), self.this_ref(fn)), None
        f, _ = self.expr(func, scope)
        if kw is not None:
            return call(self.h("callk"), f, lst(args), kw), None
        return call(self.h("call"), f, lst(args)), None

    def e_Attr(self, node, scope):
        obj, _ = self.expr(node.obj, scope)
        fn = scope.fn
        helper = "sattr" if node.safe else "getattr"
        return call(self.h(helper), obj, C(node.name), self.ctx_ref(fn), self.this_ref(fn)), None

    def e_Index(self, node, scope):
        obj, _ = self.expr(node.obj, scope)
        idx, _ = self.expr(node.index, scope)
        return call(self.h("index"), obj, idx), None

    def static_target(self, node, scope):
        target = node.target
        fn = scope.fn
        if isinstance(target, N.Name) and target.id in ("parent", "self"):
            ctx = self.context(fn)
            if ctx is None or not ctx.cls_var:
                return at(call(self.h("throw"), C("NameException"),
                               C(f"'{target.id}::' can only be used inside a class")), node)
            if target.id == "self":
                return L(ctx.cls_var)
            return at(call(self.h("parentof"), L(ctx.cls_var)), node)
        value, _ = self.expr(target, scope)
        return call(self.h("classof"), value)

    def e_StaticRef(self, node, scope):
        fn = scope.fn
        cls = self.static_target(node, scope)
        via_parent = isinstance(node.target, N.Name) and node.target.id == "parent"
        return call(self.h("static"), cls, C(node.name), self.ctx_ref(fn), self.this_ref(fn), C(via_parent)), None

    def e_New(self, node, scope):
        fn = scope.fn
        self.note_call(node, node.args)
        args, kw = self.call_args(node.args, scope)
        cls = sub(self.h("CL"), C(node.cls))
        if kw is not None:
            return call(self.h("newk"), cls, lst(args), kw, self.ctx_ref(fn), self.this_ref(fn)), "@" + node.cls
        return call(self.h("new"), cls, lst(args), self.ctx_ref(fn), self.this_ref(fn)), "@" + node.cls

    def e_Lambda(self, node, scope):
        parent = scope.fn
        display = "<lambda>"
        fn = FnCtx("lambda", parent, display=display, ret=node.ret)
        pyname = self.fn_name("_l", display)
        d = self.function(node, fn, pyname, scope, this=False)
        # The def must run before the expression that uses it: hoist it into a
        # helper call that receives the function object.
        self.pending_defs.append(d)
        return call(self.h("mkfn"), L(pyname), self.const(node)), None

