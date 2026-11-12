"""Barq templates (v0.8.0): the View module and the global view() function.

Templates are HTML files under views/ with a few extra tags:

    {expr}  {!expr!}             escaped / raw output of a Barq expression
    <if condition="{x}"> <elseif condition="{y}"> <else> </if>
    <each item in {items}> ... <else> ... </each>     (<else> = the list was empty)
    <include file="partials.nav" with="{#{"title": t}}" />
    <extends layout="layout" />   <section name="content"> ... </section>
    <yield name="content" />      <let total="{a + b}" />
    <verbatim> ... </verbatim>    {-- template comment --}

A template is parsed once into a small tree (cached until the file changes).
Each {expr} is parsed by the normal Barq parser and run by the tree-walker,
so expressions behave the same on both engines. Inside templates `a.b` also
reads dict keys, so database rows can be written {user.name}.
"""

import bisect
import html
import json
import os
import re

from . import nodes as N
from .errors import LexError, ParseError, SourceFile
from .runtime import BarqObject, Env, NativeFunction, to_display, type_name
from .stdlib import arg, module, to_json_value


# ===========================================================================
# template tree
# ===========================================================================

class TNode:
    __slots__ = ("kind", "line", "col", "a", "b", "c", "d")

    def __init__(self, kind, pos, a=None, b=None, c=None, d=None):
        self.kind = kind
        self.line, self.col = pos
        self.a, self.b, self.c, self.d = a, b, c, d


# kind      a            b              c            d
# text      string
# out       expr         raw (bool)
# if        branches     else body                         branches: [(expr, body)]
# each      key name     var name       expr         [body, empty body]
# include   file value   with value
# extends   layout value
# section   name         body
# yield     name         default body (or None)
# let       [(name, value)]
#
# A "value" is ("str", text) or ("expr", node).


class Template:
    def __init__(self, name, source, nodes, extends):
        self.name = name
        self.source = source      # SourceFile (errors show the template line)
        self.nodes = nodes
        self.extends = extends    # TNode or None


class Marker:
    """Stands in for an AST node when an error is raised at a template position."""

    def __init__(self, source, line, col):
        self.tpl_src = source
        self.line = line
        self.col = col


# ===========================================================================
# parsing
# ===========================================================================

TAGS = ("if", "elseif", "else", "each", "include", "extends", "section", "yield", "let", "verbatim")
TAG_RE = re.compile(r"<(/?)(" + "|".join(TAGS) + r")(?=[\s/>])")
RAW_OPEN_RE = re.compile(r"<(script|style)(?=[\s>])", re.I)
ATTR_NAME_RE = re.compile(r"[A-Za-z_][\w-]*")
EACH_RE = re.compile(r"\s*([A-Za-z_]\w*)(?:\s*,\s*([A-Za-z_]\w*))?\s+in\s+")
NAME_RE = re.compile(r"^[A-Za-z_]\w*$")
# Tags that print nothing: a line holding only one of them is left out of the output.
QUIET = {"if", "elseif", "else", "each", "extends", "section", "let", "/if", "/each", "/section"}


class TemplateSyntax(Exception):
    def __init__(self, message, index):
        super().__init__(message)
        self.message = message
        self.index = index


