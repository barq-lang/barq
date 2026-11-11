# Step 11 — Versioning & Roadmap

> **Previous:** [Step 10 — MVC Framework](step-10-framework.md)  
> **Next:** [Open Decisions](open-decisions.md)

---

## 11.1 Version Numbering ✅ Decided — Semantic Versioning `1.0.0`

Barq uses **Semantic Versioning (SemVer)**: `MAJOR.MINOR.PATCH`

| Segment | Meaning | Example |
|---------|---------|---------|
| **Major** | Breaking changes to language syntax or behavior | `1.0.0` → `2.0.0` |
| **Minor** | New features, backward-compatible | `1.0.0` → `1.1.0` |
| **Patch** | Bug fixes, security patches only | `1.0.0` → `1.0.1` |

```
1.0.0  — First stable release
1.1.0  — Framework added (new feature, no breaking change)
1.1.1  — Security patch
2.0.0  — Native compilation (may have breaking syntax changes)
```

**Pre-release versions** use suffixes:
```
0.1.0-alpha   — Not stable, API may change
0.5.0-beta    — Feature-complete, testing phase
1.0.0-rc.1    — Release candidate
1.0.0         — Stable release
```

---

## 11.2 Version Declaration in Code

⚠️ **Decision pending** — see [open-decisions.md](open-decisions.md) #58.

**Option A — File-level directive:**
```barq
@version 1.0

namespace App\Models

class User
    // ...
```

**Option B — Project config only (no per-file declaration):**
```barq
// barq.config (project root)
language_version = "1.0"
```

**Option C — Not required** — the runtime uses whatever version is installed.

---

## 11.3 Development Roadmap

### Phase 1 — Language Specification *(Current)*

| Task | Status |
|------|--------|
| Write language spec (steps 1–11) | ✅ Done |
| Resolve all open decisions | ⏳ Pending your input |
| Choose final language name (Urdu) | ⏳ Pending |
| Choose file extension | ⏳ Pending |
| Write formal grammar (EBNF/PEG) | ⏳ After decisions |
| Finalize standard library API | ⏳ After decisions |

---

### Phase 2 — Reference Interpreter *(In progress — see the [README](../README.md))*

| Task | Description |
|------|-------------|
| Lexer | Tokenize `.bq` source files — ✅ Done (v0.2.0-alpha) |
| Parser | Build AST from tokens — ✅ Done |
| Static checker | Type/name errors before execution — ✅ Done (first version) |
| Interpreter | Walk the AST and execute (tree-walk interpreter) — ✅ Done |
| Standard library | Implement all Barq\* modules in Python |
| DB connectors | MySQL, PostgreSQL, SQLite, MongoDB |
| HTTP server | Built-in server for running apps |
| REPL | Interactive shell (`barq repl`) — ✅ Done |
| Bytecode compiler | Compile AST to bytecode (`.bqc` files) — ✅ Done (v0.5.0-alpha): compiles to CPython bytecode, cached in `.barq/cache/` |
| Bytecode VM | Execute `.bqc` files directly (fast path) — ✅ Done: runs on CPython's VM, 4–14× faster than the tree-walker |

**Implementation language:** Python (for the reference interpreter — the production VM will be in C/Rust later).
The v0.5.0 compiler targets CPython's bytecode, because a VM loop written in Python would be no faster than the tree-walker. A Barq-specific instruction set comes with the production VM (Phase 6).

---

### Phase 3 — Tooling

