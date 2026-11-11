"""`barq check` — find syntax and compile errors without running anything.

    barq check app/ main.bq          every .bq file (views/ skipped)
    barq check --json main.bq        one JSON object per line, for editors
    barq check --json --path f.bq -  read the text from stdin (an unsaved editor buffer);
                                     --path names the file so `use` imports resolve

JSON lines look like:
    {"path": "main.bq", "line": 3, "col": 5, "endCol": 9, "kind": "CompileError", "message": "..."}
"""

import json
import os
import re
import sys

from .errors import BarqError, CheckErrors, SourceFile, report


def project_root(text, path):
    """The folder `use` paths resolve from. A file declaring `namespace App\\Models`
    that lives in .../app/Models/ has its root two folders up; otherwise it is
    the file's own folder."""
    folder = os.path.dirname(os.path.abspath(path))
    m = re.search(r"^namespace\s+([A-Za-z_][\w\\]*)", text, re.M)
    if m:
        parts = m.group(1).split("\\")
        tail = folder.split(os.sep)
        if len(tail) > len(parts) and [t.lower() for t in tail[-len(parts):]] == [x.lower() for x in parts]:
            return os.sep.join(tail[:-len(parts)]) or os.sep
        # a package's src/ file: its root is the folder holding barq.pkg
        from .pkg import MANIFEST
        d = folder
        for _ in range(len(parts) + 2):
            if os.path.isfile(os.path.join(d, MANIFEST)):
                return d
            d = os.path.dirname(d)
    return folder


def check_text(text, path, root=None):
    """List of BarqError for one file's text (empty when it is fine)."""
    from .interpreter import Interpreter
    from .parser import parse
    from .checker import check
    root = root or project_root(text, path)
    source = SourceFile(path, text)
    try:
        program = parse(source)
        interp = Interpreter(root=root, argv=[])
        return list(check(program, source, set(interp.builtins.vars), interp.find_module_file) or [])
    except CheckErrors as e:
        return list(e.errors)
    except BarqError as e:
        return [e]


def word_end(source, line, col):
    """Column just past the word at line:col, so editors underline a word, not one character."""
    text = source.line(line) if source is not None else ""
    k = max(col - 1, 0)
    if k >= len(text):
        return col + 1
    end = k
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    return (end if end > k else k + 1) + 1


def as_json(e, path):
    line = max(e.line or 1, 1)
    col = max(e.col or 1, 1)
    return json.dumps({"path": path, "line": line, "col": col, "endCol": word_end(e.source, line, col),
                       "kind": e.kind, "message": e.message}, ensure_ascii=False)


def cmd_check(argv):
    use_json = False
    path_name = None
    paths = []
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--json":
            use_json = True
        elif a == "--path":
            path_name = args.pop(0) if args else None
        elif a in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        elif a.startswith("--"):
            sys.stderr.write(f"barq check: unknown option '{a}'\n")
            return 2
        else:
            paths.append(a)
    if not paths:
        paths = ["."]

    from .cli import bq_files, with_big_stack
    from .vm import MAX_DEPTH
    found = []

    def run():
        if paths == ["-"]:
            name = path_name or "<stdin>"
            found.append((name, check_text(sys.stdin.read(), name)))
            return 0
        for given in paths:
            if not os.path.exists(given):
                sys.stderr.write(f"barq check: not found: {given}\n")
                found.append((given, None))
                continue
            for path in bq_files([given]):
                with open(path, encoding="utf-8") as f:
                    found.append((path, check_text(f.read(), path)))
        return 0

    with_big_stack(run, MAX_DEPTH * 3)
    count = 0
    for path, errors in found:
        if errors is None:
            count += 1
            continue
        for e in errors:
            count += 1
            if use_json:
                print(as_json(e, path))
            else:
                report(e)
    if not use_json:
        files = len(found)
        if count:
            sys.stderr.write(f"\n{count} error{'s' if count != 1 else ''} in {files} file{'s' if files != 1 else ''}\n")
        else:
            print(f"{files} file{'s' if files != 1 else ''} checked, no errors")
    return 1 if count else 0
