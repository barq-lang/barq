# Step 3 — Functions

> **Previous:** [Step 2 — Syntax & Variables](step-02-syntax-variables.md)  
> **Next:** [Step 4 — Control Flow](step-04-control-flow.md)

---

## 3.1 Function Declaration ✅ Decided

Functions use the `karo` keyword (Urdu کرو — "do it"). There are three forms:

| Form | Syntax | Example |
|------|--------|---------|
| Named function | `karo name(params)` | `karo sum(int a, int b)` |
| Anonymous function | `karo(params): type` + indented body | `any greet = karo(string name): string` |
| Lambda | `kr(params): type => expr` | `any double = kr(int n): int => n * 2` |

For named functions, visibility and return type are declared as **attributes** on the lines above, using `#[...]` syntax.

```barq
#[Public]
#[int]
karo sum(int a, int b) return a + b

#[Public]
#[int]
karo sum(int a, int b)
    return a + b
```

Both **single-line** and **multi-line** bodies are supported.

---

## 3.2 Visibility Attribute

The first attribute sets the visibility of the function.

| Attribute | Meaning |
|-----------|---------|
| `#[Public]` | Accessible from anywhere |
| `#[Private]` | Same class or file only |
| `#[Protected]` | Same class and subclasses |
| *(none)* | Module-private — not exported |

```barq
#[Public]
#[string]
karo greet(string name)
    return "Hello, " + name


#[Private]
#[string]
karo hashPassword(string raw)
    return Hash.bcrypt(raw)


#[Protected]
#[bool]
karo validate(dict<string, string> data)
    return data != null
```

A function with no visibility attribute is accessible only within the current file (module-private):

```barq
#[int]
karo helper(int x)
    return x + 1
```

---

## 3.3 Return Type Attribute ✅ Decided

The return type is declared as a `#[type]` attribute. All types are valid — primitives, class names, or `void`.

| Attribute | Meaning |
|-----------|---------|
| `#[int]` | Returns integer |
| `#[float]` | Returns float |
| `#[string]` | Returns string |
| `#[bool]` | Returns boolean |
| `#[void]` | No return value |
| `#[User]` | Returns a `User` object |
| `#[list<int>]` | Returns a list of ints |

```barq
#[Public]
#[void]
karo log(string message)
    Log.info(message)


#[Public]
#[User]
karo getUser(int id)
    return User.find(id)


#[Public]
#[bool]
karo register(string name, string email)
    // returns true or false
```

**Compile-time error if return type is wrong:**
```barq
#[Public]
#[int]
karo add(int a, int b)
    return "oops"   // ERROR: expected int, got string
```

---

## 3.4 `#[...]` — Attribute vs List Literal

`#[...]` is used both for **list literals** (`#["Alice", "Bob"]`) and **function attributes** (`#[Public]`).  
The parser resolves this by **context**:

| Context | Meaning |
|---------|---------|
| Standalone line before a `karo` declaration | Attribute |
| Inside an expression (assignment, argument, etc.) | List literal |

```barq
// ATTRIBUTE — standalone line before karo declaration
#[Public]
#[int]
karo sum(int a, int b) return a + b

// LIST LITERAL — inside an expression
list<string> names = #["Alice", "Bob"]
result = items.filter(kr(string x): bool => x != "")
```

This is the same disambiguation approach used in Rust — no ambiguity exists in a well-formed program.

---

## 3.5 Typed Parameters

Parameter types are **required** — strict typing is enforced at compile time.

```barq
#[Public]
#[string]
karo greet(string name)
    return "Hello, " + name


#[Public]
#[int]
karo add(int a, int b)
    return a + b


#[Public]
#[void]
karo register(string name, string email, int age)
    // body
```

---

## 3.6 Default Parameter Values

🟡 **Implemented in v0.12.0 (provisional)** — see [open-decisions.md](open-decisions.md) #10.

```barq
#[Public]
#[string]
karo greet(string name = "World")
    return "Hello, " + name

// Calling:
greet()           // Hello, World
greet("Alice")    // Hello, Alice
```

Rules:
- A default is a **constant**: a literal, `-1`, a list/dict/set of literals, `Class::CONSTANT` or `Enum::Case`. Anything else is a compile error (use `null` and set the value in the body).
- Defaults are worked out on **every call**, so `list<int> seen = #[]` gives each call its own empty list.
- Once a parameter has a default, every parameter after it needs one too (a variadic may still come last).
- Lambdas and anonymous functions take defaults too: `kr(int n, int by = 2): int => n * by`.

---