| Tool | Description |
|------|-------------|
| CLI tool (`barq`) | `barq new`, `barq run`, `barq build`, `barq test` — ✅ `run`, `test`, `compile`, `serve`, `repl` (`new` / `build` not yet) |
| Package manager | `barq install`, `barq publish`, package registry — ⏳ v0.18.0 loads packages (`barq packages`, side-by-side clones, `BARQ_PATH`); no installer or registry yet |
| Formatter | Auto-format `.bq` files — ✅ `barq fmt` (v0.18.0, see [README.md](../README.md#formatter-barq-fmt-v0180)) |
| Linter | Catch errors before runtime — ✅ first part: `barq check` (v0.18.0) reports the checker's errors without running, `--json` for editors |
| **VS Code extension** | Full IDE support — see section 11.3b below — ✅ v0.1.0 in [vscode-barq](https://github.com/barq-lang/vscode-barq): highlighting, format on save, inline errors, outline, `use` links, snippets, run commands |
| LSP (Language Server) | Powers VS Code extension + JetBrains, Vim, Neovim |
| Debugger | Step-through debugging |

---

### Phase 3b — VS Code Extension (Detailed)

The official VS Code extension provides a complete, modern developer experience. It is **the primary IDE tool** for Barq and ships before JetBrains or other editors.

#### Extension Features

| Feature | Description |
|---------|-------------|
| **Syntax highlighting** | Keywords, types, attributes, strings, comments, functions — all colored distinctly |
| **Code formatting** | Auto-format on save — consistent indentation, spacing, attribute alignment |
| **Error highlighting** | Compile-time errors shown inline as red underlines before you even run the file |
| **IntelliSense / autocomplete** | Suggests stdlib functions, class methods, variables |
| **Hover docs** | Hover over any function or class to see its signature and description |
| **Go to definition** | Ctrl+click to jump to a function, class, or variable declaration |
| **Find all references** | See everywhere a function or class is used |
| **Code snippets** | Type `barqfn` → expands full function template, `barqclass` → class skeleton, etc. |
| **Bracket matching** | Matching pairs for `#[...]`, `#{}`, `#()`, `try/except`, `switch/case` |
| **Code folding** | Fold class bodies, function bodies, switch blocks |
| **File icon** | Custom `.bq` file icon in the explorer sidebar |
| **Theme** | Optional Barq dark/light color theme |

#### Syntax Highlighting Tokens

The grammar file assigns tokens to every element:

```
Keyword:    karo, kr, class, enum, namespace, use, return, if, else, for, foreach,
            while, switch, case, default, try, except, finally, raise,
            new, extends, abstract, public, private, protected

Attribute:  #[Public] #[Private] #[Protected] #[int] #[string] #[void]
            #[Abstract] #[Static]

Type:       int, float, string, bool, null, const,
            list<T>, dict<K,V>, set<T>, User, any class name

Operator:   = += -= *= /= == != > < >= <= and or not ? : =>
            ~ (tilde cast)  ?. (null safe)  ??  |

String:     "..." (interpolated)  '...' (literal)
Interp:     {variable} inside strings — highlighted differently from surrounding string

Comment:    // single-line    /* multi-line */

Collection: #[  #{  #(  — the # prefix is highlighted as collection marker

Function:   Name after karo keyword  (karo name ← highlighted as function name;
            karo( and kr( start anonymous functions and lambdas)
Class:      Name after class keyword (class Name ← highlighted as class name)
Number:     42  3.14  0xFF
```

#### Formatter Rules

The formatter enforces consistent style across all `.bq` files:

```
1. Indentation    — 4 spaces (no tabs)
2. Attributes     — each #[...] on its own line, flush to function indent
3. Blank lines    — one blank line between top-level functions/classes
4. String quotes  — double quotes for interpolated strings, single for literals
5. Spaces         — space after keywords: if, for, while, switch
6. Collections    — no space inside: #["a","b"] not #[ "a", "b" ]
7. Operators      — space on both sides: a + b, x == y
8. Lambda         — space around =>: kr(int x): int => x * 2
9. Tilde cast     — no space: ~int(x) not ~ int(x)
10. Import order  — namespace first, then use statements, sorted alphabetically
```

> **Status (v0.18.0-alpha):** `barq fmt` applies rules 1–3 and 5–10 (`use` runs are sorted; `namespace` is not moved). Rule 4 (quote style) is not applied yet, and commas get one space after them (`#["a", "b"]`), as all the docs write it — see open decisions #122–125.

#### Snippets

| Trigger | Expands to |
|---------|-----------|
| `barqfn` | `#[Public]\n#[void]\nkaro name()\n    ` |
| `barqpub` | `#[Public]\n#[int]\nkaro name(params)\n    return ` |
| `barqclass` | `class Name\n\n    ` |
| `barqctor` | `#[Public]\n#[void]\nkaro __construct(params)\n    ` |
| `barqanon` | `karo(params): type\n    ` |
| `barqkr` | `kr(params): type => ` |
| `barqif` | `if (condition)\n    ` |
| `barqfor` | `for item in collection\n    ` |
| `barqeach` | `foreach item in items\n    ` |
| `barqtry` | `try\n    \nexcept Exception as e\n    Log.error(e.message)` |
| `barqswitch` | `switch (variable)\n    case "value":\n        ` |
| `barqmodel` | Full model class skeleton with fillable, hidden, timestamps |
| `barqcontroller` | Full controller class with index, show, store, destroy |
| `barqmigration` | Migration class with up() and down() |

#### Installation

Once published:

```bash
# From VS Code marketplace
ext install barq.barq-language

# Or from CLI
code --install-extension barq.barq-language
```

#### Extension Source Structure

```
vscode-barq/
├── package.json              // extension manifest
├── syntaxes/
│   └── barq.tmLanguage.json  // TextMate grammar for highlighting
├── language-configuration.json // brackets, comments, indentation rules
├── snippets/
│   └── barq.json             // all code snippets
├── src/
│   ├── extension.ts          // entry point
│   ├── formatter.ts          // auto-format logic
│   ├── lsp-client.ts         // connects to Barq language server
│   └── diagnostics.ts        // inline error display
└── themes/
    ├── barq-dark.json
    └── barq-light.json
```

The extension connects to the **Barq Language Server (LSP)** for IntelliSense, go-to-definition, hover docs, and real-time error checking. The LSP is a separate process shipped with the language runtime.

> **Status (v0.18.0-alpha):** the extension is built ([vscode-barq](https://github.com/barq-lang/vscode-barq), v0.1.0) in plain JavaScript with no dependencies (not TypeScript as sketched above). Until the LSP exists it calls `barq fmt -` for formatting and `barq check --json -` for errors on every edit; outline and `use` links are worked out in the extension. Completion, hover and the debugger wait for the LSP.

---

### Phase 4 — MVC Framework

| Component | Status |
|-----------|--------|
| Router | ✅ v0.6.0 — in the language (`Route`) |
| Middleware pipeline | ✅ v0.6.0 — in the language |
| ORM (Model layer) | ✅ v0.9.0 — [barq-framework](https://github.com/barq-lang/barq-framework) |
| Migrations & Seeders | ✅ v0.9.0 — barq-framework |
| Template engine | ✅ v0.8.0 — in the language (`view()`) |
| FormRequest validation | ✅ v0.9.0 — barq-framework |
| API Resources | ✅ v0.9.0 — barq-framework |
| Service Providers | ✅ v0.10.0 — barq-framework |
| CLI scaffold commands | ✅ v0.9.0 — barq-framework (`make:*`); `auth:install` in [barq-auth](https://github.com/barq-lang/barq-auth) |

### Repositories (v0.18.0)

| Repository | Contents |
|------------|----------|
| [barq](https://github.com/barq-lang/barq) | Language, standard library, CLI, `barq fmt`, `barq check`, spec (`docs/`) |
| [barq-framework](https://github.com/barq-lang/barq-framework) | ORM, migrations, container, providers, events, factories, scheduler |
| [barq-auth](https://github.com/barq-lang/barq-auth) | Authentication and authorization, `auth:install` |
| [barq-permissions](https://github.com/barq-lang/barq-permissions) | Roles and permissions |
| [vscode-barq](https://github.com/barq-lang/vscode-barq) | VS Code extension |
| [barq-website](https://github.com/barq-lang/barq-website) | Website, docs site, admin |

The language must pass its tests with no package loaded; packages plug in through `barq/hooks.py`.

---

### Phase 5 — Blog Reference Application

The blog app will be written **entirely in Barq** — no other language used.

**Features:**
- User authentication (register, login, logout)
- Create, edit, delete blog posts (admin)
- Public post listing and reading
- Comments on posts
- Tags and categories
- REST API for posts
- Full MVC structure using the framework

This app serves as:
1. Proof that Barq can build real applications
2. Reference implementation for developers learning the language
3. Living documentation — every framework feature is used in the blog

---

### Phase 6 — Production VM

| Task | Description |
|------|-------------|
| C or Rust VM | Rewrite the bytecode VM for production performance |
| Garbage collector | Efficient memory management |
| Cross-platform binaries | Native builds for Linux x64/ARM, macOS Intel/Apple Silicon, Windows x64 |
| Native compilation | Optional: compile Barq directly to native binaries |
| Concurrency | Async/await or coroutines for concurrent requests |
| Benchmarks | Publish performance comparisons vs PHP, Python, Node |

---

### Phase 7 — Official Website ✅ Planned

The official Barq website will be **built entirely in Barq** using the framework — the same way Laravel's website runs on Laravel, and Python.org runs on Python.

**The website is a proof of concept** — it shows that Barq is capable of building a real, modern, production website.

#### Pages & Features

| Section | Description |
|---------|-------------|
| **Home** | Hero, key features, performance benchmarks, quick-start code sample |
| **Docs** | Full language documentation (all 11 steps), searchable, versioned |
| **Learn** | Interactive tutorials — beginner to advanced, with examples |
| **Playground** | Try Barq in the browser — write and run code without installing anything |
| **Install** | Download the installer for Linux / macOS / Windows, one-click setup |
| **Packages** | Browse community packages from the GitHub registry |
| **Blog** | Language news, version announcements, tutorials |
| **Community** | Links to GitHub, Discord, forum |
| **Changelog** | Full version history with release notes |

#### Technical Spec of the Website

```
// Built with:
// - Barq MVC framework (the official one)
// - Barq template engine (the official one)
// - Barq ORM (database: PostgreSQL)
// - Barq HTTP server behind a reverse proxy (Nginx)
// - Deployed on Linux

// Project structure:
web/
├── routes/
│   ├── web.bq          // public pages
│   └── api.bq          // REST API (for playground runner)
├── controllers/
│   ├── HomeController.bq
│   ├── DocsController.bq
│   ├── LearnController.bq
│   ├── PlaygroundController.bq
│   └── PackagesController.bq
├── views/
│   ├── layout.bq       // main layout
│   ├── home/
│   ├── docs/
│   ├── learn/
│   └── packages/
├── models/
│   ├── DocPage.bq
│   ├── Tutorial.bq
│   └── Package.bq
└── public/
    ├── css/
    ├── js/
    └── images/
```

#### Interactive Playground

The playground sends code to a sandboxed Barq runner on the server:

```barq
// Playground API — runs user's code safely
namespace Web\Controllers

class PlaygroundController

    #[Public]
    #[string]
    karo run(Request request)
        string code    = request.input("code")
        string result  = Sandbox.run(code, timeout: 5)
        return response().json(#{"output": result})
```

---

## 11.4 Release Plan

| Version | Name | Key Features |
|---------|------|-------------|
| `0.1.0` | Spec | Language specification complete |
| `0.2.0` | Interpreter | Tree-walk interpreter, run `.bq` scripts |
| `0.5.0` | VM | Bytecode compiler + VM, ~10× faster |
| `0.8.0` | Stdlib | Full standard library, DB connectors, HTTP server |
| `1.0.0` | Stable | First stable release — Linux · macOS · Windows binaries |
| `1.1.0` | Framework | Official MVC framework released |
| `1.2.0` | Blog | Blog reference app — proves real-world capability |
| `1.3.0` | Website | Official website launches — built in Barq |
| `2.0.0` | Native | Optional native compilation, async/await, production VM |

---

## 11.4b Package Registry ✅ Decided — GitHub-based

Barq packages are **GitHub repositories** with a standard structure.  
No central server to maintain. The CLI reads directly from GitHub.

### Installing Packages

```bash
barq install username/package-name          # latest release
barq install username/package-name@1.2.0   # specific version
barq install username/package-name@main    # from branch
```

### Package Structure

A valid Barq package is any GitHub repo containing:

```
package-name/
├── barq.pkg                # package manifest
├── src/
│   └── *.bq              # package source files
└── tests/
```

### Package Manifest (`barq.pkg`)

```barq
name        = "awesome-utils"
version     = "1.0.0"
description = "Utility functions for Barq"
author      = "username"
license     = "MIT"
namespace   = "AwesomeUtils"

requires = #{
    "helper-lib": ">=1.0.0",
    "auth-tools": "^2.1.0"
}
```

### Using an Installed Package

```barq
namespace App\Controllers

use AwesomeUtils\StringHelper
use AwesomeUtils\DateHelper

class ReportController

    #[Public]
    #[string]
    karo generate()
        string slug = StringHelper.slug("My Report Title")
        return DateHelper.format(Date.now(), "Y-m-d") + "-" + slug
```

### Version Constraints

| Syntax | Meaning |
|--------|---------|
| `1.0.0` | Exactly version 1.0.0 |
| `>=1.0.0` | 1.0.0 or higher |
| `^1.2.0` | Compatible with 1.2.x (minor+patch updates) |
| `~1.2.0` | Patch updates only (1.2.x) |
| `*` | Any version |

---

## 11.5 Language Support & Compatibility

| Concern | Policy |
|---------|--------|
| **Breaking changes** | Major version only (1.x → 2.x) |
| **Deprecations** | Announced one major version before removal |
| **LTS releases** | Decided with community — pending decision #59 |
| **Platform support** | Linux (primary), macOS, Windows |
| **Minimum OS** | To be determined |

---

## 11.6 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ✅ 57 | **Version numbering scheme** | **SemVer** — `1.0.0` (Major.Minor.Patch) |
| ⚠️ 58 | **Per-file version directive** | Supported / Project config only / Not required |
| ⚠️ 59 | **LTS release policy** | Yes (e.g., every 2 years) / No LTS / Decide later |
| ⚠️ 60 | **Implementation language for VM** | Python first / Rust / C / Go |
| ⚠️ 61 | **Async/concurrency model** | async/await / coroutines / threads / not in v1 |
| ✅ 62 | **Package registry** | **GitHub-based** — `barq install username/package-name` |
| ⚠️ 63 | **Framework name** | Pending user decision — see open-decisions.md |

---

*Back to: [Open Decisions →](open-decisions.md)*
