"""`barq fmt` — rewrites .bq files in one consistent style.

The formatter works on tokens and lines, not on the syntax tree, so comments
and the line breaks the author chose are kept. What it changes:

  - indentation: every level becomes 4 spaces (or --indent N); lines inside
    brackets and continued lines keep their shape, re-indented the same way
  - spacing inside a line: one space around binary operators, after commas and
    after keywords such as `if`/`while`; none inside brackets, around `.`, `::`,
    `\\`, in `list<int>`, after unary `-`, `~`, `?` (nullable) and `*` (spread)
  - blank lines: at most one in a row, exactly one around top-level
    functions, classes, enums, interfaces and traits, none at the file's start
    or end, and one newline at the end
  - each run of top-level `use` lines is sorted
  - trailing whitespace is removed

Strings and comments are copied unchanged. Every result is checked: lexing
the formatted text must give the same tokens as the original, or the file is
left alone and an error is reported.
"""

import difflib
import os
import sys

from .errors import BarqError, SourceFile, report
from .lexer import KEYWORDS, OPERATORS, Lexer

OPENERS = {"(", "[", "{", "#[", "#{", "#("}
CLOSERS = {")", "]", "}"}
BINARY = {"**", "==", "!=", ">=", "<=", "+=", "-=", "*=", "/=", "%=", "=>", "??",
          "+", "-", "*", "/", "%", "=", "<", ">", "?"}
PREFIX = {"-", "+", "*", "?", "~"}
VALUE_KEYWORDS = {"this", "self", "parent", "static", "null", "true", "false"}
GENERIC_TYPES = {"list", "dict", "set"}
DEF_KEYWORDS = {"class", "enum", "interface", "trait", "abstract"}
SKIP_DIRS = {"views", "node_modules", "vendor", "__pycache__"}


class FormatError(BarqError):
    kind = "FormatError"


class Tok:
    __slots__ = ("kind", "text", "line", "col", "end_line", "gap")

    def __init__(self, kind, text, line, col, end_line, gap):
        self.kind = kind          # name kw num str op comment
        self.text = text          # exact source text
        self.line = line
        self.col = col
        self.end_line = end_line  # differs from line for """ strings and /* */ comments
        self.gap = gap            # whitespace before it on the same line, in the source

    def op(self, *values):
        return self.kind == "op" and self.text in values

    def __repr__(self):
        return f"Tok({self.kind}, {self.text!r}, {self.line})"


# -- scanning ---------------------------------------------------------------

def scan(text):
    """Split source that the lexer already accepted into tokens that keep
    their exact text, comments included."""
    toks = []
    i, n, line, line_start = 0, len(text), 1, 0
    gap = 0
    ops = sorted(OPERATORS, key=len, reverse=True)

    def add(kind, start, end, start_line, start_col):
        nonlocal gap
        toks.append(Tok(kind, text[start:end], start_line, start_col, line, gap))
        gap = 0

    while i < n:
        ch = text[i]
        if ch == "\n":
            i += 1
            line += 1
            line_start = i
            gap = 0
            continue
        if ch in " \t":
            i += 1
            gap += 1
            continue
        start, sline, scol = i, line, i - line_start
        if text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            end = i
            while end > start and text[end - 1] in " \t":
                end -= 1
            add("comment", start, end, sline, scol)
            continue
        if text.startswith("/*", i):
            j = text.index("*/", i + 2) + 2
            line += text.count("\n", i, j)
            if "\n" in text[i:j]:
                line_start = text.rindex("\n", i, j) + 1
            i = j
            add("comment", start, i, sline, scol)
            continue
        if ch.isdigit():
            if ch == "0" and i + 1 < n and text[i + 1] in "xX":
                i += 2
                while i < n and text[i] in "0123456789abcdefABCDEF_":
                    i += 1
            else:
                while i < n and (text[i].isdigit() or text[i] == "_"):
                    i += 1
                if i + 1 < n and text[i] == "." and text[i + 1].isdigit():
                    i += 1
                    while i < n and (text[i].isdigit() or text[i] == "_"):
                        i += 1
                if i < n and text[i] in "eE" and i + 1 < n and (
                        text[i + 1].isdigit() or (text[i + 1] in "+-" and i + 2 < n and text[i + 2].isdigit())):
                    i += 2
                    while i < n and text[i].isdigit():
                        i += 1
            add("num", start, i, sline, scol)
            continue
        if ch.isalpha() or ch == "_":
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            add("kw" if text[start:i] in KEYWORDS else "name", start, i, sline, scol)
            continue
        if ch in "\"'" and text.startswith(ch * 3, i):
            i += 3
            while not text.startswith(ch * 3, i):
                if ch == '"' and text[i] == "\\" and text[i + 1] != "\n":
                    i += 2
                    continue
                if text[i] == "\n":
                    line += 1
                    line_start = i + 1
                i += 1
            i += 3
            add("str", start, i, sline, scol)
            continue
        if ch == '"':
            i = skip_double(text, i)
            add("str", start, i, sline, scol)
            continue
        if ch == "'":
            i += 1
            while text[i] != "'":
                i += 2 if text[i] == "\\" and text[i + 1] in "'\\" else 1
            i += 1
            add("str", start, i, sline, scol)
            continue
        if ch == "#" and i + 1 < n and text[i + 1] in "[{(":
            i += 2
            add("op", start, i, sline, scol)
            continue
        for op in ops:
            if text.startswith(op, i):
                i += len(op)
                break
        else:
            raise FormatError(f"unexpected character {ch!r}")
        add("op", start, i, sline, scol)
    names_for_karo_kr(toks)
    members_are_names(toks)
    return toks


