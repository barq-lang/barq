# Step 9 — Frontend / Template Support

> **Previous:** [Step 8 — Database Layer](step-08-database-layer.md)  
> **Next:** [Step 10 — MVC Framework](step-10-framework.md)

---

## 9.1 What Does "Frontend Support" Mean in Barq?

Barq is a **server-side language**. "Frontend support" means:

1. **Rendering HTML** from the server in response to a browser request
2. **Embedding dynamic data** (from the database, session, etc.) into HTML output
3. **Serving static assets** (CSS, JS, images) through the built-in HTTP server

Barq does **not** compile to JavaScript or run in the browser.  
Client-side JavaScript is written separately in `.js` files and served as static assets.

---

## 9.2 Template Model ✅ Decided — Option C (Barq-native HTML tags)

Barq templates use HTML-like custom tags for logic. Output uses `{variable}` curly-brace syntax.  
This keeps templates readable and tool-friendly while being clearly Barq.

### Core Template Tags

| Tag | Purpose |
|-----|---------|
| `<each var in {collection}>` | Loop over a collection |
| `<if condition="{expr}">` | Conditional block |
| `<elseif condition="{expr}">` | Else-if branch |
| `<else>` | Else branch |
| `<include file="path" />` | Include a partial template |
| `<extends layout="name" />` | Inherit a layout |
| `<section name="name">` | Define a layout section |
| `<yield name="name" />` | Output a section in layout (`<yield name="x">default</yield>` gives default content) |
| `<let name="{expr}" />` | Set a template variable |
| `<verbatim>…</verbatim>` | Output a block as-is, braces included |
| `{-- comment --}` | Template comment (not sent to the browser) |

### Full Example

```html
<!-- views/users/index.bq -->
<!DOCTYPE html>
<html>
<body>
    <h1>Welcome, {auth.name}</h1>

    <if condition="{len(users) == 0}">
        <p>No users found.</p>
    <else>
        <each user in {users}>
            <div>
                <strong>{user.name}</strong> — {user.email}
            </div>
        </each>
    </if>

    <include file="partials/footer" />
</body>
</html>
```

### Conditionals

```html
<if condition="{score >= 90}">
    <p>Excellent!</p>
<elseif condition="{score >= 70}">
    <p>Good work</p>
<elseif condition="{score >= 50}">
    <p>Passing</p>
<else>
    <p>Needs improvement</p>
</if>
```

### Loops

```html
<!-- List loop -->
<each name in {names}>
    <li>{name}</li>
</each>

<!-- Loop with index -->
<each i, name in {names}>
    <li>{i}: {name}</li>
</each>

<!-- Empty list, and the loop variable -->
<each user in {users}>
    <li class="{loop.odd ? "odd" : "even"}">{loop.number} of {loop.count}: {user.name}</li>
<else>
    <li>No users yet.</li>
</each>

<!-- Dict loop -->
<each key, value in {settings}>
    <tr><td>{key}</td><td>{value}</td></tr>
</each>
```

### Output Escaping

```html
<!-- Auto HTML-escaped (safe, default) -->
<p>{user.name}</p>

<!-- Raw / unescaped output — explicit opt-in only -->
<p>{!user.bio!}</p>

<!-- A literal brace -->
<p>\{ not an expression }</p>
```

- `{expr}` is any Barq expression. `null` prints nothing.
- In templates, `a.b` also reads dict keys (`{user.name}` on a database row), and the left side of `??` may be a missing variable or key: `{title ?? "Untitled"}`.
- Inside `<script>` and `<style>`, plain braces belong to JavaScript/CSS and are left alone. Only `{!expr!}` is filled in there; pass data with `{!json(data)!}`, which escapes `<`, `>` and `&` so the data can't close the tag.
- A line holding only a control tag (`<if>`, `<each>`, `<section>`, `</if>` …) is left out of the output.
- An HTML `<section>` without a `name` attribute is plain HTML.

---

## 9.3 Rendering Views from a Controller

Regardless of which template model is chosen, views are rendered using `view()`:

```barq
// From a controller
#[Public]
#[View]
karo index()
    users = User.all()
    return view("users.index", { users: users })

// With status code
return view("errors.404", {}, 404)
```

The `view()` function:
- Locates the template file in the `views/` directory
- Passes the second argument as template variables
- Renders the template and returns an HTTP response

---

## 9.4 Template File Location

```
views/
├── layout.bq          // base layout
├── home/
│   └── index.bq       // view("home.index")
├── users/
│   ├── index.bq       // view("users.index")
│   ├── show.bq        // view("users.show")
│   └── form.bq        // view("users.form")
└── errors/
    ├── 404.bq
    └── 500.bq
```

Dot notation maps to directory path: `"users.index"` → `views/users/index.bq`

---

## 9.5 Layout Inheritance ✅ Decided — HTML tags

```html
<!-- views/layout.bq -->
<html>
<head><title><yield name="title">My App</yield></title></head>
<body>
    <include file="partials.nav" />
<yield name="content" />
    <section name="footer">
    <footer>Default footer</footer>
    </section>
</body>
</html>

<!-- views/home/index.bq -->
<extends layout="layout" />

<section name="title">Home</section>

<section name="content">
    <h1>Home Page</h1>
</section>
```

- A template that uses `<extends>` may only have `<section>` and `<let>` at its top level.
- A `<section>` inside a layout prints its own content unless a child template replaces it. Layouts can extend other layouts.

---

## 9.6 Partials

```html
<include file="partials.navbar" />
<include file="partials.card" with="{#{"title": "Hello", "body": "World"}}" />
```

An included view sees the current variables plus the `with` dict.

---

## 9.7 The View Module

| Member | Purpose |
|--------|---------|
| `view(name, data?, status?)` | Render → HTML `Response` |
| `View.render(name, data?)` | Render → string |
| `View.string(template, data?)` | Render template text |
| `View.exists(name)` | Does the view file exist? |
| `View.share(name, value)` / `View.share(dict)` | Variables every view sees |
| `View.path(folder?)` | Get or set the views folder (default `views`) |
| `View.escape(value)`, `View.json(value)` | HTML-escape / script-safe JSON |

Errors point into the template file and line. Template syntax errors raise `TemplateException`.

---

## 9.8 Serving Static Assets

The built-in HTTP server automatically serves files from the `public/` directory:

```
public/
├── css/
│   └── app.css
├── js/
│   └── app.js
└── images/
    └── logo.png
```

In templates:
```html
<link rel="stylesheet" href="/css/app.css">
<script src="/js/app.js"></script>
<img src="/images/logo.png">
```

---

## 9.9 JSON Responses (API mode)

When Barq is used as a pure API backend (no HTML output):

```barq
// Return JSON — no view needed
#[Public]
#[Response]
karo index()
    users = User.all()
    return response().json({ data: users })

// With HTTP status code
return response().json({ error: "Not found" }, 404)
```

---

## 9.10 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ✅ 44 | **Template model** | Option C (HTML tags) |
| ✅ 45 | **Layout inheritance syntax** | `<extends>` / `<section>` / `<yield>` tags (implemented in v0.8.0) |
| ⚠️ 46 | **Template file extension** | `.bq` under `views/` for now (implemented) / `.html.bq` / `.bq.html` / `.html` |
| ✅ 47 | **Unescaped output syntax** | `{!expr!}` |
| ⚠️ 48 | **Asset management** | Serve from `public/` only / built-in asset pipeline |
| ⚠️ 49 | **JavaScript interop** | Separate `.js` files only / embedded `<script>` / compile ASH to JS |

---

*Next: [Step 10 — MVC Framework →](step-10-framework.md)*
