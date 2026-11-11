# Step 13 — Facades & Helper Classes

> **Previous:** [Step 12 — Reserved Keywords](step-12-reserved-keywords.md)
> **Next:** [Step 14 — Debug & Error Pages](step-14-debug-errors.md)

---

## 13.1 What Are Facades?

A **facade** is a static-style interface to a service that lives in the application container.

Facades look like static class calls (`Str::upper()`) but under the hood they resolve the real service from the container. This means they are:
- **Testable** — the underlying service can be swapped in tests
- **Clean** — no need to inject dependencies everywhere
- **Familiar** — same pattern as Laravel's facades

```barq
// Without facade — dependency injection
use Barq\Support\Str

class SlugService
    string text

    #[Public]
    #[string]
    karo slug()
        return Str.slug(this.text)

// With facade — no import needed, global access
slug = Str.slug("Hello World")    // → "hello-world"
```

---

## 13.2 Str — String Helper

The `Str` class mirrors Python's `str` methods plus Laravel's `Str` helpers.

```barq
// Case
Str.upper("hello")              → "HELLO"
Str.lower("HELLO")              → "hello"
Str.title("hello world")        → "Hello World"
Str.camel("hello_world")        → "helloWorld"
Str.snake("helloWorld")         → "hello_world"
Str.kebab("helloWorld")         → "hello-world"
Str.pascal("hello_world")       → "HelloWorld"
Str.slug("Hello World!")        → "hello-world"

// Search & Test
Str.contains("hello world", "world")     → true
Str.startsWith("hello", "he")            → true
Str.endsWith("hello.bq", ".bq")          → true
Str.match("/^\d+$/", "123")              → true
Str.matchAll("/\d+/g", "a1 b2 c3")      → ["1","2","3"]

// Transform
Str.trim("  hello  ")            → "hello"
Str.ltrim("  hello  ")           → "hello  "
Str.rtrim("  hello  ")           → "  hello"
Str.replace("hi", "hello", "hi world")  → "hello world"
Str.replaceAll("-", "_", "foo-bar-baz") → "foo_bar_baz"
Str.reverse("hello")             → "olleh"
Str.repeat("ab", 3)              → "ababab"
Str.wrap("hello", "'")           → "'hello'"

// Extract
Str.length("hello")              → 5
Str.words("hello world")         → ["hello", "world"]
Str.lines("a\nb\nc")             → ["a","b","c"]
Str.chars("hello")               → ["h","e","l","l","o"]
Str.substr("hello", 1, 3)        → "ell"
Str.before("user@example.com", "@")  → "user"
Str.after("user@example.com", "@")   → "example.com"
Str.between("<b>text</b>", "<b>", "</b>") → "text"

// Pad & Limit
Str.pad("42", 5, "0", "left")    → "00042"
Str.limit("long text here", 8)   → "long tex..."
Str.limit("long text here", 8, "…") → "long tex…"
Str.truncate("long text", 5)     → "long…"

// Generation
Str.random(16)                   → "aB3kL9mXpQ7rN2sY"  (random alphanumeric)
Str.uuid()                       → "f47ac10b-58cc-4372-a567-0e02b2c3d479"
Str.ulid()                       → "01ARZ3NDEKTSV4RRFFQ69G5FAV"

// Encoding
Str.base64Encode("hello")        → "aGVsbG8="
Str.base64Decode("aGVsbG8=")     → "hello"
Str.htmlEncode("<div>")          → "&lt;div&gt;"
Str.htmlDecode("&lt;div&gt;")    → "<div>"
Str.urlEncode("hello world")     → "hello+world"
Str.urlDecode("hello+world")     → "hello world"

// Format
Str.format("Hello, {}!", "World")          → "Hello, World!"
Str.format("Name: {name}", #{"name":"Ali"})→ "Name: Ali"
Str.pluralize("item", 3)                   → "items"
Str.singularize("items")                   → "item"
Str.ordinalize(21)                         → "21st"
Str.humanize("user_first_name")            → "User first name"
```

---

