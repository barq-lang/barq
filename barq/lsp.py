"""`barq lsp` — a language server for editors (the Language Server Protocol on stdin/stdout).

    barq lsp        an editor starts this; it then talks JSON-RPC on stdin/stdout

It uses the same lexer, parser, checker and formatter that run programs:

  diagnostics        syntax and type errors while you type (the same as `barq check`)
  formatting         `barq fmt`
  outline            classes, functions, methods, properties, enum cases
  completion         variables, members after `.` and `::`, project classes (adds the
                     `use` line), built-in functions, modules and classes, keywords
  hover              signatures, types and the `//` comment above a declaration
  definition         functions, classes, methods, properties, variables, `use` lines
  references         also document highlights, and rename for variables and parameters
  signature help     parameter hints inside a call
  workspace symbols  search classes, functions and methods across the project

Settings (initializationOptions, or workspace/didChangeConfiguration under "barq"):
  diagnostics (true), diagnosticsOnType (true), sortImports (true).

Types come from declarations only (every Barq variable has one), so
`this.repo.find(id).` completes when `find` declares its return type.
"""

import json
import os
import queue
import re
import sys
import threading
import time

from . import nodes as N
from .errors import BarqError, SourceFile
from .interpreter import module_candidates
from .runtime import BarqClass, BarqFunction, NativeFunction, NativeModule, type_str

SKIP_DIRS = {"views", "node_modules", "vendor"}
MAX_FILES = 5000
DEBOUNCE = 0.3

KEYWORDS = ["if", "elseif", "else", "for", "foreach", "while", "do", "break", "continue", "return",
            "switch", "case", "default", "class", "enum", "abstract", "extends", "implements",
            "interface", "trait", "new", "this", "self", "parent", "static", "const", "namespace",
            "use", "as", "karo", "kr", "public", "private", "protected", "null", "true", "false",
            "and", "or", "not", "is", "in", "try", "except", "finally", "raise"]
TYPES = ["int", "float", "string", "bool", "void", "any", "list", "dict", "set", "karo"]
ATTRIBUTES = ["Public", "Private", "Protected", "Static", "Abstract"]
PRIMITIVE = {"int", "float", "string", "bool", "list", "dict", "set"}

# Model statics that return an instance of the class they are called on (User.find(1).name).
RETURNS_SELF = {"find", "findOrFail", "first", "firstOrFail", "create", "make", "firstOrCreate",
                "firstOrNew", "updateOrCreate", "forceCreate"}

# LSP enums
CI = {"method": 2, "function": 3, "constructor": 4, "field": 5, "var": 6, "param": 6, "class": 7,
      "interface": 8, "module": 9, "property": 10, "enum": 13, "keyword": 14, "case": 20,
      "const": 21, "trait": 7, "type": 25}
SK = {"class": 5, "method": 6, "property": 7, "constructor": 9, "enum": 10, "interface": 11,
      "function": 12, "var": 13, "param": 13, "const": 14, "case": 22, "trait": 5}