class Parser:
    def __init__(self, i, name, source):
        self.i = i
        self.name = name
        self.source = source
        self.text = source.text
        self.starts = [0] + [m.end() for m in re.finditer("\n", self.text)]
        self.raw = False

    # -- positions -----------------------------------------------------------

    def pos(self, index):
        k = bisect.bisect_right(self.starts, index) - 1
        return k + 1, index - self.starts[k] + 1

    def fail(self, message, index):
        raise TemplateSyntax(message, index)

    # -- scanning Barq expressions ---------------------------------------------

    def match_brace(self, k):
        """k is just after a '{'. Returns the index of the matching '}'."""
        t, n, depth = self.text, len(self.text), 0
        while k < n:
            c = t[k]
            if c == '"':
                k = self.skip_string(k + 1, '"')
                continue
            if c == "'":
                k = self.skip_string(k + 1, "'")
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    return k
                depth -= 1
            k += 1
        return -1

    def skip_string(self, k, quote):
        t, n = self.text, len(self.text)
        while k < n:
            c = t[k]
            if c == "\\":
                k += 2
                continue
            if c == quote:
                return k + 1
            if c == "{" and quote == '"':
                end = self.match_brace(k + 1)
                if end < 0:
                    return n
                k = end + 1
                continue
            k += 1
        return n

    def expression(self, start, end):
        """Parse text[start:end] as one Barq expression."""
        from .lexer import tokenize
        from .parser import Parser as BarqParser
        src = self.text[start:end]
        if not src.strip():
            self.fail("empty expression {}", start - 1)
        line0, col0 = self.pos(start)
        wrapped = SourceFile(self.source.path, "(" + src + ")")
        try:
            toks = tokenize(wrapped)
            p = BarqParser(toks, wrapped)
            expr = p.expression()
            if p.tok.type not in ("NEWLINE", "EOF"):
                p.error(f"unexpected {p.tok.value!r} after the expression")
        except (LexError, ParseError) as e:
            line, col = self.shift(e.line or 1, e.col or 1, line0, col0)
            raise _Located(e.message, line, col) from None
        self.place(expr, line0, col0)
        if self.raw:   # barq/native.py: the Rust compiler applies the template rules itself
            return expr
        return lenient(expr, self.source)

    @staticmethod
    def shift(line, col, line0, col0):
        if line == 1:
            return line0, max(col0, col0 + col - 2)
        return line0 + line - 1, col

    def place(self, node, line0, col0):
        """Move positions from the wrapped expression to the template, and tag nodes
        with the template source so errors point into the template."""
        seen = set()

        def walk(x):
            if isinstance(x, N.Node):
                if id(x) in seen:
                    return
                seen.add(id(x))
                if x.line:
                    x.line, x.col = self.shift(x.line, x.col, line0, col0)
                x.tpl_src = self.source
                for f in x._fields:
                    walk(getattr(x, f))
            elif isinstance(x, (list, tuple)):
                for y in x:
                    walk(y)

        walk(node)

    # -- tags ------------------------------------------------------------------

    def attrs(self, k, tag):
        """Read attributes from k up to '>' or '/>'. Returns (attrs, self_closing, end index)."""
        t, n = self.text, len(self.text)
        out = {}
        if tag == "each":
            m = EACH_RE.match(t, k)
            if not m:
                self.fail("write <each item in {list}> or <each key, item in {list}>", k)
            k = m.end()
            if k >= n or t[k] != "{":
                self.fail("<each>: the list must be written in braces, e.g. in {users}", k)
            end = self.match_brace(k + 1)
            if end < 0:
                self.fail("unclosed { in <each>", k)
            out["key"], out["var"] = (m.group(1), m.group(2)) if m.group(2) else (None, m.group(1))
            out["in"] = ("expr", self.expression(k + 1, end))
            k = end + 1
        while True:
            while k < n and t[k] in " \t\r\n":
                k += 1
            if k >= n:
                self.fail(f"<{tag}> is not closed with '>'", k)
            if t.startswith("/>", k):
                return out, True, k + 2
            if t[k] == ">":
                return out, False, k + 1
            if t[k] == "{" and tag in ("if", "elseif") and "condition" not in out:
                end = self.match_brace(k + 1)
                if end < 0:
                    self.fail(f"unclosed {{ in <{tag}>", k)
                out["condition"] = ("expr", self.expression(k + 1, end))
                k = end + 1
                continue
            m = ATTR_NAME_RE.match(t, k)
            if not m:
                self.fail(f"unexpected {t[k]!r} in <{tag}>", k)
            name = m.group(0)
            k = m.end()
            if name in out:
                self.fail(f'<{tag}> has two "{name}" attributes', m.start())
            if not t.startswith("=", k):
                out[name] = ("str", "")
                continue
            k += 1
            quote = t[k] if k < n and t[k] in "\"'" else None
            if quote:
                k += 1
            if k < n and t[k] == "{":
                end = self.match_brace(k + 1)
                if end < 0:
                    self.fail(f'unclosed {{ in <{tag} {name}="...">', k)
                out[name] = ("expr", self.expression(k + 1, end))
                k = end + 1
                if quote:
                    if not t.startswith(quote, k):
                        self.fail(f'<{tag} {name}="{{...}}">: put only one {{expression}} in the attribute', k)
                    k += 1
            elif quote:
                end = t.find(quote, k)
                if end < 0:
                    self.fail(f"<{tag}>: unclosed {quote} in attribute {name}", k)
                out[name] = ("str", t[k:end])
                k = end + 1
            else:
                self.fail(f"<{tag}>: the value of {name} must be quoted", k)

    # -- the main scan -----------------------------------------------------------

    def tokens(self):
        """Flat list of (kind, index, ...) tokens."""
        t, n = self.text, len(self.text)
        toks = []
        buf = []
        k = 0
        raw = None          # inside <script>/<style>: plain braces are text
        pending_raw = None  # saw <script, waiting for its '>'
        sections = []       # True for Barq <section>, False for HTML <section>
        lit_start = 0

        def flush():
            if buf:
                toks.append(("text", lit_start, "".join(buf)))
                buf.clear()

        def quiet_line(start, end, kind):
            """If the tag is alone on its line, drop the line. Returns the new end."""
            if kind not in QUIET:
                return end
            ls = t.rfind("\n", 0, start) + 1
            le = t.find("\n", end)
            le = n if le < 0 else le
            if t[ls:start].strip() or t[end:le].strip():
                return end
            # strip the indentation already emitted as text (toks[-1] is the tag itself)
            drop = start - ls
            if drop and len(toks) > 1 and toks[-2][0] == "text":
                kind, at, txt = toks[-2]
                toks[-2] = (kind, at, txt[:-drop])
            return le + 1 if le < n else n

        while k < n:
            c = t[k]
            if c == "\\" and t.startswith("\\{", k):
                if not buf:
                    lit_start = k
                buf.append("{")
                k += 2
                continue
            if c == "{":
                if t.startswith("{--", k):
                    end = t.find("--}", k + 3)
                    if end < 0:
                        self.fail("unclosed {-- comment", k)
                    k = end + 3
                    continue
                if raw and not t.startswith("{!", k):
                    buf.append(c)
                    k += 1
                    continue
                end = self.match_brace(k + 1)
                if end < 0:
                    self.fail("unclosed { — write \\{ for a literal brace", k)
                inner = t[k + 1:end]
                is_raw = len(inner) >= 2 and inner[0] == "!" and inner[-1] == "!"
                if raw and not is_raw:
                    buf.append(c)
                    k += 1
                    continue
                flush()
                if is_raw:
                    toks.append(("out", k, self.expression(k + 2, end - 1), True))
                else:
                    toks.append(("out", k, self.expression(k + 1, end), False))
                k = end + 1
                continue
            if c == ">" and pending_raw:
                raw, pending_raw = pending_raw, None
                if not buf:
                    lit_start = k
                buf.append(c)
                k += 1
                continue
            if c == "<":
                if t.startswith("<!--", k):
                    end = t.find("-->", k + 4)
                    end = n if end < 0 else end + 3
                    if not buf:
                        lit_start = k
                    buf.append(t[k:end])
                    k = end
                    continue
                if raw:
                    if t[k:k + len(raw) + 2].lower() == "</" + raw:
                        raw = None
                elif not pending_raw:
                    m = RAW_OPEN_RE.match(t, k)
                    if m:
                        pending_raw = m.group(1).lower()
                m = TAG_RE.match(t, k)
                if m:
                    closing, tag = m.group(1) == "/", m.group(2)
                    if tag == "section":
                        if closing:
                            if not sections or not sections.pop():
                                m = None   # an HTML </section>
                        else:
                            a, selfc, end = self.attrs(m.end(), tag) if "name" in self.peek_attrs(m.end()) else (None, None, None)
                            sections.append(a is not None)
                            if a is None:
                                m = None
                    if m:
                        flush()
                        if closing:
                            gt = t.find(">", m.end())
                            if gt < 0 or t[m.end():gt].strip():
                                self.fail(f"write </{tag}>", k)
                            end = gt + 1
                            toks.append(("close", k, tag))
                            k = quiet_line(k, end, "/" + tag)
                        else:
                            if tag != "section":
                                a, selfc, end = self.attrs(m.end(), tag)
                            if tag == "verbatim":
                                close = t.find("</verbatim>", end)
                                if close < 0:
                                    self.fail("<verbatim> is never closed with </verbatim>", k)
                                toks.append(("text", end, t[end:close]))
                                end = close + len("</verbatim>")
                                k = end
                                continue
                            toks.append(("open", k, tag, a, selfc))
                            k = quiet_line(k, end, tag)
                        lit_start = k
                        continue
            if not buf:
                lit_start = k
            buf.append(c)
            k += 1
        flush()
        return toks

    def peek_attrs(self, k):
        """Attribute names of the tag starting at k, without parsing values (for <section>)."""
        gt = self.text.find(">", k)
        return set(ATTR_NAME_RE.findall(re.sub(r'"[^"]*"|\'[^\']*\'', "", self.text[k:gt if gt >= 0 else len(self.text)])))

    # -- tree building ------------------------------------------------------------

    def parse(self):
        toks = self.tokens()
        root = []
        # stack entries: [tag, node, current body list, open index]
        stack = []
        body = root
        extends = None

        for tok in toks:
            kind, idx = tok[0], tok[1]
            p = self.pos(idx)
            if kind == "text":
                body.append(TNode("text", p, tok[2]))
            elif kind == "out":
                body.append(TNode("out", p, tok[2], tok[3]))
            elif kind == "open":
                tag, a, selfc = tok[2], tok[3], tok[4]
                if tag == "if":
                    cond = self.need(a, "condition", tag, idx, expr=True)
                    node = TNode("if", p, [(cond, [])], None)
                    body.append(node)
                    stack.append(["if", node, idx])
                    body = node.a[0][1]
                elif tag in ("elseif", "else"):
                    top = stack[-1] if stack else None
                    if top is None or top[0] not in ("if", "each"):
                        self.fail(f"<{tag}> must be inside <if> or <each>", idx)
                    node = top[1]
                    if top[0] == "each":
                        if tag == "elseif" or node.d[1] is not None:
                            self.fail("<each> can have one <else> (shown when the list is empty)", idx)
                        node.d[1] = []
                        body = node.d[1]
                    elif node.b is not None:
                        self.fail(f"<{tag}> after <else>", idx)
                    elif tag == "elseif":
                        cond = self.need(a, "condition", tag, idx, expr=True)
                        node.a.append((cond, []))
                        body = node.a[-1][1]
                    else:
                        node.b = []
                        body = node.b
                elif tag == "each":
                    node = TNode("each", p, a["key"], a["var"], a["in"][1], [[], None])
                    body.append(node)
                    stack.append(["each", node, idx])
                    body = node.d[0]
                elif tag == "include":
                    f = self.need(a, "file", tag, idx)
                    body.append(TNode("include", p, f, a.get("with")))
                elif tag == "extends":
                    if stack:
                        self.fail("<extends> must be at the top level of the template", idx)
                    if extends is not None:
                        self.fail("a template can only extend one layout", idx)
                    extends = TNode("extends", p, self.need(a, "layout", tag, idx))
                elif tag in ("section", "yield"):
                    name = self.need(a, "name", tag, idx)
                    if name[0] != "str" or not name[1]:
                        self.fail(f'<{tag} name="..."> must be a plain name', idx)
                    node = TNode(tag, p, name[1], None if (tag == "yield" and selfc) else [])
                    body.append(node)
                    if not selfc:
                        stack.append([tag, node, idx])
                        body = node.b
                    elif tag == "section":
                        self.fail('<section name="..."> needs a closing </section>', idx)
                elif tag == "let":
                    pairs = []
                    for name, value in a.items():
                        if not NAME_RE.match(name):
                            self.fail(f"<let>: '{name}' is not a valid variable name", idx)
                        pairs.append((name, value))
                    if not pairs:
                        self.fail('write <let name="{value}" />', idx)
                    body.append(TNode("let", p, pairs))
                for key in a or {}:
                    if key not in ALLOWED[tag]:
                        self.fail(f'<{tag}> has no "{key}" attribute (use {", ".join(ALLOWED[tag]) or "none"})', idx)
            elif kind == "close":
                tag = tok[2]
                if tag in ("elseif", "else", "include", "extends", "let", "verbatim"):
                    self.fail(f"</{tag}> is not needed — <{tag}> has no closing tag", idx)
                if not stack or stack[-1][0] != tag:
                    if stack:
                        self.fail(f"</{tag}> found, but <{stack[-1][0]}> is still open", idx)
                    self.fail(f"</{tag}> without an opening <{tag}>", idx)
                stack.pop()
                body = self.current_body(stack, root)
        if stack:
            self.fail(f"<{stack[-1][0]}> is never closed with </{stack[-1][0]}>", stack[-1][2])
        if extends is not None:
            for node in root:
                if node.kind in ("section", "let") or (node.kind == "text" and not node.a.strip()):
                    continue
                at = node
                raise _Located("a template that uses <extends> can only contain <section> and <let> at the top "
                               "level — put this inside a <section>", at.line, at.col)
        return Template(self.name, self.source, root, extends)

    @staticmethod
    def current_body(stack, root):
        if not stack:
            return root
        tag, node = stack[-1][0], stack[-1][1]
        if tag == "if":
            return node.b if node.b is not None else node.a[-1][1]
        if tag == "each":
            return node.d[1] if node.d[1] is not None else node.d[0]
        return node.b

    def need(self, a, key, tag, idx, expr=False):
        if a is None or key not in a:
            self.fail(f'<{tag}> needs a {key}="..." attribute', idx)
        v = a[key]
        if expr:
            if v[0] != "expr":
                self.fail(f'<{tag} {key}="{{...}}">: the condition must be a {{expression}}', idx)
            return v[1]
        return v


