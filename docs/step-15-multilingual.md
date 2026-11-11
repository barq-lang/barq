# Step 15 — Multi-lingual Support (i18n)

> **Previous:** [Step 14 — Debug & Error Pages](step-14-debug-errors.md)
> **Next:** [Packages — Barq Auth](https://github.com/barq-lang/barq-auth/blob/main/docs/barq-auth.md)

---

## 15.1 Overview

Barq has **first-class multi-lingual support** built into the framework — no third-party package needed.

Features:
- Language files in `lang/` directory
- `trans()` and `__()` helpers globally available
- Pluralization rules per language
- Variable substitution in translation strings
- RTL (right-to-left) auto-detection for Urdu, Arabic, Persian, Hebrew
- Number and date localization
- Currency formatting per locale
- Locale detection from browser, session, URL, or user preference

---

## 15.2 Directory Structure

```
lang/
├── en/
│   ├── messages.bq
│   ├── auth.bq
│   ├── validation.bq
│   └── pagination.bq
├── ur/
│   ├── messages.bq
│   ├── auth.bq
│   ├── validation.bq
│   └── pagination.bq
├── ar/
│   └── messages.bq
└── fr/
    └── messages.bq
```

---

## 15.3 Language Files

Language files are plain `.bq` files that return a dictionary of translation strings.

```barq
// lang/en/messages.bq
return #{
    "welcome":          "Welcome to {app}!",
    "login_prompt":     "Please login to continue.",
    "logout":           "You have been logged out.",
    "greeting":         "Hello, {name}!",

    "users": #{
        "created":      "User created successfully.",
        "updated":      "User updated successfully.",
        "deleted":      "User deleted.",
        "not_found":    "User with ID {id} not found."
    },

    "posts": #{
        "created":      "Post published.",
        "draft_saved":  "Draft saved.",
        "deleted":      "Post deleted."
    },

    // Pluralization: {1} = singular, [2,*] = plural
    "item_count":       "{1} :count item|[2,*] :count items",
    "new_message":      "{1} new message|[2,*] :count new messages"
}
```

```barq
// lang/ur/messages.bq
return #{
    "welcome":          "{app} میں خوش آمدید!",
    "login_prompt":     "جاری رکھنے کے لیے لاگ ان کریں۔",
    "logout":           "آپ لاگ آؤٹ ہو گئے ہیں۔",
    "greeting":         "السلام علیکم، {name}!",

    "users": #{
        "created":      "صارف کامیابی سے بنایا گیا۔",
        "updated":      "صارف کی معلومات اپ ڈیٹ ہو گئی۔",
        "deleted":      "صارف حذف کر دیا گیا۔",
        "not_found":    "ID {id} کا صارف نہیں ملا۔"
    },

    "item_count":       "{1} :count چیز|[2,*] :count چیزیں"
}
```

---

## 15.4 Using Translations in Code

```barq
// Basic translation
trans("messages.welcome", #{"app": "My App"})
// → "Welcome to My App!"

// Shorthand alias (identical behaviour)
__("messages.welcome", #{"app": "My App"})
// → "Welcome to My App!"

// Nested key with dot notation
trans("messages.users.created")
// → "User created successfully."

// No replacements needed
trans("messages.logout")
// → "You have been logged out."

// Key not found — returns the key itself
trans("messages.nonexistent")
// → "messages.nonexistent"
```

---

## 15.5 Pluralization

```barq
// Language file defines both forms:
// "item_count": "{1} :count item|[2,*] :count items"

trans_choice("messages.item_count", 1)
// → "1 item"

trans_choice("messages.item_count", 5)
// → "5 items"

trans_choice("messages.item_count", 0)
// → "0 items"

// With extra replacements
trans_choice("messages.item_count", 3, #{"count": 3})
// → "3 items"
```

**Pluralization rule syntax:**

```
"{1} :count apple|[2,*] :count apples"
   ↑              ↑
   exactly 1      2 or more
```

```
"{0} No items|{1} One item|[2,*] :count items"
 ↑             ↑            ↑
 zero          exactly 1    2 or more
```

---

## 15.6 Configuration

```barq
// config/app.bq
locale          = Env.get("APP_LOCALE", "en")
fallback_locale = "en"
supported_locales = #[
    "en",   // English
    "ur",   // Urdu
    "ar",   // Arabic
    "fr",   // French
    "de",   // German
    "zh",   // Chinese (Simplified)
    "es",   // Spanish
    "hi"    // Hindi
]
timezone = "UTC"
```

---

## 15.7 Changing Locale at Runtime

```barq
// Set locale for the current request
App.setLocale("ur")

// Get current locale
App.locale()           → "ur"

// Check if locale is active
App.isLocale("ur")     → true

// Get fallback locale
App.fallbackLocale()   → "en"
```

---

## 15.8 RTL (Right-to-Left) Support

Barq **automatically detects** when the current locale uses a right-to-left script and sets `App.isRtl()` accordingly.

| Locale | Language | Script Direction |
|--------|----------|-----------------|
| `ur` | Urdu | RTL (Nastaliq) |
| `ar` | Arabic | RTL |
| `fa` | Persian / Farsi | RTL |
| `he` | Hebrew | RTL |
| `ps` | Pashto | RTL |
| All others | — | LTR |

```barq
// Check in controller
App.isRtl()            → true (when locale = ur/ar/fa/he)
App.textDirection()    → "rtl" or "ltr"
```

**In templates — automatic RTL layout:**

```html
<!-- views/layout.bq -->
<html dir="{App.textDirection()}" lang="{App.locale()}">
<body class="<if condition="App.isRtl()">rtl</if>">
```

The framework automatically:
- Sets `dir="rtl"` on the HTML element
- Flips flex/grid directions for RTL layouts
- Mirrors padding/margin where needed
- Uses appropriate Urdu/Arabic font stacks

---

## 15.9 Locale Detection Middleware

The framework can detect and set the locale automatically from multiple sources:

```barq
// config/app.bq
locale_detection = #{
    "sources": #["session", "user_preference", "browser", "default"],
    "url_prefix": false,       // set true to use /ur/users /en/users style URLs
    "url_prefix_format": "/{locale}",
    "session_key": "locale",
    "cookie_name": "barq_locale"
}
```

**Detection order:**
1. URL prefix (if `url_prefix: true`) — `/ur/page` sets `ur`
2. Query parameter — `?lang=ur`
3. Session — previously saved user preference
4. User model — `user.locale` field (if Auth user has a locale)
5. Browser `Accept-Language` header
6. App default locale from config

---

## 15.10 Using Translations in Templates

```html
<!-- views/home.bq template -->
<h1>{trans("messages.welcome", #{"app": config("app.name")})}</h1>
<p>{__("messages.login_prompt")}</p>

<!-- Pluralization in template -->
<span>{trans_choice("messages.item_count", posts.length)}</span>

<!-- Conditional RTL class -->
<div class="sidebar <if condition="App.isRtl()">sidebar-rtl</if>">
```

---

## 15.11 Number Localization

```barq
// Format numbers per locale
Num.format(1234567.89)                        // → "1,234,567.89" (en)
Num.format(1234567.89, locale: "ur")          // → "12,34,567.89" (Urdu grouping)
Num.format(1234567.89, locale: "de")          // → "1.234.567,89" (German)

// Currency
Num.currency(1500.00, "USD")                  // → "$1,500.00"
Num.currency(1500.00, "PKR")                  // → "Rs 1,500"
Num.currency(1500.00, "EUR", locale: "de")    // → "1.500,00 €"

// Urdu/Arabic numerals (optional)
Num.format(42, numerals: "arabic")            // → "٤٢"
Num.format(42, numerals: "urdu")              // → "۴۲"
```

---

## 15.12 Date Localization

```barq
// Format dates in the current locale
Date.format(now, "d F Y", locale: "en")       // → "15 January 2026"
Date.format(now, "d F Y", locale: "ur")       // → "15 جنوری 2026"
Date.format(now, "d F Y", locale: "ar")       // → "15 يناير 2026"

// Relative time in locale
Date.toRelative(pastDate, locale: "ur")       // → "3 دن پہلے"
Date.toRelative(futureDate, locale: "ur")     // → "2 گھنٹے بعد"
Date.toRelative(pastDate, locale: "ar")       // → "منذ 3 أيام"
```

---

## 15.13 Validation Messages (Localized)

Validation error messages are also translatable. The framework ships with English defaults and you override per locale.

```barq
// lang/en/validation.bq
return #{
    "required":     "The {field} field is required.",
    "email":        "The {field} must be a valid email address.",
    "min":          "The {field} must be at least {min} characters.",
    "max":          "The {field} may not be greater than {max} characters.",
    "unique":       "The {field} has already been taken.",
    "confirmed":    "The {field} confirmation does not match."
}
```

```barq
// lang/ur/validation.bq
return #{
    "required":     "{field} کا خانہ ضروری ہے۔",
    "email":        "{field} ایک درست ای میل ایڈریس ہونا چاہیے۔",
    "min":          "{field} کم از کم {min} حروف ہونے چاہییں۔",
    "max":          "{field} {max} حروف سے زیادہ نہیں ہو سکتا۔",
    "unique":       "یہ {field} پہلے سے موجود ہے۔",
    "confirmed":    "{field} کی تصدیق میل نہیں کھاتی۔"
}
```

---

## 15.14 Open Decisions for this Step

| # | Question | Options |
|---|----------|---------|
| ⚠️ 77 | **Language file format** | Barq dict `.bq` (current) / JSON / YAML |
| ⚠️ 78 | **URL prefix for locales** | `/ur/users` optional / always / never |
| ⚠️ 79 | **Urdu numeral rendering** | Optional per call / locale config / always LTR numerals |
| ⚠️ 80 | **RTL CSS handling** | Auto-generate RTL stylesheet / manual class toggling |

---

*Next: [Packages — Barq Auth →](https://github.com/barq-lang/barq-auth/blob/main/docs/barq-auth.md)*
