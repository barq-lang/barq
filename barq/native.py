"""The native engine: runs a program on the Rust VM in native/ (Phase 6).

Python still reads, parses and checks the file. The checked syntax tree goes
to the `barq-native` program as JSON on stdin; it compiles the tree to Barq
bytecode and runs it. Output and errors look the same as on the other engines.

    barq run --native app.bq        or    BARQ_ENGINE=native barq run app.bq
"""

import json
import os
import shutil
import subprocess
import sys

from . import nodes as N

FORMAT = 3   # bumped when the JSON the Rust side reads changes shape

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKOUT = os.path.dirname(HERE)


def to_json(value):
    """A syntax tree as plain JSON data: a node is {"k": kind, "l": line, "c": col, ...fields}."""
    if isinstance(value, N.Node):
        out = {"k": type(value).__name__, "l": value.line, "c": value.col}
        for f in value._fields:
            v = getattr(value, f)
            if v is not None:
                out[f] = to_json(v)
        return out
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        if not -2 ** 63 <= value < 2 ** 63:
            return {"k": "BigInt", "v": str(value)}
        return value
    if isinstance(value, float):
        return {"k": "Float", "v": repr(value)}   # keeps 1.0 apart from 1
    raise TypeError(f"cannot export {type(value).__name__} to the native VM")


def collect_modules(interp, body, root):
    """Return all transitively-used user-defined modules in topological order (deps first).

    Each entry is (display, abs_path, source_text, program_tree).  Stdlib modules
    (like `use Barq\\Json`) are skipped — the native VM reports them as unsupported.
    """
    from . import nodes as N
    seen = {}  # abs_path -> index in result
    result = []

    def visit(stmts):
        for stmt in stmts:
            if not isinstance(stmt, N.Use):
                continue
            for fqn, _alias in stmt.imports:
                parts = fqn.split("\\")
                abs_path = interp.find_module_file(parts)
                if abs_path is None or abs_path in seen:
                    continue
                seen[abs_path] = len(result)
                try:
                    with open(abs_path, encoding="utf-8") as f:
                        text = f.read()
                    display = os.path.relpath(abs_path, root)
                    from .errors import SourceFile as SF
                    source = SF(display, text)
                    program = interp.parse_and_check(source)
                    result.append((display, abs_path, text, program))
                    visit(program.body)  # recurse for transitive deps
                except Exception:
                    pass  # checker already validated; skip bad imports silently

    visit(body)
    return result


def _barq_val_to_json(val):
    """Convert a Python-interpreter runtime value to a JSON-serializable Python value."""
    if val is None or isinstance(val, (bool, str)):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return val
    if isinstance(val, list):
        return [_barq_val_to_json(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _barq_val_to_json(v) for k, v in val.items()}
    # Fallback: try to coerce to a simple type
    try:
        return str(val)
    except Exception:
        return None


def load_config_data(interp, root):
    """Load config/*.bq and config/*.json under root into a JSON-serializable dict."""
    folder = os.path.join(root, "config")
    data = {}
    if not os.path.isdir(folder):
        return data
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        stem, ext = os.path.splitext(name)
        if ext == ".json":
            try:
                with open(path, encoding="utf-8") as f:
                    data[stem] = json.load(f)
            except (OSError, ValueError):
                pass
        elif ext == ".bq":
            try:
                mod = interp.load_module(path)
                env = mod.env.vars
                if stem in env:
                    data[stem] = _barq_val_to_json(env[stem][0])
                else:
                    values = {k: _barq_val_to_json(v[0]) for k, v in env.items()
                              if k not in getattr(mod, "functions", {}) and k not in getattr(mod, "classes", {})}
                    data[stem] = values
            except Exception:
                pass
    return data


def export(program, source, path, modules=(), config=None):
    mods = []
    for display, abs_path, text, mod_program in modules:
        mods.append({
            "file": abs_path,
            "display": display,
            "source": text,
            "program": to_json(mod_program),
        })
    bundle = {
        "format": FORMAT,
        "path": source.path,
        "file": os.path.abspath(path),
        "source": source.text,
        "program": to_json(program),
        "modules": mods,
    }
    if config is not None:
        bundle["config"] = config
    return json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))


def find_binary():
    """The barq-native program: $BARQ_NATIVE, a build in this checkout, or one on PATH."""
    env = os.environ.get("BARQ_NATIVE")
    if env:
        return env
    exe = "barq-native.exe" if os.name == "nt" else "barq-native"
    for kind in ("release", "debug"):
        p = os.path.join(CHECKOUT, "native", "target", kind, exe)
        if os.path.isfile(p):
            return p
    for p in (os.path.join(CHECKOUT, "runtime", exe), shutil.which(exe)):
        if p and os.path.isfile(p):
            return p
    return None


def run(path, argv, cache=True):
    """barq run --native: returns the exit code.

    The checked tree is cached next to the VM's .bqc files, as
    .barq/cache/<file>.bqn: its first line names the Barq version and the
    SHA-256 of the source, so an unchanged file is not read again by Python."""
    import hashlib
    from . import __version__
    binary = find_binary()
    if binary is None:
        sys.stderr.write("barq: the native VM is not built — run: cargo build --release --manifest-path "
                         f"{os.path.join(CHECKOUT, 'native', 'Cargo.toml')}\n")
        return 2
    apath = os.path.abspath(path)
    root = os.path.dirname(apath)
    with open(apath, encoding="utf-8") as f:
        text = f.read()
    display = os.path.relpath(apath, root)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    header = f"#barq-native {__version__} f{FORMAT} {digest} {display}\n"
    use_cache = cache and not os.environ.get("BARQ_NO_CACHE")
    cached = os.path.join(root, ".barq", "cache", display[:-3] + ".bqn" if display.endswith(".bq") else display + ".bqn")
    tree = None
    if use_cache:
        try:
            with open(cached, encoding="utf-8") as f:
                if f.readline() == header:
                    tree = cached
        except OSError:
            pass
    temp = None
    if tree is None:
        from .errors import BarqError, SourceFile, report
        from .interpreter import Interpreter
        interp = Interpreter(root=root, argv=argv)
        source = SourceFile(display, text)
        try:
            program = interp.parse_and_check(source)
        except BarqError as e:
            report(e)
            return 1
        modules = collect_modules(interp, program.body, root)
        config = load_config_data(interp, root)
        data = header + export(program, source, apath, modules=modules, config=config)
        if use_cache:
            try:
                os.makedirs(os.path.dirname(cached), exist_ok=True)
                tmp = f"{cached}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, cached)
                tree = cached
            except OSError:
                tree = None
        if tree is None:
            import tempfile
            fd, temp = tempfile.mkstemp(prefix="barq-", suffix=".bqn")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            tree = temp
    sys.stdout.flush()
    env = dict(os.environ)
    if "NO_COLOR" not in env and sys.stderr.isatty():
        env["BARQ_COLOR"] = "1"
    try:
        proc = subprocess.Popen([binary, tree, *argv], env=env)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.kill()
            sys.stderr.write("\nStopped.\n")
            return 130
    finally:
        if temp is not None:
            os.unlink(temp)