def members_are_names(toks):
    """A keyword right after `.`, `?.` or `::` is a member name (`Regex.match(...)`),
    so it gets no keyword spacing."""
    prev = None
    for t in toks:
        if t.kind == "comment":
            continue
        if t.kind == "kw" and prev is not None and prev.kind == "op" and prev.text in (".", "?.", "::"):
            t.kind = "name"
        prev = t


def names_for_karo_kr(toks):
    """Apply the lexer's rule: `karo` / `kr` that don't start a function are a
    type and a name (`karo kr`, `list<karo>`, `kr(value)`)."""
    from .lexer import Token, classify_karo_kr
    kinds = {"kw": "KEYWORD", "name": "NAME", "op": "OP", "num": "INT", "str": "STRING"}
    shims, owners, line = [], [], None
    for t in toks:
        if t.kind == "comment":
            continue
        if line is not None and t.line != line:
            shims.append(Token("NEWLINE", "\n", line, 0))
            owners.append(None)
        line = t.end_line
        shims.append(Token(kinds[t.kind], t.text, t.line, t.col))
        owners.append(t)
    shims.append(Token("EOF", None, line or 1, 0))
    owners.append(None)
    classify_karo_kr(shims)
    for s, t in zip(shims, owners):
        if t is not None and t.kind == "kw" and s.type == "NAME":
            t.kind = "name"


def skip_double(text, i):
    """Index just past the "..." string starting at i ({expr} parts included)."""
    i += 1
    while text[i] != '"':
        if text[i] == "\\":
            i += 2
        elif text[i] == "{":
            i += 1
            depth = 1
            while depth:
                c = text[i]
                if c in "'\"":
                    i += 1
                    while text[i] != c:
                        i += 2 if text[i] == "\\" else 1
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                i += 1
        else:
            i += 1
    return i + 1


# -- spacing ----------------------------------------------------------------

def operand_end(t, generic_close):
    """True when t can end an operand, so a following '-' or '(' binds to it."""
    if t.kind in ("name", "num", "str"):
        return True
    if t.kind == "kw":
        return t.text in VALUE_KEYWORDS
    return t.text in CLOSERS or id(t) in generic_close


def find_generics(code):
    """Indexes of '<' and '>' that are type arguments, as in list<int> or
    dict<string, list<User>>. Plain comparisons keep their spaces."""
    opens, closes = set(), set()

    def parse_type(j):
        if j < len(code) and code[j].op("?"):
            j += 1
        if j >= len(code) or not (code[j].kind == "name" or (code[j].kind == "kw" and code[j].text == "null")):
            return None
        j += 1
        while j + 1 < len(code) and code[j].op("\\") and code[j + 1].kind == "name":
            j += 2
        if j < len(code) and code[j].op("<"):
            end = parse_args(j)
            if end is None:
                return None
            j = end
        return j

    def parse_args(j):
        found = [j]
        j = parse_type(j + 1)
        while j is not None and j < len(code) and code[j].op(","):
            j = parse_type(j + 1)
        if j is None or j >= len(code) or not code[j].op(">"):
            return None
        for k in found:
            opens.add(id(code[k]))
        closes.add(id(code[j]))
        return j + 1

    for i, t in enumerate(code):
        if t.op("<") and i and code[i - 1].kind == "name" and id(t) not in opens:
            prev = code[i - 1]
            tight = t.gap == 0 and t.line == prev.end_line
            if prev.text in GENERIC_TYPES or tight:
                parse_args(i)
    return opens, closes


