# Step 1 — Language Overview & Goals

> **Previous:** —  
> **Next:** [Step 2 — Syntax & Variables](step-02-syntax-variables.md)

---

## 1.1 What is Barq?

Barq is a general-purpose, full-stack programming language designed to be:

- Written with **Python-style syntax** (indentation, clean keywords)
- Capable like **PHP** (server-side logic + HTML output + database in one file)
- **Fast** — not an interpreted script language; runs on a compiled bytecode VM
- **Self-contained** — every feature lives in Barq's own standard library; no npm, no pip, no composer

---

## 1.2 Who is it for?

| Audience | Why Barq fits |
|----------|----------------|
| PHP developers | Familiar OOP, namespaces, database patterns — cleaner syntax |
| Python developers | Identical control flow and indentation — adds native web and DB support |
| New developers | One language for backend, frontend, database, and APIs |
| Teams | One language across the full stack reduces context switching |

---

## 1.3 Language Identity

| Property | Value |
|----------|-------|
| **Name** | **Barq** (برق) — Urdu for Lightning |
| **File extension** | `.bq` |
| **Paradigm** | Multi-paradigm: OOP + procedural + functional (limited) |
| **Typing** | Strict — types are mandatory, enforced at compile time |
| **Execution** | Bytecode VM (compiled at runtime, cached for speed) |
| **Platform** | ✅ Linux · macOS · Windows — full support on all three |
| **Built-in** | HTTP server, DB connectors, template engine, package manager |

---

## 1.4 Performance Goals

Barq is designed to be **significantly faster than PHP and Python** for web workloads.

### How speed is achieved:

1. **Bytecode compilation** — `.bq` source is compiled to bytecode on first run, cached as `.bqc` files.
2. **No startup interpreter overhead** — the VM loads the cached bytecode directly.
3. **Built-in HTTP server** — no Apache/Nginx layer needed for development; production can use the built-in server or a reverse proxy.
4. **Zero external packages at runtime** — no dependency resolution at startup.
5. **Ahead-of-time type checking** — when types are declared, the VM uses optimized paths.
6. **Connection pooling built in** — database connections are pooled by the runtime automatically.

### Performance targets (aspirational):

| Benchmark | PHP 8.3 | Python 3.12 | Barq target |
|-----------|---------|-------------|---------------|
| Simple HTTP response | ~1 ms | ~2 ms | < 0.5 ms |
| DB query + render | ~5 ms | ~8 ms | < 2 ms |
| JSON API response | ~2 ms | ~3 ms | < 1 ms |

> ⚠️ These are design targets, not measured benchmarks. Actual performance depends on implementation.

---

## 1.5 Own Library & Package System

Barq ships with **everything built in**. The standard library covers:

- HTTP server and client
- Database (SQL + NoSQL connectors)
- Template/view rendering
- JSON, XML, CSV
- Cryptography (hash, encrypt, sign)
- File system
- Date and time
- Math
- String utilities
- Environment variables
- Logging
- Testing framework
- CLI argument parsing

Third-party code is distributed through the **Barq Package Registry** — a registry hosted and controlled by the Barq project. Packages are written in Barq and have no external binary dependencies.

```
# Install a package (CLI command — name TBD)
barq install some-package
```

---

## 1.6 Full-Stack Capability

A single `.bq` file can:

```barq
// Define a route
Route::get("/hello", karo(): Response
    return response("Hello World")
)

// Query a database
users = db.table("users").get()

// Render HTML output
return view("home", { users: users })
```

No other language file is required. The same developer writes the route, the query, and the template — in the same syntax.

---

## 1.7 Cross-Platform Support ✅ Decided

Barq runs on **all three major operating systems** — not just Linux.

| Platform | Support Level | Notes |
|----------|--------------|-------|
| **Linux** | Primary | Development and production target |
| **macOS** | Full | ARM (Apple Silicon) and Intel |
| **Windows** | Full | Windows 10 / 11 — `barq.exe` CLI |

### How cross-platform is achieved

**VM phase (Python reference interpreter):**  
Python already runs everywhere — the reference interpreter inherits this for free.

**Production VM (C/Rust — Phase 6):**  
The production VM is compiled to a native binary for each platform:

```
barq-linux-x64        # Linux 64-bit
barq-linux-arm64      # Linux ARM (servers, Raspberry Pi)
barq-macos-x64        # macOS Intel
barq-macos-arm64      # macOS Apple Silicon (M1/M2/M3)
barq-windows-x64.exe  # Windows 64-bit
```

A single installer handles all platforms — the user just runs `barq run app.bq`.

### Platform-transparent features

The standard library handles all OS differences transparently:

| Feature | How it's handled |
|---------|-----------------|
| **File paths** | `Barq\IO` accepts `/` on all platforms; converts to `\` on Windows internally |
| **Line endings** | Source files auto-normalized — `\r\n` and `\n` both work |
| **Environment variables** | `Barq\Env` reads `.env` and system env on all platforms |
| **CLI binary** | `barq` on Linux/macOS · `barq.exe` on Windows |
| **Path separators** | `Path.join("app", "Models", "User.bq")` — OS-safe |
| **HTTP server** | Runs identically on all platforms — uses OS networking APIs |

### Developer experience

```bash
# Same commands on all platforms
barq new my-app
barq run
barq test
barq build

# Windows (PowerShell / Command Prompt)
barq.exe new my-app
barq.exe run
```

No platform-specific code is needed in user applications. The same `.bq` source file runs on all three OS.

---

## 1.8 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ⚠️ 1 | **Final language name** | To be chosen from Urdu — will determine file extension |
| ⚠️ 2 | **VM vs native compilation** | Bytecode VM (like Python/PHP 8) vs native compile (like Go/Rust) for v1? |
| ⚠️ 3 | **Built-in HTTP server** | Use built-in server for production, or only development? |
| ⚠️ 4 | **Package registry** | Self-hosted registry vs integrated into the CLI tool? |
| ⚠️ 5 | **Cache file extension** | `.bqc` for compiled bytecode, or another extension? |
| ✅ 6 | **Platform support** | Linux · macOS · Windows — all three fully supported |

---

*Next: [Step 2 — Syntax & Variables →](step-02-syntax-variables.md)*