## 13.3 Hash — Cryptographic Hashing

```barq
// Password hashing (bcrypt by default)
Hash.make("my-password")                  → "$2y$10$..."
Hash.check("my-password", hashedValue)    → true
Hash.needsRehash(hashedValue)             → false
Hash.make("pass", rounds: 12)             // custom bcrypt rounds

// Message digests
Hash.md5("text")                          → 32-char hex
Hash.sha1("text")                         → 40-char hex
Hash.sha256("text")                       → 64-char hex
Hash.sha512("text")                       → 128-char hex

// HMAC (message authentication)
Hash.hmac("my-secret-key", "message")               → hex string
Hash.hmac("key", "message", algorithm: "sha256")

// Comparison (timing-safe)
Hash.equals("expected", "actual")         → true/false
```

---

## 13.4 Date — Date & Time (Carbon Equivalent)

`Date` is Barq's equivalent of PHP's Carbon library — rich date/time manipulation built into the standard library.

```barq
// Create
now     = Date.now()                     // current date + time
today   = Date.today()                   // current date (time = midnight)
date    = Date.parse("2026-01-15")
date    = Date.parse("January 15, 2026")
date    = Date.create(2026, 1, 15)
date    = Date.create(2026, 1, 15, 14, 30, 0) // with time
date    = Date.fromTimestamp(1734567890)
date    = Date.fromFormat("15/01/2026", "d/m/Y")

// Format
Date.format(now, "Y-m-d")               → "2026-01-15"
Date.format(now, "d/m/Y")               → "15/01/2026"
Date.format(now, "D, d M Y")            → "Thu, 15 Jan 2026"
Date.format(now, "h:i A")               → "02:30 PM"
Date.toISO(now)                         → "2026-01-15T14:30:00Z"
Date.toHuman(now)                       → "January 15, 2026"
Date.toRelative(now)                    → "5 minutes ago"
Date.timestamp(now)                     → 1734567890

// Add / Subtract
Date.addDays(date, 7)
Date.addMonths(date, 1)
Date.addYears(date, 2)
Date.addHours(date, 3)
Date.addMinutes(date, 30)
Date.subDays(date, 7)
Date.subMonths(date, 1)

// Comparison
Date.isBefore(date1, date2)             → true/false
Date.isAfter(date1, date2)              → true/false
Date.isEqual(date1, date2)              → true/false
Date.isToday(date)                      → true/false
Date.isPast(date)                       → true/false
Date.isFuture(date)                     → true/false
Date.isWeekend(date)                    → true/false
Date.isLeapYear(2024)                   → true

// Difference
Date.diffDays(date1, date2)             → 7
Date.diffMonths(date1, date2)           → 2
Date.diffHours(date1, date2)            → 168
Date.diffForHumans(date)                → "3 days ago" / "in 2 hours"

// Timezone
Date.setTimezone(date, "Asia/Karachi")
Date.toUTC(date)
Date.timezone()                         → "UTC" (current app timezone)

// Parts
Date.year(date)                         → 2026
Date.month(date)                        → 1
Date.day(date)                          → 15
Date.hour(date)                         → 14
Date.minute(date)                       → 30
Date.dayOfWeek(date)                    → 4 (0=Sun, 1=Mon...)
Date.dayOfYear(date)                    → 15
Date.weekOfYear(date)                   → 3
Date.daysInMonth(date)                  → 31

// Date ranges
Date.range(startDate, endDate)          → list of dates
Date.startOfMonth(date)
Date.endOfMonth(date)
Date.startOfWeek(date)
Date.endOfWeek(date)
```

**Format tokens** (PHP-compatible):

| Token | Meaning | Example |
|-------|---------|---------|
| `Y` | 4-digit year | `2026` |
| `y` | 2-digit year | `26` |
| `m` | Month (01–12) | `01` |
| `M` | Month abbr | `Jan` |
| `F` | Month full | `January` |
| `d` | Day (01–31) | `15` |
| `D` | Day abbr | `Thu` |
| `l` | Day full | `Thursday` |
| `H` | Hour 24h | `14` |
| `h` | Hour 12h | `02` |
| `i` | Minutes | `30` |
| `s` | Seconds | `00` |
| `A` | AM/PM | `PM` |
| `U` | Unix timestamp | `1734567890` |