def log(msg):
    sys.stderr.write(f"barq lsp: {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# positions: Barq columns count characters from 1, LSP counts UTF-16 units from 0
# ---------------------------------------------------------------------------

def to_index(text, u16):
    """LSP character -> index into this line."""
    if text.isascii():
        return max(0, min(u16, len(text)))
    n = 0
    for k, ch in enumerate(text):
        if n >= u16:
            return k
        n += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def to_u16(text, k):
    k = max(0, min(k, len(text)))
    return k if text.isascii() else len(text[:k].encode("utf-16-le")) // 2


def uri_to_path(uri):
    from urllib.parse import unquote, urlparse
    if not uri.startswith("file:"):
        return uri
    p = unquote(urlparse(uri).path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", p):
        p = p[1:]
    return os.path.normpath(p)


def path_to_uri(path):
    from urllib.parse import quote
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + quote(p)


# ---------------------------------------------------------------------------
# what a file declares
# ---------------------------------------------------------------------------

class Sym:
    """A declaration: class, function, method, property, constant, case, parameter or variable."""

    def __init__(self, name, kind, path, line, col, **kw):
        self.name = name
        self.kind = kind
        self.path = path
        self.line = line          # 1-based
        self.col = col            # 1-based, at the name
        self.end = line           # last line of a class or function body
        self.type = kw.get("type")            # TypeRef of a variable, parameter or property
        self.params = kw.get("params") or []  # N.Param list of a function
        self.ret = kw.get("ret")              # TypeRef returned by a function
        self.owner = kw.get("owner")          # ClassInfo of a member
        self.static = kw.get("static", False)
        self.visibility = kw.get("visibility") or "public"
        self.doc = kw.get("doc") or ""
        self.value = kw.get("value")          # constant / case / default value node


class ClassInfo:
    def __init__(self, decl, fqn, sym, info):
        self.name = decl.name
        self.fqn = fqn
        self.kind = "enum" if decl.is_enum else (decl.kind or "class")
        self.parent = decl.parent
        self.interfaces = list(decl.interfaces or [])
        self.traits = []
        self.backing = decl.backing
        self.abstract = bool(decl.is_abstract)
        self.members = {}
        self.sym = sym
        self.info = info


class Scope:
    def __init__(self, start, end, cls, func):
        self.start = start
        self.end = end
        self.cls = cls      # ClassInfo or None
        self.func = func    # the named function or method this scope is in (None at the top level)
        self.syms = []


class FileInfo:
    def __init__(self, path, text):
        self.path = path
        self.text = text
        self.lines = text.split("\n")
        self.namespace = None
        self.uses = {}        # alias -> (fqn, line)
        self.classes = {}     # short name -> ClassInfo
        self.functions = {}   # name -> Sym
        self.globals = {}     # top-level constants and variables -> Sym
        self.scopes = []
        self.ok = False

    def fqn(self, name):
        return f"{self.namespace}\\{name}" if self.namespace else name


def walk(node):
    """Every node below `node` (inclusive)."""
    todo = [node]
    while todo:
        n = todo.pop()
        if isinstance(n, N.Node):
            yield n
            for f in n._fields:
                v = getattr(n, f, None)
                if isinstance(v, (N.Node, list, tuple)):
                    todo.append(v)
        elif isinstance(n, (list, tuple)):
            todo.extend(x for x in n if isinstance(x, (N.Node, list, tuple)))


def find_name(lines, line, name, before=None, col=1, span=12):
    """(line, col) of `name` at or after line:col — after a keyword such as `karo` when given."""
    pat = re.compile((rf"\b(?:{before})\s+" if before else r"(?<![\w.])") + rf"({re.escape(name)})\b")
    for ln in range(max(line, 1), min(line + span, len(lines)) + 1):
        start = col - 1 if ln == line else 0
        m = pat.search(lines[ln - 1], start)
        if m:
            return ln, m.start(1) + 1
    return line, col


def doc_comment(lines, line):
    """The `//` lines right above a declaration (and above its attributes)."""
    k = line - 2
    while k >= 0 and lines[k].strip().startswith("#["):
        k -= 1
    out = []
    while k >= 0 and lines[k].strip().startswith("//"):
        out.append(lines[k].strip()[2:].strip())
        k -= 1
    return "\n".join(reversed(out))


def indent_of(s):
    return len(s) - len(s.lstrip(" \t"))


def block_end(lines, line):
    """Last line of the indented block whose header is on `line`."""
    if line < 1 or line > len(lines):
        return line
    base = indent_of(lines[line - 1])
    end = line
    for k in range(line, len(lines)):
        s = lines[k]
        if not s.strip():
            continue
        if indent_of(s) <= base:
            break
        end = k + 1
    return end


def analyze(text, path):
    """Parse a file and list what it declares. A file with a syntax error gives an
    empty FileInfo with ok=False."""
    from .parser import parse
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    info = FileInfo(path, text)
    program = parse_repaired(text, path)
    if program is None:
        return info
    info.ok = True
    lines = info.lines
    top = Scope(1, len(lines), None, None)
    info.scopes.append(top)
    for st in program.body:
        if isinstance(st, N.Namespace):
            info.namespace = st.name
        elif isinstance(st, N.Use):
            for fqn, alias in st.imports:
                info.uses[alias or fqn.split("\\")[-1]] = (fqn, st.line)
        elif isinstance(st, N.ClassDecl):
            add_class(info, st)
        elif isinstance(st, N.FuncDecl):
            sym = func_sym(info, st, None)
            info.functions[st.name] = sym
            add_function_scope(info, st, sym, None)
        elif isinstance(st, N.ConstDecl):
            ln, col = find_name(lines, st.line, st.name, "const")
            info.globals[st.name] = Sym(st.name, "const", path, ln, col, value=st.value,
                                         doc=doc_comment(lines, st.line))
        elif isinstance(st, N.VarDecl):
            ln, col = find_name(lines, st.line, st.name, col=st.col)
            info.globals[st.name] = Sym(st.name, "var", path, ln, col, type=st.type, doc=doc_comment(lines, st.line))
        if not isinstance(st, (N.ClassDecl, N.FuncDecl)):
            collect_locals(info, st, top, None, None)
    return info


def parse_repaired(text, path, tries=8):
    """Parse a file; when a line has a syntax error (usually the one being typed),
    put a placeholder in its place — `null`, then an empty line — and try again,
    so the rest of the file can still be read. Line numbers stay the same."""
    from .parser import parse
    lines = text.split("\n")
    last = None
    for _ in range(tries):
        try:
            return parse(SourceFile(path, "\n".join(lines)))
        except BarqError as e:
            ln = e.line or 0
            if not 1 <= ln <= len(lines):
                return None
            s = lines[ln - 1]
            if last == ln or not s.strip() or s.strip() == "null":
                if not s.strip():
                    return None
                lines[ln - 1] = ""
            else:
                lines[ln - 1] = s[:indent_of(s)] + "null"
            last = ln
        except RecursionError:
            return None
    return None


def func_sym(info, d, owner):
    kind = "function" if owner is None else ("constructor" if d.name == "__construct" else "method")
    ln, col = find_name(info.lines, d.line, d.name, "karo")
    sym = Sym(d.name, kind, info.path, ln, col, params=d.params, ret=d.ret, owner=owner,
              static=d.is_static, visibility=d.visibility, doc=doc_comment(info.lines, d.line))
    sym.end = block_end(info.lines, ln)
    return sym


def add_function_scope(info, d, sym, cls):
    scope = Scope(sym.line, sym.end, cls, sym)
    for p in d.params or []:
        ln, col = find_name(info.lines, p.line, p.name, col=p.col, span=1)
        scope.syms.append(Sym(p.name, "param", info.path, ln, col, type=p.type))
    info.scopes.append(scope)
    for st in d.body or []:
        collect_locals(info, st, scope, cls, sym)


def collect_locals(info, node, scope, cls, func):
    lines = info.lines
    todo = [node]
    while todo:
        n = todo.pop()
        if isinstance(n, (list, tuple)):
            todo.extend(x for x in n if isinstance(x, (N.Node, list, tuple)))
            continue
        if not isinstance(n, N.Node):
            continue
        if isinstance(n, N.VarDecl):
            ln, col = find_name(lines, n.line, n.name, col=n.col, span=1)
            scope.syms.append(Sym(n.name, "var", info.path, ln, col, type=n.type))
        elif isinstance(n, N.For):
            for nm in (n.key, n.var):
                if nm:
                    ln, col = find_name(lines, n.line, nm, col=n.col, span=1)
                    scope.syms.append(Sym(nm, "var", info.path, ln, col))
        elif isinstance(n, N.Try):
            for h in n.handlers:
                types, name = h[0], h[1]
                if name:
                    at = h[3][0] if len(h) > 3 else n.line
                    ln, col = find_name(lines, at, name, "as", span=1)
                    t = N.TypeRef(types[0], [], None) if len(types) == 1 else None
                    scope.syms.append(Sym(name, "var", info.path, ln, col, type=t))
        elif isinstance(n, N.Lambda):
            end = block_end(lines, n.line) if not n.is_expr else n.line
            inner = Scope(n.line, max(end, n.line), cls, func)
            for p in n.params or []:
                ln, col = find_name(lines, p.line, p.name, col=p.col, span=1)
                inner.syms.append(Sym(p.name, "param", info.path, ln, col, type=p.type))
            info.scopes.append(inner)
            collect_locals(info, n.body, inner, cls, func)
            continue
        for f in n._fields:
            v = getattr(n, f, None)
            if isinstance(v, (N.Node, list, tuple)):
                todo.append(v)


def add_class(info, d):
    lines = info.lines
    ln, col = find_name(lines, d.line, d.name, "class|interface|trait|enum")
    kind = "enum" if d.is_enum else (d.kind or "class")
    sym = Sym(d.name, kind, info.path, ln, col, doc=doc_comment(lines, d.line))
    sym.end = block_end(lines, ln)
    ci = ClassInfo(d, info.fqn(d.name), sym, info)
    sym.owner = ci
    info.classes[d.name] = ci
    for c in d.cases or []:
        cl, cc = find_name(lines, c.line, c.name, "case")
        ci.members[c.name] = Sym(c.name, "case", info.path, cl, cc, owner=ci, static=True, value=c.value,
                                 doc=doc_comment(lines, c.line))
    for m in d.members or []:
        if isinstance(m, N.FuncDecl):
            s = func_sym(info, m, ci)
            ci.members[m.name] = s
            if m.body is not None:
                add_function_scope(info, m, s, ci)
        elif isinstance(m, N.Property):
            pl, pc = find_name(lines, m.line, m.name, col=m.col, span=1)
            ci.members[m.name] = Sym(m.name, "property", info.path, pl, pc, type=m.type, owner=ci,
                                     static=m.is_static, visibility=m.visibility, value=m.value,
                                     doc=doc_comment(lines, m.line))
        elif isinstance(m, N.ConstDecl):
            cl, cc = find_name(lines, m.line, m.name, "const")
            ci.members[m.name] = Sym(m.name, "const", info.path, cl, cc, owner=ci, static=True, value=m.value,
                                     doc=doc_comment(lines, m.line))
        elif isinstance(m, N.EnumCase):
            cl, cc = find_name(lines, m.line, m.name, "case")
            ci.members[m.name] = Sym(m.name, "case", info.path, cl, cc, owner=ci, static=True, value=m.value,
                                     doc=doc_comment(lines, m.line))
        elif isinstance(m, N.UseTrait):
            ci.traits.extend(m.names)


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

def const_text(e):
    if isinstance(e, N.Literal):
        v = e.value
        if v is None:
            return "null"
        if v is True or v is False:
            return "true" if v else "false"
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        return repr(v)
    if isinstance(e, N.Unary) and isinstance(e.operand, N.Literal):
        return f"{e.op}{const_text(e.operand)}"
    if isinstance(e, N.StaticRef) and isinstance(e.target, N.Name):
        return f"{e.target.id}::{e.name}"
    if isinstance(e, N.ListLit) and not e.items:
        return "#[]"
    if isinstance(e, N.DictLit) and not e.pairs:
        return "#{}"
    return "…"


def tstr(t):
    return type_str(t) if t is not None else "any"


def param_text(p):
    s = ("*" if p.variadic else "") + (tstr(p.type.args[0]) if p.variadic and p.type.args else tstr(p.type))
    s += f" {p.name}"
    if p.default is not None:
        s += f" = {const_text(p.default)}"
    return s


def signature(sym):
    """One line that shows a declaration the way it is written."""
    k = sym.kind
    if k in ("function", "method", "constructor"):
        head = []
        if sym.owner is not None:
            if sym.visibility != "public":
                head.append(sym.visibility)
            if sym.static:
                head.append("static")
        params = ", ".join(param_text(p) for p in sym.params)
        return " ".join(head + [f"karo {sym.name}({params}): {tstr(sym.ret)}"])
    if k in ("class", "interface", "trait", "enum"):
        ci = sym.owner
        s = f"{'abstract ' if k == 'class' and ci.abstract else ''}{k} {sym.name}"
        if ci is not None:
            if ci.parent:
                s += f" extends {ci.parent}"
            if ci.interfaces:
                s += (" extends " if k == "interface" else " implements ") + ", ".join(ci.interfaces)
        return s
    if k == "property":
        bits = [] if sym.visibility == "public" else [sym.visibility]
        if sym.static:
            bits.append("static")
        s = " ".join(bits + [tstr(sym.type), sym.name])
        return s + (f" = {const_text(sym.value)}" if sym.value is not None else "")
    if k == "const":
        return f"const {sym.name}" + (f" = {const_text(sym.value)}" if sym.value is not None else "")
    if k == "case":
        return f"case {sym.name}" + (f" = {const_text(sym.value)}" if sym.value is not None else "")
    return f"{tstr(sym.type) if sym.type is not None else 'any'} {sym.name}"


def native_signature(label, fn):
    names = getattr(fn, "arg_names", None)
    lo, hi = getattr(fn, "min_args", 0), getattr(fn, "max_args", 0)
    if names:
        parts = []
        for k, nm in enumerate(names):
            parts.append(nm if nm.startswith("*") or k < lo else nm + "?")
        return f"{label}({', '.join(parts)})", parts
    if hi is not None and hi < 0:
        args = f"{lo}+ arguments" if lo else "any number of arguments"
    elif lo == hi:
        args = "no arguments" if lo == 0 else f"{lo} argument{'s' if lo != 1 else ''}"
    else:
        args = f"{lo}–{hi} arguments"
    return f"{label}(…)  // {args}", []


def parse_type_text(s):
    """'?list<dict<string, any>>' -> TypeRef (best effort, for declarations read from raw text)."""
    s = s.strip()
    nullable = s.startswith("?")
    s = s.lstrip("?")
    m = re.match(r"([\w\\]+)\s*(?:<(.*)>)?$", s)
    if not m:
        return None
    args = []
    if m.group(2):
        depth, cur = 0, ""
        for ch in m.group(2):
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
            if ch == "," and depth == 0:
                args.append(cur)
                cur = ""
            else:
                cur += ch
        args.append(cur)
        args = [a for a in (parse_type_text(x) for x in args) if a is not None]
    return N.TypeRef(m.group(1), args, nullable or None)


DECL_RE = re.compile(r"^\s*(\??[A-Za-z_][\w\\]*(?:<[^=()]*>)?)\s+([A-Za-z_]\w*)\s*(?:=(?!=)|$)")
PARAM_RE = re.compile(r"(?:^|[(,])\s*(?:public\s+|private\s+|protected\s+)?\*?(\??[A-Za-z_][\w\\]*(?:<[^()]*?>)?)\s+([A-Za-z_]\w*)")


def match_back(s, i):
    """Index of the bracket that opens the one at s[i] (a closer), or None."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    k = i
    quote = None
    while k >= 0:
        ch = s[k]
        if quote:
            if ch == quote and (k == 0 or s[k - 1] != "\\"):
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in pairs:
            stack.append(pairs[ch])
        elif ch in "([{":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
            if not stack:
                return k - 1 if k > 0 and s[k - 1] == "#" else k
        k -= 1
    return None


def parse_chain(s):
    """The receiver written before a trailing `.`, `?.` or `::` — `this.db.table("x")` ->
    [("this", False, False, None), ("db", False, False, "."), ("table", True, False, ".")].
    Each item is (name, called, indexed, separator before it). None when it is not a plain chain."""
    segs = []
    i = len(s)
    while True:
        called = indexed = False
        while i > 0 and s[i - 1] in ")]":
            j = match_back(s, i - 1)
            if j is None:
                return None
            if s[i - 1] == ")":
                called = True
            else:
                indexed = True
            i = j
            if s[j] == "#":     # #[...] list literal
                segs.append(("#list", False, False, None))
                return finish_chain(s[:j], segs)
        j = i
        while j > 0 and (s[j - 1].isalnum() or s[j - 1] == "_"):
            j -= 1
        name = s[j:i]
        if not name:
            if i > 0 and s[i - 1] in "\"'" and not called:
                q = s[i - 1]
                k = s.rfind(q, 0, i - 1)
                if k < 0:
                    return None
                segs.append(("#string", False, False, None))
                return finish_chain(s[:k], segs)
            return None
        sep = None
        if s[max(j - 2, 0):j] == "?.":
            sep, i = ".", j - 2
        elif s[max(j - 2, 0):j] == "::":
            sep, i = "::", j - 2
        elif s[j - 1:j] == "." and not (j > 1 and s[j - 2] == "."):
            sep, i = ".", j - 1
        segs.append((name, called, indexed, sep))
        if sep is None:
            return finish_chain(s[:j], segs)


def finish_chain(before, segs):
    segs.reverse()
    out = []
    for k, (name, called, indexed, sep) in enumerate(segs):
        prev_sep = segs[k - 1][3] if k > 0 else None
        out.append((name, called, indexed, prev_sep))
    is_new = bool(re.search(r"\bnew\s+$", before))
    return out, is_new


def chain_at(line_text, k):
    """(receiver chain, is_new, separator, prefix) for the cursor at index k in a line."""
    j = k
    while j > 0 and (line_text[j - 1].isalnum() or line_text[j - 1] == "_"):
        j -= 1
    prefix = line_text[j:k]
    before = line_text[:j]
    if before.endswith("?."):
        sep, recv = ".", before[:-2]
    elif before.endswith("::"):
        sep, recv = "::", before[:-2]
    elif before.endswith(".") and not before.endswith(".."):
        sep, recv = ".", before[:-1]
    else:
        return None, False, None, prefix
    parsed = parse_chain(recv)
    if parsed is None:
        return None, False, sep, prefix
    chain, is_new = parsed
    return chain, is_new, sep, prefix


def word_at(text, k):
    """(start, end) of the identifier at or touching index k."""
    a = k
    while a > 0 and (text[a - 1].isalnum() or text[a - 1] == "_"):
        a -= 1
    b = k
    while b < len(text) and (text[b].isalnum() or text[b] == "_"):
        b += 1
    if a == b or text[a].isdigit():
        return None
    return a, b


def occurrences(text, path="<file>"):
    """Every identifier token as (line, col, name, previous operator) — including the
    code inside "{...}" in strings. Falls back to a regex scan when the file does not lex."""
    from .lexer import Lexer
    out = []

    def scan(src, dline, dcol):
        toks = Lexer(SourceFile(path, src)).tokenize()
        prev = None
        for t in toks:
            if t.type in ("NAME", "KEYWORD") and isinstance(t.value, str):
                out.append((t.line + dline, t.col + (dcol if t.line == 1 else 0), t.value, prev))
            elif t.type == "STRING" and isinstance(t.value, list):
                for part in t.value:
                    if isinstance(part, tuple):
                        src2, ln, col = part
                        try:
                            scan(src2, ln - 1, col - 1)
                        except BarqError:
                            pass
            prev = t.value if t.type == "OP" else None

    try:
        scan(text, 0, 0)
        return out
    except (BarqError, RecursionError):
        out = []
    in_block = False
    for ln, line in enumerate(text.split("\n"), 1):
        code = line
        if in_block:
            if "*/" not in code:
                continue
            code = " " * (code.index("*/") + 2) + code[code.index("*/") + 2:]
            in_block = False
        cut = code.find("//")
        if cut >= 0:
            code = code[:cut]
        for m in re.finditer(r"[A-Za-z_]\w*", code):
            before = code[:m.start()].rstrip()
            prev = "." if before.endswith(".") else ("::" if before.endswith("::") else None)
            out.append((ln, m.start() + 1, m.group(0), prev))
        if "/*" in code and "*/" not in code[code.index("/*"):]:
            in_block = True
    return out


# ---------------------------------------------------------------------------
# a class seen from the outside, whether written in Barq or built in
# ---------------------------------------------------------------------------

class Member:
    __slots__ = ("name", "kind", "static", "visibility", "sym", "native", "owner")

    def __init__(self, name, kind, static, visibility, sym=None, native=None, owner=None):
        self.name = name
        self.kind = kind          # method constructor property const case
        self.static = static
        self.visibility = visibility
        self.sym = sym            # Sym for Barq code
        self.native = native      # NativeFunction / PropInfo / value for built-ins
        self.owner = owner        # View


class View:
    """A class from the project (ClassInfo) or a built-in one (BarqClass)."""

    def __init__(self, server, ci=None, native=None):
        self.server = server
        self.ci = ci
        self.native = native

    @property
    def name(self):
        return self.ci.name if self.ci else self.native.name

    @property
    def key(self):
        return ("u", self.ci.fqn) if self.ci else ("n", id(self.native))

    def __eq__(self, other):
        return isinstance(other, View) and self.key == other.key

    def __hash__(self):
        return hash(self.key)

    def parent(self):
        if self.ci:
            if not self.ci.parent:
                return None
            return self.server.resolve_class(self.ci.parent, self.ci.info)
        p = self.native.parent
        return View(self.server, native=p) if p is not None else None

    def mixins(self):
        """Traits and interfaces whose members show up on this class."""
        out = []
        if self.ci:
            for nm in self.ci.traits + self.ci.interfaces:
                v = self.server.resolve_class(nm, self.ci.info)
                if v is not None:
                    out.append(v)
        else:
            out.extend(View(self.server, native=i) for i in getattr(self.native, "interfaces", []) or [])
        return out

    def own(self):
        if self.ci:
            for s in self.ci.members.values():
                yield Member(s.name, s.kind, s.static, s.visibility, sym=s, owner=self)
            return
        c = self.native
        for nm, case in getattr(c, "cases", {}).items():
            yield Member(nm, "case", True, "public", native=case, owner=self)
        for nm, fn in c.methods.items():
            kind = "constructor" if nm == "__construct" else "method"
            yield Member(nm, kind, getattr(fn, "is_static", False), getattr(fn, "visibility", "public"),
                         native=fn, owner=self)
        for nm, p in c.props.items():
            yield Member(nm, "property", False, p.visibility, native=p, owner=self)
        for nm, v in c.static_values.items():
            p = c.statics.get(nm)
            if isinstance(v, (NativeFunction, BarqFunction)):
                yield Member(nm, "method", True, "public", native=v, owner=self)
            elif p is not None:
                yield Member(nm, "const" if p.const else "property", True, p.visibility, native=p, owner=self)
            else:
                yield Member(nm, "const", True, "public", native=v, owner=self)
        for nm, p in c.statics.items():
            if nm not in c.static_values:
                yield Member(nm, "const" if p.const else "property", True, p.visibility, native=p, owner=self)

    def members(self):
        """Own members first, then inherited ones that are not overridden."""
        seen = {}
        todo = [self]
        visited = set()
        while todo:
            v = todo.pop(0)
            if v.key in visited:
                continue
            visited.add(v.key)
            for m in v.own():
                key = (m.name, m.static or m.kind in ("const", "case"))
                if key not in seen:
                    seen[key] = m
            p = v.parent()
            todo.extend(v.mixins())
            if p is not None:
                todo.append(p)
        return list(seen.values())

    def find(self, name, static=None):
        best = None
        for m in self.members():
            if m.name != name:
                continue
            if static is None or m.static == static or m.kind in ("const", "case"):
                return m
            best = best or m
        return best

    def is_a(self, other):
        v, seen = self, set()
        while v is not None and v.key not in seen:
            if v == other:
                return True
            seen.add(v.key)
            v = v.parent()
        return False

    def def_sym(self):
        return self.ci.sym if self.ci else None

    def fqn(self):
        return self.ci.fqn if self.ci else self.native.name


# ---------------------------------------------------------------------------
# the project's files
# ---------------------------------------------------------------------------

class Workspace:
    def __init__(self):
        self.roots = []
        self.files = {}       # path -> (mtime, FileInfo)
        self.scanned = 0.0
        self.dirty = True

    def add_root(self, path):
        path = os.path.abspath(path)
        if os.path.isdir(path) and path not in self.roots:
            self.roots.append(path)
            self.dirty = True

    def paths(self):
        count = 0
        for root in self.roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
                for name in sorted(filenames):
                    if name.endswith(".bq"):
                        count += 1
                        if count > MAX_FILES:
                            return
                        yield os.path.join(dirpath, name)

    def refresh(self, force=False):
        now = time.monotonic()
        if not force and not self.dirty and now - self.scanned < 2.0:
            return
        self.scanned = now
        self.dirty = False
        found = set()
        for p in self.paths():
            found.add(p)
            self.load(p)
        for p in list(self.files):
            if p not in found and not os.path.exists(p):
                del self.files[p]

    def load(self, p):
        try:
            mtime = os.stat(p).st_mtime
        except OSError:
            self.files.pop(p, None)
            return None
        hit = self.files.get(p)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            return None
        info = analyze(text, p)
        self.files[p] = (mtime, info)
        return info


class Doc:
    def __init__(self, uri, text, version):
        self.uri = uri
        self.path = uri_to_path(uri)
        self.version = version
        self.good = None          # last FileInfo that parsed
        self.set(text)

    def set(self, text):
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = self.text.split("\n")
        self.info = analyze(self.text, self.path)
        if self.info.ok:
            self.good = self.info

    @property
    def best(self):
        return self.info if self.info.ok else (self.good or self.info)


class Ctx:
    """Where the cursor is: file, enclosing class and function, visible variables."""

    def __init__(self, info, cls, func, locals_):
        self.info = info
        self.cls = cls        # View or None
        self.func = func      # Sym or None
        self.locals = locals_  # name -> Sym


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, out):
        self.out = out
        self.lock = threading.Lock()
        self.docs = {}
        self.ws = Workspace()
        self.pending = {}         # uri -> time to check
        self.shutdown = False
        self.diagnostics = True
        self.sort_imports = True
        self.on_type = True       # check while typing, not only on open and save
        self._interp = None
        self._builtins = None

    # -- transport -----------------------------------------------------------

    def send(self, msg):
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        with self.lock:
            self.out.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
            self.out.flush()

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def handle(self, msg):
        method = msg.get("method")
        mid = msg.get("id")
        if method is None:
            return      # a response to something we never ask
        params = msg.get("params") or {}
        fn = getattr(self, "on_" + method.replace("/", "_").replace("$", "S"), None)
        if fn is None:
            if mid is not None:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})
            return
        try:
            result = fn(params)
        except LspError as e:
            if mid is not None:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32803, "message": str(e)}})
            return
        except Exception as e:  # never let one request take the server down
            import traceback
            log(f"{method} failed: {e}\n{traceback.format_exc()}")
            if mid is not None:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})
            return
        if mid is not None:
            self.send({"jsonrpc": "2.0", "id": mid, "result": result})

    # -- builtins --------------------------------------------------------------

    def interp(self, root=None):
        from .interpreter import Interpreter
        if self._interp is None:
            self._interp = Interpreter(root=root or os.getcwd(), argv=[])
        return self._interp

    def builtins(self):
        """name -> value of every global the language provides (functions, modules, classes)."""
        if self._builtins is None:
            self._builtins = {k: v[0] for k, v in self.interp().builtins.vars.items()}
        return self._builtins

    # -- lifecycle -----------------------------------------------------------

    def on_initialize(self, p):
        for f in p.get("workspaceFolders") or []:
            self.ws.add_root(uri_to_path(f.get("uri", "")))
        if not self.ws.roots and p.get("rootUri"):
            self.ws.add_root(uri_to_path(p["rootUri"]))
        elif not self.ws.roots and p.get("rootPath"):
            self.ws.add_root(p["rootPath"])
        self.apply_settings(p.get("initializationOptions") or {})
        from . import __version__
        return {
            "capabilities": {
                "textDocumentSync": {"openClose": True, "change": 1, "save": {"includeText": False}},
                "completionProvider": {"triggerCharacters": [".", ":", "\\", "["], "resolveProvider": False},
                "hoverProvider": True,
                "definitionProvider": True,
                "referencesProvider": True,
                "documentHighlightProvider": True,
                "documentSymbolProvider": True,
                "workspaceSymbolProvider": True,
                "documentFormattingProvider": True,
                "renameProvider": {"prepareProvider": True},
                "signatureHelpProvider": {"triggerCharacters": ["(", ","], "retriggerCharacters": [","]},
            },
            "serverInfo": {"name": "barq", "version": __version__},
        }

    def apply_settings(self, s):
        if not isinstance(s, dict):
            return
        if "diagnostics" in s:
            self.diagnostics = bool(s["diagnostics"])
        if "sortImports" in s:
            self.sort_imports = bool(s["sortImports"])
        if "diagnosticsOnType" in s:
            self.on_type = bool(s["diagnosticsOnType"])

    def on_initialized(self, p):
        return None

    def on_shutdown(self, p):
        self.shutdown = True
        return None

    def on_exit(self, p):
        os._exit(0 if self.shutdown else 1)

    def on_SScancelRequest(self, p):
        return None

    def on_workspace_didChangeConfiguration(self, p):
        settings = (p.get("settings") or {})
        self.apply_settings(settings.get("barq", settings))
        for uri in self.docs:
            self.pending[uri] = 0
        if not self.diagnostics:
            for uri in self.docs:
                self.notify("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": []})

    def on_workspace_didChangeWatchedFiles(self, p):
        self.ws.dirty = True

    def on_workspace_didChangeWorkspaceFolders(self, p):
        ev = p.get("event") or {}
        for f in ev.get("added") or []:
            self.ws.add_root(uri_to_path(f["uri"]))
        for f in ev.get("removed") or []:
            path = os.path.abspath(uri_to_path(f["uri"]))
            if path in self.ws.roots:
                self.ws.roots.remove(path)
                self.ws.dirty = True

    # -- documents -----------------------------------------------------------

    def on_textDocument_didOpen(self, p):
        td = p["textDocument"]
        doc = Doc(td["uri"], td.get("text", ""), td.get("version"))
        self.docs[doc.uri] = doc
        if not self.ws.roots and doc.path and os.path.isabs(doc.path):
            self.ws.add_root(self.project_dir(doc.path))
        self.pending[doc.uri] = 0

    def on_textDocument_didChange(self, p):
        doc = self.docs.get(p["textDocument"]["uri"])
        if doc is None:
            return
        changes = p.get("contentChanges") or []
        if changes:
            doc.set(changes[-1].get("text", ""))
            doc.version = p["textDocument"].get("version")
            if self.on_type:
                self.pending[doc.uri] = time.monotonic() + DEBOUNCE

    def on_textDocument_didSave(self, p):
        self.ws.dirty = True
        for uri in self.docs:      # files that import this one may change too
            self.pending[uri] = 0

    def on_textDocument_didClose(self, p):
        uri = p["textDocument"]["uri"]
        self.docs.pop(uri, None)
        self.pending.pop(uri, None)
        self.notify("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": []})

    def project_dir(self, path):
        d = os.path.dirname(path)
        while True:
            for marker in (".env", "routes", "app", "main.bq", "barq.json"):
                if os.path.exists(os.path.join(d, marker)):
                    return d
            up = os.path.dirname(d)
            if up == d:
                return os.path.dirname(path)
            d = up

    def run_checks(self):
        now = time.monotonic()
        due = [u for u, t in self.pending.items() if t <= now]
        for uri in due:
            self.pending.pop(uri, None)
            doc = self.docs.get(uri)
            if doc is not None:
                self.publish(doc)
        return min(self.pending.values()) - now if self.pending else None

    def publish(self, doc):
        from .check import check_text, word_end
        items = []
        if self.diagnostics:
            for e in check_text(doc.text, doc.path):
                ln = min(max(e.line or 1, 1), len(doc.lines))
                col = max(e.col or 1, 1)
                end = word_end(e.source if e.source is not None else SourceFile(doc.path, doc.text), ln, col)
                text = doc.lines[ln - 1]
                items.append({
                    "range": {"start": {"line": ln - 1, "character": to_u16(text, col - 1)},
                              "end": {"line": ln - 1, "character": to_u16(text, end - 1)}},
                    "severity": 1, "source": "barq", "code": e.kind, "message": e.message,
                })
        self.notify("textDocument/publishDiagnostics", {"uri": doc.uri, "version": doc.version, "diagnostics": items})

    # -- project lookup --------------------------------------------------------

    def infos(self):
        """Every FileInfo in the project; open editors win over the files on disk."""
        self.ws.refresh()
        open_ = {}
        for d in self.docs.values():
            open_[os.path.abspath(d.path)] = d.best
        for p, (_, info) in list(self.ws.files.items()):
            if p not in open_:
                yield info
        yield from open_.values()

    def info_for(self, path):
        path = os.path.abspath(path)
        for d in self.docs.values():
            if os.path.abspath(d.path) == path:
                return d.best
        return self.ws.load(path)

    def root_for(self, info):
        from .check import project_root
        return project_root(info.text, info.path)

    def file_for_fqn(self, fqn, info):
        parts = [x for x in fqn.split("\\") if x]
        if not parts or parts[0] == "Barq":
            return None
        roots = [self.root_for(info)] + [r for r in self.ws.roots]
        for root in roots:
            for c in module_candidates(parts):
                p = os.path.join(root, c)
                if os.path.isfile(p):
                    return p
        from .pkg import find_file
        for root in roots:
            p = find_file(root, parts)
            if p:
                return p
        return None

    def class_by_fqn(self, fqn, info):
        fqn = fqn.lstrip("\\")
        p = self.file_for_fqn(fqn, info)
        if p:
            other = self.info_for(p)
            if other is not None:
                ci = other.classes.get(fqn.split("\\")[-1])
                if ci is not None:
                    return ci
        for other in self.infos():
            for ci in other.classes.values():
                if ci.fqn == fqn:
                    return ci
        return None

    def resolve_class(self, name, info):
        """A class name as written in `info` -> View, or None."""
        if not name:
            return None
        name = name.lstrip("\\") if name.startswith("\\") else name
        if "\\" in name:
            first, _, rest = name.partition("\\")
            if first in info.uses:
                name = info.uses[first][0] + "\\" + rest
            ci = self.class_by_fqn(name, info)
            if ci:
                return View(self, ci=ci)
            short = name.split("\\")[-1]
            b = self.builtins().get(short)
            return View(self, native=b) if isinstance(b, BarqClass) else None
        if name in info.uses:
            fqn = info.uses[name][0]
            ci = self.class_by_fqn(fqn, info)
            if ci:
                return View(self, ci=ci)
            b = self.builtins().get(fqn.split("\\")[-1])
            if isinstance(b, BarqClass):
                return View(self, native=b)
        if name in info.classes:
            return View(self, ci=info.classes[name])
        if info.namespace:
            ci = self.class_by_fqn(info.fqn(name), info)
            if ci:
                return View(self, ci=ci)
        b = self.builtins().get(name)
        if isinstance(b, BarqClass):
            return View(self, native=b)
        for other in self.infos():      # last resort: any project class with that name
            if name in other.classes:
                return View(self, ci=other.classes[name])
        return None

    # -- cursor context --------------------------------------------------------

    def context(self, doc, line, k=None):
        """line is 1-based; k is the cursor's index in that line."""
        info = doc.best
        cls = func = None
        # The enclosing class and function come from the text as it is now: the
        # parsed version may be older while the file has a syntax error.
        cls_name, func_name, func_line = enclosing(doc.lines, line, k)
        if cls_name and cls_name in info.classes:
            cls = View(self, ci=info.classes[cls_name])
        if func_name:
            func = cls.ci.members.get(func_name) if cls is not None else info.functions.get(func_name)
            if func is not None and func.kind not in ("function", "method", "constructor"):
                func = None
        lines = doc.lines
        locals_ = {}

        def offer(sym):
            old = locals_.get(sym.name)
            # nearest declaration above the cursor wins
            if old is None or (old.line > line >= sym.line) or (sym.line <= line and sym.line > old.line):
                locals_[sym.name] = sym

        for sc in info.scopes:
            same = (sc.func is func) if func is not None else (sc.func is None and sc.cls is None)
            if same:
                for s in sc.syms:
                    offer(s)
        if func is None:
            for s in info.globals.values():
                offer(s)
        # declarations typed since the last successful parse
        start = func_line or 1
        for ln in range(start, min(line, len(lines)) + 1):
            s = lines[ln - 1]
            if ln == func_line:
                o = s.find("(")
                for m in PARAM_RE.finditer(s, o if o >= 0 else len(s)):
                    if m.group(2) not in locals_:
                        offer(Sym(m.group(2), "param", doc.path, ln, m.start(2) + 1, type=parse_type_text(m.group(1))))
                continue
            m = DECL_RE.match(s)
            if m and m.group(1) not in KEYWORDS and m.group(2) not in locals_ and ln != line:
                if cls is not None and func is None:
                    continue    # a property, not a variable
                offer(Sym(m.group(2), "var", doc.path, ln, m.start(2) + 1, type=parse_type_text(m.group(1))))
        return Ctx(info, cls, func, locals_)

    # -- types -----------------------------------------------------------------

    def type_of_ref(self, t, info, self_view=None):
        """TypeRef -> ('inst', View) | ('prim', name, TypeRef) | None"""
        if t is None:
            return None
        if t.name in PRIMITIVE:
            return ("prim", t.name, t)
        if t.name in ("any", "void", "karo", "null"):
            return None
        if t.name in ("static", "self") and self_view is not None:
            return ("inst", self_view)
        v = self.resolve_class(t.name, info)
        return ("inst", v) if v is not None else None

    def member_type(self, m, called, base):
        if m is None:
            return None
        if m.sym is not None:
            s = m.sym
            info = s.owner.info if s.owner is not None else base.ci.info
            if s.kind in ("method", "constructor"):
                return self.type_of_ref(s.ret, info, base) if called else None
            if s.kind == "case":
                return ("inst", m.owner)
            return self.type_of_ref(s.type, info, base)
        n = m.native
        if m.kind == "case":
            return ("inst", m.owner)
        if isinstance(n, BarqFunction) and called:
            return self.type_of_ref(n.ret, base.ci.info if base.ci else FileInfo("", ""), base)
        if isinstance(n, NativeFunction) and called and m.static and m.name in RETURNS_SELF:
            return ("inst", base)
        return None

    def first_type(self, ctx, name, called, indexed, is_new):
        """Type of the first part of a chain."""
        if name == "#string":
            return ("prim", "string", N.TypeRef("string", [], None))
        if name == "#list":
            return ("prim", "list", N.TypeRef("list", [], None))
        if name == "this":
            return ("inst", ctx.cls) if ctx.cls else None
        if name in ("self", "static"):
            return ("cls", ctx.cls) if ctx.cls else None
        if name == "parent":
            p = ctx.cls.parent() if ctx.cls else None
            return ("cls", p) if p else None
        sym = ctx.locals.get(name)
        if sym is not None and not called:
            t = self.type_of_ref(sym.type, ctx.info)
            return self.index_type(t, ctx.info) if indexed else t
        if called and name in ctx.info.functions:
            return self.type_of_ref(ctx.info.functions[name].ret, ctx.info)
        v = self.resolve_class(name, ctx.info) if name[:1].isupper() else None
        if v is not None:
            return ("inst", v) if is_new or called else ("cls", v)
        b = self.builtins().get(name)
        if isinstance(b, NativeModule):
            return ("mod", b)
        if called and name == "view":
            r = self.builtins().get("Response")
            return ("inst", View(self, native=r)) if isinstance(r, BarqClass) else None
        return None

    def index_type(self, t, info):
        if t and t[0] == "prim" and t[2] is not None and t[2].args:
            if t[1] == "list":
                return self.type_of_ref(t[2].args[0], info)
            if t[1] == "dict" and len(t[2].args) > 1:
                return self.type_of_ref(t[2].args[1], info)
        return None

    def chain_type(self, ctx, chain, is_new):
        t = None
        for k, (name, called, indexed, sep) in enumerate(chain):
            if k == 0:
                t = self.first_type(ctx, name, called, indexed, is_new and len(chain) == 1)
            else:
                t = self.step(t, name, called, sep, ctx)
                if indexed:
                    t = self.index_type(t, ctx.info)
            if t is None:
                return None
        return t

    def step(self, t, name, called, sep, ctx):
        if t is None:
            return None
        if t[0] in ("inst", "cls"):
            m = t[1].find(name, static=(t[0] == "cls"))
            return self.member_type(m, called, t[1])
        if t[0] == "mod":
            v = t[1].members.get(name)
            if isinstance(v, BarqClass):
                return ("cls", View(self, native=v))
            return None
        return None

    # -- what is under the cursor ----------------------------------------------

    def target(self, doc, pos):
        """-> (kind, thing, (line, start, end)) for the word at an LSP position.
        kind: 'sym' (Sym), 'member' (Member), 'native' (label, value), 'class' (View), 'use' (path)."""
        line = pos["line"]
        if line >= len(doc.lines):
            return None
        text = doc.lines[line]
        k = to_index(text, pos["character"])
        w = word_at(text, k)
        if w is None:
            return None
        a, b = w
        name = text[a:b]
        ctx = self.context(doc, line + 1, a)
        span = (line, a, b)
        # `use App\Models\User` lines
        um = re.match(r"\s*use\s+([\w\\]+)", text)
        if um and indent_of(text) == 0 and um.start(1) <= a < um.end(1):
            fqn = text[um.start(1):b]
            ci = self.class_by_fqn(fqn, ctx.info)
            if ci is not None:
                return ("class", View(self, ci=ci), span)
            p = self.file_for_fqn(fqn, ctx.info)
            if p:
                return ("use", p, span)
            b_ = self.builtins().get(name)
            if b_ is not None:
                return ("native", (name, b_), span)
            return None
        chain, is_new, sep, _ = chain_at(text, a)
        called = text[b:].lstrip().startswith("(")
        if chain:
            t = self.chain_type(ctx, chain, is_new)
            if t is None:
                return None
            if t[0] in ("inst", "cls"):
                m = t[1].find(name, static=(t[0] == "cls"))
                return ("member", m, span) if m else None
            if t[0] == "mod":
                v = t[1].members.get(name)
                if isinstance(v, BarqClass):
                    return ("class", View(self, native=v), span)
                return ("native", (f"{t[1].name}.{name}", v), span) if v is not None else None
            if t[0] == "prim":
                return self.prim_member(t[1], name, span)
            return None
        before = text[:a]
        # declarations: `karo name`, a property line in a class body
        if re.search(r"\bkaro\s+$", before):
            if ctx.cls is not None and name in ctx.cls.ci.members:
                return ("member", Member(name, "method", False, "public", sym=ctx.cls.ci.members[name], owner=ctx.cls), span)
            if name in ctx.info.functions:
                return ("sym", ctx.info.functions[name], span)
        if re.search(r"\b(class|interface|trait|enum)\s+$", before) and name in ctx.info.classes:
            return ("class", View(self, ci=ctx.info.classes[name]), span)
        if ctx.cls is not None and ctx.func is None and name in ctx.cls.ci.members:
            s = ctx.cls.ci.members[name]
            if s.line == line + 1:
                return ("member", Member(name, s.kind, s.static, s.visibility, sym=s, owner=ctx.cls), span)
        if name == "this" and ctx.cls is not None:
            return ("class", ctx.cls, span)
        sym = ctx.locals.get(name)
        if sym is not None and not called:
            return ("sym", sym, span)
        if name in ctx.info.functions:
            return ("sym", ctx.info.functions[name], span)
        if name in ctx.info.globals:
            return ("sym", ctx.info.globals[name], span)
        if name[:1].isupper() or name in ctx.info.uses:
            v = self.resolve_class(name, ctx.info)
            if v is not None:
                if is_new or re.search(r"\bnew\s+$", before):
                    ctor = v.find("__construct")
                    if ctor is not None and ctor.sym is not None and called:
                        return ("class", v, span)
                return ("class", v, span)
        bv = self.builtins().get(name)
        if bv is not None:
            return ("native", (name, bv), span)
        for other in self.infos():      # a function from another file in the project
            if name in other.functions and called:
                return ("sym", other.functions[name], span)
        return None

    def prim_member(self, kind, name, span):
        from . import builtins as B
        table = {"string": B.STRING_METHODS, "list": B.LIST_METHODS, "dict": B.DICT_METHODS,
                 "set": B.SET_METHODS}.get(kind)
        if table and name in table:
            spec = table[name]
            fake = NativeFunction(f"{kind}.{name}", None, spec[1], spec[2])
            return ("native", (f"{kind}.{name}", fake), span)
        return None

    # -- hover -----------------------------------------------------------------

    def on_textDocument_hover(self, p):
        doc = self.docs.get(p["textDocument"]["uri"])
        if doc is None:
            return None
        t = self.target(doc, p["position"])
        if t is None:
            return None
        kind, thing, (line, a, b) = t
        md = self.describe(kind, thing)
        if not md:
            return None
        text = doc.lines[line]
        return {"contents": {"kind": "markdown", "value": md},
                "range": {"start": {"line": line, "character": to_u16(text, a)},
                          "end": {"line": line, "character": to_u16(text, b)}}}

    def describe(self, kind, thing):
        code = lambda s: f"```barq\n{s}\n```"
        if kind == "sym":
            s = thing
            out = [code(signature(s))]
            if s.kind == "param":
                out.append("*parameter*")
            elif s.kind == "var":
                out.append("*variable*")
            if s.doc:
                out.append(s.doc)
            return "\n\n".join(out)
        if kind == "member":
            m = thing
            where = f"*{m.owner.fqn()}*" if m.owner is not None else ""
            if m.sym is not None:
                out = [code(signature(m.sym))]
                if m.sym.doc:
                    out.append(m.sym.doc)
                out.append(where)
                return "\n\n".join(x for x in out if x)
            n = m.native
            label = f"{m.owner.name}{'.' if m.kind == 'method' else '::'}{m.name}"
            if isinstance(n, (NativeFunction,)):
                return code(native_signature(label, n)[0]) + "\n\n*built-in*"
            if isinstance(n, BarqFunction):
                s = Sym(m.name, "method", "", n.line, 1, params=n.params, ret=n.ret, static=n.is_static)
                return code(signature(s)) + "\n\n*built-in*"
            if m.kind == "property" and hasattr(n, "type"):
                return code(f"{tstr(n.type)} {m.name}") + "\n\n*built-in*"
            return code(label) + "\n\n*built-in*"
        if kind == "class":
            v = thing
            if v.ci is not None:
                out = [code(signature(v.ci.sym))]
                if v.ci.sym.doc:
                    out.append(v.ci.sym.doc)
                out.append(f"*{v.ci.fqn}*")
                return "\n\n".join(out)
            c = v.native
            head = f"{c.kind} {c.name}" + (f" extends {c.parent.name}" if c.parent is not None else "")
            names = sorted(set(list(c.methods) + [k for k, x in c.static_values.items()
                                                  if isinstance(x, (NativeFunction, BarqFunction))]))
            names = [x for x in names if not x.startswith("__")]
            extra = ("\n\nMethods: " + ", ".join(f"`{x}`" for x in names[:40]) + (" …" if len(names) > 40 else "")) if names else ""
            return code(head) + "\n\n*built-in*" + extra
        if kind == "native":
            label, v = thing
            if isinstance(v, NativeFunction):
                return code(native_signature(label, v)[0]) + "\n\n*built-in function*"
            if isinstance(v, NativeModule):
                fns = sorted(k for k, x in v.members.items() if isinstance(x, NativeFunction))
                consts = sorted(k for k, x in v.members.items() if not isinstance(x, NativeFunction))
                out = [code(f"module {v.name}"), "*built-in module* — also `use Barq\\" + v.name + "`"]
                if fns:
                    out.append("Functions: " + ", ".join(f"`{x}`" for x in fns[:50]) + (" …" if len(fns) > 50 else ""))
                if consts:
                    out.append("Constants: " + ", ".join(f"`{x}`" for x in consts))
                return "\n\n".join(out)
            if isinstance(v, BarqClass):
                return self.describe("class", View(self, native=v))
            return code(f"{label}") + "\n\n*built-in value*"
        if kind == "use":
            return f"`{os.path.basename(thing)}`"
        return None

    # -- definition ------------------------------------------------------------

    def location(self, sym):
        if sym is None or not sym.path or not os.path.isabs(sym.path):
            return None
        info = self.info_for(sym.path)
        lines = info.lines if info is not None else []
        text = lines[sym.line - 1] if 0 < sym.line <= len(lines) else ""
        c = to_u16(text, sym.col - 1)
        return {"uri": self.uri_for(sym.path),
                "range": {"start": {"line": sym.line - 1, "character": c},
                          "end": {"line": sym.line - 1, "character": c + len(sym.name)}}}

    def uri_for(self, path):
        ap = os.path.abspath(path)
        for d in self.docs.values():
            if os.path.abspath(d.path) == ap:
                return d.uri
        return path_to_uri(path)

    def def_sym(self, t):
        kind, thing, _ = t
        if kind == "sym":
            return thing
        if kind == "member":
            return thing.sym
        if kind == "class":
            return thing.def_sym()
        return None

    def on_textDocument_definition(self, p):
        doc = self.docs.get(p["textDocument"]["uri"])
        if doc is None:
            return None
        t = self.target(doc, p["position"])
        if t is None:
            return None
        if t[0] == "use":
            return {"uri": path_to_uri(t[1]), "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}}
        return self.location(self.def_sym(t))

    # -- references / highlights / rename ----------------------------------------

    def refs(self, doc, pos, include_decl=True):
        t = self.target(doc, pos)
        if t is None:
            return None, []
        sym = self.def_sym(t)
        if sym is None:
            return t, []
        name = sym.name
        out = []
        if sym.kind in ("var", "param"):
            info = self.info_for(sym.path)
            scope = None
            for sc in (info.scopes if info else []):
                if any(s is sym for s in sc.syms):
                    scope = sc
            if scope is None:
                # declared in text not parsed yet: stay in the enclosing function
                ctx = self.context(doc, pos["line"] + 1, to_index(doc.lines[pos["line"]], pos["character"]))
                start = ctx.func.line if ctx.func else 1
                end = ctx.func.end if ctx.func else len(doc.lines)
            else:
                start, end = scope.start, scope.end
                if scope.func is not None:
                    start, end = min(start, scope.func.line), max(end, scope.func.end)
            # a top-level variable is not seen inside functions and classes
            holes = []
            if scope is not None and scope.func is None and scope.cls is None and info is not None:
                holes = [(s.line, s.end) for s in info.functions.values()]
                holes += [(c.sym.line, c.sym.end) for c in info.classes.values()]
            text = self.text_of(sym.path, doc)
            for ln, col, nm, prev in occurrences(text, sym.path):
                if nm == name and prev not in (".", "?.", "::") and start <= ln <= end \
                        and not any(a <= ln <= b for a, b in holes):
                    out.append((sym.path, ln, col))
        elif sym.kind in ("method", "constructor", "property", "const", "case") and sym.owner is not None:
            for info in self.infos():
                for ln, col, nm, prev in occurrences(info.text, info.path):
                    if nm == name and (prev in (".", "?.", "::") or (ln == sym.line and col == sym.col and info.path == sym.path)):
                        out.append((info.path, ln, col))
        else:
            for info in self.infos():
                for ln, col, nm, prev in occurrences(info.text, info.path):
                    if nm == name and prev not in (".", "?.", "::"):
                        out.append((info.path, ln, col))
        decl = (os.path.abspath(sym.path), sym.line, sym.col)
        seen, res = set(), []
        for path, ln, col in out:
            key = (os.path.abspath(path), ln, col)
            if key in seen or (not include_decl and key == decl):
                continue
            seen.add(key)
            res.append((path, ln, col, len(name)))
        if include_decl and decl not in seen and os.path.isabs(sym.path):
            res.insert(0, (sym.path, sym.line, sym.col, len(name)))
        return t, res

    def text_of(self, path, doc=None):
        ap = os.path.abspath(path)
        for d in self.docs.values():
            if os.path.abspath(d.path) == ap:
                return d.text
        info = self.info_for(path)
        return info.text if info is not None else ""

    def loc(self, path, ln, col, length):
        lines = self.text_of(path).split("\n")
        text = lines[ln - 1] if 0 < ln <= len(lines) else ""
        a = to_u16(text, col - 1)
        b = to_u16(text, col - 1 + length)
        return {"uri": self.uri_for(path), "range": {"start": {"line": ln - 1, "character": a},
                                                     "end": {"line": ln - 1, "character": b}}}

    def on_textDocument_references(self, p):
        doc = self.docs.get(p["textDocument"]["uri"])
        if doc is None:
            return None
        inc = (p.get("context") or {}).get("includeDeclaration", True)
        _, res = self.refs(doc, p["position"], inc)
        return [self.loc(*r) for r in res]

    def on_textDocument_documentHighlight(self, p):
        doc = self.docs.get(p["textDocument"]["uri"])
        if doc is None:
            return None
        _, res = self.refs(doc, p["position"], True)
        here = os.path.abspath(doc.path)
        out = []
        for path, ln, col, length in res:
            if os.path.abspath(path) == here:
                out.append({"range": self.loc(path, ln, col, length)["range"], "kind": 1})
        return out

    def on_textDocument_prepareRename(self, p):
        doc = self.docs.get(p["textDocument"]["uri"])
        if doc is None:
            return None
        t = self.target(doc, p["position"])
        sym = self.def_sym(t) if t else None
        if sym is None or sym.kind not in ("var", "param"):
            raise LspError("Barq can rename local variables and parameters only (for now).")
        line, a, b = t[2]
        text = doc.lines[line]
        return {"range": {"start": {"line": line, "character": to_u16(text, a)},
                          "end": {"line": line, "character": to_u16(text, b)}}, "placeholder": text[a:b]}

    def on_textDocument_rename(self, p):
        from .lexer import KEYWORDS as KW, TYPE_NAMES
        doc = self.docs.get(p["textDocument"]["uri"])
        if doc is None:
            return None
        new = p.get("newName", "")
        if not re.fullmatch(r"[A-Za-z_]\w*", new) or new in KW or new in TYPE_NAMES:
            raise LspError(f"'{new}' is not a valid variable name")
        t, res = self.refs(doc, p["position"], True)
        sym = self.def_sym(t) if t else None
        if sym is None or sym.kind not in ("var", "param"):
            raise LspError("Barq can rename local variables and parameters only (for now).")
        changes = {}
        for path, ln, col, length in res:
            l = self.loc(path, ln, col, length)
            changes.setdefault(l["uri"], []).append({"range": l["range"], "newText": new})
        return {"changes": changes}

    # -- outline / workspace symbols ---------------------------------------------

    def on_textDocument_documentSymbol(self, p):
        doc = self.docs.get(p["textDocument"]["uri"])
        if doc is None:
            return []
        info = doc.best
        lines = doc.lines

        def rng(s, end=None):
            e = min(end or s.end or s.line, len(lines))
            text = lines[s.line - 1] if s.line <= len(lines) else ""
            sel = {"start": {"line": s.line - 1, "character": to_u16(text, s.col - 1)},
                   "end": {"line": s.line - 1, "character": to_u16(text, s.col - 1 + len(s.name))}}
            full = {"start": {"line": s.line - 1, "character": 0},
                    "end": {"line": e - 1, "character": to_u16(lines[e - 1], len(lines[e - 1])) if e <= len(lines) else 0}}
            if s.line - 1 > e - 1 or (s.line - 1 == e - 1 and full["end"]["character"] < sel["end"]["character"]):
                full = sel
            return full, sel

        def item(s, children=()):
            full, sel = rng(s)
            detail = ""
            if s.kind in ("function", "method", "constructor"):
                detail = "(" + ", ".join(param_text(x) for x in s.params) + f"): {tstr(s.ret)}"
            elif s.kind == "property":
                detail = tstr(s.type)
            return {"name": s.name, "detail": detail, "kind": SK.get(s.kind, 13), "range": full,
                    "selectionRange": sel, "children": list(children)}

        out = []
        for ci in info.classes.values():
            kids = [item(m) for m in sorted(ci.members.values(), key=lambda x: x.line)]
            out.append(item(ci.sym, kids))
        for s in info.functions.values():
            out.append(item(s))
        for s in info.globals.values():
            if s.kind == "const":
                out.append(item(s))
        out.sort(key=lambda x: x["range"]["start"]["line"])
        return out

    def on_workspace_symbol(self, p):
        q = (p.get("query") or "").lower()
        out = []

        def match(name):
            if not q:
                return True
            it = iter(name.lower())
            return all(ch in it for ch in q)    # letters of the query in order

        for info in self.infos():
            for ci in info.classes.values():
                if match(ci.name):
                    out.append(self.wsym(ci.sym, info.namespace or ""))
                for m in ci.members.values():
                    if m.kind in ("method", "constructor") and match(m.name):
                        out.append(self.wsym(m, ci.name))
            for s in info.functions.values():
                if match(s.name):
                    out.append(self.wsym(s, info.namespace or ""))
            if len(out) > 500:
                break
        return [x for x in out if x is not None][:500]

    def wsym(self, s, container):
        loc = self.location(s)
        if loc is None:
            return None
        return {"name": s.name, "kind": SK.get(s.kind, 13), "location": loc, "containerName": container}

    # -- formatting --------------------------------------------------------------

    def on_textDocument_formatting(self, p):
        from .fmt import format_text
        doc = self.docs.get(p["textDocument"]["uri"])
        if doc is None:
            return None
        opts = p.get("options") or {}
        indent = int(opts.get("tabSize") or 4)
        try:
            new = format_text(doc.text, doc.path, indent=indent, sort_imports=self.sort_imports)
        except BarqError as e:
            log(f"fmt: {doc.path}: {e.message}")
            return []
        if new == doc.text:
            return []
        last = doc.lines[-1]
        return [{"range": {"start": {"line": 0, "character": 0},
                           "end": {"line": len(doc.lines) - 1, "character": to_u16(last, len(last))}},
                 "newText": new}]

    # -- completion --------------------------------------------------------------

    def on_textDocument_completion(self, p):
        doc = self.docs.get(p["textDocument"]["uri"])
        if doc is None:
            return None
        line = p["position"]["line"]
        if line >= len(doc.lines):
            return []
        text = doc.lines[line]
        k = to_index(text, p["position"]["character"])
        before = text[:k]
        if in_string_or_comment(before):
            return []
        ctx = self.context(doc, line + 1, k)
        # attributes: #[Pu|
        m = re.match(r"\s*#\[(\w*)$", before)
        if m:
            items = [self.item(a, "class" if a[0].isupper() else "type", "attribute") for a in ATTRIBUTES]
            items += [self.item(t, "type", "type") for t in TYPES if t != "karo"]
            items += self.class_items(ctx, m.group(1), add_use=True, line=line)
            return {"isIncomplete": False, "items": items}
        # use App\Models\|
        m = re.match(r"\s*use\s+([\w\\]*)$", before)
        if m and ctx.func is None:
            return {"isIncomplete": False, "items": self.use_items(ctx, m.group(1))}
        chain, is_new, sep, prefix = chain_at(text, k)
        if sep is not None:
            if chain is None:
                return {"isIncomplete": False, "items": []}
            t = self.chain_type(ctx, chain, is_new)
            return {"isIncomplete": False, "items": self.member_items(ctx, t, sep)}
        if before.endswith(":") and not before.endswith("::"):
            return {"isIncomplete": False, "items": []}
        if re.search(r"\bnew\s+\w*$", before):
            return {"isIncomplete": False, "items": self.class_items(ctx, prefix, add_use=True, line=line, ctor=True)}
        return {"isIncomplete": False, "items": self.name_items(ctx, prefix, line)}

    def item(self, label, kind, detail="", doc=None, insert=None, snippet=False, sort=None, edits=None):
        it = {"label": label, "kind": CI.get(kind, 1)}
        if detail:
            it["detail"] = detail
        if doc:
            it["documentation"] = {"kind": "markdown", "value": doc}
        if insert is not None:
            it["insertText"] = insert
            if snippet:
                it["insertTextFormat"] = 2
        if sort is not None:
            it["sortText"] = sort
        if edits:
            it["additionalTextEdits"] = edits
        return it

    def call_insert(self, name, has_args):
        return (f"{name}($1)" if has_args else f"{name}()"), True

    def sym_item(self, s, sort="1"):
        if s.kind in ("function", "method", "constructor"):
            ins, sn = self.call_insert(s.name, bool(s.params))
            return self.item(s.name, s.kind, signature(s), s.doc or None, ins, sn, sort + s.name)
        return self.item(s.name, s.kind, signature(s), s.doc or None, sort=sort + s.name)

    def member_items(self, ctx, t, sep):
        if t is None:
            return []
        out = []
        if t[0] in ("inst", "cls"):
            v = t[1]
            static = t[0] == "cls"
            inside = ctx.cls is not None and ctx.cls.is_a(v)
            same = ctx.cls is not None and ctx.cls == v
            for m in v.members():
                if m.name.startswith("__") and m.name != "__construct":
                    continue
                if m.kind == "constructor":
                    continue
                if static and not (m.static or m.kind in ("const", "case")):
                    continue
                if not static and (m.static or m.kind in ("const", "case")):
                    continue
                if m.visibility == "private" and not (same or m.owner == ctx.cls):
                    continue
                if m.visibility == "protected" and not inside:
                    continue
                if m.sym is not None:
                    sort = "0" if m.owner == v else "1"
                    out.append(self.sym_item(m.sym, sort))
                else:
                    out.append(self.native_member_item(v, m))
            return out
        if t[0] == "mod":
            for nm, val in sorted(t[1].members.items()):
                label = f"{t[1].name}.{nm}"
                if isinstance(val, NativeFunction):
                    sig, parts = native_signature(label, val)
                    ins, sn = self.call_insert(nm, val.max_args != 0)
                    out.append(self.item(nm, "function", sig, None, ins, sn))
                elif isinstance(val, BarqClass):
                    out.append(self.item(nm, "class", f"class {nm}"))
                else:
                    out.append(self.item(nm, "const", f"{label}"))
            return out
        if t[0] == "prim":
            from . import builtins as B
            table = {"string": B.STRING_METHODS, "list": B.LIST_METHODS, "dict": B.DICT_METHODS,
                     "set": B.SET_METHODS}.get(t[1]) or {}
            for nm, spec in sorted(table.items()):
                fake = NativeFunction(f"{t[1]}.{nm}", None, spec[1], spec[2])
                ins, sn = self.call_insert(nm, spec[2] != 0)
                out.append(self.item(nm, "method", native_signature(nm, fake)[0], None, ins, sn))
        return out

    def native_member_item(self, v, m):
        n = m.native
        label = f"{v.name}.{m.name}"
        if m.kind in ("method",) and isinstance(n, NativeFunction):
            ins, sn = self.call_insert(m.name, n.max_args != 0)
            return self.item(m.name, "method", native_signature(label, n)[0], None, ins, sn, "1" + m.name)
        if m.kind == "method" and isinstance(n, BarqFunction):
            s = Sym(m.name, "method", "", 0, 0, params=n.params, ret=n.ret, static=n.is_static)
            ins, sn = self.call_insert(m.name, bool(n.params))
            return self.item(m.name, "method", signature(s), None, ins, sn, "1" + m.name)
        if m.kind == "case":
            return self.item(m.name, "case", f"case {m.name}", sort="0" + m.name)
        if m.kind == "property":
            return self.item(m.name, "property", f"{tstr(getattr(n, 'type', None))} {m.name}", sort="1" + m.name)
        return self.item(m.name, "const", label, sort="1" + m.name)

    def use_edit(self, ctx, fqn, line):
        """additionalTextEdits that add `use fqn` under the namespace / other use lines."""
        info = ctx.info
        short = fqn.split("\\")[-1]
        if short in info.uses or short in info.classes:
            return None
        if info.namespace and fqn == info.fqn(short):
            return None
        lines = info.lines
        at = 0
        for k, s in enumerate(lines):
            if re.match(r"\s*(namespace|use)\s", s) and indent_of(s) == 0:
                at = k + 1
            elif s.strip() and not s.strip().startswith("//") and at:
                break
        prefix = ""
        if at and re.match(r"namespace\s", lines[at - 1]):
            prefix = "\n"
        if at == 0:
            return [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                     "newText": f"use {fqn}\n\n"}]
        return [{"range": {"start": {"line": at, "character": 0}, "end": {"line": at, "character": 0}},
                 "newText": f"{prefix}use {fqn}\n"}]

    def class_items(self, ctx, prefix, add_use=False, line=0, ctor=False):
        out = []
        seen = set()
        for name, ci in ctx.info.classes.items():
            seen.add(name)
            out.append(self.item(name, ci.kind, signature(ci.sym), ci.sym.doc or None, sort="1" + name))
        for alias, (fqn, _) in ctx.info.uses.items():
            if alias in seen:
                continue
            seen.add(alias)
            ci = self.class_by_fqn(fqn, ctx.info)
            out.append(self.item(alias, ci.kind if ci else "class", fqn, sort="1" + alias))
        for info in self.infos():
            for name, ci in info.classes.items():
                if name in seen or (prefix and not name.lower().startswith(prefix[:1].lower())):
                    continue
                seen.add(name)
                edits = self.use_edit(ctx, ci.fqn, line) if add_use else None
                out.append(self.item(name, ci.kind, ci.fqn + ("  (adds use)" if edits else ""),
                                     ci.sym.doc or None, sort="2" + name, edits=edits))
        for name, v in self.builtins().items():
            if isinstance(v, BarqClass) and name not in seen:
                out.append(self.item(name, "interface" if v.kind == "interface" else "class",
                                     f"built-in {v.kind}", sort="3" + name))
        return out

    def use_items(self, ctx, typed):
        out, seen = [], set()
        base = typed.rsplit("\\", 1)[0] + "\\" if "\\" in typed else ""
        for info in self.infos():
            for ci in info.classes.values():
                fqn = ci.fqn
                if fqn in seen or not fqn.lower().startswith(typed.lower()):
                    continue
                seen.add(fqn)
                out.append(self.item(fqn, ci.kind, fqn, insert=fqn[len(base):] if base else fqn,
                                     sort="1" + fqn))
        for name, v in self.builtins().items():
            if isinstance(v, NativeModule):
                fqn = f"Barq\\{name}"
                if fqn.lower().startswith(typed.lower()):
                    out.append(self.item(fqn, "module", "standard library", insert=fqn[len(base):] if base else fqn,
                                         sort="2" + fqn))
        for it in out:
            # VS Code filters on the word before the cursor, which stops at '\'
            it["filterText"] = it["insertText"] if "insertText" in it else it["label"]
        return out

    def name_items(self, ctx, prefix, line):
        out, seen = [], set()

        def add(it):
            if it["label"] not in seen:
                seen.add(it["label"])
                out.append(it)

        for s in sorted(ctx.locals.values(), key=lambda x: x.name):
            add(self.item(s.name, s.kind, signature(s), sort="0" + s.name))
        if ctx.cls is not None:
            add(self.item("this", "keyword", f"this: {ctx.cls.name}", sort="0this"))
        for s in ctx.info.functions.values():
            add(self.sym_item(s, "1"))
        for s in ctx.info.globals.values():
            add(self.sym_item(s, "1"))
        for it in self.class_items(ctx, prefix, add_use=True, line=line):
            add(it)
        for info in self.infos():
            if info is ctx.info:
                continue
            for s in info.functions.values():
                add(self.sym_item(s, "2"))
        for name, v in sorted(self.builtins().items()):
            if isinstance(v, NativeFunction):
                sig, _ = native_signature(name, v)
                ins, sn = self.call_insert(name, v.max_args != 0)
                add(self.item(name, "function", sig, None, ins, sn, "4" + name))
            elif isinstance(v, NativeModule):
                add(self.item(name, "module", f"module {name}", sort="3" + name))
        for kw in KEYWORDS:
            add(self.item(kw, "keyword", sort="5" + kw))
        for t in TYPES:
            add(self.item(t, "type", "type", sort="5" + t))
        return out

    # -- signature help ----------------------------------------------------------

    def on_textDocument_signatureHelp(self, p):
        doc = self.docs.get(p["textDocument"]["uri"])
        if doc is None:
            return None
        line = p["position"]["line"]
        if line >= len(doc.lines):
            return None
        text = doc.lines[line]
        k = to_index(text, p["position"]["character"])
        # find the open '(' of the call the cursor is in (this line and a few above)
        start_line = max(0, line - 10)
        chunk = "\n".join(doc.lines[start_line:line] + [text[:k]])
        open_at, commas = open_call(chunk)
        if open_at is None:
            return None
        head = chunk[:open_at]
        head_line = head.split("\n")[-1]
        ln = start_line + head.count("\n")
        w = word_at(head_line, len(head_line))
        if w is None:
            return None
        pos = {"line": ln, "character": to_u16(head_line, w[0])}
        t = self.target(doc, pos)
        if t is None:
            return None
        kind, thing, _ = t
        label = params = None
        doc_md = None
        if kind == "class" and thing.ci is not None:
            ctor = thing.find("__construct")
            if ctor is not None and ctor.sym is not None:
                thing, kind = ctor, "member"
            else:
                return None
        s = None
        if kind == "sym" and thing.kind in ("function", "method", "constructor"):
            s = thing
        elif kind == "member" and thing.sym is not None and thing.sym.kind in ("method", "constructor"):
            s = thing.sym
        if s is not None:
            params = [param_text(x) for x in s.params]
            label = signature(s)
            doc_md = s.doc or None
        else:
            fn = None
            if kind == "member" and isinstance(thing.native, NativeFunction):
                fn, name = thing.native, f"{thing.owner.name}.{thing.name}"
            elif kind == "native" and isinstance(thing[1], NativeFunction):
                fn, name = thing[1], thing[0]
            if fn is None:
                return None
            label, params = native_signature(name, fn)
            if not params:
                return {"signatures": [{"label": label, "parameters": []}], "activeSignature": 0}
        infos = []
        pos_in = label.find("(") + 1
        for prm in params:
            a = label.find(prm, pos_in)
            if a < 0:
                infos.append({"label": prm})
                continue
            infos.append({"label": [a, a + len(prm)]})
            pos_in = a + len(prm)
        active = commas
        if params and params[-1].startswith("*") and active >= len(params):
            active = len(params) - 1
        sig = {"label": label, "parameters": infos, "activeParameter": active}
        if doc_md:
            sig["documentation"] = {"kind": "markdown", "value": doc_md}
        return {"signatures": [sig], "activeSignature": 0, "activeParameter": active}


def open_call(chunk):
    """(index of the '(' of the call that is still open at the end of chunk, commas
    written in it so far) — (None, 0) when the end is not inside a call."""
    stack = []                       # [bracket, index, commas]
    quote = None
    i = 0
    while i < len(chunk):
        ch = chunk[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote or ch == "\n":
                quote = None
        elif chunk.startswith("//", i):
            while i < len(chunk) and chunk[i] != "\n":
                i += 1
            continue
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            stack.append([ch, i, 0])
        elif ch in ")]}":
            if stack:
                stack.pop()
        elif ch == "," and stack:
            stack[-1][2] += 1
        i += 1
    if stack and stack[-1][0] == "(":
        return stack[-1][1], stack[-1][2]
    return None, 0


def enclosing(lines, line, k=None):
    """(class name, function name, function header line) around a 1-based line, by indentation."""
    cur = lines[line - 1] if 0 < line <= len(lines) else ""
    if not cur[:k].strip() and k is not None:
        limit = k                     # a blank line: the cursor column is the indentation
    elif cur.strip():
        limit = indent_of(cur)
    else:
        limit = 10 ** 6
    cls_name = func_name = func_line = None
    hm = re.match(r"\s*karo\s+([A-Za-z_]\w*)\s*\(", cur)
    if hm:
        func_name, func_line = hm.group(1), line
    for n in range(line - 1, 0, -1):
        s = lines[n - 1]
        if not s.strip() or indent_of(s) >= limit:
            continue
        limit = indent_of(s)
        m = re.match(r"\s*karo\s+([A-Za-z_]\w*)\s*\(", s)
        c = re.match(r"\s*(?:abstract\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)", s)
        if m and func_name is None:
            func_name, func_line = m.group(1), n
        elif c:
            cls_name = c.group(1)
            break
        if limit == 0:
            break
    return cls_name, func_name, func_line


class LspError(Exception):
    pass


def in_string_or_comment(before):
    """True when the cursor (end of `before`, one line) is inside a "..." / '...' string
    (but not inside a {...} of an interpolated one) or after //."""
    quote = None
    brace = 0
    i = 0
    while i < len(before):
        ch = before[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if quote == '"' and ch == "{":
                brace += 1
            elif quote == '"' and ch == "}" and brace:
                brace -= 1
            elif ch == quote and not brace:
                quote = None
        else:
            if ch in "\"'":
                quote = ch
            elif before.startswith("//", i):
                return True
        i += 1
    return quote is not None and brace == 0


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def read_messages(inp, q):
    while True:
        length = None
        while True:
            line = inp.readline()
            if not line:
                q.put(None)
                return
            line = line.strip()
            if not line:
                break
            name, _, value = line.decode("ascii", "replace").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        if length is None:
            continue
        body = inp.read(length)
        try:
            q.put(json.loads(body.decode("utf-8")))
        except ValueError:
            log("bad message skipped")


def serve(inp, out):
    server = Server(out)
    q = queue.Queue()
    threading.Thread(target=read_messages, args=(inp, q), daemon=True).start()
    wait = None
    while True:
        try:
            msg = q.get(timeout=wait) if wait is None or wait > 0 else q.get_nowait()
        except queue.Empty:
            msg = ()
        if msg is None:                 # the editor closed stdin
            return 0 if server.shutdown else 1
        if msg != ():
            server.handle(msg)
        # answer queued requests first; check files when typing pauses
        wait = 0 if not q.empty() else server.run_checks()
        if wait == 0 and q.empty():
            wait = server.run_checks()


def cmd_lsp(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv and argv[0] == "--stdio":
        argv = argv[1:]
    inp = sys.stdin.buffer
    out = sys.stdout.buffer
    sys.stdout = sys.stderr     # anything printed by mistake must not corrupt the protocol
    from .cli import with_big_stack
    from .vm import MAX_DEPTH
    return with_big_stack(lambda: serve(inp, out), MAX_DEPTH * 3)
