"""Tree-walking interpreter for Barq.

This is the reference implementation from the roadmap (Phase 2). It favours
clear behaviour and good error messages over speed; the bytecode VM comes later.
"""

import os
import sys

from . import nodes as N
from .errors import BarqError, SourceFile
from .parser import parse
from .runtime import (
    BarqClass, BarqFunction, BarqObject, BarqSet, BarqThrow, BoundMethod, BreakSignal,
    ContinueSignal, Env, Module, NativeFunction, NativeModule, NON_NULLABLE, PropInfo,
    ReturnSignal, hash_key, to_display, type_name, type_str, values_equal,
)

MAX_CALL_DEPTH = 5000
COLLECTION_TYPES = {"list": 1, "dict": 2, "set": 1}
from .hooks import RELATION_TYPES     # filled by barq-framework (HasMany, BelongsTo, ...)
BUILTIN_TYPES = {"int", "float", "string", "bool", "void", "any", "null", "list", "dict", "set", "range", "karo"}



IMPORT_PENDING = object()    # placeholder for an import that shadows a stdlib module
MISSING = object()           # a parameter that no argument filled (yet)


def arity_text(params):
    """'2 arguments', '1 to 3 arguments', 'at least 1 argument'."""
    n = len(params)
    variadic = n > 0 and params[-1].variadic
    required = sum(1 for p in params if p.default is None and not p.variadic)
    if variadic:
        return f"at least {required} argument{'s' if required != 1 else ''}"
    if required == n:
        return f"{n} argument{'s' if n != 1 else ''}"
    return f"{required} to {n} arguments"


def module_candidates(parts):
    """Files a `use A\\B\\C` may live in, relative to the project root: A/B/C.bq, then
    a/B/C.bq (App\\Models -> app/Models), then a/b/C.bq (Database\\Seeders -> database/seeders)."""
    out = [os.path.join(*parts) + ".bq"]
    if len(parts) > 1:
        out.append(os.path.join(parts[0].lower(), *parts[1:]) + ".bq")
        lower = os.path.join(*[x.lower() for x in parts[:-1]], parts[-1]) + ".bq"
        if lower not in out:
            out.append(lower)
    return out