---

## 13.5 Arr — Array & List Helper

```barq
items = #[3, 1, 4, 1, 5, 9, 2, 6]

// Transform
Arr.map(items, kr(int x): int => x * 2)                   → #[6,2,8,2,10,18,4,12]
Arr.filter(items, kr(int x): bool => x > 3)               → #[4,5,9,6]
Arr.reduce(items, kr(int acc, int x): int => acc + x, 0)  → 31
Arr.sort(items)                                          → #[1,1,2,3,4,5,6,9]
Arr.sortDesc(items)                                      → #[9,6,5,4,3,2,1,1]
Arr.sortBy(users, "name")                               // sort objects by field
Arr.reverse(items)
Arr.unique(items)                                        → #[3,1,4,5,9,2,6]
Arr.flatten(#[#[1,2],#[3,4]])                            → #[1,2,3,4]
Arr.flatMap(items, fn)
Arr.chunk(items, 3)                                      → #[#[3,1,4],#[1,5,9],#[2,6]]

// Search
Arr.contains(items, 5)                                   → true
Arr.search(items, 5)                                     → 4 (index)
Arr.first(items)                                         → 3
Arr.last(items)                                          → 6
Arr.firstWhere(users, "active", true)                    // first matching object
Arr.find(items, kr(int x): bool => x > 8)                 → 9

// Extract
Arr.pluck(users, "name")                                 → #["Alice","Bob"]
Arr.pluck(users, "address.city")                        // nested key
Arr.keys(dict)
Arr.values(dict)
Arr.column(matrix, 0)                                   // extract column from 2D list

// Combine
Arr.merge(#[1,2], #[3,4])                               → #[1,2,3,4]
Arr.zip(#[1,2], #["a","b"])                             → #[#[1,"a"],#[2,"b"]]
Arr.combine(keys, values)                               // → dict from two lists

// Set operations
Arr.diff(#[1,2,3], #[2,3,4])                           → #[1]
Arr.intersect(#[1,2,3], #[2,3,4])                      → #[2,3]
Arr.union(#[1,2], #[2,3])                              → #[1,2,3]

// Group & Count
Arr.groupBy(users, "role")                              // → dict of lists by role
Arr.countBy(users, "role")                              // → dict of counts
Arr.sum(#[1,2,3,4])                                    → 10
Arr.avg(#[1,2,3,4])                                    → 2.5
Arr.min(#[1,2,3,4])                                    → 1
Arr.max(#[1,2,3,4])                                    → 4

// Pad & Slice
Arr.take(items, 3)                                      → #[3,1,4]
Arr.skip(items, 3)                                      → #[1,5,9,2,6]
Arr.slice(items, 2, 4)                                  → #[4,1]
Arr.pad(#[1,2], 5, 0)                                  → #[1,2,0,0,0]
Arr.fill(5, 0)                                          → #[0,0,0,0,0]
Arr.range(1, 5)                                         → #[1,2,3,4,5]
```

---

## 13.6 Num — Number Utilities

```barq
// Rounding
Num.floor(3.7)                  → 3
Num.ceil(3.2)                   → 4
Num.round(3.456, 2)             → 3.46
Num.truncate(3.9)               → 3

// Math
Num.abs(-5)                     → 5
Num.sqrt(16)                    → 4.0
Num.pow(2, 10)                  → 1024
Num.log(100, 10)                → 2.0
Num.sin(angle), Num.cos(angle), Num.tan(angle)
Num.pi()                        → 3.14159...
Num.e()                         → 2.71828...

// Bounds
Num.min(3, 7, 1)                → 1
Num.max(3, 7, 1)                → 7
Num.clamp(value, min, max)      // keep value within range
Num.between(5, 1, 10)          → true

// Random
Num.random()                    → 0.0–1.0
Num.random(1, 100)              → int between 1 and 100
Num.randomFloat(0.0, 1.0)

// Format
Num.format(1234567.89)                        → "1,234,567.89"
Num.format(1234567.89, 2, ",", ".")           → "1.234.567,89"
Num.currency(1234.5, "USD")                   → "$1,234.50"
Num.percent(0.75)                             → "75%"
Num.bytes(1073741824)                         → "1 GB"
Num.ordinal(21)                               → "21st"

// Type checks
Num.isInt(3.0)                  → false
Num.isFloat(3.14)               → true
Num.isPositive(5)               → true
Num.isNegative(-3)              → true
Num.isZero(0)                   → true
Num.isEven(4)                   → true
Num.isOdd(3)                    → true
```

