# Step 8 — Database Layer

> **Previous:** [Step 7 — Standard Library](step-07-standard-library.md)  
> **Next:** [Step 9 — Frontend Support](step-09-frontend-support.md)

---

## 8.1 Supported Databases

All connectors ship with the language — no external driver packages needed.

| Database | Type | Status |
|----------|------|--------|
| MySQL 5.7+ / 8.0+ | Relational SQL | ✅ Core |
| MariaDB 10.5+ | Relational SQL | ✅ Core |
| PostgreSQL 13+ | Relational SQL | ✅ Core |
| SQLite 3 | Embedded SQL | ✅ Core |
| MongoDB 5+ | Document NoSQL | ✅ Core |
| Redis 6+ | Key-Value / Cache | 🔄 Planned |
| Microsoft SQL Server | Relational SQL | 🔄 Planned |
| Cassandra | Wide-Column NoSQL | 🔄 Planned |
| Oracle DB | Relational SQL | 🔄 Planned |

---

## 8.2 Configuration ✅ Decided — Both `.env` and `config/database.bq`

Database credentials can be configured in **either or both** places.  
The config file reads from `.env` for sensitive values — credentials stay out of source code.

```barq
// .env file
DB_DRIVER   = mysql
DB_HOST     = localhost
DB_PORT     = 3306
DB_NAME     = myapp
DB_USER     = root
DB_PASS     = secret
```

```barq
// config/database.bq
database = {
    default: Env.get("DB_DRIVER", "mysql"),

    connections: {
        mysql: {
            host: Env.get("DB_HOST", "localhost"),
            port: Env.get("DB_PORT", 3306),
            name: Env.get("DB_NAME"),
            user: Env.get("DB_USER"),
            pass: Env.get("DB_PASS")
        },
        sqlite: {
            path: "storage/database.db"
        },
        mongodb: {
            uri:  Env.get("MONGO_URI", "mongodb://localhost:27017"),
            name: Env.get("MONGO_DB")
        }
    }
}
```

---

## 8.3 Connecting

```barq
use Barq\Db\Database

// Use default connection (from config)
db = Database.connect()

// Use named connection
db = Database.connect("mysql")
db = Database.connect("sqlite")
db = Database.connect("mongodb")

// Connect with inline config (useful in scripts)
db = Database.connect("postgres", {
    host: "localhost",
    port: 5432,
    name: "mydb",
    user: "admin",
    pass: "secret"
})
```

---

## 8.4 Query Builder — SQL Databases

A chainable query builder for all SQL databases. Same API regardless of MySQL / Postgres / SQLite.

### SELECT
```barq
// Get all records
users = db.table("users").get()

// Get first record
user = db.table("users").first()

// With conditions
user = db.table("users")
         .where("id", 1)
         .first()

// Multiple conditions
users = db.table("users")
          .where("active", 1)
          .where("age", ">", 18)
          .get()

// OR conditions
users = db.table("users")
          .where("role", "admin")
          .orWhere("role", "superadmin")
          .get()

// Select specific columns
users = db.table("users")
          .select(["id", "name", "email"])
          .get()

// Order, limit, offset
users = db.table("users")
          .orderBy("name", "asc")
          .limit(10)
          .offset(20)
          .get()

// Count
total = db.table("users").count()

// Distinct
names = db.table("users").distinct("name").get()
```

### INSERT
```barq
// Single insert
db.table("users").insert({
    name:  "Alice",
    email: "alice@example.com",
    age:   25
})

// Insert and get ID
id = db.table("users").insertGetId({
    name:  "Bob",
    email: "bob@example.com"
})

// Batch insert
db.table("users").insertMany([
    { name: "Alice", email: "alice@example.com" },
    { name: "Bob",   email: "bob@example.com" }
])
```

### UPDATE
```barq
// Update matching rows
db.table("users")
  .where("id", 1)
  .update({ name: "Alice B.", age: 26 })

// Increment / decrement
db.table("posts").where("id", 1).increment("views")
db.table("posts").where("id", 1).decrement("stock", 5)
```

### DELETE
```barq
db.table("users").where("id", 1).delete()
db.table("sessions").where("expired", true).delete()
```

### JOINs
```barq
posts = db.table("posts")
           .join("users", "posts.user_id", "=", "users.id")
           .select(["posts.title", "users.name"])
           .get()

// Left join
users = db.table("users")
           .leftJoin("orders", "users.id", "=", "orders.user_id")
           .get()
```

### RAW SQL
```barq
// When the query builder isn't enough
result = db.raw("SELECT * FROM users WHERE id = ?", [1])
db.rawExec("TRUNCATE TABLE sessions")
```

---

## 8.5 Transactions
```barq
db.transaction(karo(): void
    db.table("accounts").where("id", 1).decrement("balance", 100)
    db.table("accounts").where("id", 2).increment("balance", 100)
)

// Manual transaction
db.beginTransaction()
try
    db.table("orders").insert(orderData)
    db.table("inventory").where("id", itemId).decrement("stock")
    db.commit()
catch (Exception e)
    db.rollback()
    Log.error(e.message)
```

---

## 8.6 Query Builder — MongoDB

MongoDB uses a collection-based API distinct from SQL's table-based API.

```barq
// Connect to MongoDB
db = Database.connect("mongodb")

// Insert
db.collection("users").insert({
    name:  "Alice",
    email: "alice@example.com"
})

// Find
users = db.collection("users").find({ active: true })
user  = db.collection("users").findOne({ _id: id })

// Update
db.collection("users")
  .where({ _id: id })
  .update({ "$set": { name: "Alice B." } })

// Delete
db.collection("users").where({ _id: id }).delete()

// Aggregation
result = db.collection("orders").aggregate([
    { "$match":  { status: "complete" } },
    { "$group":  { _id: "$user_id", total: { "$sum": "$amount" } } },
    { "$sort":   { total: -1 } }
])
```

---

## 8.7 Connection Pooling

Connection pooling is **built into the runtime** — no configuration needed for basic use.

```barq
// Pool is managed automatically
// Default pool size: 10 connections per database
// Configurable in project config:

database = {
    pool: {
        min:     2,
        max:     20,
        timeout: 30    // seconds to wait for available connection
    }
}
```

---

## 8.8 Schema Builder (for Migrations — see Step 10)

```barq
// Create table
schema.create("users", karo(Blueprint table): void
    table.id()                           // auto-increment PK
    table.string("name")
    table.string("email").unique()
    table.string("password")
    table.int("age").nullable()
    table.bool("active").default(true)
    table.text("bio").nullable()
    table.float("balance").default(0.0)
    table.timestamps()                   // created_at, updated_at
)

// Drop table
schema.drop("users")
schema.dropIfExists("old_table")

// Modify table
schema.alter("users", karo(Blueprint table): void
    table.addColumn("phone").string().nullable()
    table.dropColumn("old_field")
    table.rename("fname", "first_name")
)
```

---

## 8.9 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ⚠️ 38 | **DB config location** | `.env` only / `config/database.bq` / both |
| ⚠️ 39 | **SQL vs NoSQL API unification** | Separate `table()`/`collection()` / unified API |
| ⚠️ 40 | **Raw query return type** | Array of dicts / typed result objects |
| ⚠️ 41 | **Default connection pool size** | 10 / configurable only / auto (based on CPU) |
| ⚠️ 42 | **Multiple DB connections at once** | Supported / single connection only in v1 |
| ⚠️ 43 | **Schema migration auto-detect** | Auto-migrate on change / explicit migration commands only |

---

*Next: [Step 9 — Frontend Support →](step-09-frontend-support.md)*
