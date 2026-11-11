"""Recursive-descent parser: tokens in, AST out."""

from . import nodes as N
from .errors import ParseError, SourceFile
from .lexer import Lexer

VISIBILITY_ATTRS = {"Public": "public", "Private": "private", "Protected": "protected"}
ASSIGN_OPS = ("=", "+=", "-=", "*=", "/=", "%=")
COMPARE_OPS = ("==", "!=", "<", ">", "<=", ">=")


def describe(tok):
    if tok.type == "EOF":
        return "end of file"
    if tok.type == "NEWLINE":
        return "end of line"
    if tok.type == "INDENT":
        return "an indented block"
    if tok.type == "DEDENT":
        return "end of block"
    if tok.type in ("STRING", "RAWSTRING"):
        return "a string"
    if tok.type in ("INT", "FLOAT"):
        return f"number {tok.value}"
    return f"'{tok.value}'"


class _Backtrack(Exception):
    pass


class Parser:
    def __init__(self, tokens, source):
        self.tokens = tokens
        self.source = source
        self.pos = 0
        self.ended_with_block = False   # an anonymous karo(...) block ended the current line
        self.member_kind = None         # "class" / "interface" / "trait" / "enum" while in a body

    # -- token helpers -----------------------------------------------------

    @property
    def tok(self):
        return self.tokens[self.pos]

    def peek(self, n=1):
        i = min(self.pos + n, len(self.tokens) - 1)
        return self.tokens[i]

    def advance(self):
        t = self.tokens[self.pos]
        if t.type != "EOF":
            self.pos += 1
        return t

    def error(self, msg, tok=None):
        tok = tok or self.tok
        raise ParseError(msg, self.source, tok.line, tok.col)

    def expect_op(self, value, context=""):
        if not self.tok.is_op(value):
            where = f" {context}" if context else ""
            self.error(f"expected '{value}'{where}, got {describe(self.tok)}")
        return self.advance()

    def expect_kw(self, value):
        if not self.tok.is_kw(value):
            self.error(f"expected '{value}', got {describe(self.tok)}")
        return self.advance()

    def expect_name(self, what="a name"):
        if self.tok.type != "NAME":
            if self.tok.type == "KEYWORD":
                self.error(f"'{self.tok.value}' is a reserved keyword and cannot be used as {what}")
            self.error(f"expected {what}, got {describe(self.tok)}")
        return self.advance()

    def match_op(self, *values):
        if self.tok.is_op(*values):
            return self.advance()
        return None

    def at_line_end(self):
        return self.tok.type in ("NEWLINE", "EOF", "DEDENT")

    def end_statement(self):
        if self.ended_with_block:
            self.ended_with_block = False
            return
        if self.tok.type == "NEWLINE":
            self.advance()
        elif self.tok.type in ("EOF", "DEDENT"):
            pass
        else:
            if self.tok.is_op(";"):
                self.error("semicolons are not used in Barq; end the statement with a new line")
            self.error(f"expected end of line, got {describe(self.tok)}")

    def skip_newlines(self):
        while self.tok.type == "NEWLINE":
            self.advance()

    # -- program & blocks --------------------------------------------------

    def parse_program(self):
        body = []
        self.skip_newlines()
        while self.tok.type != "EOF":
            body.append(self.statement())
            self.skip_newlines()
        return N.Program(body, line=1, col=1)

    def block(self, in_class=False):
        """NEWLINE INDENT statements DEDENT"""
        if self.tok.type != "NEWLINE":
            self.error(f"expected end of line before block, got {describe(self.tok)}")
        self.advance()
        if self.tok.type != "INDENT":
            self.error("expected an indented block")
        self.advance()
        body = []
        while self.tok.type not in ("DEDENT", "EOF"):
            body.append(self.class_member() if in_class else self.statement())
            self.skip_newlines()
        if self.tok.type == "DEDENT":
            self.advance()
        return body

    def body(self):
        """Either an indented block or a single statement on the same line."""
        if self.tok.type == "NEWLINE":
            return self.block()
        return [self.simple_statement()]

    # -- statements --------------------------------------------------------

    def statement(self):
        t = self.tok
        if t.type == "NAME" and t.value in ("barq", "br", "def", "fn", "function") \
                and self.peek().type == "NAME" and self.peek(2).is_op("("):
            self.error(f"functions are declared with 'karo', e.g. 'karo {self.peek().value}(...)'")
        if t.is_op("#["):
            return self.function_with_attrs(top_level=True)
        if t.type == "KEYWORD":
            kw = t.value
            if kw == "if":
                return self.if_stmt()
            if kw in ("for", "foreach"):
                return self.for_stmt()
            if kw == "while":
                return self.while_stmt()
            if kw == "do":
                return self.do_while()
            if kw == "switch":
                return self.switch_stmt()
            if kw == "try":
                return self.try_stmt()
            if kw == "karo" and not self.peek().type == "OP":
                return self.function_decl([], t)
            if kw in ("class", "abstract"):
                return self.class_decl()
            if kw == "enum":
                return self.enum_decl()
            if kw == "namespace":
                return self.namespace_stmt()
            if kw == "use":
                return self.use_stmt()
            if kw == "interface":
                return self.interface_decl()
            if kw == "trait":
                return self.trait_decl()
            if kw in ("public", "private", "protected"):
                self.error(f"'{kw}' at file level is not supported yet; use #[Public] on functions")
        return self.simple_statement()

    def simple_statement(self):
        t = self.tok
        if t.type == "KEYWORD":
            kw = t.value
            if kw == "return":
                self.advance()
                value = None if self.at_line_end() else self.expression()
                self.end_statement()
                return N.Return(value, line=t.line, col=t.col)
            if kw == "break":
                self.advance()
                self.end_statement()
                return N.Break(line=t.line, col=t.col)
            if kw == "continue":
                self.advance()
                self.end_statement()
                return N.Continue(line=t.line, col=t.col)
            if kw == "raise":
                self.advance()
                value = None if self.at_line_end() else self.expression()
                self.end_statement()
                return N.Raise(value, line=t.line, col=t.col)
            if kw == "const":
                self.advance()
                name = self.expect_name("a constant name")
                self.expect_op("=", "in constant declaration")
                value = self.expression()
                self.end_statement()
                return N.ConstDecl(name.value, value, line=t.line, col=t.col)
            if kw in ("if", "for", "foreach", "while", "switch", "try", "karo", "class", "enum", "interface", "trait"):
                self.error(f"'{kw}' cannot be written on the same line here; start it on its own line")

        decl = self.try_declaration()
        if decl is not None:
            return decl

        expr = self.expression()
        if self.tok.type == "OP" and self.tok.value in ASSIGN_OPS:
            op = self.advance()
            if not isinstance(expr, (N.Name, N.Attr, N.Index, N.StaticRef)):
                self.error("cannot assign to this expression", op)
            if isinstance(expr, N.Attr) and expr.safe:
                self.error("cannot assign through '?.'", op)
            value = self.expression()
            self.end_statement()
            return N.Assign(expr, op.value, value, line=t.line, col=t.col)
        self.end_statement()
        return N.ExprStmt(expr, line=t.line, col=t.col)

    def try_declaration(self):
        """`Type name = value` or `Type name`. Returns None if this is not one."""
        if self.tok.type != "NAME" and not self.tok.is_op("?"):
            return None
        start = self.pos
        t = self.tok
        try:
            type_ref = self.parse_type()
        except _Backtrack:
            self.pos = start
            return None
        if self.tok.type == "NAME" and (self.peek().is_op("=") or self.peek().type in ("NEWLINE", "EOF", "DEDENT")):
            name = self.advance()
            value = None
            if self.match_op("="):
                value = self.expression()
            self.end_statement()
            return N.VarDecl(type_ref, name.value, value, line=t.line, col=t.col)
        if self.tok.type == "KEYWORD" and self.peek().is_op("="):
            self.error(f"'{self.tok.value}' is a reserved keyword and cannot be used as a variable name")
        self.pos = start
        return None

    def parse_type(self):
        """Type grammar: '?'? Name ('\\' Name)* ('<' Type (',' Type)* '>')?
        Raises _Backtrack when the tokens are not a type."""
        t = self.tok
        if t.is_op("?") and (self.peek().type == "NAME" or self.peek().is_kw("null")):
            self.advance()
            inner = self.parse_type()
            if inner.name in ("any", "void", "null"):
                why = "already allows null" if inner.name == "any" else "cannot be nullable"
                self.error(f"'?{inner.name}' is not a type — {inner.name} {why}", t)
            inner.nullable = True
            inner.line, inner.col = t.line, t.col
            return inner
        if t.type == "NAME":
            parts = [self.advance().value]
        elif t.is_kw("null"):
            self.advance()
            return N.TypeRef("null", [], line=t.line, col=t.col)
        else:
            raise _Backtrack()
        while self.tok.is_op("\\") and self.peek().type == "NAME":
            self.advance()
            parts.append(self.advance().value)
        args = []
        if self.tok.is_op("<"):
            self.advance()
            args.append(self.parse_type())
            while self.match_op(","):
                args.append(self.parse_type())
            if not self.tok.is_op(">"):
                raise _Backtrack()
            self.advance()
        return N.TypeRef("\\".join(parts), args, line=t.line, col=t.col)

    def type_or_error(self, what="a type"):
        start = self.tok
        try:
            return self.parse_type()
        except _Backtrack:
            self.error(f"expected {what}, got {describe(start)}", start)

    def if_stmt(self):
        t = self.advance()
        branches = []
        cond = self.expression()
        branches.append((cond, self.body()))
        other = None
        while True:
            self.skip_newlines_before_else()
            if self.tok.is_kw("elseif") or (self.tok.is_kw("else") and self.peek().is_kw("if")):
                if self.tok.is_kw("else"):
                    self.advance()
                self.advance()
                cond = self.expression()
                branches.append((cond, self.body()))
                continue
            if self.tok.is_kw("else"):
                self.advance()
                other = self.body()
            break
        return N.If(branches, other, line=t.line, col=t.col)

    def skip_newlines_before_else(self):
        # Blank lines between an if-block and its else are allowed.
        i = self.pos
        while self.tokens[i].type == "NEWLINE":
            i += 1
        if self.tokens[i].is_kw("else", "elseif"):
            self.pos = i

    def for_stmt(self):
        t = self.advance()
        first = self.expect_name("a loop variable")
        key = None
        var = first.value
        if self.match_op(","):
            key = first.value
            var = self.expect_name("a loop variable").value
        self.expect_kw("in")
        iterable = self.expression()
        body = self.body()
        return N.For(key, var, iterable, body, line=t.line, col=t.col)

    def while_stmt(self):
        t = self.advance()
        cond = self.expression()
        return N.While(cond, self.body(), line=t.line, col=t.col)

    def do_while(self):
        t = self.advance()
        body = self.block()
        self.skip_newlines()
        self.expect_kw("while")
        cond = self.expression()
        self.end_statement()
        return N.DoWhile(body, cond, line=t.line, col=t.col)

    def switch_stmt(self):
        t = self.advance()
        subject = self.expression()
        if self.tok.type != "NEWLINE":
            self.error("expected end of line after switch subject")
        self.advance()
        if self.tok.type != "INDENT":
            self.error("expected indented 'case' lines after switch")
        self.advance()
        cases = []
        default = None
        pending = []   # labels stacked on top of each other (explicit fallthrough)
        while self.tok.type not in ("DEDENT", "EOF"):
            ct = self.tok
            if ct.is_kw("case"):
                self.advance()
                labels = [self.expression()]
                while self.match_op(","):
                    labels.append(self.expression())
                self.expect_op(":", "after case value")
                pending.extend(labels)
                if self.tok.type == "NEWLINE" and self.peek().type != "INDENT":
                    self.advance()   # empty case: falls into the next one
                    self.skip_newlines()
                    continue
                cases.append((pending, self.body()))
                pending = []
            elif ct.is_kw("default"):
                self.advance()
                self.expect_op(":", "after default")
                if pending:
                    self.error("case labels before 'default' have no body", ct)
                default = self.body()
            else:
                self.error(f"expected 'case' or 'default' inside switch, got {describe(ct)}")
            self.skip_newlines()
        if pending:
            self.error("the last case has no body")
        if self.tok.type == "DEDENT":
            self.advance()
        return N.Switch(subject, cases, default, line=t.line, col=t.col)

    def try_stmt(self):
        t = self.advance()
        body = self.block()
        handlers = []
        final = None
        self.skip_newlines_before("except", "finally")
        while self.tok.is_kw("except"):
            et = self.advance()
            types = []
            name = None
            if not self.at_line_end() and not self.tok.is_kw("as"):
                types.append(self.qualified_name())
                while self.match_op(","):
                    types.append(self.qualified_name())
            if self.tok.is_kw("as"):
                self.advance()
                name = self.expect_name("an exception variable").value
            handlers.append((types, name, self.block()))
            handlers[-1] = handlers[-1] + ((et.line, et.col),)
            self.skip_newlines_before("except", "finally")
        if self.tok.is_kw("finally"):
            self.advance()
            final = self.block()
        if not handlers and final is None:
            self.error("'try' needs at least one 'except' or a 'finally'")
        return N.Try(body, handlers, final, line=t.line, col=t.col)

    def skip_newlines_before(self, *kws):
        i = self.pos
        while self.tokens[i].type == "NEWLINE":
            i += 1
        if self.tokens[i].is_kw(*kws):
            self.pos = i

    def qualified_name(self):
        parts = [self.expect_name("a class name").value]
        while self.tok.is_op("\\"):
            self.advance()
            parts.append(self.expect_name("a name").value)
        return "\\".join(parts)

    # -- functions ---------------------------------------------------------

    def attributes(self):
        attrs = []
        while self.tok.is_op("#["):
            open_tok = self.advance()
            type_ref = self.type_or_error("an attribute")
            self.expect_op("]", "to close the attribute")
            attrs.append(type_ref)
            if self.tok.type != "NEWLINE":
                self.error("an attribute must be on its own line", open_tok)
            self.advance()
            self.skip_newlines()
        return attrs

    def function_with_attrs(self, top_level=False, member=False):
        start = self.tok
        attrs = self.attributes()
        if self.tok.is_kw("enum"):
            if not top_level:
                self.error("an enum must be declared at the top level of a file", self.tok)
            return self.enum_decl(attrs)
        if not self.tok.is_kw("karo"):
            what = "a 'karo' function or an enum" if top_level else "a 'karo' function"
            self.error(f"attributes must be followed by {what}, got {describe(self.tok)}", start)
        return self.function_decl(attrs, start, member)

    def function_decl(self, attrs, start, member=False):
        self.expect_kw("karo")
        name = self.expect_name("a function name")
        visibility = None
        ret = None
        is_static = False
        is_abstract = False
        for a in attrs:
            if a.name in VISIBILITY_ATTRS and not a.args:
                if visibility:
                    self.error("a function can only have one visibility attribute", a)
                visibility = VISIBILITY_ATTRS[a.name]
            elif a.name == "Static" and not a.args:
                is_static = True
            elif a.name == "Abstract" and not a.args:
                is_abstract = True
            else:
                if ret is not None:
                    self.error(f"a function can only have one return type attribute (found #[{ret.name}] and #[{a.name}])", a)
                ret = a
        params = self.params()
        promoted = next((p for p in params if p.promote), None)
        if promoted is not None and not (member and name.value == "__construct" and self.member_kind in ("class", "trait")):
            self.error(f"only constructor parameters can become properties — remove '{promoted.promote}' "
                       f"or move it to __construct", promoted)
        if self.match_op(":"):
            colon_ret = self.type_or_error("a return type")
            if ret is not None:
                self.error("return type given twice (attribute and ': type')", colon_ret)
            ret = colon_ret
        if member and self.member_kind == "interface":
            if self.tok.type == "NEWLINE" and self.peek().type == "INDENT":
                self.error(f"interface methods have no body — classes that implement the interface write "
                           f"'{name.value}()'", name)
            if visibility not in (None, "public"):
                self.error("interface methods are always public", name)
            is_abstract = True
        if is_abstract:
            if self.tok.type == "NEWLINE" and self.peek().type == "INDENT":
                self.error(f"abstract method '{name.value}' cannot have a body", name)
            self.end_statement()
            body = None
        elif self.tok.type == "NEWLINE" and self.peek().type != "INDENT":
            self.error(f"function '{name.value}' has no body", name)
        else:
            body = self.body()
        if ret is None:
            self.error(f"function '{name.value}' needs a return type attribute such as #[void] or #[int]", name)
        return N.FuncDecl(name.value, params, ret, visibility, is_static, is_abstract, body,
                          line=start.line, col=start.col)

    def params(self):
        """( [public|private|protected] [*] Type name [= constant], ... )"""
        self.expect_op("(", "before parameters")
        params = []
        with_default = None
        while not self.tok.is_op(")"):
            pt = self.tok
            if params and params[-1].variadic:
                self.error(f"the variadic parameter '*{params[-1].name}' must be the last one", pt)
            promote = None
            if self.tok.is_kw("public", "private", "protected"):
                promote = self.advance().value
            variadic = self.match_op("*") is not None
            type_ref = self.type_or_error("a parameter type")
            if self.tok.is_op(",", ")"):
                self.error(f"parameter '{type_ref.name}' needs a type, e.g. 'int {type_ref.name}' (use 'any' for untyped)", pt)
            pname = self.expect_name("a parameter name")
            default = None
            if self.tok.is_op("="):
                eq = self.advance()
                if variadic:
                    self.error(f"the variadic parameter '*{pname.value}' cannot have a default — it is an empty list "
                               "when no arguments are left", eq)
                default = self.expression()
                self.check_constant(default, pname.value)
                with_default = pname.value
            elif with_default is not None and not variadic:
                self.error(f"parameter '{pname.value}' needs a default value because '{with_default}' before it has one", pname)
            if variadic:
                type_ref = N.TypeRef("list", [type_ref], line=type_ref.line, col=type_ref.col)
            params.append(N.Param(type_ref, pname.value, default, variadic, promote, line=pt.line, col=pt.col))
            if not self.match_op(","):
                break
        self.expect_op(")", "after parameters")
        return params

    def check_constant(self, e, pname):
        """Default values are constants: literals, collections of them, -x, a + b,
        and Class::CONSTANT / Enum::Case. They are worked out on every call."""
        def ok(x):
            if isinstance(x, N.Literal):
                return True
            if isinstance(x, (N.ListLit, N.SetLit)):
                return all(ok(i) for i in x.items)
            if isinstance(x, N.DictLit):
                return all(ok(k) and ok(v) for k, v in x.pairs)
            if isinstance(x, N.Unary):
                return ok(x.operand)
            if isinstance(x, (N.Binary, N.Logical)):
                return ok(x.left) and ok(x.right)
            if isinstance(x, N.StaticRef):
                return isinstance(x.target, N.Name) and x.target.id not in ("self", "parent")
            return False
        if not ok(e):
            self.error(f"the default value of '{pname}' must be a constant — a literal, a list or dict of literals, "
                       "or Class::CONSTANT (use null and set it in the body otherwise)", e)

    def no_promote(self, params):
        for p in params:
            if p.promote:
                self.error(f"only constructor parameters can become properties — remove '{p.promote}'", p)

    # -- classes -----------------------------------------------------------

    def class_decl(self):
        t = self.tok
        is_abstract = False
        if t.is_kw("abstract"):
            self.advance()
            is_abstract = True
        self.expect_kw("class")
        name = self.expect_name("a class name")
        parent = None
        if self.tok.is_kw("extends"):
            self.advance()
            parent = self.qualified_name()
            if self.tok.is_op(","):
                self.error("a class extends one parent class — list interfaces after 'implements'")
        interfaces = self.implements_list()
        if self.tok.is_kw("extends"):
            self.error("write 'extends' before 'implements': class A extends B implements C")
        members = self.class_body("class")
        for m in members:
            if isinstance(m, N.EnumCase):
                self.error(f"'case' is only allowed inside an enum — did you mean 'enum {name.value}'?", m)
        members = self.promote_params(members)
        return N.ClassDecl(name.value, parent, is_abstract, members, False, None, [], interfaces, "class",
                           line=t.line, col=t.col)

    def implements_list(self):
        names = []
        if self.tok.is_kw("implements"):
            self.advance()
            names.append(self.qualified_name())
            while self.match_op(","):
                names.append(self.qualified_name())
        return names

    def class_body(self, kind):
        saved = self.member_kind
        self.member_kind = kind
        try:
            if self.tok.type == "NEWLINE" and self.peek().type != "INDENT":
                self.advance()
                return []
            return self.block(in_class=True)
        finally:
            self.member_kind = saved

    def promote_params(self, members):
        """`karo __construct(public int x)` declares property x and assigns it
        first thing in the constructor."""
        out = list(members)
        for m in members:
            if not (isinstance(m, N.FuncDecl) and m.name == "__construct"):
                continue
            promoted = [p for p in m.params if p.promote]
            if not promoted:
                continue
            if m.body is None:
                self.error("an abstract constructor cannot declare properties", promoted[0])
            props, assigns = [], []
            for p in promoted:
                props.append(N.Property(p.promote, False, p.type, p.name, None, line=p.line, col=p.col))
                target = N.Attr(N.This(line=p.line, col=p.col), p.name, False, line=p.line, col=p.col)
                assigns.append(N.Assign(target, "=", N.Name(p.name, line=p.line, col=p.col), line=p.line, col=p.col))
            m.body = assigns + m.body
            at = out.index(m)
            out[at:at] = props
        return out

    def interface_decl(self):
        """interface Name [extends A, B]  + method signatures and constants."""
        t = self.advance()
        name = self.expect_name("an interface name").value
        parents = []
        if self.tok.is_kw("extends"):
            self.advance()
            parents.append(self.qualified_name())
            while self.match_op(","):
                parents.append(self.qualified_name())
        if self.tok.is_kw("implements"):
            self.error(f"an interface extends other interfaces: interface {name} extends A, B")
        members = self.class_body("interface")
        for m in members:
            if isinstance(m, N.Property):
                self.error(f"interfaces cannot have properties — declare a method such as get{m.name[:1].upper() + m.name[1:]}() instead", m)
            if isinstance(m, N.EnumCase):
                self.error("'case' is only allowed inside an enum", m)
            if isinstance(m, N.FuncDecl) and m.name == "__construct":
                self.error("interfaces cannot declare a constructor", m)
        return N.ClassDecl(name, None, True, members, False, None, [], parents, "interface", line=t.line, col=t.col)

    def trait_decl(self):
        """trait Name  + properties, methods and constants copied into the classes that `use` it."""
        t = self.advance()
        name = self.expect_name("a trait name").value
        if self.tok.is_kw("extends", "implements"):
            self.error(f"a trait cannot {self.tok.value} anything — write 'use OtherTrait' inside trait {name}, "
                       "and 'implements' on the classes")
        members = self.class_body("trait")
        for m in members:
            if isinstance(m, N.EnumCase):
                self.error("'case' is only allowed inside an enum", m)
        members = self.promote_params(members)
        return N.ClassDecl(name, None, True, members, False, None, [], [], "trait", line=t.line, col=t.col)

    def enum_decl(self, attrs=()):
        """enum Status            (pure)     |   #[String]           (backed by #[String] or #[Int])
               case Active                   |   enum Status
                                             |       case Active = "active"
        Enums may also hold constants and methods, but no properties."""
        backing = None
        for a in attrs:
            kind = {"string": "string", "int": "int"}.get(a.name.lower()) if not a.args else None
            if kind is None:
                self.error(f"an enum only takes a #[String] or #[Int] attribute, got #[{a.name}]", a)
            if backing is not None:
                self.error("an enum can only have one backing type attribute", a)
            backing = kind
        t = self.advance()
        name = self.expect_name("an enum name").value
        if self.tok.is_op(":"):
            self.advance()
            bt = self.tok.value if self.tok.type == "NAME" else "string"
            self.error(f"write the backing type as an attribute above the enum:\n    #[{bt.capitalize()}]\n    enum {name}")
        if self.tok.is_kw("extends"):
            self.error("an enum cannot extend a class")
        interfaces = self.implements_list()
        if self.tok.type != "NEWLINE" or self.peek().type != "INDENT":
            self.error(f"enum {name} needs at least one case, e.g.\n    case Active")
        members = self.class_body("enum")
        cases, others = [], []
        seen_values = {}
        for m in members:
            if isinstance(m, N.EnumCase):
                if any(c.name == m.name for c in cases):
                    self.error(f"case '{m.name}' is declared twice in enum {name}", m)
                if backing is None and m.value is not None:
                    self.error(f"enum {name} has no backing type, so case '{m.name}' cannot have a value — "
                               f"put #[String] (or #[Int]) on the line above 'enum {name}' to give cases values", m)
                if backing is not None:
                    if m.value is None:
                        self.error(f"enum {name} is #[{backing.capitalize()}], so every case needs a value, e.g. case {m.name} = "
                                   + ('"' + m.name.lower() + '"' if backing == "string" else "1"), m)
                    m.value = self.enum_value(m.value, backing, name, m)
                    key = m.value.value
                    if key in seen_values:
                        self.error(f"cases '{seen_values[key]}' and '{m.name}' of enum {name} have the same value", m)
                    seen_values[key] = m.name
                cases.append(m)
            elif isinstance(m, N.Property):
                self.error(f"enums cannot have properties — use a constant or a method instead", m)
            else:
                if isinstance(m, N.FuncDecl) and m.is_abstract:
                    self.error("enum methods cannot be abstract", m)
                others.append(m)
        if not cases:
            self.error(f"enum {name} needs at least one case", t)
        names = {c.name for c in cases}
        for m in others:
            if m.name in names:
                self.error(f"'{m.name}' is already a case of enum {name}", m)
            if isinstance(m, N.FuncDecl) and m.name in ("cases", "from", "tryFrom", "__construct"):
                self.error(f"enums provide {m.name}() themselves; pick another method name", m)
        return N.ClassDecl(name, None, False, others, True, backing, cases, interfaces, "enum", line=t.line, col=t.col)

    def enum_value(self, expr, backing, enum_name, case):
        """Case values must be plain literals of the backing type."""
        if isinstance(expr, N.Unary) and expr.op == "-" and isinstance(expr.operand, N.Literal) \
                and type(expr.operand.value) is int:
            return N.Literal(-expr.operand.value, line=expr.line, col=expr.col)
        if isinstance(expr, N.Literal) and type(expr.value) is (int if backing == "int" else str):
            return expr
        got = "a" if not isinstance(expr, N.Literal) else type(expr.value).__name__.replace("str", "string")
        self.error(f"case '{case.name}' of #[{backing.capitalize()}] enum {enum_name} must be a plain {backing} value, got "
                   + ("an expression" if got == "a" else got), expr)

    def trait_rule(self):
        """[Trait::]method insteadof Trait, ...  |  [Trait::]method as [visibility] [alias]"""
        t = self.tok
        trait = None
        if self.peek().is_op("\\") or self.peek().is_op("::"):
            trait = self.qualified_name()
            self.expect_op("::", "between the trait and the method name")
        method = self.expect_name("a method name").value
        if self.tok.type == "NAME" and self.tok.value == "insteadof":
            self.advance()
            if trait is None:
                self.error(f"say which trait's {method}() to keep, e.g. 'A::{method} insteadof B'", t)
            others = [self.qualified_name()]
            while self.match_op(","):
                others.append(self.qualified_name())
            return N.TraitRule(trait, method, others, None, None, line=t.line, col=t.col)
        if self.tok.is_kw("as"):
            self.advance()
            visibility = alias = None
            if self.tok.is_kw("public", "private", "protected"):
                visibility = self.advance().value
            if self.tok.type == "NAME":
                alias = self.advance().value
            if visibility is None and alias is None:
                self.error(f"'as' needs a new name or a visibility, e.g. '{method} as {method}2' or '{method} as protected'")
            return N.TraitRule(trait, method, None, alias, visibility, line=t.line, col=t.col)
        self.error(f"expected 'insteadof' or 'as' after the method name, got {describe(self.tok)}")

    def class_member(self):
        t = self.tok
        if t.is_op("#["):
            return self.function_with_attrs(member=True)
        if t.is_kw("karo"):
            return self.function_decl([], t, member=True)
        if t.is_kw("const"):
            self.advance()
            name = self.expect_name("a constant name")
            self.expect_op("=", "in constant declaration")
            value = self.expression()
            self.end_statement()
            return N.ConstDecl(name.value, value, line=t.line, col=t.col)
        if t.is_kw("use"):
            if self.member_kind == "interface":
                self.error("interfaces cannot use traits — the classes that implement them can")
            self.advance()
            names = [self.qualified_name()]
            while self.match_op(","):
                names.append(self.qualified_name())
            if self.tok.is_op("{"):
                self.error("trait rules go in an indented block under 'use', not in { ... }, e.g.\n"
                           f"    use {', '.join(names)}\n        {names[0]}::method insteadof {names[-1]}")
            rules = []
            if self.tok.type == "NEWLINE" and self.peek().type == "INDENT":
                self.advance()
                self.advance()
                while self.tok.type not in ("DEDENT", "EOF"):
                    rules.append(self.trait_rule())
                    self.end_statement()
                    self.skip_newlines()
                if self.tok.type == "DEDENT":
                    self.advance()
            else:
                self.end_statement()
            return N.UseTrait(names, rules, line=t.line, col=t.col)
        if t.is_kw("case"):
            self.advance()
            name = self.expect_name("a case name")
            value = None
            if self.match_op("="):
                value = self.expression()
            self.end_statement()
            return N.EnumCase(name.value, value, line=t.line, col=t.col)
        visibility = "public"
        is_static = False
        if t.is_kw("public", "private", "protected"):
            visibility = self.advance().value
        if self.tok.is_kw("static"):
            self.advance()
            is_static = True
        if self.tok.type != "NAME" and not self.tok.is_op("?"):
            self.error(f"expected a property or method in class body, got {describe(self.tok)}")
        type_ref = self.type_or_error("a property type")
        if self.tok.type != "NAME":
            self.error(f"property needs a type and a name, e.g. 'public string {type_ref.name}'", t)
        name = self.advance()
        value = None
        if self.match_op("="):
            value = self.expression()
        self.end_statement()
        return N.Property(visibility, is_static, type_ref, name.value, value, line=t.line, col=t.col)

    def namespace_stmt(self):
        t = self.advance()
        name = self.qualified_name()
        self.end_statement()
        return N.Namespace(name, line=t.line, col=t.col)

    def use_stmt(self):
        t = self.advance()
        parts = [self.expect_name("a namespace").value]
        imports = []
        while self.tok.is_op("\\"):
            self.advance()
            if self.tok.is_op("{"):
                self.advance()
                prefix = "\\".join(parts)
                while not self.tok.is_op("}"):
                    item = self.qualified_name()
                    alias = None
                    if self.tok.is_kw("as"):
                        self.advance()
                        alias = self.expect_name("an alias").value
                    imports.append((prefix + "\\" + item, alias))
                    if not self.match_op(","):
                        break
                self.expect_op("}", "to close grouped import")
                self.end_statement()
                return N.Use(imports, line=t.line, col=t.col)
            parts.append(self.expect_name("a name").value)
        alias = None
        if self.tok.is_kw("as"):
            self.advance()
            alias = self.expect_name("an alias").value
        self.end_statement()
        return N.Use([("\\".join(parts), alias)], line=t.line, col=t.col)

    # -- expressions -------------------------------------------------------

    def expression(self):
        return self.ternary()

    def ternary(self):
        expr = self.coalesce()
        t = self.tok
        if t.is_op("?"):
            self.advance()
            then = self.ternary()
            self.expect_op(":", "in '? :' expression")
            other = self.ternary()
            return N.Ternary(expr, then, other, line=t.line, col=t.col)
        if t.is_kw("if"):
            self.advance()
            cond = self.coalesce()
            if not self.tok.is_kw("else"):
                self.error("inline 'if' needs an 'else', e.g. 'a if cond else b'")
            self.advance()
            other = self.ternary()
            return N.Ternary(cond, expr, other, line=t.line, col=t.col)
        return expr

    def coalesce(self):
        expr = self.or_expr()
        while self.tok.is_op("??"):
            t = self.advance()
            expr = N.Logical("??", expr, self.or_expr(), line=t.line, col=t.col)
        return expr

    def or_expr(self):
        expr = self.and_expr()
        while self.tok.is_kw("or"):
            t = self.advance()
            expr = N.Logical("or", expr, self.and_expr(), line=t.line, col=t.col)
        return expr

    def and_expr(self):
        expr = self.not_expr()
        while self.tok.is_kw("and"):
            t = self.advance()
            expr = N.Logical("and", expr, self.not_expr(), line=t.line, col=t.col)
        return expr

    def not_expr(self):
        if self.tok.is_kw("not"):
            t = self.advance()
            return N.Unary("not", self.not_expr(), line=t.line, col=t.col)
        return self.comparison()

    def comparison(self):
        expr = self.additive()
        while True:
            t = self.tok
            if t.type == "OP" and t.value in COMPARE_OPS:
                self.advance()
                expr = N.Compare(t.value, expr, self.additive(), line=t.line, col=t.col)
            elif t.is_kw("in"):
                self.advance()
                expr = N.Compare("in", expr, self.additive(), line=t.line, col=t.col)
            elif t.is_kw("not") and self.peek().is_kw("in"):
                self.advance()
                self.advance()
                expr = N.Compare("not in", expr, self.additive(), line=t.line, col=t.col)
            elif t.is_kw("is"):
                self.advance()
                negate = False
                if self.tok.is_kw("not"):
                    self.advance()
                    negate = True
                type_ref = self.type_or_error("a type after 'is'")
                expr = N.IsType(expr, type_ref, line=t.line, col=t.col)
                if negate:
                    expr = N.Unary("not", expr, line=t.line, col=t.col)
            else:
                return expr

    def additive(self):
        expr = self.multiplicative()
        while self.tok.is_op("+", "-"):
            t = self.advance()
            expr = N.Binary(t.value, expr, self.multiplicative(), line=t.line, col=t.col)
        return expr

    def multiplicative(self):
        expr = self.unary()
        while self.tok.is_op("*", "/", "%"):
            t = self.advance()
            expr = N.Binary(t.value, expr, self.unary(), line=t.line, col=t.col)
        return expr

    def unary(self):
        if self.tok.is_op("-", "+"):
            t = self.advance()
            return N.Unary(t.value, self.unary(), line=t.line, col=t.col)
        return self.power()

    def power(self):
        expr = self.postfix()
        if self.tok.is_op("**"):
            t = self.advance()
            return N.Binary("**", expr, self.unary(), line=t.line, col=t.col)
        return expr

    def postfix(self):
        expr = self.primary()
        while True:
            t = self.tok
            if t.is_op("("):
                args = self.call_args()
                expr = N.Call(expr, args, line=t.line, col=t.col)
            elif t.is_op(".", "?."):
                self.advance()
                name = self.tok
                if name.type not in ("NAME", "KEYWORD"):
                    self.error(f"expected a name after '{t.value}', got {describe(name)}")
                self.advance()
                expr = N.Attr(expr, name.value, t.value == "?.", line=name.line, col=name.col)
            elif t.is_op("["):
                self.advance()
                index = self.expression()
                self.expect_op("]", "to close index")
                expr = N.Index(expr, index, line=t.line, col=t.col)
            elif t.is_op("::"):
                self.advance()
                name = self.tok
                if name.is_kw("class"):
                    # User::class is the class itself (a class name is already a value)
                    if isinstance(expr, N.Name) and expr.id in ("self", "parent"):
                        self.error(f"{expr.id}::class is not supported — use __class__ for the current class's name")
                    self.advance()
                    continue
                if name.type != "NAME":
                    self.error(f"expected a name after '::', got {describe(name)}")
                self.advance()
                expr = N.StaticRef(expr, name.value, line=name.line, col=name.col)
            else:
                return expr

    def call_args(self):
        self.expect_op("(")
        args = []
        named = set()
        while not self.tok.is_op(")"):
            t = self.tok
            if t.type == "NAME" and self.peek().is_op(":"):
                self.advance()
                self.advance()
                if t.value in named:
                    self.error(f"argument '{t.value}' is given twice", t)
                named.add(t.value)
                args.append(N.NamedArg(t.value, self.expression(), line=t.line, col=t.col))
            else:
                if named:
                    self.error("positional arguments must come before named ones", t)
                if t.is_op("*"):
                    self.advance()
                    args.append(N.Spread(self.expression(), line=t.line, col=t.col))
                else:
                    args.append(self.expression())
            if not self.match_op(","):
                break
        self.expect_op(")", "to close the argument list")
        return args

    def primary(self):
        t = self.tok
        tt = t.type
        if tt in ("INT", "FLOAT"):
            self.advance()
            return N.Literal(t.value, line=t.line, col=t.col)
        if tt == "RAWSTRING":
            self.advance()
            return N.Literal(t.value, line=t.line, col=t.col)
        if tt == "STRING":
            self.advance()
            return self.interpolated(t)
        if tt == "NAME":
            self.advance()
            return N.Name(t.value, line=t.line, col=t.col)
        if tt == "KEYWORD":
            kw = t.value
            if kw in ("true", "false"):
                self.advance()
                return N.Literal(kw == "true", line=t.line, col=t.col)
            if kw == "null":
                self.advance()
                return N.Literal(None, line=t.line, col=t.col)
            if kw == "this":
                self.advance()
                return N.This(line=t.line, col=t.col)
            if kw in ("parent", "self"):
                self.advance()
                if not self.tok.is_op("::"):
                    self.error(f"'{kw}' must be followed by '::', e.g. {kw}::__construct()")
                return N.Name(kw, line=t.line, col=t.col)
            if kw == "new":
                self.advance()
                cls = self.qualified_name()
                args = self.call_args() if self.tok.is_op("(") else []
                return N.New(cls, args, line=t.line, col=t.col)
            if kw == "kr":
                return self.kr_lambda()
            if kw == "karo":
                return self.anonymous_function()
            self.error(f"unexpected keyword '{kw}' in expression")
        if tt == "OP":
            op = t.value
            if op == "(":
                lam = self.try_arrow_lambda()
                if lam is not None:
                    self.error("lambdas start with 'kr': kr(int n): int => n * 2", t)
                self.advance()
                expr = self.expression()
                self.expect_op(")", "to close '('")
                return expr
            if op == "#[":
                self.advance()
                items = self.items("]")
                return N.ListLit(items, line=t.line, col=t.col)
            if op == "#(":
                self.advance()
                items = self.items(")")
                return N.SetLit(items, line=t.line, col=t.col)
            if op == "#{":
                self.advance()
                pairs = []
                while not self.tok.is_op("}"):
                    key = self.expression()
                    self.expect_op(":", "between dict key and value")
                    pairs.append((key, self.expression()))
                    if not self.match_op(","):
                        break
                self.expect_op("}", "to close dict")
                return N.DictLit(pairs, line=t.line, col=t.col)
            if op == "~":
                self.advance()
                type_ref = self.type_or_error("a type after '~'")
                if type_ref.nullable:
                    self.error(f"a cast cannot be nullable — write ~{type_ref.name}(...) and check for null first", type_ref)
                self.expect_op("(", f"after ~{type_ref.name}")
                expr = self.expression()
                self.expect_op(")", "to close the cast")
                return N.Cast(type_ref, expr, line=t.line, col=t.col)
            if op == "[":
                self.error("lists are written with a # prefix: #[1, 2, 3]")
            if op == "{":
                self.error('dicts are written with a # prefix: #{"key": value}')
        if tt == "INDENT":
            self.error("unexpected indentation")
        self.error(f"expected an expression, got {describe(t)}")

    def items(self, closer):
        items = []
        while not self.tok.is_op(closer):
            items.append(self.expression())
            if not self.match_op(","):
                break
        self.expect_op(closer, "to close the collection")
        return items

    def try_arrow_lambda(self):
        """(int n): int => n * 2   — returns None when '(' starts something else."""
        start = self.pos
        t = self.tok
        try:
            self.advance()
            params = []
            while not self.tok.is_op(")"):
                pt = self.tok
                type_ref = self.parse_type()
                if self.tok.type != "NAME":
                    raise _Backtrack()
                pname = self.advance()
                params.append(N.Param(type_ref, pname.value, line=pt.line, col=pt.col))
                if not self.match_op(","):
                    break
            if not self.tok.is_op(")"):
                raise _Backtrack()
            self.advance()
            ret = None
            if self.tok.is_op(":"):
                self.advance()
                ret = self.parse_type()
            if not self.tok.is_op("=>"):
                raise _Backtrack()
        except _Backtrack:
            self.pos = start
            return None
        self.advance()
        body = self.expression()
        return N.Lambda(params, ret, body, True, line=t.line, col=t.col)

    def kr_lambda(self):
        """kr(int n): int => n * 2   — a one-expression lambda."""
        t = self.advance()
        params = self.params()
        self.no_promote(params)
        ret = None
        if self.match_op(":"):
            ret = self.type_or_error("a return type")
        if self.match_op("=>"):
            body = self.expression()
            return N.Lambda(params, ret, body, True, line=t.line, col=t.col)
        if self.tok.type == "NEWLINE":
            self.error("kr(...) is a one-line lambda: kr(int n): int => n * 2 — "
                       "for a body on several lines use an anonymous function: karo(...): type")
        self.error("expected '=>' after kr(...), e.g. kr(int n): int => n * 2")

    def anonymous_function(self):
        """karo(int n): int  + an indented body on the next lines."""
        t = self.advance()
        if not self.tok.is_op("("):
            self.error("an anonymous function is written karo(...): type with its body on the next lines")
        params = self.params()
        self.no_promote(params)
        ret = None
        if self.match_op(":"):
            ret = self.type_or_error("a return type")
        if self.tok.is_op("=>"):
            self.error("one-line functions are lambdas and use 'kr': kr(int n): int => n * 2")
        if self.tok.type != "NEWLINE":
            self.error("expected a new line with an indented body after karo(...)")
        if ret is None:
            self.error("an anonymous function needs a return type: karo(...): type (use void if it returns nothing)")
        body = self.block()
        # The block consumed the line ending, so the enclosing statement is done
        # unless we're inside brackets (then more tokens follow, e.g. ')').
        if self.tok.type != "OP" or self.tok.value not in (")", "]", "}", ","):
            self.ended_with_block = True
        return N.Lambda(params, ret, body, False, line=t.line, col=t.col)

    def interpolated(self, tok):
        parts = []
        for part in tok.value:
            if isinstance(part, str):
                parts.append(part)
                continue
            src, line, col = part
            sub = SourceFile(self.source.path, src)
            lexer = Lexer(sub)
            try:
                tokens = lexer.tokenize()
            except Exception as e:
                raise ParseError(f"in string interpolation: {getattr(e, 'message', e)}", self.source, line, col)
            for tk in tokens:
                if tk.line == 1:
                    tk.col += col - 1
                tk.line += line - 1
            sub_parser = Parser(tokens, self.source)
            expr = sub_parser.expression()
            if sub_parser.tok.type not in ("NEWLINE", "EOF"):
                sub_parser.error(f"unexpected {describe(sub_parser.tok)} in string interpolation")
            parts.append(expr)
        if all(isinstance(p, str) for p in parts):
            return N.Literal("".join(parts), line=tok.line, col=tok.col)
        return N.Interp(parts, line=tok.line, col=tok.col)


def parse(source):
    from .lexer import tokenize
    tokens = tokenize(source)
    return Parser(tokens, source).parse_program()
