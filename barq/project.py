"""Project commands built into the language: running a project's routes and
its .env file.

    barq serve                   serve routes/web.bq and routes/api.bq (reloads when files change)
    barq route:list              print the routes
    barq key:generate            a random APP_KEY in .env
    barq storage:link            public/storage -> storage/app/public

Packages add more (barq-framework: migrate, db:seed, make:*, schedule:*;
barq-auth: auth:install; barq-permissions: permissions:*). They run in the
current folder (the project root).
"""

import os
import re
import sys
import time


def new_interp(engine):
    root = os.getcwd()
    if engine == "tree":
        from .interpreter import Interpreter
        interp = Interpreter(root=root, argv=[])
    else:
        from .vm import VM
        interp = VM(root=root, argv=[], cache=False)
    load_dotenv(interp)
    return interp


def load_dotenv(interp):
    """Read <project root>/.env at startup; variables already set win."""
    if os.path.isfile(os.path.join(interp.root, ".env")):
        from .stdlib import env_load
        env_load(interp, [], None)


def boot(interp):
    """Run the packages' start-up hooks (barq-framework starts the service providers)."""
    from . import hooks, plugins
    plugins.load()
    for fn in hooks.BOOT:
        fn(interp)


def run_in_project(fn, engine):
    from .cli import run_guarded, with_big_stack
    from .vm import MAX_DEPTH
    interp = new_interp(engine)

    def go():
        boot(interp)
        fn(interp)
    code = with_big_stack(lambda: run_guarded(go), MAX_DEPTH * 3)
    sys.stdout.flush()
    return code


def option(args, name, default=None):
    """--name value / --name=value; returns (value, remaining args)."""
    rest = []
    value = default
    k = 0
    while k < len(args):
        a = args[k]
        if a == name and k + 1 < len(args):
            value = args[k + 1]
            k += 2
            continue
        if a.startswith(name + "="):
            value = a[len(name) + 1:]
        elif a == name:
            value = True
        else:
            rest.append(a)
        k += 1
    return value, rest


def any_text_has(folder, names, needle):
    for name in names:
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            if needle in f.read():
                return True
    return False


def all_text_has(folder, names, needle):
    for name in names:
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            if needle not in f.read():
                return False
    return True


def env_value(name):
    if not os.path.isfile(".env"):
        return None
    with open(".env", encoding="utf-8") as f:
        for line in f:
            m = re.match(rf"^\s*{name}\s*=\s*(.*?)\s*$", line)
            if m:
                return m.group(1).strip("\"'") or None
    return None


