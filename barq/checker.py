"""Static checks that run before a file executes.

The spec requires that type errors, undeclared variables and logic errors are
reported before any code runs. This pass walks the AST once with a scope
model and a small type inference. It is conservative: when it cannot know a
type (values from other files, dict lookups, method results) it says nothing
and the runtime checks take over.
"""

import os

from . import nodes as N
from .errors import CheckError

PRIMS = {"int", "float", "string", "bool"}
NON_NULLABLE = {"int", "float", "bool"}
BUILTIN_TYPE_NAMES = {"int", "float", "string", "bool", "void", "any", "null", "list", "dict", "set", "range", "function"}
SPECIAL_NAMES = {"__line__", "__file__", "__class__", "__method__", "__namespace__"}


def arity_text(params):
    n = len(params)
    variadic = n > 0 and params[-1].variadic
    required = sum(1 for p in params if p.default is None and not p.variadic)
    if variadic:
        return f"at least {required} argument{'s' if required != 1 else ''}"
    if required == n:
        return f"{n} argument{'s' if n != 1 else ''}"
    return f"{required} to {n} arguments"


def T(name, *args):
    return N.TypeRef(name, list(args))


def tstr(t):
    if t is None:
        return "any"
    q = "?" if t.nullable else ""
    if t.args:
        return f"{q}{t.name}<{', '.join(tstr(a) for a in t.args)}>"
    return q + t.name


class Scope:
    def __init__(self, parent=None, kind="block"):
        self.parent = parent
        self.kind = kind
        self.vars = {}      # name -> (TypeRef | None, is_const)

    def find(self, name):
        s = self
        while s is not None:
            if name in s.vars:
                return s.vars[name]
            s = s.parent
        return None


class FuncCtx:
    def __init__(self, name, ret, cls=None, is_static=False, is_lambda=False):
        self.name = name
        self.ret = ret
        self.cls = cls
        self.is_static = is_static
        self.is_lambda = is_lambda


