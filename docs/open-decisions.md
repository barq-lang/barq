# Open Decisions — All Pending

This file tracks every decision that requires your input before implementation begins.  
Nothing here has been decided. Work through them in any order.

> Update: Mark each row ✅ with your choice as you decide.

---

## Identity

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| 1 | **Final language name** | See name candidates below | ⏳ |
| 2 | **File extension** | Derived from final name | ⏳ |
| 3 | **CLI tool name** | Matches language name | ⏳ |
| 4 | **Framework name** | Matches language name | ⏳ |
| 5 | **Stdlib namespace prefix** | Matches language name | ⏳ |

---

## Name Candidates from Urdu

Each name below is an Urdu word with its meaning, suggested file extension, CLI name, and example usage.

### Theme — Speed & Power

| Name | Urdu | Meaning | Extension | CLI | Example |
|------|------|---------|-----------|-----|---------|
| **Barq** | برق | Lightning / Electricity | `.bq` | `barq run` | `barq new my-app` |
| **Teez** | تیز | Fast / Sharp | `.tz` | `teez run` | `teez install pkg` |
| **Raftar** | رفتار | Speed / Pace | `.rft` | `raftar run` | `raftar build` |
| **Zor** | زور | Force / Power | `.zr` | `zor run` | `zor test` |

### Theme — Light & Clarity

| Name | Urdu | Meaning | Extension | CLI | Example |
|------|------|---------|-----------|-----|---------|
| **Noor** | نور | Light / Radiance | `.nr` | `noor run` | `noor new app` |
| **Roshan** | روشن | Bright / Illuminated | `.rsh` | `roshan run` | `roshan build` |
| **Kiran** | کرن | Ray of Light | `.krn` | `kiran run` | `kiran test` |
| **Mehr** | مہر | Sun / Warmth | `.mhr` | `mehr run` | `mehr new app` |

### Theme — Mind & Intelligence

| Name | Urdu | Meaning | Extension | CLI | Example |
|------|------|---------|-----------|-----|---------|
| **Zehn** | ذہن | Mind / Intellect | `.zhn` | `zehn run` | `zehn new app` |
| **Soch** | سوچ | Thought / Think | `.sch` | `soch run` | `soch build` |
| **Idraak** | ادراک | Perception / Understanding | `.idk` | `idraak run` | `idraak test` |
| **Aql** | عقل | Wisdom / Reason | `.aql` | `aql run` | `aql new app` |

### Theme — Writing & Creation

| Name | Urdu | Meaning | Extension | CLI | Example |
|------|------|---------|-----------|-----|---------|
| **Qalam** | قلم | Pen / Writer | `.qlm` | `qalam run` | `qalam new app` |
| **Likhaan** | لکھاں | Writing / Script | `.lkh` | `likhaan run` | `likhaan build` |
| **Bayan** | بیان | Expression / Statement | `.byn` | `bayan run` | `bayan new app` |
| **Takhleeq** | تخلیق | Creation / To Create | `.tkh` | `takhleeq run` | `takhleeq build` |

### Theme — Flow & Order

| Name | Urdu | Meaning | Extension | CLI | Example |
|------|------|---------|-----------|-----|---------|
| **Rawan** | رواں | Flowing / Fluent | `.rwn` | `rawan run` | `rawan new app` |
| **Nizam** | نظام | System / Order | `.nzm` | `nizam run` | `nizam build` |
| **Silsila** | سلسلہ | Chain / Sequence | `.sls` | `silsila run` | `silsila test` |
| **Tarteeeb** | ترتیب | Arrangement / Structure | `.trt` | `tarteeb run` | `tarteeb build` |

---

### How the name ripples through everything

Once you choose, every placeholder changes:

```
// If name = "Barq":
barq new my-app            // CLI tool
namespace App\Controllers  // stays the same
use Barq\Http\Request      // stdlib prefix
.bq                        // file extension
barq.pkg                   // package manifest
```

```
// If name = "Noor":
noor new my-app
use Noor\Http\Request
.nr file extension
noor.pkg
```