def cmd_key_generate(args):
    """barq key:generate — a random APP_KEY in .env (signs URLs). --show prints it, --force replaces one."""
    import base64
    import secrets
    key = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()
    if "--show" in args:
        print(key)
        return 0
    if env_value("APP_KEY") and "--force" not in args:
        print("APP_KEY is already set in .env (use --force to replace it: links signed with the old key stop working)")
        return 0
    lines = []
    if os.path.isfile(".env"):
        with open(".env", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if not re.match(r"^\s*APP_KEY\s*=", ln)]
    lines.append(f"APP_KEY={key}")
    with open(".env", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("Set APP_KEY in .env")
    return 0


def cmd_storage_link(args):
    target = os.path.abspath(os.path.join("storage", "app", "public"))
    link = os.path.join("public", "storage")
    if os.path.lexists(link):
        print(f"{link} already exists")
        return 0
    os.makedirs(target, exist_ok=True)
    os.makedirs("public", exist_ok=True)
    try:
        os.symlink(os.path.relpath(target, "public"), link, target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        sys.stderr.write(f"barq: cannot link {link} -> {target}: {e}\n")
        return 1
    print(f"Linked {link} -> storage/app/public")
    return 0


def write_new(path, text):
    if os.path.exists(path):
        sys.stderr.write(f"barq: {path} already exists\n")
        return 1
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Created {path}")
    return 0


ROUTE_FILES = (os.path.join("routes", "web.bq"), os.path.join("routes", "api.bq"))


AUTH_ROUTES = os.path.join("routes", "auth.bq")


WATCH = ("app", "routes", "config", "views", "database", ".env", "main.bq")


def load_routes(i):
    """routes/auth.bq (from auth:install) and routes/web.bq as they are, routes/api.bq under /api."""
    from .web import router
    web, api = (os.path.join(i.root, f) for f in ROUTE_FILES)
    auth = os.path.join(i.root, AUTH_ROUTES)
    if not os.path.isfile(web) and not os.path.isfile(api) and not os.path.isfile(auth):
        i.throw("NameException", "no routes: create routes/web.bq (and/or routes/api.bq, served under /api)")
    if os.path.isfile(auth):
        i.load_module(auth)
    if os.path.isfile(web):
        i.load_module(web)
    if os.path.isfile(api):
        st = router(i)
        st.groups.append(("api", [], ""))
        try:
            i.load_module(api)
        finally:
            st.groups.pop()


def project_routes(i):
    """barq test: load the app's routes (if it has any) so TestClient can reach them."""
    if any(os.path.isfile(os.path.join(i.root, f)) for f in ROUTE_FILES + (AUTH_ROUTES,)):
        load_routes(i)


def serve_app(i, host, port, limit):
    from .stdlib2 import http_serve
    from .web import dispatch
    from .runtime import NativeFunction
    load_routes(i)
    opts = {"port": port, "host": host}
    if os.path.isdir(os.path.join(i.root, "public")):
        opts["static"] = "public"
    if limit is not None:
        opts["max"] = limit
    handler = NativeFunction("Route.dispatch", lambda i2, a, n: dispatch(i, a[0], n), 1, 1)
    http_serve(i, [handler, opts], None)


def snapshot(root):
    """mtime of every file the app is made of."""
    out = {}
    for name in WATCH:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            out[path] = os.stat(path).st_mtime_ns
        elif os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for f in filenames:
                    if f.endswith((".bq", ".json", ".env")):
                        full = os.path.join(dirpath, f)
                        try:
                            out[full] = os.stat(full).st_mtime_ns
                        except OSError:
                            pass
    return out


def changed_files(before, after):
    return sorted(os.path.relpath(p) for p in set(before) | set(after) if before.get(p) != after.get(p))


def supervise(child_args):
    """Run the server in a child process and restart it when a file changes."""
    import subprocess
    root = os.getcwd()
    cmd = [sys.executable, "-m", "barq", "serve", "--child", *child_args]
    print("Watching app/, routes/, config/, views/, database/ and .env for changes.", file=sys.stderr, flush=True)
    seen = snapshot(root)
    proc = subprocess.Popen(cmd)
    try:
        while True:
            time.sleep(0.4)
            now = snapshot(root)
            changes = changed_files(seen, now)
            if changes:
                seen = now
                shown = ", ".join(changes[:3]) + (f" and {len(changes) - 3} more" if len(changes) > 3 else "")
                print(f"\nChanged: {shown} — restarting.", file=sys.stderr, flush=True)
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                proc = subprocess.Popen(cmd)
            elif proc.poll() is not None and not getattr(proc, "_reported", False):
                proc._reported = True
                print("The server stopped — fix the error and save a file to restart it.", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    print("\nServer stopped.", file=sys.stderr)
    return 0


def cmd_serve(args):
    port, args = option(args, "--port", os.environ.get("PORT", "8000"))
    host, args = option(args, "--host", "127.0.0.1")
    limit, args = option(args, "--max")
    engine = "tree" if "--interp" in args else None
    child = "--child" in args
    reload = "--no-reload" not in args and limit is None
    unknown = [a for a in args if a not in ("--interp", "--vm", "--child", "--no-reload")]
    if unknown:
        sys.stderr.write(f"barq serve: unknown option '{unknown[0]}' (use --port, --host, --no-reload, --interp)\n")
        return 2
    if port is True or not str(port).isdigit() or not 0 < int(port) < 65536:
        sys.stderr.write("barq serve: --port needs a number from 1 to 65535\n")
        return 2
    if limit is not None and (limit is True or not str(limit).isdigit()):
        sys.stderr.write("barq serve: --max needs a number\n")
        return 2
    if not any(os.path.isfile(f) for f in ROUTE_FILES + (AUTH_ROUTES,)):
        sys.stderr.write("barq serve: no routes — create routes/web.bq (and/or routes/api.bq, served under /api)\n")
        return 1
    if reload and not child:
        return supervise(["--port", str(port), "--host", host] + (["--interp"] if engine else []))
    return run_in_project(lambda i: serve_app(i, host, int(port), int(limit) if limit is not None else None), engine)


def route_list(i):
    from .web import route_list as routes
    load_routes(i)
    rows = [[" | ".join(r["methods"]), "/" + r["path"].lstrip("/"), r["name"] or "", r["action"],
             ", ".join(r["middleware"])] for r in routes(i, [], None)]
    if not rows:
        print("No routes defined.")
        return
    head = ["Method", "Path", "Name", "Action", "Middleware"]
    widths = [max(len(x) for x in col) for col in zip(head, *rows)]
    for row in [head] + rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
    print(f"\n{len(rows)} route{'s' if len(rows) != 1 else ''}")
