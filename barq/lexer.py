"""Turns Barq source text into a flat list of tokens.

Blocks are indentation based, so the lexer emits NEWLINE, INDENT and DEDENT
tokens the same way Python's tokenizer does. Newlines inside brackets are
ignored, with one exception: an anonymous `karo(...)` function whose body starts
on the next line opens an indented block even when it sits inside a call's
parentheses.
"""

from .errors import LexError

KEYWORDS = {
    "if", "else", "elseif", "for", "foreach", "while", "do", "break", "continue",
    "return", "switch", "case", "default", "match",
    "class", "enum", "abstract", "extends", "implements", "interface", "trait", "new",
    "this", "self", "parent", "static", "const",
    "namespace", "use", "as",
    "karo", "kr",
    "public", "private", "protected",
    "null", "true", "false",
    "and", "or", "not", "is", "in",
    "try", "except", "finally", "raise",
}

# Type names are ordinary identifiers to the lexer; the parser treats them
# specially. They are still reserved so nobody can shadow them.
TYPE_NAMES = {"int", "float", "string", "bool", "void", "any", "list", "dict", "set"}

OPERATORS = [
    # Longest first so that "**=" is not read as "**" then "=".
    "**", "==", "!=", ">=", "<=", "+=", "-=", "*=", "/=", "%=", "=>", "::", "?.", "??",
    "+", "-", "*", "/", "%", "=", "<", ">", "(", ")", "[", "]", "{", "}",
    ",", ".", ":", "?", "~", "\\", ";",
]

# A line that ends with one of these carries on to the next line. ':' (case x:,
# headers), ',' and '>' (karo f(): list<int>) are left out because a line can end with them.
CONTINUES = {"+", "-", "*", "/", "%", "**", "==", "!=", "<=", ">=", "<", "??", "?", "=>",
             "=", "+=", "-=", "*=", "/=", "%="}
CONTINUES_KW = {"and", "or", "not", "in", "is"}

OPENERS = {"(": ")", "[": "]", "{": "}", "#[": "]", "#{": "}", "#(": ")"}
CLOSERS = {")", "]", "}"}


class Token:
    __slots__ = ("type", "value", "line", "col")

    def __init__(self, type_, value, line, col):
        self.type = type_    # NAME KEYWORD INT FLOAT STRING RAWSTRING OP NEWLINE INDENT DEDENT EOF
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return f"Token({self.type}, {self.value!r}, {self.line}:{self.col})"

    def is_op(self, *values):
        return self.type == "OP" and self.value in values

    def is_kw(self, *values):
        return self.type == "KEYWORD" and self.value in values


class _Frame:
    """An indentation-sensitive region. The file itself is the first frame;
    each multi-line anonymous `karo(...)` inside brackets pushes another."""

    def __init__(self, depth, base_indent):
        self.depth = depth          # bracket depth at which newlines matter
        self.indents = [base_indent]


