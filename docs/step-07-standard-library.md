# Step 7 — Built-in Standard Library

> **Previous:** [Step 6 — Namespaces](step-06-namespaces.md)  
> **Next:** [Step 8 — Database Layer](step-08-database-layer.md)

---

## 7.1 Principle — No External Packages Required

The Barq standard library is **complete by design**.  
Every common task a web application needs is covered by a built-in module.  
No third-party packages are required for core functionality.

This means:
- No installing `bcrypt`, `dotenv`, `axios`, or similar packages
- Faster startup (no dependency loading)
- Predictable behavior — the stdlib is versioned with the language
- One documentation source

---

## 7.2 Module List

| Module | Purpose |
|--------|---------|
| `Barq\IO` | Console, file read/write, streams |
| `Barq\Http` | HTTP server + client |
| `Barq\Db` | Database connectors and query builder |
| `Barq\Hash` | Cryptographic hashing |
| `Barq\Crypto` | Encryption, signing, JWT |
| `Barq\Json` | JSON encode/decode |
| `Barq\Xml` | XML parse/generate |
| `Barq\Csv` | CSV read/write |
| `Barq\Date` | Date and time operations |
| `Barq\Math` | Mathematical functions |
| `Barq\Str` | String utilities |
| `Barq\Arr` | Array/list utilities |
| `Barq\Env` | Environment variables |
| `Barq\Log` | Logging |
| `Barq\Cache` | In-memory and file cache |
| `Barq\Session` | HTTP session management |
| `Barq\Cookie` | HTTP cookie management |
| `Barq\Mail` | Sending email (SMTP) |
| `Barq\Queue` | Background job queues |
| `Barq\Test` | Unit testing framework |
| `Barq\Cli` | CLI argument parsing |
| `Barq\Path` | File path utilities |
| `Barq\Regex` | Regular expressions |
| `Barq\Uuid` | UUID generation |
| `Barq\Validation` | Input validation rules |
| `Barq\Config` | App configuration access |

---

## 7.3 Module Reference

### Barq\IO
```barq
use Barq\IO

IO.print("Hello, World!")          // console output
IO.println("With newline")
content = IO.readFile("data.txt")  // read file
IO.writeFile("out.txt", content)   // write file
IO.appendFile("log.txt", "line")   // append to file
exists = IO.exists("file.txt")     // boolean
```

---

### Barq\Http
```barq
use Barq\Http\Client
use Barq\Http\Response

// HTTP client (outbound requests)
res = Client.get("https://api.example.com/users")
print(res.status)   // 200
print(res.body)     // raw response body
data = res.json()   // parsed JSON

// POST with body
res = Client.post("https://api.example.com/users", {
    name:  "Alice",
    email: "alice@example.com"
})

// Response (server side)
return Response.make("Hello World", 200)
return Response.json({ status: "ok" })
return Response.redirect("/login")
```

---

### Barq\Hash
```barq
use Barq\Hash

hashed  = Hash.bcrypt("mypassword")       // bcrypt hash
valid   = Hash.verify("mypassword", hashed)  // boolean
sha256  = Hash.sha256("data")
md5     = Hash.md5("data")
hmac    = Hash.hmac("secret", "message")
```

---

### Barq\Crypto
```barq
use Barq\Crypto

// Symmetric encryption
encrypted = Crypto.encrypt("secret text", "key")
decrypted = Crypto.decrypt(encrypted, "key")

// JWT
token   = Crypto.jwtSign({ userId: 1 }, "secret")
payload = Crypto.jwtVerify(token, "secret")

// Generate secure random
token = Crypto.randomBytes(32)
pin   = Crypto.randomInt(1000, 9999)
```

---

### Barq\Json
```barq
use Barq\Json

string json = Json.encode({ name: "Alice", age: 25 })
data        = Json.decode(json)
pretty      = Json.pretty({ name: "Alice" })   // formatted output
```

---