ALLOWED = {
    "if": ("condition",), "elseif": ("condition",), "else": (), "each": ("key", "var", "in"),
    "include": ("file", "with"), "extends": ("layout",), "section": ("name",), "yield": ("name",),
    "let": None, "verbatim": (),
}


class _AnyKeys(tuple):
    def __contains__(self, item):
        return True


ALLOWED["let"] = _AnyKeys()


class _Located(Exception):
    def __init__(self, message, line, col):
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col


# ---------------------------------------------------------------------------
# `a.b` on dicts
# ---------------------------------------------------------------------------

def tpl_get(i, a, n):
    obj, name, safe = a
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        if safe:
            return None
        from .builtins import builtin_method
        method = builtin_method(i, obj, name, n)
        if method is not None:
            return method
        keys = ", ".join(f'"{k}"' for k in list(obj)[:8] if isinstance(k, str))
        i.throw("NameException", f'dict has no key "{name}"' + (f" (keys: {keys})" if keys else " (it is empty)")
                + f" — use ?.{name} if it may be missing", n)
    if obj is None and safe:
        return None
    return i.get_attr(obj, name, n, Env())


TPL_GET = NativeFunction("get", tpl_get, 3, 3)


def tpl_opt(i, a, n):
    """A variable on the left of ??: null when it is not defined."""
    env = getattr(i, "_tpl_env", None)
    from .interpreter import IMPORT_PENDING
    entry = env.find(a[0]) if env is not None else None
    if entry is not None and entry[0] is not IMPORT_PENDING:
        return entry[0]
    value = i.resolve_name(a[0], i.module, n) if i.module is not None else None
    return value


