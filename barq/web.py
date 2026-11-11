"""Standard library, batch 3 — the web layer (v0.6.0):
Route (router + middleware), Cookie, Session, Http.abort, Request.make and
TestClient (sends requests to the router without a network).

Http.serve calls finish_request() after every handler, which saves the
session and turns the response's cookies into Set-Cookie headers.
"""

import hmac
import json
import os
import re
import secrets
import time
import urllib.parse

from . import hooks
from .builtins import need
from .runtime import BarqClass, BarqFunction, BarqObject, BarqThrow, BoundMethod, NativeFunction, PropInfo, \
    to_display, type_name
from .stdlib import arg, to_json_value
from .stdlib2 import (STATUS_TEXT, RequestState, check_headers, flat_query, header_lookup, make_response,
                      native_class, new_native, resp_json, to_response)


# ===========================================================================
# helpers
# ===========================================================================

def fn_params(f):
    """Parameters of a Barq function (None for native functions)."""
    if isinstance(f, BoundMethod):
        f = f.func
    if isinstance(f, BarqFunction):
        return f.params
    return None


def is_callable(v):
    return isinstance(v, (BarqFunction, NativeFunction, BoundMethod))


def wants_json(req):
    accept = header_lookup(req.fields["headers"], "Accept") or ""
    ctype = header_lookup(req.fields["headers"], "Content-Type") or ""
    return "json" in accept or "json" in ctype


def error_response(i, req, status, message, n, extra=None):
    message = message or STATUS_TEXT.get(status, "Error")
    if wants_json(req):
        data = {"message": message}
        data.update(extra or {})
        return resp_json(i, [data, status], n)
    return make_response(i, message, status, {"Content-Type": "text/plain; charset=utf-8"})


# ===========================================================================
# Cookie
# ===========================================================================

COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
COOKIE_SAFE = "!#$&'()*+-./:<=>?@[]^_`{|}~"
COOKIE_OPTIONS = ("maxAge", "expires", "path", "domain", "secure", "httpOnly", "sameSite")


def cookie_header(i, name, value, opts, n, fname):
    if not isinstance(name, str) or not COOKIE_NAME.match(name):
        i.throw("ValueException", f"{fname}(): invalid cookie name {to_display(name, i, True)}", n)
    if not isinstance(value, str):
        i.throw("TypeException", f"{fname}(): cookie value must be a string, got {type_name(value)}", n)
    for k in opts:
        if k not in COOKIE_OPTIONS:
            i.throw("ValueException", f'{fname}(): unknown cookie option "{k}" (use {", ".join(COOKIE_OPTIONS)})', n)
    parts = [f"{name}={urllib.parse.quote(value, safe=COOKIE_SAFE)}"]
    max_age = opts.get("maxAge")
    if max_age is not None:
        need(i, max_age, ("int",), fname + " option maxAge", 1, n)
        parts.append(f"Max-Age={max_age}")
        expires = int(time.time()) + max_age if max_age > 0 else 0
        parts.append("Expires=" + time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(expires)))
    elif opts.get("expires") is not None:
        ts = need(i, opts["expires"], ("int",), fname + " option expires", 1, n)
        parts.append("Expires=" + time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(ts)))
    path = need(i, opts.get("path", "/"), ("string",), fname + " option path", 1, n)
    domain = opts.get("domain")
    for label, v in (("path", path), ("domain", domain)):
        if isinstance(v, str) and re.search(r"[;\r\n]", v):
            i.throw("ValueException", f"{fname}(): cookie {label} contains an invalid character", n)
    parts.append(f"Path={path}")
    if domain is not None:
        parts.append("Domain=" + need(i, domain, ("string",), fname + " option domain", 1, n))
    same = opts.get("sameSite", "Lax")
    if same is not None:
        same = need(i, same, ("string",), fname + " option sameSite", 1, n).capitalize()
        if same not in ("Lax", "Strict", "None"):
            i.throw("ValueException", f'{fname}(): sameSite must be "Lax", "Strict" or "None", got "{same}"', n)
    secure = opts.get("secure", same == "None")
    if need(i, secure, ("bool",), fname + " option secure", 1, n):
        parts.append("Secure")
    if need(i, opts.get("httpOnly", True), ("bool",), fname + " option httpOnly", 1, n):
        parts.append("HttpOnly")
    if same is not None:
        parts.append(f"SameSite={same}")
    return "; ".join(parts)


def parse_cookie_header(text):
    out = {}
    for part in (text or "").split(";"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        if k and k not in out:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] == '"':
                v = v[1:-1]
            out[k] = urllib.parse.unquote(v)
    return out


def cookie_make(i, a, n):
    return cookie_header(i, a[0], need(i, a[1], ("string",), "Cookie.make", 2, n),
                         arg(i, a, 2, ("dict",), "Cookie.make", n, {}), n, "Cookie.make")


def cookie_forget(i, a, n):
    opts = dict(arg(i, a, 1, ("dict",), "Cookie.forget", n, {}))
    opts["maxAge"] = 0
    return cookie_header(i, need(i, a[0], ("string",), "Cookie.forget", 1, n), "", opts, n, "Cookie.forget")


COOKIE = {
    "make": (cookie_make, 2, 3),
    "forget": (cookie_forget, 1, 2),
    "parse": (lambda i, a, n: parse_cookie_header(need(i, a[0], ("string",), "Cookie.parse", 1, n)), 1, 1),
}


def res_cookies(res):
    if res.native is None:
        res.native = {"cookies": []}
    return res.native["cookies"]


def resp_with_cookie(i, a, n, this):
    name = need(i, a[0], ("string",), "Response.withCookie", 1, n)
    value = need(i, a[1], ("string",), "Response.withCookie", 2, n)
    opts = arg(i, a, 2, ("dict",), "Response.withCookie", n, {})
    res_cookies(this).append(cookie_header(i, name, value, opts, n, "Response.withCookie"))
    return this


def resp_without_cookie(i, a, n, this):
    res_cookies(this).append(cookie_forget(i, a, n))
    return this


def req_cookies(req):
    st = req.native
    if st.cookies is None:
        st.cookies = parse_cookie_header(header_lookup(req.fields["headers"], "Cookie") or "")
    return st.cookies


# ===========================================================================
# Session
# ===========================================================================

SESSION_DEFAULTS = {"driver": "memory", "lifetime": 7200, "cookie": "barq_session", "path": "storage/sessions",
                    "secure": False, "sameSite": "Lax", "table": "sessions", "connection": None}
SESSION_DRIVERS = ("memory", "file", "database")
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class SessionState:
    def __init__(self, sid, data, is_new):
        self.id = sid
        self.data = data
        self.is_new = is_new
        self.old_id = None      # set by regenerate(): delete this one on save


def session_settings(i, n=None):
    cfg = getattr(i, "_session_cfg", None)
    if cfg is None:
        from .stdlib2 import config_lookup
        cfg = dict(SESSION_DEFAULTS)
        conf = config_lookup(i, "session")
        if isinstance(conf, dict):
            cfg.update({k: v for k, v in conf.items() if k in SESSION_DEFAULTS})
        env = os.environ.get("SESSION_DRIVER")
        if env and not isinstance(conf, dict):
            cfg["driver"] = env
        i._session_cfg = cfg
    return cfg


def session_config(i, a, n):
    opts = need(i, a[0], ("dict",), "Session.config", 1, n)
    cfg = dict(session_settings(i, n))
    for k, v in opts.items():
        if k not in SESSION_DEFAULTS:
            i.throw("ValueException", f'Session.config(): unknown option "{k}" (use {", ".join(SESSION_DEFAULTS)})', n)
        cfg[k] = v
    if cfg["driver"] not in SESSION_DRIVERS:
        i.throw("ValueException", f'Session.config(): driver must be "memory", "file" or "database", got '
                f'{to_display(cfg["driver"], i, True)}', n)
    need(i, cfg["lifetime"], ("int",), "Session.config option lifetime", 1, n)
    i._session_cfg = cfg
    return None