### Barq\Date
```barq
use Barq\Date

now       = Date.now()                     // current timestamp
formatted = Date.format(now, "Y-m-d")      // "2026-11-05"
tomorrow  = Date.add(now, 1, "day")
diff      = Date.diff(date1, date2, "days")
parsed    = Date.parse("2026-01-15")
unix      = Date.toUnix(now)
```

---

### Barq\Math
```barq
use Barq\Math

Math.abs(-5)          // 5
Math.round(3.7)       // 4
Math.floor(3.9)       // 3
Math.ceil(3.1)        // 4
Math.sqrt(16)         // 4.0
Math.pow(2, 10)       // 1024
Math.min(1, 2, 3)     // 1
Math.max(1, 2, 3)     // 3
Math.random()         // float between 0 and 1
Math.PI               // 3.14159...
```

---

### Barq\Str
```barq
use Barq\Str

Str.upper("hello")          // "HELLO"
Str.lower("HELLO")          // "hello"
Str.trim("  hello  ")       // "hello"
Str.split("a,b,c", ",")     // ["a","b","c"]
Str.join(["a","b"], ",")     // "a,b"
Str.replace("hello", "l", "r")  // "herro"
Str.contains("hello", "ell")    // true
Str.startsWith("hello", "he")   // true
Str.endsWith("hello", "lo")     // true
Str.length("hello")         // 5
Str.substr("hello", 1, 3)   // "ell"
Str.slug("Hello World!")    // "hello-world"
Str.pad("5", 3, "0", "left") // "005"
```

---

### Barq\Arr
```barq
use Barq\Arr

list = [3, 1, 4, 1, 5]

Arr.sort(list)              // [1, 1, 3, 4, 5]
Arr.reverse(list)           // [5, 1, 4, 1, 3]
Arr.unique(list)            // [3, 1, 4, 5]
Arr.contains(list, 4)       // true
Arr.count(list)             // 5
Arr.first(list)             // 3
Arr.last(list)              // 5
Arr.push(list, 9)           // adds 9 to end
Arr.pop(list)               // removes and returns last
Arr.map(list, fn)           // apply fn to each element
Arr.filter(list, fn)        // keep elements where fn returns true
Arr.reduce(list, fn, 0)     // fold list to single value
Arr.merge(list1, list2)     // combine two lists
Arr.flatten([[1,2],[3]])     // [1, 2, 3]
Arr.slice(list, 1, 3)       // [1, 4]
Arr.keys(dict)              // list of dict keys
Arr.values(dict)            // list of dict values
```

---

### Barq\Validation
```barq
use Barq\Validation

v = Validation.make(request.all(), {
    name:  "required|string|min:2|max:50",
    email: "required|email",
    age:   "required|int|min:18"
})

if (v.fails())
    return response({ errors: v.errors() }, 422)
```

---

### Barq\Log
```barq
use Barq\Log

Log.info("User logged in")
Log.warning("Slow query detected")
Log.error("Database connection failed")
Log.debug("Request data: " + Json.encode(data))
```

Log levels: `debug`, `info`, `warning`, `error`, `critical`

---

### Barq\Cache
```barq
use Barq\Cache

Cache.set("key", value, 3600)     // store for 1 hour
value = Cache.get("key")          // retrieve
exists = Cache.has("key")         // boolean
Cache.forget("key")               // delete
Cache.forever("key", value)       // no expiry
Cache.remember("key", 3600, karo(): list<dict<string, any>>
    return db.table("config").get()
)
```

---

### Barq\Env
```barq
use Barq\Env

Env.get("DB_HOST")              // read env variable
Env.get("DB_HOST", "localhost") // with default
Env.set("DEBUG", "true")
Env.has("DB_HOST")              // boolean
```

---

### Barq\Uuid
```barq
use Barq\Uuid

id = Uuid.v4()   // "550e8400-e29b-41d4-a716-446655440000"
```