class Interpreter:
    def __init__(self, root=None, stdout=None, stdin=None, argv=None, static_checks=True):
        self.static_checks = static_checks
        self.root = os.path.abspath(root or os.getcwd())
        self.stdout = stdout or sys.stdout
        self.stdin = stdin or sys.stdin
        self.argv = list(argv or [])
        self.builtins = Env()
        self.modules = {}      # absolute path -> Module
        self.classes = {}      # fully-qualified name -> BarqClass
        self.functions = {}    # fully-qualified name -> BarqFunction (public ones)
        self.stack = []        # call frames: (function name, Module, line)
        self.module = None     # module whose code is running now
        self.line = 0          # line of the statement running now
        self.handling = []     # exceptions being handled (for bare `raise`)
        self._type_cache = {}
        from .builtins import install
        install(self)
        self.exec_table = {
            N.VarDecl: self.exec_vardecl, N.ConstDecl: self.exec_const, N.Assign: self.exec_assign,
            N.ExprStmt: self.exec_expr, N.If: self.exec_if, N.For: self.exec_for,
            N.While: self.exec_while, N.DoWhile: self.exec_dowhile, N.Break: self.exec_break,
            N.Continue: self.exec_continue, N.Return: self.exec_return, N.Raise: self.exec_raise,
            N.Switch: self.exec_switch, N.Try: self.exec_try,
            N.FuncDecl: self.exec_nested_func, N.ClassDecl: self.exec_misplaced,
            N.Namespace: self.exec_misplaced, N.Use: self.exec_misplaced,
        }
        self.eval_table = {
            N.Literal: self.eval_literal, N.Interp: self.eval_interp, N.Name: self.eval_name,
            N.This: self.eval_this, N.ListLit: self.eval_list, N.DictLit: self.eval_dict,
            N.SetLit: self.eval_set, N.Unary: self.eval_unary, N.Binary: self.eval_binary,
            N.Logical: self.eval_logical, N.Compare: self.eval_compare, N.IsType: self.eval_is,
            N.Ternary: self.eval_ternary, N.Cast: self.eval_cast, N.Call: self.eval_call,
            N.Attr: self.eval_attr, N.Index: self.eval_index, N.StaticRef: self.eval_static,
            N.New: self.eval_new, N.Lambda: self.eval_lambda,
        }

    # =====================================================================
    # Errors
    # =====================================================================

    def make_exception(self, cls_name, message, node=None):
        cls = self.classes[cls_name]
        obj = BarqObject(cls)
        obj.fields["message"] = message
        obj.fields["code"] = 0
        self.stamp(obj, node)
        return obj

    def stamp(self, obj, node=None):
        """Record where an exception was raised and the call stack at that point."""
        if obj.origin is None:
            tpl = getattr(node, "tpl_src", None)
            if tpl is not None:   # raised inside a template expression
                obj.origin = (tpl, node.line, node.col)
                obj.trace = list(self.stack)
                return
            line = node.line if node is not None and node.line else self.line
            col = node.col if node is not None else 0
            source = self.module.source if self.module else None
            obj.origin = (source, line, col)
            obj.trace = list(self.stack)

    def throw(self, cls_name, message, node=None):
        raise BarqThrow(self.make_exception(cls_name, message, node))

    # =====================================================================
    # Modules, hoisting and name resolution
    # =====================================================================

    def run_file(self, path):
        return self.load_module(path, entry=True)

    def parse_and_check(self, source):
        program = parse(source)
        if self.static_checks:
            from .checker import check
            from .errors import CheckErrors
            errors = check(program, source, set(self.builtins.vars), self.find_module_file)
            if errors:
                raise CheckErrors(errors)
        return program

    def run_source(self, text, path="<string>"):
        source = SourceFile(path, text)
        program = self.parse_and_check(source)
        module = Module(path, source, Env(self.builtins))
        self.modules[path] = module
        self.setup_module(module, program.body)
        self.exec_module(module, program.body)
        return module

    def load_module(self, path, entry=False):
        path = os.path.abspath(path)
        if path in self.modules:
            return self.modules[path]
        with open(path, encoding="utf-8") as f:
            text = f.read()
        display = os.path.relpath(path, self.root) if path.startswith(self.root) else path
        source = SourceFile(display, text)
        program = self.parse_and_check(source)
        module = Module(path, source, Env(self.builtins))
        self.modules[path] = module
        prev = self.module
        self.module = module
        try:
            self.setup_module(module, program.body)
            self.exec_module(module, program.body)
        finally:
            self.module = prev
        return module

    def setup_module(self, module, body):
        """Register a file's namespace, imports, functions and classes before
        any of its statements run, so declaration order does not matter."""
        module.imports = {}
        prev = self.module
        self.module = module
        try:
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
                            # `use App\Middleware\Auth` wins over the Auth module (resolved on first use)
                            module.env.declare(short, IMPORT_PENDING, None, const=True)
            classes = []
            for stmt in body:
                if isinstance(stmt, N.FuncDecl):
                    self.declare_function(module, stmt)
                elif isinstance(stmt, N.ClassDecl):
                    classes.append(self.declare_class(module, stmt))
            for cls in classes:
                self.finish_class(cls)
        finally:
            self.module = prev

    def compile_error(self, message, node):
        from .errors import CheckError
        raise CheckError(message, self.module.source if self.module else None, node.line, node.col)

    def declare_function(self, module, decl):
        if decl.is_abstract:
            self.compile_error("only class methods can be abstract", decl)
        if decl.name in module.env.vars:
            self.compile_error(f"'{decl.name}' is already declared in this file", decl)
        if decl.name in self.builtins.vars:
            self.compile_error(f"'{decl.name}' is a reserved global function and cannot be redefined", decl)
        fn = BarqFunction(decl.name, decl.params, decl.ret, decl.body, module.env, module,
                          visibility=decl.visibility or "module", line=decl.line)
        module.env.declare(decl.name, fn, None, const=True)
        module.functions[decl.name] = fn
        if decl.visibility == "public":
            self.functions[module.qualify(decl.name)] = fn

    def declare_class(self, module, decl):
        if decl.name in module.env.vars:
            self.compile_error(f"'{decl.name}' is already declared in this file", decl)
        builtin = self.builtins.vars.get(decl.name)
        # Stdlib modules (Log, Date, Cache...) may be shadowed by your own class;
        # the module stays reachable with `use Barq\Log as BarqLog`.
        if (builtin is not None and not isinstance(builtin[0], NativeModule)) or decl.name in BUILTIN_TYPES:
            self.compile_error(f"'{decl.name}' is a reserved name and cannot be used as a class name", decl)
        fqn = module.qualify(decl.name)
        if fqn in self.classes:
            self.compile_error(f"class {fqn} is already defined", decl)
        cls = BarqClass(decl.name, fqn, module, is_abstract=decl.is_abstract)
        cls.decl = decl
        cls.is_enum = bool(decl.is_enum)
        cls.backing = decl.backing
        cls.kind = decl.kind or ("enum" if decl.is_enum else "class")
        module.env.declare(decl.name, cls, None, const=True)
        module.classes[decl.name] = cls
        self.classes[fqn] = cls
        return cls

    def finish_class(self, cls):
        if cls.finished:
            return
        cls.finished = True
        decl = cls.decl
        if cls.kind == "trait":
            return      # a trait's members are copied into each class that uses it
        if decl.parent:
            parent = self.resolve_class(decl.parent, cls.module, decl)
            if parent is cls or parent.is_subclass_of(cls):
                self.compile_error(f"class {cls.name} cannot extend itself", decl)
            if parent.kind == "interface":
                self.compile_error(f"{parent.name} is an interface — write 'class {cls.name} implements {parent.name}'", decl)
            if parent.kind == "trait":
                self.compile_error(f"{parent.name} is a trait — write 'use {parent.name}' inside class {cls.name}", decl)
            if parent.kind == "enum":
                self.compile_error(f"class {cls.name} cannot extend enum {parent.name}", decl)
            if parent.decl is not None:
                self.finish_class(parent)    # parents first, so inherited methods are known
            cls.parent = parent
        for iname in decl.interfaces or []:
            iface = self.resolve_class(iname, cls.module, decl)
            if iface.kind != "interface":
                if cls.kind == "interface":
                    self.compile_error(f"interface {cls.name} can only extend interfaces, and {iface.name} is a {iface.kind}", decl)
                hint = (f" — a class extends it: class {cls.name} extends {iface.name}" if iface.kind == "class"
                        else f" — write 'use {iface.name}' inside {cls.name}" if iface.kind == "trait" else "")
                self.compile_error(f"{iface.name} is not an interface{hint}", decl)
            if iface is cls or iface.is_subclass_of(cls):
                self.compile_error(f"interface {cls.name} cannot extend itself", decl)
            if iface.decl is not None:
                self.finish_class(iface)
            if iface not in cls.interfaces:
                cls.interfaces.append(iface)
        if cls.is_enum:
            self.finish_enum(cls)   # cases first, so constants can refer to them
        for m in decl.members:
            if isinstance(m, N.FuncDecl):
                if m.name in cls.methods:
                    self.compile_error(f"method '{m.name}' is declared twice in {cls.name}", m)
                if m.is_abstract and not cls.is_abstract:
                    self.compile_error(f"class {cls.name} has abstract method '{m.name}' so it must be declared 'abstract class'", m)
                cls.methods[m.name] = BarqFunction(
                    m.name, m.params, m.ret, m.body, cls.module.env, cls.module,
                    visibility=m.visibility or "public", is_static=m.is_static,
                    is_abstract=m.is_abstract, owner=cls, line=m.line)
            elif isinstance(m, N.Property):
                if m.name in cls.props or m.name in cls.statics:
                    self.compile_error(f"property '{m.name}' is declared twice in {cls.name}", m)
                self.check_type_known(m.type, cls.module, m)
                info = PropInfo(m.name, m.type, m.visibility, m.value, cls, is_static=m.is_static)
                if m.is_static:
                    cls.statics[m.name] = info
                    value = self.eval(m.value, cls.module.env) if m.value is not None else self.zero_value(m.type)
                    self.check_assign(value, m.type, f"static property '{cls.name}::{m.name}'", m)
                    cls.static_values[m.name] = value
                else:
                    cls.props[m.name] = info
            elif isinstance(m, N.ConstDecl):
                if m.name in cls.statics:
                    self.compile_error(f"'{m.name}' is declared twice in {cls.name}", m)
                cls.statics[m.name] = PropInfo(m.name, None, "public", None, cls, is_static=True, const=True)
                cls.static_values[m.name] = self.eval(m.value, cls.module.env)
        self.add_traits(cls)
        if not cls.is_abstract:
            missing = cls.abstract_methods()
            if missing:
                name, owner = next(iter(missing.items()))
                m = owner.methods.get(name)
                source = f"trait {m.trait.name}" if getattr(m, "trait", None) is not None else owner.name
                self.compile_error(f"class {cls.name} must implement abstract method '{name}()' from {source}", decl)
            self.check_interfaces(cls)

    # -- traits & interfaces ---------------------------------------------------

    def trait_members(self, decl, module, stack=()):
        """(member, trait, name, visibility) tuples from the `use` lines of a
        class or trait body, nested traits included, after the `insteadof` / `as`
        rules written under those lines. `name` is what the member is called in
        the class (an alias differs from member.name); `visibility` is None
        unless a rule changed it. A trait's own members win over its traits'."""
        used = []          # (trait, its items), in `use` order
        rules = []
        for u in decl.members:
            if not isinstance(u, N.UseTrait):
                continue
            rules.extend(u.rules or [])
            for tname in u.names:
                trait = self.resolve_class(tname, module, u)
                if trait.kind != "trait":
                    hint = (f" — write 'implements {trait.name}' after the class name" if trait.kind == "interface"
                            else f" — write 'extends {trait.name}' after the class name" if trait.kind == "class" else "")
                    self.compile_error(f"{trait.name} is not a trait{hint}", u)
                if trait in stack:
                    self.compile_error(f"trait {trait.name} uses itself", u)
                own = [(m, trait, m.name, None) for m in trait.decl.members
                       if isinstance(m, (N.FuncDecl, N.Property, N.ConstDecl))]
                names = {m.name for m in trait.decl.members if isinstance(m, (N.FuncDecl, N.Property, N.ConstDecl))}
                inner = self.trait_members(trait.decl, trait.module, stack + (trait,))
                used.append((trait, [x for x in inner if x[2] not in names] + own))
        if rules:
            self.apply_trait_rules(decl, module, used, rules)
        return [x for _, items in used for x in items]

    def apply_trait_rules(self, decl, module, used, rules):
        def find_trait(name, rule):
            cls = self.resolve_class(name, module, rule)
            for i, (trait, _) in enumerate(used):
                if trait is cls:
                    return i
            self.compile_error(f"trait {cls.name} is not used by {decl.name} — add it to the 'use' line", rule)

        def methods_named(i, name, items=None):
            items = used[i][1] if items is None else items[i]
            return [x for x in items if x[2] == name and isinstance(x[0], N.FuncDecl)]

        def no_such(i, rule):
            trait = used[i][0]
            other = next((x for x in used[i][1] if x[2] == rule.method), None)
            if other is not None:
                self.compile_error(f"'{rule.method}' in trait {trait.name} is not a method — only methods "
                                   "can be chosen or renamed", rule)
            self.compile_error(f"trait {trait.name} has no method '{rule.method}()'", rule)

        prev_module = self.module
        self.module = module
        try:
            # insteadof first, so `as` rules see which method won. A new name
            # can still be given to the method that lost (`Salaam::greet as salaam`).
            before = [list(items) for _, items in used]
            for rule in rules:
                if rule.insteadof is None:
                    continue
                keep = find_trait(rule.trait, rule)
                if not methods_named(keep, rule.method):
                    no_such(keep, rule)
                for other in rule.insteadof:
                    j = find_trait(other, rule)
                    if j == keep:
                        self.compile_error(f"{used[j][0].name}::{rule.method} cannot be used instead of itself", rule)
                    if not methods_named(j, rule.method):
                        no_such(j, rule)
                    used[j] = (used[j][0], [x for x in used[j][1]
                                            if not (x[2] == rule.method and isinstance(x[0], N.FuncDecl))])
            for rule in rules:
                if rule.insteadof is not None:
                    continue
                source = before if rule.alias is not None else None
                if rule.trait is not None:
                    i = find_trait(rule.trait, rule)
                    found = methods_named(i, rule.method, source)
                    if not found:
                        no_such(i, rule)
                else:
                    hits = [(i, x) for i in range(len(used)) for x in methods_named(i, rule.method, source)]
                    if not hits:
                        self.compile_error(f"no trait used by {decl.name} has a method '{rule.method}()'", rule)
                    if len({id(x[0]) for _, x in hits}) > 1:
                        a, b = used[hits[0][0]][0].name, used[hits[1][0]][0].name
                        self.compile_error(f"'{rule.method}' comes from both trait {a} and trait {b} — "
                                           f"write {a}::{rule.method} as ...", rule)
                    i = hits[0][0]
                    found = [hits[0][1]]
                m, trait, name, vis = found[0]
                if rule.alias is not None:
                    if m.is_abstract:
                        self.compile_error(f"abstract method '{rule.method}()' cannot be given another name", rule)
                    used[i][1].append((m, trait, rule.alias, rule.visibility or vis))
                else:
                    used[i] = (used[i][0], [(x[0], x[1], x[2], rule.visibility) if x is found[0] else x
                                            for x in used[i][1]])
        finally:
            self.module = prev_module

    def add_traits(self, cls):
        """Copy trait members into cls as if they were written there. Methods
        written in the class win; two traits giving the same method is an error
        unless an `insteadof` rule chooses one."""
        items = self.trait_members(cls.decl, cls.module)
        if not items:
            return
        own = {m.name for m in cls.decl.members if isinstance(m, (N.FuncDecl, N.Property, N.ConstDecl))}
        came_from = {}
        prev_module = self.module
        try:
            for m, trait, name, vis in items:
                if name in own:
                    # A method written in the class replaces the trait's; a property cannot.
                    if isinstance(m, N.Property):
                        self.compile_error(f"property '{name}' is declared in both {cls.name} and trait {trait.name}", cls.decl)
                    continue
                seen = came_from.get(name)
                if seen is not None:
                    if seen[0] is m:
                        continue                     # the same trait reached twice
                    if isinstance(m, N.FuncDecl) and isinstance(seen[0], N.FuncDecl):
                        if m.is_abstract:
                            continue
                        if not seen[0].is_abstract:
                            a, b = seen[1].name, trait.name
                            self.compile_error(f"method '{name}()' comes from both trait {a} and trait {b} — "
                                               f"choose one under the 'use' line ({a}::{name} insteadof {b}) "
                                               f"or write {name}() in {cls.name}", cls.decl)
                    else:
                        self.compile_error(f"'{name}' comes from both trait {seen[1].name} and trait {trait.name}", cls.decl)
                came_from[name] = (m, trait)
                self.module = trait.module
                if isinstance(m, N.FuncDecl):
                    if m.is_abstract and cls.parent is not None:
                        inherited = cls.parent.find_method(name)
                        if inherited is not None and not inherited.is_abstract:
                            continue
                    fn = BarqFunction(name, m.params, m.ret, m.body, trait.module.env, trait.module,
                                      visibility=vis or m.visibility or "public", is_static=m.is_static,
                                      is_abstract=m.is_abstract, owner=cls, line=m.line)
                    fn.trait = trait
                    fn.trait_method = m.name
                    cls.methods[name] = fn
                elif isinstance(m, N.Property):
                    if cls.is_enum:
                        self.compile_error(f"enum {cls.name} cannot use trait {trait.name}: it has property '{m.name}'", cls.decl)
                    self.check_type_known(m.type, trait.module, m)
                    info = PropInfo(m.name, m.type, m.visibility, m.value, cls, is_static=m.is_static, module=trait.module)
                    if m.is_static:
                        cls.statics[m.name] = info
                        value = self.eval(m.value, trait.module.env) if m.value is not None else self.zero_value(m.type)
                        self.check_assign(value, m.type, f"static property '{cls.name}::{m.name}'", m)
                        cls.static_values[m.name] = value
                    else:
                        cls.props[m.name] = info
                else:
                    cls.statics[m.name] = PropInfo(m.name, None, "public", None, cls, is_static=True, const=True)
                    cls.static_values[m.name] = self.eval(m.value, trait.module.env)
        finally:
            self.module = prev_module

    def check_interfaces(self, cls):
        """A concrete class has every method its interfaces declare, public and
        taking the same arguments."""
        for iface in cls.all_interfaces():
            for m in iface.decl.members:
                if not isinstance(m, N.FuncDecl):
                    continue
                impl = cls.find_method(m.name)
                if impl is None or impl.is_abstract:
                    self.compile_error(f"class {cls.name} must implement method '{m.name}()' from interface {iface.name}", cls.decl)
                if impl.visibility != "public":
                    self.compile_error(f"{cls.name}.{m.name}() must be public: interface {iface.name} declares it", cls.decl)
                if bool(impl.is_static) != bool(m.is_static):
                    want = "static" if m.is_static else "not static"
                    self.compile_error(f"{cls.name}.{m.name}() must be {want}, as in interface {iface.name}", cls.decl)
                params = getattr(impl, "params", None)
                if params is None:
                    continue
                takes = len(params) if not (params and params[-1].variadic) else 10 ** 6
                required = sum(1 for p in params if p.default is None and not p.variadic)
                iface_required = sum(1 for p in m.params if p.default is None and not p.variadic)
                if takes < len(m.params) or required > iface_required:
                    self.compile_error(f"{cls.name}.{m.name}() must accept the arguments of interface {iface.name}: "
                                       f"it takes {arity_text(params)}, the interface {arity_text(m.params)}", cls.decl)

    def exec_module(self, module, body):
        prev = self.module
        self.module = module
        try:
            for stmt in body:
                if isinstance(stmt, (N.FuncDecl, N.ClassDecl, N.Namespace, N.Use)):
                    continue
                self.exec(stmt, module.env)
        except ReturnSignal:
            pass
        finally:
            self.module = prev

    def resolve_name(self, name, module, node):
        """Look up a name that is not a local variable: imports, then classes in
        the same namespace (autoloaded from disk if needed)."""
        imports = getattr(module, "imports", {})
        if name in imports:
            fqn, use_node = imports[name]
            value = self.lookup_fqn(fqn, use_node, module)
            module.env.declare(name, value, None, const=True)
            return value
        if module.namespace:
            fqn = module.qualify(name)
            value = self.lookup_fqn(fqn, node, module, required=False)
            if value is not None:
                module.env.declare(name, value, None, const=True)
                return value
        return None

    def lookup_fqn(self, fqn, node, module, required=True):
        if fqn in self.classes:
            return self.classes[fqn]
        if fqn in self.functions:
            return self.functions[fqn]
        parts = fqn.split("\\")
        if parts[0] == "Barq" and len(parts) >= 2:
            from .stdlib2 import STDLIB_ALIASES
            short = STDLIB_ALIASES.get("\\".join(parts[1:]), parts[-1])
            if short in self.builtins.vars:
                return self.builtins.vars[short][0]
        path = self.find_module_file(parts)
        if path is not None and os.path.abspath(path) not in self.modules:
            self.load_module(path)
            if fqn in self.classes:
                return self.classes[fqn]
            if fqn in self.functions:
                return self.functions[fqn]
        if not required:
            return None
        prev = self.module
        self.module = module
        try:
            if path is None:
                self.throw("NameException", f"cannot find '{fqn}' (looked for {os.path.join(*parts)}.bq)", node)
            self.throw("NameException", f"'{fqn}' is not defined (or not #[Public]) in {os.path.relpath(path, self.root)}", node)
        finally:
            self.module = prev

    def find_module_file(self, parts):
        for c in module_candidates(parts):
            p = os.path.join(self.root, c)
            if os.path.isfile(p):
                return p
        from .pkg import find_file
        return find_file(self.root, parts)     # installed packages (vendor/)

    def resolve_class(self, name, module, node):
        value = self.lookup_global(name, module, node)
        if not isinstance(value, BarqClass):
            self.throw("TypeException", f"'{name}' is a {type_name(value)}, not a class", node)
        return value

    def lookup_global(self, name, module, node):
        if "\\" in name:
            first, _, rest = name.partition("\\")
            imports = getattr(module, "imports", {})
            if first in imports:
                name = imports[first][0] + "\\" + rest
            return self.lookup_fqn(name.lstrip("\\"), node, module)
        entry = module.env.find(name)
        if entry is not None and entry[0] is not IMPORT_PENDING:
            return entry[0]
        value = self.resolve_name(name, module, node)
        if value is None:
            self.throw("NameException", f"'{name}' is not defined", node)
        return value

    # =====================================================================
    # Types
    # =====================================================================

    def type_class(self, type_ref, module):
        key = (id(module), type_ref.name)
        cls = self._type_cache.get(key)
        if cls is None:
            cls = self.resolve_class(type_ref.name, module, type_ref)
            self._type_cache[key] = cls
        return cls

    def check_type_known(self, t, module, node):
        if t is None:
            return
        if t.name not in BUILTIN_TYPES:
            self.type_class(t, module)
        elif t.name in COLLECTION_TYPES and t.args and len(t.args) != COLLECTION_TYPES[t.name]:
            self.compile_error(f"{t.name} takes {COLLECTION_TYPES[t.name]} type argument(s), e.g. "
                               + ("dict<string, int>" if t.name == "dict" else f"{t.name}<int>"), t)
        for a in t.args:
            self.check_type_known(a, module, node)

    def matches(self, value, t, module=None):
        if t is None:
            return True
        n = t.name
        if n == "any":
            return True
        if value is None:
            return n not in NON_NULLABLE or bool(t.nullable)
        tv = type(value)
        if n == "int":
            return tv is int
        if n == "string":
            return tv is str
        if n == "float":
            return tv is float
        if n == "bool":
            return tv is bool
        if n == "list":
            if tv is not list:
                return False
            return not t.args or all(self.matches(x, t.args[0], module) for x in value)
        if n == "dict":
            if tv is not dict:
                return False
            if not t.args:
                return True
            kt, vt = t.args
            return all(self.matches(k, kt, module) and self.matches(v, vt, module) for k, v in value.items())
        if n == "set":
            if tv is not BarqSet:
                return False
            return not t.args or all(self.matches(x, t.args[0], module) for x in value.values())
        if n in ("void", "null"):
            return False
        if n == "range":
            return tv is range
        if n == "karo":
            return isinstance(value, (BarqFunction, NativeFunction, BoundMethod))
        if tv is not BarqObject:
            return False
        return value.cls.is_subclass_of(self.type_class(t, module or self.module))

    def describe_value_type(self, value):
        if isinstance(value, list) and value:
            inner = {self.describe_value_type(x) for x in value}
            return f"list<{inner.pop()}>" if len(inner) == 1 else "list<mixed>"
        return type_name(value)

    def check_assign(self, value, t, what, node, module=None):
        if not self.matches(value, t, module):
            if t is not None and value is None and t.name in NON_NULLABLE:
                self.throw("TypeException", f"{what} is {type_str(t)} and cannot be null "
                                            f"— make it ?{type_str(t)} to allow null", node)
            hint = ""
            got = self.describe_value_type(value)
            if t is not None and t.name in ("int", "float", "string", "bool") and got in ("int", "float", "string", "bool"):
                hint = f" — convert it with ~{t.name}(...)"
            self.throw("TypeException", f"{what} expects {type_str(t)}, got {got}{hint}", node)

    def zero_value(self, t):
        if t is None or t.nullable:
            return None
        return {"int": 0, "float": 0.0, "string": "", "bool": False}.get(t.name) if t.name not in ("list", "dict", "set") \
            else ([] if t.name == "list" else {} if t.name == "dict" else BarqSet())

    # =====================================================================
    # Statements
    # =====================================================================

    def exec(self, stmt, env):
        self.line = stmt.line
        self.exec_table[type(stmt)](stmt, env)

    def exec_block(self, stmts, env):
        table = self.exec_table
        for stmt in stmts:
            self.line = stmt.line
            table[type(stmt)](stmt, env)

    def exec_misplaced(self, stmt, env):
        kind = {N.ClassDecl: "class", N.Namespace: "namespace", N.Use: "use"}[type(stmt)]
        self.compile_error(f"'{kind}' is only allowed at the top level of a file", stmt)

    def exec_nested_func(self, stmt, env):
        if stmt.name in env.vars:
            self.compile_error(f"'{stmt.name}' is already declared in this scope", stmt)
        fn = BarqFunction(stmt.name, stmt.params, stmt.ret, stmt.body, env, self.module, line=stmt.line)
        env.declare(stmt.name, fn, None, const=True)

    def exec_vardecl(self, stmt, env):
        if stmt.name in env.vars:
            self.throw("NameException", f"variable '{stmt.name}' is already declared in this scope", stmt)
        t = stmt.type
        self.check_type_known(t, self.module, stmt)
        if t.name == "void":
            self.throw("TypeException", "variables cannot have type void", stmt)
        value = self.eval(stmt.value, env) if stmt.value is not None else self.zero_value(t)
        self.check_assign(value, t, f"variable '{stmt.name}'", stmt.value or stmt)
        env.declare(stmt.name, value, t)

    def exec_const(self, stmt, env):
        if stmt.name in env.vars:
            self.throw("NameException", f"'{stmt.name}' is already declared in this scope", stmt)
        env.declare(stmt.name, self.eval(stmt.value, env), None, const=True)

    def exec_assign(self, stmt, env):
        target = stmt.target
        op = stmt.op
        if isinstance(target, N.Name):
            entry = env.find(target.id)
            if entry is None:
                if op == "=":
                    self.throw("NameException",
                               f"'{target.id}' is not declared — give it a type, e.g. "
                               f"'{self.guess_type(stmt.value, env)} {target.id} = ...'", target)
                self.throw("NameException", f"'{target.id}' is not declared", target)
            if entry[2]:
                self.throw("TypeException", f"cannot reassign constant '{target.id}'", target)
            value = self.eval(stmt.value, env)
            if op != "=":
                value = self.binop(op[:-1], entry[0], value, stmt)
            self.check_assign(value, entry[1], f"variable '{target.id}'", stmt.value)
            entry[0] = value
        elif isinstance(target, N.Attr):
            obj = self.eval(target.obj, env)
            value = self.eval(stmt.value, env)
            if op != "=":
                value = self.binop(op[:-1], self.get_attr(obj, target.name, target, env), value, stmt)
            self.set_attr(obj, target.name, value, target, env)
        elif isinstance(target, N.Index):
            container = self.eval(target.obj, env)
            key = self.eval(target.index, env)
            value = self.eval(stmt.value, env)
            if op != "=":
                value = self.binop(op[:-1], self.index(container, key, target), value, stmt)
            self.set_index(container, key, value, target)
        elif isinstance(target, N.StaticRef):
            cls, _ = self.static_target(target, env)
            value = self.eval(stmt.value, env)
            if op != "=":
                value = self.binop(op[:-1], self.get_static(cls, target.name, target, env), value, stmt)
            self.set_static(cls, target.name, value, target, env)

    def guess_type(self, expr, env):
        if isinstance(expr, N.Literal):
            return type_name(expr.value) if expr.value is not None else "any"
        if isinstance(expr, N.Interp):
            return "string"
        if isinstance(expr, N.ListLit):
            return "list<...>"
        if isinstance(expr, N.DictLit):
            return "dict<..., ...>"
        if isinstance(expr, N.New):
            return expr.cls.split("\\")[-1]
        return "any"

    def exec_expr(self, stmt, env):
        self.eval(stmt.expr, env)

    def cond(self, node, env, what="condition"):
        value = self.eval(node, env)
        if value is True or value is False:
            return value
        self.throw("TypeException",
                   f"{what} must be bool, got {type_name(value)} — compare explicitly, e.g. "
                   + ("'x != null'" if value is None else "'x != 0'" if isinstance(value, (int, float)) else "'s != \"\"'"
                      if isinstance(value, str) else "'items.len() > 0'" if isinstance(value, (list, dict, BarqSet)) else "'x != null'"),
                   node)

    def exec_if(self, stmt, env):
        for cond, body in stmt.branches:
            if self.cond(cond, env):
                self.exec_block(body, Env(env))
                return
        if stmt.other is not None:
            self.exec_block(stmt.other, Env(env))

    def iterate(self, value, node, pairs):
        if isinstance(value, (list, range)):
            return list(enumerate(value)) if pairs else list(value)
        if isinstance(value, dict):
            return list(value.items()) if pairs else list(value.keys())
        if isinstance(value, BarqSet):
            items = value.values()
            return list(enumerate(items)) if pairs else items
        if isinstance(value, str):
            return list(enumerate(value)) if pairs else list(value)
        self.throw("TypeException", f"cannot loop over {type_name(value)}", node)

    def exec_for(self, stmt, env):
        iterable = self.eval(stmt.iterable, env)
        items = self.iterate(iterable, stmt.iterable, stmt.key is not None)
        body = stmt.body
        for item in items:
            loop_env = Env(env)
            if stmt.key is not None:
                loop_env.declare(stmt.key, item[0])
                loop_env.declare(stmt.var, item[1])
            else:
                loop_env.declare(stmt.var, item)
            try:
                self.exec_block(body, loop_env)
            except BreakSignal:
                break
            except ContinueSignal:
                continue

    def exec_while(self, stmt, env):
        while self.cond(stmt.cond, env, "while condition"):
            try:
                self.exec_block(stmt.body, Env(env))
            except BreakSignal:
                break
            except ContinueSignal:
                continue

    def exec_dowhile(self, stmt, env):
        while True:
            try:
                self.exec_block(stmt.body, Env(env))
            except BreakSignal:
                break
            except ContinueSignal:
                pass
            if not self.cond(stmt.cond, env, "while condition"):
                break

    def exec_break(self, stmt, env):
        raise BreakSignal()

    def exec_continue(self, stmt, env):
        raise ContinueSignal()

    def exec_return(self, stmt, env):
        raise ReturnSignal(self.eval(stmt.value, env) if stmt.value is not None else None, stmt)

    def exec_raise(self, stmt, env):
        if stmt.value is None:
            if not self.handling:
                self.throw("RuntimeException", "'raise' with no value can only be used inside 'except'", stmt)
            raise BarqThrow(self.handling[-1])
        value = self.eval(stmt.value, env)
        if not (isinstance(value, BarqObject) and value.cls.is_subclass_of(self.classes["Exception"])):
            self.throw("TypeException", f"only Exception objects can be raised, got {type_name(value)}", stmt.value)
        self.stamp(value, stmt)
        raise BarqThrow(value)

    def exec_switch(self, stmt, env):
        subject = self.eval(stmt.subject, env)
        body = stmt.default
        for labels, case_body in stmt.cases:
            if any(values_equal(subject, self.eval(label, env)) for label in labels):
                body = case_body
                break
        if body is not None:
            try:
                self.exec_block(body, Env(env))
            except BreakSignal:
                pass

    def exec_try(self, stmt, env):
        try:
            self.exec_block(stmt.body, Env(env))
        except BarqThrow as thrown:
            obj = thrown.obj
            for types, name, body, _pos in stmt.handlers:
                if types and not any(obj.cls.is_subclass_of(self.resolve_class(t, self.module, stmt)) for t in types):
                    continue
                handler_env = Env(env)
                if name:
                    handler_env.declare(name, obj)
                self.handling.append(obj)
                try:
                    self.exec_block(body, handler_env)
                finally:
                    self.handling.pop()
                break
            else:
                raise
        finally:
            if stmt.final is not None:
                self.exec_block(stmt.final, Env(env))

    # =====================================================================
    # Expressions
    # =====================================================================

    def eval(self, node, env):
        return self.eval_table[type(node)](node, env)

    def eval_literal(self, node, env):
        return node.value

    def eval_interp(self, node, env):
        out = []
        for part in node.parts:
            if isinstance(part, str):
                out.append(part)
            else:
                out.append(to_display(self.eval(part, env), self))
        return "".join(out)

    SPECIAL_NAMES = {"__line__", "__file__", "__class__", "__method__", "__namespace__"}

    def eval_name(self, node, env):
        entry = env.find(node.id)
        if entry is not None and entry[0] is not IMPORT_PENDING:
            return entry[0]
        name = node.id
        if name in self.SPECIAL_NAMES:
            if name == "__line__":
                return node.line
            if name == "__file__":
                return self.module.path
            if name == "__namespace__":
                return self.module.namespace
            _, cls, func = env.context()
            if name == "__class__":
                return cls.name if cls else ""
            return func or ""
        value = self.resolve_name(name, self.module, node)
        if value is not None:
            return value
        self.throw("NameException", f"'{name}' is not defined", node)

    def eval_this(self, node, env):
        this, _, _ = env.context()
        if this is None:
            self.throw("NameException", "'this' can only be used inside a (non-static) method", node)
        return this

    def eval_list(self, node, env):
        return [self.eval(x, env) for x in node.items]

    def key_of(self, value, node, what="dict key"):
        try:
            hash_key(value)
        except TypeError:
            self.throw("TypeException", f"{type_name(value)} cannot be used as a {what}", node)
        return value

    def eval_dict(self, node, env):
        out = {}
        for k, v in node.pairs:
            out[self.key_of(self.eval(k, env), k)] = self.eval(v, env)
        return out

    def eval_set(self, node, env):
        s = BarqSet()
        for x in node.items:
            s.add(self.key_of(self.eval(x, env), x, "set item"))
        return s

    def eval_unary(self, node, env):
        value = self.eval(node.operand, env)
        if node.op == "not":
            if value is True or value is False:
                return not value
            self.throw("TypeException", f"'not' needs a bool, got {type_name(value)}", node)
        if type(value) in (int, float):
            return -value if node.op == "-" else value
        self.throw("TypeException", f"unary '{node.op}' needs a number, got {type_name(value)}", node)

    def eval_binary(self, node, env):
        return self.binop(node.op, self.eval(node.left, env), self.eval(node.right, env), node)

    def binop(self, op, a, b, node):
        ta, tb = type(a), type(b)
        if ta is tb and ta in (int, float):
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                if b == 0:
                    self.throw("DivisionByZeroException", "division by zero", node)
                if ta is int:
                    q = abs(a) // abs(b)
                    return q if (a >= 0) == (b >= 0) else -q
                return a / b
            if op == "%":
                if b == 0:
                    self.throw("DivisionByZeroException", "modulo by zero", node)
                if ta is int:
                    r = abs(a) % abs(b)
                    return r if a >= 0 else -r
                import math
                return math.fmod(a, b)
            if op == "**":
                if ta is int and b < 0:
                    self.throw("TypeException", "int ** negative int is not an int — use floats: ~float(x) ** ~float(y)", node)
                try:
                    return a ** b
                except OverflowError:
                    self.throw("ValueException", "number too large", node)
        if op == "+":
            if ta is str and tb is str:
                return a + b
            if ta is list and tb is list:
                return a + b
        names = (type_name(a), type_name(b))
        hint = ""
        if set(names) == {"int", "float"}:
            hint = " — convert one side with ~float(...) or ~int(...)"
        elif op == "+" and "string" in names:
            hint = " — use ~string(...) or interpolation: \"{x}\""
        self.throw("TypeException", f"cannot apply '{op}' to {names[0]} and {names[1]}{hint}", node)

    def eval_logical(self, node, env):
        if node.op == "??":
            left = self.eval(node.left, env)
            return left if left is not None else self.eval(node.right, env)
        left = self.cond(node.left, env, f"left side of '{node.op}'")
        if node.op == "and" and not left:
            return False
        if node.op == "or" and left:
            return True
        return self.cond(node.right, env, f"right side of '{node.op}'")

    def eval_compare(self, node, env):
        a = self.eval(node.left, env)
        b = self.eval(node.right, env)
        op = node.op
        if op == "==":
            return values_equal(a, b)
        if op == "!=":
            return not values_equal(a, b)
        if op in ("in", "not in"):
            result = self.contains(b, a, node)
            return result if op == "in" else not result
        ta, tb = type(a), type(b)
        if (ta in (int, float) and tb in (int, float)) or (ta is str and tb is str):
            if op == "<":
                return a < b
            if op == ">":
                return a > b
            if op == "<=":
                return a <= b
            return a >= b
        self.throw("TypeException", f"cannot compare {type_name(a)} {op} {type_name(b)}", node)

    def contains(self, container, item, node):
        if isinstance(container, str):
            if not isinstance(item, str):
                self.throw("TypeException", f"'in' on a string needs a string, got {type_name(item)}", node)
            return item in container
        if isinstance(container, (list, range)):
            return any(values_equal(item, x) for x in container)
        if isinstance(container, dict):
            try:
                return item in container
            except TypeError:
                return False
        if isinstance(container, BarqSet):
            try:
                return container.has(item)
            except TypeError:
                return False
        self.throw("TypeException", f"'in' needs a list, dict, set or string, got {type_name(container)}", node)

    def eval_is(self, node, env):
        value = self.eval(node.expr, env)
        if node.type.name == "null":
            return value is None
        if value is None:
            return bool(node.type.nullable)
        return self.matches(value, node.type)

    def eval_ternary(self, node, env):
        if self.cond(node.cond, env, "ternary condition"):
            return self.eval(node.then, env)
        return self.eval(node.other, env)

    def eval_cast(self, node, env):
        from .builtins import cast
        return cast(self, node.type, self.eval(node.expr, env), node)

    def eval_call(self, node, env):
        f = node.func
        if type(f) is N.Attr:
            obj = self.eval(f.obj, env)
            # obj.method(...) asks for the method itself, not a property-style value
            func = None if obj is None and f.safe else self.get_attr(obj, f.name, f, env, call=True)
        else:
            func = self.eval(f, env)
        args, kwargs = self.eval_args(node.args, env)
        return self.call_function(func, args, node, kwargs)

    def eval_args(self, nodes, env):
        """Argument values: (positional list, {name: value} or None)."""
        args = []
        kwargs = None
        for a in nodes:
            t = type(a)
            if t is N.NamedArg:
                if kwargs is None:
                    kwargs = {}
                kwargs[a.name] = self.eval(a.value, env)
            elif t is N.Spread:
                args.extend(self.spread(self.eval(a.value, env), a))
            else:
                args.append(self.eval(a, env))
        return args, kwargs

    def spread(self, value, node):
        if type(value) is not list:
            self.throw("TypeException", f"only a list can be spread into arguments with *, got {type_name(value)}", node)
        return value

    def eval_attr(self, node, env):
        obj = self.eval(node.obj, env)
        if obj is None and node.safe:
            return None
        return self.get_attr(obj, node.name, node, env)

    def eval_index(self, node, env):
        return self.index(self.eval(node.obj, env), self.eval(node.index, env), node)

    def eval_static(self, node, env):
        cls, via_parent = self.static_target(node, env)
        if isinstance(cls, NativeModule):
            return self.get_attr(cls, node.name, node, env)
        return self.get_static(cls, node.name, node, env, via_parent)

    def eval_new(self, node, env):
        cls = self.resolve_class(node.cls, self.module, node)
        args, kwargs = self.eval_args(node.args, env)
        return self.instantiate(cls, args, node, env, kwargs)

    def eval_lambda(self, node, env):
        for p in node.params:
            self.check_type_known(p.type, self.module, p)
        return BarqFunction("<lambda>", node.params, node.ret, node.body, env, self.module,
                            is_expr=node.is_expr, line=node.line)

    # =====================================================================
    # Calls
    # =====================================================================

    def call_function(self, func, args, node, kwargs=None):
        if isinstance(func, BoundMethod):
            inner = func.func
            if isinstance(inner, NativeFunction):
                if kwargs:
                    args = self.native_kwargs(inner, args, kwargs, node)
                self.check_native_args(inner, args, node)
                return inner.fn(self, args, node, func.this)
            return self.invoke(inner, args, node, func.this, kwargs)
        if isinstance(func, BarqFunction):
            if func.owner is not None and not func.is_static:
                self.throw("TypeException", f"method '{func.name}' needs an object; call it as obj.{func.name}()", node)
            return self.invoke(func, args, node, None, kwargs)
        if isinstance(func, NativeFunction):
            if kwargs:
                args = self.native_kwargs(func, args, kwargs, node)
            self.check_native_args(func, args, node)
            return func.fn(self, args, node)
        if isinstance(func, BarqClass):
            self.throw("TypeException", f"use 'new {func.name}(...)' to create an object", node)
        self.throw("TypeException", f"{type_name(func)} is not callable", node)

    def native_kwargs(self, func, args, kwargs, node):
        """Named arguments to a built-in that lists its parameter names
        (NativeFunction.arg_names). Skipped optional ones are passed as null."""
        names = func.arg_names
        if not names:
            self.no_named_args(func, node)
        label = func.name if func.owner is None or "." in func.name else f"{func.owner.name}.{func.name}"
        star = next((k for k, nm in enumerate(names) if nm.startswith("*")), None)
        extra = None
        if star is not None:
            # ("ability", "*arguments", "team"): names after the *rest one are
            # keyword-only and reach the built-in as a trailing KwArgs
            kw_only = names[star + 1:]
            extra = {k: kwargs[k] for k in kw_only if k in kwargs}
            kwargs = {k: v for k, v in kwargs.items() if k not in extra}
            names = [nm.lstrip("*") for nm in names]
            for key in kwargs:
                if key in names[star:]:
                    self.throw("TypeException", f"{label}() takes '{key}' as a positional argument only", node)
        out = list(args)
        filled = set(range(len(args)))
        for key, value in kwargs.items():
            if key not in names:
                self.throw("TypeException", f"{label}() has no parameter '{key}' (its parameters: "
                           f"{', '.join(names)})", node)
            k = names.index(key)
            if k in filled:
                self.throw("TypeException", f"{label}() got a value for '{key}' twice", node)
            while len(out) <= k:
                out.append(None)
            out[k] = value
            filled.add(k)
        for k in range(min(func.min_args, len(names))):
            if k not in filled:
                self.throw("TypeException", f"{label}() is missing argument '{names[k]}'", node)
        if extra:
            from .runtime import KwArgs
            out.append(KwArgs(extra))
        return out

    def no_named_args(self, func, node):
        name = func.name if func.owner is None or "::" in func.name else f"{func.owner.name}.{func.name}"
        self.throw("TypeException", f"{name}() is built in and takes positional arguments only — "
                   "named arguments work with functions written in Barq", node)

    # -- binding arguments to parameters -------------------------------------

    def bind_args(self, fn, args, kwargs, node):
        """One value per parameter from positional and named arguments: fills
        defaults and collects the rest into a *variadic list. Every value is
        type-checked here, so errors point at the argument that was wrong."""
        params = fn.params
        name = self.display_name(fn)
        n = len(params)
        variadic = n > 0 and params[-1].variadic
        fixed = n - 1 if variadic else n
        npos = len(args)
        given = npos + (len(kwargs) if kwargs else 0)
        if npos > fixed and not variadic:
            self.throw_at_arg("TypeException", f"{name}() expects {arity_text(params)}, got {given}", node, None)
        take = min(npos, fixed)
        out = list(args[:take]) + [MISSING] * (fixed - take)
        src = list(range(take)) + [None] * (fixed - take)
        if variadic:
            out.append(list(args[fixed:]))
            src.append(None)
        if kwargs:
            index = {p.name: i for i, p in enumerate(params)}
            for k, (key, value) in enumerate(kwargs.items()):
                i = index.get(key)
                if i is None:
                    names = ", ".join(p.name for p in params) or "none"
                    self.throw_at_arg("TypeException", f"{name}() has no parameter named '{key}' (parameters: {names})",
                                      node, npos + k)
                if params[i].variadic:
                    self.throw_at_arg("TypeException", f"{name}(): the variadic parameter '{key}' cannot be passed by name",
                                      node, npos + k)
                if out[i] is not MISSING:
                    self.throw_at_arg("TypeException", f"{name}() got two values for '{key}'", node, npos + k)
                out[i] = value
                src[i] = npos + k
        for i in range(fixed):
            p = params[i]
            if out[i] is MISSING:
                if p.default is None:
                    message = (f"{name}() is missing argument '{p.name}'" if kwargs
                               else f"{name}() expects {arity_text(params)}, got {given}")
                    self.throw_at_arg("TypeException", message, node, None)
                out[i] = value = self.default_value(fn, p)
                if not self.matches(value, p.type, fn.module):
                    self.arg_type_error(value, p.type, f"default of parameter '{p.name}' of {name}()", node, None, fn.module)
            elif not self.matches(out[i], p.type, fn.module):
                self.arg_type_error(out[i], p.type, f"argument '{p.name}' of {name}()", node, src[i], fn.module)
        if variadic:
            et = params[-1].type.args[0] if params[-1].type.args else None
            for j, value in enumerate(out[-1]):
                if not self.matches(value, et, fn.module):
                    self.arg_type_error(value, et, f"argument {fixed + j + 1} of {name}()", node, fixed + j, fn.module)
        return out

    def default_value(self, fn, p):
        d = p.default
        if type(d) is N.Literal:
            return d.value
        prev = self.module
        self.module = fn.module
        try:
            return self.eval(d, fn.module.env)
        finally:
            self.module = prev

    def arg_node(self, node, index):
        args = getattr(node, "args", None)
        if index is not None and args and index < len(args):
            return args[index]
        return node

    def throw_at_arg(self, cls_name, message, node, index):
        self.throw(cls_name, message, self.arg_node(node, index))

    def arg_type_error(self, value, t, what, node, index, module):
        self.check_assign(value, t, what, self.arg_node(node, index), module)

    def check_native_args(self, func, args, node):
        n = len(args)
        if n < func.min_args or (func.max_args is not None and func.max_args >= 0 and n > func.max_args):
            if func.min_args == func.max_args:
                expected = f"{func.min_args} argument{'s' if func.min_args != 1 else ''}"
            elif func.max_args is None or func.max_args < 0:
                expected = f"at least {func.min_args} arguments"
            else:
                expected = f"{func.min_args} to {func.max_args} arguments"
            self.throw("TypeException", f"{func.name}() expects {expected}, got {n}", node)

    def display_name(self, fn):
        if fn.owner is not None:
            sep = "::" if fn.is_static else "."
            return f"{fn.owner.name}{sep}{fn.name}"
        return fn.name

    def invoke(self, fn, args, node, this, kwargs=None):
        name = self.display_name(fn)
        if fn.is_abstract:
            self.throw("TypeException", f"cannot call abstract method {name}()", node)
        params = fn.params
        bound = kwargs is not None or not fn.simple
        if bound:
            args = self.bind_args(fn, args, kwargs, node)
        elif len(args) != len(params):
            self.throw("TypeException",
                       f"{name}() expects {len(params)} argument{'s' if len(params) != 1 else ''}, got {len(args)}", node)
        if len(self.stack) >= MAX_CALL_DEPTH:
            self.throw("RuntimeException", f"maximum call depth ({MAX_CALL_DEPTH}) exceeded — infinite recursion?", node)
        is_lambda = fn.name == "<lambda>"
        env = Env(fn.closure) if is_lambda else Env(fn.closure, this=this, cls=fn.owner, func=fn.name)
        arg_nodes = getattr(node, "args", None) or []
        for i, (p, a) in enumerate(zip(params, args)):
            if not bound and not self.matches(a, p.type, fn.module):
                where = arg_nodes[i] if i < len(arg_nodes) else node
                self.check_assign(a, p.type, f"argument '{p.name}' of {name}()", where, fn.module)
            env.declare(p.name, a, p.type)
        self.stack.append((name, self.module, node.line if node is not None else self.line))
        prev_module, prev_line = self.module, self.line
        self.module = fn.module
        finished = False
        where = fn.body if fn.is_expr else None
        try:
            if fn.is_expr:
                result = self.eval(fn.body, env)
                finished = True
            else:
                try:
                    self.exec_block(fn.body, env)
                    result = None
                except ReturnSignal as r:
                    result = r.value
                    where = r.node.value if r.node is not None and r.node.value is not None else r.node
                    finished = True
            ret = fn.ret
            if ret is not None and ret.name != "any":
                if ret.name == "void":
                    if result is not None:
                        self.throw("TypeException", f"{name}() is #[void] but returned {type_name(result)}", where)
                elif not finished:
                    self.throw("TypeException", f"{name}() must return {type_str(ret)} but reached the end without 'return'", ret)
                elif not self.matches(result, ret):
                    self.check_assign(result, ret, f"return value of {name}()", where)
        except (BreakSignal, ContinueSignal):
            self.throw("RuntimeException", "'break' or 'continue' used outside of a loop", node)
        finally:
            self.stack.pop()
            self.module, self.line = prev_module, prev_line
        return result

    # =====================================================================
    # Objects, members and indexing
    # =====================================================================

    def can_access(self, visibility, owner, env):
        if visibility == "public":
            return True
        _, ctx, _ = env.context()
        if ctx is None:
            return False
        if visibility == "private":
            return ctx is owner
        return ctx.is_subclass_of(owner) or owner.is_subclass_of(ctx)

    def instantiate(self, cls, args, node, env=None, kwargs=None):
        if cls.is_abstract:
            what = cls.kind if cls.kind in ("interface", "trait") else "abstract class"
            self.throw("TypeException", f"cannot create an instance of {what} {cls.name}", node)
        obj = self.instantiate_bare(cls)
        ctor = cls.find_method("__construct")
        if ctor is not None:
            if ctor.visibility != "public" and env is not None and not self.can_access(ctor.visibility, ctor.owner, env):
                self.throw("TypeException", f"the constructor of {cls.name} is {ctor.visibility}", node)
            self.call_function(BoundMethod(obj, ctor), args, node, kwargs)
        elif args or kwargs:
            self.throw("TypeException", f"{cls.name} has no __construct() but {len(args)} argument(s) were given", node)
        return obj

    def finish_enum(self, cls):
        """Give an enum its read-only `name`/`value`, one object per case, and
        the built-in static methods cases(), from() and tryFrom()."""
        from .builtins import T, need
        cls.props["name"] = PropInfo("name", T("string"), "public", None, cls)
        if cls.backing:
            cls.props["value"] = PropInfo("value", T(cls.backing), "public", None, cls)
        for case in cls.decl.cases:
            if case.name in cls.statics:
                self.compile_error(f"'{case.name}' is already a constant of enum {cls.name}", case)
            obj = BarqObject(cls)
            obj.fields["name"] = case.name
            if cls.backing:
                obj.fields["value"] = case.value.value
            cls.cases[case.name] = obj
            cls.statics[case.name] = PropInfo(case.name, T(cls.name), "public", None, cls, is_static=True, const=True)
            cls.static_values[case.name] = obj

        def lookup(i, a, n, fname):
            value = need(i, a[0], (cls.backing,), f"{cls.name}::{fname}", 1, n)
            for obj in cls.cases.values():
                if obj.fields["value"] == value:
                    return obj
            return None

        def from_value(i, a, n):
            found = lookup(i, a, n, "from")
            if found is None:
                i.throw("ValueException", f"{to_display(a[0], i, True)} is not a valid value for enum {cls.name}", n)
            return found

        natives = {"cases": (lambda i, a, n: list(cls.cases.values()), 0)}
        if cls.backing:
            natives["from"] = (from_value, 1)
            natives["tryFrom"] = (lambda i, a, n: lookup(i, a, n, "tryFrom"), 1)
        for mname, (fn, count) in natives.items():
            f = NativeFunction(f"{cls.name}::{mname}", fn, count, count)
            f.owner = cls
            f.is_static = True
            cls.methods[mname] = f

    def instantiate_bare(self, cls):
        """Create an object with property defaults, without running __construct."""
        if cls.is_enum:
            first = next(iter(cls.cases), "Case")
            self.throw("TypeException", f"cannot create an instance of enum {cls.name} — use a case such as {cls.name}::{first}")
        obj = BarqObject(cls)
        for c in cls.chain():
            for p in c.props.values():
                home = p.module or c.module
                if p.default is not None:
                    prev = self.module
                    self.module = home
                    try:
                        value = self.eval(p.default, home.env)
                    finally:
                        self.module = prev
                else:
                    value = self.zero_value(p.type)
                # Type names in the declaration belong to the class's own file.
                self.check_assign(value, p.type, f"default of property '{c.name}.{p.name}'", p.default or c.decl, home)
                obj.fields[p.name] = value
        return obj

    def get_attr(self, obj, name, node, env, call=False):
        if isinstance(obj, BarqObject):
            cls = obj.cls
            p = cls.find_prop(name)
            if p is not None:
                if p.visibility != "public" and not self.can_access(p.visibility, p.owner, env):
                    self.throw("TypeException", f"cannot read {p.visibility} property '{name}' of {cls.name}", node)
                return obj.fields[name]
            m = cls.find_method(name)
            if m is not None:
                if m.visibility != "public" and not self.can_access(m.visibility, m.owner, env):
                    self.throw("TypeException", f"cannot call {m.visibility} method '{name}()' of {cls.name}", node)
                if not call and type(m) is BarqFunction and m.ret is not None and m.ret.name in RELATION_TYPES:
                    from . import hooks
                    hit, value = hooks.property_access(self, obj, m, node)
                    if hit:
                        return value
                return m if m.is_static else BoundMethod(obj, m)
            getter = cls.find_method("__get")
            if getter is not None:
                return self.call_function(BoundMethod(obj, getter), [name], node)
            self.throw("NameException", f"{cls.name} has no property or method '{name}'", node)
        if isinstance(obj, BarqClass):
            return self.get_static(obj, name, node, env)
        if isinstance(obj, NativeModule):
            if name in obj.members:
                return obj.members[name]
            self.throw("NameException", f"{obj.name} has no member '{name}'", node)
        if obj is None:
            self.throw("NullException", f"cannot read '{name}' of null — check for null first or use '?.'", node)
        from .builtins import builtin_method
        method = builtin_method(self, obj, name, node)
        if method is not None:
            return method
        if isinstance(obj, dict):
            self.throw("NameException", f"dict has no method '{name}' — read values with d[\"{name}\"]", node)
        self.throw("NameException", f"{type_name(obj)} has no method '{name}'", node)

    def set_attr(self, obj, name, value, node, env):
        if isinstance(obj, BarqObject) and obj.cls.is_enum:
            self.throw("TypeException", f"enum cases are read-only: cannot set '{name}' on {to_display(obj)}", node)
        if isinstance(obj, BarqObject):
            cls = obj.cls
            p = cls.find_prop(name)
            if p is None:
                if cls.find_method(name) is not None:
                    self.throw("TypeException", f"cannot assign to method '{name}' of {cls.name}", node)
                setter = cls.find_method("__set")
                if setter is not None:
                    self.call_function(BoundMethod(obj, setter), [name, value], node)
                    return
                self.throw("NameException", f"{cls.name} has no property '{name}' — declare it in the class body, e.g. 'public any {name}'", node)
            if p.visibility != "public" and not self.can_access(p.visibility, p.owner, env):
                self.throw("TypeException", f"cannot write {p.visibility} property '{name}' of {cls.name}", node)
            self.check_assign(value, p.type, f"property '{cls.name}.{name}'", node)
            obj.fields[name] = value
            return
        if isinstance(obj, BarqClass):
            self.set_static(obj, name, value, node, env)
            return
        if obj is None:
            self.throw("NullException", f"cannot set '{name}' on null", node)
        self.throw("TypeException", f"cannot set '{name}' on {type_name(obj)}", node)

    def static_target(self, node, env):
        target = node.target
        if isinstance(target, N.Name) and target.id in ("parent", "self"):
            _, ctx, _ = env.context()
            if ctx is None:
                self.throw("NameException", f"'{target.id}::' can only be used inside a class", node)
            if target.id == "self":
                return ctx, False
            if ctx.parent is None:
                self.throw("NameException", f"{ctx.name} has no parent class", node)
            return ctx.parent, True
        value = self.eval(target, env)
        if isinstance(value, (BarqClass, NativeModule)):
            return value, False
        self.throw("TypeException", f"'::' needs a class on the left, got {type_name(value)}", node)

    def get_static(self, cls, name, node, env, via_parent=False):
        p = cls.find_static(name)
        if p is not None:
            if p.visibility != "public" and not self.can_access(p.visibility, p.owner, env):
                self.throw("TypeException", f"cannot read {p.visibility} static '{name}' of {cls.name}", node)
            value = p.owner.static_values[name]
            if type(value) is NativeFunction and value.late_static:
                return BoundMethod(cls, value)   # User.find(): the function learns it was called on User
            return value
        m = cls.find_method(name)
        if m is not None:
            if m.visibility != "public" and not self.can_access(m.visibility, m.owner, env):
                self.throw("TypeException", f"cannot call {m.visibility} method '{name}()' of {cls.name}", node)
            if m.is_static:
                return m
            this, ctx, _ = env.context()
            # parent::method() / self::method() call an instance method on `this`.
            if this is not None and ctx is not None and this.cls.is_subclass_of(cls):
                return BoundMethod(this, m)
            self.throw("TypeException", f"{cls.name}.{name}() is not static; call it on an object", node)
        if cls.find_prop(name) is not None:
            self.throw("TypeException", f"'{name}' is an instance property of {cls.name}; read it from an object", node)
        c = cls
        while c is not None:
            fallback = c.__dict__.get("static_fallback")
            if fallback is not None:
                found = fallback(self, cls, name, node)
                if found is not None:
                    return found
                break
            c = c.parent
        self.throw("NameException", f"{cls.name} has no static member '{name}'", node)

    def set_static(self, cls, name, value, node, env):
        p = cls.find_static(name)
        if p is None:
            self.throw("NameException", f"{cls.name} has no static property '{name}'", node)
        if p.const:
            self.throw("TypeException", f"cannot reassign constant {cls.name}::{name}", node)
        if p.visibility != "public" and not self.can_access(p.visibility, p.owner, env):
            self.throw("TypeException", f"cannot write {p.visibility} static '{name}' of {cls.name}", node)
        self.check_assign(value, p.type, f"static property '{cls.name}::{name}'", node)
        p.owner.static_values[name] = value

    def index(self, container, key, node):
        if isinstance(container, (list, str, range)):
            if type(key) is not int:
                self.throw("TypeException", f"{type_name(container)} index must be int, got {type_name(key)}", node)
            n = len(container)
            if not -n <= key < n:
                self.throw("IndexException", f"index {key} is out of range (length {n})", node)
            return container[key]
        if isinstance(container, dict):
            try:
                return container[key]
            except KeyError:
                self.throw("KeyException", f"key {to_display(key, self, True)} not found — use d.get(key, default) for optional keys", node)
            except TypeError:
                self.throw("TypeException", f"{type_name(key)} cannot be a dict key", node)
        if container is None:
            self.throw("NullException", "cannot index null", node)
        self.throw("TypeException", f"{type_name(container)} cannot be indexed with []", node)

    def set_index(self, container, key, value, node):
        if isinstance(container, list):
            if type(key) is not int:
                self.throw("TypeException", f"list index must be int, got {type_name(key)}", node)
            n = len(container)
            if not -n <= key < n:
                self.throw("IndexException", f"index {key} is out of range (length {n}) — use push() to add items", node)
            container[key] = value
            return
        if isinstance(container, dict):
            container[self.key_of(key, node)] = value
            return
        if isinstance(container, str):
            self.throw("TypeException", "strings cannot be changed in place", node)
        self.throw("TypeException", f"{type_name(container)} does not support [] assignment", node)
