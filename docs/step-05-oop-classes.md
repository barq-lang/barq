# Step 5 — OOP: Classes, Inheritance & Access

> **Previous:** [Step 4 — Control Flow](step-04-control-flow.md)  
> **Next:** [Step 6 — Namespaces](step-06-namespaces.md)

---

## 5.1 Defining a Class

Classes are declared with the `class` keyword. The body is indented — no braces.  
**Properties** use inline visibility keywords. **Methods** use `#[Visibility]` and `#[ReturnType]` attributes above the `karo` keyword.

```barq
class User

    public  string name
    private string email
    private string password

    #[Public]
    #[void]
    karo __construct(string name, string email, string password)
        this.name     = name
        this.email    = email
        this.password = Hash.bcrypt(password)

    #[Public]
    #[string]
    karo getName()
        return this.name

    #[Public]
    #[string]
    karo getEmail()
        return this.email

    #[Private]
    #[bool]
    karo checkPassword(string raw)
        return Hash.verify(raw, this.password)
```

---

## 5.2 Creating Objects

```barq
user = new User("Alice", "alice@example.com", "secret")

print(user.getName())    // Alice
print(user.getEmail())   // alice@example.com
```

---

## 5.3 Access Modifiers

**Properties** — inline keyword:
```barq
public  string name
private string email
protected string role
```

**Methods** — `#[Visibility]` attribute:

| Attribute | Accessible from |
|-----------|----------------|
| `#[Public]` | Anywhere |
| `#[Private]` | Same class only |
| `#[Protected]` | Same class + subclasses |

```barq
class BankAccount

    private float balance

    #[Public]
    #[void]
    karo deposit(float amount)
        this.balance += amount

    #[Public]
    #[float]
    karo getBalance()
        return this.balance

    #[Private]
    #[void]
    karo audit()
        Log.write("Balance: " + string(this.balance))
```

---

## 5.4 Inheritance

Use `extends` to inherit from a parent class.

```barq
class Animal

    public string name

    #[Public]
    #[void]
    karo __construct(string name)
        this.name = name

    #[Public]
    #[string]
    karo speak()
        return "..."


class Dog extends Animal

    #[Public]
    #[string]
    karo speak()
        return this.name + " says: Woof!"


class Cat extends Animal

    #[Public]
    #[string]
    karo speak()
        return this.name + " says: Meow!"


dog = new Dog("Rex")
print(dog.speak())     // Rex says: Woof!
```

---

## 5.5 Calling Parent Methods

```barq
class Admin extends User

    private string role

    #[Public]
    #[void]
    karo __construct(string name, string email, string password, string role)
        parent::__construct(name, email, password)
        this.role = role

    #[Public]
    #[string]
    karo getRole()
        return this.role
```

---

## 5.6 Static Members

⚠️ **Decision pending** — see [open-decisions.md](open-decisions.md) #20.

Proposed — `#[Static]` attribute alongside visibility:

```barq
class Counter

    public static int count = 0

    #[Public]
    #[void]
    karo __construct()
        Counter::count += 1

    #[Public]
    #[Static]
    #[int]
    karo getCount()
        return Counter::count
```

