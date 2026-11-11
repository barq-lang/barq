"""The `barq` command-line tool."""

import os
import sys
import threading

from . import __version__
from .errors import BarqError, SourceFile, format_location, report, _use_color
from .runtime import BarqThrow, ExitSignal, to_display

USAGE = f"""Barq {__version__} — reference interpreter

Usage:
  barq run <file.bq> [args...]   Run a Barq program (compiled, on the VM)
  barq <file.bq> [args...]       Same as 'barq run'
  barq compile <file.bq|dir>...  Check and compile files into .barq/cache/ without running them
  barq test [files or dirs...]   Run tests (default: tests/ — files named *_test.bq, *Test.bq, test_*.bq)
  barq repl                      Start the interactive shell
  barq fmt [files or dirs...]    Format .bq files in place (--check, --diff, -  for stdin)

Project commands (run in the project folder):
  barq serve                     Serve routes/web.bq and routes/api.bq (under /api) on
                                 http://127.0.0.1:8000 — restarts when a file changes
                                 (--port N, --host H, --no-reload)
  barq route:list                Print every route
  barq key:generate              Put a random APP_KEY in .env (--show, --force)
  barq storage:link              Link public/storage to storage/app/public
  barq packages                  List the installed Barq packages
{{packages}}
  barq tokens <file.bq>          Print the tokens of a file (debugging)
  barq ast <file.bq>             Print the syntax tree of a file (debugging)
  barq emit <file.bq>            Print the Python code the compiler makes (debugging)
  barq check <file.bq|dir>...    Report syntax and type errors without running (--json)
  barq version                   Print the version

Options for run and test:
  --interp      Use the tree-walking interpreter instead of the VM
  --no-cache    Don't read or write .bqc cache files
"""


def usage():
    from . import hooks, plugins
    try:
        plugins.load()
    except RuntimeError as e:
        sys.stderr.write(f"barq: {e}\n")
    extra = "".join(hooks.USAGE)
    if not extra:
        extra = ("\n  (install barq-framework, barq-auth or barq-permissions for migrate, make:*,\n"
                 "   schedule:*, auth:install and permissions:* — see https://github.com/barq-lang)\n")
    return USAGE.replace("{packages}", extra)


def report_uncaught(thrown, stream=sys.stderr):
    obj = thrown.obj
    color = _use_color(stream)
    red = "\033[1;31m" if color else ""
    dim = "\033[2m" if color else ""
    reset = "\033[0m" if color else ""
    message = obj.fields.get("message", "")
    stream.write(f"{red}Uncaught {obj.cls.name}:{reset} {to_display(message)}\n")
    if obj.origin:
        source, line, col = obj.origin
        stream.write(format_location(source, line, col, color) + "\n")
    lines = []
    for name, module, line in reversed(obj.trace or []):
        path = module.source.path if module is not None else "<unknown>"
        lines.append(f"  {dim}in{reset} {name}() {dim}called from{reset} {path}:{line}\n")
    # Deep recursion repeats one line thousands of times: show it a few times.
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and lines[j] == lines[i]:
            j += 1
        run = j - i
        stream.write("".join(lines[i:i + min(run, 3)]))
        if run > 3:
            stream.write(f"  {dim}... the same call {run - 3} more times{reset}\n")
        i = j


def run_guarded(fn):
    """Run fn() and turn Barq errors into messages and exit codes. Returns the exit code."""
    try:
        fn()
        return 0
    except ExitSignal as e:
        return e.code
    except BarqThrow as t:
        sys.stdout.flush()
        report_uncaught(t)
        return 1
    except BarqError as e:
        sys.stdout.flush()
        report(e)
        return 1
    except RecursionError:
        sys.stdout.flush()
        sys.stderr.write("RuntimeError: Python recursion limit reached (program nests too deeply)\n")
        return 1


def with_big_stack(fn, limit=200000):
    """Deep Barq recursion needs a deep Python stack, so run in a thread with one."""
    result = []
    threading.stack_size(512 * 1024 * 1024)
    sys.setrecursionlimit(limit)
    # A daemon thread, so Ctrl+C (which Python delivers to the main thread) ends the program.
    t = threading.Thread(target=lambda: result.append(fn()), daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.25)
    except KeyboardInterrupt:
        sys.stdout.flush()
        sys.stderr.write("\nStopped.\n")
        return 130
    return result[0] if result else 1


def cmd_run(path, args, engine=None, cache=True):
    if not os.path.isfile(path):
        sys.stderr.write(f"barq: file not found: {path}\n")
        return 2
    root = os.path.dirname(os.path.abspath(path))
    engine = engine or os.environ.get("BARQ_ENGINE", "vm")
    if engine == "tree":
        from .interpreter import Interpreter
        interp = Interpreter(root=root, argv=args)
        limit = 200000
    else:
        from .vm import VM, MAX_DEPTH
        interp = VM(root=root, argv=args, cache=cache)
        # Compiled Barq calls use 1-3 Python frames each.
        limit = MAX_DEPTH * 3
    from .project import load_dotenv
    if run_guarded(lambda: load_dotenv(interp)) != 0:
        return 1
    from .project import boot

    def go():
        boot(interp)
        interp.run_file(path)
    code = with_big_stack(lambda: run_guarded(go), limit)
    sys.stdout.flush()
    return code


