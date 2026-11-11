# Step 4 — Control Flow

> **Previous:** [Step 3 — Functions](step-03-functions.md)  
> **Next:** [Step 5 — OOP & Classes](step-05-oop-classes.md)

---

## 4.1 If / Else

Conditions can use optional parentheses. Both single-line and block forms are valid.

### Single-line (guard clause):
```barq
if (a) return 'good'
if (a) return 'good'   // parentheses optional — both valid
if  a  return 'good'
```

### Block form:
```barq
if (a)
    return 'good'

if (score >= 90)
    grade = "A"
else if (score >= 80)
    grade = "B"
else if (score >= 70)
    grade = "C"
else
    grade = "F"
```

### Nested:
```barq
if (user != null)
    if (user.active)
        return "Active user"
    else
        return "Inactive user"
else
    return "No user"
```

---

## 4.2 For Loop

Python-style `for ... in` syntax.

### Numeric range:
```barq
for i in range(10)
    print(i)             // 0 1 2 3 ... 9

for i in range(1, 11)
    print(i)             // 1 2 3 ... 10

for i in range(0, 100, 5)
    print(i)             // 0 5 10 ... 95
```

`range(start, stop, step)` — `start` and `step` are optional (default 0 and 1).

### Iterating a list:
```barq
names = ["Alice", "Bob", "Carol"]

for name in names
    print(name)
```

### With index:
⚠️ **Decision pending** — see [open-decisions.md](open-decisions.md) #14.

```barq
// Option A — enumerate style (Python):
for i, name in enumerate(names)
    print(i, name)

// Option B — index built into for:
for i, name in names
    print(i, name)
```

---

## 4.3 Foreach Loop

`foreach` is designed for iterating collections (arrays, query results, dicts).  
It is preferred over `for` when you don't need a numeric counter.

### Simple:
```barq
foreach item in items
    print(item)
```

### With key-value (dict/map):
```barq
user = { name: "Alice", age: 25 }

foreach key, value in user
    print(key + ": " + value)
```

### Database results:
```barq
users = db.table("users").get()

foreach user in users
    print(user.name)
```

---

## 4.4 While Loop

```barq
while condition
    doSomething()
```

### Example — retry logic:
```barq
int retries = 3

while retries > 0
    result = tryConnect()
    if (result) break
    retries -= 1
```

### Infinite loop:
```barq
while true
    request = server.accept()
    handle(request)
```

---

## 4.5 Loop Control

| Keyword | Effect |
|---------|--------|
| `break` | Exit the loop immediately |
| `continue` | Skip to next iteration |

```barq
for i in range(10)
    if (i == 5) break       // stop at 5
    if (i % 2 == 0) continue  // skip even numbers
    print(i)                 // prints 1 3
```

---

## 4.6 Switch / Case ✅ Decided

PHP-style `switch/case`. Indentation replaces braces.

```barq
switch (status)
    case "active":
        return "User is active"
    case "banned":
        return "User is banned"
    case "pending":
        return "Awaiting approval"
    default:
        return "Unknown status"

// With fallthrough (explicit)
switch (role)
    case "superadmin":
    case "admin":
        return "Has admin access"
    case "user":
        return "Standard access"
    default:
        return "No access"

// Switch on type
switch (type(value))
    case "int":
        return "Integer"
    case "string":
        return "String"
    default:
        return "Other"
```

---

## 4.7 Error Handling ✅ Decided

Python-style `try/except`. Multiple exception types and `finally` are supported.

```barq
try
    string result = db.query("SELECT ...")
except DatabaseException as e
    Log.error(e.message)
    return null
except ConnectionException as e
    Log.critical("DB offline: " + e.message)
    return null
finally
    db.close()

// Catching any exception
try
    int result = riskyOperation()
except Exception as e
    Log.error(e.message)

// Re-throwing
try
    processPayment(data)
except PaymentException as e
    Log.error(e.message)
    raise e   // re-throw
```

**Custom exceptions:**
```barq
namespace App\Exceptions

class NotFoundException extends Exception

    #[Public]
    #[void]
    karo __construct(string message)
        parent::__construct(message, 404)
```

**Raising exceptions:**
```barq
#[Public]
#[User]
karo findOrFail(int id)
    User user = User.find(id)
    if (user == null)
        raise new NotFoundException("User " + string(id) + " not found")
    return user
```

---

## 4.8 Ternary / Inline Expression ✅ Decided — Both forms supported

**Form A — `? :` operator:**
```barq
string label = active ? "Active" : "Inactive"
int    fee   = premium ? 0 : 10
string grade = score >= 90 ? "A" : score >= 80 ? "B" : "C"
```

**Form B — Inline `if/else`:**
```barq
string label = "Active" if active else "Inactive"
int    fee   = 0 if premium else 10
```

Both forms are equivalent. Use whichever reads more naturally in context.

---

## 4.9 Null Coalescing

⚠️ **Decision pending** — see [open-decisions.md](open-decisions.md) #18.

**Option A — `??` operator (PHP/JS style):**
```barq
name = user.name ?? "Guest"
```

**Option B — `or` keyword (Python-ish):**
```barq
name = user.name or "Guest"
```

---

## 4.10 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ✅ 15 | **Match/Switch** | `switch/case` (PHP style) |
| ✅ 16 | **Error handling** | `try/except` (Python style) |
| ✅ 17 | **Ternary expression** | Both — `cond ? a : b` AND `a if cond else b` |
| ⚠️ 14 | **For loop with index** | `enumerate()` / built-in `for i, v in list` |
| ⚠️ 18 | **Null coalescing** | `??` / `or` / not in v1 |
| ⚠️ 19 | **`do/while` loop** | Supported / not in v1 |

---

*Next: [Step 5 — OOP & Classes →](step-05-oop-classes.md)*