TPL_OPT = NativeFunction("get", tpl_opt, 1, 1)


def lenient(expr, source):
    """Rewrite `a.b` (not a method call) so it also reads dict keys."""

    def soft(x):
        """Left side of ??: a missing variable or dict key is null, not an error."""
        if isinstance(x, N.Name):
            return tag(N.Call(lit(TPL_OPT, x), [lit(x.id, x)], line=x.line, col=x.col))
        if isinstance(x, N.Attr):
            return tag(N.Call(lit(TPL_GET, x), [soft(x.obj), lit(x.name, x), lit(True, x)], line=x.line, col=x.col))
        return fix(x)

    def lit(v, at):
        return tag(N.Literal(v, line=at.line, col=at.col))

    def fix(x):
        if isinstance(x, N.Logical) and x.op == "??":
            x.left = soft(x.left)
            x.right = fix(x.right)
            return x
        if isinstance(x, N.Call):
            f = x.func
            if isinstance(f, N.Attr):
                f.obj = fix(f.obj)
            else:
                x.func = fix(f)
            x.args = [fix(y) for y in x.args]
            return x
        if isinstance(x, N.Attr):
            obj = fix(x.obj)
            return tag(N.Call(lit(TPL_GET, x), [obj, lit(x.name, x), lit(bool(x.safe), x)], line=x.line, col=x.col))
        if isinstance(x, N.Lambda) and not x.is_expr:
            return x   # statement bodies keep normal Barq rules
        if isinstance(x, N.Node):
            for f in x._fields:
                setattr(x, f, fix(getattr(x, f)))
            return x
        if isinstance(x, list):
            return [fix(y) for y in x]
        if isinstance(x, tuple):
            return tuple(fix(y) for y in x)
        return x

    def tag(node):
        node.tpl_src = source
        return node

    return fix(expr)