class Spacer:
    """Decides the space between two tokens on one output line. It keeps
    bracket and ternary state across the lines of one statement."""

    def __init__(self, code):
        self.opens, self.closes = find_generics(code)
        self.prefix = set()       # ids of unary/prefix operator tokens
        self.ternary = [0]        # open '?' per bracket depth
        self.reset()

    def reset(self):
        self.ternary = [0]

    def track(self, t, prev):
        """Update state for token t (call for every code token in order)."""
        if t.kind != "op":
            return
        if t.text in OPENERS:
            self.ternary.append(0)
        elif t.text in CLOSERS:
            if len(self.ternary) > 1:
                self.ternary.pop()
        elif t.text in PREFIX and (prev is None or not operand_end(prev, self.closes)):
            self.prefix.add(id(t))
        elif t.text == "?":
            self.ternary[-1] += 1

    def ternary_colon(self, t):
        if t.op(":") and self.ternary[-1] > 0:
            self.ternary[-1] -= 1
            return True
        return False

    def between(self, p, c, colon_ternary):
        """Space before code token c, which follows p on the same line."""
        pt, ct = (p.text if p.kind == "op" else None), (c.text if c.kind == "op" else None)
        if pt in OPENERS or ct in CLOSERS or ct in (",", ";"):
            return ""
        if pt in (",", ";"):
            return " "
        if ct in (".", "?.", "::") or pt in (".", "?.", "::"):
            return ""
        if pt == "\\" or (ct == "\\" and p.kind == "name"):
            return ""
        if id(c) in self.opens or id(c) in self.closes or id(p) in self.opens:
            return ""
        if id(p) in self.prefix:
            return ""
        if ct == ":":
            return " " if colon_ternary else ""
        if pt == ":":
            return " "
        if id(c) in self.prefix:
            return " "
        if (ct in BINARY) or (pt in BINARY):
            return " "
        if ct in ("(", "[", "{"):
            if p.kind == "kw" and p.text in ("karo", "kr"):
                return ""
            return "" if operand_end(p, self.closes) else " "
        return " "


# -- layout -----------------------------------------------------------------

def place(stack, width, unit, mutate=True):
    """Map a source indentation width to an output one. The stack holds
    (source width, output width) pairs of the open levels."""
    if not mutate:
        stack = list(stack)
    while len(stack) > 1 and stack[-1][0] > width:
        stack.pop()
    if stack[-1][0] == width:
        return stack[-1][1]
    if stack[-1][0] < width:
        new = stack[-1][1] + unit
        if mutate:
            stack.append((width, new))
        return new
    return stack[-1][1]


def shift_comment(text, delta):
    """Move the inner lines of a /* */ comment along with its first line."""
    if "\n" not in text or delta == 0:
        return text
    head, *rest = text.split("\n")
    out = [head]
    for ln in rest:
        if delta > 0:
            out.append(" " * delta + ln if ln.strip() else ln.rstrip())
        else:
            lead = len(ln) - len(ln.lstrip(" "))
            out.append(ln[min(lead, -delta):].rstrip())
    return "\n".join(out)


def indent_width(raw_line):
    w = 0
    for ch in raw_line:
        if ch == " ":
            w += 1
        elif ch == "\t":
            w += 4
        else:
            break
    return w


class Segment:
    """One output line: the tokens that start on a source line, plus any that
    follow a multi-line token on its last line."""

    def __init__(self, line):
        self.line = line
        self.toks = []
        self.indent = 0
        self.kind = None
        self.text = ""

    @property
    def end_line(self):
        return self.toks[-1].end_line

    @property
    def first(self):
        return self.toks[0]

    def code(self):
        return [t for t in self.toks if t.kind != "comment"]