def bq_files(paths):
    for p in paths:
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "views")
                for name in sorted(filenames):
                    if name.endswith(".bq"):
                        yield os.path.join(dirpath, name)
        else:
            yield p


def cmd_compile(paths):
    """Parse, check and compile files into the cache, without running them."""
    from .vm import VM
    failed = 0
    count = 0
    for given in paths:
        # The project root is the folder given, or the folder of the file given.
        root = os.path.abspath(given if os.path.isdir(given) else os.path.dirname(given) or ".")
        for path in bq_files([given]):
            if not os.path.isfile(path):
                sys.stderr.write(f"barq: file not found: {path}\n")
                failed += 1
                continue
            vm = VM(root=root)
            if run_guarded(lambda: vm.compile_file(path)) != 0:
                failed += 1
            else:
                count += 1
    print(f"compiled {count} file{'s' if count != 1 else ''}" + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


def is_test_file(name):
    stem = name[:-3]
    return name.endswith(".bq") and (stem.endswith("_test") or stem.endswith("Test") or stem.startswith("test_"))


def cmd_test(paths, engine=None, cache=True):
    """Run every test file in a fresh interpreter; tests registered with
    Test.it() and not run by the file itself are run after it."""
    import time
    from .stdlib3 import run_tests, summary_line, test_state, use_color
    root = os.getcwd()
    if not paths:
        paths = ["tests"]
    files = []
    for p in paths:
        if not os.path.exists(p):
            sys.stderr.write(f"barq: not found: {p}\n")
            return 2
        if os.path.isdir(p):
            files.extend(f for f in bq_files([p]) if is_test_file(os.path.basename(f)))
        else:
            files.append(p)
    if not files:
        sys.stderr.write(f"barq: no test files found in {', '.join(paths)} "
                         "(names must end in _test.bq or Test.bq, or start with test_)\n")
        return 1
    engine = engine or os.environ.get("BARQ_ENGINE", "vm")
    color = use_color(sys.stdout)
    bold, red, reset = ("\033[1m", "\033[31m", "\033[0m") if color else ("", "", "")
    start = time.perf_counter()
    totals = [0, 0, 0]
    broken = []
    failures = []

    def run_all():
        for path in files:
            if engine == "tree":
                from .interpreter import Interpreter
                interp = Interpreter(root=root, argv=[])
            else:
                from .vm import VM
                interp = VM(root=root, argv=[], cache=cache)
            from .project import boot, load_dotenv, project_routes
            run_guarded(lambda: load_dotenv(interp))
            print(f"{bold}{os.path.relpath(path, root)}{reset}")
            code = run_guarded(lambda: (boot(interp), project_routes(interp), interp.run_file(path)))
            if code == 0:
                code = run_guarded(lambda: run_tests(interp))
            st = test_state(interp)
            for k in range(3):
                totals[k] += st.totals[k]
            failures.extend((os.path.relpath(path, root), *f) for f in st.failures)
            if code != 0:
                broken.append(os.path.relpath(path, root))
            sys.stdout.flush()
        return 0

    from .vm import MAX_DEPTH
    with_big_stack(run_all, MAX_DEPTH * 3)
    ms = (time.perf_counter() - start) * 1000
    print()
    if failures:
        print(f"{red}Failures:{reset}")
        for path, name, head, where in failures:
            print(f"  {path} › {name}")
            first = head.splitlines()[0] if head else ""
            print(f"      {first}" + (f"  ({where})" if where else ""))
        print()
    line = summary_line(*totals, color)
    if broken:
        line += f", {red}{len(broken)} file{'s' if len(broken) != 1 else ''} crashed{reset}"
    print(f"Tests: {line}  ({len(files)} file{'s' if len(files) != 1 else ''}, {ms:.0f} ms)")
    return 1 if totals[1] or broken else 0


def cmd_emit(path):
    import ast
    from .vm import VM
    vm = VM(root=os.path.dirname(os.path.abspath(path)), cache=False)
    return run_guarded(lambda: print(ast.unparse(vm.compile_file(path, emit=True))))


def cmd_tokens(path):
    from .lexer import tokenize
    source = SourceFile(path, open(path, encoding="utf-8").read())

    def go():
        for tok in tokenize(source):
            print(f"{tok.line:>4}:{tok.col:<4} {tok.type:<10} {tok.value!r}")
    return run_guarded(go)


def cmd_ast(path):
    from .parser import parse
    from . import nodes as N
    source = SourceFile(path, open(path, encoding="utf-8").read())

    def show(node, indent=0):
        pad = "  " * indent
        if isinstance(node, list):
            for x in node:
                show(x, indent)
            return
        if isinstance(node, tuple):
            for x in node:
                show(x, indent)
            return
        if not isinstance(node, N.Node):
            print(f"{pad}{node!r}")
            return
        simple = {f: getattr(node, f) for f in node._fields
                  if not isinstance(getattr(node, f), (N.Node, list, tuple)) or isinstance(getattr(node, f), N.TypeRef)}
        label = ", ".join(f"{k}={v.name if isinstance(v, N.TypeRef) else v!r}" for k, v in simple.items())
        print(f"{pad}{type(node).__name__}({label})  @{node.line}")
        for f in node._fields:
            v = getattr(node, f)
            if isinstance(v, (N.Node, list, tuple)) and not isinstance(v, N.TypeRef) and v:
                print(f"{pad}  .{f}:")
                show(v, indent + 2)

    return run_guarded(lambda: show(parse(source).body))


def cmd_repl():
    from .repl import repl
    return with_big_stack(repl)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0
    cmd = argv[0]
    if cmd in ("version", "-v", "--version"):
        print(f"barq {__version__}")
        return 0
    if cmd == "packages":
        return cmd_packages()
    if cmd == "check":
        from .check import cmd_check
        return cmd_check(argv[1:])
    if cmd == "repl":
        return cmd_repl()
    if cmd == "run" or cmd.startswith("--"):
        rest = argv[1:] if cmd == "run" else argv
        engine, cache = None, True
        while rest and rest[0].startswith("--"):
            flag = rest.pop(0)
            if flag == "--interp":
                engine = "tree"
            elif flag == "--vm":
                engine = "vm"
            elif flag == "--no-cache":
                cache = False
            else:
                sys.stderr.write(f"barq: unknown option '{flag}'\n")
                return 2
        if not rest:
            sys.stderr.write("usage: barq run [--interp] [--no-cache] <file.bq>\n")
            return 2
        return cmd_run(rest[0], rest[1:], engine, cache)
    if cmd == "test":
        rest = argv[1:]
        engine, cache = None, True
        while rest and rest[0].startswith("--"):
            flag = rest.pop(0)
            if flag == "--interp":
                engine = "tree"
            elif flag == "--vm":
                engine = "vm"
            elif flag == "--no-cache":
                cache = False
            else:
                sys.stderr.write(f"barq: unknown option '{flag}'\n")
                return 2
        return cmd_test(rest, engine, cache)
    if cmd == "compile":
        if len(argv) < 2:
            sys.stderr.write("usage: barq compile <file.bq|dir>...\n")
            return 2
        return cmd_compile(argv[1:])
    if cmd == "fmt":
        from .fmt import cmd_fmt
        return cmd_fmt(argv[1:])
    if cmd == "emit":
        if len(argv) < 2:
            sys.stderr.write("usage: barq emit <file.bq>\n")
            return 2
        return cmd_emit(argv[1])
    if cmd in ("tokens", "ast"):
        if len(argv) < 2:
            sys.stderr.write(f"usage: barq {cmd} <file.bq>\n")
            return 2
        return cmd_tokens(argv[1]) if cmd == "tokens" else cmd_ast(argv[1])
    if cmd == "serve":
        from .project import cmd_serve
        return cmd_serve(argv[1:])
    if cmd == "route:list":
        from .project import route_list, run_in_project
        return run_in_project(route_list, "tree" if "--interp" in argv else None)
    if cmd == "key:generate":
        from .project import cmd_key_generate
        return cmd_key_generate(argv[1:])
    if cmd == "storage:link":
        from .project import cmd_storage_link
        return cmd_storage_link(argv[1:])
    if ":" in cmd or not cmd.endswith(".bq"):
        from . import hooks, plugins
        try:
            plugins.load()
        except RuntimeError as e:
            sys.stderr.write(f"barq: {e}\n")
            return 1
        fn = hooks.find_command(cmd)
        if fn is not None:
            return fn(cmd, argv[1:])
    if cmd.endswith(".bq") or os.path.isfile(cmd):
        return cmd_run(cmd, argv[1:])
    hint = ""
    if cmd.split(":")[0] in ("migrate", "db", "make", "schedule"):
        hint = " (it comes with barq-framework: https://github.com/barq-lang/barq-framework)"
    elif cmd.startswith(("auth:", "permissions:")):
        pkg = "barq-auth" if cmd.startswith("auth:") else "barq-permissions"
        hint = f" (it comes with {pkg}: https://github.com/barq-lang/{pkg})"
    sys.stderr.write(f"barq: unknown command '{cmd}'{hint}\n\n{usage()}")
    return 2


def cmd_packages():
    from . import plugins
    try:
        found = plugins.describe()
    except RuntimeError as e:
        sys.stderr.write(f"barq: {e}\n")
        return 1
    if not found:
        print("No Barq packages installed. Clone barq-framework, barq-auth or barq-permissions next to this\n"
              "barq checkout, or list their folders in BARQ_PATH.")
        return 0
    for name, version in found:
        print(f"{name} {version}".rstrip())
    return 0