def session_file(i, cfg, sid):
    folder = cfg["path"] if os.path.isabs(cfg["path"]) else os.path.join(i.root, cfg["path"])
    return folder, os.path.join(folder, sid + ".json")


def store_read(i, cfg, sid):
    """-> data dict, or None when missing or expired."""
    if not SESSION_ID.match(sid or ""):
        return None
    now = time.time()
    if cfg["driver"] == "memory":
        mem = i.__dict__.setdefault("_session_mem", {})
        entry = mem.get(sid)
        if entry is None:
            return None
        text, last = entry
    elif cfg["driver"] == "database":
        row = db_session_row(i, cfg, sid)
        if row is None:
            return None
        text, last = row.get("payload") or "", row.get("last_activity") or 0
    else:
        _, path = session_file(i, cfg, sid)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            last = os.path.getmtime(path)
        except OSError:
            return None
    if now - last > cfg["lifetime"]:
        store_delete(i, cfg, sid)
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def store_write(i, cfg, sid, data, n):
    text = json.dumps(to_json_value(i, data, n), ensure_ascii=False)
    if cfg["driver"] == "memory":
        i.__dict__.setdefault("_session_mem", {})[sid] = (text, time.time())
        return
    if cfg["driver"] == "database":
        return db_session_write(i, cfg, sid, data, text, n)
    folder, path = session_file(i, cfg, sid)
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        i.throw("IOException", f"cannot save session in {folder}: {e.strerror or e}", n)


def store_delete(i, cfg, sid):
    if cfg["driver"] == "memory":
        i.__dict__.setdefault("_session_mem", {}).pop(sid, None)
        return
    if cfg["driver"] == "database":
        from .stdlib2 import QUERY_METHODS
        QUERY_METHODS["delete"][0](i, [], None, db_session_query(i, cfg, None, sid))
        return
    try:
        os.remove(session_file(i, cfg, sid)[1])
    except OSError:
        pass


def store_all(i, cfg, user_key=None):
    """Every live session: [(id, data, last activity)]. With user_key, the
    database driver only reads rows whose user_id matches."""
    now = time.time()
    out = []
    if cfg["driver"] == "memory":
        for sid, (text, last) in list(i.__dict__.setdefault("_session_mem", {}).items()):
            out.append((sid, text, last))
    elif cfg["driver"] == "database":
        from .stdlib2 import add_where, qb_get
        q = db_session_query(i, cfg, None)
        if user_key is not None:
            add_where(i, ["user_id", user_key], None, q, "AND", "Session")
        for row in qb_get(i, [], None, q):
            out.append((row["id"], row.get("payload") or "", row.get("last_activity") or 0))
    else:
        folder = session_file(i, cfg, "x")[0]
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                last = os.path.getmtime(path)
            except OSError:
                continue
            out.append((name[:-5], text, last))
    live = []
    for sid, text, last in out:
        if now - last > cfg["lifetime"]:
            store_delete(i, cfg, sid)
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict):
            live.append((sid, data, last))
    return live


# ---- the database driver: a sessions table (barq auth:install writes its migration)

def db_session_db(i, cfg, n):
    from . import hooks
    name = cfg.get("connection")
    return hooks.named_db(i, name, n) if name else hooks.default_db(i, n)


def table_columns(i, db, table, n):
    from .stdlib2 import db_run, db_state, qid
    cur = db_run(i, db_state(i, db, n), f"PRAGMA table_info({qid(i, table, n, 'table')})", [], n)
    return [r[1] for r in cur.fetchall()]


def db_session_query(i, cfg, n, sid=None):
    from .stdlib2 import add_where, new_query, qid
    db = db_session_db(i, cfg, n)
    table = cfg.get("table") or "sessions"
    if not table_columns(i, db, table, n):
        i.throw("DbException", f'the "database" session driver needs the "{table}" table — run "barq auth:install" '
                'and "barq migrate" (or create it: t.string("id").primary(), t.int("user_id").nullable(), '
                't.string("ip_address", 45).nullable(), t.text("user_agent").nullable(), t.text("payload"), '
                't.int("last_activity"))', n)
    q = new_query(i, db, qid(i, table, n, "table"))
    if sid is not None:
        add_where(i, ["id", sid], n, q, "AND", "Session")
    return q


def db_session_row(i, cfg, sid):
    from .stdlib2 import qb_first
    return qb_first(i, [], None, db_session_query(i, cfg, None, sid))


def session_user_key(data):
    """The user id a session belongs to (the first guard that has one), for the user_id column."""
    for k, v in data.items():
        if k.startswith("_auth_") and isinstance(v, (int, str)):
            return v
    return None


def db_session_write(i, cfg, sid, data, text, n):
    from .stdlib2 import QUERY_METHODS, insert_rows
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    row = {"user_id": session_user_key(data), "ip_address": meta.get("ip"), "user_agent": meta.get("agent"),
           "payload": text, "last_activity": int(time.time())}
    if db_session_row(i, cfg, sid) is not None:
        QUERY_METHODS["update"][0](i, [row], n, db_session_query(i, cfg, n, sid))
    else:
        insert_rows(i, db_session_query(i, cfg, n), [dict(row, id=sid)], n, "Session")


def touch_meta(req, data):
    """Where a session is used from: shown by user.sessions() and session().browser()."""
    data["_meta"] = {"ip": req.native.ip, "agent": header_lookup(req.fields["headers"], "User-Agent") or "",
                     "last": int(time.time())}


def new_session_id():
    return secrets.token_urlsafe(32)


def start_session(i, req, n):
    st = req.native
    if st.session is not None:
        return st.session
    cfg = session_settings(i, n)
    sid = req_cookies(req).get(cfg["cookie"])
    data = store_read(i, cfg, sid) if sid else None
    is_new = data is None
    if is_new:
        sid, data = new_session_id(), {}
    # Flash data lives for exactly one more request.
    for key in data.get("_flash_old", []):
        data.pop(key, None)
    data["_flash_old"] = data.get("_flash_new", [])
    data["_flash_new"] = []
    touch_meta(req, data)
    st.session = new_native(i, "Session", SessionState(sid, data, is_new))
    return st.session


def save_session(i, session, n):
    """Persist the session, return the Set-Cookie header for it."""
    cfg = session_settings(i, n)
    s = session.native
    if s.old_id:
        store_delete(i, cfg, s.old_id)
        s.old_id = None
    store_write(i, cfg, s.id, s.data, n)
    return cookie_header(i, cfg["cookie"], s.id, {"maxAge": cfg["lifetime"], "secure": cfg["secure"],
                                                  "sameSite": cfg["sameSite"]}, n, "Session")


def finish_request(i, req, res, n):
    """After a handler: save the session; return every Set-Cookie header."""
    cookies = list(res.native["cookies"]) if isinstance(res.native, dict) else []
    if isinstance(req.native, RequestState):
        cookies.extend(req.native.queued)
        req.native.queued = []
        if req.native.session is not None:
            remember_page(req, res)
            touch_meta(req, sdata(req.native.session))
            cookies.append(save_session(i, req.native.session, n))
    return cookies


def remember_page(req, res):
    """Keep the last page a browser saw, for back() when there is no Referer."""
    if req.fields["method"].upper() != "GET" or wants_json(req) or res.fields.get("status") != 200:
        return
    ctype = header_lookup(res.fields.get("headers") or {}, "Content-Type") or ""
    if "html" in ctype:
        url = req.fields.get("url") or req.fields["path"]
        parts = urllib.parse.urlsplit(url)
        sdata(req.native.session)["_previous"] = parts.path + ("?" + parts.query if parts.query else "")


def sdata(this):
    return this.native.data


def public_keys(data):
    return {k: v for k, v in data.items() if not k.startswith("_")}


def check_key(i, key, n, fname):
    need(i, key, ("string",), fname, 1, n)
    if key.startswith("_"):
        i.throw("ValueException", f'{fname}(): session keys starting with "_" are reserved', n)
    return key


