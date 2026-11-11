"""barq build — a folder you can copy to a server and run.

    dist/
      app/          the project: its code, views, config, public/, vendor/...
        .barq/cache/  every .bq file already checked and compiled
      runtime/      this Barq and the packages it loaded (barq-framework...)
      barq          runs any barq command on app/ (barq.cmd on Windows)
      start         runs the app: serves it (web) or runs main.bq (console)
      build.json    what went in: versions, packages, entry

The server needs Python 3 and nothing else. Left out by default: tests,
.env (secrets), databases, what is in storage/ (the folders are kept), .git,
.barq and node_modules. barq.json can add a "build" section:

    "build": {"entry": "main.bq", "exclude": ["docs"], "include": ["tests"]}
"""

import datetime
import fnmatch
import json
import os
import shutil
import stat
import sys
import zipfile

from . import __version__

HELP = """usage: barq build [options]

Makes a folder to deploy: the project with every file compiled, plus this Barq
and its packages, and scripts to start it. The server only needs Python 3.

  --out DIR         Where to put it (default: dist)
  --zip             Also pack it into DIR.zip
  --entry FILE      The program start runs (default: serve routes/ if there
                    are any, else main.bq)
  --exclude PATTERN Leave out more files or folders (can repeat)
  --include PATTERN Keep something left out by default, e.g. tests (can repeat)
  --with-env        Copy .env too (it usually holds secrets; by default the
                    server brings its own .env or environment variables)
  --force           Replace DIR even if barq build didn't make it

Patterns without a / match a name anywhere (docs, *.log); with a / they match
a path from the project root (database/*.db). barq.json can hold the same as
"build": {"entry": ..., "exclude": [...], "include": [...]}.
"""

MANIFEST = "build.json"
DEFAULT_EXCLUDE = [".git", ".gitignore", ".github", ".barq", "node_modules", "__pycache__", ".DS_Store",
                   "tests", ".env", "*.db", "*.sqlite", "*.sqlite3", "*.pyc"]
SKELETON = ("storage",)     # folders copied empty (only .gitkeep files)
ROUTES = (os.path.join("routes", "web.bq"), os.path.join("routes", "api.bq"), os.path.join("routes", "auth.bq"))


class BuildError(Exception):
    pass


# =========================================================================
# Choosing the files
# =========================================================================

def _match(pattern, rel, name):
    pattern = pattern.strip().rstrip("/")
    if not pattern:
        return False
    if "/" in pattern:
        return fnmatch.fnmatchcase(rel, pattern.lstrip("/"))
    return fnmatch.fnmatchcase(name, pattern)


class Picker:
    def __init__(self, exclude, include, skip_dirs):
        self.exclude = exclude
        self.include = include
        self.skip_dirs = {os.path.abspath(d) for d in skip_dirs}

    def included(self, rel, name):
        return any(_match(p, rel, name) for p in self.include)

    def keep(self, rel, name):
        if self.included(rel, name):
            return True
        return not any(_match(p, rel, name) for p in self.exclude)

    def leads_to_include(self, rel):
        """An excluded folder is still entered when an --include names something inside it."""
        return any("/" in p and p.strip().lstrip("/").startswith(rel + "/") for p in self.include)

    def files(self, root):
        """(relative path, absolute path, kind) for everything to copy; kind is
        'dir', 'file' or 'link'."""
        out = []

        def walk(folder, rel_folder, skeleton):
            try:
                entries = sorted(os.scandir(folder), key=lambda e: e.name)
            except OSError as e:
                raise BuildError(f"cannot read {folder}: {e.strerror}") from None
            for e in entries:
                rel = f"{rel_folder}/{e.name}" if rel_folder else e.name
                if os.path.abspath(e.path) in self.skip_dirs:
                    continue
                kept = self.keep(rel, e.name)
                if e.is_symlink():
                    if kept and not skeleton:
                        out.append((rel, e.path, "link"))
                    continue
                if e.is_dir():
                    in_skeleton = skeleton or (not rel_folder and e.name in SKELETON and not self.included(rel, e.name))
                    if kept:
                        out.append((rel, e.path, "dir"))
                        walk(e.path, rel, in_skeleton)
                    elif self.leads_to_include(rel):
                        walk(e.path, rel, in_skeleton)
                elif e.is_file() and kept:
                    if skeleton and e.name != ".gitkeep" and not self.included(rel, e.name):
                        continue
                    out.append((rel, e.path, "file"))

        walk(root, "", False)
        return out


