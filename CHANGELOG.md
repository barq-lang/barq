# Changelog

All versions are alphas of the reference implementation. Details for each are in the [README](README.md).

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