# ===========================================================================
# rendering
# ===========================================================================

class ViewState:
    def __init__(self, root):
        self.root = root
        self.folder = os.path.join(root, "views")
        self.shared = {}
        self.cache = {}     # path -> (mtime_ns, size, Template)


def state(i):
    s = getattr(i, "_views", None)
    if s is None:
        s = i._views = ViewState(i.root)
    return s


class Ctx:
    def __init__(self, sections=None, depth=0):
        self.sections = sections if sections is not None else {}
        self.depth = depth


MAX_DEPTH = 40


def view_path(i, name, n):
    if not isinstance(name, str):
        i.throw("TypeException", f"a view name must be a string, got {type_name(name)}", n)
    rel = name.strip()
    if not rel.endswith(".bq"):
        rel = rel.replace(".", "/") + ".bq"
    parts = rel.replace("\\", "/").split("/")
    if not name.strip() or rel.startswith("/") or any(p in ("", "..", ".") for p in parts):
        i.throw("ValueException", f'invalid view name "{name}" — use dots or slashes, e.g. "users.index"', n)
    return os.path.join(state(i).folder, *parts)


def load(i, name, n):
    path = view_path(i, name, n)
    s = state(i)
    try:
        st = os.stat(path)
    except OSError:
        rel = os.path.relpath(path, i.root).replace(os.sep, "/")
        i.throw("TemplateException", f'view "{name}" not found (looked for {rel})', n)
    hit = s.cache.get(path)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text.endswith("\n"):   # the file's last newline is not part of the output
        text = text[:-1]
    rel = os.path.relpath(path, i.root).replace(os.sep, "/")
    tpl = compile_template(i, name, SourceFile(rel, text))
    s.cache[path] = (st.st_mtime_ns, st.st_size, tpl)
    return tpl


