"""Interactive shell: `barq repl`."""

import os
import re
import sys

from . import __version__
from .errors import BarqError, report
from .runtime import BarqThrow, ExitSignal, to_display

BLOCK_STARTERS = ("if ", "if(", "for ", "foreach ", "while ", "do", "switch", "try", "class ", "enum ", "abstract ",
                  "interface ", "trait ", "karo ", "#[")
TRAILING_OP = re.compile(r"([-+*/%=?<]|\b(and|or|not|in|is))$")   # the line carries on (see lexer.CONTINUES)


def open_string(text):
    """True while a \"\"\" or ''' string is still open."""
    return text.count('"""') % 2 == 1 or text.count("'''") % 2 == 1


def needs_more(line):
    s = line.strip()
    if s.startswith(BLOCK_STARTERS) or s.endswith(("(", "#[", "#{", "#(", ",")):
        return True
    if TRAILING_OP.search(s) or open_string(s):
        return True
    return s.count("(") > s.count(")")


def repl():
    from .cli import report_uncaught
    from .interpreter import Interpreter
    from . import nodes as N
    from .parser import Parser
    from .lexer import tokenize
    from .errors import SourceFile
    from .runtime import Env, Module

    try:
        import readline  # noqa: F401  (arrow keys and history where available)
    except ImportError:
        pass

    interp = Interpreter(root=os.getcwd())
    module = Module("<repl>", SourceFile("<repl>", ""), Env(interp.builtins))
    module.imports = {}
    interp.module = module
    print(f"Barq {__version__} — type an expression or statement. Blocks end with an empty line. 'exit' to quit.")
    while True:
        try:
            line = input("barq> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.strip() in ("exit", "quit", "exit()"):
            return 0
        if not line.strip():
            continue
        lines = [line]
        if needs_more(line):
            while True:
                try:
                    more = input("  ... ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not more.strip() and not open_string("\n".join(lines)):
                    break
                lines.append(more)
        text = "\n".join(lines) + "\n"
        source = SourceFile("<repl>", text)
        module.source = source
        try:
            program = Parser(tokenize(source), source).parse_program()
            body = program.body
            interp.setup_module(module, [s for s in body if isinstance(s, (N.FuncDecl, N.ClassDecl, N.Use))])
            for stmt in body:
                if isinstance(stmt, (N.FuncDecl, N.ClassDecl, N.Use, N.Namespace)):
                    continue
                if isinstance(stmt, N.ExprStmt):
                    value = interp.eval(stmt.expr, module.env)
                    # Show results, but not the null returned by print() and other void calls.
                    if value is not None or not isinstance(stmt.expr, N.Call):
                        print(to_display(value, interp, quote=True))
                else:
                    interp.exec(stmt, module.env)
        except ExitSignal as e:
            return e.code
        except BarqThrow as t:
            sys.stdout.flush()
            report_uncaught(t)
        except BarqError as e:
            sys.stdout.flush()
            report(e)
        sys.stdout.flush()