class Lexer:
    def __init__(self, source):
        self.source = source
        self.text = source.text
        self.pos = 0
        self.line = 1
        self.line_start = 0
        self.tokens = []
        self.brackets = []          # stack of (opener, line, col)
        self.frames = [_Frame(0, 0)]
        self.indent_style = None    # "space" or "tab", fixed by the first indented line
        self.br_pending = None      # bracket depth where a `karo(`/`kr(` parameter list started
        self.br_header = False      # True between its ")" and the end of its header
        self.line_indent = 0
        self.continued = False      # the last line ended with an operator: the next one carries on
        self.line_kinds = None      # {line: kind} when the formatter asks (see note())

    # -- helpers -----------------------------------------------------------

    def error(self, msg, line=None, col=None):
        raise LexError(msg, self.source, line or self.line, col or (self.pos - self.line_start + 1))

    def col(self):
        return self.pos - self.line_start + 1

    def add(self, type_, value, line, col):
        self.tokens.append(Token(type_, value, line, col))

    def peek(self, offset=0):
        i = self.pos + offset
        return self.text[i] if i < len(self.text) else ""

    @property
    def depth(self):
        return len(self.brackets)

    # -- main loop ---------------------------------------------------------

    def tokenize(self):
        at_line_start = True
        while True:
            if at_line_start:
                at_line_start = False
                if self.handle_line_start():
                    continue
            if self.pos >= len(self.text):
                break
            ch = self.text[self.pos]

            if ch == "\n":
                self.newline()
                at_line_start = True
                continue
            if ch in " \t":
                self.pos += 1
                continue
            if ch == "/" and self.peek(1) == "/":
                while self.pos < len(self.text) and self.text[self.pos] != "\n":
                    self.pos += 1
                continue
            if ch == "/" and self.peek(1) == "*":
                self.block_comment()
                continue
            if ch.isdigit():
                self.number()
                continue
            if ch.isalpha() or ch == "_":
                self.name()
                continue
            if ch in "\"'" and self.text.startswith(ch * 3, self.pos):
                self.string_triple(ch)
                continue
            if ch == '"':
                self.string_double()
                continue
            if ch == "'":
                self.string_single()
                continue
            if ch == "#" and self.peek(1) in "[{(":
                line, col = self.line, self.col()
                op = "#" + self.peek(1)
                self.pos += 2
                self.open_bracket(op, line, col)
                continue
            self.operator()

        if self.brackets:
            op, line, col = self.brackets[-1]
            self.error(f"'{op}' was never closed", line, col)
        self.finish()
        return self.tokens

    def handle_line_start(self):
        """Measure indentation at the start of a physical line. Returns True when
        the line is blank or a comment and was skipped entirely."""
        start = self.pos
        first_line = self.line
        width = 0
        chars = set()
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            chars.add(self.text[self.pos])
            width += 1 if self.text[self.pos] == " " else 4
            self.pos += 1
        rest = self.text[self.pos:self.pos + 2]
        if self.pos >= len(self.text) or self.text[self.pos] == "\n" or rest == "//":
            # Blank or comment-only line: no effect on indentation.
            self.note(first_line, True)
            while self.pos < len(self.text) and self.text[self.pos] != "\n":
                self.pos += 1
            return True
        if rest == "/*":
            # A block comment at line start counts as whitespace.
            self.block_comment()
            while self.pos < len(self.text) and self.text[self.pos] in " \t":
                self.pos += 1
            if self.pos >= len(self.text) or self.text[self.pos] == "\n" or self.text.startswith("//", self.pos):
                self.note(first_line, True)
                while self.pos < len(self.text) and self.text[self.pos] != "\n":
                    self.pos += 1
                return True
        self.line_indent = width
        self.note(first_line, False)
        frame = self.frames[-1]
        if self.depth != frame.depth:
            return False  # inside brackets: indentation does not matter
        if self.continued:
            self.continued = False
            return False  # carries on the line above, so its indentation does not matter

        # A line starting with "." continues a method chain from the line above.
        if self.text[self.pos] == "." and not self.peek(1).isdigit():
            if self.tokens and self.tokens[-1].type == "NEWLINE":
                self.tokens.pop()
                return False

        if width > 0:
            style = "tab" if chars == {"\t"} else "space" if chars == {" "} else "mixed"
            if style == "mixed":
                self.error("indentation mixes tabs and spaces", self.line, 1)
            if self.indent_style is None:
                self.indent_style = style
            elif self.indent_style != style:
                self.error(f"inconsistent indentation: this file indents with {self.indent_style}s", self.line, 1)

        current = frame.indents[-1]
        if width > current:
            if not self.tokens or self.tokens[-1].type != "NEWLINE":
                self.error("unexpected indentation", self.line, 1)
            frame.indents.append(width)
            self.add("INDENT", width, self.line, 1)
        else:
            while len(frame.indents) > 1 and width < frame.indents[-1]:
                frame.indents.pop()
                self.add("DEDENT", width, self.line, 1)
            if len(self.frames) > 1 and len(frame.indents) == 1 and width <= frame.indents[0]:
                # The multi-line lambda body is over; return to bracket mode.
                self.frames.pop()
                return False
            if width != frame.indents[-1]:
                self.error("dedent does not match any outer indentation level", self.line, 1)
        return False

    def note(self, line, comment):
        """For the formatter: "block" lines have their indentation checked;
        "free" ones (inside brackets, continued lines, method chains, lambda
        bodies inside brackets) do not. Comment-only lines get "comment-" first."""
        if self.line_kinds is None:
            return
        frame = self.frames[-1]
        free = (self.depth != frame.depth or self.continued or len(self.frames) > 1
                or (not comment and self.text[self.pos] == "." and not self.peek(1).isdigit()
                    and self.tokens and self.tokens[-1].type == "NEWLINE"))
        self.line_kinds[line] = ("comment-" if comment else "") + ("free" if free else "block")

    def newline(self):
        frame = self.frames[-1]
        if self.depth == frame.depth:
            last = self.tokens[-1] if self.tokens else None
            if last is not None and ((last.type == "OP" and last.value in CONTINUES)
                                     or (last.type == "KEYWORD" and last.value in CONTINUES_KW)):
                self.continued = True   # `total = a +` goes on on the next line
            elif last is not None and last.type not in ("NEWLINE", "INDENT", "DEDENT"):
                self.add("NEWLINE", "\n", self.line, self.col())
        elif self.br_header and self.depth > frame.depth:
            # `kr(...)` header ended a line inside brackets: open a block frame.
            self.br_header = False
            self.frames.append(_Frame(self.depth, self.line_indent))
            self.add("NEWLINE", "\n", self.line, self.col())
        self.br_header = False
        self.pos += 1
        self.line += 1
        self.line_start = self.pos

    def finish(self):
        line = self.line
        if self.tokens and self.tokens[-1].type not in ("NEWLINE", "DEDENT", "INDENT"):
            self.add("NEWLINE", "\n", line, 1)
        while len(self.frames) > 1:
            self.frames.pop()
        frame = self.frames[0]
        while len(frame.indents) > 1:
            frame.indents.pop()
            self.add("DEDENT", 0, line, 1)
        self.add("EOF", None, line, 1)

    # -- token kinds -------------------------------------------------------

    def block_comment(self):
        line, col = self.line, self.col()
        self.pos += 2
        while self.pos < len(self.text):
            if self.text.startswith("*/", self.pos):
                self.pos += 2
                return
            if self.text[self.pos] == "\n":
                self.line += 1
                self.line_start = self.pos + 1
            self.pos += 1
        self.error("unterminated /* comment", line, col)

    def number(self):
        line, col = self.line, self.col()
        start = self.pos
        text = self.text
        if text[self.pos] == "0" and self.peek(1) in "xX":
            self.pos += 2
            while self.pos < len(text) and (text[self.pos] in "0123456789abcdefABCDEF_"):
                self.pos += 1
            raw = text[start:self.pos].replace("_", "")
            if len(raw) == 2:
                self.error("hex literal needs digits", line, col)
            self.add("INT", int(raw, 16), line, col)
            return
        while self.pos < len(text) and (text[self.pos].isdigit() or text[self.pos] == "_"):
            self.pos += 1
        is_float = False
        if self.peek() == "." and self.peek(1).isdigit():
            is_float = True
            self.pos += 1
            while self.pos < len(text) and (text[self.pos].isdigit() or text[self.pos] == "_"):
                self.pos += 1
        if self.peek() in ("e", "E") and (self.peek(1).isdigit() or (self.peek(1) in "+-" and self.peek(2).isdigit())):
            is_float = True
            self.pos += 2
            while self.pos < len(text) and text[self.pos].isdigit():
                self.pos += 1
        raw = text[start:self.pos].replace("_", "")
        if self.peek().isalpha() or self.peek() == "_":
            self.error(f"invalid number literal '{raw}{self.peek()}'", line, col)
        self.add("FLOAT" if is_float else "INT", float(raw) if is_float else int(raw), line, col)

    def name(self):
        line, col = self.line, self.col()
        start = self.pos
        while self.pos < len(self.text) and (self.text[self.pos].isalnum() or self.text[self.pos] == "_"):
            self.pos += 1
        word = self.text[start:self.pos]
        if word in KEYWORDS:
            self.add("KEYWORD", word, line, col)
            # `karo(` (anonymous function) may open a block inside brackets.
            # `kr(` is tracked too, so a block body after kr gets a clear error.
            if word in ("karo", "kr"):
                self.br_pending = "await-paren"
        else:
            if self.br_pending == "await-paren":
                self.br_pending = None      # `karo name(` is a named function
            self.add("NAME", word, line, col)

    def read_escape(self, quote):
        ch = self.peek(1)
        mapping = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'", "{": "{", "}": "}"}
        if ch in mapping:
            self.pos += 2
            return mapping[ch]
        if ch == "u" and len(self.text) >= self.pos + 6:
            hexpart = self.text[self.pos + 2:self.pos + 6]
            try:
                value = chr(int(hexpart, 16))
            except ValueError:
                self.error("invalid \\u escape")
            self.pos += 6
            return value
        self.error(f"unknown escape sequence '\\{ch}'")

    def string_single(self):
        """'...' strings are literal: no interpolation."""
        line, col = self.line, self.col()
        self.pos += 1
        out = []
        while True:
            if self.pos >= len(self.text) or self.text[self.pos] == "\n":
                self.error("unterminated string", line, col)
            ch = self.text[self.pos]
            if ch == "'":
                self.pos += 1
                break
            if ch == "\\" and self.peek(1) in ("'", "\\"):
                out.append(self.peek(1))
                self.pos += 2
                continue
            out.append(ch)
            self.pos += 1
        self.add("RAWSTRING", "".join(out), line, col)

    def string_double(self):
        """"..." strings support {expr} interpolation. The token value is a list
        of parts: plain str, or (expr_source, line, col) tuples."""
        line, col = self.line, self.col()
        self.pos += 1
        parts = []
        buf = []
        while True:
            if self.pos >= len(self.text) or self.text[self.pos] == "\n":
                self.error("unterminated string", line, col)
            ch = self.text[self.pos]
            if ch == '"':
                self.pos += 1
                break
            if ch == "\\":
                buf.append(self.read_escape('"'))
                continue
            if ch == "{":
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append(self.interpolation())
                continue
            if ch == "}":
                self.error("single '}' in string; write \\} for a literal brace")
            buf.append(ch)
            self.pos += 1
        if buf or not parts:
            parts.append("".join(buf))
        self.add("STRING", parts, line, col)

    def string_triple(self, quote):
        """\"\"\"...\"\"\"\" (interpolated) and '''...''' (literal) strings may span lines.
        A line break right after the opening quotes is dropped, and so is the
        last line when it only holds the closing quotes. The indentation that
        all lines share (the closing line counts too) is removed."""
        line, col = self.line, self.col()
        delim = quote * 3
        self.pos += 3
        chars = []    # (char, line, col)
        text = self.text
        while True:
            if self.pos >= len(text):
                self.error(f"unterminated {delim} string", line, col)
            if text.startswith(delim, self.pos):
                self.pos += 3
                break
            ch = text[self.pos]
            if ch == "\\" and quote == '"' and self.pos + 1 < len(text) and text[self.pos + 1] != "\n":
                chars.append((ch, self.line, self.col()))
                chars.append((text[self.pos + 1], self.line, self.col() + 1))
                self.pos += 2
                continue
            chars.append((ch, self.line, self.col()))
            self.pos += 1
            if ch == "\n":
                self.line += 1
                self.line_start = self.pos
        lines = [[]]
        for c in chars:
            if c[0] == "\n":
                lines.append([])
            else:
                lines[-1].append(c)
        blank = lambda ln: all(c[0] in " \t" for c in ln)
        if len(lines) > 1 and blank(lines[0]):
            lines.pop(0)
        closing = None
        if len(lines) > 1 and blank(lines[-1]):
            closing = lines.pop()
        widths = [len(ln) - len(lstrip_chars(ln)) for ln in lines if not blank(ln)]
        if closing is not None:
            widths.append(len(closing))
        cut = min(widths) if widths else 0
        out = []
        for i, ln in enumerate(lines):
            if i:
                out.append(("\n", ln[0][1] if ln else line, 0))
            out.extend(ln[cut:] if not blank(ln) else [])
        if quote == "'":
            self.add("RAWSTRING", "".join(c[0] for c in out), line, col)
            return
        self.add("STRING", self.scan_interpolated(out, line, col), line, col)

    def scan_interpolated(self, cs, line, col):
        """Escapes and {expr} parts of a dedented \"\"\" string, given as (char, line, col)."""
        mapping = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'", "{": "{", "}": "}"}
        parts, buf = [], []
        i, n = 0, len(cs)
        while i < n:
            ch, ln, cl = cs[i]
            if ch == "\\":
                nxt = cs[i + 1][0] if i + 1 < n else ""
                if nxt in mapping:
                    buf.append(mapping[nxt])
                    i += 2
                    continue
                if nxt == "u" and i + 5 < n:
                    try:
                        buf.append(chr(int("".join(c[0] for c in cs[i + 2:i + 6]), 16)))
                    except ValueError:
                        self.error("invalid \\u escape", ln, cl)
                    i += 6
                    continue
                self.error(f"unknown escape sequence '\\{nxt}'", ln, cl)
            if ch == "{":
                depth, j = 1, i + 1
                while j < n:
                    c = cs[j][0]
                    if c == "\n":
                        break
                    if c in "'\"":
                        j += 1
                        while j < n and cs[j][0] not in (c, "\n"):
                            j += 2 if cs[j][0] == "\\" else 1
                    elif c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if j >= n or cs[j][0] != "}":
                    self.error("unterminated {expression} in string", ln, cl)
                src = "".join(c[0] for c in cs[i + 1:j])
                if not src.strip():
                    self.error("empty {} in string; write \\{ for a literal brace", ln, cl)
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append((src, ln, cl + 1))
                i = j + 1
                continue
            if ch == "}":
                self.error("single '}' in string; write \\} for a literal brace", ln, cl)
            buf.append(ch)
            i += 1
        if buf or not parts:
            parts.append("".join(buf))
        return parts

    def interpolation(self):
        line, col = self.line, self.col()
        self.pos += 1
        start = self.pos
        depth = 1
        while True:
            if self.pos >= len(self.text) or self.text[self.pos] == "\n":
                self.error("unterminated {expression} in string", line, col)
            ch = self.text[self.pos]
            if ch in "'\"":
                # Skip a nested string inside the expression.
                quote = ch
                self.pos += 1
                while self.pos < len(self.text) and self.text[self.pos] not in (quote, "\n"):
                    if self.text[self.pos] == "\\":
                        self.pos += 1
                    self.pos += 1
                self.pos += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    src = self.text[start:self.pos]
                    self.pos += 1
                    if not src.strip():
                        self.error("empty {} in string; write \\{ for a literal brace", line, col)
                    return (src, line, col + 1)
            self.pos += 1

    def open_bracket(self, op, line, col):
        self.brackets.append((op, line, col))
        self.add("OP", op, line, col)
        if self.br_pending == "await-paren" and op == "(":
            self.br_pending = self.depth
        elif self.br_pending == "await-paren":
            self.br_pending = None

    def operator(self):
        line, col = self.line, self.col()
        for op in OPERATORS:
            if self.text.startswith(op, self.pos):
                self.pos += len(op)
                if op in OPENERS:
                    self.open_bracket(op, line, col)
                    return
                if op in CLOSERS:
                    if not self.brackets:
                        self.error(f"unmatched '{op}'", line, col)
                    opener = self.brackets[-1][0]
                    if OPENERS[opener] != op:
                        self.error(f"'{op}' does not match '{opener}' opened on line {self.brackets[-1][1]}", line, col)
                    if len(self.frames) > 1 and self.frames[-1].depth == self.depth:
                        self.error(f"'{op}' closes a bracket while a lambda body is still open", line, col)
                    if self.br_pending == self.depth:
                        self.br_pending = None
                        self.br_header = True
                    self.brackets.pop()
                    self.add("OP", op, line, col)
                    return
                if op == "=>":
                    self.br_header = False
                if self.br_pending == "await-paren":
                    self.br_pending = None
                self.add("OP", op, line, col)
                return
        self.error(f"unexpected character '{self.text[self.pos]}'", line, col)


def lstrip_chars(ln):
    i = 0
    while i < len(ln) and ln[i][0] in " \t":
        i += 1
    return ln[i:]


def tokenize(source):
    return Lexer(source).tokenize()
