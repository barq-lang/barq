# Changelog

All versions are alphas of the reference implementation. Details for each are in the [README](README.md).

## 0.26.0-alpha — 2026-11-11

- **The native VM (Phase 6 starts).** A VM written in Rust, in `native/` (no dependencies outside Rust's standard library): `cargo build --release --manifest-path native/Cargo.toml`, then `barq run --native app.bq` (or `BARQ_ENGINE=native`). Python still parses and checks the file and hands the tree over as JSON; the Rust side compiles it to Barq's own bytecode (a stack VM with local slots, closure cells and fused instructions for loops and `+=`) and runs it.
- It runs the core language, traits included, with the reference interpreter's messages, error positions and call traces: all 99 test programs, error tests, benchmarks and examples it can run print exactly what the tree-walker prints (`NativeEngine` tests). Programs that use what it doesn't have yet — `use` (more than one file), the standard library modules, the magic helpers, templates — stop before running with `NativeError: … is not supported by the native VM yet`.
- 1.8–3.9× faster than the VM on the benchmarks, startup included (`benchmarks/run.sh` has a native column). The checked tree is cached as `.barq/cache/<file>.bqn`, so an unchanged file isn't parsed again.
- On the native VM `int` is 64-bit (overflow throws `ValueException`), memory is reference-counted, and a bare `raise` works only in its own `except` block. Provisional decisions #149–#153.
- Decision #59 / #60 (the production VM's language): **Rust**.
- CI builds the native VM and runs its tests.

## 0.25.0-alpha — 2026-11-11

- **The reference application: [barq-blog](https://github.com/barq-lang/barq-blog)**, a blog written only in Barq (roadmap Phase 5): public pages with search and pages, categories and tags, accounts, comments, admin / editor / writer roles, an admin area with Markdown posts, drafts, publishing and trash, and a JSON API with tokens. 34 tests, on both engines.
- `use Database\Seeders\X` finds `database/seeders/X.bq` on case-sensitive file systems (every folder of a `use` path may be lowercase). The language server finds such files too.
- `barq fmt` no longer puts a space after a keyword used as a member name: `Regex.match(x)` stays as it is.
- **A base controller**, as in Laravel: `barq new` writes `app/Controllers/Controller.bq` (`abstract class Controller`), and `HomeController` extends it. (barq-framework: `make:controller` extends it when the project has one; barq-auth: the `auth:install` controllers extend it, and it is written if missing.)
- `barq compile <folder>` finds the project root from each file's namespace, as `barq check` does, so classes from the same namespace (a parent class, say) resolve.
- `barq new` adds `/dist/` to `.gitignore`. (barq-auth: `routes/auth.bq` is written with sorted `use` lines, so `barq fmt --check` passes on a new project.)

## 0.24.0-alpha — 2026-11-11

- **The debugger.** `barq debug app.bq` runs a program and stops on its first line: breakpoints (a line, `FILE:LINE` or a function name, with `if` conditions), step over / into / out, the call stack, variables in every frame, and Barq expressions evaluated in the stopped frame (`p user.name`; `x = 5` changes a variable, type-checked). It stops where an uncaught exception was raised, with that frame's variables (`--exceptions all` stops on every raise).
- **`barq dap`: the Debug Adapter Protocol** server editors use. The [VS Code extension](https://github.com/barq-lang/vscode-barq) 0.3.0 runs it for F5: breakpoints with conditions, hit counts and log messages, Locals / Globals / the exception, watch and hover, changing values, pause.
- `breakpoint()` stops there when a debugger is attached and does nothing otherwise.
- Debugging uses Python's `sys.monitoring`: with no breakpoints in a file, its code runs at full speed. The compiler records where each variable can be seen (the `.bqc` format changed, so caches are rebuilt once).

## 0.23.0-alpha — 2026-11-11

- **The type of function values is `karo`** (it was `function`): `karo applyTwice(karo kr, int value)` then `return kr(kr(value))`. `list<karo>`, `?karo`, `#[karo]`, `x is karo` all work; `type(f)` is `"karo"` and errors say `expects karo`. Writing `function` as a type is an error that says to write `karo`.
- `karo` and `kr` are keywords only where they start a function (`karo name(`, `karo(`, `kr(...) => ...`); anywhere else `karo` is the type and `kr` is an ordinary name.

## 0.22.0-alpha — 2026-11-11

- **`barq build`: a folder to deploy.** `dist/` holds the app with every file compiled, this Barq and its packages, and `start` / `barq` scripts (`.cmd` on Windows), so a server needs only Python 3. `--zip`, `--out`, `--entry`, `--exclude`, `--include`, `--with-env`, and a `"build"` section in `barq.json`. Leaves out tests, `.env`, databases and stored files by default; a file that doesn't compile stops the build and keeps the last one.
- A project folder that moved keeps its `.bqc` cache instead of compiling again.
- `barq serve`, `migrate` and the other project commands use the `.bqc` cache too (`BARQ_NO_CACHE=1` still turns it off).

## 0.21.0-alpha — 2026-11-11

- **The package manager.** `barq install user/name[@version]` downloads packages from GitHub (any git repository through `BARQ_REGISTRY`), picks the newest tag that fits every constraint (`1.2.0`, `^1.2`, `~1.2.3`, `>=1.0 <2.0`, `1.2.*`, `||`, or a branch/commit), puts them in `vendor/` and pins exact commits in `barq.lock`. `barq update`, `barq remove`, and `barq publish` (checks the package and tags its version).
- `use` reads classes from installed packages (`barq.pkg` `namespace` → `src/`) in run, test, check and the language server. Python plugin packages (barq-framework, barq-auth, barq-permissions) load from `vendor/` too.
- `barq new` writes an empty `barq.json` and ignores `vendor/`. `barq check`, `compile` and `test` skip `vendor/`.

## 0.20.0-alpha — 2026-11-11

- **`barq new`: start a project.** A web app (default), an API (`--api`) or a program (`--console`), with routes, a controller, views, config, `.env` with an `APP_KEY`, `.gitignore` and tests that pass. `--auth` runs barq-auth's `auth:install` too; `--force` fills in an existing folder without overwriting anything.
- Packages add files to new projects through `hooks.NEW_PROJECT` (barq-framework 0.20.0 adds the User model, its migration, factory and seeder, and a service provider).

## 0.19.0-alpha — 2026-11-11

- **`barq lsp`: the language server.** Editors start it and talk the Language Server Protocol on stdin/stdout. It gives completion (variables, members after `.` and `::`, project classes with their `use` line added, built-ins, keywords), hover, go to definition, references, highlights, rename of variables and parameters, signature help, outline, workspace symbols, formatting and diagnostics — from the same lexer, parser, checker and formatter that run programs. Pure Python, no dependencies.
- It keeps working while the file being edited has a syntax error.
- The [VS Code extension](https://github.com/barq-lang/vscode-barq) 0.2.0 uses it.

## 0.18.0-alpha — 2026-11-11

- **Repositories split.** The language (this repo) runs on its own. The framework, auth and permissions moved to [barq-framework](https://github.com/barq-lang/barq-framework), [barq-auth](https://github.com/barq-lang/barq-auth) and [barq-permissions](https://github.com/barq-lang/barq-permissions), which plug in through `barq/hooks.py` and are found by `barq/plugins.py` (side-by-side clones, `BARQ_PATH`, or Python's path). `BARQ_PACKAGES` picks which load.
- `barq fmt`: the formatter (`--check`, `--diff`, `--indent`, `--no-sort`, stdin).
- `barq check`: syntax and type errors without running (`--json` for editors).
- `barq packages`: lists the installed packages. Package commands (`migrate`, `make:*`, `auth:install`...) say which package they come from when it is missing.
- `barq/devtest.py`: the test helpers the package suites share.
- The [VS Code extension](https://github.com/barq-lang/vscode-barq) 0.1.0 uses `barq fmt` and `barq check`.

## 0.17.0-alpha — 2026-11-11

File uploads (`req.file()`, `UploadedFile`, upload validation rules, size limits) and S3 disks (AWS SigV4, presigned URLs).

## 0.16.0-alpha — 2026-11-06

Roles and permissions (now barq-permissions).

## 0.15.0-alpha — 2026-11-06

Social login, teams and device sessions (now barq-auth); `database` session driver, intended URLs, `Http.fake()`, named arguments for built-ins.

## 0.14.0-alpha — 2026-11-06

Auth pages, signed URLs, rate limits, password resets, email verification, two-factor login, QR codes (now barq-auth); CSRF, method spoofing, `back()` / `old()` / `errors` form helpers.

## 0.13.0-alpha — 2026-11-06

Nullable types `?T`, the `function` type, trait `insteadof` / `as` rules.

## 0.12.0-alpha — 2026-11-06

Default parameters, named arguments, variadics and spread, interfaces, traits, constructor promotion, `"""` strings, line continuation, `X::class`.

## 0.11.0-alpha — 2026-11-06

Auth, Gate and API tokens (now barq-auth), `Storage`, the task scheduler (now barq-framework).

## 0.10.0-alpha — 2026-11-06

Events, service container and providers, model events, soft deletes, `Fake`, factories (now barq-framework); `barq serve`, `barq route:list`.

## 0.9.0-alpha — 2026-11-06

Models, relations, form requests, resources, migrations, seeders, `make:*` (now barq-framework); `__get` / `__set`.

## 0.8.0-alpha — 2026-11-06

Templates: `<if>`, `<each>`, `<extends>` / `<section>` / `<yield>`, `<include>`, `{expr}` / `{!raw!}`, `view()`.

## 0.7.0-alpha — 2026-11-06

The magic helper library: ~100 global `is_*`, `list_*`, `dict_*`, `str_*`, `rx_*` functions.

## 0.6.0-alpha — 2026-11-05

`Route`, middleware, `Session`, `Cookie`, `TestClient`, `Xml`, `Mail`, `Queue`, `Cli`, `Test` and `barq test`.

## 0.5.0-alpha — 2026-11-05

The compiler: Barq compiles to CPython bytecode (4–14× faster than the tree-walker), `.bqc` cache, `barq compile` / `barq emit`.

## 0.4.0-alpha — 2026-11-05

`Database` (SQLite) with a query builder and schema builder, `Http` client and server, `Request` / `Response`, `Csv`, `Validation`, `Config`; enums.

## 0.3.0-alpha — 2026-11-05

Standard library batch 1: `Json`, `IO`, `Path`, `Env`, `Hash`, `Crypto`, `Uuid`, `Regex`, `Date`, `Arr`, `Log`, `Cache`.

## 0.2.0-alpha — 2026-11-05

The core language: lexer, parser, static checker, tree-walking interpreter, REPL.