def s_put(fname):
    def fn(i, a, n, this):
        if isinstance(a[0], dict) and len(a) == 1:
            for k, v in a[0].items():
                sdata(this)[check_key(i, k, n, fname)] = v
            return None
        if len(a) != 2:
            i.throw("TypeException", f"{fname}() expects (key, value) or a dict", n)
        sdata(this)[check_key(i, a[0], n, fname)] = a[1]
        return None
    return fn


def s_get(i, a, n, this):
    return sdata(this).get(check_key(i, a[0], n, "Session.get"), a[1] if len(a) > 1 else None)


def s_pull(i, a, n, this):
    return sdata(this).pop(check_key(i, a[0], n, "Session.pull"), a[1] if len(a) > 1 else None)


def s_forget(i, a, n, this):
    keys = a[0] if isinstance(a[0], list) else [a[0]]
    for k in keys:
        sdata(this).pop(check_key(i, k, n, "Session.forget"), None)
    return None


def s_flash(i, a, n, this):
    key = check_key(i, a[0], n, "Session.flash")
    d = sdata(this)
    d[key] = a[1]
    if key not in d["_flash_new"]:
        d["_flash_new"].append(key)
    if key in d["_flash_old"]:
        d["_flash_old"].remove(key)
    return None


def s_reflash(i, a, n, this):
    d = sdata(this)
    d["_flash_new"] = list(dict.fromkeys(d["_flash_new"] + d["_flash_old"]))
    d["_flash_old"] = []
    return None


def s_flush(i, a, n, this):
    d = sdata(this)
    token, meta = d.get("_token"), d.get("_meta")
    d.clear()
    d.update({"_flash_old": [], "_flash_new": []})
    if token:
        d["_token"] = token
    if meta:
        d["_meta"] = meta
    return None


def s_regenerate(i, a, n, this):
    s = this.native
    if s.old_id is None and not s.is_new:
        s.old_id = s.id
    s.id = new_session_id()
    s.data.pop("_token", None)
    return None


def s_invalidate(i, a, n, this):
    s_flush(i, a, n, this)
    sdata(this).pop("_token", None)
    return s_regenerate(i, a, n, this)


def s_token(i, a, n, this):
    d = sdata(this)
    if not d.get("_token"):
        d["_token"] = secrets.token_hex(20)
    return d["_token"]


def s_increment(i, a, n, this):
    key = check_key(i, a[0], n, "Session.increment")
    by = arg(i, a, 1, ("int",), "Session.increment", n, 1)
    cur = sdata(this).get(key, 0)
    if type(cur) is not int:
        i.throw("TypeException", f'Session.increment(): "{key}" holds {type_name(cur)}, not int', n)
    sdata(this)[key] = cur + by
    return cur + by


def session_meta(this):
    m = sdata(this).get("_meta")
    return m if isinstance(m, dict) else {}


def parse_agent(agent):
    """-> (browser, platform, is_mobile) from a User-Agent header; good enough for a list of devices."""
    a = agent or ""
    browser = "Unknown"
    for name, pat in (("Edge", r"Edg(e|A|iOS)?/"), ("Opera", r"OPR/|Opera"), ("Samsung Internet", r"SamsungBrowser"),
                      ("Chrome", r"Chrome/|CriOS/"), ("Firefox", r"Firefox/|FxiOS/"), ("Safari", r"Safari/"),
                      ("curl", r"^curl/"), ("Barq", r"^Barq")):
        if re.search(pat, a):
            browser = name
            break
    platform = "Unknown"
    for name, pat in (("iOS", r"iPhone|iPad|iPod"), ("Android", r"Android"), ("Windows", r"Windows"),
                      ("ChromeOS", r"CrOS"), ("macOS", r"Macintosh|Mac OS X"), ("Linux", r"Linux|X11")):
        if re.search(pat, a):
            platform = name
            break
    mobile = bool(re.search(r"Mobi|iPhone|iPod|Android.*Mobile", a))
    return browser, platform, mobile


SESSION_METHODS = {
    "get": (s_get, 1, 2),
    "put": (s_put("Session.put"), 1, 2),
    "set": (s_put("Session.set"), 1, 2),
    "has": (lambda i, a, n, this: sdata(this).get(check_key(i, a[0], n, "Session.has")) is not None, 1, 1),
    "pull": (s_pull, 1, 2),
    "forget": (s_forget, 1, 1),
    "all": (lambda i, a, n, this: public_keys(sdata(this)), 0, 0),
    "flush": (s_flush, 0, 0),
    "flash": (s_flash, 2, 2),
    "reflash": (s_reflash, 0, 0),
    "increment": (s_increment, 1, 2),
    "regenerate": (s_regenerate, 0, 0),
    "invalidate": (s_invalidate, 0, 0),
    "token": (s_token, 0, 0),
    "id": (lambda i, a, n, this: this.native.id, 0, 0),
    "isNew": (lambda i, a, n, this: this.native.is_new, 0, 0),
    "ip": (lambda i, a, n, this: session_meta(this).get("ip"), 0, 0),
    "userAgent": (lambda i, a, n, this: session_meta(this).get("agent") or "", 0, 0),
    "browser": (lambda i, a, n, this: parse_agent(session_meta(this).get("agent"))[0], 0, 0),
    "platform": (lambda i, a, n, this: parse_agent(session_meta(this).get("agent"))[1], 0, 0),
    "isMobile": (lambda i, a, n, this: parse_agent(session_meta(this).get("agent"))[2], 0, 0),
    "lastActivity": (lambda i, a, n, this: session_meta(this).get("last"), 0, 0),
    "setIntendedUrl": (lambda i, a, n, this: sdata(this).__setitem__(INTENDED_KEY, need(
        i, a[0], ("string",), "Session.setIntendedUrl", 1, n)), 1, 1),
    "intendedUrl": (lambda i, a, n, this: sdata(this).get(INTENDED_KEY), 0, 0),
}

SESSION_STATICS = {
    "config": (session_config, 1, 1),
}


# ===========================================================================
# Request additions: Request.make, params, cookies, session
# ===========================================================================

REQUEST_MAKE_OPTIONS = ("headers", "body", "json", "form", "files", "cookies", "ip")


def build_request(i, method, url, opts, n, fname):
    method = method.upper()
    if not re.match(r"^[A-Z]+$", method):
        i.throw("ValueException", f"{fname}(): invalid HTTP method {method!r}", n)
    for k in opts:
        if k not in REQUEST_MAKE_OPTIONS:
            i.throw("ValueException", f'{fname}(): unknown option "{k}" (use {", ".join(REQUEST_MAKE_OPTIONS)})', n)
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    headers = check_headers(i, dict(opts.get("headers") or {}), n, fname)
    body, form, files = "", None, {}
    from .upload import encode_multipart, has_uploads, parse_multipart
    extra = opts.get("files")
    if extra is not None:
        need(i, extra, ("dict",), fname + " option files", 1, n)
    data = opts.get("form") if opts.get("form") is not None else opts.get("json")
    if extra or has_uploads(data):
        # files go as a real multipart body, parsed back the way the server does it
        fields = dict(need(i, data, ("dict",), fname + " option form", 1, n)) if data is not None else {}
        fields.update(extra or {})
        raw, ctype = encode_multipart(i, fields, n, fname)
        headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        headers["Content-Type"] = ctype
        form, files = parse_multipart(i, raw, ctype)
        body = raw.decode("utf-8", "replace")
    elif opts.get("form") is not None:
        data = need(i, opts["form"], ("dict",), fname + " option form", 1, n)
        form = {k: to_display(v) for k, v in data.items()}
        body = urllib.parse.urlencode(form)
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif opts.get("json") is not None:
        data = to_json_value(i, opts["json"], n)
        body = json.dumps(data, ensure_ascii=False)
        form = data if isinstance(data, dict) else None
        headers.setdefault("Content-Type", "application/json")
    elif opts.get("body") is not None:
        body = need(i, opts["body"], ("string",), fname + " option body", 1, n)
        ctype = header_lookup(headers, "Content-Type") or ""
        if "application/x-www-form-urlencoded" in ctype:
            form = flat_query(body)
        elif "json" in ctype and body.strip():
            try:
                form = json.loads(body)
            except ValueError:
                form = None
    cookies = opts.get("cookies")
    if cookies:
        need(i, cookies, ("dict",), fname + " option cookies", 1, n)
        jar = "; ".join(f"{k}={urllib.parse.quote(to_display(v), safe=COOKIE_SAFE)}" for k, v in cookies.items())
        old = header_lookup(headers, "Cookie")
        headers["Cookie"] = f"{old}; {jar}" if old else jar
    ip = need(i, opts.get("ip", "127.0.0.1"), ("string",), fname + " option ip", 1, n)
    full = path + ("?" + parts.query if parts.query else "")
    req = new_native(i, "Request", RequestState(flat_query(parts.query), form, ip),
                     method=method, path=path, url=full, body=body, headers=headers)
    req.native.files = files
    return req