def copy_files(picked, src_root, dest):
    count = 0
    for rel, path, kind in picked:
        target = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if kind == "dir":
            os.makedirs(target, exist_ok=True)
        elif kind == "file":
            shutil.copy2(path, target)
            count += 1
        else:
            link = os.readlink(path)
            if os.path.isabs(link):
                real = os.path.realpath(path)
                if real != src_root and not real.startswith(src_root + os.sep):
                    raise BuildError(f"{rel} links outside the project ({link}); "
                                     f"exclude it with --exclude {rel} or replace it with a copy")
                link = os.path.relpath(real, os.path.dirname(os.path.realpath(os.path.join(src_root, rel))))
            os.symlink(link, target)
    return count


# =========================================================================
# The runtime: this Barq and the packages it loaded
# =========================================================================

def _copy_package(src, dest):
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _license(pkg_dir, dest_dir):
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
        p = os.path.join(os.path.dirname(pkg_dir), name)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dest_dir, name))
            return


def copy_runtime(dest, project):
    """Copy barq/ and every loaded package that isn't in the project's vendor/.
    Returns the package list for build.json."""
    from . import plugins
    core = os.path.dirname(os.path.abspath(__file__))
    home = os.path.join(dest, "barq")
    _copy_package(core, os.path.join(home, "barq"))
    _license(core, home)
    packages = []
    for mod in plugins.load():
        pkg_dir = os.path.dirname(os.path.abspath(mod.__file__))
        name = getattr(mod, "NAME", mod.__name__)
        entry = {"name": name, "version": getattr(mod, "__version__", ""), "module": mod.__name__}
        if pkg_dir.startswith(project + os.sep):
            entry["from"] = "vendor"         # it travels inside app/vendor/
        else:
            folder = os.path.join(dest, name.replace("_", "-"))
            _copy_package(pkg_dir, os.path.join(folder, mod.__name__))
            _license(pkg_dir, folder)
            entry["from"] = "runtime"
        packages.append(entry)
    import compileall
    compileall.compile_dir(dest, quiet=1)
    return packages


# =========================================================================
# Compiling and checking
# =========================================================================

def compile_app(app):
    """Check and compile every .bq file (templates in views/ aside). Returns
    (compiled, failed paths)."""
    from .cli import run_guarded
    from .vm import VM
    done, failed = 0, []
    for dirpath, dirnames, filenames in os.walk(app):
        rel_dir = os.path.relpath(dirpath, app)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "node_modules"
                             and not (d == "views" and rel_dir == "."))
        for name in sorted(filenames):
            if not name.endswith(".bq"):
                continue
            path = os.path.join(dirpath, name)
            if os.sep + "views" + os.sep in os.sep + os.path.relpath(path, app):
                continue      # a package's views/ are templates too
            vm = VM(root=app)
            vm.use_cache = True      # even with BARQ_NO_CACHE: the cache is what is shipped
            if run_guarded(lambda: vm.compile_file(path)) != 0:
                failed.append(os.path.relpath(path, app))
            else:
                done += 1
    return done, failed


def check_vendor(base):
    """vendor/ must hold what barq.lock pins."""
    from . import pkg
    project = pkg.read_json(os.path.join(base, pkg.PROJECT), pkg.PROJECT) or {}
    require = project.get("require") or {}
    lock = (pkg.read_json(os.path.join(base, pkg.LOCK), pkg.LOCK) or {}).get("packages") or {}
    if not require and not lock:
        return project
    if not pkg.lock_fits(require, lock):
        raise BuildError(f"{pkg.LOCK} doesn't match {pkg.PROJECT}; run barq install first")
    for name, e in lock.items():
        if pkg.vendored_commit(base, name) != e.get("commit"):
            raise BuildError(f"vendor/ doesn't have {name} as {pkg.LOCK} pins it; run barq install first")
    return project


# =========================================================================
# Launchers
# =========================================================================

SH = """#!/bin/sh
# Made by barq build: runs the Barq in runtime/ on the app in app/.
# Any barq command works: ./barq migrate, ./barq route:list, ./barq run tool.bq
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${{PYTHON:-python3}}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "barq: $PY not found; install Python 3 (this was built with {python}) or set PYTHON" >&2
  exit 127
fi
cd "$DIR/app" || exit 1
export BARQ_PACKAGES="${{BARQ_PACKAGES:-{packages}}}"
PYTHONPATH="$DIR/runtime/barq${{PYTHONPATH:+:$PYTHONPATH}}" exec "$PY" -m barq "$@"
"""

CMD = """@echo off
rem Made by barq build: runs the Barq in runtime\\ on the app in app\\.
setlocal
set "DIR=%~dp0"
if not defined PYTHON set "PYTHON=python"
if not defined BARQ_PACKAGES set "BARQ_PACKAGES={packages}"
set "PYTHONPATH=%DIR%runtime\\barq;%PYTHONPATH%"
cd /d "%DIR%app" || exit /b 1
"%PYTHON%" -m barq %*
"""

START_SH = """#!/bin/sh
# Made by barq build: {what}
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/barq" {command}
"""