def compile_template(i, name, source):
    p = Parser(i, name, source)
    try:
        return p.parse()
    except TemplateSyntax as e:
        line, col = p.pos(e.index)
        i.throw("TemplateException", e.message, Marker(source, line, col))
    except _Located as e:
        i.throw("TemplateException", e.message, Marker(source, e.line, e.col))


def scope(i, data, n, fname):
    """The variables a template sees: its data, then shared values, then the
    classes and functions of the file that rendered it, then the builtins."""
    caller = i.module
    base = Env(i.builtins)
    base.declare("json", JSON_FN, None, const=True)
    from .web import error_bag
    base.declare("errors", error_bag(i), None)     # validation errors flashed by the last request
    if caller is not None:
        for name, value in list(caller.functions.items()) + list(caller.classes.items()):
            base.declare(name, value, None, const=True)
    shared = Env(base)
    for k, v in state(i).shared.items():
        shared.declare(k, v)
    env = Env(shared)
    for k, v in (data or {}).items():
        if not isinstance(k, str) or not NAME_RE.match(k):
            i.throw("ValueException", f"{fname}(): template variable names must be identifiers, got {to_display(k, i, True)}", n)
        env.declare(k, v)
    return env


def ev(i, node, env):
    prev = getattr(i, "_tpl_env", None)
    i._tpl_env = env
    try:
        return i.eval(node, env)
    finally:
        i._tpl_env = prev