class Checker:
    def __init__(self, program, source, builtin_names, root=None, find_file=None, max_errors=20):
        self.program = program
        self.source = source
        self.builtin_names = builtin_names
        self.root = root
        self.find_file = find_file
        self.max_errors = max_errors
        self.errors = []
        self.namespace = ""
        self.imports = set()
        self.classes = {}       # name -> ClassDecl (this file)
        self.functions = {}     # name -> FuncDecl (this file, top level)
        self.loop_depth = 0
        self.switch_depth = 0
        self.func = None

    # -- reporting ---------------------------------------------------------

    def error(self, message, node):
        if len(self.errors) < self.max_errors:
            self.errors.append(CheckError(message, self.source, node.line, node.col))

    # -- entry -------------------------------------------------------------

    def check(self):
        body = self.program.body
        for stmt in body:
            if isinstance(stmt, N.Namespace):
                self.namespace = stmt.name
            elif isinstance(stmt, N.Use):
                for fqn, alias in stmt.imports:
                    self.imports.add(alias or fqn.split("\\")[-1])
            elif isinstance(stmt, N.ClassDecl):
                self.classes[stmt.name] = stmt
            elif isinstance(stmt, N.FuncDecl):
                self.functions[stmt.name] = stmt
        module = Scope(kind="module")
        self.module_scope = module
        # Top-level code first, so functions can see every file-level variable.
        for stmt in body:
            if not isinstance(stmt, (N.FuncDecl, N.ClassDecl, N.Namespace, N.Use)):
                self.stmt(stmt, module)
        for stmt in body:
            if isinstance(stmt, N.FuncDecl):
                self.check_function(stmt, module)
            elif isinstance(stmt, N.ClassDecl):
                self.check_class(stmt, module)
        self.errors.sort(key=lambda e: (e.line, e.col))
        return self.errors

    # -- names & types -----------------------------------------------------

    def global_exists(self, name):
        if name in self.builtin_names or name in self.imports or name in SPECIAL_NAMES:
            return True
        if name in self.classes or name in self.functions:
            return True
        if self.namespace and self.find_file is not None:
            return self.find_file((self.namespace + "\\" + name).split("\\")) is not None
        return False

    def check_type_ref(self, t, node=None):
        if t is None:
            return
        if t.name not in BUILTIN_TYPE_NAMES and "\\" not in t.name and not self.global_exists(t.name):
            self.error(f"unknown type '{t.name}'", t)
        if t.name in ("list", "set") and t.args and len(t.args) != 1:
            self.error(f"{t.name} takes 1 type argument, e.g. {t.name}<int>", t)
        if t.name == "dict" and t.args and len(t.args) != 2:
            self.error("dict takes 2 type arguments, e.g. dict<string, int>", t)
        for a in t.args:
            self.check_type_ref(a)

    def class_chain(self, name):
        """Ancestors of a class declared in this file. Returns (chain, complete).
        Members that come from traits or interfaces are not fully known here
        (a value typed as an interface is some class with more methods), so
        such chains count as incomplete."""
        chain = []
        seen = set()
        complete = True
        while name in self.classes and name not in seen:
            seen.add(name)
            decl = self.classes[name]
            chain.append(decl)
            if decl.kind in ("trait", "interface") or decl.interfaces \
                    or any(isinstance(m, N.UseTrait) for m in decl.members):
                complete = False
            if decl.parent is None:
                return chain, complete
            name = decl.parent
        return chain, False

    def is_subclass(self, child, parent, seen=None):
        """True / False, or None when it depends on classes in other files."""
        seen = seen if seen is not None else set()
        if child in seen:
            return False
        seen.add(child)
        chain, complete = self.class_chain(child)
        if any(c.name == parent for c in chain):
            return True
        unknown = not chain or (chain[-1].parent is not None and chain[-1].parent not in self.classes)
        for c in chain:
            for iname in c.interfaces or []:
                if iname == parent:
                    return True
                if iname not in self.classes:
                    unknown = True
                    continue
                found = self.is_subclass(iname, parent, seen)
                if found:
                    return True
                if found is None:
                    unknown = True
        return None if unknown else False

    def compatible(self, target, actual):
        """False only when we are sure the value cannot fit the target type."""
        if target is None or actual is None:
            return True
        tn, an = target.name, actual.name
        if tn == "any" or an == "any":
            return True
        if an == "null":
            return (tn not in NON_NULLABLE or bool(target.nullable)) and tn != "void"
        if an == "void":
            return False
        if tn in PRIMS or an in PRIMS:
            return tn == an
        if tn in ("list", "dict", "set") or an in ("list", "dict", "set", "range"):
            if tn != an:
                return False
            if target.args and actual.args and len(target.args) == len(actual.args):
                return all(self.compatible(a, b) for a, b in zip(target.args, actual.args))
            return True
        if tn == "range" or tn in ("void", "null"):
            return tn == an
        # Both are class names.
        if tn == an:
            return True
        result = self.is_subclass(an, tn)
        return result is not False

    def describe_mismatch(self, target, actual):
        if target is not None and actual is not None and actual.name == "null":
            hint = f" — make it ?{tstr(target)} to allow null" if target.name in NON_NULLABLE else ""
            return f"is {tstr(target)} and cannot be null{hint}"
        hint = ""
        if target is not None and actual is not None and target.name in PRIMS and actual.name in PRIMS:
            hint = f" — convert it with ~{target.name}(...)"
        return f"expects {tstr(target)}, got {tstr(actual)}{hint}"

    # -- declarations --------------------------------------------------------

    def check_defaults(self, params, scope):
        for p in params:
            if p.default is not None:
                actual = self.expr(p.default, scope)
                if not self.compatible(p.type, actual):
                    self.error(f"the default value of '{p.name}' {self.describe_mismatch(p.type, actual)}", p.default)

    def check_function(self, decl, scope, cls=None):
        self.check_defaults(decl.params, scope)
        for p in decl.params:
            self.check_type_ref(p.type)
        if decl.ret is not None:
            self.check_type_ref(decl.ret)
        if decl.body is None:
            return
        fscope = Scope(scope, kind="function")
        seen = set()
        for p in decl.params:
            if p.name in seen:
                self.error(f"parameter '{p.name}' is listed twice", p)
            seen.add(p.name)
            fscope.vars[p.name] = (p.type, False)
        outer = (self.func, self.loop_depth, self.switch_depth)
        self.func = FuncCtx(decl.name, decl.ret, cls, decl.is_static)
        self.loop_depth = self.switch_depth = 0
        self.block(decl.body, fscope, new_scope=False)
        if decl.ret is not None and decl.ret.name not in ("void", "any") and not self.always_exits(decl.body):
            label = f"{cls.name}.{decl.name}" if cls is not None else decl.name
            self.error(f"{label}() must return {tstr(decl.ret)} on every path (add a 'return' at the end)", decl)
        self.func, self.loop_depth, self.switch_depth = outer

    def check_class(self, decl, scope):
        if decl.parent and "\\" not in decl.parent and not self.global_exists(decl.parent):
            self.error(f"unknown parent class '{decl.parent}'", decl)
        for iname in decl.interfaces or []:
            if "\\" not in iname and not self.global_exists(iname):
                self.error(f"unknown interface '{iname}'", decl)
        for m in decl.members:
            if isinstance(m, N.UseTrait):
                for tname in m.names:
                    if "\\" not in tname and not self.global_exists(tname):
                        self.error(f"unknown trait '{tname}'", m)
        for m in decl.members:
            if isinstance(m, N.Property):
                self.check_type_ref(m.type)
                if m.value is not None:
                    actual = self.expr(m.value, scope)
                    if not self.compatible(m.type, actual):
                        self.error(f"property '{m.name}' {self.describe_mismatch(m.type, actual)}", m.value)
            elif isinstance(m, N.ConstDecl):
                self.expr(m.value, scope)
        for m in decl.members:
            if isinstance(m, N.FuncDecl):
                self.check_function(m, scope, decl)

    def prop_type(self, cls_name, prop):
        """(found, type) for a property declared on a class in this file."""
        decl = self.classes.get(cls_name)
        if decl is not None and decl.is_enum:
            if prop == "name":
                return True, T("string")
            if prop == "value" and decl.backing:
                return True, T(decl.backing)
        chain, complete = self.class_chain(cls_name)
        magic = False
        for c in chain:
            for m in c.members:
                if isinstance(m, N.Property) and m.name == prop and not m.is_static:
                    return True, m.type
                if isinstance(m, N.FuncDecl) and m.name == prop:
                    return True, None
                if isinstance(m, N.FuncDecl) and m.name in ("__get", "__set"):
                    magic = True
        if magic:
            return None, None     # __get / __set accept any name
        return (False if complete else None), None

    def method_decl(self, cls_name, name):
        chain, _ = self.class_chain(cls_name)
        for c in chain:
            for m in c.members:
                if isinstance(m, N.FuncDecl) and m.name == name:
                    return m
        return None

    # -- control-flow analysis -----------------------------------------------

    def always_exits(self, stmts):
        """True when a block can never fall off its end."""
        for s in stmts:
            if isinstance(s, (N.Return, N.Raise)):
                return True
            if isinstance(s, N.ExprStmt) and isinstance(s.expr, N.Call) and isinstance(s.expr.func, N.Name) \
                    and s.expr.func.id in ("exit", "die", "dd"):
                return True
            if isinstance(s, N.If) and s.other is not None:
                if all(self.always_exits(b) for _, b in s.branches) and self.always_exits(s.other):
                    return True
            if isinstance(s, N.Switch) and s.default is not None:
                if all(self.always_exits(b) for _, b in s.cases) and self.always_exits(s.default):
                    return True
            if isinstance(s, N.Try):
                if s.final is not None and self.always_exits(s.final):
                    return True
                if self.always_exits(s.body) and all(self.always_exits(h[2]) for h in s.handlers):
                    return True
            if isinstance(s, N.While) and isinstance(s.cond, N.Literal) and s.cond.value is True:
                if not self.contains_break(s.body):
                    return True
        return False

    def contains_break(self, stmts):
        for s in stmts:
            if isinstance(s, N.Break):
                return True
            if isinstance(s, N.If):
                if any(self.contains_break(b) for _, b in s.branches) or (s.other and self.contains_break(s.other)):
                    return True
            if isinstance(s, N.Try):
                if self.contains_break(s.body) or any(self.contains_break(h[2]) for h in s.handlers) \
                        or (s.final and self.contains_break(s.final)):
                    return True
        return False

    # -- statements ------------------------------------------------------------

    def block(self, stmts, scope, new_scope=True):
        inner = Scope(scope) if new_scope else scope
        for s in stmts:
            self.stmt(s, inner)

    def declare(self, scope, name, type_ref, const, node):
        if scope.kind == "module" and (name in self.functions or name in self.classes):
            self.error(f"'{name}' is already declared in this file", node)
        if name in scope.vars:
            self.error(f"'{name}' is already declared in this scope", node)
        scope.vars[name] = (type_ref, const)

    def stmt(self, s, scope):
        k = type(s)
        if k is N.VarDecl:
            self.check_type_ref(s.type)
            if s.type.name == "void":
                self.error("variables cannot have type void", s)
            if s.value is not None:
                actual = self.expr(s.value, scope)
                if not self.compatible(s.type, actual):
                    self.error(f"variable '{s.name}' {self.describe_mismatch(s.type, actual)}", s.value)
            self.declare(scope, s.name, s.type, False, s)
        elif k is N.ConstDecl:
            actual = self.expr(s.value, scope)
            self.declare(scope, s.name, actual, True, s)
        elif k is N.Assign:
            self.assign(s, scope)
        elif k is N.ExprStmt:
            self.expr(s.expr, scope)
        elif k is N.If:
            for cond, body in s.branches:
                self.condition(cond, scope, "if condition")
                self.block(body, scope)
            if s.other is not None:
                self.block(s.other, scope)
        elif k is N.For:
            it = self.expr(s.iterable, scope)
            inner = Scope(scope)
            key_t, val_t = self.loop_types(it, s)
            if s.key is not None:
                inner.vars[s.key] = (key_t, False)
            inner.vars[s.var] = (val_t, False)
            self.loop_depth += 1
            self.block(s.body, inner, new_scope=False)
            self.loop_depth -= 1
        elif k is N.While:
            self.condition(s.cond, scope, "while condition")
            self.loop_depth += 1
            self.block(s.body, scope)
            self.loop_depth -= 1
        elif k is N.DoWhile:
            self.loop_depth += 1
            self.block(s.body, scope)
            self.loop_depth -= 1
            self.condition(s.cond, scope, "while condition")
        elif k is N.Break:
            if self.loop_depth == 0 and self.switch_depth == 0:
                self.error("'break' used outside of a loop or switch", s)
        elif k is N.Continue:
            if self.loop_depth == 0:
                self.error("'continue' used outside of a loop", s)
        elif k is N.Return:
            self.check_return(s, scope)
        elif k is N.Raise:
            if s.value is not None:
                t = self.expr(s.value, scope)
                if t is not None and (t.name in PRIMS or t.name in ("list", "dict", "set", "null")):
                    self.error(f"only Exception objects can be raised, got {tstr(t)}", s.value)
        elif k is N.Switch:
            self.expr(s.subject, scope)
            self.switch_depth += 1
            for labels, body in s.cases:
                for label in labels:
                    self.expr(label, scope)
                self.block(body, scope)
            if s.default is not None:
                self.block(s.default, scope)
            self.switch_depth -= 1
        elif k is N.Try:
            self.block(s.body, scope)
            for types, name, body, _pos in s.handlers:
                inner = Scope(scope)
                for t in types:
                    if "\\" not in t and not self.global_exists(t):
                        self.error(f"unknown exception class '{t}'", N.Node(line=_pos[0], col=_pos[1]))
                if name:
                    inner.vars[name] = (T(types[0]) if len(types) == 1 else T("Exception"), False)
                self.block(body, inner, new_scope=False)
            if s.final is not None:
                self.block(s.final, scope)
        elif k is N.FuncDecl:
            self.declare(scope, s.name, None, True, s)
            self.check_function(s, scope)
        elif k in (N.ClassDecl, N.Namespace, N.Use):
            kind = {N.ClassDecl: "class", N.Namespace: "namespace", N.Use: "use"}[k]
            self.error(f"'{kind}' is only allowed at the top level of a file", s)

    def loop_types(self, it, node):
        if it is None:
            return None, None
        if it.name == "range":
            return T("int"), T("int")
        if it.name == "list":
            return T("int"), (it.args[0] if it.args else None)
        if it.name == "set":
            return T("int"), (it.args[0] if it.args else None)
        if it.name == "dict":
            if it.args:
                return it.args[0], it.args[1]
            return None, None
        if it.name == "string":
            return T("int"), T("string")
        if it.name in ("int", "float", "bool", "null"):
            self.error(f"cannot loop over {it.name}", node.iterable)
        return None, None

    def condition(self, cond, scope, what):
        t = self.expr(cond, scope)
        if t is not None and t.name not in ("bool", "any"):
            self.error(f"{what} must be bool, got {tstr(t)} — compare explicitly, e.g. 'x != 0'", cond)

    def check_return(self, s, scope):
        actual = self.expr(s.value, scope) if s.value is not None else None
        f = self.func
        if f is None or f.ret is None:
            return
        label = f"{f.cls.name}.{f.name}" if f.cls is not None else f.name
        if f.ret.name == "void":
            if s.value is not None:
                self.error(f"{label}() is #[void] and cannot return a value", s.value)
            return
        if s.value is None:
            if f.ret.name != "any":
                self.error(f"{label}() must return {tstr(f.ret)}", s)
            return
        if not self.compatible(f.ret, actual):
            self.error(f"return value of {label}() {self.describe_mismatch(f.ret, actual)}", s.value)

    def assign(self, s, scope):
        target = s.target
        actual = self.expr(s.value, scope)
        if isinstance(target, N.Name):
            entry = scope.find(target.id)
            if entry is None:
                if target.id in self.functions or target.id in self.classes:
                    self.error(f"cannot assign to '{target.id}' — it is a function or class", target)
                elif target.id in self.builtin_names:
                    self.error(f"cannot assign to built-in '{target.id}'", target)
                else:
                    self.error(f"'{target.id}' is not declared — give it a type, e.g. "
                               f"'{self.suggest(actual)} {target.id} = ...'", target)
                return
            declared, const = entry
            if const:
                self.error(f"cannot reassign constant '{target.id}'", target)
                return
            if s.op == "=":
                if not self.compatible(declared, actual):
                    self.error(f"variable '{target.id}' {self.describe_mismatch(declared, actual)}", s.value)
            else:
                result = self.arith(s.op[:-1], declared, actual, s)
                if result is not None and not self.compatible(declared, result):
                    self.error(f"variable '{target.id}' {self.describe_mismatch(declared, result)}", s.value)
        elif isinstance(target, N.Attr):
            obj_t = self.expr(target.obj, scope)
            if isinstance(target.obj, N.This) and self.func is not None and self.func.cls is not None:
                found, ptype = self.prop_type(self.func.cls.name, target.name)
                if found is False:
                    self.error(f"{self.func.cls.name} has no property '{target.name}' — declare it in the class body", target)
                elif found and s.op == "=" and not self.compatible(ptype, actual):
                    self.error(f"property '{self.func.cls.name}.{target.name}' {self.describe_mismatch(ptype, actual)}", s.value)
            elif obj_t is not None and obj_t.name in self.classes and self.classes[obj_t.name].is_enum:
                self.error(f"enum cases are read-only: cannot set '{target.name}' on a {obj_t.name}", target)
            elif obj_t is not None and obj_t.name in self.classes:
                found, ptype = self.prop_type(obj_t.name, target.name)
                if found is False:
                    self.error(f"{obj_t.name} has no property '{target.name}'", target)
                elif found and s.op == "=" and not self.compatible(ptype, actual):
                    self.error(f"property '{obj_t.name}.{target.name}' {self.describe_mismatch(ptype, actual)}", s.value)
        else:
            self.expr(target, scope)

    def suggest(self, t):
        if t is None or t.name in ("null", "void"):
            return "any"
        return tstr(t)

    # -- expressions -------------------------------------------------------

    def expr(self, e, scope):
        """Walk an expression, report problems, return its static type or None."""
        k = type(e)
        if k is N.Literal:
            v = e.value
            if v is None:
                return T("null")
            if isinstance(v, bool):
                return T("bool")
            if isinstance(v, int):
                return T("int")
            if isinstance(v, float):
                return T("float")
            return T("string")
        if k is N.Interp:
            for p in e.parts:
                if not isinstance(p, str):
                    self.expr(p, scope)
            return T("string")
        if k is N.Name:
            entry = scope.find(e.id)
            if entry is not None:
                return entry[0]
            if e.id in self.functions:
                return None
            if e.id in ("parent", "self"):
                if self.func is None or self.func.cls is None:
                    self.error(f"'{e.id}::' can only be used inside a class", e)
                return None
            if not self.global_exists(e.id):
                self.error(f"'{e.id}' is not defined", e)
            return None
        if k is N.This:
            f = self.func
            if f is None or f.cls is None:
                self.error("'this' can only be used inside a class method", e)
            elif f.is_static:
                self.error(f"'this' cannot be used in static method {f.cls.name}::{f.name}()", e)
            else:
                return T(f.cls.name)
            return None
        if k is N.ListLit:
            types = [self.expr(x, scope) for x in e.items]
            return T("list", self.common(types)) if types and self.common(types) else T("list")
        if k is N.SetLit:
            types = [self.expr(x, scope) for x in e.items]
            return T("set", self.common(types)) if types and self.common(types) else T("set")
        if k is N.DictLit:
            kts = [self.expr(a, scope) for a, _ in e.pairs]
            vts = [self.expr(b, scope) for _, b in e.pairs]
            kt, vt = self.common(kts), self.common(vts)
            return T("dict", kt, vt) if e.pairs and kt and vt else T("dict")
        if k is N.Unary:
            t = self.expr(e.operand, scope)
            if e.op == "not":
                if t is not None and t.name not in ("bool", "any"):
                    self.error(f"'not' needs a bool, got {tstr(t)}", e)
                return T("bool")
            if t is not None and t.name not in ("int", "float", "any"):
                self.error(f"unary '{e.op}' needs a number, got {tstr(t)}", e)
            return t
        if k is N.Binary:
            return self.arith(e.op, self.expr(e.left, scope), self.expr(e.right, scope), e)
        if k is N.Logical:
            a = self.expr(e.left, scope)
            b = self.expr(e.right, scope)
            if e.op == "??":
                return b if a is None or a.name == "null" else a
            for side, t in (("left", a), ("right", b)):
                if t is not None and t.name not in ("bool", "any"):
                    self.error(f"{side} side of '{e.op}' must be bool, got {tstr(t)}", e)
            return T("bool")
        if k is N.Compare:
            a = self.expr(e.left, scope)
            b = self.expr(e.right, scope)
            if e.op in ("<", ">", "<=", ">=") and a is not None and b is not None:
                nums = {"int", "float"}
                ok = (a.name in nums and b.name in nums) or (a.name == b.name == "string") or "any" in (a.name, b.name)
                if not ok:
                    self.error(f"cannot compare {tstr(a)} {e.op} {tstr(b)}", e)
            return T("bool")
        if k is N.IsType:
            self.expr(e.expr, scope)
            self.check_type_ref(e.type)
            return T("bool")
        if k is N.Ternary:
            self.condition(e.cond, scope, "ternary condition")
            a = self.expr(e.then, scope)
            b = self.expr(e.other, scope)
            if a is not None and b is not None and tstr(a) == tstr(b):
                return a
            return None
        if k is N.Cast:
            self.check_type_ref(e.type)
            self.expr(e.expr, scope)
            return e.type
        if k is N.Call:
            return self.call(e, scope)
        if k is N.Attr:
            obj_t = self.expr(e.obj, scope)
            if obj_t is not None and obj_t.name in ("int", "float", "bool"):
                self.error(f"{obj_t.name} has no property or method '{e.name}'", e)
            if obj_t is not None and obj_t.name == "null" and not e.safe:
                self.error(f"cannot read '{e.name}' of null", e)
            if obj_t is not None and obj_t.name in self.classes:
                found, ptype = self.prop_type(obj_t.name, e.name)
                if found is False:
                    self.error(f"{obj_t.name} has no property or method '{e.name}'", e)
                return ptype
            return None
        if k is N.Index:
            obj_t = self.expr(e.obj, scope)
            idx_t = self.expr(e.index, scope)
            if obj_t is not None:
                if obj_t.name in ("list", "string", "range") and idx_t is not None and idx_t.name not in ("int", "any"):
                    self.error(f"{obj_t.name} index must be int, got {tstr(idx_t)}", e.index)
                if obj_t.name == "list" and obj_t.args:
                    return obj_t.args[0]
                if obj_t.name == "dict" and obj_t.args:
                    return obj_t.args[1]
                if obj_t.name == "string":
                    return T("string")
                if obj_t.name in ("int", "float", "bool", "null"):
                    self.error(f"{obj_t.name} cannot be indexed with []", e)
            return None
        if k is N.StaticRef:
            self.expr(e.target, scope)
            if isinstance(e.target, N.Name):
                decl = self.classes.get(e.target.id)
                if decl is not None and decl.is_enum:
                    if any(c.name == e.name for c in decl.cases):
                        return T(decl.name)
                    builtin = ("cases", "from", "tryFrom") if decl.backing else ("cases",)
                    if e.name in ("from", "tryFrom") and not decl.backing:
                        self.error(f"only enums with values have {e.name}() — put #[String] (or #[Int]) above 'enum {decl.name}'", e)
                    elif e.name not in builtin and not any(getattr(m, "name", None) == e.name for m in decl.members):
                        names = ", ".join(c.name for c in decl.cases)
                        self.error(f"enum {decl.name} has no case '{e.name}' (cases: {names})", e)
            return None
        if k is N.New:
            new_types = [self.expr(a, scope) for a in e.args]
            name = e.cls
            if "\\" in name:
                return T(name)
            if not self.global_exists(name):
                self.error(f"unknown class '{name}'", e)
                return None
            decl = self.classes.get(name)
            if decl is not None and decl.is_enum:
                self.error(f"cannot create an instance of enum {name} — use a case such as {name}::{decl.cases[0].name}", e)
                return T(name)
            if decl is not None:
                if decl.is_abstract:
                    what = decl.kind if decl.kind in ("interface", "trait") else "abstract class"
                    self.error(f"cannot create an instance of {what} {name}", e)
                ctor = self.method_decl(name, "__construct")
                chain, complete = self.class_chain(name)
                if ctor is not None:
                    self.check_args(ctor.params, e.args, new_types, f"{name}.__construct", e)
                elif ctor is None and complete and e.args:
                    self.error(f"{name} has no __construct() but {len(e.args)} argument(s) were given", e)
            return T(name)
        if k is N.NamedArg:
            return self.expr(e.value, scope)
        if k is N.Spread:
            t = self.expr(e.value, scope)
            if t is not None and t.name not in ("list", "any"):
                self.error(f"only a list can be spread into arguments with *, got {tstr(t)}", e.value)
            return None
        if k is N.Lambda:
            self.check_defaults(e.params, scope)
            fscope = Scope(scope, kind="function")
            for p in e.params:
                self.check_type_ref(p.type)
                fscope.vars[p.name] = (p.type, False)
            outer = (self.func, self.loop_depth, self.switch_depth)
            enclosing = self.func
            self.func = FuncCtx("<lambda>", e.ret, enclosing.cls if enclosing else None,
                                enclosing.is_static if enclosing else False, is_lambda=True)
            self.loop_depth = self.switch_depth = 0
            if e.is_expr:
                actual = self.expr(e.body, fscope)
                if e.ret is not None and not self.compatible(e.ret, actual):
                    self.error(f"lambda return value {self.describe_mismatch(e.ret, actual)}", e.body)
            else:
                self.block(e.body, fscope, new_scope=False)
                if e.ret is not None and e.ret.name not in ("void", "any") and not self.always_exits(e.body):
                    self.error(f"lambda must return {tstr(e.ret)} on every path", e)
            self.func, self.loop_depth, self.switch_depth = outer
            return None
        return None

    def common(self, types):
        if not types or any(t is None for t in types):
            return None
        first = tstr(types[0])
        if all(tstr(t) == first for t in types):
            return types[0]
        return None

    def arith(self, op, a, b, node):
        if a is None or b is None or "any" in (a.name, b.name):
            return None
        an, bn = a.name, b.name
        if an == bn and an in ("int", "float"):
            if op == "**" and an == "int" and isinstance(getattr(node, "right", None), N.Unary) and node.right.op == "-":
                self.error("int ** negative int is not an int — use floats", node)
            return a
        if op == "+" and an == bn == "string":
            return a
        if op == "+" and an == bn == "list":
            return a
        hint = ""
        if {an, bn} == {"int", "float"}:
            hint = " — convert one side with ~float(...) or ~int(...)"
        elif op == "+" and "string" in (an, bn):
            hint = " — use ~string(...) or interpolation: \"{x}\""
        self.error(f"cannot apply '{op}' to {tstr(a)} and {tstr(b)}{hint}", node)
        return None

    def check_args(self, params, args, types, label, node):
        """Arguments of a call to a function declared in this file: count, names, types."""
        if any(type(a) is N.Spread for a in args):
            return          # how many arguments a spread gives is known only when it runs
        n = len(params)
        variadic = n > 0 and params[-1].variadic
        fixed = n - 1 if variadic else n
        pos = [(a, t) for a, t in zip(args, types) if type(a) is not N.NamedArg]
        named = [(a, t) for a, t in zip(args, types) if type(a) is N.NamedArg]
        if len(pos) > fixed and not variadic:
            self.error(f"{label}() expects {arity_text(params)}, got {len(args)}", node)
            return
        filled = set()
        for i, (a, t) in enumerate(pos[:fixed]):
            filled.add(i)
            if not self.compatible(params[i].type, t):
                self.error(f"argument '{params[i].name}' of {label}() {self.describe_mismatch(params[i].type, t)}", a)
        if variadic:
            et = params[-1].type.args[0] if params[-1].type.args else None
            for j, (a, t) in enumerate(pos[fixed:]):
                if not self.compatible(et, t):
                    self.error(f"argument {fixed + j + 1} of {label}() {self.describe_mismatch(et, t)}", a)
        index = {p.name: i for i, p in enumerate(params)}
        for a, t in named:
            i = index.get(a.name)
            if i is None:
                names = ", ".join(p.name for p in params) or "none"
                self.error(f"{label}() has no parameter named '{a.name}' (parameters: {names})", a)
                continue
            if params[i].variadic:
                self.error(f"{label}(): the variadic parameter '{a.name}' cannot be passed by name", a)
                continue
            if i in filled:
                self.error(f"{label}() got two values for '{a.name}'", a)
                continue
            filled.add(i)
            if not self.compatible(params[i].type, t):
                self.error(f"argument '{a.name}' of {label}() {self.describe_mismatch(params[i].type, t)}", a)
        missing = [p for i, p in enumerate(params[:fixed]) if i not in filled and p.default is None]
        if missing:
            if named:
                self.error(f"{label}() is missing argument '{missing[0].name}'", node)
            else:
                self.error(f"{label}() expects {arity_text(params)}, got {len(args)}", node)

    def call(self, e, scope):
        func = e.func
        arg_types = [self.expr(a, scope) for a in e.args]
        decl = None
        label = None
        if isinstance(func, N.Name):
            if scope.find(func.id) is None and func.id in self.functions:
                decl = self.functions[func.id]
                label = func.id
            elif scope.find(func.id) is not None and scope.find(func.id)[0] is not None:
                t = scope.find(func.id)[0]
                if t.name in PRIMS or t.name in ("list", "dict", "set", "null"):
                    self.error(f"'{func.id}' is a {tstr(t)}, not a function", func)
                return None
            else:
                self.expr(func, scope)
                if func.id in ("len", "type", "input"):
                    return T("int") if func.id == "len" else T("string")
                if func.id == "range":
                    for a, t in zip(e.args, arg_types):
                        if t is not None and t.name not in ("int", "any"):
                            self.error(f"range() needs int arguments, got {tstr(t)}", a)
                    return T("range")
                return None
        elif isinstance(func, N.Attr) and isinstance(func.obj, N.This) and self.func is not None and self.func.cls is not None:
            self.expr(func.obj, scope)
            m = self.method_decl(self.func.cls.name, func.name)
            found, _ = self.prop_type(self.func.cls.name, func.name)
            if m is None and found is False:
                self.error(f"{self.func.cls.name} has no method '{func.name}'", func)
            decl, label = m, f"{self.func.cls.name}.{func.name}"
        elif isinstance(func, N.Attr):
            obj_t = self.expr(func.obj, scope)
            if obj_t is not None and obj_t.name in self.classes:
                m = self.method_decl(obj_t.name, func.name)
                found, _ = self.prop_type(obj_t.name, func.name)
                if m is None and found is False:
                    self.error(f"{obj_t.name} has no method '{func.name}'", func)
                decl, label = m, f"{obj_t.name}.{func.name}"
            elif obj_t is not None and obj_t.name in ("int", "float", "bool"):
                self.error(f"{obj_t.name} has no method '{func.name}'", func)
            elif obj_t is not None and obj_t.name == "string" and func.name in ("upper", "lower", "trim", "replace", "substr", "repeat", "reverse", "slug", "capitalize", "pad"):
                return T("string")
            elif obj_t is not None and func.name in ("len", "length", "indexOf"):
                return T("int")
            elif obj_t is not None and func.name in ("contains", "startsWith", "endsWith", "has", "isEmpty"):
                return T("bool")
        else:
            self.expr(func, scope)
        if decl is None or not isinstance(decl, N.FuncDecl):
            return None
        self.check_args(decl.params, e.args, arg_types, label, e)
        ret = decl.ret
        if ret is not None and ret.name == "void":
            return T("void")
        return ret


def check(program, source, builtin_names, find_file=None):
    return Checker(program, source, builtin_names, find_file=find_file).check()
