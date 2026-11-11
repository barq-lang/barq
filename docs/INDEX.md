# Barq Language — Documentation Index

> **Status:** Draft  
> **Current Name:** Barq (final name will be chosen from Urdu — file extension will follow the name)  
> **Goal:** A fast, full-stack language with its own built-in library and package ecosystem — no external dependencies required.

> **Code:** the language is [barq](https://github.com/barq-lang/barq) (this repo); the framework and packages are [barq-framework](https://github.com/barq-lang/barq-framework), [barq-auth](https://github.com/barq-lang/barq-auth) and [barq-permissions](https://github.com/barq-lang/barq-permissions); editor support is [vscode-barq](https://github.com/barq-lang/vscode-barq); the website is [barq-website](https://github.com/barq-lang/barq-website). See [Repositories and packages](../README.md#repositories-and-packages).

---

## Steps

| Step | File | Topic | Status |
|------|------|--------|--------|
| 1 | [step-01-overview.md](step-01-overview.md) | Language Overview & Goals | Draft |
| 2 | [step-02-syntax-variables.md](step-02-syntax-variables.md) | Syntax Rules & Variables | Draft |
| 3 | [step-03-functions.md](step-03-functions.md) | Functions | Draft |
| 4 | [step-04-control-flow.md](step-04-control-flow.md) | Control Flow (if, for, foreach, while) | Draft |
| 5 | [step-05-oop-classes.md](step-05-oop-classes.md) | OOP — Classes, Inheritance, Access | Draft |
| 6 | [step-06-namespaces.md](step-06-namespaces.md) | Namespaces & Imports | Draft |
| 7 | [step-07-standard-library.md](step-07-standard-library.md) | Built-in Standard Library | Draft |
| 8 | [step-08-database-layer.md](step-08-database-layer.md) | Database Layer (MySQL, Mongo, etc.) | Draft |
| 9 | [step-09-frontend-support.md](step-09-frontend-support.md) | Frontend / Template Support | Draft |
| 10 | [step-10-framework.md](step-10-framework.md) | MVC Framework (Laravel+Django style) | Draft |
| 11 | [step-11-versioning-roadmap.md](step-11-versioning-roadmap.md) | Versioning & Roadmap | Draft |
| — | [open-decisions.md](open-decisions.md) | All Pending Decisions | Pending |

---

## Core Principles

1. **Fast by design** — compiled or bytecode-interpreted, not script-interpreted line by line.
2. **No external dependencies** — every feature (HTTP, DB, JSON, crypto, etc.) is part of the language's own standard library.
3. **Full-stack** — backend logic and frontend rendering live in the same language.
4. **Python syntax, PHP power** — indentation-based blocks, PHP-style OOP and namespaces.
5. **Own package ecosystem** — Barq packages are distributed through Barq's own registry (like PyPI for Python, Packagist for PHP) — but built-in, not third-party.

---

## Naming

The final language name will be chosen from the **Urdu language**.  
The file extension (currently `.bq`) will be derived from the chosen name.  
Until then, all examples use `.bq` as a placeholder.

---

*All decisions marked ⚠️ in each step are open and require your input before implementation begins.*
