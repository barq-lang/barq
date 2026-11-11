# Step 14 — Debug Tools & Error Pages

> **Previous:** [Step 13 — Facades & Helper Classes](step-13-facades-helpers.md)
> **Next:** [Step 15 — Multi-lingual Support](step-15-multilingual.md)

---

## 14.1 Philosophy

Barq's error experience is **first-class** — not an afterthought.

In development mode, every error shows a beautiful, information-rich page with:
- Clear error message and type
- Exact file + line number with syntax-highlighted context
- Full expandable stack trace
- Request details (method, URL, headers, body)
- Server environment info

In production mode, all errors show a clean, minimal error page with no technical details exposed to end users. All details go to the log.

This is Barq's equivalent of **Laravel's Ignition** — built into the core, not a plugin.

---

## 14.2 Development vs Production Error Mode

```barq
// config/app.bq
debug = Env.get("APP_DEBUG", false)   // true in dev, false in prod
env   = Env.get("APP_ENV", "production")
```

```
// .env (development)
APP_DEBUG=true
APP_ENV=development

// .env (production)
APP_DEBUG=false
APP_ENV=production
```

| Mode | Error output |
|------|-------------|
| `APP_DEBUG=true` | Full error page with code, stack trace, request info |
| `APP_DEBUG=false` | Clean "Something went wrong" page — details in log only |

---

## 14.3 `dd()` — Dump and Die

`dd()` is a **debug function** that pretty-prints one or more variables and immediately stops execution. Nothing after `dd()` runs.

```barq
string name = "Alice"
int    age  = 30

dd(name)                   // stops here, prints name
dd(name, age)              // prints both, then stops
dd(request.all())          // dump the entire request
dd(user, user.posts)       // dump multiple related objects
```

### `dd()` output features

The `dd()` output panel shows:
- Variable name (detected automatically from source)
- Type badge: `string`, `int`, `bool`, `null`, `list<User>`, etc.
- Value displayed with full syntax highlighting
- Objects show all properties, expandable/collapsible
- Lists show all items, with count
- Dicts show key → value pairs
- Nested structures are indented and expandable
- Large strings are truncated with a "show more" toggle
- Timestamps show human-readable time + relative ("2 minutes ago")

**Example output for `dd(user)`:**

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 dd()  ·  HomeController.bq  ·  line 24
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 user  App\Models\User
 ├── id:         42            int
 ├── name:       "Alice"       string
 ├── email:      "alice@..."   string
 ├── active:     true          bool
 ├── created_at: 2026-01-15    Date  (10 days ago)
 └── posts:      [3 items]     list<Post>
      ├── [0]  Post { id: 1, title: "Hello World" }
      ├── [1]  Post { id: 2, title: "My Journey" }
      └── [2]  Post { id: 7, title: "Barq is fast" }
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Execution stopped at dd()
```

---

## 14.4 `dump()` — Dump Without Stopping

`dump()` works identically to `dd()` but does **not** stop execution. The output appears inline in the page or in the debug toolbar, and the script continues.

```barq
dump(user)                          // inspect user, keep going
dump("checkpoint 1")
// ... more code runs after this
dump(result, "final result")        // inspect result too
```

Use `dump()` when you want to inspect values at multiple points in a flow without stopping execution.

---

## 14.4b Step-through debugging ✅ v0.24.0

`dd()` and `dump()` show values; the debugger stops the program and lets you look around. `barq debug app.bq` (or F5 in VS Code) runs it with breakpoints, stepping, the call stack and variables. `breakpoint()` stops on that line when a debugger is attached and does nothing otherwise:

```barq
for order in orders
    if order.total > 1000
        breakpoint()      // look at this order before it is charged
    charge(order)
```

See the [README](../README.md#debugging-barq-debug-and-barq-dap-v0240) for the commands.

---

## 14.5 The Barq Error Page (Development)

When an unhandled exception is thrown in `APP_DEBUG=true` mode, Barq renders a full-screen error page.

### Sections of the error page:

#### 1. Error Header
```
┌─────────────────────────────────────────────────────────────┐
│  NotFoundException                                          │
│  User with ID 999 not found                                 │
│                                                             │
│  thrown in  app/Controllers/UserController.bq  line 47     │
└─────────────────────────────────────────────────────────────┘
```

#### 2. Code Snippet
The exact lines around the error, syntax-highlighted, with the error line marked in red:

```
 44 │  karo show(Request request, int id)
 45 │      user = User.find(id)
 46 │
 47 ►│      if (user == null)                     ← error thrown here
 48 │          raise new NotFoundException(
 49 │              "User with ID {id} not found"
 50 │          )
```

#### 3. Stack Trace (expandable)
Each frame shows:
- File path + line number
- Class and method name
- Arguments passed (hover to see values)
- Click to expand code context at that frame

```
▼  UserController::show()         app/Controllers/UserController.bq:47
   Barq\Http\Router::dispatch()   vendor/barq/router/Router.bq:112
   Barq\Http\Kernel::handle()     vendor/barq/kernel/Kernel.bq:88
   App\Http\Kernel::handle()      app/Http/Kernel.bq:34
   public/index.bq                public/index.bq:14