def request_make(i, a, n):
    method = need(i, a[0], ("string",), "Request.make", 1, n)
    url = need(i, a[1], ("string",), "Request.make", 2, n)
    return build_request(i, method, url, arg(i, a, 2, ("dict",), "Request.make", n, {}), n, "Request.make")


def req_param(i, a, n, this):
    return this.native.params.get(need(i, a[0], ("string",), "Request.param", 1, n), a[1] if len(a) > 1 else None)


def req_route_name(i, a, n, this):
    r = this.native.route
    return r.name if r is not None else None


REQUEST_EXTRA = {
    "param": (req_param, 1, 2),
    "params": (lambda i, a, n, this: dict(this.native.params), 0, 0),
    "routeName": (req_route_name, 0, 0),
    "cookie": (lambda i, a, n, this: req_cookies(this).get(need(i, a[0], ("string",), "Request.cookie", 1, n),
                                                            a[1] if len(a) > 1 else None), 1, 2),
    "cookies": (lambda i, a, n, this: dict(req_cookies(this)), 0, 0),
    "session": (lambda i, a, n, this: start_session(i, this, n), 0, 0),
    "wantsJson": (lambda i, a, n, this: wants_json(this), 0, 0),
}

RESPONSE_EXTRA = {
    "withCookie": (resp_with_cookie, 2, 3),
    "withoutCookie": (resp_without_cookie, 1, 2),
    "cookies": (lambda i, a, n, this: list(res_cookies(this)), 0, 0),
}


# ===========================================================================
# Http.abort
# ===========================================================================

def http_abort(i, a, n):
    status = need(i, a[0], ("int",), "Http.abort", 1, n)
    if not 400 <= status <= 599:
        i.throw("ValueException", f"Http.abort() status must be 400-599, got {status}", n)
    message = arg(i, a, 1, ("string",), "Http.abort", n, STATUS_TEXT.get(status, "Error"))
    obj = i.make_exception("AbortException", message, n)
    obj.fields["status"] = status
    raise BarqThrow(obj)


# ===========================================================================
# Route — the router
# ===========================================================================

PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(\?)?\}")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
RESOURCE_ACTIONS = [  # action, methods, suffix
    ("index", ("GET",), ""),
    ("create", ("GET",), "/create"),
    ("store", ("POST",), ""),
    ("show", ("GET",), "/{id}"),
    ("edit", ("GET",), "/{id}/edit"),
    ("update", ("PUT", "PATCH"), "/{id}"),
    ("destroy", ("DELETE",), "/{id}"),
]


class RouteDef:
    def __init__(self, methods, path, handler, middleware, name_prefix):
        self.methods = methods
        self.path = path
        self.handler = handler
        self.middleware = list(middleware)
        self.name_prefix = name_prefix
        self.name = None
        self.wheres = {}
        self._regex = None

    def regex(self, i, n):
        if self._regex is None:
            out, pos = "", 0
            for m in PARAM.finditer(self.path):
                lit = self.path[pos:m.start()]
                name, optional = m.group(1), m.group(2)
                pat = self.wheres.get(name, "[^/]+")
                if optional and lit.endswith("/"):
                    out += re.escape(lit[:-1]) + f"(?:/(?P<{name}>{pat}))?"
                else:
                    out += re.escape(lit) + f"(?P<{name}>{pat})" + ("?" if optional else "")
                pos = m.end()
            out += re.escape(self.path[pos:])
            try:
                self._regex = re.compile("^" + out + "$")
            except re.error as e:
                i.throw("ValueException", f"route {self.path}: invalid where() pattern: {e}", n)
        return self._regex


class RouterState:
    def __init__(self):
        self.routes = []
        self.names = {}
        self.aliases = {}
        self.global_mw = []
        self.groups = []        # stack of (prefix, middleware list, name prefix)
        self.fallback = None


def router(i):
    st = getattr(i, "_router", None)
    if st is None:
        st = i._router = RouterState()
    return st


COLON_PARAM = re.compile(r"(?<=/):([A-Za-z_][A-Za-z0-9_]*)(\?)?(?=/|$)")


def join_path(*parts):
    """Join URL pieces; `:id` / `:id?` become `{id}` / `{id?}` (both forms work,
    and `:id` is handy inside "double quoted" strings, where {id} interpolates)."""
    path = "/" + "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return COLON_PARAM.sub(lambda m: "{" + m.group(1) + (m.group(2) or "") + "}", path)


def mw_list(i, v, n, fname):
    if v is None:
        return []
    items = v if isinstance(v, list) else [v]
    for m in items:
        if not (isinstance(m, (str, BarqClass)) or is_callable(m)):
            i.throw("TypeException", f"{fname}(): middleware must be a name, a class or a function, got {type_name(m)}", n)
    return items


def check_handler(i, h, n, fname):
    if is_callable(h):
        return h
    if isinstance(h, list) and len(h) == 2 and isinstance(h[0], BarqClass) and isinstance(h[1], str):
        cls, mname = h
        m = cls.find_method(mname)
        if m is None:
            i.throw("NameException", f"{fname}(): class {cls.name} has no method '{mname}'", n)
        if m.visibility != "public":
            i.throw("TypeException", f"{fname}(): {cls.name}.{mname}() is {m.visibility} — route handlers must be #[Public]", n)
        return h
    i.throw("TypeException", f"{fname}(): a route handler must be a function or #[ControllerClass, \"method\"], got {type_name(h)}", n)


def add_route(i, methods, path, handler, n, fname):
    st = router(i)
    prefix = join_path(*(g[0] for g in st.groups))
    mws = [m for g in st.groups for m in g[1]]
    names = "".join(g[2] for g in st.groups)
    r = RouteDef(set(methods), join_path(prefix, path), check_handler(i, handler, n, fname), mws, names)
    for m in PARAM.finditer(r.path):
        if r.path.count("{" + m.group(1)) > 1:
            i.throw("ValueException", f"{fname}(): route parameter {{{m.group(1)}}} appears twice in {r.path}", n)
    st.routes.append(r)
    return new_native(i, "Route", r)


def route_verb(*methods):
    fname = "Route." + (methods[0].lower() if len(methods) == 1 else "any")

    def fn(i, a, n):
        path = need(i, a[0], ("string",), fname, 1, n)
        return add_route(i, methods, path, a[1], n, fname)
    return fn


def route_match(i, a, n):
    methods = need(i, a[0], ("list",), "Route.match", 1, n)
    ms = []
    for m in methods:
        m = need(i, m, ("string",), "Route.match", 1, n).upper()
        if m not in HTTP_METHODS:
            i.throw("ValueException", f'Route.match(): unknown HTTP method "{m}"', n)
        ms.append(m)
    return add_route(i, ms, need(i, a[1], ("string",), "Route.match", 2, n), a[2], n, "Route.match")