def cond(i, node, env):
    prev = getattr(i, "_tpl_env", None)
    i._tpl_env = env
    try:
        return i.cond(node, env, "<if> condition")
    finally:
        i._tpl_env = prev


def value_of(i, v, env):
    return v[1] if v[0] == "str" else ev(i, v[1], env)


def display(i, v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return to_display(v, i)


def render_template(i, tpl, env, ctx):
    if ctx.depth > MAX_DEPTH:
        i.throw("TemplateException", "views include or extend each other too deeply (a loop?)",
                Marker(tpl.source, 1, 0))
    if tpl.extends is not None:
        for node in tpl.nodes:
            if node.kind == "section":
                if node.a not in ctx.sections:
                    out = []
                    render_nodes(i, node.b, env, ctx, out, tpl)
                    ctx.sections[node.a] = "".join(out)
            elif node.kind == "let":
                render_nodes(i, [node], env, ctx, [], tpl)
        layout = value_of(i, tpl.extends.a, env)
        parent = load(i, layout, Marker(tpl.source, tpl.extends.line, tpl.extends.col))
        return render_template(i, parent, env, Ctx(ctx.sections, ctx.depth + 1))
    out = []
    render_nodes(i, tpl.nodes, env, ctx, out, tpl)
    return "".join(out)


def render_nodes(i, nodes, env, ctx, out, tpl):
    for node in nodes:
        k = node.kind
        if k == "text":
            out.append(node.a)
        elif k == "out":
            s = display(i, ev(i, node.a, env))
            out.append(s if node.b else html.escape(s, quote=True))
        elif k == "if":
            for test, body in node.a:
                if cond(i, test, env):
                    render_nodes(i, body, env, ctx, out, tpl)
                    break
            else:
                if node.b is not None:
                    render_nodes(i, node.b, env, ctx, out, tpl)
        elif k == "each":
            render_each(i, node, env, ctx, out, tpl)
        elif k == "include":
            render_include(i, node, env, ctx, out, tpl)
        elif k == "section":
            if node.a in ctx.sections:
                out.append(ctx.sections[node.a])
            else:
                render_nodes(i, node.b, env, ctx, out, tpl)
        elif k == "yield":
            if node.a in ctx.sections:
                out.append(ctx.sections[node.a])
            elif node.b is not None:
                render_nodes(i, node.b, env, ctx, out, tpl)
        elif k == "let":
            for name, v in node.a:
                env.declare(name, value_of(i, v, env))


def render_each(i, node, env, ctx, out, tpl):
    key, var, expr, (body, empty) = node.a, node.b, node.c, node.d
    items = i.iterate(ev(i, expr, env), expr, key is not None)
    if not items:
        if empty is not None:
            render_nodes(i, empty, env, ctx, out, tpl)
        return
    outer = env.find("loop")
    parent = outer[0] if outer is not None and isinstance(outer[0], dict) and "count" in outer[0] else None
    count = len(items)
    for k, item in enumerate(items):
        e = Env(env)
        if key is not None:
            e.declare(key, item[0])
            e.declare(var, item[1])
        else:
            e.declare(var, item)
        e.declare("loop", {"index": k, "number": k + 1, "count": count, "first": k == 0, "last": k == count - 1,
                           "even": (k + 1) % 2 == 0, "odd": (k + 1) % 2 == 1, "parent": parent})
        render_nodes(i, body, e, ctx, out, tpl)


def render_include(i, node, env, ctx, out, tpl):
    at = Marker(tpl.source, node.line, node.col)
    name = value_of(i, node.a, env)
    child = Env(env)
    if node.b is not None:
        extra = value_of(i, node.b, env)
        if not isinstance(extra, dict):
            i.throw("TypeException", f'<include with="{{...}}"> needs a dict, got {type_name(extra)}', at)
        for k, v in extra.items():
            if not isinstance(k, str) or not NAME_RE.match(k):
                i.throw("ValueException", f"<include with>: variable names must be identifiers, got {to_display(k, i, True)}", at)
            child.declare(k, v)
    sub = load(i, name, at)
    out.append(render_template(i, sub, child, Ctx(dict(ctx.sections), ctx.depth + 1)))


# ===========================================================================
# the API
# ===========================================================================

def check_data(i, a, k, fname, n):
    data = arg(i, a, k, ("dict", "null"), fname, n)
    return data or {}


def render(i, name, data, n):
    tpl = load(i, name, n)
    return render_template(i, tpl, scope(i, data, n, "view"), Ctx())


def g_view(i, a, n):
    from .stdlib2 import check_status, make_response
    name = arg(i, a, 0, ("string",), "view", n)
    data = check_data(i, a, 1, "view", n)
    status = check_status(i, arg(i, a, 2, ("int",), "view", n, 200), n)
    body = render(i, name, data, n)
    return make_response(i, body, status, {"Content-Type": "text/html; charset=utf-8"})


def v_render(i, a, n):
    return render(i, arg(i, a, 0, ("string",), "View.render", n), check_data(i, a, 1, "View.render", n), n)


def v_string(i, a, n):
    text = arg(i, a, 0, ("string",), "View.string", n)
    data = check_data(i, a, 1, "View.string", n)
    tpl = compile_template(i, "<string>", SourceFile("<view string>", text))
    return render_template(i, tpl, scope(i, data, n, "View.string"), Ctx())


def v_exists(i, a, n):
    path = view_path(i, arg(i, a, 0, ("string",), "View.exists", n), n)
    return os.path.isfile(path)


def v_share(i, a, n):
    first = a[0]
    shared = state(i).shared
    if isinstance(first, dict):
        if len(a) > 1:
            i.throw("TypeException", "View.share(dict) takes one argument", n)
        items = first.items()
    elif isinstance(first, str):
        if len(a) < 2:
            i.throw("TypeException", "View.share(name, value) needs a value", n)
        items = [(first, a[1])]
    else:
        i.throw("TypeException", f"View.share() argument 1 must be string or dict, got {type_name(first)}", n)
    for k, v in items:
        if not isinstance(k, str) or not NAME_RE.match(k):
            i.throw("ValueException", f"View.share(): names must be identifiers, got {to_display(k, i, True)}", n)
        shared[k] = v
    return None


def v_shared(i, a, n):
    return dict(state(i).shared)


def v_path(i, a, n):
    s = state(i)
    if a:
        folder = arg(i, a, 0, ("string",), "View.path", n)
        s.folder = folder if os.path.isabs(folder) else os.path.join(i.root, folder)
        s.cache.clear()
    return os.path.relpath(s.folder, i.root).replace(os.sep, "/")


def v_escape(i, a, n):
    return html.escape(display(i, a[0]), quote=True)


def v_clear(i, a, n):
    state(i).cache.clear()
    return None


def safe_json(i, a, n):
    """JSON that is safe inside <script> and HTML attributes: {!json(data)!}."""
    text = json.dumps(to_json_value(i, a[0], n), ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") \
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


JSON_FN = NativeFunction("json", safe_json, 1, 1)

VIEW = {
    "render": (v_render, 1, 2),
    "string": (v_string, 1, 2),
    "exists": (v_exists, 1, 1),
    "share": (v_share, 1, 2),
    "shared": (v_shared, 0, 0),
    "path": (v_path, 0, 1),
    "escape": (v_escape, 1, 1),
    "json": (safe_json, 1, 1),
    "clearCache": (v_clear, 0, 0),
}


def install(i):
    from .runtime import BarqClass
    cls = BarqClass("TemplateException", "TemplateException", None, parent=i.classes["Exception"], native=True)
    i.classes["TemplateException"] = cls
    i.builtins.declare("TemplateException", cls, None, const=True)
    i.builtins.declare("View", module("View", VIEW), None, const=True)
    i.builtins.declare("view", NativeFunction("view", g_view, 1, 3), None, const=True)
