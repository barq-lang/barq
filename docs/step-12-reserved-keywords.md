# Step 12 — Reserved Keywords, Variables & Class Names

> **Previous:** [Step 11 — Versioning & Roadmap](step-11-versioning-roadmap.md)
> **Next:** [Step 13 — Facades & Helper Classes](step-13-facades-helpers.md)

---

## 12.1 What Are Reserved Names?

Reserved names are identifiers that have a fixed meaning in Barq.  
You **cannot** use them as variable names, function names, class names, or namespace segments.

This is the same principle as PHP — `class`, `function`, `namespace`, `null`, `true`, `false` etc. are reserved and cannot be reused.

---

## 12.2 Reserved Keywords

These words are part of the Barq grammar. The parser treats them as tokens, not identifiers.

### Control Flow

| Keyword | Usage |
|---------|-------|
| `if` | Conditional block |
| `else` | Alternative branch |
| `elseif` | Additional condition branch |
| `for` | Range/index loop |
| `foreach` | Iterate over collection |
| `while` | Condition loop |
| `do` | Do-while loop |
| `break` | Exit loop |
| `continue` | Skip to next iteration |
| `return` | Return from function |
| `switch` | Switch expression |
| `case` | Switch case · enum case |
| `default` | Switch default |
| `match` | Pattern match expression |

### Object-Oriented

| Keyword | Usage |
|---------|-------|
| `class` | Declare a class |
| `enum` | Declare an enum — `enum Suit`, or `#[String]` on the line above for a backed enum |
| `abstract` | Mark class or method as abstract |
| `extends` | Inherit from a parent class |
| `implements` | Implement an interface |
| `interface` | Declare an interface |
| `trait` | Declare a trait (mixin) |
| `new` | Instantiate a class |
| `this` | Current object instance |
| `self` | Current class (static context) |
| `parent` | Parent class |
| `static` | Static method/property modifier |
| `const` | Declare a constant |

### Namespace & Imports

| Keyword | Usage |
|---------|-------|
| `namespace` | Declare a namespace |
| `use` | Import a class or namespace |
| `as` | Alias an import |

### Function Declaration ✅ Decided (#64)

| Keyword | Usage |
|---------|-------|
| `karo` | Named function — `karo sum(int a, int b)` (return type as `#[int]` above it) |
| `karo` | Anonymous function — `any greet = karo(string name): string` + indented body |
| `kr` | Lambda (one expression) — `any double = kr(int n): int => n * 2` |
| `karo` (type) | The type of function values — `karo double = kr(int n): int => n * 2` (v0.22.0) |

`karo` and `kr` are keywords only where they start a function (`karo name(`, `karo(`, `kr(...) =>`). Anywhere else `karo` is a type and `kr` an ordinary name: `karo applyTwice(karo kr, int value)` → `return kr(kr(value))`.

### Visibility Modifiers

| Keyword | Usage |
|---------|-------|
| `public` | Public access |
| `private` | Private access |
| `protected` | Protected access |

### Type Keywords

| Keyword | Usage |
|---------|-------|
| `int` | Integer type |
| `float` | Float type |
| `string` | String type |
| `bool` | Boolean type |
| `null` | Null value / null type |
| `void` | No return value |
| `any` | Untyped (escape hatch) |
| `true` | Boolean true |
| `false` | Boolean false |

### Logical Operators (keyword form)

| Keyword | Usage |
|---------|-------|
| `and` | Logical AND |
| `or` | Logical OR |
| `not` | Logical NOT |
| `is` | Identity / type check |
| `in` | Membership check |

### Error Handling

| Keyword | Usage |
|---------|-------|
| `try` | Try block |
| `except` | Catch block |
| `finally` | Always-execute block |
| `raise` | Throw an exception |

---

## 12.3 Reserved Variable Names

These built-in variables are always available within their scope and cannot be reassigned.

| Name | Scope | Meaning |
|------|-------|---------|
| `this` | Inside a class method | The current object instance |
| `self` | Inside a class (static) | The current class itself |
| `parent` | Inside a subclass | The parent class |
| `__file__` | Anywhere | Absolute path of the current `.bq` file |
| `__line__` | Anywhere | Current line number |
| `__class__` | Inside a class | Current class name as string |
| `__method__` | Inside a method | Current method name as string |
| `__namespace__` | Anywhere | Current namespace as string |

---

## 12.4 Reserved Global Helper Functions

These functions are **globally available** in every Barq file without any `use` import.  
You cannot define a function with these names in the global scope.

### Output & Debug

```barq
print("Hello, World!")          // print to stdout
println("Hello")                // print with newline
dump(variable)                  // pretty-print variable, continue execution
dd(variable)                    // dump and die — stops execution
exit(0)                         // exit with code
die("message")                  // print and exit
```

### View & HTTP

```barq
view("home", #{ users: users }) // render a template
response()                      // create HTTP response
response("text", 200)           // text response
redirect("/path")               // redirect response
redirect()->back()              // redirect to previous URL
request()                       // current HTTP request
abort(404)                      // throw HTTP error
abort(403, "Forbidden")
```

### Configuration & Environment

```barq
env("DB_HOST", "localhost")     // read .env variable with default
config("app.name")              // read config file value
app()                           // application instance
app("path")                     // resolve from service container
```

### Routing & URL

