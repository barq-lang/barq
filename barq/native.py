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

FORMAT = 1   # bumped when the JSON the Rust side reads changes shape

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


def export(program, source, path):
    return json.dumps({
        "format": FORMAT,
        "path": source.path,
        "file": os.path.abspath(path),
        "source": source.text,
        "program": to_json(program),
    }, ensure_ascii=False, separators=(",", ":"))


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
        data = header + export(program, source, apath)
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