---

### Barq\Regex
```barq
use Barq\Regex

matched = Regex.match("/^\d+$/", "12345")      // true
groups  = Regex.capture("/(\w+)@(\w+)/", email)
result  = Regex.replace("/\s+/", " ", text)
```

---

## 7.4 Global Built-ins (Always Available)

These functions are always available without any `use` import:

| Function | Description |
|----------|-------------|
| `print(value)` | Print to console/output |
| `dump(value)` | Debug print with type info |
| `die(message)` | Stop execution with message |
| `type(value)` | Returns type name as string |
| `range(n)` | Generate number sequence |
| `len(value)` | Length of string or list |
| `int(x)` | Cast to integer |
| `float(x)` | Cast to float |
| `string(x)` | Cast to string |
| `bool(x)` | Cast to boolean |
| `isset(var)` | Check if variable is set and not null |
| `empty(var)` | Check if null, 0, "", [], false |
| `clone(obj)` | Deep copy an object |

---

## 7.5 Magic Helper Library

Barq ships a large set of **global helper functions** — always available, no `use` needed.  
They follow consistent prefix naming that makes intent obvious at a glance.

---

### Type Checks — `is_*`

```barq
is_null(v)          // true if null
is_str(v)           // true if string
is_int(v)           // true if integer (not bool)
is_float(v)         // true if float
is_num(v)           // true if int or float
is_bool(v)          // true if boolean
is_list(v)          // true if list
is_dict(v)          // true if dict
is_fn(v)            // true if function/lambda
is_obj(v)           // true if class instance
is_set(v)           // true if set
is_numeric(v)       // true if number OR numeric string ("123")
is_empty(v)         // true if null | false | 0 | "" | [] | {}
not_null(v)         // true if not null
```

---

### Number Helpers

```barq
is_odd(7)              // true
is_even(8)             // true
between(5, 1, 10)      // true  — lo <= v <= hi
clamp(15, 1, 10)       // 10   — constrain to [lo, hi]
sign(-3)               // -1   — returns -1, 0, or 1
```

---

### Collection — List Helpers — `list_*`

```barq
in_list(value, list)                 // true if value is in list
list_contains(list, value)           // same as in_list, fluent form
list_search(list, value)             // first index or null
list_count(list)                     // length
list_first(list)                     // first element or null
list_last(list)                      // last element or null
list_sum(#[1,2,3])                   // 6
list_reverse(list)                   // new reversed list
list_sort(list)                      // new sorted list
list_sort(list, kr(any x): any => x) // sort by key function
list_unique(list)                    // remove duplicates
list_flatten(list)                   // deep flatten
list_chunk(list, 2)                  // split into chunks of size n
list_zip(listA, listB)               // [[a0,b0], [a1,b1], ...]
list_slice(list, 1, 3)               // sub-list [start, end)
list_merge(listA, listB)             // concatenate two lists
list_map(list, fn)                   // transform each element
list_filter(list, fn)                // keep where fn returns true
list_reduce(list, fn, initial)       // fold to single value
list_each(list, fn)                  // iterate, return null
list_any(list, fn)                   // true if any element matches
list_all(list, fn)                   // true if all elements match
list_find(list, fn)                  // first matching element or null
```

---

### Collection — Dict Helpers — `dict_*`

```barq
has_key(dict, key)              // true if key exists
dict_has(dict, key)             // same as has_key, fluent form
dict_get(dict, key, default)    // value or default
dict_keys(dict)                 // list of keys
dict_values(dict)               // list of values
dict_merge(dictA, dictB)        // merge (B wins on conflicts)
dict_pick(dict, #["a","b"])     // new dict with only listed keys
dict_omit(dict, #["x"])         // new dict without listed keys
```

---

### String Helpers — `str_*`