START_CMD = """@echo off
rem Made by barq build: {what}
call "%~dp0barq.cmd" {command}
"""


def write_script(path, text, executable):
    with open(path, "w", encoding="utf-8", newline="\r\n" if path.endswith(".cmd") else "\n") as f:
        f.write(text)
    if executable:
        os.chmod(path, 0o755)


def entry_of(app, given):
    """('web', None) | ('console', file) | ('none', None)."""
    if given:
        if "/" in given.replace("\\", "/").strip("/") or not given.endswith(".bq"):
            raise BuildError(f"--entry must be a .bq file in the project folder, not {given}")
        if not os.path.isfile(os.path.join(app, given)):
            raise BuildError(f"--entry {given}: no such file in the project (or it was left out)")
        return "console", given
    if any(os.path.isfile(os.path.join(app, r)) for r in ROUTES):
        return "web", None
    if os.path.isfile(os.path.join(app, "main.bq")):
        return "console", "main.bq"
    return "none", None


def write_launchers(out, packages, kind, entry):
    names = ",".join(p["module"] for p in packages) or "none"
    python = ".".join(map(str, sys.version_info[:3]))
    write_script(os.path.join(out, "barq"), SH.format(packages=names, python=python), True)
    write_script(os.path.join(out, "barq.cmd"), CMD.format(packages=names), False)
    if kind == "web":
        what = "serves the app (HOST and PORT choose where; default 127.0.0.1:8000)."
        sh = 'serve --no-reload --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" "$@"'
        cmd = "serve --no-reload --host %HOST% --port %PORT% %*"
        cmd_prelude = ('if not defined HOST set "HOST=127.0.0.1"\r\n'
                       'if not defined PORT set "PORT=8000"\r\n')
    elif kind == "console":
        what = f"runs {entry} with the arguments given."
        sh = f'run {entry} "$@"'
        cmd = f"run {entry} %*"
        cmd_prelude = ""
    else:
        return
    write_script(os.path.join(out, "start"), START_SH.format(what=what, command=sh), True)
    text = START_CMD.format(what=what, command=cmd)
    if cmd_prelude:
        text = text.replace("@echo off\n", "@echo off\nsetlocal\n" + cmd_prelude.replace("\r\n", "\n"), 1)
    write_script(os.path.join(out, "start.cmd"), text, False)


# =========================================================================
# Zip
# =========================================================================

def make_zip(out, zip_path):
    """Zip with the folder name at the top; keeps run permissions and symlinks."""
    top = os.path.basename(out)
    tmp = zip_path + f".{os.getpid()}.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(out):
            dirnames.sort()
            rel_dir = os.path.relpath(dirpath, out)
            arc_dir = top if rel_dir == "." else f"{top}/{rel_dir.replace(os.sep, '/')}"
            info = zipfile.ZipInfo(arc_dir + "/")
            info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
            z.writestr(info, b"")
            for name in sorted(filenames) + sorted(d for d in dirnames if os.path.islink(os.path.join(dirpath, d))):
                path = os.path.join(dirpath, name)
                arc = f"{arc_dir}/{name}"
                if os.path.islink(path):
                    info = zipfile.ZipInfo(arc)
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(info, os.readlink(path))
                else:
                    z.write(path, arc)
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
    os.replace(tmp, zip_path)


# =========================================================================
# The command
# =========================================================================

def parse_args(argv):
    opts = {"out": None, "zip": False, "entry": None, "exclude": [], "include": [], "env": False, "force": False}
    args = list(argv)
    while args:
        a = args.pop(0)
        if a in ("-h", "--help"):
            opts["help"] = True
        elif a == "--zip":
            opts["zip"] = True
        elif a == "--with-env":
            opts["env"] = True
        elif a == "--force":
            opts["force"] = True
        elif a.split("=", 1)[0] in ("--out", "--entry", "--exclude", "--include"):
            name, eq, value = a.partition("=")
            if not eq:
                if not args or args[0].startswith("--"):
                    raise BuildError(f"{name} needs a value")
                value = args.pop(0)
            key = name[2:]
            if key in ("exclude", "include"):
                opts[key].append(value)
            else:
                opts[key] = value
        else:
            raise BuildError(f"unknown option '{a}' (see barq build --help)")
    return opts


def settings(project, opts):
    section = project.get("build") or {}
    if not isinstance(section, dict):
        raise BuildError('"build" in barq.json should be an object like {"exclude": ["docs"]}')
    for key in ("exclude", "include"):
        value = section.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise BuildError(f'"build.{key}" in barq.json should be a list of patterns')
    entry = opts["entry"] or section.get("entry")
    if entry is not None and not isinstance(entry, str):
        raise BuildError('"build.entry" in barq.json should be a file name')
    return entry, section.get("exclude", []) + opts["exclude"], section.get("include", []) + opts["include"]


