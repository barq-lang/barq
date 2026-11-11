# Step 10 — MVC Framework

> **Previous:** [Step 9 — Frontend Support](step-09-frontend-support.md)  
> **Next:** [Step 11 — Versioning & Roadmap](step-11-versioning-roadmap.md)

---

> **Where it lives (v0.18.0):** [barq-framework](https://github.com/barq-lang/barq-framework) — the router, middleware and templates are part of the language itself.
>
> **Status (v0.10.0-alpha reference interpreter):** Models (10.5) with relations, scopes, casts, eager loading, model events/observers and soft deletes; `FormRequest` (10.7); Resources (10.8); service providers and the container (10.9); migrations, seeders and `barq migrate*` / `barq db:seed` / `barq make:*` (10.10–10.11); factories and `Fake` (10.12); `barq serve` and `barq route:list` are built. See [the barq-framework README](https://github.com/barq-lang/barq-framework#framework-part-2-events-container-factories-serving-v0100). The examples in 10.9 and 10.12 are in working syntax.
>
> The examples below are the original draft. In working Barq code: lists and dicts are `#[...]` / `#{"key": ...}`, a class is passed as a value (`this.hasMany(Post)`, `#[UserController, "show"]` — no `::class`), method calls use `.` (`Route.get(...)`), properties use real types (`protected list<string> fillable`), and errors are caught with `except Exception as e`.

## 10.1 Framework Overview

The official Barq framework is an **MVC framework** built on top of the core language.  
It is designed to be:

- As **approachable** as Laravel (PHP)
- As **powerful** as Django (Python)
- Using Barq's own built-in tools — no external dependencies

> ⚠️ **Framework name is a pending decision** — see [open-decisions.md](open-decisions.md) #50.

---

## 10.2 Project Directory Structure

```
myapp/
│
├── app/
│   ├── Controllers/            // HTTP controllers
│   ├── Models/                 // Database models (ORM)
│   ├── Middleware/             // Request middleware
│   ├── Providers/              // Service providers (bootstrap)
│   ├── Requests/               // Form request / input validation
│   └── Resources/              // API response transformers
│
├── config/
│   ├── app.bq                 // App name, timezone, locale
│   ├── database.bq            // DB connections
│   ├── cache.bq               // Cache driver config
│   └── mail.bq                // Mail config
│
├── database/
│   ├── migrations/             // Schema migrations
│   ├── seeders/                // Test data seeders
│   └── factories/              // Model factories (for testing)
│
├── routes/
│   ├── web.bq                 // Browser routes
│   └── api.bq                 // API routes (JSON responses)
│
├── views/
│   ├── layout.bq              // Base HTML layout
│   └── ...                     // View files per feature
│
├── public/
│   ├── css/
│   ├── js/
│   └── images/
│
├── storage/
│   ├── logs/
│   ├── cache/
│   └── uploads/
│
├── tests/
│   ├── unit/
│   └── feature/
│
├── .env                        // Environment variables
└── main.bq                    // Application entry point
```

---

## 10.3 Routing

### Web routes:
```barq
// routes/web.bq
use App\Controllers\HomeController
use App\Controllers\UserController

Route::get("/",             [HomeController::class, "index"])
Route::get("/users",        [UserController::class, "index"])
Route::get("/users/{id}",   [UserController::class, "show"])
Route::post("/users",       [UserController::class, "store"])
Route::put("/users/{id}",   [UserController::class, "update"])
Route::delete("/users/{id}",[UserController::class, "destroy"])
```

### Named routes:
```barq
// routes/web.bq
use App\Controllers\ProfileController
use App\Controllers\Api\ProfileController as ApiProfileController

// ->name() assigns a route name used in redirect()->route() and route()
Route::get("/profile", [ProfileController::class, "show"])->name("profile.show")
Route::get("/profile", [Api\ProfileController::class, "show"])->name("api.show")
Route::post("/profile", [ProfileController::class, "update"])->name("profile.update")
```

### Chaining route modifiers:
```barq
// Chain ->name(), ->middleware(), ->where() in any order
Route::get("/dashboard", [DashboardController::class, "index"])
    ->middleware("auth")
    ->name("dashboard")

Route::get("/users/{id}", [UserController::class, "show"])
    ->where("id", "[0-9]+")
    ->name("users.show")
    ->middleware("auth")

// Use a named route
return redirect()->route("profile.show")
string url = route("users.show", { id: 42 })
```

### Available chain methods:

| Method | Example | Description |
|--------|---------|-------------|
| `->name(string)` | `->name("profile.show")` | Assign a route name |
| `->middleware(string\|list)` | `->middleware("auth")` | Attach middleware |
| `->where(param, pattern)` | `->where("id", "[0-9]+")` | Constrain a route parameter |
| `->prefix(string)` | `->prefix("api/v1")` | Prepend a URL prefix |

### API routes:
```barq
// routes/api.bq
use App\Controllers\Api\UserController
use App\Controllers\Api\ProfileController

// Resource routes — generates all 7 CRUD routes automatically
Route::resource("users", UserController::class)

// Named API route with namespaced controller and chained name
Route::get("/profile", [Api\ProfileController::class, "show"])->name("api.profile.show")
Route::put("/profile", [Api\ProfileController::class, "update"])->name("api.profile.update")

// Route group with prefix and middleware
Route::group({ prefix: "v1", middleware: ["auth"] }, karo(): void
    Route::get("profile",  [UserController::class, "profile"])->name("v1.profile")
    Route::get("settings", [UserController::class, "settings"])->name("v1.settings")
)
```

### Generated resource routes:

| Method | URL | Controller method |
|--------|-----|-------------------|
| GET | `/users` | `index()` |
| GET | `/users/{id}` | `show(id)` |
| POST | `/users` | `store()` |
| PUT | `/users/{id}` | `update(id)` |
| DELETE | `/users/{id}` | `destroy(id)` |

---

## 10.4 Controllers

```barq
// app/Controllers/UserController.bq
namespace App\Controllers

use App\Models\User
use App\Requests\UserStoreRequest

class UserController

    #[Public]
    #[View]
    karo index()
        users = User.all()
        return view("users.index", { users: users })

    #[Public]
    #[View]
    karo show(int id)
        user = User.findOrFail(id)
        return view("users.show", { user: user })

    #[Public]
    #[Response]
    karo store(UserStoreRequest request)
        user = User.create(request.validated())
        return redirect("/users/" + user.id)

    #[Public]
    #[Response]
    karo update(int id, UserStoreRequest request)
        user = User.findOrFail(id)
        user.update(request.validated())
        return redirect("/users/" + id)

    #[Public]
    #[Response]
    karo destroy(int id)
        User.findOrFail(id).delete()
        return redirect("/users")
```

---

## 10.5 Models (ORM)

```barq
// app/Models/User.bq
namespace App\Models

class User extends Model

    // Table name (auto-inferred as "users" if not set)
    protected string table = "users"

    // Columns that can be mass-assigned
    protected array fillable = ["name", "email", "password"]

    // Columns hidden from JSON output
    protected array hidden = ["password"]

    // Relationships
    #[Public]
    #[HasMany]
    karo posts()
        return this.hasMany(Post::class)

    #[Public]
    #[HasOne]
    karo profile()
        return this.hasOne(Profile::class)
```

### ORM queries:
```barq
// All records
users = User.all()

// Find by ID
user = User.find(1)
user = User.findOrFail(1)      // throws 404 if not found

// Where conditions
users = User.where("active", 1).get()
users = User.where("age", ">", 18).orderBy("name").get()

// Create
user = User.create({ name: "Alice", email: "alice@example.com" })

// Update
user = User.find(1)
user.name = "Alice B."
user.save()

// Or update inline
User.where("id", 1).update({ name: "Alice B." })

// Delete
user = User.find(1)
user.delete()

// Pagination
users = User.paginate(15)   // 15 per page
```

---

## 10.6 Middleware

Middleware runs **before** (and optionally **after**) the controller handles a request.

```barq
// app/Middleware/AuthMiddleware.bq
namespace App\Middleware

class AuthMiddleware

    #[Public]
    #[Response]
    karo handle(Request request, any next)
        if (!request.session().has("user_id"))
            return redirect("/login")
        return next(request)


// app/Middleware/RateLimitMiddleware.bq
namespace App\Middleware

class RateLimitMiddleware

    #[Public]
    #[Response]
    karo handle(Request request, any next)
        key = "rate:" + request.ip()
        count = Cache.increment(key)
        Cache.expire(key, 60)

        if (count > 100)
            return response().json({ error: "Too many requests" }, 429)

        return next(request)
```

### Registering middleware:
```barq
// routes/api.bq
Route::group({ middleware: ["auth", "rateLimit"] }, karo(): void
    Route::get("users", [UserController::class, "index"])
)
```

---

## 10.7 Requests (Input Validation)

```barq
// app/Requests/UserStoreRequest.bq
namespace App\Requests

class UserStoreRequest extends FormRequest

    #[Public]
    #[dict<string, string>]
    karo rules()
        return {
            name:     "required|string|min:2|max:50",
            email:    "required|email|unique:users",
            password: "required|string|min:8",
            age:      "required|int|min:18"
        }

    #[Public]
    #[dict<string, string>]
    karo messages()
        return {
            "name.required":     "Name is required.",
            "email.unique":      "This email is already registered.",
            "password.min":      "Password must be at least 8 characters."
        }
```

---

## 10.8 Resources (API Response Transformers)

Resources control what data is returned in API responses:

```barq
// app/Resources/UserResource.bq
namespace App\Resources

class UserResource extends Resource

    #[Public]
    #[dict<string, any>]
    karo toArray()
        return {
            id:    this.id,
            name:  this.name,
            email: this.email,
            joined: Date.format(this.created_at, "Y-m-d")
        }


// In controller:
#[Public]
#[UserResource]
karo show(int id)
    user = User.findOrFail(id)
    return new UserResource(user)

// Collection:
#[Public]
#[Response]
karo index()
    users = User.all()
    return UserResource.collection(users)
```

---

## 10.9 Service Providers

Providers bootstrap components when the app starts:

```barq
// app/Providers/AppServiceProvider.bq
namespace App\Providers

use App\Models\User
use App\Observers\UserObserver
use App\Services\MailerService

class AppServiceProvider extends ServiceProvider

    #[Public]
    #[void]
    karo register()
        // Bind names and classes to implementations
        this.app.singleton("Mailer", MailerService)

    #[Public]
    #[void]
    karo boot()
        // Runs after every provider's register()
        User.observe(UserObserver)
        Event.listen("user.registered", SendWelcomeEmail)
```

Every class in `app/Providers/` is a provider, unless `config/app.bq` lists them under `"providers"`. `barq run`, `barq test`, `barq serve` and the database commands start them before anything else. Reach the container with `this.app` in a provider and `app()` / `app(Name)` everywhere else. `make(SomeClass)` builds a class and makes each constructor parameter by its type. Controllers, middleware, listeners and observers are made this way, and so are route handler parameters of class types.

### Model events and soft deletes

```barq
User.creating(karo(User u): void
    u.slug = str_slug(u.name)
)
User.deleting(kr(User u): bool => not u.isAdmin())     // false cancels

class Post extends Model
    protected bool softDeletes = true                  // + table.softDeletes() in the migration

post.delete()                 // sets deleted_at
Post.withTrashed().get()      // include deleted rows; Post.onlyTrashed() for only them
post.restore()                // and post.forceDelete() to really delete
```

### Authentication, authorization and scheduled tasks (v0.11.0)

```barq
// routes/web.bq
Route.post("/login", karo(Request req): any
    if Auth.attempt(#{"email": req.input("email"), "password": req.input("password")})
        return Response.redirect("/dashboard")
    return Response.redirect("/login")
).middleware("guest")
Route.get("/dashboard", kr(Request req): string => "Hi " + req.user().name).middleware("auth")
Route.put("/posts/:post", #[PostController, "update"]).middleware(#["auth", "can:update,post"])
Route.get("/api/me", kr(): any => Auth.user()).middleware("auth:api")    // Bearer token

// app/Policies/PostPolicy.bq — found automatically for App\Models\Post
class PostPolicy
    #[Public]
    #[bool]
    karo update(User user, Post post)
        return user.id == post.user_id

// routes/console.bq — run by `barq schedule:run` (one cron line: * * * * *)
Schedule.call(karo(): void
    Storage.deleteDirectory("tmp")
).dailyAt("03:00")
```

`barq auth:install` adds `config/auth.bq` and the API-tokens migration. `barq make:policy PostPolicy --model Post` writes a policy. The rest of the [barq-auth package](https://github.com/barq-lang/barq-auth/blob/main/docs/barq-auth.md) (registration screens, email verification, OAuth, 2FA, teams) is not built yet.

---

## 10.10 Migrations

```barq
// database/migrations/2026_11_05_create_users_table.bq
namespace Database\Migrations

class CreateUsersTable extends Migration

    #[Public]
    #[void]
    karo up(Schema schema)
        schema.create("users", karo(Blueprint table): void
            table.id()
            table.string("name")
            table.string("email").unique()
            table.string("password")
            table.int("age").nullable()
            table.bool("active").default(true)
            table.timestamps()
        )

    #[Public]
    #[void]
    karo down(Schema schema)
        schema.dropIfExists("users")
```

### CLI commands (name TBD):
```bash
barq migrate           # run all pending migrations
barq migrate:rollback  # roll back last batch
barq migrate:fresh     # drop all tables and re-run
barq migrate:status    # show migration status
```

---

## 10.11 Seeders

```barq
// database/seeders/UserSeeder.bq
namespace Database\Seeders

use App\Models\User

class UserSeeder extends Seeder

    #[Public]
    #[void]
    karo run()
        User.create({
            name:     "Admin",
            email:    "admin@example.com",
            password: Hash.bcrypt("password"),
            active:   true
        })
```

---

## 10.12 Factories

Factories generate fake data for testing:

```barq
// database/factories/UserFactory.bq
namespace Database\Factories

class UserFactory extends Factory

    // optional: UserFactory makes User by its name
    protected string model = 'App\Models\User'

    #[Public]
    #[dict<string, any>]
    karo definition()
        return #{
            "name":     Fake.name(),
            "email":    Fake.unique().email(),
            "password": Hash.password("password"),
            "age":      Fake.int(18, 60)
        }
```

```barq
User.factory().create()
User.factory().count(10).state(#{"active": false}).create()
User.factory().has(Post.factory().count(3)).create()
```

---

## 10.13 REST API Example

A complete CRUD API for a posts resource:

```barq
// routes/api.bq
Route::prefix("v1", karo(): void
    Route::resource("posts", PostController::class)
)

// app/Controllers/Api/PostController.bq
namespace App\Controllers\Api

use App\Models\Post
use App\Resources\PostResource

class PostController

    #[Public]
    #[Response]
    karo index()
        posts = Post.orderBy("created_at", "desc").paginate(20)
        return PostResource.collection(posts)

    #[Public]
    #[PostResource]
    karo show(int id)
        post = Post.findOrFail(id)
        return new PostResource(post)

    #[Public]
    #[PostResource]
    karo store(PostStoreRequest request)
        post = Post.create(request.validated())
        return new PostResource(post)   // 201 status auto-set

    #[Public]
    #[PostResource]
    karo update(int id, PostStoreRequest request)
        post = Post.findOrFail(id)
        post.update(request.validated())
        return new PostResource(post)

    #[Public]
    #[Response]
    karo destroy(int id)
        Post.findOrFail(id).delete()
        return response().json({ message: "Deleted" })
```

---

## 10.14 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ⚠️ 50 | **Framework name** | Your choice (Urdu word or other) |
| ⚠️ 51 | **CLI tool name** | `barq` / final lang name / other |
| ⚠️ 52 | **Route parameter syntax** | `{id}` / `:id` / `<id>` |
| ⚠️ 53 | **Model auto-table naming** | PluralLowercase (User → users) / explicit only |
| ⚠️ 54 | **Pagination style** | Simple (prev/next) / cursor-based / both |
| ⚠️ 55 | **API versioning** | Route prefix only / header-based / both |
| ⚠️ 56 | **Migration timestamp format** | `YYYY_MM_DD_HHMMSS_` prefix / sequential numbers |

---

*Next: [Step 11 — Versioning & Roadmap →](step-11-versioning-roadmap.md)*