Access syntax options (decision pending — see #25):
- `Counter::count` (PHP double-colon — static access)
- `Counter.count` (dot access on class name)

---

## 5.7 Abstract Classes ✅ Decided — Supported

Use `abstract class` and `#[Abstract]` attribute on abstract methods.  
Abstract methods have no body — subclasses must implement them.

```barq
abstract class Shape

    #[Abstract]
    #[Public]
    #[float]
    karo area()           // no body — must be implemented by subclass

    #[Abstract]
    #[Public]
    #[string]
    karo label()          // no body

    #[Public]
    #[string]
    karo describe()       // has a body — not abstract
        return "I am a {this.label()} with area: {~string(this.area())}"


class Circle extends Shape

    private float radius

    #[Public]
    #[void]
    karo __construct(float radius)
        this.radius = radius

    #[Public]
    #[float]
    karo area()
        return Math.PI * this.radius * this.radius

    #[Public]
    #[string]
    karo label()
        return "circle"


class Rectangle extends Shape

    private float width
    private float height

    #[Public]
    #[void]
    karo __construct(float width, float height)
        this.width  = width
        this.height = height

    #[Public]
    #[float]
    karo area()
        return this.width * this.height

    #[Public]
    #[string]
    karo label()
        return "rectangle"


c = new Circle(5.0)
r = new Rectangle(4.0, 6.0)
print(c.describe())    // I am a circle with area: 78.53
print(r.describe())    // I am a rectangle with area: 24.0
```

**Rules:**
- Cannot instantiate an abstract class directly — `new Shape()` → compile error
- Subclass must implement all abstract methods, or it must also be declared `abstract`

---

## 5.8 Interfaces

🟡 **Implemented in v0.12.0 (provisional)** — PHP-style `interface` and `implements`, see [open-decisions.md](open-decisions.md) #25.

```barq
interface Serializable

    #[Public]
    #[string]
    karo toJson()

    #[Public]
    #[void]
    karo fromJson(string json)


interface Loggable

    #[Public]
    #[void]
    karo log()


class User implements Serializable, Loggable

    #[Public]
    #[string]
    karo toJson()
        return Json.encode(this)

    #[Public]
    #[void]
    karo fromJson(string json)
        this = Json.decode(json)

    #[Public]
    #[void]
    karo log()
        Logger.info("User: " + this.name)
```

Rules:
- An interface holds method signatures (no body; always public) and constants (`const UNIT = "cm"`, read as `Iface::UNIT` or through the class). No properties or constructor.
- `interface Shape extends HasArea, Named` — an interface can extend several.
- `class C extends Parent implements A, B` — one parent class, any number of interfaces. Enums can implement them: `enum Suit implements Labeled`.
- When the class is loaded, each interface method must exist on it, be public, match `#[Static]`, and accept the interface's arguments (extra optional parameters are fine). Abstract classes may leave methods to their subclasses.
- Interfaces are types: `list<Shape>`, `Shape s = new Square(2)`, `x is Shape`, `except Retryable as e`, and container bindings `app().bind(Clock, SystemClock)`.

---

## 5.9 Traits

🟡 **Implemented in v0.12.0 (provisional)** — see [open-decisions.md](open-decisions.md) #26.

Traits allow sharing behavior across unrelated classes (like PHP traits):

```barq
trait Timestampable

    public string createdAt
    public string updatedAt

    #[Public]
    #[void]
    karo touch()
        this.updatedAt = Date.now()


trait SoftDeletable

    public bool deleted = false
    public string deletedAt = null

    #[Public]
    #[void]
    karo softDelete()
        this.deleted   = true
        this.deletedAt = Date.now()


class Post

    use Timestampable
    use SoftDeletable          // or on one line: use Timestampable, SoftDeletable

    public string title
    public string body
```

Rules:
- A trait's properties, methods, static properties and constants are copied into the class as if written there: `private` members are reachable, `self::` and `__class__` mean the class, and each class gets its own static values.
- A method written in the class replaces the trait's. If two traits give the same method, the class writes it itself or chooses one with a rule (below). A property declared in both the class and a trait is an error.
- Traits can `use` other traits and declare `#[Abstract]` methods that the class must provide.
- A trait is not a type and can't be created with `new`.

**Conflict rules** (🟡 v0.13.0, provisional — #96). PHP's `insteadof` / `as`, written in an indented block under the `use` line:

```barq
class Person
    use Hello, Salaam
        Hello::greet insteadof Salaam    // Hello's greet() wins
        Salaam::greet as salaam          // Salaam's is still reachable as salaam()
        wave as protected                // new visibility
        wave as public waveHi            // a copy with a new name (and visibility)
```

`method as …` may leave out the trait when only one trait has that method. An alias can name a method that `insteadof` dropped. Rules can only name traits used by the class, and only methods.

---

## 5.10 Constructor Shorthand

🟡 **Both options work since v0.12.0 (provisional)** — see [open-decisions.md](open-decisions.md) #27.

**Option A — Explicit (always):**
```barq
class Point

    public int x
    public int y

    #[Public]
    #[void]
    karo __construct(int x, int y)
        this.x = x
        this.y = y
```

**Option B — Constructor shorthand (declare and assign in one):**
```barq
class Point

    #[Public]
    #[void]
    karo __construct(public int x, public int y = 0)
        // x and y are automatically assigned as properties
```

A visibility keyword (`public`, `protected`, `private`) in front of a constructor parameter declares the property and assigns it before the constructor body runs. Promoted and plain parameters can be mixed, and promoted ones can have defaults. Only `__construct` can do this.

---

## 5.11 Enums ✅ Decided — PHP-style

An enum is a type with a fixed set of named values, called **cases**. Like PHP 8.1, Barq has **pure** enums (cases only) and **backed** enums (each case also holds an `int` or `string` value — handy for database columns and JSON). The backing type is an attribute on the line above the enum: `#[String]` or `#[Int]`.

### Pure enum
```barq
enum Suit
    case Hearts
    case Diamonds
    case Clubs
    case Spades

Suit s = Suit::Hearts
print(s)            // Suit::Hearts
print(s.name)       // Hearts
```

### Backed enum
Put `#[String]` or `#[Int]` on the line above `enum`, the same way a function's return type sits above `karo`. Every case then needs a unique plain value of that type:

```barq
#[String]
enum Status
    case Active  = "active"
    case Pending = "pending"
    case Banned  = "banned"

#[Int]
enum Priority
    case Low  = 1
    case High = 10

print(Status::Active.value)        // active
Status s = Status::from("banned")  // Status::Banned — raises ValueException if no case matches
Status found = Status::tryFrom("x")   // null when no case matches
```

### Methods and constants
Enums can have methods, static methods and constants — but no properties. Inside a method, `this` is the current case:

```barq
#[String]
enum Status
    case Active = "active"
    case Banned = "banned"

    const DEFAULT = Status::Active

    #[Public]
    #[string]
    karo label()
        switch (this)
            case Status::Active:
                return "Can log in"
            default:
                return "Blocked"

    #[Public]
    #[Static]
    #[list<Status>]
    karo loginAllowed()
        return Status::cases().filter(kr(Status s): bool => s == Status::Active)

print(Status::Banned.label())   // Blocked
```

### What every enum has

| Member | Pure | Backed | Returns |
|--------|:----:|:------:|---------|
| `Enum::Case` / `Enum.Case` | ✅ | ✅ | the case |
| `case.name` | ✅ | ✅ | `string` — the case name |
| `case.value` | — | ✅ | `int` or `string` |
| `Enum::cases()` | ✅ | ✅ | list of all cases, in declaration order |
| `Enum::from(value)` | — | ✅ | the matching case, or raises `ValueException` |
| `Enum::tryFrom(value)` | — | ✅ | the matching case, or `null` |

### Rules
- An enum is a type: `Status s = Status::Active`, `karo ban(User u, Status reason)`, `list<Suit>`, `dict<string, Status>`.
- Each case exists exactly once, so `==` compares cases directly; `s is Status` checks the type. Cases work as dict keys, in sets, with `in`, and in `switch`.
- Cases are read-only — `s.value = "x"` is a compile error.
- `new Status()` is an error; use a case.
- Enums cannot extend a class or be extended, and cannot have properties or abstract methods. `cases`, `from` and `tryFrom` are reserved method names.
- `Json.encode()` writes a backed case as its value; a pure case cannot be encoded.
- `print` and string interpolation show `Status::Active`; `dump()` shows `enum(Status::Active: "active")`.
- Enums are autoloaded like classes: `namespace App\Enums` in `app/Enums/Status.bq`, then `use App\Enums\Status`.

---

## 5.12 Magic Methods (Proposed)

| Method | Trigger |
|--------|---------|
| `__construct()` | Object creation (`new`) |
| `__destruct()` | Object destroyed |
| `__toString()` | Cast to string |
| `__get(name)` | Access undefined property |
| `__set(name, val)` | Set undefined property |
| `__call(name, args)` | Call undefined method |

---

## 5.13 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ✅ 21 | **Abstract classes** | Supported — `abstract class` + `#[Abstract]` on methods |
| ✅ | **Enums** | PHP-style — pure `enum Suit`, and backed with `#[String]` / `#[Int]` above `enum Status`; methods and constants |
| ⚠️ 20 | **Static members** | Supported with `#[Static]` / not in v1 |
| 🟡 22 | **Interfaces** | Supported / not in v1 — *v0.12.0: supported* |
| 🟡 23 | **Traits** | Supported / not in v1 — *v0.12.0: supported* |
| 🟡 24 | **Constructor shorthand** | Supported / explicit only — *v0.12.0: both work* |
| ⚠️ 25 | **Static access syntax** | `Class::member` / `Class.member` |
| 🟡 26 | **Multiple inheritance** | Not allowed / via interfaces only / allowed — *v0.12.0: via interfaces (and traits)* |

---

*Next: [Step 6 — Namespaces →](step-06-namespaces.md)*
