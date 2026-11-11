# Barq (برق) — v0.25.0-alpha

[![tests](https://github.com/barq-lang/barq/actions/workflows/tests.yml/badge.svg)](https://github.com/barq-lang/barq/actions/workflows/tests.yml)

Barq is a strictly typed, full-stack programming language: Python's indentation-based syntax with PHP's web power. This repository is the language itself, written in pure Python 3 with **no dependencies** (tested on Python 3.14). Programs are compiled to bytecode and run on a VM; a tree-walking interpreter is kept as the reference and for the REPL.

```bash
git clone https://github.com/barq-lang/barq
cd barq
./bin/barq examples/hello.bq          # Linux / macOS (compiled, on the VM)
./bin/barq new my-app                 # start a project (--api, --console, --auth)
bin\barq.cmd examples\hello.bq        # Windows
./bin/barq run --interp app.bq        # same program on the tree-walker
./bin/barq compile src/               # check + compile a folder without running it
./bin/barq check app/                 # report errors without running (--json for editors)
./bin/barq fmt                        # format every .bq file in the folder (--check, --diff)
./bin/barq debug app.bq               # step through a program: breakpoints, variables (help at the prompt)
./bin/barq lsp                        # the language server editors start (stdin/stdout)
./bin/barq repl                       # interactive shell
./bin/barq test                       # run your project's tests/ (Test module)
./bin/barq serve                      # serve routes/ on http://127.0.0.1:8000, reloading on changes
./bin/barq packages                   # list the installed packages
./bin/barq build                      # dist/: the app compiled, with Barq and its packages, to deploy
python3 tests/test_barq.py            # the language's own test suite (both engines, no packages)
benchmarks/run.sh                     # time the tree-walker against the VM
```

The language spec is in [docs/](docs/INDEX.md) — start with [step 1](docs/step-01-overview.md); the roadmap is [step 11](docs/step-11-versioning-roadmap.md) and the open questions are in [docs/open-decisions.md](docs/open-decisions.md).

## Repositories and packages

| Repository | What it holds |
|------------|---------------|
| [barq](https://github.com/barq-lang/barq) | The language: compiler, VM, REPL, standard library, `barq fmt`, `barq check`, `barq serve` (this repo) |
| [barq-framework](https://github.com/barq-lang/barq-framework) | Models (ORM), migrations, seeders, factories, service container, providers, events, scheduler, `make:*` |
| [barq-auth](https://github.com/barq-lang/barq-auth) | Guards, Gate and policies, API tokens, password resets, email verification, 2FA, OAuth, teams, devices, `auth:install` |
| [barq-permissions](https://github.com/barq-lang/barq-permissions) | Roles and permissions, wildcards, team roles, `role:` / `permission:` middleware |
| [vscode-barq](https://github.com/barq-lang/vscode-barq) | VS Code extension: highlighting, format on save, inline errors, outline, snippets |
| [barq-website](https://github.com/barq-lang/barq-website) | The website, docs site and admin dashboard |
| [barq-blog](https://github.com/barq-lang/barq-blog) | The reference application: a blog written entirely in Barq (posts, comments, roles, admin, JSON API) |

The framework and packages are Python packages named `barq_framework`, `barq_auth` and `barq_permissions`. The `barq` command finds them when they are cloned **next to this checkout**, listed in `BARQ_PATH` (folders separated by `:`), or importable by Python:

```bash
git clone https://github.com/barq-lang/barq-framework      # next to barq/
git clone https://github.com/barq-lang/barq-auth
git clone https://github.com/barq-lang/barq-permissions
barq/bin/barq packages             # barq-framework 0.18.0-alpha, barq-auth ..., barq-permissions ...
BARQ_PACKAGES=none barq/bin/barq run app.bq           # the language alone
BARQ_PACKAGES=barq_framework barq/bin/barq run app.bq # only that package (and what it needs)
```

A package is a `barq_<name>` folder whose `__init__.py` has `NAME`, `__version__`, `REQUIRES` and a `register(hooks)` function. It adds built-ins (`hooks.INSTALLERS`), commands (`hooks.add_command`, `hooks.add_prefix_command`), start-up code (`hooks.BOOT`) and fills the extension points the language leaves open: route model binding and form requests (`PARAM_BINDERS`, `PARAM_EARLY`), validation rules (`VALIDATION_RULES`), the service container (`container_make`, `resolvable`), relations read as properties (`RELATION_TYPES`, `property_access`), the login redirect (`login_url`) and the shared database connection (`default_db`, `named_db`). See [barq/hooks.py](barq/hooks.py) and [barq/plugins.py](barq/plugins.py). Package test suites use [barq/devtest.py](barq/devtest.py).

## How it works

```
source.bq ─► lexer ─► parser ─► checker ─┬─► compiler ─► Python bytecode ─► CPython VM     (barq run)
             tokens    AST      compile  │               cached as .bqc
                                errors   └─► interpreter (walks the AST)                   (--interp, repl)
```

| File | Job |
|------|-----|
| [barq/lexer.py](barq/lexer.py) | Tokens, indentation (INDENT/DEDENT), `#[ #{ #(`, `"{x}"` interpolation, multi-line anonymous `karo(...)` functions inside calls |
| [barq/parser.py](barq/parser.py) | Recursive-descent parser → AST ([barq/nodes.py](barq/nodes.py)) |
| [barq/checker.py](barq/checker.py) | Checks the whole file **before anything runs**: undeclared variables, type mismatches, wrong argument counts, missing returns, `break` outside loops, unknown types/classes, abstract misuse. It reports every error it finds at once. |
| [barq/compiler.py](barq/compiler.py) | Turns the checked AST into a Python syntax tree, which CPython compiles to bytecode |
| [barq/vm.py](barq/vm.py) | Runs compiled files: the runtime helpers compiled code calls, error locations and traces, the `.bqc` cache |
| [barq/interpreter.py](barq/interpreter.py) | The tree-walker: runs the AST directly. Also checks types at runtime for things the checker can't know ahead of time. The VM reuses its class setup, member access and error messages |
| [barq/runtime.py](barq/runtime.py) | Value types, formatting, equality |
| [barq/builtins.py](barq/builtins.py) | Global functions, `Math`, `Str`, methods on strings/lists/dicts/sets, `~type()` casts, exception classes |
| [barq/stdlib.py](barq/stdlib.py) | Standard library batch 1: `Json`, `IO`, `Path`, `Env`, `Hash`, `Crypto`, `Uuid`, `Regex`, `Date`, `Arr`, `Log`, `Cache` |
| [barq/stdlib2.py](barq/stdlib2.py) | Standard library batch 2: `Database` (SQLite), `Http` client + server, `Request`, `Response`, `Csv`, `Validation`, `Config` |
| [barq/web.py](barq/web.py) | Standard library batch 3, web half: `Route` (router + middleware), `Session`, `Cookie`, `Http.abort`, `Request.make`, `TestClient` |
| [barq/stdlib3.py](barq/stdlib3.py) | Standard library batch 3: `Xml`, `Mail`, `Queue`, `Cli`, `Test` (and the `barq test` runner's engine) |
| [barq/view.py](barq/view.py) | Templates (v0.8.0): `view()`, `View`, the template parser and renderer |
| [barq/storage.py](barq/storage.py), [barq/s3.py](barq/s3.py), [barq/upload.py](barq/upload.py) | `Storage` disks (local, memory, S3), file uploads |
| [barq/project.py](barq/project.py) | `barq serve`, `barq route:list`, `barq key:generate`, `barq storage:link`, `.env` loading |
| [barq/fmt.py](barq/fmt.py), [barq/check.py](barq/check.py) | `barq fmt` (formatter) and `barq check` (errors without running, `--json` for editors) |
| [barq/hooks.py](barq/hooks.py), [barq/plugins.py](barq/plugins.py) | Extension points and package loading (barq-framework, barq-auth, barq-permissions) |
| [barq/cli.py](barq/cli.py), [barq/repl.py](barq/repl.py) | `barq` command and REPL |

## The compiler and VM (v0.5.0)

`barq run` compiles each file once into a Python code object and runs it on CPython's own bytecode VM. A VM loop written in Python would be no faster than the tree-walker, so the compiler targets the fast VM that is already there. A Barq-specific instruction set comes with the production VM in C/Rust (Phase 6).

What the compiler does:

- **A file becomes one Python function.** Top-level variables are its locals, and every Barq function and method is a nested function inside it, so calls between Barq functions are plain Python calls.
- **Known types compile to plain operations.** `int + int`, `float < float`, `bool and bool`, comparisons of two strings, and calls to functions in the same file all become direct Python operations. Everything else calls a small helper that does the full Barq checks, so the language behaves exactly as before.
- **Type checks stay.** Typed variables, arguments, return values and properties are still checked on every assignment. Checks the compiler can prove will pass are left out (an `int` literal stored in an `int` variable needs no check).
- **Errors cost nothing until they happen.** Every bytecode instruction carries the Barq line and column it came from. When an error is raised, the VM reads the position of the running instruction in each Barq frame. That gives the same `at file:line:col` and call trace as the tree-walker without tracking anything while the program runs.
- **Closures behave as in Barq.** A lambda made inside a loop captures that iteration's variables (`kr(): int => i` in a loop over 0..2 gives 0, 1, 2), and block-scoped variables stay separate even when they share a name.

### The `.bqc` cache

Compiled files are saved as `.bqc` files under `<project>/.barq/cache/`, mirroring the source tree (`app/Models/User.bq` → `.barq/cache/app/Models/User.bqc`). The project root is the folder of the file you run. On the next run, a file whose source text is unchanged skips parsing, checking and compiling. A cache file stores the syntax tree, the bytecode, and a SHA-256 of the source. It is ignored and rebuilt when the source, the Barq version or the Python version changes, or when it can't be read. You can delete `.barq/` at any time, and it doesn't belong in version control. A project folder that moved keeps its cache: the code is given the new path and the file rewritten, without parsing or compiling again.

`--no-cache` (or `BARQ_NO_CACHE=1`) turns the cache off. `barq compile <files or folders>` checks and caches files without running them, which is useful before a deploy.

Like Python's `.pyc` files, a `.bqc` file is trusted code: only run caches your own project wrote.

### Speed

`benchmarks/run.sh`, best of 3 runs, startup included:

| Benchmark | Tree-walker | VM | Speed-up |
|-----------|-------------|-----|----------|
| `fib` — recursive calls, `int` maths | 1.15s | 0.08s | 14× |
| `loops` — `for` / `while` over 1.3M steps | 1.86s | 0.15s | 12× |
| `objects` — `new`, method calls, properties | 4.69s | 0.56s | 8× |
| `collections` — lists, dicts, strings, lambdas | 1.66s | 0.37s | 4.5× |

Code that spends its time in typed arithmetic, loops and function calls gets the full speed-up. Code that spends it in the standard library, or in checks Barq requires (assigning a 100,000-item list to a `list<int>` checks every item), gains less. Startup drops from about 125ms to 70ms with a warm cache; most of what's left is Python loading the standard library modules.

### The two engines

The test suite runs every program on both engines and requires identical output, errors included. Use the tree-walker with `barq run --interp` (or `BARQ_ENGINE=tree`); the REPL always uses it.

The engines differ in two ways:

- **Recursion limit.** The tree-walker stops at exactly 5000 nested Barq calls. The VM counts Python frames, which allows roughly 5000–15000 nested calls depending on the kind of call, so its message just says `maximum call depth exceeded`.
- **Debugging.** `barq emit <file>` prints the Python code the compiler generates for a file.

Deep recursion also prints a much shorter trace now on both engines: a call repeated thousands of times is shown three times, followed by `... the same call N more times`.

## What works

- **Variables:** strict typed declarations (`int`, `float`, `string`, `bool`, `any`, `list<T>`, `dict<K,V>`, `set<T>`, class names, `karo` for functions), nullable `?int` / `?User` (v0.13), `const`, no implicit coercion
- **Strings:** `"Hello {name}"` interpolation with full expressions, `'literal'` strings, `"""multi-line"""` strings (v0.12), escapes (`\n`, `\{`, `ب`)
- **Collections:** `#[...]`, `#{...}`, `#(...)`, nesting, indexing, methods (`push`, `map`, `filter`, `reduce`, `sort`, `join`, `keys`, `has`, `union`…)
- **Casts:** `~int()`, `~float()`, `~string()`, `~bool()`, `~list()`, `~set()`, `~dict(obj)`, `~User(dict)`; failures raise `CastException`
- **Functions:** `karo` with `#[Public]`/`#[Private]`/`#[Protected]`, `#[Static]`, `#[Abstract]` and `#[type]` attributes; one-line and block bodies; recursion; functions as values; default values, named arguments, `*variadic` parameters and `*list` spreading (v0.12)
- **Functions as values:** anonymous functions `karo(int n): int` with an indented body, and one-expression lambdas `kr(int n): int => n * 2` — both work as call arguments. Mixing them up (a block after `kr`, `=>` after `karo(...)`, or the old bare `(int n): int => …`) is a compile error that shows the right form
- **Control flow:** `if / else if / elseif / else` (block and one-line), `for`/`foreach … in` (with `key, value`), `while`, `do … while`, `break`, `continue`, `switch/case/default` (stacked cases), `cond ? a : b`, `a if cond else b`
- **Errors:** `try / except Type as e / finally`, `raise`, re-raise, custom exception classes, uncaught errors print the source line and a call trace
- **Enums:** pure `enum Suit`, and backed enums with `#[String]` or `#[Int]` on the line above (`#[String]` / `enum Status`) — `Status::Active`, `.name`, `.value`, `Status::cases()`, `from()`, `tryFrom()`, methods, static methods and constants; cases are read-only singletons usable as types, dict keys and in `switch`
- **OOP:** classes, properties with visibility and defaults, `__construct`, `this`, `extends`, `parent::`, `self::`, abstract classes and methods, static properties/methods, class constants, `__toString`, `is` type checks; interfaces, traits and constructor promotion (v0.12); trait `insteadof` / `as` rules (v0.13)
- **Namespaces:** `namespace App\Models`, `use X`, `use X as Y`, `use App\Models\{A, B as C}`, autoloading `App\Models\User` from `app/Models/User.bq`, same-namespace classes found without `use`
- **Globals:** `print`, `write` (no newline), `dump`, `dd`, `exit`, `die`, `range`, `len`, `type`, `input`, `args`, `assert`, `Math.*`, `Str.*`

## Standard library — batch 1 (v0.3.0)

Every module is global, and `use Barq\Json` works too. Your own class may reuse a module name (e.g. `App\Models\Log`); reach the module with `use Barq\Log as BarqLog`.

| Module | Members |
|--------|---------|
| `Json` | `encode(v, pretty?)`, `decode(s)`, `pretty(v)`, `valid(s)` — objects encode their public properties, or call `toJson()` if defined. Errors raise `JsonException` |
| `IO` | `readFile`, `readLines`, `writeFile`, `appendFile`, `deleteFile`, `copy`, `move`, `exists`, `isFile`, `isDir`, `makeDir`, `listDir`, `removeDir`, `size`, `modified`, `print`, `println`, `error` — errors raise `IOException` |
| `Path` | `join`, `basename`, `dirname`, `extension`, `stem`, `absolute`, `normalize`, `isAbsolute`, `cwd`, `temp`, `home`, `root` — results always use `/` |
| `Env` | `get(name, default?)`, `int`, `bool`, `set`, `has`, `remove`, `all`, `load(path?, override?)` reads a `.env` file |
| `Hash` | `md5`, `sha1`, `sha256`, `sha512`, `hmac(key, msg, algo?)`, `password(pw)`, `verify(pw, hash)`, `equals` (constant-time) |
| `Crypto` | `randomInt(min, max)`, `randomHex(bytes?)`, `token(bytes?)`, `base64Encode`, `base64Decode`, `jwtSign(payload, secret, ttl?)`, `jwtVerify(token, secret)` — errors raise `CryptoException` |
| `Uuid` | `v4()`, `v7()` (time-ordered, good for DB keys), `isValid(s)` |
| `Regex` | `match`, `capture`, `named`, `matchAll`, `replace(pattern, repl, text, limit?)`, `split`, `escape` — patterns are `'/body/flags'` in single quotes; `repl` may use `$1`/`${name}` or be a function |
| `Date` | `now`, `nowMs`, `today`, `make(y, m, d, h?, i?, s?)`, `parse`, `format(ts, "Y-m-d H:i:s", tz?)`, `iso`, `add`, `sub`, `diff`, `parts`, `startOf`, `ago`, `isLeapYear`, `daysInMonth`, `sleep` |
| `Arr` | every list method as a function, plus `unique`, `flatten`, `merge`, `chunk`, `zip`, `groupBy`, `keyBy`, `countBy`, `pluck`, `partition`, `min`, `max`, `avg`, `fill`, `count`, `keys`, `values`. `Arr.sort`/`Arr.reverse` return a new list |
| `Log` | `debug`, `info`, `warning` (`warn`), `error`, `critical`, `setLevel`, `setFile` — writes to stderr by default |
| `Cache` | in-memory: `set(key, v, ttl?)`, `get(key, default?)`, `has`, `forget`, `forever`, `remember(key, ttl, fn)`, `increment`, `flush` |

## Standard library — batch 2 (v0.4.0)

Objects like a database connection or an HTTP response belong to built-in classes, so you can use them as types: `Database db = Database.connect()`. These class names are reserved. Nested imports work: `use Barq\Db\Database`, `use Barq\Http\Client` (same as `Http`), `use Barq\Http\Response`.

### Database (SQLite)

```barq
Database db = Database.connect("sqlite", #{"path": "storage/app.db"})   // or ":memory:"
Schema schema = db.schema()
schema.create("users", karo(Blueprint t): void
    t.id()
    t.string("email").unique()
    t.int("age").nullable()
    t.bool("active").default(true)
    t.timestamps()
)
int id = db.table("users").insertGetId(#{"email": "a@b.co", "age": 30})
list<dict<string, any>> adults = db.table("users").where("age", ">=", 18).orderBy("email").get()
db.transaction(karo(): void
    db.table("users").where("id", id).increment("age")
)
```

| Object | Members |
|--------|---------|
| `Database` | `Database.connect(name?, settings?)`, `table`, `raw(sql, params?)` → rows, `rawExec(sql, params?)` → rows changed, `transaction(fn)`, `beginTransaction`, `commit`, `rollback`, `inTransaction`, `lastInsertId`, `tables`, `hasTable`, `schema`, `close`; props `driver`, `name` |
| `QueryBuilder` | `select`, `distinct`, `where(col, value)` / `where(col, op, value)` / `where(dict)` / `where(fn)` for a `( … )` group, `orWhere`, `whereIn`, `whereNotIn`, `orWhereIn`, `whereNull`, `whereNotNull`, `orWhereNull`, `whereBetween`, `join`, `leftJoin`, `orderBy`, `orderByDesc`, `latest`, `oldest`, `groupBy`, `limit`/`take`, `offset`/`skip` · `get`, `first`, `find(id)`, `value(col)`, `pluck(col, key?)`, `count`, `sum`, `avg`, `min`, `max`, `exists`, `paginate(page, perPage?)` · `insert`, `insertMany`, `insertGetId`, `update`, `increment`, `decrement`, `delete` · `toSql`, `bindings` |
| `Schema` | `create`, `createIfNotExists`, `alter` (alias `table`), `drop`, `dropIfExists`, `rename`, `hasTable`, `hasColumn`, `columns` |
| `Blueprint` | `id`, `increments`, `string(name, length?)`, `text`, `int`, `bigInt`, `float`, `decimal`, `bool`, `json`, `uuid`, `date`, `datetime`, `timestamp`, `foreignId`, `timestamps`, `softDeletes`, `unique(cols)`, `index(cols)`; inside `alter`: `dropColumn`, `renameColumn` |
| `ColumnDef` | `nullable()`, `default(v)`, `unique()`, `primary()`, `index()`, `constrained(table?, col?)`, `references(col).on(table)`, `onDelete("cascade")` |

`Database.connect()` with no arguments reads `database.default` and `database.connections.<name>` from `Config`, then `DB_DRIVER` / `DB_PATH` from the environment, then falls back to SQLite at `storage/database.db`. Every value goes through `?` placeholders; table and column names must be plain identifiers, so user input can't inject SQL. Errors raise `DbException`.

### Http

```barq
Response r = Http.get("https://api.example.com/users", #{"query": #{"page": 2}, "token": key})
if (r.ok())
    list<any> users = r.json()
Http.post(url, #{"name": "Alice"})                       // dict/list -> JSON body
Http.post(url, #{"name": "Alice"}, #{"form": true})      // form-encoded

Http.serve(karo(Request req): any
    if (req.path == "/")
        return "<h1>Hello</h1>"                           // string -> HTML
    if (req.path == "/api/user")
        return #{"name": req.query("name", "guest")}      // dict/list -> JSON
    return Response.text("Not found", 404)
, #{"port": 8000, "static": "public"})
```

| Name | Members |
|------|---------|
| `Http` | `get(url, options?)`, `delete`, `head`, `post(url, data?, options?)`, `put`, `patch`, `request(method, url, options?)`, `serve(handler, options?)`. Client options: `headers`, `query`, `timeout` (seconds, default 30), `token` (Bearer), `form`. Server options: `port` (8000), `host` (127.0.0.1), `static` (folder of files), `max` (stop after n requests), `quiet` |
| `Response` | `Response.make(body?, status?, headers?)`, `Response.json(data, status?)`, `Response.html`, `Response.text`, `Response.redirect(url, status?)`, `Response.noContent()`, `new Response(...)`; props `status`, `body`, `headers`; methods `json()`, `ok()`, `failed()`, `header(name, default?)`, `withHeader`, `withStatus` |
| `Request` | props `method`, `path`, `url`, `body`, `headers`; methods `all()`, `input(key, default?)`, `query(key?, default?)`, `only(keys)`, `has(key)`, `json()`, `header(name, default?)`, `ip()`, `isJson()` |

The client returns a `Response` for every HTTP status (4xx/5xx included — check `ok()`); only network problems raise `HttpException`. In tests, `Http.fake()` answers instead of the network (see [Http.fake](#httpfake-tests-without-a-network)). The server handles one request at a time. An uncaught error in the handler becomes a 500 and its trace is printed; the server keeps running.

### Csv, Validation, Config

| Module | Members |
|--------|---------|
| `Csv` | `parse(text, header?, delimiter?)` → `list<dict<string, string>>` (or `list<list<string>>` with `header = false`), `read(path, …)`, `encode(rows, delimiter?)`, `write(path, rows, delimiter?)`. Errors raise `CsvException` |
| `Validation` | `make(data, rules, messages?)` → `Validator` with `fails`, `passes`, `errors`, `messages`, `first(field?)`, `has`, `validated`; `validate(data, rules, messages?)` returns the validated fields or raises `ValidationException` (its `errors` prop holds the details); `check(value, rules)` → bool |
| `Config` | `get("app.mail.port", default?)`, `set`, `has`, `all`, `load(folder?)` — reads every `config/*.bq` (the variable named after the file, e.g. `dict<string, any> app = #{…}` in `config/app.bq`) and `config/*.json` under the project root, on first use |

Validation rules, written `"required|email"` or `#["required", "email"]`: `required`, `nullable`, `sometimes`, `string`, `int`, `numeric`/`float`, `bool`, `email`, `url`, `uuid`, `alpha`, `alphaNum`, `alphaDash`, `min:n`, `max:n`, `between:a,b`, `size:n`, `in:a,b`, `notIn:a,b`, `regex:/…/`, `same:field`, `different:field`, `confirmed`, `date`, `list`, `dict`, `accepted`, `digits:n`, `startsWith:x`, `endsWith:x`, `json`. Form input arrives as strings, so `int` and `numeric` also accept `"25"`, and `min`/`max` then compare the number. Custom messages are keyed `"field.rule"` or `"rule"` and can use `:field`, `:min`, `:max`.

## Standard library — batch 3 (v0.6.0)

New reserved class names: `Route`, `Session`, `TestClient`, `XmlNode`, `Message`, `PendingMail`, `JobQueue`. New exceptions: `AbortException` (has `status`), `XmlException`, `MailException`, `QueueException`, `AssertionException`. A runnable app using most of it is in [examples/notes/](examples/notes/main.bq).

### Route — router and middleware

```barq
use App\Controllers\UserController
use App\Middleware\Auth

Route.alias("auth", Auth)                                   // a class with handle(Request req, any next)
Route.globalMiddleware(karo(Request req, any next): any     // runs around every request
    Response res = next(req)
    return res.withHeader("X-Powered-By", "Barq")
)
Route.get("/", kr(): string => "<h1>Home</h1>").name("home")
Route.get("/users/:id", kr(int id): string => "user {id}").whereNumber("id")
Route.get('/posts/{slug?}', kr(Request req, string slug): string => "post {slug}")
Route.resource("users", UserController)                     // index, create, store, show, edit, update, destroy
Route.group(#{"prefix": "api/v1", "middleware": "auth", "name": "api."}, karo(): void
    Route.get("/me", #[UserController, "me"]).name("me")
)
Route.serve(#{"port": 8000})                                 // same options as Http.serve
```

- **Paths.** Parameters are written `:id` or `{id}`, optional ones `:id?` / `{id?}`. Inside `"double quotes"` `{id}` would be string interpolation, so use `:id` there, or single quotes: `'/users/{id}'`. Trailing slashes are ignored. `where("id", "[0-9]+")` or `whereNumber("id")` restricts a parameter (use `(?:…)`, not `(…)`, for groups).
- **Handlers** are a function or `#[ControllerClass, "method"]` (a new controller is made per request). Arguments are filled by type: a `Request` parameter gets the request, every other parameter gets the route parameter with the same name (or the next one in order), converted to its type — `int id` with `/users/abc` is a 404. Handlers return a `Response`, a string (HTML), a dict/list (JSON) or null (204).
- **Middleware** is a function `(Request req, any next)`, a class with `handle(Request req, any next)`, or a name registered with `Route.alias`. `"role:admin,editor"` passes `"admin"` and `"editor"` as extra string arguments. Middleware can return early or change what `next(req)` returned.
- **Errors.** No route → 404 (or `Route.fallback(handler)`); wrong method → 405 with `Allow`; `Http.abort(403, "message")` → that status; an uncaught `ValidationException` (from `Validation.validate`) → 422 with `{message, errors}`. Error bodies are JSON when the request asks for JSON, plain text otherwise.

| Name | Members |
|------|---------|
| `Route` | `get`, `post`, `put`, `patch`, `delete`, `options`, `any(path, handler)`, `match(methods, path, handler)`, `group(options, fn)` (`prefix`, `middleware`, `name`), `resource(name, Controller, options?)` (`only`, `except`), `alias(name, middleware)`, `globalMiddleware(mw)`, `fallback(handler)`, `dispatch(req)` → `Response`, `url(name, params?)` (extra params become the query string), `has(name)`, `list()`, `clear()`, `serve(options?)` |
| a route | `name(n)`, `middleware(m or list)`, `where(param, regex)` / `where(dict)`, `whereNumber(param)`, `prefix(p)`, `path()`, `methods()`, `routeName()` |
| `Request` (new) | `Request.make(method, url, options?)` (`headers`, `json`, `form`, `body`, `cookies`, `ip`) builds a request without a network; `param(name, default?)`, `params()`, `routeName()`, `cookie(name, default?)`, `cookies()`, `session()`, `wantsJson()` |
| `Response` (new) | `withCookie(name, value, options?)`, `withoutCookie(name)`, `cookies()` |
| `Http` (new) | `abort(status, message?)` |

### Session and Cookie

```barq
Route.post("/login", karo(Request req): any
    Session s = req.session()
    s.regenerate()                       // new id after login
    s.put("user", req.input("email"))
    s.flash("status", "Welcome back!")   // readable on the next request only
    return Response.redirect("/dashboard").withCookie("theme", "dark", #{"maxAge": 86400})
)
```

| Name | Members |
|------|---------|
| `Session` | `get(key, default?)`, `put(key, value)` / `put(dict)` (alias `set`), `has`, `pull`, `forget(key or list)`, `all`, `flush`, `flash(key, value)`, `reflash`, `increment(key, by?)`, `regenerate`, `invalidate`, `token()` (CSRF token), `id`, `isNew`; `Session.config(#{"driver", "lifetime", "cookie", "path", "secure", "sameSite"})` |
| `Cookie` | `make(name, value, options?)` → a `Set-Cookie` value, `forget(name, options?)`, `parse(header)` → dict. Options: `maxAge`, `expires` (timestamp), `path` (`/`), `domain`, `secure`, `httpOnly` (default `true`), `sameSite` (default `"Lax"`) |

A session starts the first time a handler calls `req.session()`. After the handler, the server saves it and sends the `barq_session` cookie (HttpOnly, SameSite=Lax). Session ids are 256-bit random tokens; ids from cookies are checked against a strict pattern before they touch the disk. Drivers: `memory` (default — lost when the server stops) and `file` (one JSON file per session under `storage/sessions/`). Settings also come from `Config` `session.*` or `SESSION_DRIVER`. Values must be JSON-compatible. Keys starting with `_` are reserved.

### Xml

```barq
XmlNode rss = Xml.parse(IO.readFile("feed.xml"))
for item in rss.findAll("channel/item")
    print(item.attr("id"), item.textOf("title"))
print(Xml.toDict('<user id="7"><name>Ali</name></user>'))   // #{"user": #{"@id": "7", "name": "Ali"}}
print(Xml.fromDict("user", #{"@id": 7, "name": "Ali", "role": #["admin", "dev"]}))
```

| Name | Members |
|------|---------|
| `Xml` | `parse(text)` → `XmlNode`, `valid(text)`, `toDict(text)`, `fromDict(root, data, pretty?)`, `element(name, attrs?, text?)`, `encode(node, pretty?)`, `escape(s)` |
| `XmlNode` | props `name`, `attrs`, `text`, `children`; `attr(name, default?)`, `setAttr`, `find(path)`, `findAll(path)`, `textOf(path, default?)`, `add(child)` → child, `toDict()`, `toString(pretty?)` |

Paths are element names separated by `/`; `*` matches any element and a leading `//` searches all descendants (`"//item"`). Names keep their prefix (`atom:link`) and `xmlns` attributes stay as attributes. `text` is the element's own text, trimmed; when text and child elements are mixed, the text is kept but its position between the children is not. In `toDict`, attributes become `@name` keys, text next to attributes or children becomes `#text`, and repeated elements become lists. `<!ENTITY>` declarations are rejected, which blocks entity-expansion and external-entity attacks.

### Mail

```barq
class WelcomeMail
    public string name = ""
    // __construct(string name) ...
    #[Public]
    #[void]
    karo build(Message m)
        m.subject("Welcome, {this.name}!").text("Thanks for joining.").html("<p>Thanks for joining.</p>")

Mail.to(user.email).cc("team@shop.pk").send(new WelcomeMail(user.name))
Mail.to(user.email).queue(new WelcomeMail(user.name))          // send from a Queue worker
Mail.raw("Your order shipped.", karo(Message m): void
    m.to("ali@example.com").subject("Order #12")
)
```

| Name | Members |
|------|---------|
| `Mail` | `to(address or list)` → `PendingMail`, `send(mailable or fn)`, `raw(text, fn)`, `config(settings)`, `fake()`, `sent()`, `driver()` |
| `PendingMail` | `cc`, `bcc`, `send(mailable or fn)`, `queue(mailable, queueName?)`, `later(seconds, mailable)` |
| `Message` | `to`, `cc`, `bcc`, `replyTo`, `from(address, name?)`, `subject`, `text`, `html`, `header(name, value)`, `attach(path, name?)`, `attachData(content, name, mime?)` — all return the message |

A mailable is any object with a `build(Message m)` method. Drivers: `log` (default — prints the message to stderr), `smtp` (`host`, `port`, `username`, `password`, `encryption` `"tls"` / `"ssl"` / `"none"`, `timeout`), `file` (writes `.eml` files to `storage/mail/`) and `array` (keeps them for `Mail.sent()`; `Mail.fake()` switches to it). Settings come from `Mail.config()`, `Config` `mail.*`, or `MAIL_MAILER`, `MAIL_HOST`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_ENCRYPTION`, `MAIL_FROM_ADDRESS`, `MAIL_FROM_NAME`. Addresses are validated, and line breaks in addresses, subjects or headers are rejected, so user input can't inject headers.

### Queue

```barq
namespace App\Jobs
class SendInvoice
    public int orderId = 0
    public int tries = 3          // optional: attempts before the job counts as failed
    public int backoff = 10       // optional: seconds between attempts
    // __construct(int orderId) ...
    #[Public]
    #[void]
    karo handle()
        ...
```
```barq
Queue.push(new SendInvoice(42))
Queue.on("emails").later(300, new SendReminder(user.id))
Queue.work(#{"queue": #["default", "emails"], "stopWhenEmpty": false})   // a worker: loops until Ctrl+C
```

| Name | Members |
|------|---------|
| `Queue` | `push(job, queue?)` → id, `later(seconds, job, queue?)`, `on(name)` → `JobQueue`, `work(options?)` → jobs run (`queue`, `max`, `stopWhenEmpty` (default `true`), `sleep`, `quiet`), `size(queue?)`, `clear(queue?)`, `failed()`, `retry(id or "all")`, `flushFailed()`, `config(#{"driver", "path", "queue"})`, `driver()`, `fake()`, `pushed()` |
| `JobQueue` | `push`, `later`, `size`, `clear`, `work`, `name` |

A job is an object with a `handle()` method (or, except with the database driver, a function). A job that keeps failing is moved to the failed list, and its `failed(Exception e)` method runs if it has one. Drivers: `sync` (default — runs the job immediately), `memory` (runs when `Queue.work()` is called, same process), `database` (SQLite at `storage/queue.db`; a separate worker process can run the jobs). The database driver stores a job's properties — numbers, strings, bools, lists, dicts, sets, enums and other objects of your own classes — and rebuilds the object in the worker without calling `__construct`. Job classes must be autoloadable (in their own namespaced file) for that. Settings also come from `Config` `queue.*` or `QUEUE_CONNECTION`.

### Cli

```barq
Cli.command("greet", "Say hello", karo(list<string> rest): int
    string name = Cli.ask("Your name?", "stranger")
    if (Cli.confirm("Shout?"))
        name = name.upper()
    Cli.success("Hello, " + name)
    return 0
)
exit(Cli.run("My tool"))      // barq run tool.bq greet --verbose
```

| Name | Members |
|------|---------|
| `Cli` | `args()` (positional arguments), `options()`, `option(name, default?)`, `flag(name)`, `hasOption(name)`, `line`, `info`, `success`, `warn`, `error` (the last two go to stderr), `color(text, name)`, `bold`, `ask(question, default?)`, `confirm(question, default?)`, `secret(question)`, `choice(question, options, default?)`, `table(headers, rows)`, `command(name, description, fn)`, `run(title?)` → exit code |

Options: `--name=value`, `--flag` (true), `--no-flag` (false), `-v`, `-abc` (three flags), and `--` ends the options. `--name value` (with a space) is read as a flag plus a positional argument. Colors are used only on a terminal, and never when `NO_COLOR` is set.

### Test

```barq
// tests/cart_test.bq
use App\Cart

Test.describe("Cart", karo(): void
    Test.it("adds items", karo(): void
        Cart cart = new Cart()
        cart.add("pen", 2)
        Test.assertEquals(2, cart.count())
    )
    Test.it("rejects negative amounts", karo(): void
        Test.assertThrows(ValueException, kr(): void => new Cart().add("pen", -1), "negative")
    )
)
Test.it("serves the home page", karo(): void
    Test.assertStatus(Test.client().get("/"), 200)
)
```

`barq test` runs every `*_test.bq`, `*Test.bq` and `test_*.bq` file under `tests/` (or the files and folders you name), each in a fresh interpreter, with the current folder as the project root. It prints each test, then the failures with their file and line, and exits with 1 if anything failed. A file can also call `Test.run()` itself.

| Name | Members |
|------|---------|
| `Test` | `it(name, fn)` (alias `test`), `skip(name, fn?)`, `describe(name, fn)`, `beforeEach(fn)`, `afterEach(fn)` (inside a `describe` they apply to that group), `run()` → bool, `client()` → `TestClient`, `fail(message?)`, `assertEquals(expected, actual, message?)`, `assertNotEquals`, `assertTrue`, `assertFalse`, `assertNull`, `assertNotNull`, `assertContains(needle, haystack)`, `assertNotContains`, `assertCount(n, collection)`, `assertEmpty`, `assertNotEmpty`, `assertThrows(ExceptionClass, fn, messagePart?)` → the exception, `assertInstanceOf(Class, value)`, `assertMatches(regex, text)`, `assertGreaterThan(limit, actual)`, `assertLessThan`, `assertStatus(response, code)` |
| `TestClient` | `get(path, options?)`, `delete`, `post(path, data?, options?)`, `put`, `patch`, `request(method, path, options?)`, `withHeaders(dict)`, `cookies()`, `clearCookies()` — sends requests to `Route` without a network and keeps cookies between them, so sessions work. `data` is sent as JSON, or as a form with `#{"form": true}` |

## Templates (v0.8.0)

Views are HTML files under `views/` with a few Barq tags. `view("users.index", data)` renders `views/users/index.bq` and returns an HTML `Response`, so a route handler can return it directly. The `views/` folder holds templates, not programs: `barq compile` skips it.

```html
<!-- views/users/index.bq -->
<extends layout="layout" />

<section name="title">Users</section>

<section name="content">
    <if condition="{len(users) == 0}">
    <p>No users yet.</p>
    <else>
    <ul>
        <each user in {users}>
        <li class="{loop.odd ? "odd" : "even"}">{loop.number}. {user.name} — {user.email}</li>
        </each>
    </ul>
    </if>
    <include file="partials.card" with="{#{"title": "Tip"}}" />
</section>
```

```html
<!-- views/layout.bq -->
<html>
<head><title><yield name="title">My App</yield></title></head>
<body>
    <include file="partials.nav" />
<yield name="content" />
    <script>const boot = {!json(config)!};</script>
</body>
</html>
```

```barq
Route.get("/users", karo(): Response
    return view("users.index", #{"users": db.table("users").get(), "config": #{"debug": false}})
)
```

| Syntax | Meaning |
|--------|---------|
| `{expr}` | Any Barq expression, HTML-escaped. `null` prints nothing |
| `{!expr!}` | Raw, not escaped — only for HTML you trust |
| `\{` | A literal `{`. `<verbatim>…</verbatim>` leaves a whole block untouched |
| `{-- … --}` | Template comment, not sent to the browser. HTML comments are sent as they are, and `{…}` inside them is not evaluated |
| `<if condition="{x}">` / `<elseif condition="{y}">` / `<else>` / `</if>` | Conditions; `<if {x}>` is short for the same. Conditions must be `bool`, as in Barq |
| `<each item in {list}>` / `<each key, value in {dict}>` | Loops, with `<else>` inside for an empty list. `loop` holds `index` (from 0), `number` (from 1), `count`, `first`, `last`, `odd`, `even`, `parent` (the outer loop) |
| `<include file="partials.nav" with="{dict}" />` | Renders another view. It sees the current variables plus the `with` dict |
| `<extends layout="layout" />` + `<section name="x">…</section>` | Fills the layout's sections. A template that extends can only have `<section>` and `<let>` at the top level |
| `<yield name="x" />` or `<yield name="x">default</yield>` | Where a layout prints a section. A `<section>` in a layout is printed in place unless a child template replaces it |
| `<let total="{a + b}" label="text" />` | Sets template variables |

- **Expressions are plain Barq**, parsed by the Barq parser: method calls, `??`, ternaries, lambdas, the magic helpers (`str_upper`, `list_map`…), classes and functions of the file that called `view()`. Two template-only conveniences: `a.b` also reads a dict key (so database rows work as `{user.name}`), and the left side of `??` may be a missing variable or key (`{title ?? "Untitled"}`).
- **Escaping.** `{…}` escapes `< > & " '`. Inside `<script>` and `<style>`, plain braces are JavaScript/CSS and are left alone; only `{!…!}` is filled in there. Use `{!json(value)!}` to pass data to a script: `json()` escapes `<`, `>` and `&`, so a `</script>` in the data can't end the tag.
- **Tidy output.** A line holding only a control tag (`<if>`, `<each>`, `<section>`, `</if>`…) is left out of the output, and a view file's final newline is dropped.
- **Errors** point into the template: `at views/users/index.bq:12:9`, with the line and a caret, followed by the Barq call trace. Syntax errors raise `TemplateException`; errors inside expressions raise their usual exception (`NameException`, `TypeException`…).
- **Caching.** A view is parsed once and kept until its file changes, so a running server picks up edits.
- An HTML `<section>` without a `name` attribute is ordinary HTML.

| Name | Members |
|------|---------|
| `view(name, data?, status?)` | Renders a view → `Response` (`text/html`) |
| `View` | `render(name, data?)` → string, `string(template, data?)` renders template text, `exists(name)`, `share(name, value)` / `share(dict)` — variables every view sees, `shared()`, `path(folder?)` — get or set the views folder (default `views`), `escape(value)`, `json(value)`, `clearCache()` |

Names use dots or slashes (`"users.index"`, `"users/index"`); `..` and absolute paths are rejected.

## `barq serve` and `barq route:list` (v0.10.0)

```bash
barq serve                      # http://127.0.0.1:8000 — routes/web.bq, and routes/api.bq under /api
barq serve --port 3000 --host 0.0.0.0
barq serve --no-reload          # one process, no file watching
barq route:list                 # method, path, name, action, middleware
```

`make:*`, `migrate` and `db:seed` come with [barq-framework](https://github.com/barq-lang/barq-framework).

- `barq serve` runs the app in a child process.
- It restarts the child when anything in `app/`, `routes/`, `config/`, `views/`, `database/` or `.env` changes.
- If the app crashes, it waits for the next save.
- Files in `public/` are served as static files.
- Ctrl+C now stops a running server cleanly, including `Route.serve()` started from `barq run`.

## Storage (v0.11.0)

New file: `barq/storage.py`. Tests: [tests/cases/storage.bq](tests/cases/storage.bq).

```barq
Storage.put("reports/jan.txt", text)        // the default disk: storage/app/
Storage.get("reports/jan.txt")              // null when missing
Storage.json("data/settings.json")
Storage.exists(p)   Storage.missing(p)   Storage.delete(p or list)   Storage.copy(a, b)   Storage.move(a, b)
Storage.append(p, line)   Storage.prepend(p, line)   Storage.size(p)   Storage.mime(p)   Storage.lastModified(p)
Storage.files("reports")   Storage.files("reports", true)   Storage.allFiles()   Storage.directories()
Storage.makeDirectory(p)   Storage.deleteDirectory(p)   Storage.path(p)
Storage.disk("public").url("avatars/42.png")    // "/storage/avatars/42.png"
return Storage.download("reports/jan.pdf", "January.pdf")   // or Storage.response(p) to show it inline
```

- **Disks** come from `config/filesystems.bq`: `#{"default": "local", "disks": #{"name": #{"driver": "local", "root": "...", "url": "..."}}}`.
- Built in: `local` (`storage/app/`) and `public` (`storage/app/public/`, url `/storage`). `barq storage:link` links `public/storage` to it, so `barq serve` serves those files.
- **Safe paths.** A path can't leave its disk: `"../x"` is a `StorageException`.
- **Contents** are strings. Binary files survive a `get()`/`put()` round trip, and a response serves the original bytes.
- **Tests.** `Storage.fake("public")` swaps in an in-memory disk with `assertExists(p)` / `assertMissing(p)`. `Storage.build(#{"driver": "local", "root": "tmp"})` makes a one-off disk.

### Imports win over modules (v0.11.0)

- `use App\Middleware\Auth` (or any import) now wins over a standard-library module of the same name in that file. Before, the module won.

## Language additions (v0.12.0)

Features the spec proposed but the interpreter refused until now. They work on both engines, in the static checker, and in the framework (route handlers, the container).

### Default values, named arguments, variadics

```barq
#[string]
karo greet(string name = "World", string greeting = "Hello")
    return "{greeting}, {name}!"

greet()                          // Hello, World!
greet("Ali", greeting: "Salaam") // Salaam, Ali!
greet(greeting: "Hi")            // Hi, World!

#[int]
karo sum(*int numbers)           // numbers is a list<int>
    int total = 0
    for n in numbers
        total += n
    return total

sum()                            // 0
sum(1, 2, 3)                     // 6
list<int> more = #[4, 5]
sum(*more, 6)                    // 15 — *list spreads a list into the arguments
```

- **Defaults** must be constants: literals, `-1`, lists and dicts of literals, `Class::CONSTANT` or `Enum::Case`. They are worked out on every call, so `list<int> seen = #[]` is a new list each time. Once a parameter has a default, the ones after it need one too (a variadic can still come last).
- **Named arguments** go after the positional ones and work with any function, method, lambda or constructor written in Barq: `new User(name: "Ali", age: 30)`. Built-in functions take positional arguments only.
- **Variadic** parameters are written `*Type name`, must be last, and collect the remaining positional arguments into a `list<Type>` (empty when there are none). Each one is type-checked: `sum(1, "2")` is `argument 2 of sum() expects int, got string`.
- Route handlers can use defaults for missing route parameters: `karo index(Request req, int page = 1)` serves both `/posts` and `/posts/:page`. The container keeps defaults it can't fill: `karo __construct(Clock clock, int retries = 3)`.

### Interfaces

```barq
interface HasArea
    const UNIT = "cm²"

    #[float]
    karo area()

interface Shape extends HasArea      // an interface can extend several: extends A, B
    #[string]
    karo name()

class Square implements Shape        // or: class Square extends Base implements Shape, Other
    ...
```

An interface holds method signatures (no body, always public) and constants. A class that implements it must have each method, public, with the same static-ness, accepting the interface's arguments (it may add optional ones). Abstract classes can leave methods to their subclasses. Interfaces work as types (`list<HasArea>`, `Shape s = sq`), with `is`, in `except` (`except Retryable as e`), and as container keys (`app().bind(Clock, FixedClock)`). Enums can implement interfaces: `enum Suit implements Labeled`.

### Traits

```barq
trait Counts
    public int count = 0
    static int made = 0

    #[int]
    karo bump()
        this.count += 1
        return this.count

class Robot
    use Counts, Greets               // a trait can `use` other traits too
```

A trait's properties, methods, static properties and constants are copied into each class that uses it, as if written there: `private` members are reachable from the class, `self::` and `__class__` mean the using class, and every class gets its own static values. A method written in the class replaces the trait's. Two traits giving the same method is an error, unless you write the method in the class or choose one with `insteadof` ([v0.13](#trait-rules-insteadof--as)). A trait can declare `#[Abstract]` methods the class must provide. Traits are not types, and `new Trait()` is an error.

### Constructor promotion

```barq
class Point
    #[void]
    karo __construct(public int x, public int y = 0, private string label = "p")
        print("made {this.label}")
```

A visibility keyword in front of a constructor parameter declares the property and assigns it before the body runs. Only `__construct` (in a class or trait) can do this.

### Multi-line strings, long lines, `::class`

```barq
string html = """
    <div class="card">
        <h2>{title}</h2>
    </div>
    """
string pattern = '''
    ^\d{3}-\d{4}$
    '''

int total = price +
    shipping +
    tax

this.hasMany(Post::class)
```

- `"""…"""` strings interpolate and take escapes; `'''…'''` strings are literal (no escapes either). The line break after the opening quotes is dropped, so is a last line that only holds the closing quotes, and the indentation all lines share is removed (the closing line counts too, like Java text blocks).
- A line that ends with an operator — `+ - * / % ** == != < <= >= ?? ? => =`, a compound assignment, `and`, `or`, `not`, `in`, `is` — carries on to the next line, whatever its indentation. Lines ending in `:`, `,` or `>` don't, because headers like `karo f(): list<int>` end that way. Inside brackets, line breaks were already free.
- `User::class` is the class itself, so code written with PHP habits works wherever a class is expected. `self::class` is not supported; use `__class__` for the current class's name.

## Language additions (v0.13.0)

The language gaps left open after v0.12. All three work on both engines and in the static checker.

### Nullable types

```barq
?int age = null              // int, float and bool can't hold null — ?int can
?int count                   // no value: null (a plain `int count` starts at 0)
list<?int> scores = #[90, null, 75]

#[?User]
karo find(int id)
    return id == 1 ? new User("Ali") : null

print(find(2)?.name ?? "nobody")
int n = age ?? 0
print(age is ?int)           // true, also when age is null
```

- `?` goes in front of any type: variables, parameters, properties (`public ?int age`), return types (`#[?User]` or `: ?User`) and type arguments (`list<?int>`).
- It matters most for `int`, `float` and `bool`, which reject null. `string`, collections and class types already accept null, so `?string` and `?User` behave the same as `string` and `User`, but they tell the reader that null is expected. Making plain `User` reject null would break existing code, so that's left to a later decision (#94).
- `?any`, `?void` and `?null` are errors, and so is a nullable cast (`~?int(x)`).
- Assigning null to a plain `int` is an error that suggests the fix: `variable 'x' is int and cannot be null — make it ?int to allow null`.
- **Gate checks** typed `?User` are called for guests too (with `null`), so `kr(?User u): bool => …` replaces the old `any u` workaround (#86).

### The `karo` type

```barq
#[int]
karo applyTwice(karo kr, int value)
    return kr(kr(value))

karo double = kr(int n): int => n * 2
applyTwice(double, 3)                    // 12
applyTwice(kr(int n): int => n + 10, 1)  // 21
list<karo> steps = #[double, inc]        // named functions, lambdas,
karo bump = counter.bump                 // bound methods and built-ins all fit
```

`karo` is also the type of any callable value: a named function, an anonymous function or lambda, a bound method (`obj.method`) or a built-in. Anything else is a type error (`argument 'kr' of applyTwice() expects karo, got int`); `type(double)` is `"karo"` and `double is karo` is true. The type doesn't describe parameters or the return type yet (#95); the function itself still checks its arguments when it is called. (Before v0.22.0 this type was spelled `function`; that now gives an error saying to write `karo`.)

`karo` and `kr` are keywords only where they start a function: `karo name(`, `karo(` and a lambda `kr(...) => ...` / `kr(...): type => ...`. Anywhere else `karo` is the type and `kr` is an ordinary name, as in `karo kr` and `kr(kr(value))` above.

### Trait rules: `insteadof` / `as`

```barq
class Person
    use Hello, Salaam
        Hello::greet insteadof Salaam    // Hello's greet() wins
        Salaam::greet as salaam          // ...and Salaam's is still there as salaam()
        wave as protected                // change a method's visibility
        wave as public waveHi            // a copy under a new name, with its own visibility
```

- The rules go in an indented block under the `use` line (Barq has no `{ }` blocks). `insteadof` isn't a reserved word, so it can still be used as a name elsewhere.
- `Trait::method insteadof A, B` keeps one trait's method and drops it from the others. `as` gives a method a new name, a new visibility, or both. The original name stays unless an `insteadof` removed it, and an alias can still reach a method that `insteadof` dropped, as in PHP.
- `method as …` without a trait name is fine when only one trait has that method. Otherwise the error asks for `Trait::method`. Rules can name only traits used by this class, and only their methods (not properties). Abstract methods can't be renamed.
- A trait that uses other traits can have rules too. Methods keep late binding: in a trait, `this.greet()` calls whichever `greet()` the class ended up with.

## Forms (v0.14.0)

Form helpers in `barq/web.py`, used by the barq-auth pages but available to every app.

```barq
// views: {!csrf_field()!}  {!method_field("DELETE")!}  {old("email", "")}  {errors.first("email")}
dict<string, any> data = req.validate(#{"email": "required|email"})   // fails: back to the form, errors + input flashed
return back().with("status", "Saved")              // redirect("/x"), back(fallback?), Response.back()
return back().withErrors(#{"email": "Taken."}).withInput()
session("status")                                  // session() is the request's Session
```

- **`"csrf"` middleware.** POST/PUT/PATCH/DELETE must send the session token, as a `_token` field or an `X-CSRF-TOKEN` header. Otherwise the response is 419. The generated routes use it. `TestClient` skips the check, as Laravel's tests do. `new TestClient().withCsrfChecks()` turns it on.
- **Method spoofing.** A POST with `_method=PUT|PATCH|DELETE` (or `X-HTTP-Method-Override`) is routed as that method.
- `req.all()` / `input()` / `only()` leave out `_token` and `_method`.
- **`errors` in every view** is an `ErrorBag`: `has(field?)`, `first(field?)`, `get(field)`, `all()`, `any()`, `isEmpty()`, `count()`, `keys()`, `messages()`. A view's own `errors` variable wins.
- **`back()`** uses the Referer header. Without one it uses the last HTML page the session saw.

## Named arguments for built-ins, `Http.fake` (v0.15.0)

### Named arguments for built-ins

Built-in functions that list their parameter names take named arguments, as Barq functions do: `team.addMember(user, role: "admin")`, `team.invite(email, send: false)`. The team, session and OAuth functions in barq-auth do. Most older built-ins are still positional only, and say so in the error. A skipped optional argument is passed as `null`, which these functions read as "use the default".

### Http.fake: tests without a network

```barq
Http.fake(#{
    "https://api.test/token": #{"access_token": "t"},        // dict/list: a JSON body
    "api.test/users/*": kr(dict<string, any> r): any => #{"asked": r["url"]},    // a function gets the request
    "https://api.test/text": "plain text",                   // string: the body
    "https://api.test/gone": 410,                            // int: the status
    "https://api.test/made": Response.json(#{"id": 7}, 201),
    "*": 404                                                 // everything else
})
Http.recorded()     // #[#{"method", "url", "headers", "body", "data", "query"}, ...]
Http.restore()      // back to the network
```

Patterns use `*` as a wildcard. A pattern without `http://` or `https://` matches both, and the query string only counts when the pattern has one. `Http.fake()` with no argument answers every request with an empty 200. While faking, a URL that matches nothing throws `HttpException`, so a test never reaches the real network by mistake.

## File uploads and S3 disks (v0.17.0)

New files: `barq/upload.py` (multipart parsing, `UploadedFile`, file validation rules) and `barq/s3.py` (S3 disks, AWS Signature V4 with the standard library only). Tests: [tests/cases/uploads.bq](tests/cases/uploads.bq), `Http.test_uploads` (a real multipart POST to `Http.serve`), `S3Disk` (AWS's signature examples, and a fake S3 server that checks every signature).

```barq
// <form method="POST" action="/avatar" enctype="multipart/form-data"> ... <input type="file" name="avatar">
Route.post("/avatar", karo(Request req): any
    dict<string, any> data = req.validate(#{"avatar": "required|image|max:2048|dimensions:min_width=100"})
    UploadedFile f = data["avatar"]               // or req.file("avatar")
    string path = f.store("avatars", "public")    // "avatars/<40 random chars>.png"
    return #{"url": Storage.disk("public").url(path)}
)

req.file("avatar")        req.file("photos")      // name="photos[]" gives a list
req.hasFile("avatar")     req.files()             req.all()      // all() includes the files

f.name()  f.extension()  f.mime()  f.size()  f.get()  f.dimensions()  f.isImage()  f.isValid()  f.error()
f.clientExtension()  f.clientMime()  f.hashName()
f.store("avatars")  f.store("avatars", "s3")  f.store("avatars", #{"disk": "s3"})
f.storeAs("avatars", "user-42.png", "public")  f.storePublicly("avatars")  f.storePubliclyAs("avatars", "a.png")
Storage.putFile("avatars", f)  Storage.putFileAs("avatars", f, "a.png")  Storage.put("a.png", f)
```

- **The type comes from the file's bytes**, not the name or the `Content-Type` the browser sent: `mime()` / `extension()` look at the first bytes (png, jpeg, gif, webp, bmp, pdf, zip, gzip, mp3, ogg, mp4, svg); other text files use the name (`notes.md` → `text/markdown`), other binary is `application/octet-stream` / `bin`. So `shell.php` holding a PNG is stored as `.png`, and `evil.php` holding PHP code is `text/plain` / `.txt`. `clientExtension()` and `clientMime()` give what was sent. Office files (docx, xlsx...) are zip files and keep their extension.
- **Names:** `name()` is the sent name without folders (`C:\pics\me.png` → `me.png`). `store()` never uses it — it picks `hashName()`, 40 random characters plus the detected extension. `storeAs()` takes your name; it may not contain `/` or `..`.
- **Limits** (config/filesystems.bq): `max_upload` — KB per file, default 10240 (10 MB). A bigger file arrives with `isValid()` false and `error()` set; the `file` rule says "The avatar failed to upload." and storing it throws. `max_post` — KB per request body, default 20480; `Http.serve` / `barq serve` answer 413 without reading it. Files are kept in memory.
- **Validation rules:** `file`, `image` (jpeg, png, gif, bmp, webp; `image:allow_svg` adds svg), `mimes:jpg,png` (detected extension; jpg = jpeg), `mimetypes:image/*,application/pdf`, `extensions:pdf` (the sent name's extension), `dimensions:min_width=100,max_width=..,min_height=..,max_height=..,width=..,height=..,ratio=3/2`. `min` / `max` / `size` / `between` measure files in kilobytes.
- **Forms:** files are never flashed into `old()` when validation sends a form back. A file input left empty is not sent at all, so `required` fails and `nullable` passes.
- **Tests:** `UploadedFile.fake("cv.pdf", "contents")`, `UploadedFile.fake("big.pdf", 3000)` (3000 KB of zeros), `UploadedFile.fakeImage("me.png", 640, 480)` (a real PNG; `.jpg` / `.gif` names give a small file with the right header and size). Pass files in the data: `client.post("/avatar", #{"avatar": file})` and `Request.make("POST", "/x", #{"form": #{...}, "files": #{"cv": file}})` build a real multipart body. `Json.encode(file)` gives `{"name", "mime", "size"}`.

### S3 disks

```barq
// config/filesystems.bq
"s3": #{"driver": "s3", "key": env("AWS_ACCESS_KEY_ID"), "secret": env("AWS_SECRET_ACCESS_KEY"),
        "region": env("AWS_DEFAULT_REGION", "us-east-1"), "bucket": env("AWS_BUCKET"),
        "endpoint": env("AWS_ENDPOINT"),   // MinIO: "http://127.0.0.1:9000"; R2: "https://<account>.r2.cloudflarestorage.com"
        "url": env("AWS_URL")}             // optional public base (a CDN) for url()

Storage.disk("s3").put("reports/jan.pdf", data)
Storage.disk("s3").temporaryUrl("reports/jan.pdf", 300)    // a presigned link, valid 300 seconds (max 7 days)
```

- Every `Disk` method works on S3 except `path()`. Folders are key prefixes; `makeDirectory("x")` writes an empty `x/` object. `copy()` / `move()` copy inside the bucket. Listings follow S3's paging.
- Options: `path_style` (bucket in the path; default true when `endpoint` is set, false for AWS), `root` (a folder inside the bucket every path lives under), `token` (session token), `timeout` (seconds, 30).
- `url()` is `url` + path when `url` is set, else the bucket's address. `temporaryUrl()` works on S3 disks only.
- Errors become `StorageException` with S3's code: `"Storage.put(): S3 error SignatureDoesNotMatch: ..."`.
- `Storage.fake("s3")` swaps it for an in-memory disk in tests, as with the others.

### Also new

- The `Http` client keeps every byte of binary responses (no charset, not text/json/xml), so downloaded images survive `Storage.put()`.

## Formatter: `barq fmt` (v0.18.0)

First piece of Phase 3 (tooling). Formats `.bq` files in place, using the rules in [step 11 §11.3b](docs/step-11-versioning-roadmap.md#formatter-rules).

```bash
barq fmt                     # every .bq file under the current folder
barq fmt app/ routes/web.bq  # some folders and files
barq fmt --check             # change nothing; list files that need it, exit 1 if any (for CI)
barq fmt --diff              # change nothing; print a unified diff
barq fmt - < in.bq > out.bq  # stdin to stdout (for editors)
barq fmt --indent 2          # spaces per level (default 4)
barq fmt --no-sort           # leave the order of `use` lines alone
```

What it changes:

- **Indentation**: 4 spaces per level; tabs become spaces. Lines inside brackets, continued lines, `.method()` chains and `karo(...)` bodies inside a call keep their shape and move with the code around them.
- **Spaces in a line**: one around binary operators (`a + b`, `x == y`, `=>`, `??`, `? :`), after commas, keywords (`if (`, `while (`, `not (`) and colons (`): int`, `#{"a": 1}`, `name: v`). None inside brackets (`#[1, 2]`, `f(x)`), around `.` `?.` `::` `\`, in type arguments (`dict<string, list<int>>`), after unary `-`, `~` casts, `?` nullable and `*` spread. A `<` written as `a < b` stays a comparison; `list<` and a tight `Name<...>` are type arguments.
- **Blank lines**: at most one in a row; exactly one before every top-level `karo name`, `class`, `enum`, `interface` and `trait` (with the comments and `#[...]` attributes right above it) and one after its body; none at the start or end of the file, and one final newline.
- **Imports**: each run of top-level `use` lines (no blank line between them) is sorted alphabetically.
- Trailing spaces go; Windows line endings become `\n`.

What it never touches: strings (including `"""` strings) and comments are copied unchanged (the inner lines of a `/* */` comment move with its first line). Line breaks stay where the author put them; long lines are not wrapped.

**Safety check:** every result is lexed again and must give exactly the same tokens as the original (the `use` sort only reorders whole lines). If it doesn't, the file is left alone and `barq fmt` reports a formatter bug. Files that don't lex are reported and skipped; folders named `views/` (templates are HTML), `node_modules/`, `vendor/`, `__pycache__/` and hidden folders are skipped.

Not done (yet): hand-aligned `=` columns are collapsed to one space (#122); quotes are not switched between `"` and `'` (#123); `namespace` is not moved above `use` lines (the checker already wants it first).

## `barq check` and packages (v0.18.0)

```bash
barq check app/ routes/          # every error in every file, without running anything
barq check --json main.bq        # one JSON object per error, for editors
barq check --json --path app/Models/User.bq - < buffer.bq   # an unsaved buffer
barq packages                    # barq-framework 0.18.0-alpha, barq-auth ..., or "No Barq packages installed"
```

- `barq check` runs the parser and the checker (the same errors `barq run` stops on). For a file declaring `namespace App\Models` it finds the project root from the folders, so `use` lines resolve as they do at run time.
- JSON lines carry `path`, `line`, `col`, `endCol` (the end of the word at that spot), `kind` and `message`. The [VS Code extension](https://github.com/barq-lang/vscode-barq) uses them.
- The packages are described in [Repositories and packages](#repositories-and-packages). A command from a missing package (`barq migrate` without barq-framework) says which package it comes from.

## Language server: `barq lsp` (v0.19.0)

Editors start `barq lsp` and talk the [Language Server Protocol](https://microsoft.github.io/language-server-protocol/) with it on stdin/stdout. The [VS Code extension](https://github.com/barq-lang/vscode-barq) does this for you; for another editor, point its LSP client at `barq lsp` for `.bq` files. For example, in Neovim 0.11:

```lua
vim.filetype.add({ extension = { bq = "barq" } })
vim.lsp.config("barq", { cmd = { "barq", "lsp" }, filetypes = { "barq" }, root_markers = { ".env", "app", "routes", "main.bq" } })
vim.lsp.enable("barq")
```

| Feature | What it does |
|---------|--------------|
| Diagnostics | The errors `barq check` reports, while you type (after a 0.3 s pause) and on save |
| Completion | Variables and parameters in scope; members after `.` (`this.`, `user.`, `Math.`, `"text".`) and `::` (`Status::`); classes of the project — picking one not imported yet adds its `use` line; built-in functions, modules and classes; keywords and types; `use` paths; attributes after `#[` |
| Hover | The declaration as written (`karo greet(string other, int times = 1): string`), the `//` comment above it, and the class it belongs to |
| Go to definition | Functions, classes, methods, properties, constants, enum cases, variables, and the file of a `use` line |
| References, highlights | Variables within their function; functions and classes across the project; methods and properties by name after `.` / `::` |
| Rename | Variables and parameters (other renames are refused for now — see #132) |
| Signature help | Parameter names and the one being typed, for Barq functions, methods, constructors and built-ins |
| Outline, workspace symbols | Classes, enums, interfaces, traits, functions, methods, properties, constants, cases |
| Formatting | `barq fmt` (the editor's tab size is the indent) |

- Types come from declarations — every Barq variable, parameter, property and method states one — so `this.repo.find(id).` completes when `find` has a return type. List and dict element types are used: with `list<User> people`, `people[0].` completes `User`'s members. Model statics such as `User.find()` and `User.create()` give a `User`. Built-in methods have no declared return types yet, so a chain stops after one (`Database.connect(...).` does not complete).
- While a file has a syntax error, the line with the error is replaced by a placeholder and the rest is read as usual, so completion keeps working on the line being typed.
- The project is every `.bq` file under the editor's workspace folders (not `views/`, `vendor/`, `node_modules/` or hidden folders), re-read when files change on disk. Open files are taken from the editor.
- Settings (LSP `initializationOptions`, or `workspace/didChangeConfiguration` under `barq`): `diagnostics`, `diagnosticsOnType`, `sortImports` — all `true` by default.
- Columns are UTF-16 units, as LSP requires, so lines with Urdu text or emoji underline the right word.

## Starting a project: `barq new` (v0.20.0)

```bash
barq new my-app              # a web app
barq new my-api --api        # JSON only: routes/api.bq, no views or public/
barq new my-tool --console   # a program: main.bq, app/, tests/
barq new my-app --auth       # the web app plus barq-auth's auth:install
barq new . --force           # fill in an existing folder; files already there are kept
```

A web app gets `routes/web.bq` and `routes/api.bq` (served under `/api`), `app/Controllers/Controller.bq` (the abstract base class every controller extends, as in Laravel — the place for helpers controllers share), `app/Controllers/HomeController.bq`, `views/layout.bq` and `views/home.bq`, `public/css/app.css`, `config/app.bq` and `config/database.bq`, a feature test in `tests/feature/`, `.env` with a fresh `APP_KEY` (plus `.env.example`), `.gitignore` and a README. Everything it writes passes `barq check`, `barq fmt --check` and `barq test`.

Installed packages add their own files through `hooks.NEW_PROJECT` (`--no-packages` leaves them out). With barq-framework: `app/Providers/AppServiceProvider.bq`, the `User` model, its migration, `UserFactory` and a `DatabaseSeeder` — so `barq migrate`, `barq db:seed` and `barq serve` work straight away. With `--auth`, barq-auth's `auth:install` writes the `User` model and `users` table instead.

## Packages: `barq install` (v0.21.0)

Packages are git repositories, on GitHub by default — there is no central server.

```bash
barq install acme/strings            # newest version; saved as "^1.2.0" in barq.json
barq install acme/strings@1.2.0      # or @^1.2, @~1.2.3, @">=1.0 <2.0", @1.2.*, @*
barq install acme/strings@main       # a branch, tag or commit
barq install                         # everything barq.json lists (as pinned in barq.lock)
barq update [acme/strings]           # newest versions barq.json allows
barq remove acme/strings
barq packages                        # what's loaded and what's in vendor/
```

`barq install` picks, for each package, the newest git tag (`v1.2.0` or `1.2.0`) that fits every constraint on it — from `barq.json` and from the packages that need it — copies it into `vendor/<user>/<name>/` and writes the exact commit of each to `barq.lock`. Commit `barq.json` and `barq.lock`; leave `vendor/` out of git (`barq new` does both). While `barq.lock` still fits `barq.json`, `barq install` puts back exactly those commits; `barq update` looks again. Conflicts are reported with who wants what, e.g. `no version of acme/strings fits ^2.0 (wanted by barq.json), ^1.0 (wanted by acme/report)`.

Once installed, `use` finds the package's classes — in `barq run`, `barq test`, `barq check` and the language server:

```barq
use Acme\Strings\Shout          // vendor/acme/strings/src/Shout.bq

print(Shout.it("salaam"))
```

### Writing a package

A package is a repository with a `barq.pkg` at its top and its code in `src/`:

```barq
// barq.pkg
name        = "strings"
version     = "1.2.0"
description = "String helpers"
license     = "MIT"
namespace   = 'Acme\Strings'        // single quotes: "\S" would be an escape
requires    = #{
    "acme/text-core": "^1.0",
    "barq": ">=0.21.0-alpha"         // the Barq version it needs
}
```

`namespace` maps to `src/` (`source = "lib"` changes the folder): `Acme\Strings\Shout` is `src/Shout.bq`, `Acme\Strings\Sub\X` is `src/Sub/X.bq`. Inside the package's own folder its namespace resolves too, so its tests and `barq check` work there. Packages that add commands or built-ins in Python (a `barq_<name>/` folder, like barq-framework, barq-auth and barq-permissions) install the same way and load from `vendor/`.

`barq publish` checks `barq.pkg` and every file in `src/`, makes sure the git tree is clean and the version is new, then tags it `v<version>`. `--push` pushes the tag (otherwise it prints the command); `--dry-run` only checks.

Settings: `BARQ_REGISTRY` — where packages come from, a template with `{name}` (default `https://github.com/{name}.git`; a folder of repositories works too). `BARQ_CACHE` — where downloads are kept (default `~/.cache/barq`, one bare git clone per package). `git` must be installed.

## Deploying: `barq build` (v0.22.0)

```bash
barq build                   # dist/
barq build --zip             # dist/ and dist.zip
barq build --out ../release  # somewhere else
```

`barq build` makes a folder that runs on its own. The server needs Python 3 and nothing else — no Barq checkout, no `barq install`, no `PYTHONPATH`:

```
dist/
  app/          the project, with every .bq file already checked and compiled (.barq/cache/)
  runtime/      this Barq and the packages it loaded (barq-framework, barq-auth...)
  barq          any barq command, run in app/:  dist/barq migrate,  dist/barq route:list
  start         runs the app: a web app is served (HOST, PORT; default 127.0.0.1:8000,
                no reloading), a console app runs main.bq with the arguments given
  barq.cmd, start.cmd    the same for Windows
  build.json    the Barq and Python versions, the packages, the entry, file counts
```

A file that doesn't compile stops the build, and the previous `dist/` stays as it was. Packages in `vendor/` must match `barq.lock` (run `barq install` first); they travel inside `app/vendor/`. The launcher loads exactly the packages the build had (`BARQ_PACKAGES`), not whatever else the server has installed.

Left out by default: `tests/`, `.env` (it usually holds secrets — give the server its own `app/.env`, or set the variables; `--with-env` copies it), `*.db` / `*.sqlite`, the files in `storage/` (its folders are kept), `.git`, `.gitignore`, `.github`, `.barq`, `node_modules`, `__pycache__`. Symlinks inside the project (like `public/storage` from `barq storage:link`) are kept. More options, also settable in `barq.json`:

```json
{
    "name": "shop",
    "require": {},
    "build": {"entry": "main.bq", "exclude": ["docs", "*.log"], "include": ["tests"]}
}
```

`--exclude` / `--include` (repeatable) add to those lists. A pattern without `/` matches a name anywhere; with `/` it matches a path from the project root (`database/*.db`). `--entry FILE` picks the program `start` runs (a file in the project folder). `--force` replaces an `--out` folder that `barq build` didn't make.

Compiled files are tied to the Barq and Python versions: on a server with another Python 3 the app still runs, it just compiles each file on first use. Moving `dist/` doesn't matter — the cache follows it (see [the `.bqc` cache](#the-bqc-cache)).

## Debugging: `barq debug` and `barq dap` (v0.24.0)

```bash
barq debug main.bq                     # stop on the first line
barq debug -b add -b app/Cart.bq:13 -c main.bq     # run to a breakpoint
barq debug -b "app/Cart.bq:13 if price > 100" main.bq
barq debug --exceptions all main.bq    # stop on every raise (default: uncaught only)
```

At the `(barq)` prompt:

```
→ main.bq:10 in <main>
    10 │     total = add(total, i)
(barq) s                        step into add()
→ main.bq:5 in add
     5 │     int c = a + b
(barq) v                        the variables in scope
  a = 0
  b = 0
(barq) bt                       the call stack (u / d move up and down it)
(barq) p a + b * 10             any Barq expression, run in this frame
(barq) a = 7                    change a variable (its type is checked)
(barq) b app/Cart.bq:13 if price == 20
(barq) o                        run until add() returns: back on the line of the call
(barq) c                        continue to the next breakpoint
```

`n` steps over calls, `l` lists the source, `b` alone lists breakpoints, `cl N` removes one, `q` stops the program. Enter repeats the last step. Anything that isn't a command is evaluated, so `total` prints `total` (`p n` for a variable named like a command). Ctrl+C while the program runs stops it at the next line.

A breakpoint on a line with no statement (a blank line, a comment, `karo add(...)`) moves to the next statement. `breakpoint()` in the code stops there when a debugger is attached and does nothing in `barq run`. An uncaught exception stops on the line that raised it, with the variables of every frame it passed through; continuing reports it as usual.

**Editors** run `barq dap`: the [Debug Adapter Protocol](https://microsoft.github.io/debug-adapter-protocol/) on stdin/stdout. It supports launch (`program`, `args`, `cwd`, `env`, `stopOnEntry`, `noDebug`), breakpoints with conditions, hit counts (`5`, `>= 5`, `% 5`) and log messages (`total is {total}`), uncaught / all exceptions, step over / into / out, pause, Locals / Globals / Exception scopes, watch and hover (no calls on hover), evaluate in the debug console and setting values. The program's output becomes output events; its stdin is empty. The [VS Code extension](https://github.com/barq-lang/vscode-barq) 0.3.0 starts it for F5.

How it works: the compiler gives every instruction its Barq line, so Python's `sys.monitoring` (PEP 669) reports Barq lines directly. Line events are turned on only in files with breakpoints, or everywhere while stepping — code without breakpoints runs at full speed. Variables are read from the frames (the compiler records, per variable, the lines where it can be seen). Expressions run on the tree-walker in the stopped frame, so calls into compiled code work. Debugging is VM-only: `--interp` programs and template expressions can't be stepped through.

## The reference application: barq-blog (v0.25.0)

[barq-blog](https://github.com/barq-lang/barq-blog) is a complete blog written only in Barq — Phase 5 of the roadmap and the place to look for how a Barq web app fits together: public pages with search, pages, categories and tags; accounts from barq-auth; comments; admin / editor / writer roles from barq-permissions; an admin area with Markdown posts, drafts, publishing, trash and categories; a JSON API with tokens. Its tests (`sh test.sh`) run on both engines against a fresh, seeded database.

Writing it turned up a few things, fixed in this version:

- `use Database\Seeders\X` finds `database/seeders/X.bq` on case-sensitive file systems. A `use` path now tries the path as written, then the first folder in lowercase (`app/Models/X.bq`), then every folder in lowercase.
- `barq fmt` treated a keyword after a dot as a keyword: `Regex.match(x)` became `Regex.match (x)`.
- Controllers have a base class, as in Laravel: `barq new` writes `app/Controllers/Controller.bq` (`abstract class Controller`), and `HomeController`, `barq make:controller` and the controllers `auth:install` writes extend it (a controller in a subfolder adds `use App\Controllers\Controller`). The blog keeps its shared helpers there: `this.user()`, `this.authorize()`, `this.pageNumber()`, `this.page()`.
- `barq compile app` (a folder inside the project) resolves classes from the project root, found from each file's namespace — it used `app/` as the root, so `class HomeController extends Controller` failed to compile there while `barq run` worked.
- `barq new` puts `/dist/` (what `barq build` makes) in `.gitignore`; `barq auth:install` writes `routes/auth.bq` the way `barq fmt` would (sorted `use` lines).

## Choices made where the spec is still open

The interpreter needed an answer for these to run at all. Each is easy to change — say the word and I'll switch it and update the tests.

| # | Open decision | What the interpreter does now |
|---|---------------|-------------------------------|
| 9, 21 | Null-safe `?.` / null coalescing | Both implemented: `user?.name ?? "Guest"` |
| 8 | Dot access on dicts | Not allowed — `d["key"]` only (dot is for methods) |
| 11 | Indentation | Spaces **or** tabs, but one style per file |
| 17 | Loop with index | `for i, v in list` (no `enumerate()`) |
| 22 | `do/while` | Supported |
| 23, 28 | Static members & access | `#[Static]` / `static`; both `Class::member` and `Class.member` work |
| 33 | Stdlib imports | `Math`, `Str` are global; `use Barq\Math` also works |
| 10 | Default params | Supported (v0.12); constants only, worked out on every call |
| 15 | Named args | Supported for functions written in Barq; positional arguments first |
| 16 | Variadics | `*int nums` (a `list<int>`), last parameter only; `f(*list)` spreads |
| 25 | Interfaces | `interface X extends A, B`, `implements`; methods + constants; checked when the class is loaded |
| 26 | Traits | `use A, B` in the class body; class methods win; a method in two traits is an error unless an `insteadof` rule picks one |
| 27 | Constructor shorthand | `__construct(public int x)` |
| 29 | Multiple inheritance | One parent class, any number of interfaces and traits |
| 91 | Multi-line strings | `"""…"""` and `'''…'''`, shared indentation removed |
| 92 | Long lines | A line ending in an operator carries on |
| 94 | Nullable types | `?T` (v0.13); only changes `int`, `float`, `bool` — `string`, collections and classes still accept null without `?` |
| 95 | Type for function values | `karo` (v0.22; `function` from v0.13 until then), with no parameter or return types |
| 96 | Trait conflict rules | `insteadof` / `as` in an indented block under `use` (v0.13) |
| — | `print` vs `println` | The spec says `print` has no newline, but almost every example assumes one. `print` adds a newline (like Python); `write()` doesn't |
| — | `int / int` | Stays `int`, rounded toward zero (`7 / 2` → `3`), like Go/Java. Use floats for `3.5` |
| — | Conditions | Must be `bool`: `if (count)` is an error, write `if (count > 0)` |
| — | `null` | Allowed for `string`, collections and class types; `int`, `float`, `bool` need `?int` etc. |
| — | `switch` | No fall-through except stacked empty `case` lines; `break` leaves the switch |
| — | Methods without a visibility attribute | Public |
| — | Project root for autoloading | The folder of the file you run |
| — | Dates | A date is an `int` Unix timestamp (seconds). `Date.format`/`parse` use UTC unless you pass `"local"`, an offset like `"+05:00"`, or a zone name |
| — | `Hash.bcrypt` | Python has no built-in bcrypt, so passwords use salted PBKDF2-SHA256 (310k rounds) under `Hash.password()` / `Hash.verify()` |
| — | `Crypto.encrypt/decrypt` | Not yet — needs AES, which Python's standard library doesn't have. Will come with the native runtime |
| 35 | Cache backend | In-memory only, for now |
| 38 | DB config location | Both: `config/database.bq` (via `Config`) and `.env` (`DB_DRIVER`, `DB_PATH`) |
| 39 | SQL vs NoSQL API | `table()` only for now; `collection()` comes with MongoDB support |
| 40 | Raw query return type | `list<dict<string, any>>` |
| 42 | Multiple connections | Supported — call `Database.connect()` as often as you like |
| — | Database drivers | SQLite only (Python ships it). MySQL, Postgres and MongoDB need network drivers, so they come with the native runtime |
| — | Booleans in SQLite | SQLite has no bool type: `true` is stored as `1` and read back as `1` |
| — | Dates in the schema | `date`/`datetime`/`timestamp` columns hold `int` Unix timestamps, matching `Date` |
| — | `Response` | One class for both sides: what `Http.get()` returns and what a server handler returns |
| 2 | VM vs native compilation | Bytecode VM for now; v0.5.0 compiles to CPython bytecode |
| 5 | Cache file extension | `.bqc`, stored under `.barq/cache/` in the project root |
| 30 | Route parameter syntax | Both `{id}` and `:id`. `"{id}"` in double quotes is string interpolation, so `:id` is the form that works everywhere; `{id}` needs single quotes |
| 93 | `UserController::class` | The class itself (a class name is already a value), so `#[UserController, "show"]` and `#[UserController::class, "show"]` are the same |
| — | Handler arguments | Filled by type: `Request` → the request, anything else → the route parameter of the same name, converted (`int id`) |
| — | Defaults for the new modules | Session `memory`, Mail `log`, Queue `sync`: nothing is sent or stored by accident while developing |
| — | Mailables | Any object with `build(Message m)`; no base class needed |
| 46 | Layout inheritance | HTML tags (`<extends>`, `<section>`, `<yield>`), matching the Option C template model, not `@extends` |
| 47 | Template file extension | `.bq` files under `views/` (as in Step 9) |
| 48 | Unescaped output | `{!expr!}` (Step 9's form) |
| — | Dot access in templates | `a.b` reads dict keys inside templates only; Barq code still uses `d["key"]` |
| — | Queued jobs | Any object with `handle()`; optional `tries`, `backoff` properties and `failed(Exception e)` method |
| 52 | Model table names | Plural snake_case of the class name (`BlogPost` → `blog_posts`); `protected string table` overrides |
| 53 | Pagination | Page-based: `paginate(perPage?, page?)` returns `data`, `total`, `page`, `perPage`, `lastPage`, `hasMore` |
| 55 | Migration file names | `YYYY_MM_DD_HHMMSS_name.bq` |
| 44 | Migrations | Explicit commands only (`barq migrate`), no auto-migrate |
| — | `Post::class` in relations | Not needed: `this.hasMany(Post)` — a class name is a value |
| — | Mass assignment | Off-list columns are an error, not silently dropped. With neither `fillable` nor `guarded`, every column is allowed |
| — | Relation as property | `user.posts` (no parentheses) is the loaded result; `user.posts()` is a query |
| — | Model config | Ordinary properties (`protected list<string> fillable = …`); `static` works too |
| — | Soft deletes | A model property, `protected bool softDeletes = true` (no traits yet); `deleted_at` holds an `int` timestamp |
| — | Service providers | Every class in `app/Providers/` (file-name order) unless `config/app.bq` lists `providers`; booted automatically by run/test/serve/migrate/db:seed |
| — | `barq serve` routes | `routes/web.bq` as is, `routes/api.bq` under `/api`; `public/` served as static files |
| — | Container access | `app()` / `app(Name)` global and `this.app` in providers (no `App` facade, so it can't clash with the `App\` namespace) |
| — | Event names | Strings, wildcards with `*`; firing an object uses its class name; a listener returning `false` stops the rest |
| — | Factory lookup | `User.factory()` → `UserFactory`, from `database/factories/`; factories ignore `fillable`, like Laravel's |
| 72 | Storage disk default | `local` = `storage/app/`; `public` = `storage/app/public/` served at `/storage` after `barq storage:link` |
| — | Auth config | `config/auth.bq` in the shape of [barq-auth.md](https://github.com/barq-lang/barq-auth/blob/main/docs/barq-auth.md) (`default_guard`, `guards`, `providers`); guards `web` (session) and `api` (token) |
| — | Auth traits | `createToken()`, `tokens()`, `tokenCan()`, `can()`, `cannot()` are on every `Model`, not opt-in `use HasApiTokens` (traits exist since v0.12; #87 decides) |
| 86 | Guests and the Gate | Denied without calling the check, unless its user parameter is `?User` or `any` |
| — | Policy discovery | `App\Models\Post` → `App\Policies\PostPolicy`; `Gate.policy()` overrides |
| — | Token format | The row id, a bar, then a 40-hex-character secret; the table stores only the secret's SHA-256, like Sanctum |
| — | Unauthenticated web requests | Redirect to the route named `login` if there is one, otherwise 401 |
| — | Schedule file and time zone | `routes/console.bq`; times in the server's local time unless config `app.timezone` or `.timezone()` says otherwise |
| — | `Schedule.job()` | Pushes the job onto the Queue (so the queue driver decides when it runs) |
| 97 | CSRF protection | A `"csrf"` middleware the generated routes use, not on for every route; `TestClient` skips it unless `withCsrfChecks()` |
| 98 | 2FA secrets at rest | Stored as plain text: Python's standard library has no AES. They get encrypted with `APP_KEY` when the native runtime adds `Crypto.encrypt` |
| 99 | Login with 2FA | `TwoFactor.attempt()` returns `"login"` / `"two-factor"` / `"failed"`; `Auth.attempt()` is unchanged and never asks for a code |
| 100 | Where `auth:install` puts routes | `routes/auth.bq`, loaded before `routes/web.bq` by `serve`, `route:list` and `test` |
| 101 | Signed URL contents | The path and query are signed, not the host, so links survive a proxy or a changed domain |
| 102 | Form helper globals | `back()`, `redirect()`, `old()`, `session()`, `csrf_token()`, `csrf_field()`, `method_field()`, and `errors` in views |
| 103 | Linking a social login to an existing account | Only when the provider says the email is verified (Google `email_verified`, GitHub's verified primary email). Facebook doesn't say, so its emails count as unverified: new Facebook users verify their email as usual, and an existing account must log in with its password |
| 104 | Team roles | The owner is `teams.user_id`, not a row in `team_members`; other roles come from config; the owner passes every `hasTeamRole()` check; `members()` includes the owner |
| 105 | Who can accept a team invitation | A logged-in user whose email matches the invitation. Guests log in first and come back through `intended()` |
| 106 | Logging out other devices | Their stored sessions are deleted and `remember_token` changes (Laravel instead re-checks a password hash on every request). Works with all three session drivers |
| 107 | `Http.fake()` and unknown URLs | They throw `HttpException` (Laravel sends a real request unless `preventStrayRequests()`) |
| 108 | Named arguments for built-ins | Only built-ins that list parameter names (the v0.15 auth functions); the rest stay positional. Skipped ones are `null` |
| 109 | Intended URL | Saved for GET requests when the `auth` middleware turns a guest away; `intended()`, `URL.intended()`; only same-site paths |
| 110 | Where the role methods live | On every `Model` (like the team methods); `use HasRoles` is accepted as an empty trait |
| 111 | `hasRole(list)` | All of them, as the package page says (spatie: any one). `hasAnyRole()` for any |
| 112 | Unknown permission names | Checks return `false`; giving or assigning one throws `PermissionException` (spatie throws on checks too) |
| 113 | Team-scoped roles | `team_id` on the assignment tables, not on `roles`; an assignment without a team counts in every team; no "current team" is assumed — pass `team:` |
| 114 | Guards in checks | Without `guard:`, roles and permissions of every guard count; names given to `assignRole()` use the model's default guard |
| 115 | Guests and `role:` / `permission:` middleware | 401 (redirect to `login` for web pages), like `auth`; spatie answers 403 |
| 116 | Permission cache | In memory per process with a stamp file for other processes, not the `Cache` module (which is per process too) |
| 130 | Language server | `barq lsp` in the reference implementation (Python, no dependencies), not a separate program |
| 131 | Types in the editor | From declarations only, no guessing from values; built-in methods have no return types yet |
| 132 | Rename and references | Rename only variables and parameters; references to methods and properties match by name, not by class |
| 134 | Where packages go | `vendor/<user>/<name>/` in the project; `barq.json` lists them, `barq.lock` pins commits |
| 135 | Manifest formats | A package's `barq.pkg` uses Barq literals (as the spec shows); the project's `barq.json` is JSON, because `barq install` rewrites it |
| 136 | Picking versions | The newest tag that fits every constraint, no backtracking; a pre-release sorts just below its release and is not excluded (`^0.21.0` allows `0.21.1-alpha`); a package with no tags installs its default branch |
| 137 | Downloading | With `git` (one cached bare clone per package), not GitHub's API, so any git host or folder works |
| 138 | Names in `requires` | Full `user/name` (the spec's example used `"helper-lib"`); the key `barq` is the language version |
| 139 | `barq publish` | A git tag `v<version>`, pushed with `--push`; nothing is uploaded anywhere |
| 140 | What `barq build` makes | A folder (app + runtime + start scripts), optionally zipped — not a single executable; Python 3 must be on the server |
| 141 | What a build leaves out | Tests, `.env`, databases, files in `storage/`, `.git`, `.barq`, `node_modules`; `barq.json` `"build"` and `--exclude` / `--include` change it |
| 142 | Where `start` listens | 127.0.0.1:8000 (`HOST`, `PORT`), so it sits behind a proxy by default; `HOST=0.0.0.0` for a container |
| 143 | Moved caches | A `.bqc` written at another path is reused with the new path and rewritten, not recompiled; `serve`, `migrate` and other project commands now use the cache too |
| 144 | How the debugger runs code | The VM under Python's `sys.monitoring` — the same code `barq run` runs, full speed without breakpoints; `--interp` programs can't be debugged |
| 145 | A statement that stops | `breakpoint()`, a builtin (no new keyword); it does nothing without a debugger |
| 146 | When exceptions stop | Uncaught only by default (`--exceptions all` / `none`); uncaught = it leaves the last Barq frame |
| 147 | Terminal commands | pdb's (`n s c b p l bt u d q`), plus `o` (step out) and `v` (variables); other input is evaluated |
| 148 | A plain hit count `5` | Stops from the fifth hit on; `== 5` for only the fifth, `% 5` for every fifth |

## Not built yet (next phases)

- Connecting a social account to a logged-in user; team-scoped API tokens; custom `Role` / `Permission` model classes
- Streaming uploads to disk instead of memory; S3 multipart uploads for very large files
- `barq build` as one executable file (it makes a folder now). Done: formatter, `barq check`, VS Code extension (v0.18.0), language server (v0.19.0), `barq new` (v0.20.0), package manager (v0.21.0), `barq build` (v0.22.0), debugger (v0.24.0)
- Debugger: `barq serve` and `barq test` under the debugger, attaching to a running program, stepping through templates
- Packages: a searchable index of packages, checksums for downloads, backtracking when versions conflict
- Language server: return types for built-in methods, rename of classes and members, code actions (quick fixes)
- `barq check`: a member that a built-in module doesn't have (`Regex.test(...)`) is only found when the line runs
- `barq test`: a way to give tests their own database without a script (`.env.testing`, or running migrations from a test)
- Language: function types with signatures (`function(int): int`), strict non-null class types (#94)

## Contributing

Run `python3 tests/test_barq.py` before sending a change; it must pass with no package installed. A change to the hooks should also keep the package suites passing (`python3 tests/test_framework.py` in barq-framework, and so on, with the repos cloned side by side). See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
