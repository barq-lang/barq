"""Error types used by every stage of the Barq toolchain."""

import os
import sys


class SourceFile:
    """A loaded .bq file. Keeps the text so errors can show the failing line."""

    def __init__(self, path, text):
        self.path = path
        # Windows and Unix line endings are both accepted.
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = self.text.split("\n")

    def line(self, n):
        if 1 <= n <= len(self.lines):
            return self.lines[n - 1]
        return ""


class BarqError(Exception):
    """Base for errors reported before or outside of Barq-level exceptions."""

    kind = "Error"

    def __init__(self, message, source=None, line=0, col=0):
        super().__init__(message)
        self.message = message
        self.source = source
        self.line = line
        self.col = col


class LexError(BarqError):
    kind = "SyntaxError"


class ParseError(BarqError):
    kind = "SyntaxError"


class CheckError(BarqError):
    kind = "CompileError"


class CheckErrors(BarqError):
    """Several compile errors found in one file."""

    kind = "CompileError"

    def __init__(self, errors):
        super().__init__(errors[0].message, errors[0].source, errors[0].line, errors[0].col)
        self.errors = errors


def _use_color(stream):
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def format_location(source, line, col, color=False):
    """Render `file:line` plus the source line with a caret under the column."""
    red = "\033[31m" if color else ""
    dim = "\033[2m" if color else ""
    reset = "\033[0m" if color else ""
    out = []
    path = source.path if source else "<unknown>"
    out.append(f"  {dim}at{reset} {path}:{line}" + (f":{col}" if col else ""))
    if source and line:
        text = source.line(line)
        if text.strip():
            gutter = f"{line:>4} | "
            out.append(f"{dim}{gutter}{reset}{text}")
            if col:
                out.append(" " * (len(gutter) + col - 1) + f"{red}^{reset}")
    return "\n".join(out)


def report(err, stream=None):
    """Print a BarqError in the standard format."""
    stream = stream or sys.stderr
    if isinstance(err, CheckErrors):
        for e in err.errors:
            report(e, stream)
        n = len(err.errors)
        stream.write(f"\n{n} error{'s' if n != 1 else ''} found — nothing was run.\n")
        return
    color = _use_color(stream)
    bold_red = "\033[1;31m" if color else ""
    reset = "\033[0m" if color else ""
    stream.write(f"{bold_red}{err.kind}:{reset} {err.message}\n")
    if err.source is not None or err.line:
        stream.write(format_location(err.source, err.line, err.col, color) + "\n")
