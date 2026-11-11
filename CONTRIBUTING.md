# Contributing to Barq

Thanks for helping. Barq is pure Python 3 with no dependencies, and it should stay that way.

## Layout

- `barq/` — the language: lexer, parser, checker, compiler, VM, interpreter, standard library, CLI.
- `docs/` — the language spec (step 1 to 16) and [open decisions](docs/open-decisions.md).
- `tests/` — `python3 tests/test_barq.py` runs every program on both engines (VM and tree-walker); both must print exactly the same.
- The framework and the packages live in their own repos: [barq-framework](https://github.com/barq-lang/barq-framework), [barq-auth](https://github.com/barq-lang/barq-auth), [barq-permissions](https://github.com/barq-lang/barq-permissions).

## Before you send a change

1. `python3 tests/test_barq.py` passes. It runs with no package loaded, so the language never depends on the framework.
2. If you touched `barq/hooks.py`, `barq/plugins.py`, `barq/web.py`, `barq/stdlib2.py` or the CLI, clone the packages next to this repo and run their suites too (`python3 tests/test_framework.py` in barq-framework, `test_auth.py`, `test_permissions.py`).
3. New behaviour gets a test: a `tests/cases/NAME.bq` with its `NAME.out`, or a `tests/errors/NAME.bq` whose first line is `// expect: <message>`.
4. `barq fmt --check` on the `.bq` files you added.

## Where things go

A feature belongs in the language when a program without the framework could use it (a stdlib module, syntax, a CLI tool). It belongs in a package when it needs models, the container or auth. When the language needs to call into a package, add a hook with a working default to `barq/hooks.py` rather than importing the package.

## Writing a package

A package is a `barq_<name>` Python package whose `__init__.py` defines `NAME`, `__version__`, `REQUIRES` (other packages to load first) and `register(hooks)`. barq-framework's [`__init__.py`](https://github.com/barq-lang/barq-framework/blob/main/barq_framework/__init__.py) is a complete example. Its tests can use `barq/devtest.py`.