```barq
str_len("hello")                     // 5
str_upper("hello")                   // "HELLO"
str_lower("HELLO")                   // "hello"
str_trim("  hi  ")                   // "hi"
str_ltrim("  hi  ")                  // "hi  "
str_rtrim("  hi  ")                  // "  hi"
str_trim("--hi--", "-")              // "hi"      (custom chars)
str_replace("hello", "l", "r")       // "herro"
str_replace(#["a","b"], #["x","y"], text) // list-of-pairs replacement
str_contains("hello", "ell")         // true
str_starts("hello", "he")            // true
str_ends("hello", "lo")              // true
str_split("a,b,c", ",")              // ["a","b","c"]
str_join(#["a","b","c"], "-")        // "a-b-c"
str_pad("5", 4, "0", "left")         // "0005"   sides: left|right|both
str_repeat("ab", 3)                  // "ababab"
str_slug("Hello World!")             // "hello-world"
str_reverse("abcd")                  // "dcba"
str_substr("hello world", 6, 5)      // "world"
str_index("hello", "ll")             // 2   (or null if not found)
str_last_index("abcabc", "b")        // 4   (or null)
str_count("banana", "a")             // 3
str_words("the quick fox")           // ["the","quick","fox"]
str_word_count("one two three")      // 3
str_capitalize("hello world")        // "Hello world"
str_title("hello world")             // "Hello World"
str_format("Hi %s, age %s", "Ali", 30)  // "Hi Ali, age 30"
str_is_empty("")                     // true
str_to_list("abc")                   // ["a","b","c"]
str_lines("line1\nline2")            // ["line1","line2"]
str_before("2024-01-15", "-")        // "2024"
str_after("2024-01-15", "-")         // "01-15"
str_between("a[x]b", "[", "]")       // "x"
```

---

### Regex Helpers — `rx_*`

Patterns can be bare strings (`"[0-9]+"`) or slash-delimited with flags (`"/pattern/i"`).

Supported flags: `i` (case-insensitive) · `m` (multiline) · `s` (dot-all)

```barq
rx_match("/[0-9]+/", "abc 123")        // true  — match anywhere
rx_test("/^[0-9]+$/", "12345")         // true  — full match
rx_find("/([a-z]+)@([a-z]+)/", text)   // ["user","example"] or null
rx_find_all("/[0-9]+/", "1 and 2")     // ["1","2"]
rx_replace("/[ ]+/", "_", "a   b")     // "a_b"
rx_split("/,[ ]*/", "a, b, c")         // ["a","b","c"]
rx_count("/[0-9]+/", "1 two 3 four")   // 2
```

---

### Object / Class Reflection

```barq
has_class("User")           // true if class is defined
has_method(obj, "save")     // true if method exists
has_prop(obj, "name")       // true if property exists
class_of(obj)               // "User" (or built-in type name)
instance_of(obj, "User")    // true if obj is User or subclass
prop_names(obj)             // list of public property names
method_names(obj)           // list of all method names
to_dict(obj)                // {prop: value, ...} of public props
```

---

### Value Helpers

```barq
coalesce(null, null, "found")    // "found" — first non-null
val(null, "default")             // "default"
val("actual", "default")         // "actual"
clone(obj)                       // deep copy (list / dict / object)
to_str(42)                       // "42"
to_int("99")                     // 99
to_float("3.14")                 // 3.14
to_bool("true")                  // true
to_list("abc")                   // ["a","b","c"]
```

---

## 7.6 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ⚠️ 33 | **Stdlib namespace prefix** | `Barq\` / language final name prefix / no prefix |
| ⚠️ 34 | **Global built-ins scope** | Listed above / expanded / reduced |
| ⚠️ 35 | **Cache backend** | In-memory only / file / Redis / configurable |
| ⚠️ 36 | **Mail provider** | SMTP only / multiple providers / not in v1 |
| ⚠️ 37 | **Queue backend** | In-memory / file / DB-based / not in v1 |

---

*Next: [Step 8 — Database Layer →](step-08-database-layer.md)*