---

## 13.7 Cache Facade

```barq
// Basic get/set
Cache.get("user_count")                            → value or null
Cache.get("user_count", 0)                         → value or default 0
Cache.set("user_count", 1500, ttl: 3600)           // TTL in seconds
Cache.has("user_count")                            → true/false
Cache.missing("user_count")                        → true/false
Cache.forget("user_count")

// Persistent (no expiry)
Cache.forever("config_hash", hashValue)
Cache.forgetForever("config_hash")

// Atomic operations
Cache.increment("visits")
Cache.increment("visits", 5)
Cache.decrement("remaining_slots")

// Remember pattern (get or store)
users = Cache.remember("active_users", 3600, karo(): list<User>
    return User.where("active", true).get()
)

// Remember forever
settings = Cache.rememberForever("app_settings", karo(): dict<string, any>
    return Config.all()
)

// Flush all
Cache.flush()

// Tags (group related cache keys)
Cache.tags(#["users", "admin"]).set("list", users, 3600)
Cache.tags(#["users"]).flush()

// Multiple values at once
Cache.many(#["key1", "key2", "key3"])              → dict
Cache.setMany(#{"key1": val1, "key2": val2}, 3600)
```

---

## 13.8 Log Facade

```barq
// Log levels (PSR-3)
Log.emergency("System is down!")
Log.alert("Database unreachable")
Log.critical("Payment gateway failed")
Log.error("User not found: {id}", #{"id": userId})
Log.warning("Deprecated method used")
Log.notice("New user registered")
Log.info("Request completed in 42ms")
Log.debug("Query: SELECT * FROM users")

// With context
Log.info("User logged in", #{"user_id": user.id, "ip": request.ip()})

// Named channels
Log.channel("slack").error("Deploy failed!")
Log.channel("database").info("Audit log entry")

// Log to multiple channels
Log.stack(#["daily", "slack"]).critical("Server overload")
```

---

## 13.9 Auth Facade