def prepare_out(out, base, force):
    if out == base or base.startswith(out + os.sep):
        raise BuildError(f"--out {out} would hold the project itself; pick another folder")
    if os.path.exists(out):
        if not os.path.isdir(out):
            raise BuildError(f"{out} is a file")
        if os.listdir(out) and not os.path.isfile(os.path.join(out, MANIFEST)) and not force:
            raise BuildError(f"{out} is not empty and barq build didn't make it; use --force to replace it")


def build(base, opts, out_fn=print):
    from . import pkg
    if not os.path.isfile(os.path.join(base, pkg.PROJECT)) and not any(
            os.path.isfile(os.path.join(base, f)) for f in ROUTES + ("main.bq",)):
        raise BuildError("run barq build in a project folder (one with barq.json, routes/ or main.bq)")
    project = check_vendor(base)
    entry, exclude, include = settings(project, opts)
    out = os.path.abspath(os.path.join(base, opts["out"] or "dist"))
    prepare_out(out, base, opts["force"])
    tmp = os.path.join(os.path.dirname(out), f".{os.path.basename(out)}.building-{os.getpid()}")
    old = os.path.join(os.path.dirname(out), f".{os.path.basename(out)}.old-{os.getpid()}")
    excludes = DEFAULT_EXCLUDE + exclude
    if opts["env"]:
        excludes = [p for p in excludes if p != ".env"]
    picker = Picker(excludes, include, [out, tmp])
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        app = os.path.join(tmp, "app")
        os.makedirs(app)
        picked = picker.files(base)
        copied = copy_files(picked, os.path.realpath(base), app)
        kind, entry = entry_of(app, entry)
        compiled, failed = compile_app(app)
        if failed:
            raise BuildError(f"{len(failed)} file{'s' if len(failed) != 1 else ''} didn't compile "
                             f"(errors above); nothing was written to {os.path.relpath(out, base)}")
        packages = copy_runtime(os.path.join(tmp, "runtime"), os.path.realpath(base))
        write_launchers(tmp, packages, kind, entry)
        manifest = {
            "_readme": "Written by barq build. Run ./start (start.cmd on Windows), or ./barq <command>.",
            "name": project.get("name") or os.path.basename(base),
            "barq": __version__,
            "python": ".".join(map(str, sys.version_info[:3])),
            "built": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
            "entry": {"kind": kind, "file": entry} if entry else {"kind": kind},
            "packages": packages,
            "files": copied,
            "compiled": compiled,
        }
        with open(os.path.join(tmp, MANIFEST), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=4)
            f.write("\n")
        if os.path.exists(out):
            os.rename(out, old)
        os.rename(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)
    zip_path = None
    if opts["zip"]:
        zip_path = out + ".zip"
        make_zip(out, zip_path)
    report(base, out, manifest, zip_path, opts, picked, out_fn)
    return manifest


def report(base, out, m, zip_path, opts, picked, out_fn):
    shown = os.path.relpath(out, os.getcwd())
    if shown.startswith(".."):
        shown = out
    kinds = {"web": "a web app", "console": f"runs {m['entry'].get('file')}", "none": "no start script"}
    out_fn(f"Built {shown}/ ({kinds[m['entry']['kind']]}; {m['files']} files, {m['compiled']} compiled)")
    for p in m["packages"]:
        out_fn(f"  with {p['name']} {p['version']}".rstrip() + (" (from vendor/)" if p["from"] == "vendor" else ""))
    if zip_path:
        out_fn(f"  packed into {os.path.relpath(zip_path, os.getcwd())}")
    has_env = os.path.isfile(os.path.join(base, ".env"))
    if has_env and not opts["env"]:
        out_fn("  left out .env: give the server its own (app/.env) or set the variables; --with-env copies it")
    out_fn("")
    out_fn(f"Copy it to the server (it needs Python 3; this was built with {m['python']}), then:")
    if m["entry"]["kind"] == "web":
        if any(r.startswith("database/migrations/") for r, _, k in picked if k == "file"):
            out_fn(f"  {shown}/barq migrate       create the tables")
        out_fn(f"  {shown}/start              serve on 127.0.0.1:8000 (set HOST, PORT)")
    elif m["entry"]["kind"] == "console":
        out_fn(f"  {shown}/start [args]       run {m['entry']['file']}")
    else:
        out_fn(f"  {shown}/barq run <file.bq> (no routes/ or main.bq, so there is no start script)")


def cmd_build(argv):
    from .pkg import PackageError
    try:
        opts = parse_args(argv)
        if opts.get("help"):
            print(HELP.rstrip())
            return 0
        from . import plugins
        plugins.load()
        build(os.getcwd(), opts)
        return 0
    except (BuildError, PackageError, RuntimeError, OSError) as e:
        sys.stderr.write(f"barq build: {e}\n")
        return 1
