# Step 6 — Namespaces & Imports

> **Previous:** [Step 5 — OOP & Classes](step-05-oop-classes.md)  
> **Next:** [Step 7 — Standard Library](step-07-standard-library.md)

---

## 6.1 What are Namespaces?

Namespaces group related classes, functions, and constants under a logical path.  
They prevent naming conflicts and define the structure of a project.

Barq namespaces are **PHP-style** — declared at the top of a file, using a separator character (decision pending).

---

## 6.2 Declaring a Namespace

The `namespace` declaration must be the **first non-comment line** of the file.

```barq
namespace App\Controllers

class UserController

    #[Public]
    #[View]
    karo index()
        return view("users.index")
```

---

## 6.3 Namespace Separator ✅ Decided

**Backslash `\`** — same as PHP convention.

```barq
namespace App\Controllers
namespace App\Controllers\Api
namespace App\Models
namespace App\Services\Auth
namespace Database\Migrations
```

Directory mapping (like PSR-4):
```
namespace App\Controllers\UserController
→ file: app/Controllers/UserController.bq
```

The root mapping is defined in the project config file:

---

## 6.4 Importing with `use`

The `use` keyword brings a class (or function/constant) from another namespace into the current file.

```barq
namespace App\Controllers

use App\Models\User
use App\Services\AuthService
use App\Http\Request

class UserController

    #[Public]
    #[Response]
    karo store(Request request)
        user = new User(
            request.input("name"),
            request.input("email")
        )
        user.save()
        return response({ success: true })
```

---

## 6.5 Aliasing ✅ Decided — `use X as Y`

When two classes share the same short name, or you want a shorter alias, use `as`:

```barq
use App\Models\User   as AppUser
use Api\Models\User   as ApiUser

appUser = new AppUser()
apiUser = new ApiUser()
```

Aliases are also useful for shortening long namespace paths:
```barq
use App\Services\Payment\StripePaymentGateway as Stripe

gateway = new Stripe()
```

---

## 6.6 Grouped Imports ✅ Decided — `use Namespace\{A, B, C}`

Multiple classes from the same namespace can be imported in one statement:

```barq
use App\Models\{User, Post, Comment}

use App\Http\{Request, Response, Middleware}

use Barq\Db\{Database, QueryBuilder, Schema}
```

Grouped imports can also be aliased individually:
```barq
use App\Models\{
    User    as AppUser,
    Post    as AppPost,
    Comment as AppComment
}
```

---

## 6.7 Standard Library Imports

Standard library modules are accessed with `use` or as globals (decision pending — see [open-decisions.md](open-decisions.md) #30).

```barq
// Option A — explicit import
use Barq\Db\Database
use Barq\Http\Response
use Barq\Hash

// Option B — always available as globals
db       = Database.connect(...)
response = Response.make(...)
hash     = Hash.bcrypt(...)
```

---

## 6.8 Directory to Namespace Mapping

The namespace path maps to the file path, similar to PHP's PSR-4.

Example — file at `app/Models/User.bq`:
```barq
namespace App\Models    // maps to app/Models/User.bq
```

The root mapping is defined in the project config file (name TBD):

```barq
// Project root config (barq.config or similar)
autoload = {
    "App":  "app/",
    "Barq": "stdlib/"
}
```

---

## 6.9 Global Namespace

Code without a `namespace` declaration lives in the **global namespace**.  
Useful for quick scripts and entry-point files.

```barq
// No namespace — global scope
print("Hello, World!")
```

---

## 6.10 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ✅ 27 | **Namespace separator** | Backslash `\` |
| ✅ 28 | **Import aliasing** | Supported — `use X as Y` |
| ✅ 29 | **Grouped imports** | Supported — `use Namespace\{A, B, C}` |
| ⚠️ 30 | **Stdlib import style** | Explicit `use` / always global / configurable |
| ⚠️ 31 | **Project config file name** | `barq.config` / `project.bq` / `.bqconfig` |
| ⚠️ 32 | **Autoload config format** | Key-value in .bq / separate JSON / separate TOML |

---

*Next: [Step 7 — Standard Library →](step-07-standard-library.md)*
