# Step 16 — Frontend Assets: Tailwind & Bootstrap

> **Previous:** [Step 15 — Multi-lingual Support](step-15-multilingual.md)
> **Next:** [Open Decisions](open-decisions.md)

---

## 16.1 Overview

Barq's framework has **first-class support for both Tailwind CSS and Bootstrap**.  
**Default: Tailwind CSS** — new projects use Tailwind out of the box.

You can switch to Bootstrap or use no CSS framework — the asset pipeline supports all three.

| Framework | Support | Default |
|-----------|---------|---------|
| Tailwind CSS v3 | ✅ Full | ✅ Yes |
| Bootstrap 5 | ✅ Full | No |
| None (custom CSS) | ✅ | No |

---

## 16.2 Tailwind CSS (Default)

When you run `barq new my-app`, Tailwind is set up automatically.

### Project structure

```
public/
├── css/
│   └── app.css          // built CSS output (generated)
├── js/
│   └── app.js
resources/
├── css/
│   └── app.css          // your Tailwind source
└── js/
    └── app.js
tailwind.config.bq        // Tailwind configuration
postcss.config.bq         // PostCSS configuration
```

### Tailwind config file

```barq
// tailwind.config.bq
return #{
    "content": #[
        "views/**/*.bq",
        "resources/**/*.bq",
        "app/**/*.bq"
    ],
    "darkMode": "class",    // or "media"
    "theme": #{
        "extend": #{
            "colors": #{
                "primary": #{
                    "50":  "#f0fdf0",
                    "100": "#dcfcdc",
                    "200": "#b8f7b8",
                    "300": "#81ed81",
                    "400": "#4dd94d",
                    "500": "#228B22",   // Barq Green (primary brand color)
                    "600": "#1a6b1a",
                    "700": "#145214",
                    "800": "#0f3d0f",
                    "900": "#082b08"
                },
                "secondary": #{
                    "50":  "#eeeeff",
                    "100": "#ddddff",
                    "200": "#c0c0ff",
                    "300": "#9696f9",
                    "400": "#6b6bf5",
                    "500": "#3434e6",   // Barq Blue (secondary brand color)
                    "600": "#2525bb",
                    "700": "#1b1b8e",
                    "800": "#141462",
                    "900": "#0d0d44"
                }
            },
            "fontFamily": #{
                "display": #["Sora", "system-ui", "sans-serif"],
                "body":    #["Inter", "system-ui", "sans-serif"],
                "mono":    #["JetBrains Mono", "monospace"]
            }
        }
    },
    "plugins": #[]
}
```

### Source CSS file

```css
/* resources/css/app.css */
@tailwind base;
@tailwind components;
@tailwind utilities;

/* Your custom component classes */
@layer components {
  .btn-primary {
    @apply bg-primary-500 hover:bg-primary-600 text-white font-semibold
           px-6 py-2.5 rounded-lg transition-colors duration-200;
  }

  .btn-secondary {
    @apply border border-secondary-500 text-secondary-500
           hover:bg-secondary-500 hover:text-white
           px-6 py-2.5 rounded-lg transition-colors duration-200;
  }

  .card {
    @apply bg-white dark:bg-gray-800 rounded-xl shadow-sm
           border border-gray-100 dark:border-gray-700 p-6;
  }

  .input {
    @apply w-full rounded-lg border border-gray-300 dark:border-gray-600
           bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100
           px-4 py-2.5 focus:ring-2 focus:ring-primary-500
           focus:border-transparent outline-none transition;
  }
}
```

### Build commands

```bash
# Build for development (with watcher)
barq asset:dev

# Build for production (minified, purged)
barq asset:build

# Watch for changes
barq asset:watch
```

### Using in templates

```html
<!-- views/home.bq -->
<div class="min-h-screen bg-gray-50 dark:bg-gray-900">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-12">

    <h1 class="text-3xl font-bold text-gray-900 dark:text-white mb-6">
      Welcome to {config("app.name")}
    </h1>

    <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
      <each item="user" in="users">
        <div class="card">
          <h2 class="text-lg font-semibold text-gray-800 dark:text-gray-100">
            {user.name}
          </h2>
          <p class="text-gray-500 text-sm mt-1">{user.email}</p>
          <if condition="user.active">
            <span class="inline-flex items-center px-2.5 py-0.5 rounded-full
                         text-xs font-medium bg-green-100 text-green-800
                         dark:bg-green-900 dark:text-green-200 mt-3">
              Active
            </span>
          </if>
        </div>
      </each>
    </div>

  </div>
</div>
```