## 3.7 Anonymous Functions & Lambdas ✅ Decided

A function without a name is a value: store it in a variable or pass it as an argument. There are two forms.

### Anonymous function — `karo(...)`
`karo` without a name, a return type after `:`, and an indented body on the next lines:

```barq
// Assigned to a variable
any process = karo(string name): string
    string upper = Str.upper(name)
    return "Hello, " + upper

// Passed directly as argument
list<int> result = items.map(karo(int x): int
    int doubled = x * 2
    return doubled
)

// Returns nothing
db.transaction(karo(): void
    db.table("accounts").where("id", 1).decrement("balance", 100)
    db.table("accounts").where("id", 2).increment("balance", 100)
)
```

### Lambda — `kr(...)`
`kr` is the short form for a function that is a single expression. The value after `=>` is returned:

```barq
any double = kr(int n): int => n * 2
any triple = kr(int n): int => n * 3

// Passed as argument
list<int> doubled = items.map(kr(int x): int => x * 2)
list<string> names = items.filter(kr(string x): bool => Str.len(x) > 0)
```

### Stored and reused
```barq
any validate = karo(string email): bool
    if (Str.len(email) == 0) return false
    return email.contains("@")

bool ok = validate("user@example.com")   // true
```

### Summary
| Form | Syntax | Best for |
|------|--------|----------|
| Named function | `#[Vis]` / `#[type]` / `karo name(params)` | Reusable, exported functions |
| Anonymous function | `karo(params): type` + indented body | Multi-line logic passed around as a value |
| Lambda | `kr(params): type => expr` | Single-expression callbacks |

Using the wrong form is a compile error with a hint: a block body after `kr(...)` asks for `karo(...)`, and `=>` after `karo(...)` asks for `kr(...)`.

---

## 3.8 Calling Functions

```barq
// Local function
result = add(5, 10)

// Method on object
user.getName()

// Chained
db.table("users").where("active", 1).get()

// With named arguments (v0.12.0, provisional — see #15)
greet(name: "Alice")
greet("Alice", greeting: "Salaam")   // positional first, then named
new User(name: "Ali", age: 30)       // constructors too
```

Named arguments work with any function, method, lambda or constructor written in Barq; built-in functions take positional arguments only. Naming a parameter that doesn't exist, giving one twice, or leaving out one without a default are errors.

---

## 3.9 Variadic Functions

🟡 **Implemented in v0.12.0 (provisional)** — Python-style `*args`, see [open-decisions.md](open-decisions.md) #16.

```barq
#[Public]
#[int]
karo sum(*int numbers)       // numbers is a list<int>
    int total = 0
    foreach n in numbers
        total += n
    return total

sum(1, 2, 3, 4)   // 10
sum()             // 0 — an empty list

list<int> more = #[5, 6]
sum(*more, 7)     // 18 — *list spreads a list into the arguments
```

The variadic parameter must be the last one and can't have a default or be passed by name. Every value it collects is type-checked (`argument 3 of sum() expects int, got string`).

---

## 3.10 Recursion

```barq
#[Public]
#[int]
karo factorial(int n)
    if (n <= 1) return 1
    return n * factorial(n - 1)
```

---

## 3.11 Function as Value

Functions are first-class values and can be passed as arguments. Their type is `function` (🟡 v0.13.0, provisional — [open-decisions.md](open-decisions.md) #95):

```barq
#[Public]
#[int]
karo applyTwice(function fn, int value)
    return fn(fn(value))

function double = kr(int n): int => n * 2
int result = applyTwice(double, 3)   // 12
```

A `function` value can be a named function, an anonymous function or lambda, a bound method (`obj.method`) or a built-in. The type doesn't say which parameters or return type the function has. The function checks those itself when it is called.

---

## 3.12 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ✅ | **Function keyword** | `karo sum(...)` (named) · `karo(...)` (anonymous) · `kr(...) =>` (lambda) |
| ✅ | **Visibility** | `#[Public]` · `#[Private]` · `#[Protected]` attributes |
| ✅ | **Return type** | `#[int]` · `#[string]` · `#[void]` etc. attribute |
| 🟡 10 | **Default parameter values** | Supported / Not in v1 — *v0.12.0: supported (constants)* |
| 🟡 12 | **Named arguments** | Supported (`greet(name: "x")`) / not in v1 — *v0.12.0: supported* |
| 🟡 13 | **Variadic parameters** | `*args` Python-style / `...args` JS-style / not in v1 — *v0.12.0: `*int args`* |

---

*Next: [Step 4 — Control Flow →](step-04-control-flow.md)*