**Recommendation:** Short names (4-5 chars) work best — `Barq`, `Noor`, `Zehn`, `Rawan`.  
They're easy to type, memorable, and the CLI commands feel natural.

## Confirmed Decisions (Log)

| # | Topic | Decision |
|---|-------|----------|
| ✅ | **Typing** | Strict — types are mandatory, enforced at compile time, no implicit coercion |
| ✅ | **Pre-execution errors** | Compiler checks types, undefined vars, logic errors before any code runs |
| ✅ | **Security** | Fully secure by design — sanitization, parameterized queries, XSS escape, CSRF built-in |
| ✅ | **Typing** | Strict — types mandatory, enforced at compile time, no implicit coercion |
| ✅ | **Pre-execution errors** | Compiler checks types, undefined vars, logic errors before any code runs |
| ✅ | **Security** | Fully secure by design — sanitization, parameterized queries, XSS escape, CSRF built-in |
| ✅ | **Function keyword** | `karo sum(int a, int b)` (named) · `karo(...)` (anonymous) · `kr(...) =>` (lambda) |
| ✅ | **Visibility** | `#[Public]` · `#[Private]` · `#[Protected]` attributes above the function |
| ✅ | **Return type** | `#[int]` · `#[string]` · `#[void]` attribute above the function |
| ✅ | **Anonymous functions** | `karo(params): type` + indented body · lambda `kr(params): type => expr` |
| ✅ | **String interpolation** | `"Hello {name}"` — curly brace inside string, no prefix needed |
| ✅ | **Type casting** | `~type(value)` — tilde cast, unique to this language |
| ✅ | **Ternary expression** | Both — `cond ? a : b` AND `a if cond else b` |
| ✅ | **Abstract classes** | Supported — `abstract class` + `#[Abstract]` on abstract methods |
| ✅ | **Enums** | PHP-style — pure `enum Suit`, backed with `#[String]` / `#[Int]` above `enum Status`; `cases()`, `from()`, `tryFrom()`, methods, constants |
| ✅ | **Import aliasing** | Supported — `use App\Models\User as AppUser` |
| ✅ | **Grouped imports** | Supported — `use App\Models\{User, Post, Comment}` |
| ✅ | **DB config location** | Both — `.env` for credentials + `config/database.bq` for structure |
| ✅ | **Error handling** | `try/except` (Python style) |
| ✅ | **Match/Switch** | `switch/case` (PHP style) |
| ✅ | **Namespace separator** | Backslash `\` — e.g., `App\Controllers\UserController` |
| ✅ | **Collections** | Option C — `#[]` list · `#{}` dict · `#()` set · typed as `list<T>`, `dict<K,V>`, `set<T>` |
| ✅ | **Template model** | Option C — HTML-tag style: `<each>`, `<if>`, `<elseif>`, `<else>`, `{variable}` |
| ✅ | **Version numbering** | Semantic versioning — `1.0.0` (Major.Minor.Patch) |
| ✅ | **Package registry** | GitHub-based — `barq install username/package-name` |

---

## Syntax & Variables (Step 2)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| ✅ 6 | **String interpolation** | `"Hello {name}"` curly brace | Decided |
| ✅ 7 | **Collections** | Option C — `#[]`, `#{}`, `#()` | Decided |
| ✅ 10 | **Type casting** | `~type(value)` tilde cast | Decided |
| ⚠️ 8 | **Dot access on dicts** | `user.name` allowed / `user["name"]` only | ⏳ |
| ⚠️ 9 | **Null safety operator** | `?.` operator / explicit check only | ⏳ |
| ⚠️ 11 | **Indentation style** | 4 spaces / tab / both allowed | ⏳ |

---