def format_text(text, path="<stdin>", indent=4, sort_imports=True):
    """Return the formatted text. Raises BarqError when the file does not lex,
    FormatError when formatting would change its meaning."""
    source = SourceFile(path, text)
    text = source.text
    lexer = Lexer(source)
    lexer.line_kinds = {}
    before = lexer.tokenize()
    kinds = lexer.line_kinds
    toks = scan(text)
    if not toks:
        return ""
    raw_lines = text.split("\n")

    segments = []
    for t in toks:
        if segments and t.line == segments[-1].end_line:
            segments[-1].toks.append(t)
        else:
            segments.append(Segment(t.line))
            segments[-1].toks.append(t)

    code = [t for t in toks if t.kind != "comment"]
    spacer = Spacer(code)
    prev_code = None
    block = [(0, 0)]
    temp = [(0, 0)]
    for seg in segments:
        kind = kinds.get(seg.line, "free" if seg.first.kind != "comment" else "comment-free")
        seg.kind = kind
        width = indent_width(raw_lines[seg.line - 1])
        if kind == "block":
            seg.indent = place(block, width, indent)
            temp = list(block)
            spacer.reset()
            prev_code = None
        elif kind == "free":
            seg.indent = place(temp, width, indent)
        elif kind == "comment-block":
            seg.indent = place(block, width, indent, mutate=False)
        else:
            seg.indent = place(temp, width, indent, mutate=False)

        out = []
        prev = None
        for t in seg.toks:
            if t.kind == "comment":
                if prev is not None:
                    out.append(" " * max(1, t.gap))
                out.append(shift_comment(t.text, seg.indent - width) if prev is None else t.text)
                prev = t
                continue
            spacer.track(t, prev_code)
            colon = spacer.ternary_colon(t)
            if prev is not None:
                if prev.kind == "comment":       # /* inline */ code
                    out.append(" " * max(1, t.gap))
                else:
                    out.append(spacer.between(prev, t, colon))
            out.append(t.text)
            prev = t
            prev_code = t
        seg.text = " " * seg.indent + "".join(out)

    # Check the result before sorting the imports, which reorders tokens on
    # purpose; then check the sorted one has the same tokens in another order.
    plain = "\n".join(layout(segments, False)).rstrip("\n") + "\n"
    result = "\n".join(layout(segments, sort_imports)).rstrip("\n") + "\n"
    try:
        after = Lexer(SourceFile(path, plain)).tokenize()
        sorted_after = after if result == plain else Lexer(SourceFile(path, result)).tokenize()
    except BarqError:
        after = sorted_after = None
    if (after is None or not same_tokens(before, after)
            or sorted(map(repr, map(token_key, after))) != sorted(map(repr, map(token_key, sorted_after)))):
        raise FormatError("formatting would change the meaning of this file — it was left unchanged "
                          "(please report this as a formatter bug)", source, 1, 1)
    return result


def is_def_header(seg):
    code = seg.code()
    if seg.indent or not code:
        return False
    t = code[0]
    if t.kind == "kw" and t.text in DEF_KEYWORDS:
        return True
    return t.kind == "kw" and t.text == "karo" and len(code) > 1 and code[1].kind == "name"


def is_attr(seg):
    code = seg.code()
    return seg.indent == 0 and bool(code) and code[0].op("#[") and seg.kind == "block"


def layout(segments, sort_imports):
    """Join segments into lines with the blank-line rules."""
    # Blank lines in the source before each segment (at most one is kept).
    blanks = []
    prev_end = 0
    for seg in segments:
        blanks.append(min(1, seg.line - prev_end - 1) if prev_end else 0)
        prev_end = seg.end_line

    top = [seg.kind in ("block", "comment-block") and seg.indent == 0 for seg in segments]

    # One blank line before each top-level definition, counting the comments
    # and attributes right above it, and one after its body.
    for i, seg in enumerate(segments):
        if not (top[i] and is_def_header(seg)):
            continue
        start = i
        while start > 0 and top[start - 1] and blanks[start] == 0 and (
                segments[start - 1].first.kind == "comment" or is_attr(segments[start - 1])):
            start -= 1
        if start > 0:
            blanks[start] = 1
        j = i + 1
        while j < len(segments) and not top[j]:
            j += 1
        if j < len(segments) and j > i + 1:
            blanks[j] = 1

    if sort_imports:
        segments = list(segments)
        sort_uses(segments, blanks, top)

    lines = []
    for seg, blank in zip(segments, blanks):
        if blank and lines:
            lines.append("")
        lines.append(seg.text)
    return lines


