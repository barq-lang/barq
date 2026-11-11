# Step 2 — Syntax Rules & Variables

> **Previous:** [Step 1 — Overview](step-01-overview.md)  
> **Next:** [Step 3 — Functions](step-03-functions.md)

---

## 2.1 Syntax Philosophy

Barq follows **Python-style indentation** as the block delimiter.

- No curly braces `{}` to open/close blocks
- No semicolons `;` at end of lines
- 4 spaces or 1 tab per indentation level (consistent within a file)
- Comments use `//` for single-line and `/* */` for multi-line

```barq
// Single-line comment

/*
   Multi-line comment
*/
```

---

## 2.2 Variable Declaration — Strict Typing

**Types are mandatory in Barq.** Every variable must have an explicit type declaration.  
The compiler checks types **before execution** — no runtime surprises.

```barq
string  name   = "Alice"
int     age    = 25
float   score  = 98.5
bool    active = true
```

**Compile-time errors (caught before running):**
```barq
name = "Alice"           // ERROR: missing type declaration
string age = 25          // ERROR: cannot assign int to string
int x = "hello"          // ERROR: type mismatch — expected int, got string
```

**No implicit type coercion:**
```barq
int    a = 5
float  b = 3.14
// a + b   // ERROR: cannot add int and float without explicit cast
float  c = float(a) + b   // OK: explicit cast required
```

### Constants:
```barq
const MAX_RETRIES = 3          // type inferred from value — cannot reassign
const APP_VERSION = "1.0.0"
const PI = 3.14159
```

Constants cannot be reassigned after declaration. Attempting to do so is a compile-time error.

---

## 2.3 Built-in Types

| Keyword | Description | Example |
|---------|-------------|---------|
| `int` | Whole number (64-bit) | `int x = 10` |
| `float` | Decimal number (64-bit) | `float x = 3.14` |
| `string` | Text (UTF-8) | `string s = "hello"` |
| `bool` | Boolean | `bool b = true` |
| `null` | Null / absence of value | `x = null` |
| `const` | Immutable value | `const X = 10` |

---

## 2.4 Visibility Modifiers

Visibility modifiers control access. They are valid:
- Inside a **class** — control member access
- At **module (file) level** — control what is exported/accessible from outside the file

| Modifier | Inside class | At module level |
|----------|-------------|-----------------|
| `public` | Accessible from anywhere | Exported symbol |
| `private` | Only within same class | Not exported |
| `protected` | Same class and subclasses | Not exported, accessible to child modules |

```barq
// At module level
public  VERSION = "1.0"      // other files can read this
private SECRET  = "key123"   // only this file can read this

// Inside a class
class User
    public  string name
    private string password
    protected string role
```

---

## 2.5 String Interpolation ✅ Decided — `"{variable}"` curly brace style

Variables are embedded inside strings using `{name}` curly braces directly in the string literal.  
No prefix required. All string literals support interpolation.

```barq
string name  = "Alice"
int    age   = 25
string city  = "Karachi"

string greeting = "Hello, {name}!"
string info     = "Name: {name}, Age: {age}"
string message  = "Welcome to {city}, {name}!"
```

**Expressions inside `{}`:**
```barq
string result = "Total: {price * qty}"
string label  = "Status: {user.active ? 'active' : 'inactive'}"
string path   = "User: {user.name.upper()}"
```

**No interpolation — use single quotes:**
```barq
string literal = 'Hello, {name}!'   // NOT interpolated — printed as-is
string normal  = "Hello, World!"    // interpolation but no {}, just a plain string
```