## Functions (Step 3)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| ✅ 9 | **Function keyword** | `karo` (named + anonymous) / `kr` (lambda) | Decided |
| ✅ 12 | **Return type syntax** | `#[int]` attribute above function | Decided |
| ✅ 14 | **Anonymous function syntax** | `karo(int x): int` + block body · lambda `kr(int x): int => x * 2` | Decided |
| ⚠️ 10 | **Default parameter values** | Supported / not in v1 | ⏳ v0.12.0: supported — `string name = "World"`; constants only (literals, lists/dicts of them, `Class::CONST`, `Enum::Case`), worked out on every call |
| ⚠️ 15 | **Named arguments** | Supported `greet(name: "x")` / not in v1 | ⏳ v0.12.0: supported for functions written in Barq (and constructors); positional ones first |
| ⚠️ 16 | **Variadic parameters** | `*args` / `...args` / not in v1 | ⏳ v0.12.0: `*int numbers` (a `list<int>` inside), last parameter only; `f(*list)` spreads a list into arguments |

---

## Control Flow (Step 4)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| ✅ 18 | **Switch** | `switch/case` (PHP style) | Decided |
| ✅ 19 | **Error handling** | `try/except` (Python style) | Decided |
| ✅ 20 | **Ternary expression** | Both — `cond ? a : b` AND `a if cond else b` | Decided |
| ⚠️ 17 | **For loop with index** | `enumerate()` / `for i, v in list` | ⏳ |
| ⚠️ 21 | **Null coalescing** | `??` / `or` / not in v1 | ⏳ |
| ⚠️ 22 | **`do/while` loop** | Supported / not in v1 | ⏳ |

---

## OOP & Classes (Step 5)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| ✅ 24 | **Abstract classes** | Supported — `abstract class` + `#[Abstract]` on methods | Decided |
| ✅ | **Enums** | PHP-style — pure and `int`/`string`-backed, with methods and constants | Decided |
| ⚠️ 23 | **Static members** | Supported with `#[Static]` / not in v1 | ⏳ |
| ⚠️ 25 | **Interfaces** | Supported / not in v1 | ⏳ v0.12.0: supported — `interface X extends A, B` (method signatures + constants), `class C extends P implements X, Y`, enums can implement them |
| ⚠️ 26 | **Traits** | Supported / not in v1 | ⏳ v0.12.0: supported — `use A, B` in a class body; the class's own methods win, two traits with the same method is an error unless an `insteadof` rule picks one (v0.13.0, #96) |
| ⚠️ 27 | **Constructor shorthand** | Supported / explicit only | ⏳ v0.12.0: supported — `karo __construct(public int x, private string y = "a")` |
| ⚠️ 28 | **Static access syntax** | `Class::member` / `Class.member` | ⏳ |
| ⚠️ 29 | **Multiple inheritance** | Not allowed / via interfaces only / allowed | ⏳ v0.12.0: one parent class, any number of interfaces and traits |

---

## Namespaces & Imports (Step 6)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| ✅ 30 | **Namespace separator** | Backslash `\` | Decided |
| ✅ 31 | **Import aliasing** | `use X as Y` | Decided |
| ✅ 32 | **Grouped imports** | `use Namespace\{A, B, C}` | Decided |
| ⚠️ 33 | **Stdlib import style** | Explicit `use` / always global / configurable | ⏳ |
| ⚠️ 34 | **Project config file name** | `barq.config` / `project.bq` / `.bqconfig` | ⏳ |
| ⚠️ 35 | **Autoload config format** | Key-value in .bq / JSON / TOML | ⏳ |

---

## Standard Library (Step 7)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| 36 | **Cache backend** | In-memory only / file / Redis / configurable | ⏳ |
| 37 | **Mail provider** | SMTP only / multiple / not in v1 | ⏳ |
| 38 | **Queue backend** | In-memory / file / DB-based / not in v1 | ⏳ |

---

## Database Layer (Step 8)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| ✅ 39 | **DB config location** | Both — `.env` credentials + `config/database.bq` structure | Decided |
| ⚠️ 40 | **SQL vs NoSQL API** | Separate `table()`/`collection()` / unified | ⏳ |
| ⚠️ 41 | **Raw query return type** | Array of dicts / typed result objects | ⏳ |
| ⚠️ 42 | **Default pool size** | 10 / auto (based on CPU) / configurable | ⏳ |
| ⚠️ 43 | **Multiple DB connections** | Supported / single only in v1 | ⏳ |
| ⚠️ 44 | **Schema migration auto-detect** | Auto-migrate / explicit commands only | ⏳ v0.9.0 uses explicit commands (`barq migrate`) |