def route_group(i, a, n):
    opts = need(i, a[0], ("dict",), "Route.group", 1, n)
    for k in opts:
        if k not in ("prefix", "middleware", "name"):
            i.throw("ValueException", f'Route.group(): unknown option "{k}" (use prefix, middleware, name)', n)
    prefix = need(i, opts.get("prefix", ""), ("string",), "Route.group option prefix", 1, n)
    name = need(i, opts.get("name", ""), ("string",), "Route.group option name", 1, n)
    mws = mw_list(i, opts.get("middleware"), n, "Route.group")
    fn = a[1]
    if not is_callable(fn):
        i.throw("TypeException", f"Route.group() needs a function as argument 2, got {type_name(fn)}", n)
    st = router(i)
    st.groups.append((prefix, mws, name))
    try:
        i.call_function(fn, [], n)
    finally:
        st.groups.pop()
    return None


def route_resource(i, a, n):
    name = need(i, a[0], ("string",), "Route.resource", 1, n).strip("/")
    cls = need(i, a[1], ("class",), "Route.resource", 2, n)
    opts = arg(i, a, 2, ("dict",), "Route.resource", n, {})
    only = opts.get("only")
    exclude = opts.get("except") or []
    added = []
    for action, methods, suffix in RESOURCE_ACTIONS:
        if only is not None and action not in only or action in exclude:
            continue
        m = cls.find_method(action)
        if m is None or m.visibility != "public":
            continue
        obj = add_route(i, methods, name + suffix, [cls, action], n, "Route.resource")
        r = obj.native
        r.name = r.name_prefix + name.replace("/", ".") + "." + action
        router(i).names[r.name] = r
        added.append(obj)
    if not added:
        i.throw("ValueException", f"Route.resource(): {cls.name} has none of the methods index, create, store, show, edit, update, destroy", n)
    return added


def route_alias(i, a, n):
    name = need(i, a[0], ("string",), "Route.alias", 1, n)
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", name):
        i.throw("ValueException", f'Route.alias(): invalid middleware name "{name}"', n)
    mw = a[1]
    if isinstance(mw, str) or not mw_list(i, mw, n, "Route.alias"):
        i.throw("TypeException", "Route.alias() needs a middleware class or function as argument 2", n)
    router(i).aliases[name] = mw
    return None


def route_global(i, a, n):
    router(i).global_mw.extend(mw_list(i, a[0], n, "Route.globalMiddleware"))
    return None


def route_fallback(i, a, n):
    router(i).fallback = check_handler(i, a[0], n, "Route.fallback")
    return None