```barq
route("user.show", #{ id: 1 }) // generate URL for named route
url("/path")                    // full URL for path
asset("css/app.css")            // URL to public asset
```

### Auth & Session

```barq
auth()                          // Auth instance
auth().user()                   // authenticated user
auth().check()                  // is user logged in?
session()                       // Session instance
session("key")                  // get session value
session("key", "value")         // set session value
cookie("name")                  // read cookie
```

### Translation

```barq
trans("messages.welcome")                     // translate key
trans("messages.welcome", #{"name": "Ali"})   // with replacements
__("messages.welcome")                        // shorthand alias
trans_choice("messages.items", 5)             // pluralized
```

### Cryptography

```barq
bcrypt("password")              // hash password with bcrypt
encrypt("secret data")          // encrypt string
decrypt(encrypted)              // decrypt string
```

### Miscellaneous

```barq
now()                           // current DateTime object
today()                         // current Date object
collect(list)                   // wrap list in Collection helper
base_path("app/Models")         // path relative to project root
app_path("Controllers")         // path inside app/
storage_path("logs")            // path inside storage/
public_path("images")           // path inside public/
```

---

## 12.5 Reserved Standard Library Class Names

The following class names are part of Barq's standard library.  
You cannot create a class with these names in the global namespace.

### Type Utilities

| Class | Purpose |
|-------|---------|
| `Str` | String manipulation — `.upper()`, `.slug()`, `.camel()` etc. |
| `Num` | Number utilities — `.round()`, `.format()`, `.random()` |
| `Arr` | Array/list operations — `.map()`, `.filter()`, `.groupBy()` |
| `Dict` | Dictionary operations |
| `Math` | Math functions — `.sqrt()`, `.pow()`, `.sin()` |
| `Regex` | Regular expressions |

### Data & Encoding

| Class | Purpose |
|-------|---------|
| `JSON` | Encode and decode JSON |
| `XML` | Parse and generate XML |
| `CSV` | Parse and write CSV |
| `Base64` | Encode/decode Base64 |
| `Hash` | Hashing — bcrypt, SHA, MD5, HMAC |
| `Crypto` | Encryption/decryption |

### Date & Time

| Class | Purpose |
|-------|---------|
| `Date` | Date and time manipulation (Barq's Carbon equivalent) |
| `Timezone` | Timezone operations |
| `Duration` | Time intervals |

### Web & HTTP

| Class | Purpose |
|-------|---------|
| `Request` | HTTP request object |
| `Response` | HTTP response builder |
| `Route` | Router facade |
| `Http` | HTTP client (make external requests) |
| `Cookie` | Cookie management |
| `Session` | Session management |
| `View` | Template rendering |

### Database

| Class | Purpose |
|-------|---------|
| `DB` | Database query builder and connection |
| `Schema` | Table/schema manipulation |
| `Model` | Base ORM model class |
| `Migration` | Base migration class |
| `Seeder` | Base seeder class |

### Application Infrastructure

| Class | Purpose |
|-------|---------|
| `Auth` | Authentication facade |
| `Cache` | Cache facade |
| `Log` | Logging facade |
| `Config` | Configuration facade |
| `Env` | Environment variable reader |
| `Event` | Event dispatcher |
| `Queue` | Job queue |
| `Mail` | Email sending |
| `Storage` | File storage |
| `File` | Local filesystem |
| `Path` | Path utilities |

### Exceptions

| Class | Meaning |
|-------|---------|
| `Exception` | Base exception class |
| `RuntimeError` | Runtime execution error |
| `TypeError` | Type mismatch error |
| `ValueError` | Invalid value error |
| `DatabaseError` | Database operation error |
| `AuthException` | Authentication error |
| `NotFoundException` | 404 — resource not found |
| `ValidationError` | Input validation failed |
| `HttpException` | HTTP-level error |
| `PermissionError` | Unauthorized action |

---

## 12.6 Attribute Names (Reserved)

Attribute identifiers used in `#[...]` before declarations are reserved:

| Attribute | Usage |
|-----------|-------|
| `#[Public]` | Public visibility |
| `#[Private]` | Private visibility |
| `#[Protected]` | Protected visibility |
| `#[Static]` | Static member |
| `#[Abstract]` | Abstract method (no body) |
| `#[Override]` | Explicitly overrides parent method |
| `#[Final]` | Cannot be overridden |
| `#[Deprecated]` | Marked for removal |
| `#[int]` | Return type: integer |
| `#[float]` | Return type: float |
| `#[string]` | Return type: string |
| `#[bool]` | Return type: boolean |
| `#[void]` | No return value |
| `#[null]` | Nullable return |
| `#[Route(...)]` | Route attribute (framework) |
| `#[Middleware(...)]` | Apply middleware |
| `#[Validate(...)]` | Auto-validate input |

---

## 12.7 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ✅ 64 | **Function keyword** | Decided: `karo` (named) + `kr` (lambda) — Urdu کرو "do it"; `kr` is its short form for anonymous functions |
| ⚠️ 65 | **Dollar-sign variables** | `$name` like PHP / `name` like Python (current) |
| ⚠️ 66 | **`__magic__` names** | Use dunder names for special methods like Python, or `@override` attributes |
| ⚠️ 67 | **`const` scope** | File-level constants / class-level only / both |

---

*Next: [Step 13 — Facades & Helper Classes →](step-13-facades-helpers.md)*
