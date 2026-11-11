"""barq new — start a project.

    barq new my-app              a web app: routes, a controller, views, config, tests
    barq new my-app --api        JSON only: routes/api.bq, no views or public/
    barq new my-tool --console   a program: main.bq and a test, nothing else
    barq new my-app --auth       the web app plus barq-auth's `auth:install`
    barq new . / --force         write into an existing (empty, or not with --force) folder

Packages add their own files through hooks.NEW_PROJECT: barq-framework adds a
service provider, the User model, its migration, factory and seeder.
Files that already exist are never overwritten.
"""

import base64
import os
import re
import secrets
import sys

from . import __version__

KINDS = ("web", "api", "console")


def app_name(folder):
    words = [w for w in re.split(r"[-_\s]+", os.path.basename(os.path.abspath(folder))) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or "Barq App"


def app_key():
    return "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()


GITIGNORE = """.barq/
/vendor/
/dist/
/storage/*
!/storage/.gitkeep
/.env
*.db
"""


def env_file(name, key):
    return f"""APP_NAME="{name}"
APP_ENV=local
APP_DEBUG=true
APP_KEY={key}

DB_CONNECTION=sqlite
DB_PATH=storage/database.db
"""


def env_example(name):
    return env_file(name, "")


def readme(name, kind, packages):
    run = {
        "console": "barq run main.bq",
        "api": "barq serve          # http://127.0.0.1:8000/api",
        "web": "barq serve          # http://127.0.0.1:8000",
    }[kind]
    extra = ""
    if "barq-framework" in packages:
        extra = "barq migrate        # create the tables in storage/database.db\nbarq db:seed\n"
    return f"""# {name}

A [Barq](https://github.com/barq-lang/barq) project.

```sh
{extra}{run}
barq test           # runs tests/
barq fmt            # formats every .bq file
```

Packages: `barq install user/name` adds one to barq.json and vendor/
(commit barq.json and barq.lock, not vendor/).
"""


CONFIG_APP = '''// Read with Config.get("app.name")
dict<string, any> app = #{
    "name": Env.get("APP_NAME", "{name}"),
    "env": Env.get("APP_ENV", "production"),
    "debug": Env.get("APP_DEBUG", "false") == "true",
    "timezone": "UTC",
    "locale": "en"
}
'''

CONFIG_DATABASE = '''// Only SQLite runs in the reference interpreter for now.
dict<string, any> database = #{
    "default": Env.get("DB_CONNECTION", "sqlite"),
    "connections": #{
        "sqlite": #{"driver": "sqlite", "path": Env.get("DB_PATH", "storage/database.db")}
    }
}
'''

WEB_ROUTES = '''use App\\Controllers\\HomeController

Route.get("/", #[HomeController, "index"]).name("home")
'''

API_ROUTES = '''// Every route here is served under /api.
Route.get("/health", kr(): dict<string, any> => #{"status": "ok", "time": Date.iso(Date.now())})
'''

BASE_CONTROLLER = '''namespace App\\Controllers

// The base class of every controller: put the helpers they share here.
// Controllers in a subfolder bring it in with: use App\\Controllers\\Controller
abstract class Controller
'''

HOME_CONTROLLER = '''namespace App\\Controllers

class HomeController extends Controller

    #[Public]
    #[Response]
    karo index()
        return view("home", #{"name": Config.get("app.name", "Barq")})
'''

API_HOME_CONTROLLER = '''namespace App\\Controllers

class HomeController extends Controller

    #[Public]
    #[dict<string, any>]
    karo index()
        return #{"name": Config.get("app.name", "Barq"), "version": "1.0"}
'''

API_ONLY_ROUTES = '''use App\\Controllers\\HomeController

// Every route here is served under /api.
Route.get("/", #[HomeController, "index"]).name("home")
Route.get("/health", kr(): dict<string, any> => #{"status": "ok", "time": Date.iso(Date.now())})
'''

LAYOUT_VIEW = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title><yield name="title">{name}</yield></title>
    <link rel="stylesheet" href="/css/app.css">
</head>
<body>
    <main>
        <yield name="content" />
    </main>
</body>
</html>
'''

HOME_VIEW = '''<extends layout="layout" />

<section name="title">{name}</section>

<section name="content">
    <h1>{name}</h1>
    <p>Your Barq app is running. Edit <code>views/home.bq</code> to change this page,
    and <code>routes/web.bq</code> to add pages.</p>
</section>
'''

APP_CSS = """:root {
    --green: #15803d;
    --blue: #4f46e5;
    --text: #111827;
    --muted: #4b5563;
    --bg: #ffffff;
}
@media (prefers-color-scheme: dark) {
    :root { --text: #f3f4f6; --muted: #9ca3af; --bg: #0b0f19; }
}
body {
    margin: 0;
    font-family: Inter, system-ui, sans-serif;
    color: var(--text);
    background: var(--bg);
    line-height: 1.6;
}
main { max-width: 42rem; margin: 0 auto; padding: 4rem 1rem; }
h1 {
    font-size: 2.5rem;
    background: linear-gradient(90deg, var(--green), var(--blue));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
}
p { color: var(--muted); }
code { font-family: "JetBrains Mono", monospace; }
"""

WEB_TEST = '''Test.it("shows the home page", karo(): void
    Response r = Test.client().get("/")
    Test.assertStatus(r, 200)
    Test.assertContains(Config.get("app.name"), r.body)
)

Test.it("answers the health check", karo(): void
    Response r = Test.client().get("/api/health")
    Test.assertStatus(r, 200)
    Test.assertEquals("ok", r.json()["status"])
)
'''

API_TEST = '''Test.it("answers the health check", karo(): void
    Response r = Test.client().get("/api/health")
    Test.assertStatus(r, 200)
    Test.assertEquals("ok", r.json()["status"])
)

Test.it("names the app", karo(): void
    Test.assertEquals(Config.get("app.name"), Test.client().get("/api").json()["name"])
)
'''

CONSOLE_MAIN = '''use App\\Greeter

list<string> args = Cli.args()
string who = "world"
if (len(args) > 0)
    who = args[0]
print(new Greeter().greet(who))
'''

CONSOLE_GREETER = '''namespace App

class Greeter

    #[Public]
    #[string]
    karo greet(string name)
        return "Salaam, {name}!"
'''

CONSOLE_TEST = '''use App\\Greeter

Test.it("greets by name", karo(): void
    Test.assertEquals("Salaam, Ali!", new Greeter().greet("Ali"))
)
'''


def barq_json(name):
    """The package list `barq install` reads and writes (empty to start)."""
    import json
    slug = "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split()) or "app"
    return json.dumps({"name": slug, "require": {}}, indent=4) + "\n"


def project_files(kind, name):
    """{relative path: text} for the core part of a new project."""
    key = app_key()
    files = {
        ".gitignore": GITIGNORE,
        "barq.json": barq_json(name),
        ".env": env_file(name, key),
        ".env.example": env_example(name),
    }
    if kind == "console":
        files.update({
            "main.bq": CONSOLE_MAIN,
            "app/Greeter.bq": CONSOLE_GREETER,
            "tests/GreeterTest.bq": CONSOLE_TEST,
        })
        return files
    files.update({
        "config/app.bq": CONFIG_APP.replace("{name}", name.replace('"', "'")),
        "config/database.bq": CONFIG_DATABASE,
        "storage/.gitkeep": "",
    })
    if kind == "api":
        files.update({
            "routes/api.bq": API_ONLY_ROUTES,
            "app/Controllers/Controller.bq": BASE_CONTROLLER,
            "app/Controllers/HomeController.bq": API_HOME_CONTROLLER,
            "tests/feature/ApiTest.bq": API_TEST,
        })
    else:
        files.update({
            "routes/web.bq": WEB_ROUTES,
            "routes/api.bq": API_ROUTES,
            "app/Controllers/Controller.bq": BASE_CONTROLLER,
            "app/Controllers/HomeController.bq": HOME_CONTROLLER,
            "views/layout.bq": LAYOUT_VIEW.replace("{name}", "{name ?? \"Barq\"}"),
            "views/home.bq": HOME_VIEW,
            "public/css/app.css": APP_CSS,
            "tests/feature/HomeTest.bq": WEB_TEST,
        })
    return files


def parse_args(args):
    opts = {"kind": "web", "auth": False, "force": False, "packages": True}
    folder = None
    for a in args:
        if a in ("--api", "--console", "--web"):
            opts["kind"] = a[2:]
        elif a == "--auth":
            opts["auth"] = True
        elif a == "--force":
            opts["force"] = True
        elif a == "--no-packages":
            opts["packages"] = False
        elif a.startswith("-"):
            raise ValueError(f"unknown option '{a}'")
        elif folder is None:
            folder = a
        else:
            raise ValueError(f"one folder at a time (got '{folder}' and '{a}')")
    if folder is None:
        raise ValueError("usage: barq new <folder> [--api | --console] [--auth] [--force] [--no-packages]")
    if opts["auth"] and opts["kind"] == "console":
        raise ValueError("--auth needs a web project (leave out --console)")
    if opts["auth"] and not opts["packages"]:
        raise ValueError("--auth needs barq-auth (leave out --no-packages)")
    return folder, opts


def cmd_new(args):
    try:
        folder, opts = parse_args(args)
    except ValueError as e:
        sys.stderr.write(f"barq: {e}\n")
        return 2
    if os.path.exists(folder) and not os.path.isdir(folder):
        sys.stderr.write(f"barq: {folder} is a file\n")
        return 1
    if os.path.isdir(folder) and os.listdir(folder) and not opts["force"]:
        sys.stderr.write(f"barq: {folder} is not empty (use --force to add the files that are missing)\n")
        return 1

    from . import hooks, plugins
    packages = []
    if opts["packages"]:
        try:
            plugins.load()
        except RuntimeError as e:
            sys.stderr.write(f"barq: {e}\n")
            return 1
        packages = [n for n, _ in plugins.describe()]
    if opts["auth"] and hooks.find_command("auth:install") is None:
        sys.stderr.write("barq: --auth needs barq-auth (https://github.com/barq-lang/barq-auth)\n")
        return 1

    name = app_name(folder)
    files = project_files(opts["kind"], name)
    if opts["packages"]:
        for fn in hooks.NEW_PROJECT:
            fn(files, dict(opts, name=name))
    files["README.md"] = readme(name, opts["kind"], packages)

    os.makedirs(folder, exist_ok=True)
    for rel in sorted(files):
        path = os.path.join(folder, *rel.split("/"))
        if os.path.exists(path):
            print(f"Kept {rel} (already there)")
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(files[rel])

    if opts["auth"]:
        here = os.getcwd()
        os.chdir(folder)
        try:
            code = hooks.find_command("auth:install")("auth:install", [])
        finally:
            os.chdir(here)
        if code:
            return code

    kind = {"web": "web app", "api": "API", "console": "program"}[opts["kind"]]
    print(f"Created the {kind} {name} in {folder} (Barq {__version__}{', ' + ', '.join(packages) if packages else ''})")
    print()
    print("Next:")
    if os.path.abspath(folder) != os.getcwd():
        print(f"  cd {folder}")
    if "barq-framework" in packages and opts["kind"] != "console":
        print("  barq migrate")
    print("  barq run main.bq" if opts["kind"] == "console" else "  barq serve")
    print("  barq test")
    return 0