**Multi-line strings** (🟡 v0.12.0, provisional — [open-decisions.md](open-decisions.md) #91): `"""…"""` interpolates, `'''…'''` is literal. The line break after the opening quotes, a last line holding only the closing quotes, and the indentation all lines share are removed:
```barq
string card = """
    <div class="card">
        <h2>{title}</h2>
    </div>
    """
// card is "<div class=\"card\">\n    <h2>…</h2>\n</div>"
```

**Long lines** (🟡 v0.12.0, #92): a line that ends with an operator (`+`, `-`, `*`, `/`, `==`, `<=`, `??`, `?`, `=`, `+=`, `and`, `or`, `not`, `in`, `is`…) carries on to the next line. Inside `( )`, `#[ ]` and `#{ }` line breaks are always free.
```barq
int total = price +
    shipping +
    tax
```

---

## 2.6 Collections ✅ Decided — Option C

Barq uses a `#` prefix for all collection literals. This makes collections visually distinct from plain expressions and is unique to Barq.

### List — `#[...]`
Ordered, allows duplicates.
```barq
list<string> names = #["Alice", "Bob", "Carol"]
list<int>    ids   = #[1, 2, 3, 4, 5]
```

### Dict — `#{...}`
Key-value pairs. Key and value types must match the declaration.
```barq
dict<string, int>  scores = #{"Alice": 95, "Bob": 87, "Carol": 91}
dict<string, string> meta = #{"host": "localhost", "port": "3306"}
```

### Set — `#(...)`
Unique values only. Duplicate entries are a compile-time warning.
```barq
set<string> tags  = #("php", "python", "barq")
set<int>    primes = #(2, 3, 5, 7, 11)
```

### Accessing Collections
```barq
// List — index access
string first = names[0]       // "Alice"

// Dict — key access
int score = scores["Alice"]   // 95
int score = scores.Alice      // dot access (decision pending)

// Set — membership check
bool has = tags.has("php")    // true
```

### Typed Nested Collections
```barq
list<list<int>>         matrix  = #[#[1,2], #[3,4]]
dict<string, list<int>> grouped = #{"evens": #[2,4,6], "odds": #[1,3,5]}
```

### Empty Collections
```barq
list<string> empty  = #[]
dict<string, int> empty = #{}
set<string> empty   = #()
```

---

## 2.7 Null Handling

⚠️ **Decision pending** — see [open-decisions.md](open-decisions.md) #6.

```barq
// Null assignment
user = null

// Null check
if (user == null)
    return "not found"
```

**Nullable types** (🟡 v0.13.0, provisional — [open-decisions.md](open-decisions.md) #94). `int`, `float` and `bool` cannot hold null; put `?` in front of the type when null is allowed:

```barq
?int age = null
?int count                  // no value: null
list<?int> scores = #[90, null, 75]

#[?User]
karo find(int id)
    ...

int years = age ?? 0
```

`string`, collections and class types already accept null, so `?string` / `?User` mean the same as `string` / `User`, but they tell the reader null is expected.

Proposed null-safe access operator (decision pending — option A or B):

**Option A — `?.` operator (like modern PHP/Kotlin):**
```barq
name = user?.profile?.name
```

**Option B — explicit null check only:**
```barq
if (user != null and user.profile != null)
    name = user.profile.name
```

---

## 2.8 Type Casting ✅ Decided — `~type(value)` tilde cast

Barq uses the **`~`** tilde operator for type casting — a unique syntax not borrowed from other languages.

```barq
int    result = ~int("25")          // string → int
string text   = ~string(100)        // int → string
float  num    = ~float("3.14")      // string → float
bool   flag   = ~bool(1)            // int → bool
string label  = ~string(3.99)       // float → string
```

**Casting objects / class instances:**
```barq
User user = ~User(rawDict)          // dict → User object
Admin admin = ~Admin(user)          // User → Admin (upcast)
```

**Casting inside expressions:**
```barq
int total = ~int(price) * qty
string display = "Score: " + ~string(score)
```

**Failed cast raises a `CastException` at runtime:**
```barq
try
    int x = ~int("not-a-number")
except CastException as e
    Log.error("Cast failed: {e.message}")
```

---

## 2.9 Operators

### Arithmetic
| Operator | Meaning |
|----------|---------|
| `+` | Add |
| `-` | Subtract |
| `*` | Multiply |
| `/` | Divide |
| `%` | Modulo |
| `**` | Power (exponentiation) |

### Comparison
| Operator | Meaning |
|----------|---------|
| `==` | Equal |
| `!=` | Not equal |
| `>` | Greater than |
| `<` | Less than |
| `>=` | Greater or equal |
| `<=` | Less or equal |

### Logical
| Operator | Meaning |
|----------|---------|
| `and` | Logical AND |
| `or` | Logical OR |
| `not` | Logical NOT |

### Assignment
| Operator | Meaning |
|----------|---------|
| `=` | Assign |
| `+=` | Add and assign |
| `-=` | Subtract and assign |
| `*=` | Multiply and assign |
| `/=` | Divide and assign |

---

## 2.10 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ✅ 3 | **String interpolation** | `"Hello {name}"` — curly brace inside string |
| ✅ 4 | **Collection syntax** | Option C — `#[]` list · `#{}` dict · `#()` set |
| ✅ 7 | **Type casting** | `~type(value)` — tilde cast, unique to this language |
| ⚠️ 5 | **Dot access on dicts** | `user.name` allowed? Or only `user["name"]`? |
| ⚠️ 6 | **Null safety operator** | `?.` operator or explicit checks only |
| ⚠️ 8 | **Indentation** | 4 spaces only / tab only / both allowed |

---

*Next: [Step 3 — Functions →](step-03-functions.md)*