```

#### 4. Request Information
```
Method:   GET
URL:      http://localhost:8000/users/999
IP:       127.0.0.1
User:     Not authenticated

Headers:
  Accept:        text/html
  User-Agent:    Mozilla/5.0 ...
  Cookie:        barq_session=...

Query Parameters:
  (none)

Request Body:
  (none)
```

#### 5. Environment Panel
```
Barq Version:    0.2.0-dev
PHP/Python:      Python 3.12.3
OS:              Linux 7.0.0-42-generic
App Name:        My App
App Env:         development
App Debug:       true
Database:        mysql (connected)
Cache:           file
Queue:           sync
```

#### 6. Solutions Panel *(smart suggestions)*
Barq analyses the error type and suggests fixes:

```
💡 Suggested Solutions:

  → User::find() returns null when no record matches.
    Use User::findOrFail(id) to automatically throw a NotFoundException.

  → Or add a null check:
    if (user == null)
        return abort(404)
```

---

## 14.6 Production Error Pages

In `APP_DEBUG=false`, the user sees a clean, branded error page. No technical information is shown.

### 404 — Not Found
```
┌────────────────────────────────────────┐
│                                        │
│          404                           │
│    Page not found                      │
│                                        │
│  The page you're looking for doesn't   │
│  exist or has been moved.              │
│                                        │
│  [← Back]   [Home]                    │
│                                        │
└────────────────────────────────────────┘
```

### 500 — Server Error
```
┌────────────────────────────────────────┐
│                                        │
│          500                           │
│    Something went wrong                │
│                                        │
│  An unexpected error occurred.         │
│  Our team has been notified.           │
│                                        │
│  [← Back]   [Home]                    │
│                                        │
└────────────────────────────────────────┘
```

### 403 — Forbidden
```
┌────────────────────────────────────────┐
│                                        │
│          403                           │
│    Access denied                       │
│                                        │
│  You don't have permission to          │
│  view this page.                       │
│                                        │
│  [Login]   [Home]                     │
│                                        │
└────────────────────────────────────────┘
```

Custom error page templates can be placed at:
```
views/
├── errors/
│   ├── 404.bq
│   ├── 403.bq
│   ├── 500.bq
│   └── layout.bq
```

---

## 14.7 Custom Exception Classes

```barq
namespace App\Exceptions

class UserNotFoundException extends NotFoundException

    #[Public]
    #[void]
    karo __construct(int userId)
        parent::__construct("User with ID {userId} not found")

class InsufficientBalanceException extends Exception

    float required
    float available

    #[Public]
    #[void]
    karo __construct(float required, float available)
        this.required  = required
        this.available = available
        parent::__construct(
            "Insufficient balance: need {required}, have {available}"
        )

// Usage
try
    if (account.balance < amount)
        raise new InsufficientBalanceException(amount, account.balance)
    account.deduct(amount)
except InsufficientBalanceException as e
    Log.warning(e.message)
    return response().json(#{"error": e.message}, 422)
```

---

## 14.8 The `abort()` Helper

```barq
// Throw HTTP exceptions anywhere
abort(404)
abort(404, "User not found")
abort(403, "You cannot edit this post")
abort(422, "Validation failed")
abort(500, "Internal server error")

// Abort with custom response headers
abort(401, "Unauthorized", #{"WWW-Authenticate": "Bearer"})

// Abort if condition is true
abort_if(user.id != post.user_id, 403, "Not your post")

// Abort unless condition is true
abort_unless(auth().check(), 401, "Login required")
```

---

## 14.9 Global Exception Handler

```barq
// app/Exceptions/Handler.bq
namespace App\Exceptions

use Barq\Exception\Handler as BaseHandler

class Handler extends BaseHandler

    // Don't report these exceptions to the log
    list<string> dontReport = #[
        "NotFoundException",
        "AuthException",
        "ValidationError"
    ]

    #[Protected]
    #[void]
    karo report(Exception e)
        // Send critical errors to Slack
        if (e is RuntimeError)
            Log.channel("slack").critical(e.message)

        parent::report(e)

    #[Protected]
    #[Response]
    karo render(Request request, Exception e)
        if (e is ValidationError)
            return response().json(#{
                "errors": e.errors()
            }, 422)

        if (e is NotFoundException)
            if (request.expectsJson())
                return response().json(#{"error": "Not found"}, 404)

        return parent::render(request, e)
```

---

## 14.10 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ⚠️ 73 | **Error page design language** | Dark theme / Light theme / Follows system |
| ⚠️ 74 | **Solutions panel** | Built-in smart suggestions / Community-contributed only / Not in v1 |
| ⚠️ 75 | **Error reporting service** | Built-in log only / Optional Sentry-compatible integration |
| ⚠️ 76 | **CLI error display** | ANSI colored terminal output / Plain text |

---

*Next: [Step 15 — Multi-lingual Support →](step-15-multilingual.md)*