> **Status (v0.11.0-alpha):** built, with session (`web`) and token (`api`) guards, remember-me, API tokens and the `auth` / `guest` / `can:` middleware. `Auth.login(user, true)` is the "remember me" form (named arguments don't exist yet). Authorization is `Gate` / policies / `user.can()`. See [the barq-auth README](https://github.com/barq-lang/barq-auth#authentication-v0110).

```barq
// Check status
Auth.check()                          → true/false
Auth.guest()                          → true if NOT logged in
Auth.id()                             → authenticated user ID or null
Auth.user()                           → User object or null

// Login / Logout
Auth.login(user)                      // log in a User object
Auth.login(user, remember: true)      // with "remember me"
Auth.loginById(42)
Auth.logout()

// Attempt login with credentials
Auth.attempt(#{
    "email":    "alice@example.com",
    "password": "secret"
})                                    → true/false

// Guards (for multi-auth: web, api, admin)
Auth.guard("api").user()
Auth.guard("admin").check()
Auth.guard("api").attempt(credentials)

// Once (login for one request only, no session)
Auth.once(credentials)
Auth.onceUsingId(42)
```

---

## 13.10 Mail Facade

```barq
// Basic send
Mail.to("alice@example.com")
    .send(new WelcomeMail(user))

// Multiple recipients
Mail.to(#["alice@example.com", "bob@example.com"])
    .send(new NewsletterMail(edition))

// CC and BCC
Mail.to(user.email)
    .cc("manager@company.com")
    .bcc("audit@company.com")
    .send(new InvoiceMail(invoice))

// Queue for async sending
Mail.to(user.email).queue(new WelcomeMail(user))

// Send later
Mail.to(user.email).later(300, new ReminderMail(user))

// Raw text email
Mail.raw("Your order #123 has shipped.", karo(Message message): void
    message.to("alice@example.com")
    message.subject("Order Shipped")
)
```

---

## 13.11 Event Facade

> **Status (v0.10.0-alpha):** built, with `Event.fake()` and assertions for tests. Pass a listener class as `SendWelcomeEmail` (no `::class`). See [the barq-framework README](https://github.com/barq-lang/barq-framework#events).

```barq
// Fire an event
Event.fire("user.registered", user)
Event.fire("order.placed", #{"order": order, "user": user})

// Listen to an event
Event.listen("user.registered", karo(User user): void
    Mail.to(user.email).send(new WelcomeMail(user))
)

// Named listener class
Event.listen("user.registered", SendWelcomeEmail::class)

// Wildcard listener
Event.listen("user.*", karo(string event, any data): void
    Log.info("User event: {event}")
)

// One-time listener (fires once then removes itself)
Event.once("payment.complete", karo(Payment payment): void
    Receipt.generate(payment)
)
```

---

## 13.12 Queue Facade

```barq
// Push a job to the default queue
Queue.push(new SendEmailJob(user))

// Push to a specific queue
Queue.on("emails").push(new SendEmailJob(user))

// Delay execution
Queue.later(300, new SendReminderJob(user))         // 300 seconds
Queue.later(Date.addMinutes(now, 10), new Job())    // DateTime

// Job class example
class SendEmailJob

    User user

    #[Public]
    #[void]
    karo __construct(User user)
        this.user = user

    #[Public]
    #[void]
    karo handle()
        Mail.to(this.user.email).send(new WelcomeMail(this.user))
```

---

## 13.13 Storage Facade

> **Status (v0.17.0-alpha):** built, with `local`, `public` and `s3` disks (AWS, MinIO, R2...), `Storage.fake()` for tests and `barq storage:link`. File uploads: `req.file("avatar").store("avatars")`, `Storage.putFile()`, `temporaryUrl()` for S3. See [README.md](../README.md#storage-v0110).

```barq
// Local disk (default)
Storage.put("reports/jan.pdf", fileContent)
Storage.get("reports/jan.pdf")
Storage.exists("reports/jan.pdf")
Storage.delete("reports/jan.pdf")
Storage.url("reports/jan.pdf")          // public URL
Storage.path("reports/jan.pdf")         // absolute local path
Storage.size("reports/jan.pdf")         // bytes
Storage.mime("reports/jan.pdf")         // "application/pdf"
Storage.files("reports/")               // list files in directory
Storage.directories("reports/")         // list subdirectories
Storage.makeDirectory("archive/2026")
Storage.deleteDirectory("temp/")

// Named disks (configured in config/filesystems.bq)
Storage.disk("s3").put("uploads/image.jpg", content)
Storage.disk("s3").url("uploads/image.jpg")
Storage.disk("local").get("config.bq")
Storage.disk("public").put("avatars/user42.jpg", imageData)
```

---

## 13.14 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ⚠️ 68 | **Date format tokens** | PHP-compatible (Y m d) / Moment.js style (YYYY MM DD) / own format |
| ⚠️ 69 | **Cache driver default** | File / Redis / Memory (array for testing) |
| ⚠️ 70 | **Mail driver default** | SMTP / Sendmail / Mailgun / Log (dev mode) |
| ⚠️ 71 | **Queue driver default** | Sync (dev) / Database / Redis |
| ⚠️ 72 | **Storage disk default** | Local `storage/app/` / `public/storage/` |

---

*Next: [Step 14 — Debug & Error Pages →](step-14-debug-errors.md)*