def route_url(i, a, n):
    name = need(i, a[0], ("string",), "Route.url", 1, n)
    params = dict(arg(i, a, 1, ("dict",), "Route.url", n, {}))
    r = router(i).names.get(name)
    if r is None:
        i.throw("NameException", f'Route.url(): no route named "{name}"', n)

    def fill(m):
        key, optional = m.group(1), m.group(2)
        if key not in params or params[key] is None:
            if optional:
                return ""
            i.throw("ValueException", f'Route.url(): route "{name}" needs a value for {{{key}}}', n)
        return urllib.parse.quote(to_display(params.pop(key)), safe="")
    path = PARAM.sub(fill, r.path)
    path = re.sub(r"/+", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    if params:
        path += "?" + urllib.parse.urlencode({k: to_display(v) for k, v in params.items()})
    return path


def route_list(i, a, n):
    out = []
    for r in router(i).routes:
        h = r.handler
        if isinstance(h, list):
            action = f"{h[0].name}.{h[1]}"
        else:
            action = "function"
        out.append({"methods": sorted(r.methods, key=HTTP_METHODS.index), "path": r.path, "name": r.name,
                    "action": action, "middleware": [m if isinstance(m, str) else type_name(m) if not isinstance(m, BarqClass)
                                                      else m.name for m in r.middleware]})
    return out


def route_clear(i, a, n):
    i._router = RouterState()
    return None


# ---- the Route object returned by Route.get() & co ---------------------------

def rd(this):
    return this.native


def r_name(i, a, n, this):
    r = rd(this)
    r.name = r.name_prefix + need(i, a[0], ("string",), "Route.name", 1, n)
    router(i).names[r.name] = r
    return this


def r_middleware(i, a, n, this):
    rd(this).middleware.extend(mw_list(i, a[0], n, "Route.middleware"))
    return this


def r_where(i, a, n, this):
    r = rd(this)
    if isinstance(a[0], dict) and len(a) == 1:
        pairs = a[0].items()
    elif len(a) == 2:
        pairs = [(a[0], a[1])]
    else:
        i.throw("TypeException", "Route.where() expects (param, pattern) or a dict", n)
    for k, v in pairs:
        k = need(i, k, ("string",), "Route.where", 1, n)
        v = need(i, v, ("string",), "Route.where", 2, n)
        if "{" + k not in r.path:
            i.throw("ValueException", f"Route.where(): route {r.path} has no parameter {{{k}}}", n)
        try:
            re.compile(v)
        except re.error as e:
            i.throw("ValueException", f'Route.where(): invalid pattern "{v}": {e}', n)
        if "(" in v.replace("(?:", ""):
            i.throw("ValueException", f'Route.where(): use (?:...) instead of (...) in "{v}"', n)
        r.wheres[k] = v
    r._regex = None
    return this


def r_where_number(i, a, n, this):
    return r_where(i, [need(i, a[0], ("string",), "Route.whereNumber", 1, n), "[0-9]+"], n, this)


def r_prefix(i, a, n, this):
    r = rd(this)
    r.path = join_path(need(i, a[0], ("string",), "Route.prefix", 1, n), r.path)
    r._regex = None
    return this


ROUTE_METHODS = {
    "name": (r_name, 1, 1),
    "middleware": (r_middleware, 1, 1),
    "where": (r_where, 1, 2),
    "whereNumber": (r_where_number, 1, 1),
    "prefix": (r_prefix, 1, 1),
    "path": (lambda i, a, n, this: rd(this).path, 0, 0),
    "methods": (lambda i, a, n, this: sorted(rd(this).methods, key=HTTP_METHODS.index), 0, 0),
    "routeName": (lambda i, a, n, this: rd(this).name, 0, 0),
}


# ---- dispatching -------------------------------------------------------------

class NotFound(Exception):
    pass


def convert_param(i, raw, t, pname, n):
    tname = t.name if t is not None else "any"
    if raw is None:
        return None
    if tname == "int":
        if not re.match(r"^-?\d+$", raw):
            raise NotFound()
        return int(raw)
    if tname == "float":
        try:
            return float(raw)
        except ValueError:
            raise NotFound()
    if tname == "bool":
        if raw not in ("true", "false", "1", "0"):
            raise NotFound()
        return raw in ("true", "1")
    if tname in ("string", "any"):
        return raw
    i.throw("TypeException", f"route parameter '{pname}' must be int, float, bool, string or any — got {tname}", n)


class HttpResponseSignal(Exception):
    """Stop handling a request and send this response (FormRequest redirects, 403s)."""

    def __init__(self, response):
        super().__init__()
        self.response = response


def is_request_param(i, t):
    if t is None or t.name in ("any", "string", "int", "float", "bool"):
        return False
    cls = i.classes.get(t.name)
    req = i.classes["Request"]
    return cls is req or t.name == "Request" or (cls is not None and cls.is_subclass_of(req))


def call_with_injection(i, fn, req, n):
    params = fn_params(fn)
    if params is None:
        return i.call_function(fn, [req], n)
    route_params = dict(req.native.params)
    route = req.native.route
    declared = [m.group(1) for m in PARAM.finditer(route.path)] if route is not None else []
    args = []
    kwargs = None       # after a parameter left to its default, the rest go by name
    owner = fn.func.owner if isinstance(fn, BoundMethod) else None
    module = owner.module if owner is not None else getattr(fn.func if isinstance(fn, BoundMethod) else fn, "module", None)

    def give(p, value):
        if kwargs is None:
            args.append(value)
        else:
            kwargs[p.name] = value

    for p in params:
        if p.variadic:
            break
        cls = param_class(i, p.type, module)
        early = next((r for r in (h(i, cls, req, n) for h in hooks.PARAM_EARLY) if r), None) if cls else None
        if early:
            give(p, early[1])       # a FormRequest (barq-framework), built and validated
            continue
        if is_request_param(i, p.type) or (cls is not None and cls.is_subclass_of(i.classes["Request"])):
            give(p, req)
            continue
        if cls is not None and p.name not in route_params and p.name not in declared:
            if hooks.resolvable(i, cls):
                give(p, hooks.container_make(i, cls, n))      # services come from the container
                continue
        if p.name in route_params:
            raw = route_params.pop(p.name)
        elif route_params:
            raw = route_params.pop(next(iter(route_params)))
        elif p.default is not None:
            if kwargs is None:
                kwargs = {}
            continue            # `int page = 1` with no {page} in the route keeps its default
        elif p.name in declared:
            raw = None          # an optional {name?} that wasn't given
        else:
            where = f"route {route.path}" if route is not None else "this route"
            i.throw("TypeException", f"handler parameter '{p.name}' has no matching {{{p.name}}} in {where} "
                    "(handlers take Request and route parameters)", n)
        bound = next((r for r in (h(i, cls, raw, n) for h in hooks.PARAM_BINDERS) if r), None) if cls else None
        if bound:
            give(p, bound[1])       # {user} -> User.findOrFail(...) (barq-framework)
            continue
        give(p, convert_param(i, raw, p.type, p.name, n))
    return i.call_function(fn, args, n, kwargs or None)


BUILTIN_PARAM = {"any", "string", "int", "float", "bool", "list", "dict", "set", "null"}


def param_class(i, t, module):
    """The class a handler parameter's type names (imports and namespace included), or None."""
    if t is None or t.name in BUILTIN_PARAM:
        return None
    cls = i.classes.get(t.name)
    if cls is None and module is not None:
        try:
            cls = i.type_class(t, module)
        except BarqThrow:
            return None
    return cls if isinstance(cls, BarqClass) else None


def resolve_handler(i, h, n):
    if isinstance(h, list):
        cls, mname = h
        m = cls.find_method(mname)
        if m.is_static:
            return m
        return BoundMethod(hooks.container_make(i, cls, n), m)
    return h


BUILTIN_MIDDLEWARE = {}     # "auth", "guest", "can" — filled by barq-auth; Route.alias() wins


def resolve_middleware(i, mw, n):
    """-> (callable, extra string args)"""
    extra = []
    if isinstance(mw, str):
        name, _, rest = mw.partition(":")
        found = router(i).aliases.get(name, BUILTIN_MIDDLEWARE.get(name))
        if found is None:
            i.throw("NameException", f'unknown middleware "{name}" — register it with Route.alias("{name}", ...)', n)
        extra = rest.split(",") if rest else []
        mw = found
    if isinstance(mw, BarqClass):
        mw = hooks.container_make(i, mw, n)
    if isinstance(mw, BarqObject):
        m = mw.cls.find_method("handle")
        if m is None:
            i.throw("TypeException", f"middleware class {mw.cls.name} needs a handle(Request req, any next) method", n)
        mw = BoundMethod(mw, m)
    params = fn_params(mw)
    want = len(params) if params is not None else 2 + len(extra)
    if want != 2 + len(extra):
        if extra:
            i.throw("TypeException", f"middleware got {len(extra)} parameter(s) after ':' but its handler takes {want} arguments "
                    f"(Request, next, then one string per parameter)", n)
        i.throw("TypeException", f"a middleware function must take (Request req, any next), it takes {want} argument(s)", n)
    return mw, extra


def guarded(i, req, fn, n):
    """Run fn(); turn Http.abort() and validation failures into responses."""
    try:
        return to_response(i, fn(), n)
    except HttpResponseSignal as r:
        return r.response
    except NotFound:
        return error_response(i, req, 404, None, n)
    except BarqThrow as t:
        cls = t.obj.cls
        if cls.name == "AuthenticationException" and not wants_json(req):
            url = hooks.login_url(i, n)
            if url:
                from .stdlib2 import resp_redirect
                if req.fields["method"].upper() == "GET":
                    remember_intended(i, req, n)
                return resp_redirect(i, [url], n)
        if cls.is_subclass_of(i.classes["AbortException"]):
            return error_response(i, req, t.obj.fields.get("status", 500), t.obj.fields.get("message"), n)
        if cls.is_subclass_of(i.classes["ValidationException"]):
            return error_response(i, req, 422, t.obj.fields.get("message"), n,
                                  {"errors": t.obj.fields.get("errors") or {}})
        raise


def run_pipeline(i, req, mws, final, n):
    def step(k, request):
        if k == len(mws):
            return guarded(i, request, lambda: final(request), n)
        fn, extra = resolve_middleware(i, mws[k], n)

        def nxt(i2, a, n2):
            r = need(i, a[0], ("Request",), "next", 1, n2)
            return step(k + 1, r)
        next_fn = NativeFunction("next", nxt, 1, 1)
        return guarded(i, request, lambda: i.call_function(fn, [request, next_fn, *extra], n), n)
    return step(0, req)


def match_route(i, req, n):
    """-> (route, params) or (None, allowed methods)"""
    path = req.fields["path"]
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    method = req.fields["method"].upper()
    allowed = set()
    for r in router(i).routes:
        m = r.regex(i, n).match(path)
        if m is None:
            continue
        if method in r.methods or (method == "HEAD" and "GET" in r.methods):
            params = {k: urllib.parse.unquote(v) for k, v in m.groupdict().items() if v is not None}
            return r, params
        allowed |= r.methods
    return None, allowed


SPOOFABLE = ("PUT", "PATCH", "DELETE")


def spoof_method(req):
    """HTML forms can only POST: a _method field (or X-HTTP-Method-Override) picks PUT, PATCH or DELETE."""
    if req.fields["method"].upper() != "POST" or not isinstance(req.native, RequestState):
        return
    form = req.native.form
    want = form.get("_method") if isinstance(form, dict) else None
    want = want or header_lookup(req.fields["headers"], "X-HTTP-Method-Override")
    if isinstance(want, str) and want.upper() in SPOOFABLE:
        req.fields["method"] = want.upper()


def dispatch(i, req, n):
    st = router(i)
    spoof_method(req)

    def route_it(request):
        r, found = match_route(i, request, n)
        if r is None:
            if found:
                res = error_response(i, request, 405, None, n)
                res.fields["headers"]["Allow"] = ", ".join(sorted(found, key=HTTP_METHODS.index))
                return res
            if st.fallback is not None:
                return call_with_injection(i, resolve_handler(i, st.fallback, n), request, n)
            raise NotFound()
        request.native.params = found
        request.native.route = r
        handler = r.handler
        return run_pipeline(i, request, r.middleware,
                            lambda rq: call_with_injection(i, resolve_handler(i, handler, n), rq, n), n)
    with current(i, req):
        return run_pipeline(i, req, st.global_mw, route_it, n)


class current:
    """`with current(i, req):` — the request being handled (Auth.user() reads it)."""
    def __init__(self, i, req):
        self.stack = i.__dict__.setdefault("_requests", [])
        self.req = req

    def __enter__(self):
        self.stack.append(self.req)

    def __exit__(self, *exc):
        self.stack.pop()


def current_request(i):
    stack = i.__dict__.get("_requests")
    return stack[-1] if stack else None


def route_dispatch(i, a, n):
    return dispatch(i, need(i, a[0], ("Request",), "Route.dispatch", 1, n), n)


def route_serve(i, a, n):
    from .stdlib2 import http_serve
    if not router(i).routes and router(i).fallback is None:
        i.throw("ValueException", "Route.serve(): no routes are defined", n)
    handler = NativeFunction("Route.dispatch", lambda i2, a2, n2: dispatch(i, a2[0], n2), 1, 1)
    return http_serve(i, [handler] + list(a), n)


ROUTE_STATICS = {
    "get": (route_verb("GET"), 2, 2),
    "post": (route_verb("POST"), 2, 2),
    "put": (route_verb("PUT"), 2, 2),
    "patch": (route_verb("PATCH"), 2, 2),
    "delete": (route_verb("DELETE"), 2, 2),
    "options": (route_verb("OPTIONS"), 2, 2),
    "any": (route_verb(*HTTP_METHODS), 2, 2),
    "match": (route_match, 3, 3),
    "group": (route_group, 2, 2),
    "resource": (route_resource, 2, 3),
    "alias": (route_alias, 2, 2),
    "globalMiddleware": (route_global, 1, 1),
    "fallback": (route_fallback, 1, 1),
    "dispatch": (route_dispatch, 1, 1),
    "url": (route_url, 1, 2),
    "has": (lambda i, a, n: need(i, a[0], ("string",), "Route.has", 1, n) in router(i).names, 1, 1),
    "list": (route_list, 0, 0),
    "clear": (route_clear, 0, 0),
    "serve": (route_serve, 0, 1),
}


# ===========================================================================
# TestClient — requests to the router, no network
# ===========================================================================

class ClientState:
    def __init__(self):
        self.cookies = {}
        self.headers = {}
        self.acting = None      # (guard name, user) from actingAs()
        self.csrf = False       # withCsrfChecks(): run the "csrf" middleware like a browser request


def client_send(i, this, method, path, data, opts, n, fname):
    st = this.native
    opts = dict(opts)
    for k in opts:
        if k not in ("headers", "form", "json", "body", "files", "cookies"):
            i.throw("ValueException", f'{fname}(): unknown option "{k}" (use headers, form, json, body, files, '
                    'cookies)', n)
    headers = dict(st.headers)
    headers.update(need(i, opts.get("headers") or {}, ("dict",), fname + " option headers", 1, n))
    opts["headers"] = headers
    if data is not None:
        from .upload import has_uploads
        if isinstance(data, str):
            opts["body"] = data
        elif opts.pop("form", None) is True or has_uploads(data):
            opts["form"] = data
        else:
            opts["json"] = data
    elif opts.get("form") is True:
        opts.pop("form")
    cookies = dict(st.cookies)
    cookies.update(opts.get("cookies") or {})
    opts["cookies"] = cookies
    req = build_request(i, method, path, opts, n, fname)
    req.native.skip_csrf = not st.csrf
    if st.acting is not None:
        req.native.auth[st.acting[0]] = st.acting[1]
    res = dispatch(i, req, n)
    for header in finish_request(i, req, res, n):
        first = header.split(";")[0]
        name, _, value = first.partition("=")
        if "Max-Age=0" in header:
            st.cookies.pop(name, None)
        else:
            st.cookies[name] = urllib.parse.unquote(value)
    return res


def client_verb(method, has_body):
    fname = "TestClient." + method.lower()

    def fn(i, a, n, this):
        path = need(i, a[0], ("string",), fname, 1, n)
        if has_body:
            data = a[1] if len(a) > 1 else None
            opts = arg(i, a, 2, ("dict",), fname, n, {})
        else:
            data = None
            opts = arg(i, a, 1, ("dict",), fname, n, {})
        return client_send(i, this, method, path, data, opts, n, fname)
    return fn


def client_request(i, a, n, this):
    method = need(i, a[0], ("string",), "TestClient.request", 1, n)
    path = need(i, a[1], ("string",), "TestClient.request", 2, n)
    opts = dict(arg(i, a, 2, ("dict",), "TestClient.request", n, {}))
    return client_send(i, this, method, path, None, opts, n, "TestClient.request")


def client_with_headers(i, a, n, this):
    this.native.headers.update(check_headers(i, need(i, a[0], ("dict",), "TestClient.withHeaders", 1, n), n,
                                             "TestClient.withHeaders"))
    return this


TEST_CLIENT_METHODS = {
    "get": (client_verb("GET", False), 1, 2),
    "delete": (client_verb("DELETE", False), 1, 2),
    "post": (client_verb("POST", True), 1, 3),
    "put": (client_verb("PUT", True), 1, 3),
    "patch": (client_verb("PATCH", True), 1, 3),
    "request": (client_request, 2, 3),
    "withHeaders": (client_with_headers, 1, 1),
    "withCsrfChecks": (lambda i, a, n, this: setattr(this.native, "csrf", True) or this, 0, 0),
    "cookies": (lambda i, a, n, this: dict(this.native.cookies), 0, 0),
    "clearCookies": (lambda i, a, n, this: this.native.cookies.clear(), 0, 0),
}


def new_test_client(i):
    return new_native(i, "TestClient", ClientState())


# ===========================================================================
# forms (v0.14.0): CSRF, back()/redirect(), flash data, old(), the errors bag
# ===========================================================================

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def request_session(i, n, fname):
    req = current_request(i)
    if req is None:
        i.throw("ValueException", f"{fname}() only works while a request is being handled", n)
    return start_session(i, req, n)


def g_csrf_token(i, a, n):
    """The session's CSRF token ("" outside a request, e.g. a view rendered by a script)."""
    req = current_request(i)
    if req is None:
        return ""
    return s_token(i, [], n, start_session(i, req, n))


def g_csrf_field(i, a, n):
    return f'<input type="hidden" name="_token" value="{g_csrf_token(i, a, n)}">'


def g_method_field(i, a, n):
    m = need(i, a[0], ("string",), "method_field", 1, n).upper()
    if m not in SPOOFABLE:
        i.throw("ValueException", f'method_field() takes "PUT", "PATCH" or "DELETE", got "{a[0]}"', n)
    return f'<input type="hidden" name="_method" value="{m}">'


def mw_csrf(i, a, n):
    """Unsafe methods must send the session's token: form field _token or an X-CSRF-TOKEN header."""
    req, nxt = a[0], a[1]
    if req.fields["method"].upper() not in SAFE_METHODS and not req.native.skip_csrf:
        form = req.native.form
        sent = form.get("_token") if isinstance(form, dict) else None
        headers = req.fields["headers"]
        sent = sent or header_lookup(headers, "X-CSRF-TOKEN") or header_lookup(headers, "X-XSRF-TOKEN")
        with current(i, req):
            expected = sdata(start_session(i, req, n)).get("_token")
        if not isinstance(sent, str) or not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
            return error_response(i, req, 419, "CSRF token mismatch.", n)
    return i.call_function(nxt, [req], n)


INTENDED_KEY = "_url_intended"


def remember_intended(i, req, n):
    """The page a guest asked for, so intended() can send them there after logging in."""
    parts = urllib.parse.urlsplit(req.fields.get("url") or req.fields["path"])
    sdata(start_session(i, req, n))[INTENDED_KEY] = (parts.path or "/") + ("?" + parts.query if parts.query else "")


def pull_intended(i, fallback):
    req = current_request(i)
    if req is None:
        return fallback
    url = sdata(start_session(i, req, None)).pop(INTENDED_KEY, None)
    # only paths on this site: never an open redirect
    if isinstance(url, str) and url.startswith("/") and not url.startswith("//"):
        return url
    return fallback


def g_intended(i, a, n):
    """intended("/dashboard"): back to the page the "auth" middleware turned away, else the fallback."""
    from .stdlib2 import resp_redirect
    fallback = arg(i, a, 0, ("string",), "intended", n, "/")
    return resp_redirect(i, [pull_intended(i, fallback), arg(i, a, 1, ("int",), "intended", n, 302)], n)


def previous_url(i, fallback):
    req = current_request(i)
    if req is None:
        return fallback
    ref = header_lookup(req.fields["headers"], "Referer")
    if ref:
        return ref
    if req.native.session is not None or req_cookies(req).get(session_settings(i)["cookie"]):
        prev = sdata(start_session(i, req, None)).get("_previous")
        if prev:
            return prev
    return fallback


def g_back(i, a, n):
    from .stdlib2 import resp_redirect
    fallback = arg(i, a, 0, ("string",), "back", n, "/")
    return resp_redirect(i, [previous_url(i, fallback), arg(i, a, 1, ("int",), "back", n, 302)], n)


def g_redirect(i, a, n):
    from .stdlib2 import resp_redirect
    return resp_redirect(i, a, n)


def flash_pairs(i, a, n, fname):
    if len(a) == 1 and isinstance(a[0], dict):
        return list(a[0].items())
    if len(a) != 2:
        i.throw("TypeException", f"{fname}() expects (key, value) or a dict", n)
    return [(a[0], a[1])]


def resp_with(i, a, n, this):
    session = request_session(i, n, "Response.with")
    for k, v in flash_pairs(i, a, n, "Response.with"):
        s_flash(i, [k, v], n, session)
    return this


def error_dict(i, v, n, fname):
    """Validator, dict of field -> message(s), or one message -> {field: [messages]}."""
    if isinstance(v, BarqObject) and v.cls.name == "Validator":
        return {k: list(m) for k, m in v.native.errors.items()}
    if isinstance(v, str):
        return {"message": [v]}
    need(i, v, ("dict",), fname, 1, n)
    out = {}
    for k, m in v.items():
        msgs = m if isinstance(m, list) else [m]
        for x in msgs:
            need(i, x, ("string",), fname + " messages", 1, n)
        out[to_display(k)] = list(msgs)
    return out


def resp_with_errors(i, a, n, this):
    errors = error_dict(i, a[0], n, "Response.withErrors")
    if len(a) > 1 and isinstance(a[0], str):
        errors = {need(i, a[1], ("string",), "Response.withErrors", 2, n): [a[0]]}
    s_flash(i, ["errors", errors], n, request_session(i, n, "Response.withErrors"))
    return this


def safe_input(data):
    from .upload import safe_value
    return {k: v for k, v in data.items() if "password" not in str(k) and safe_value(v)}


def resp_with_input(i, a, n, this):
    from .stdlib2 import req_all
    session = request_session(i, n, "Response.withInput")
    data = need(i, a[0], ("dict",), "Response.withInput", 1, n) if a else req_all(i, [], n, current_request(i))
    s_flash(i, ["old", safe_input(data)], n, session)
    return this


def flashed(i, key):
    req = current_request(i)
    if req is None:
        return None
    if req.native.session is None and not req_cookies(req).get(session_settings(i)["cookie"]):
        return None
    return sdata(start_session(i, req, None)).get(key)


def g_old(i, a, n):
    old = flashed(i, "old")
    old = old if isinstance(old, dict) else {}
    if not a:
        return dict(old)
    key = need(i, a[0], ("string",), "old", 1, n)
    return old.get(key, a[1] if len(a) > 1 else None)


def g_session(i, a, n):
    session = request_session(i, n, "session")
    if not a:
        return session
    return s_get(i, a, n, session)


class ErrorBagState:
    def __init__(self, errors):
        self.errors = errors


def error_bag(i, errors=None):
    if errors is None:
        errors = flashed(i, "errors")
    return new_native(i, "ErrorBag", ErrorBagState(errors if isinstance(errors, dict) else {}))


def eb_field(i, a, n, fname):
    return need(i, a[0], ("string",), fname, 1, n) if a else None


def eb_has(i, a, n, this):
    f = eb_field(i, a, n, "ErrorBag.has")
    return bool(this.native.errors) if f is None else bool(this.native.errors.get(f))


def eb_first(i, a, n, this):
    f = eb_field(i, a, n, "ErrorBag.first")
    lists = [this.native.errors.get(f) or []] if f is not None else this.native.errors.values()
    for msgs in lists:
        if msgs:
            return msgs[0]
    return None


ERROR_BAG_METHODS = {
    "has": (eb_has, 0, 1),
    "any": (lambda i, a, n, this: bool(this.native.errors), 0, 0),
    "isEmpty": (lambda i, a, n, this: not this.native.errors, 0, 0),
    "first": (eb_first, 0, 1),
    "get": (lambda i, a, n, this: list(this.native.errors.get(eb_field(i, a, n, "ErrorBag.get")) or []), 1, 1),
    "all": (lambda i, a, n, this: [m for v in this.native.errors.values() for m in v], 0, 0),
    "messages": (lambda i, a, n, this: {k: list(v) for k, v in this.native.errors.items()}, 0, 0),
    "keys": (lambda i, a, n, this: list(this.native.errors), 0, 0),
    "count": (lambda i, a, n, this: sum(len(v) for v in this.native.errors.values()), 0, 0),
}


def validation_failed(i, req, data, errors, n):
    """Browser form: redirect back with the errors and input flashed. JSON: a 422 ValidationException."""
    if not wants_json(req):
        back = previous_url(i, None) if current_request(i) is req else header_lookup(req.fields["headers"], "Referer")
        if back:
            session = start_session(i, req, n)
            s_flash(i, ["errors", errors], n, session)
            s_flash(i, ["old", safe_input(data)], n, session)
            from .stdlib2 import resp_redirect
            raise HttpResponseSignal(resp_redirect(i, [back], n))
    first = next(iter(errors.values()))[0]
    exc = i.make_exception("ValidationException", first, n)
    exc.fields["errors"] = errors
    raise BarqThrow(exc)


def req_validate(i, a, n, this):
    from .stdlib2 import req_all, run_validation
    rules = need(i, a[0], ("dict",), "Request.validate", 1, n)
    messages = arg(i, a, 1, ("dict",), "Request.validate", n, {})
    data = req_all(i, [], n, this)
    errors, validated = run_validation(i, data, rules, messages, n)
    if errors:
        with current(i, this):
            validation_failed(i, this, data, errors, n)
    return validated


# ===========================================================================
# install
# ===========================================================================

def add_natives(i, cls_name, methods=None, statics=None):
    cls = i.classes[cls_name]
    for mname, (fn, lo, hi) in (methods or {}).items():
        f = NativeFunction(f"{cls_name}.{mname}", fn, lo, hi)
        f.owner = cls
        cls.methods[mname] = f
    for mname, (fn, lo, hi) in (statics or {}).items():
        cls.statics[mname] = PropInfo(mname, None, "public", None, cls, is_static=True, const=True)
        cls.static_values[mname] = NativeFunction(f"{cls_name}.{mname}", fn, lo, hi)


def install(i):
    from .stdlib import module
    cls = BarqClass("AbortException", "AbortException", None, parent=i.classes["Exception"], native=True)
    from .builtins import T
    cls.props["status"] = PropInfo("status", T("int"), "public", None, cls)
    i.classes["AbortException"] = cls
    i.builtins.declare("AbortException", cls, None, const=True)
    add_natives(i, "Request", REQUEST_EXTRA, {"make": (request_make, 2, 3)})
    add_natives(i, "Response", RESPONSE_EXTRA)
    native_class(i, "Route", None, ROUTE_METHODS, ROUTE_STATICS)
    native_class(i, "Session", None, SESSION_METHODS, SESSION_STATICS)
    native_class(i, "TestClient", None, TEST_CLIENT_METHODS,
                 construct=lambda i2, a, n, this: setattr(this, "native", ClientState()))
    i.builtins.vars["Http"][0].members["abort"] = NativeFunction("Http.abort", http_abort, 1, 2)
    i.builtins.declare("Cookie", module("Cookie", COOKIE), None, const=True)
    native_class(i, "ErrorBag", None, ERROR_BAG_METHODS)
    add_natives(i, "Request", {"validate": (req_validate, 1, 2)})
    add_natives(i, "Response", {"with": (resp_with, 1, 2), "withErrors": (resp_with_errors, 1, 2),
                                "withInput": (resp_with_input, 0, 1)},
                {"back": (g_back, 0, 2), "intended": (g_intended, 0, 2)})
    for name, fn, lo, hi in (("csrf_token", g_csrf_token, 0, 0), ("csrf_field", g_csrf_field, 0, 0),
                             ("intended", g_intended, 0, 2),
                             ("method_field", g_method_field, 1, 1), ("back", g_back, 0, 2),
                             ("redirect", g_redirect, 1, 2), ("old", g_old, 0, 2), ("session", g_session, 0, 2)):
        i.builtins.declare(name, NativeFunction(name, fn, lo, hi), None, const=True)
    BUILTIN_MIDDLEWARE["csrf"] = NativeFunction("csrf", mw_csrf, 2, -1)