---

## 16.3 Switching to Bootstrap 5

If you prefer Bootstrap, install with:

```bash
barq new my-app --css=bootstrap
# or on existing app:
barq asset:switch bootstrap
```

This replaces the Tailwind config with Bootstrap's setup:

```barq
// config/assets.bq  (after switching)
return #{
    "css_framework": "bootstrap",
    "version": "5.3.0"
}
```

```css
/* resources/css/app.css — Bootstrap version */
@import "bootstrap/scss/bootstrap";

/* Custom overrides */
:root {
  --bs-primary:   #228B22;
  --bs-secondary: #3434e6;
}

.btn-primary {
  background-color: var(--bs-primary);
  border-color: var(--bs-primary);
}
```

### Using Bootstrap in templates

```html
<div class="container py-5">
  <div class="row g-4">
    <each item="user" in="users">
      <div class="col-md-4">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">{user.name}</h5>
            <p class="card-text text-muted">{user.email}</p>
            <if condition="user.active">
              <span class="badge bg-success">Active</span>
            </if>
          </div>
        </div>
      </div>
    </each>
  </div>
</div>
```

---

## 16.4 Asset Pipeline

Barq's asset pipeline (`barq asset:*`) handles:

| Task | Command |
|------|---------|
| Build CSS + JS | `barq asset:build` |
| Watch + rebuild | `barq asset:watch` |
| Development server | `barq asset:dev` |
| Cache-bust assets | `barq asset:version` |

### Versioned assets in templates

```barq
// In any template — Barq adds ?v=hash automatically
<link rel="stylesheet" href="{asset('css/app.css')}">
<script src="{asset('js/app.js')}"></script>

// Generated output:
// <link rel="stylesheet" href="/css/app.css?v=abc12345">
// <script src="/js/app.js?v=abc12345"></script>
```

---

## 16.5 Dark Mode Support

Both Tailwind and Bootstrap setups include dark mode support.

```barq
// config/assets.bq
return #{
    "dark_mode": "class"   // "class" or "media"
}
```

**Class-based dark mode** (recommended): Toggle by adding/removing `.dark` class on `<html>`.

```barq
// In any controller
karo toggleDarkMode(Request request)
    string mode = session("dark_mode") == "dark" ? "light" : "dark"
    session("dark_mode", mode)
    return redirect()->back()
```

```html
<!-- In layout template: auto-apply dark mode from session -->
<html class="{session("dark_mode") == "dark" ? "dark" : ""}">
```

---

## 16.6 JavaScript

The framework includes a lightweight asset build for JavaScript too.

```
resources/js/
├── app.js        // main entry point
├── bootstrap.js  // framework boot (axios, alpine, etc.)
└── components/   // your JS components
```

```js
// resources/js/app.js
import './bootstrap';
import Alpine from 'alpinejs';

window.Alpine = Alpine;
Alpine.start();
```

**Supported JS tools** (zero config needed):
- Alpine.js (lightweight Vue-style reactivity)
- Axios (HTTP client, pre-configured with CSRF token)
- Barq SPA components (see Step 9 — Frontend Support)

---

## 16.7 CSRF Protection

Barq automatically protects all non-GET forms with a CSRF token.

```html
<!-- In any form template — always include this -->
<form method="POST" action="/users">
    {csrf_field()}
    <!-- renders: <input type="hidden" name="_token" value="abc123..."> -->

    <input type="text" name="name">
    <button type="submit">Save</button>
</form>

<!-- For AJAX requests -->
<meta name="csrf-token" content="{csrf_token()}">
```

For Axios requests, the CSRF token is injected automatically from the meta tag.

---

## 16.8 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ⚠️ 81 | **JS bundler** | Built-in simple bundler / Vite integration / Webpack |
| ⚠️ 82 | **Alpine.js bundled?** | Yes, included with framework / Optional install |
| ⚠️ 83 | **CSS framework default** | Tailwind ✅ (decided) / Bootstrap / none |
| ✅ 83 | **CSS framework default** | **Tailwind CSS** — Bootstrap supported as opt-in |

---

*Back to: [Step 15 — Multi-lingual Support →](step-15-multilingual.md)*