---

## Frontend / Templates (Step 9)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| 45 | **Template model** | Option A (PHP tags) / B (Blade) / C (HTML tags) | ✅ C (HTML tags) |
| 46 | **Layout inheritance syntax** | `@extends`/`@section` / function calls / not in v1 | ⏳ v0.8.0 uses `<extends>`/`<section>`/`<yield>` tags |
| 47 | **Template file extension** | `.bq` / `.html.bq` / `.bq.html` / `.html` | ⏳ v0.8.0 uses `.bq` under `views/` |
| 48 | **Unescaped output syntax** | `{!! !!}` / `{raw}` / `noEscape()` | ⏳ v0.8.0 uses `{!expr!}` (Step 9) |
| 49 | **Asset management** | Serve `public/` only / built-in pipeline | ⏳ |
| 50 | **JavaScript interop** | Separate `.js` / embedded `<script>` / ASH to JS | ⏳ |

---

## MVC Framework (Step 10)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| 51 | **Route parameter syntax** | `{id}` / `:id` / `<id>` | ⏳ |
| 52 | **Model auto-table naming** | PluralLowercase (User → users) / explicit | ⏳ v0.9.0: plural snake_case (`BlogPost` → `blog_posts`), override with `protected string table` |
| 53 | **Pagination style** | Simple / cursor-based / both | ⏳ v0.9.0: page-based — `paginate(perPage?, page?)` → `data`, `total`, `page`, `perPage`, `lastPage`, `hasMore` |
| 54 | **API versioning** | Route prefix / header-based / both | ⏳ |
| 55 | **Migration timestamp format** | `YYYY_MM_DD_` prefix / sequential numbers | ⏳ v0.9.0: `YYYY_MM_DD_HHMMSS_` prefix (from `barq make:migration`) |
| 81 | **Soft deletes opt-in** | `protected bool softDeletes = true` / a `SoftDeletes` trait (needs traits, #26) / attribute `#[SoftDeletes]` | ⏳ v0.10.0: the property |
| 82 | **Finding service providers** | Every class in `app/Providers/` / only those listed in `config/app.bq` | ⏳ v0.10.0: the folder, unless `config/app.bq` has a `providers` list |
| 83 | **Container access** | `app()` global + `this.app` / an `App` facade / both | ⏳ v0.10.0: `app()` and `this.app` (an `App` global would clash with the `App\` namespace) |
| 84 | **API routes prefix in `barq serve`** | `routes/api.bq` under `/api` / no prefix / configurable | ⏳ v0.10.0: under `/api` |
| 85 | **Fake data locale** | Mixed Pakistani + international names / English only / per-locale (`Fake.locale("ur")`) | ⏳ v0.10.0: mixed |
| 86 | **Guests in Gate checks** | Denied unless the user parameter is typed `any` / always call with `null` / nullable `?User` types (new syntax) | ⏳ v0.13.0: denied unless the user parameter is `?User` (or `any`) |
| 87 | **Where auth helpers live on the model** | On every `Model` (`user.createToken()`, `user.can()`) / opt-in traits `use HasApiTokens` (needs traits, #26) | ⏳ v0.11.0: on every `Model` |
| 88 | **Scheduled tasks file** | `routes/console.bq` / `app/Console/Kernel.bq` / any service provider only | ⏳ v0.11.0: `routes/console.bq` (providers work too) |
| 89 | **Scheduler time zone** | Server local time / UTC (like `Date`) / config `app.timezone` | ⏳ v0.11.0: local time, unless `app.timezone` or `.timezone()` is set |
| 91 | **Multi-line strings** | `"""..."""` (+ `'''...'''` literal) / backticks / none | ⏳ v0.12.0: triple quotes; shared indentation, the line break after the opening quotes and the closing line are removed |
| 92 | **Continuing a long line** | Line ending in an operator carries on / a `\` at line end / brackets only | ⏳ v0.12.0: a line ending in `+ - * / % ** == != < <= >= ?? ? => = += …`, `and`, `or`, `not`, `in` or `is` carries on (brackets already did) |
| 93 | **`User::class`** | The class itself / its full name as a string / not supported | ⏳ v0.12.0: the class itself — so `hasMany(Post::class)`, `#[UserController::class, "show"]` and `app(Service::class)` all work |
| 94 | **Nullable types** | `?T` only changes `int`/`float`/`bool` (other types already accept null) / `?T` everywhere and plain class types reject null (strict, breaks existing code) / no `?` | ⏳ v0.13.0: `?T`; for `string`, collections and classes it only documents intent |
| 95 | **Type for function values** | `function` / `callable` / with a signature `function(int): int` | ⏳ v0.13.0: `function`, no signature (any named function, lambda, bound method or built-in) |
| 96 | **Trait conflict rules** | PHP's `insteadof` / `as` in an indented block under `use` / write the method in the class only | ⏳ v0.13.0: `A::m insteadof B`, `B::m as other`, `m as protected`, `m as private other` under the `use` line; `insteadof` is not reserved |
| 97 | **CSRF protection** | Opt-in `"csrf"` middleware / on for every web route / off | ⏳ v0.14.0: opt-in middleware; the `auth:install` routes use it; `TestClient` skips it unless `withCsrfChecks()` |
| 98 | **2FA secrets at rest** | Plain text until AES exists / homemade cipher / refuse to store | ⏳ v0.14.0: plain text (no AES in Python's standard library); encrypt with `APP_KEY` once the native runtime has `Crypto.encrypt` |
| 99 | **Login with 2FA** | `TwoFactor.attempt()` returning `"login"`/`"two-factor"`/`"failed"` / make `Auth.attempt()` refuse 2FA users / a `TwoFactorRequired` exception | ⏳ v0.14.0: `TwoFactor.attempt()`; `Auth.attempt()` unchanged |
| 100 | **Generated auth routes** | `routes/auth.bq` loaded automatically / appended to `routes/web.bq` / `Route.auth()` call | ⏳ v0.14.0: `routes/auth.bq`, loaded before `web.bq` by serve, route:list and test |
| 101 | **What a signed URL signs** | Path + query / full URL with host | ⏳ v0.14.0: path + query |
| 102 | **Form helper names** | Laravel-style globals `back()`, `redirect()`, `old()`, `session()`, `csrf_field()` / methods on `Response` only | ⏳ v0.14.0: both (globals plus `Response.back()`, `res.with()`, `res.withErrors()`, `res.withInput()`) |
| 103 | **Linking a social login to an existing account** | Only with a verified email / always by email / never (separate accounts) | ⏳ v0.15.0: only when the provider says the email is verified (Google, GitHub). Facebook doesn't say, so its emails count as unverified |
| 104 | **Team owner and roles** | Owner = `teams.user_id` + roles from config / owner is a `team_members` row with role `owner` | ⏳ v0.15.0: owner is `teams.user_id`; roles `#["admin", "member"]` in config; the owner passes every `hasTeamRole()` check |
| 105 | **Who can accept a team invitation** | Logged-in user with the invited email / anyone with the link / creates the account on accept | ⏳ v0.15.0: a logged-in user whose email matches; guests log in first and come back via `intended()` |
| 106 | **Logging out other devices** | Delete their sessions + new remember token / Laravel-style password-hash check on every request | ⏳ v0.15.0: delete the sessions (all three drivers) and change `remember_token` |
| 107 | **`Http.fake()` and unmatched URLs** | Throw / send the real request | ⏳ v0.15.0: throw `HttpException` |
| 108 | **Named arguments for built-ins** | Built-ins that list parameter names / all built-ins / none | ⏳ v0.15.0: only built-ins that list names (the v0.15 auth functions); skipped ones are null |
| 109 | **Intended URL after login** | Remember GET pages the `auth` middleware turns away / any page / none | ⏳ v0.15.0: GET pages, same-site paths only; `intended()` and `URL.intended()` |
| 110 | **Where the role/permission methods live** | On every `Model` / only models with `use HasRoles` | ⏳ v0.16.0: on every model; `use HasRoles` is an empty built-in trait, accepted for readability |
| 111 | **`user.hasRole(list)`** | All of them (this repo's package page) / any of them (spatie) | ⏳ v0.16.0: all of them; `hasAnyRole()` for any |
| 112 | **Checking a permission that was never created** | `false` / throw | ⏳ v0.16.0: `false`; giving or assigning an unknown name throws `PermissionException` |
| 113 | **Team-scoped roles and permissions** | `team_id` on the assignments, global ones count everywhere / per-team roles / a "current team" setting | ⏳ v0.16.0: `team_id` on `model_has_roles` / `model_has_permissions`; no team = every team; pass `team:` explicitly |
| 114 | **Guards when checking** | Every guard unless `guard:` is given / only the model's default guard | ⏳ v0.16.0: every guard unless `guard:`; names given to `assignRole()` use the model's default guard |
| 115 | **Guests and the `role:` / `permission:` middleware** | 401 like `auth` / 403 (spatie) | ⏳ v0.16.0: 401 (web pages redirect to `login`) |
| 116 | **Permission cache store** | In-process + stamp file / the `Cache` module / none | ⏳ v0.16.0: in memory, `expiration_time` seconds, reset on every change; `.barq/permissions.stamp` tells other processes when the database is a file |
| 117 | **Upload file type** | From the file's bytes / from the sent name / from the sent Content-Type | ⏳ v0.17.0: from the bytes; text files fall back to the name; `clientExtension()` / `clientMime()` give what was sent |
| 118 | **Upload limits** | Per-file + per-body config / one limit / none | ⏳ v0.17.0: `filesystems.max_upload` 10240 KB per file (bigger = invalid file), `filesystems.max_post` 20480 KB per body (413) |
| 119 | **Files in `req.all()` and validated data** | Included (Laravel) / only via `req.file()` | ⏳ v0.17.0: included; never flashed into `old()` |
| 120 | **`store()` file names** | 40 random characters + detected extension / keep the sent name | ⏳ v0.17.0: random (`hashName()`); `storeAs()` to choose |
| 121 | **S3 addressing** | Path-style with a custom endpoint, virtual-host on AWS / always virtual-host | ⏳ v0.17.0: path-style when `endpoint` is set, else virtual-host; `path_style` overrides |
| 122 | **Formatter: aligned `=` columns** | Collapse to one space (black) / keep the author's alignment / align automatically (gofmt) | ⏳ v0.18.0: collapse to one space |
| 123 | **Formatter: quote style** (rule 4) | Leave quotes alone / switch `"..."` without `{}` to `'...'` | ⏳ v0.18.0: left alone — switching would rewrite almost every string in existing code |
| 124 | **Formatter: blank lines** | At most one, one around top-level definitions / two around top-level definitions (PEP 8) | ⏳ v0.18.0: at most one anywhere; exactly one around top-level functions, classes, enums, interfaces, traits |
| 125 | **Formatter: long lines** | Keep the author's line breaks / wrap at a column limit | ⏳ v0.18.0: keep them; no wrapping |
| 126 | **Repositories** | One repo / language + one per package | ✅ v0.18.0 (your choice): `barq`, `barq-framework`, `barq-auth`, `barq-permissions`, `vscode-barq`, `barq-website` under the `barq-lang` GitHub organization |
| 127 | **How packages are found** | Side-by-side clones + `BARQ_PATH` + Python's path / a `barq.json` per project / a registry | ⏳ v0.18.0: the first; every found package loads unless `BARQ_PACKAGES` names some (or `none`). v0.21.0 adds the project's `vendor/` (from `barq install` and `barq.json`) |
| 128 | **Package names** | `barq-auth` (repo) / `barq_auth` (Python) / `barq/auth` (the docs' old form) | ⏳ v0.18.0: `barq-auth` for repos and display, `barq_auth` in Python |
| 129 | **VS Code extension id** | `barq-lang.barq-vscode` / `barq-lang.barq` | ⏳ v0.18.0: `barq-lang.barq-vscode` (the name the extension page already used) |
| 130 | **Where the language server lives** | `barq lsp` in the reference implementation / a separate program (e.g. TypeScript, Rust) | ⏳ v0.19.0: `barq lsp`, pure Python, so it always uses the installed parser and checker |
| 131 | **Types in the editor** | From declarations only / also guess from values (`x = new User()`) | ⏳ v0.19.0: declarations only — every Barq variable has a declared type. Built-in methods have no return types yet, so chains on them stop |
| 132 | **Rename and references** | Rename everything with a type-checked index / only what is certain | ⏳ v0.19.0: rename variables and parameters only; references to methods and properties match the name after `.` / `::` in every file |
| 133 | **What `barq new` makes** | One full web app / a choice of kinds / an interactive wizard | ⏳ v0.20.0: three kinds by flag — web (default), `--api`, `--console` — plus `--auth`; no prompts, so it works in scripts. Packages add their files (barq-framework: User model, migration, factory, seeder, provider) |
| 134 | **Where installed packages go** | `vendor/<user>/<name>/` per project / one shared folder per user | ⏳ v0.21.0: `vendor/` per project, out of git; `barq.json` lists them, `barq.lock` pins exact commits |
| 135 | **Manifest formats** | Barq-literal `barq.pkg` for both / JSON for both / `barq.pkg` for packages + `barq.json` for projects | ⏳ v0.21.0: the last — `barq install` rewrites the project file, which is easy with JSON. In `barq.pkg`, `namespace` needs single quotes (`'Acme\Utils'`) |
| 136 | **Picking versions** | Newest fitting tag, no backtracking / a full solver | ⏳ v0.21.0: newest tag that fits every constraint; conflicts are reported. Pre-releases sort below their release but are not excluded; no tags → the default branch |
| 137 | **How packages are downloaded** | git / GitHub's tarball API | ⏳ v0.21.0: git (cached bare clones), so any host or a local folder works via `BARQ_REGISTRY` |
| 138 | **Names in `requires`** | `"helper-lib"` (spec example) / `"user/helper-lib"` | ⏳ v0.21.0: `user/name`, which says where to download from; the key `barq` constrains the language version |
| 139 | **`barq publish`** | Upload to a registry / tag in git | ⏳ v0.21.0: checks the package, tags `v<version>`; `--push` pushes the tag |
| 90 | **Unauthenticated web requests** | Redirect to the `login` route (else 401) / always 401 / configurable only | ⏳ v0.11.0: redirect to the `login` route or config `auth.login`, else 401; JSON requests always get 401 |

---

## Versioning & Runtime (Step 11)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| 56 | **Version numbering scheme** | SemVer / Major-only / Date-based | ⏳ |
| 57 | **Per-file version directive** | Supported / project config only / not required | ⏳ |
| 58 | **LTS release policy** | Yes (every 2 years) / No LTS / Decide later | ⏳ |
| 59 | **VM implementation language** | Python (reference) then Rust / C / Go | ⏳ |
| 60 | **Async/concurrency model** | async/await / coroutines / threads / not in v1 | ⏳ |
| 61 | **Package registry** | Self-hosted / GitHub-based / CLI-integrated | ✅ GitHub-based (#62); built in v0.21.0 as `barq install` |

---

## Function Keyword (Step 3 — ✅ Decided: `karo` + `kr`)

**Decision #64** — What keyword introduces a function definition?

**Decision: Option G — `karo` (کرو, "do it") for named functions (`karo sum(int a, int b)`) and anonymous functions (`karo(int x): int` + an indented body), and its short form `kr` for one-expression lambdas (`kr(int x): int => x * 2`).** It replaces the earlier `barq` / `br` choice. The options considered are kept below for reference.

### Options

| Option | Keyword | Full Example | Notes |
|--------|---------|-------------|-------|
| A | `def` | `def sum(int a, int b): int` | Python-inspired. Clean, familiar to Python devs. |
| B | `fn` | `fn sum(int a, int b): int` | Rust-inspired. Super short. |
| C | `fun` | `fun sum(int a, int b): int` | Kotlin/Dart style. Friendly feel. |
| D | `func` | `func sum(int a, int b): int` | Go/Swift style. 4 chars. |
| E | `bk` | `barq sum(int a, int b): int` | Barq-unique. Short for "Barq". No other language uses this. |
| F | `barq` | `barq sum(int a, int b): int` | Previous choice. Language name as keyword — unique but verbose. |
| **G ✅** | **`karo`** | `karo sum(int a, int b)` | **Chosen.** Urdu — کرو = "do it". Thematic, fully unique. Anonymous: `karo(...)`. Lambda: `kr(...)`. |

### How it looks with attributes

```bq
// Option A: def
#[Public]
#[int]
def sum(int a, int b)
    return a + b

// Option B: fn
#[Public]
#[int]
fn sum(int a, int b)
    return a + b

// Option E: barq (named) + br (lambda)
#[Public]
#[int]
barq sum(int a, int b)
    return a + b

// Option G: karo (named) + kr (lambda) — ✅ chosen
#[Public]
#[int]
karo sum(int a, int b)
    return a + b
```

### Anonymous functions

```bq
// A: def
double = def(int n): int => n * 2

// B: fn
double = fn(int n): int => n * 2

// E: br (lambda)
double = br(int n): int => n * 2

// G: karo (anonymous) + kr (lambda) — ✅ chosen
any greet = karo(string name): string
    return "Hello " + name
any double = kr(int n): int => n * 2
```

> **Note:** Whichever keyword is chosen, the language's CLI and runtime remain `barq` (`barq run`, `barq new`). The function keyword is a separate concern from the language name.

---

## Reserved Names & Helpers (Step 12)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| ✅ 64 | **Function keyword** | `def` / `fn` / `fun` / `func` / `bk` / `barq` / `karo` | `karo` (named) / `kr` (lambda) — decided |
| 65 | **Dollar-sign variables** | `$name` PHP-style / `name` Python-style (current) | ⏳ |
| 66 | **Magic method names** | `__construct` / `__init__` / `init` / attribute-based | ⏳ |
| 67 | **Constant scope** | File-level + class-level / class-level only | ⏳ |

## Facades & Helpers (Step 13)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| 68 | **Date format tokens** | PHP-style (`Y m d`) / Moment.js (`YYYY MM DD`) / own | ⏳ |
| 69 | **Cache driver default** | File / Redis / Memory | ⏳ |
| 70 | **Mail driver default** | SMTP / Log (dev) / Mailgun | ⏳ |
| 71 | **Queue driver default** | Sync / Database / Redis | ⏳ |
| 72 | **Storage disk default** | Local `storage/app/` / `public/storage/` | ⏳ v0.11.0: `local` = `storage/app/`; `public` = `storage/app/public/`, linked to `public/storage` by `barq storage:link` |

## Debug & Errors (Step 14)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| 73 | **Error page theme** | Dark / Light / Follows system | ⏳ |
| 74 | **Solutions panel** | Built-in smart suggestions / Community / Not in v1 | ⏳ |
| 75 | **Error reporting** | Log only / Optional Sentry-compatible integration | ⏳ |
| 76 | **CLI error display** | ANSI colored / Plain text | ⏳ |

## Multi-lingual (Step 15)

| # | Question | Options | Your Decision |
|---|----------|---------|---------------|
| 77 | **Language file format** | Barq dict `.bq` / JSON / YAML | ⏳ |
| 78 | **URL prefix for locales** | `/ur/users` optional / always / never | ⏳ |
| 79 | **Urdu numeral rendering** | Optional per call / locale config / always LTR | ⏳ |
| 80 | **RTL CSS handling** | Auto-generate RTL stylesheet / manual class toggling | ⏳ |

---

## First Priority Decisions

Start with these — they affect everything else:

1. **#64 — Function keyword** (`karo` for named, `kr` for lambda — decided!)
2. **#4 — Framework name** (Urdu companion to Barq)
3. **#1 — Language name** ✅ Decided: Barq (برق)
4. **#2 — File extension** ✅ Decided: `.bq`

---

*Total open decisions: 139*
*Resolved: 20+*