def sort_uses(segments, blanks, top):
    i = 0
    while i < len(segments):
        if not is_use(segments[i], top[i]):
            i += 1
            continue
        j = i + 1
        while j < len(segments) and blanks[j] == 0 and is_use(segments[j], top[j]):
            j += 1
        run = sorted(segments[i:j], key=lambda s: (s.text.lower(), s.text))
        segments[i:j] = run
        i = j


def is_use(seg, top):
    code = seg.code()
    return (top and seg.kind == "block" and bool(code) and code[0].kind == "kw" and code[0].text == "use"
            and seg.first.kind != "comment" and all(t.line == t.end_line for t in seg.toks)
            and not any(t.kind == "comment" for t in seg.toks))


def token_key(t):
    """What must not change: the type and value, but not positions (string
    tokens hold the position of each {expr} part) or indentation widths."""
    if t.type in ("INDENT", "DEDENT", "NEWLINE"):
        return (t.type,)
    if t.type == "STRING":
        return (t.type, tuple(p if isinstance(p, str) else ("expr", p[0]) for p in t.value))
    return (t.type, repr(t.value))


def same_tokens(a, b):
    return len(a) == len(b) and all(token_key(x) == token_key(y) for x, y in zip(a, b))


# -- command ----------------------------------------------------------------

FMT_USAGE = """usage: barq fmt [options] [files or dirs...]

Formats .bq files in place (default: the current folder). Folders named
views/ (templates), node_modules/, vendor/ and hidden folders are skipped.

  --check       Change nothing; list the files that need formatting (exit 1 if any)
  --diff        Change nothing; print what would change
  --indent N    Spaces per indentation level (default 4)
  --no-sort     Leave the order of 'use' lines alone
  -             Read source from stdin and write the result to stdout
"""


def fmt_files(paths):
    for p in paths:
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
                for name in sorted(filenames):
                    if name.endswith(".bq"):
                        yield os.path.join(dirpath, name)
        else:
            yield p


def cmd_fmt(argv):
    check = diff = False
    indent = 4
    sort_imports = True
    paths = []
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--check":
            check = True
        elif a == "--diff":
            diff = True
        elif a == "--no-sort":
            sort_imports = False
        elif a == "--indent" or a.startswith("--indent="):
            value = a.split("=", 1)[1] if "=" in a else (args.pop(0) if args else "")
            if not value.isdigit() or not 1 <= int(value) <= 8:
                sys.stderr.write("barq fmt: --indent takes a number from 1 to 8\n")
                return 2
            indent = int(value)
        elif a in ("-h", "--help"):
            print(FMT_USAGE)
            return 0
        elif a.startswith("--"):
            sys.stderr.write(f"barq fmt: unknown option '{a}'\n\n{FMT_USAGE}")
            return 2
        else:
            paths.append(a)

    if paths == ["-"]:
        text = sys.stdin.read()
        try:
            sys.stdout.write(format_text(text, "<stdin>", indent, sort_imports))
        except BarqError as e:
            report(e)
            return 1
        return 0

    changed = failed = total = 0
    for path in fmt_files(paths or ["."]):
        if not os.path.isfile(path):
            sys.stderr.write(f"barq fmt: file not found: {path}\n")
            failed += 1
            continue
        total += 1
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        try:
            result = format_text(text, path, indent, sort_imports)
        except BarqError as e:
            report(e)
            failed += 1
            continue
        if result == text:
            continue
        changed += 1
        if diff:
            sys.stdout.writelines(difflib.unified_diff(
                text.replace("\r\n", "\n").splitlines(True), result.splitlines(True),
                f"a/{path}", f"b/{path}"))
        elif check:
            print(path)
        else:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(result)
    files = lambda k: f"{k} file{'s' if k != 1 else ''}"
    if check or diff:
        summary = f"{files(changed)} would be reformatted" if changed else f"{files(total)} already formatted"
    else:
        summary = f"reformatted {files(changed)}" if changed else f"{files(total)} already formatted"
    if failed:
        summary += f", {failed} could not be read"
    (sys.stderr if check or diff else sys.stdout).write(summary + "\n")
    if failed:
        return 1
    return 1 if (check or diff) and changed else 0
