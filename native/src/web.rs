//! The web layer (barq/web.py): Route (router + middleware), Request,
//! Session (memory driver), Cookie, Http.abort and TestClient — requests go
//! straight to the router, no network.

use crate::code::{Builtin, RspOp};
use crate::compiler::native_value;
use crate::stdlib::{parse_regex, regex_escape, regex_search, Regex};
use crate::stdlib2::HttpRspState;
use crate::value::*;
use crate::vm::{Flow, Vm, R};
use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

// ===========================================================================
// values
// ===========================================================================

pub enum WebObj {
    Request(RefCell<ReqState>),
    Route(usize),
    Session(RefCell<SessState>),
    Client(RefCell<ClientState>),
    Next(NextState),
}

impl WebObj {
    pub fn type_name(&self) -> &'static str {
        match self {
            WebObj::Request(_) => "Request",
            WebObj::Route(_) => "Route",
            WebObj::Session(_) => "Session",
            WebObj::Client(_) => "TestClient",
            WebObj::Next(_) => "karo",
        }
    }
}

pub struct ReqState {
    pub method: String,
    pub path: String,
    pub url: String,
    pub body: String,
    pub headers: Vec<(String, String)>,
    pub query: Vec<(String, String)>,
    /// dict from an urlencoded / JSON body
    pub form: Option<Value>,
    pub ip: String,
    pub params: Vec<(String, String)>,
    pub route: Option<usize>,
    pub session: Option<Value>,
    pub cookies: Option<Vec<(String, String)>>,
}

pub struct SessState {
    pub id: String,
    pub data: Rc<RefCell<OMap>>,
    pub is_new: bool,
    pub old_id: Option<String>,
}

#[derive(Default)]
pub struct ClientState {
    pub cookies: Vec<(String, String)>,
    pub headers: Vec<(String, String)>,
}

#[derive(Clone)]
pub struct NextState {
    mws: Rc<Vec<Value>>,
    k: usize,
    stage: Stage,
}

#[derive(Clone, Copy)]
enum Stage {
    Global,
    Route(usize),
}

fn web(o: WebObj) -> Value {
    Value::Web(Rc::new(o))
}

// ===========================================================================
// small helpers
// ===========================================================================

fn key(k: &str) -> HKey {
    HKey::Str(Rc::new(BStr::new(k.to_string())))
}

fn dget(m: &OMap, k: &str) -> Option<Value> {
    m.get(&key(k)).cloned()
}

fn dput(m: &mut OMap, k: &str, v: Value) {
    m.insert(key(k), s(k), v);
}

fn drm(m: &mut OMap, k: &str) -> Option<Value> {
    m.remove(&key(k))
}

fn pairs_dict(p: &[(String, String)]) -> Value {
    let mut m = OMap::new(false);
    for (k, v) in p {
        dput(&mut m, k, s(v.clone()));
    }
    dict(m)
}

fn str_list(items: &[String]) -> Value {
    list(items.iter().map(|x| s(x.clone())).collect())
}

fn lookup(pairs: &[(String, String)], name: &str) -> Option<String> {
    let low = name.to_ascii_lowercase();
    pairs.iter().find(|(k, _)| k.to_ascii_lowercase() == low).map(|(_, v)| v.clone())
}

fn set_pair(pairs: &mut Vec<(String, String)>, k: &str, v: String) {
    match pairs.iter_mut().find(|(x, _)| x == k) {
        Some(p) => p.1 = v,
        None => pairs.push((k.to_string(), v)),
    }
}

fn need_str(vm: &mut Vm, a: &[Value], i: usize, fname: &str) -> R<String> {
    match a.get(i) {
        Some(Value::Str(t)) => Ok(t.s.clone()),
        v => {
            let tn = v.map(type_name).unwrap_or_else(|| "null".into());
            vm.fail("TypeException", format!("{}() argument {} must be string, got {}", fname, i + 1, tn))
        }
    }
}

fn need_int(vm: &mut Vm, v: &Value, fname: &str, pos: usize) -> R<i64> {
    match v {
        Value::Int(n) => Ok(*n),
        _ => vm.fail("TypeException", format!("{}() argument {} must be int, got {}", fname, pos, type_name(v))),
    }
}

fn need_bool(vm: &mut Vm, v: &Value, fname: &str, pos: usize) -> R<bool> {
    match v {
        Value::Bool(b) => Ok(*b),
        _ => vm.fail("TypeException", format!("{}() argument {} must be bool, got {}", fname, pos, type_name(v))),
    }
}

fn str_of(vm: &mut Vm, v: &Value, fname: &str, pos: usize) -> R<String> {
    match v {
        Value::Str(t) => Ok(t.s.clone()),
        _ => vm.fail("TypeException", format!("{}() argument {} must be string, got {}", fname, pos, type_name(v))),
    }
}

/// Optional dict argument (missing or null: empty).
fn opt_dict(vm: &mut Vm, a: &[Value], i: usize, fname: &str) -> R<OMap> {
    match a.get(i) {
        None | Some(Value::Null) => Ok(OMap::new(false)),
        Some(Value::Dict(d)) => Ok(d.borrow().clone()),
        Some(v) => vm.fail("TypeException", format!("{}() argument {} must be dict, got {}", fname, i + 1, type_name(v))),
    }
}

fn dict_of(vm: &mut Vm, v: &Value, fname: &str, pos: usize) -> R<OMap> {
    match v {
        Value::Dict(d) => Ok(d.borrow().clone()),
        _ => vm.fail("TypeException", format!("{}() argument {} must be dict, got {}", fname, pos, type_name(v))),
    }
}

fn str_keys(vm: &mut Vm, m: &OMap) -> R<Vec<(String, Value)>> {
    let mut out = Vec::new();
    for (k, v) in m.iter() {
        let ks = match k {
            Value::Str(t) => t.s.clone(),
            _ => vm.display(k, false)?,
        };
        out.push((ks, v.clone()));
    }
    Ok(out)
}

fn check_headers(vm: &mut Vm, m: &OMap, fname: &str) -> R<Vec<(String, String)>> {
    let mut out = Vec::new();
    for (k, v) in m.iter() {
        match (k, v) {
            (Value::Str(a), Value::Str(b)) => {
                if a.s.contains(['\n', '\r']) || b.s.contains(['\n', '\r']) {
                    return vm.fail("ValueException", format!("{}(): header '{}' contains a line break", fname, a.s));
                }
                out.push((a.s.clone(), b.s.clone()));
            }
            _ => return vm.fail("TypeException", format!("{}(): headers must be dict<string, string>", fname)),
        }
    }
    Ok(out)
}

fn now_secs() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn status_text(code: i64) -> &'static str {
    match code {
        100 => "Continue",
        101 => "Switching Protocols",
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        204 => "No Content",
        301 => "Moved Permanently",
        302 => "Found",
        303 => "See Other",
        304 => "Not Modified",
        307 => "Temporary Redirect",
        308 => "Permanent Redirect",
        400 => "Bad Request",
        401 => "Unauthorized",
        402 => "Payment Required",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        406 => "Not Acceptable",
        408 => "Request Timeout",
        409 => "Conflict",
        410 => "Gone",
        411 => "Length Required",
        412 => "Precondition Failed",
        413 => "Request Entity Too Large",
        414 => "Request-URI Too Long",
        415 => "Unsupported Media Type",
        416 => "Requested Range Not Satisfiable",
        417 => "Expectation Failed",
        418 => "I'm a Teapot",
        421 => "Misdirected Request",
        422 => "Unprocessable Entity",
        423 => "Locked",
        424 => "Failed Dependency",
        425 => "Too Early",
        426 => "Upgrade Required",
        428 => "Precondition Required",
        429 => "Too Many Requests",
        431 => "Request Header Fields Too Large",
        451 => "Unavailable For Legal Reasons",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        505 => "HTTP Version Not Supported",
        _ => "Error",
    }
}

// ---- URL encoding (urllib.parse) --------------------------------------------

fn quote(text: &str, safe: &str, plus: bool) -> String {
    let mut out = String::new();
    for b in text.bytes() {
        let c = b as char;
        if c.is_ascii_alphanumeric() || "_.-~".contains(c) || (b < 128 && safe.contains(c)) {
            out.push(c);
        } else if plus && c == ' ' {
            out.push('+');
        } else {
            out.push_str(&format!("%{:02X}", b));
        }
    }
    out
}

fn unquote(text: &str, plus: bool) -> String {
    let b = text.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            let h = std::str::from_utf8(&b[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok());
            if let Some(v) = h {
                out.push(v);
                i += 3;
                continue;
            }
        }
        if plus && b[i] == b'+' {
            out.push(b' ');
        } else {
            out.push(b[i]);
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// urllib.parse.parse_qs(keep_blank_values=True), last value wins
fn flat_query(qs: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for part in qs.split('&') {
        if part.is_empty() {
            continue;
        }
        let (k, v) = match part.split_once('=') {
            Some((k, v)) => (k, v),
            None => (part, ""),
        };
        let k = unquote(k, true);
        let v = unquote(v, true);
        set_pair(&mut out, &k, v);
    }
    out
}

fn urlencode(pairs: &[(String, String)]) -> String {
    pairs.iter().map(|(k, v)| format!("{}={}", quote(k, "", true), quote(v, "", true))).collect::<Vec<_>>().join("&")
}

/// -> (path, query) of a URL, like urlsplit
fn split_url(url: &str) -> (String, String) {
    let url = url.split('#').next().unwrap_or("");
    let mut rest = url;
    if let Some(p) = url.find("://") {
        let after = &url[p + 3..];
        rest = match after.find(['/', '?']) {
            Some(i) => &after[i..],
            None => "",
        };
    }
    let (path, query) = match rest.split_once('?') {
        Some((a, b)) => (a, b),
        None => (rest, ""),
    };
    let mut path = if path.is_empty() { "/".to_string() } else { path.to_string() };
    if !path.starts_with('/') {
        path.insert(0, '/');
    }
    (path, query.to_string())
}

fn http_date(ts: i64) -> String {
    let days = ts.div_euclid(86400);
    let secs = ts.rem_euclid(86400);
    // Hinnant's civil_from_days
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    let wd = (days + 4).rem_euclid(7); // 1970-01-01 was a Thursday
    const WD: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    const MON: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    format!("{}, {:02} {} {} {:02}:{:02}:{:02} GMT", WD[wd as usize], d, MON[(m - 1) as usize], y,
            secs / 3600, secs / 60 % 60, secs % 60)
}

// ===========================================================================
// Cookie
// ===========================================================================

const COOKIE_SAFE: &str = "!#$&'()*+-./:<=>?@[]^_`{|}~";
const COOKIE_OPTIONS: [&str; 7] = ["maxAge", "expires", "path", "domain", "secure", "httpOnly", "sameSite"];

fn valid_cookie_name(n: &str) -> bool {
    !n.is_empty() && n.chars().all(|c| c.is_ascii_alphanumeric() || "!#$%&'*+-.^_`|~".contains(c))
}

fn cookie_header(vm: &mut Vm, name: &Value, value: &Value, opts: &OMap, fname: &str) -> R<String> {
    let name = match name {
        Value::Str(t) if valid_cookie_name(&t.s) => t.s.clone(),
        v => {
            let shown = vm.display(v, true)?;
            return vm.fail("ValueException", format!("{}(): invalid cookie name {}", fname, shown));
        }
    };
    let value = match value {
        Value::Str(t) => t.s.clone(),
        v => return vm.fail("TypeException", format!("{}(): cookie value must be a string, got {}", fname, type_name(v))),
    };
    let opts = str_keys(vm, opts)?;
    for (k, _) in &opts {
        if !COOKIE_OPTIONS.contains(&k.as_str()) {
            return vm.fail("ValueException",
                format!("{}(): unknown cookie option \"{}\" (use {})", fname, k, COOKIE_OPTIONS.join(", ")));
        }
    }
    let get = |k: &str| opts.iter().find(|(x, _)| x == k).map(|(_, v)| v.clone());
    let mut parts = vec![format!("{}={}", name, quote(&value, COOKIE_SAFE, false))];
    let oname = format!("{} option", fname);
    match get("maxAge") {
        Some(v) if !matches!(v, Value::Null) => {
            let max_age = need_int(vm, &v, &format!("{} maxAge", oname), 1)?;
            parts.push(format!("Max-Age={}", max_age));
            let exp = if max_age > 0 { now_secs() as i64 + max_age } else { 0 };
            parts.push(format!("Expires={}", http_date(exp)));
        }
        _ => {
            if let Some(v) = get("expires") {
                if !matches!(v, Value::Null) {
                    let ts = need_int(vm, &v, &format!("{} expires", oname), 1)?;
                    parts.push(format!("Expires={}", http_date(ts)));
                }
            }
        }
    }
    let path = match get("path") {
        None => "/".to_string(),
        Some(v) => str_of(vm, &v, &format!("{} path", oname), 1)?,
    };
    let domain = match get("domain") {
        None | Some(Value::Null) => None,
        Some(v) => Some(str_of(vm, &v, &format!("{} domain", oname), 1)?),
    };
    for (label, v) in [("path", Some(path.clone())), ("domain", domain.clone())] {
        if let Some(v) = v {
            if v.contains([';', '\r', '\n']) {
                return vm.fail("ValueException", format!("{}(): cookie {} contains an invalid character", fname, label));
            }
        }
    }
    parts.push(format!("Path={}", path));
    if let Some(d) = &domain {
        parts.push(format!("Domain={}", d));
    }
    let same = match get("sameSite") {
        None => Some("Lax".to_string()),
        Some(Value::Null) => None,
        Some(v) => {
            let t = str_of(vm, &v, &format!("{} sameSite", oname), 1)?;
            let mut c = t.chars();
            let cap = match c.next() {
                Some(f) => f.to_uppercase().collect::<String>() + &c.as_str().to_lowercase(),
                None => String::new(),
            };
            if !["Lax", "Strict", "None"].contains(&cap.as_str()) {
                return vm.fail("ValueException",
                    format!("{}(): sameSite must be \"Lax\", \"Strict\" or \"None\", got \"{}\"", fname, cap));
            }
            Some(cap)
        }
    };
    let secure = match get("secure") {
        None => same.as_deref() == Some("None"),
        Some(v) => need_bool(vm, &v, &format!("{} secure", oname), 1)?,
    };
    if secure {
        parts.push("Secure".into());
    }
    let http_only = match get("httpOnly") {
        None => true,
        Some(v) => need_bool(vm, &v, &format!("{} httpOnly", oname), 1)?,
    };
    if http_only {
        parts.push("HttpOnly".into());
    }
    if let Some(sm) = same {
        parts.push(format!("SameSite={}", sm));
    }
    Ok(parts.join("; "))
}

fn parse_cookie_header(text: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for part in text.split(';') {
        let Some((k, v)) = part.split_once('=') else { continue };
        let k = k.trim();
        if k.is_empty() || out.iter().any(|(x, _)| x == k) {
            continue;
        }
        let mut v = v.trim();
        if v.len() >= 2 && v.starts_with('"') && v.ends_with('"') {
            v = &v[1..v.len() - 1];
        }
        out.push((k.to_string(), unquote(v, false)));
    }
    out
}

fn cookie_forget(vm: &mut Vm, a: &[Value], fname: &str) -> R<String> {
    let name = need_str(vm, a, 0, fname)?;
    let mut opts = opt_dict(vm, a, 1, fname)?;
    dput(&mut opts, "maxAge", Value::Int(0));
    cookie_header(vm, &s(name), &s(""), &opts, fname)
}

fn req_cookies(r: &WebObj) -> Vec<(String, String)> {
    let WebObj::Request(st) = r else { return Vec::new() };
    let mut st = st.borrow_mut();
    if st.cookies.is_none() {
        let h = lookup(&st.headers, "Cookie").unwrap_or_default();
        st.cookies = Some(parse_cookie_header(&h));
    }
    st.cookies.clone().unwrap()
}

// ===========================================================================
// Session (memory driver)
// ===========================================================================

struct SessCfg {
    driver: String,
    lifetime: i64,
    cookie: String,
    secure: bool,
    same_site: Value,
}

thread_local! {
    static SESS_CFG: RefCell<Option<Rc<SessCfg>>> = const { RefCell::new(None) };
    static SESS_MEM: RefCell<HashMap<String, (String, f64)>> = RefCell::new(HashMap::new());
    static ROUTER: RefCell<RouterState> = RefCell::new(RouterState::default());
}

const SESSION_KEYS: [&str; 8] = ["driver", "lifetime", "cookie", "path", "secure", "sameSite", "table", "connection"];

fn session_settings(vm: &mut Vm) -> R<Rc<SessCfg>> {
    if let Some(c) = SESS_CFG.with(|c| c.borrow().clone()) {
        return Ok(c);
    }
    let mut cfg = SessCfg { driver: "memory".into(), lifetime: 7200, cookie: "barq_session".into(), secure: false,
                            same_site: s("Lax") };
    let (found, conf) = crate::stdlib2::config_lookup("session");
    if found {
        if let Value::Dict(d) = &conf {
            let m = d.borrow().clone();
            apply_session_opts(vm, &mut cfg, &m, "Session.config", false)?;
        }
    } else if let Ok(env) = std::env::var("SESSION_DRIVER") {
        if !env.is_empty() {
            cfg.driver = env;
        }
    }
    let rc = Rc::new(cfg);
    SESS_CFG.with(|c| *c.borrow_mut() = Some(rc.clone()));
    Ok(rc)
}

fn apply_session_opts(vm: &mut Vm, cfg: &mut SessCfg, m: &OMap, fname: &str, strict: bool) -> R<()> {
    for (k, v) in str_keys(vm, m)? {
        match k.as_str() {
            "driver" => cfg.driver = vm.display(&v, false)?,
            "lifetime" => cfg.lifetime = need_int(vm, &v, "Session.config option lifetime", 1)?,
            "cookie" => cfg.cookie = vm.display(&v, false)?,
            "secure" => cfg.secure = matches!(v, Value::Bool(true)),
            "sameSite" => cfg.same_site = v,
            "path" | "table" | "connection" => {}
            _ if strict => {
                return vm.fail("ValueException",
                    format!("{}(): unknown option \"{}\" (use {})", fname, k, SESSION_KEYS.join(", ")));
            }
            _ => {}
        }
    }
    if !["memory", "file", "database"].contains(&cfg.driver.as_str()) {
        let shown = vm.display(&s(cfg.driver.clone()), true)?;
        return vm.fail("ValueException",
            format!("Session.config(): driver must be \"memory\", \"file\" or \"database\", got {}", shown));
    }
    Ok(())
}

fn check_driver(vm: &mut Vm, cfg: &SessCfg) -> R<()> {
    if cfg.driver != "memory" {
        return vm.fail("NativeError",
            format!("the \"{}\" session driver is not supported by the native VM yet", cfg.driver));
    }
    Ok(())
}

fn valid_session_id(sid: &str) -> bool {
    (32..=128).contains(&sid.len()) && sid.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn store_read(vm: &mut Vm, cfg: &SessCfg, sid: &str) -> R<Option<OMap>> {
    if !valid_session_id(sid) {
        return Ok(None);
    }
    check_driver(vm, cfg)?;
    let Some((text, last)) = SESS_MEM.with(|m| m.borrow().get(sid).cloned()) else { return Ok(None) };
    if now_secs() - last > cfg.lifetime as f64 {
        SESS_MEM.with(|m| m.borrow_mut().remove(sid));
        return Ok(None);
    }
    match crate::json::parse(&text) {
        Ok(j) => match crate::builtins::json_decode_j(&j) {
            Value::Dict(d) => Ok(Some(d.borrow().clone())),
            _ => Ok(None),
        },
        Err(_) => Ok(None),
    }
}

fn store_write(vm: &mut Vm, cfg: &SessCfg, sid: &str, data: &Rc<RefCell<OMap>>) -> R<()> {
    check_driver(vm, cfg)?;
    let text = crate::builtins::json_encode_value(vm, &Value::Dict(data.clone()), None, 0)?;
    SESS_MEM.with(|m| m.borrow_mut().insert(sid.to_string(), (text, now_secs())));
    Ok(())
}

fn store_delete(sid: &str) {
    SESS_MEM.with(|m| m.borrow_mut().remove(sid));
}

fn new_session_id() -> String {
    crate::stdlib::b64url_encode(&crate::stdlib::random_bytes(32))
}

fn touch_meta(req: &ReqState, data: &mut OMap) {
    let mut meta = OMap::new(false);
    dput(&mut meta, "ip", s(req.ip.clone()));
    dput(&mut meta, "agent", s(lookup(&req.headers, "User-Agent").unwrap_or_default()));
    dput(&mut meta, "last", Value::Int(now_secs() as i64));
    dput(data, "_meta", dict(meta));
}

fn str_items(v: Option<Value>) -> Vec<String> {
    match v {
        Some(Value::List(l)) => l.borrow().iter().map(crate::builtins::plain_display).collect(),
        _ => Vec::new(),
    }
}

fn start_session(vm: &mut Vm, req: &Rc<WebObj>) -> R<Value> {
    let WebObj::Request(st) = &**req else { unreachable!() };
    if let Some(sv) = st.borrow().session.clone() {
        return Ok(sv);
    }
    let cfg = session_settings(vm)?;
    let sid = req_cookies(req).into_iter().find(|(k, _)| *k == cfg.cookie).map(|(_, v)| v);
    let data = match &sid {
        Some(sid) => store_read(vm, &cfg, sid)?,
        None => None,
    };
    let is_new = data.is_none();
    let (sid, mut data) = match data {
        Some(d) => (sid.unwrap(), d),
        None => (new_session_id(), OMap::new(false)),
    };
    // flash data lives for exactly one more request
    for k in str_items(dget(&data, "_flash_old")) {
        drm(&mut data, &k);
    }
    let new = dget(&data, "_flash_new").unwrap_or_else(|| list(vec![]));
    dput(&mut data, "_flash_old", new);
    dput(&mut data, "_flash_new", list(vec![]));
    touch_meta(&st.borrow(), &mut data);
    let sv = web(WebObj::Session(RefCell::new(SessState { id: sid, data: Rc::new(RefCell::new(data)), is_new, old_id: None })));
    st.borrow_mut().session = Some(sv.clone());
    Ok(sv)
}

fn save_session(vm: &mut Vm, sess: &Value) -> R<String> {
    let cfg = session_settings(vm)?;
    let Value::Web(w) = sess else { unreachable!() };
    let WebObj::Session(ss) = &**w else { unreachable!() };
    let (id, data, old) = {
        let mut b = ss.borrow_mut();
        (b.id.clone(), b.data.clone(), b.old_id.take())
    };
    if let Some(old) = old {
        store_delete(&old);
    }
    store_write(vm, &cfg, &id, &data)?;
    let mut opts = OMap::new(false);
    dput(&mut opts, "maxAge", Value::Int(cfg.lifetime));
    dput(&mut opts, "secure", Value::Bool(cfg.secure));
    dput(&mut opts, "sameSite", cfg.same_site.clone());
    cookie_header(vm, &s(cfg.cookie.clone()), &s(id), &opts, "Session")
}

fn wants_json(st: &ReqState) -> bool {
    let a = lookup(&st.headers, "Accept").unwrap_or_default();
    let c = lookup(&st.headers, "Content-Type").unwrap_or_default();
    a.contains("json") || c.contains("json")
}

/// After a handler: save the session; return every Set-Cookie header.
fn finish_request(vm: &mut Vm, req: &Rc<WebObj>, res: &Value) -> R<Vec<String>> {
    let mut cookies = match res {
        Value::HttpRsp(r) => r.borrow().cookies.clone(),
        _ => Vec::new(),
    };
    let WebObj::Request(st) = &**req else { return Ok(cookies) };
    let sess = st.borrow().session.clone();
    if let Some(sv) = sess {
        let Value::Web(w) = &sv else { unreachable!() };
        let WebObj::Session(ss) = &**w else { unreachable!() };
        let data = ss.borrow().data.clone();
        remember_page(&st.borrow(), res, &mut data.borrow_mut());
        touch_meta(&st.borrow(), &mut data.borrow_mut());
        cookies.push(save_session(vm, &sv)?);
    }
    Ok(cookies)
}

/// Keep the last page a browser saw, for back() when there is no Referer.
fn remember_page(st: &ReqState, res: &Value, data: &mut OMap) {
    let Value::HttpRsp(r) = res else { return };
    let r = r.borrow();
    if st.method.to_ascii_uppercase() != "GET" || wants_json(st) || r.status != 200 {
        return;
    }
    let ctype = lookup(&r.headers, "Content-Type").unwrap_or_default();
    if ctype.contains("html") {
        let (path, q) = split_url(&st.url);
        dput(data, "_previous", s(if q.is_empty() { path } else { format!("{}?{}", path, q) }));
    }
}

fn check_key(vm: &mut Vm, k: &Value, fname: &str) -> R<String> {
    let k = str_of(vm, k, fname, 1)?;
    if k.starts_with('_') {
        return vm.fail("ValueException", format!("{}(): session keys starting with \"_\" are reserved", fname));
    }
    Ok(k)
}

fn session_method(vm: &mut Vm, ss: &RefCell<SessState>, name: &str, a: Vec<Value>) -> R<Value> {
    let data = ss.borrow().data.clone();
    let arity = |vm: &mut Vm, lo: usize, hi: usize| -> R<()> {
        if a.len() < lo || a.len() > hi {
            let exp = if lo == hi { format!("{} argument{}", lo, if lo == 1 { "" } else { "s" }) } else { format!("{} to {} arguments", lo, hi) };
            return vm.fail("TypeException", format!("Session.{}() expects {}, got {}", name, exp, a.len()));
        }
        Ok(())
    };
    let meta = |k: &str| -> Value {
        match dget(&data.borrow(), "_meta") {
            Some(Value::Dict(m)) => dget(&m.borrow(), k).unwrap_or(Value::Null),
            _ => Value::Null,
        }
    };
    match name {
        "get" => {
            arity(vm, 1, 2)?;
            let k = check_key(vm, &a[0], "Session.get")?;
            Ok(dget(&data.borrow(), &k).unwrap_or_else(|| a.get(1).cloned().unwrap_or(Value::Null)))
        }
        "put" | "set" => {
            arity(vm, 1, 2)?;
            let fname = format!("Session.{}", name);
            if a.len() == 1 {
                if let Value::Dict(d) = &a[0] {
                    let items: Vec<(Value, Value)> = d.borrow().iter().cloned().collect();
                    for (k, v) in items {
                        let k = check_key(vm, &k, &fname)?;
                        dput(&mut data.borrow_mut(), &k, v);
                    }
                    return Ok(Value::Null);
                }
                return vm.fail("TypeException", format!("{}() expects (key, value) or a dict", fname));
            }
            let k = check_key(vm, &a[0], &fname)?;
            dput(&mut data.borrow_mut(), &k, a[1].clone());
            Ok(Value::Null)
        }
        "has" => {
            arity(vm, 1, 1)?;
            let k = check_key(vm, &a[0], "Session.has")?;
            Ok(Value::Bool(!matches!(dget(&data.borrow(), &k), None | Some(Value::Null))))
        }
        "pull" => {
            arity(vm, 1, 2)?;
            let k = check_key(vm, &a[0], "Session.pull")?;
            Ok(drm(&mut data.borrow_mut(), &k).unwrap_or_else(|| a.get(1).cloned().unwrap_or(Value::Null)))
        }
        "forget" => {
            arity(vm, 1, 1)?;
            let keys = match &a[0] {
                Value::List(l) => l.borrow().clone(),
                v => vec![v.clone()],
            };
            for k in keys {
                let k = check_key(vm, &k, "Session.forget")?;
                drm(&mut data.borrow_mut(), &k);
            }
            Ok(Value::Null)
        }
        "all" => {
            arity(vm, 0, 0)?;
            let mut m = OMap::new(false);
            for (k, v) in data.borrow().iter() {
                if let Value::Str(t) = k {
                    if !t.s.starts_with('_') {
                        dput(&mut m, &t.s, v.clone());
                    }
                }
            }
            Ok(dict(m))
        }
        "flush" => {
            arity(vm, 0, 0)?;
            flush(&data);
            Ok(Value::Null)
        }
        "flash" => {
            arity(vm, 2, 2)?;
            let k = check_key(vm, &a[0], "Session.flash")?;
            let mut d = data.borrow_mut();
            dput(&mut d, &k, a[1].clone());
            let mut new = str_items(dget(&d, "_flash_new"));
            if !new.contains(&k) {
                new.push(k.clone());
            }
            let old: Vec<String> = str_items(dget(&d, "_flash_old")).into_iter().filter(|x| *x != k).collect();
            dput(&mut d, "_flash_new", str_list(&new));
            dput(&mut d, "_flash_old", str_list(&old));
            Ok(Value::Null)
        }
        "reflash" => {
            arity(vm, 0, 0)?;
            let mut d = data.borrow_mut();
            let mut new = str_items(dget(&d, "_flash_new"));
            for k in str_items(dget(&d, "_flash_old")) {
                if !new.contains(&k) {
                    new.push(k);
                }
            }
            dput(&mut d, "_flash_new", str_list(&new));
            dput(&mut d, "_flash_old", list(vec![]));
            Ok(Value::Null)
        }
        "increment" => {
            arity(vm, 1, 2)?;
            let k = check_key(vm, &a[0], "Session.increment")?;
            let by = match a.get(1) {
                None | Some(Value::Null) => 1,
                Some(v) => need_int(vm, v, "Session.increment", 2)?,
            };
            let cur = dget(&data.borrow(), &k).unwrap_or(Value::Int(0));
            let Value::Int(c) = cur else {
                return vm.fail("TypeException", format!("Session.increment(): \"{}\" holds {}, not int", k, type_name(&cur)));
            };
            dput(&mut data.borrow_mut(), &k, Value::Int(c + by));
            Ok(Value::Int(c + by))
        }
        "regenerate" => {
            arity(vm, 0, 0)?;
            regenerate(ss);
            Ok(Value::Null)
        }
        "invalidate" => {
            arity(vm, 0, 0)?;
            flush(&data);
            drm(&mut data.borrow_mut(), "_token");
            regenerate(ss);
            Ok(Value::Null)
        }
        "token" => {
            arity(vm, 0, 0)?;
            let mut d = data.borrow_mut();
            match dget(&d, "_token") {
                Some(Value::Str(t)) if !t.s.is_empty() => Ok(Value::Str(t)),
                _ => {
                    let tok: String = crate::stdlib::random_bytes(20).iter().map(|b| format!("{:02x}", b)).collect();
                    dput(&mut d, "_token", s(tok.clone()));
                    Ok(s(tok))
                }
            }
        }
        "id" => Ok(s(ss.borrow().id.clone())),
        "isNew" => Ok(Value::Bool(ss.borrow().is_new)),
        "ip" => Ok(meta("ip")),
        "userAgent" => Ok(match meta("agent") {
            Value::Null => s(""),
            v => v,
        }),
        "lastActivity" => Ok(meta("last")),
        "setIntendedUrl" => {
            arity(vm, 1, 1)?;
            let u = str_of(vm, &a[0], "Session.setIntendedUrl", 1)?;
            dput(&mut data.borrow_mut(), "_url_intended", s(u));
            Ok(Value::Null)
        }
        "intendedUrl" => Ok(dget(&data.borrow(), "_url_intended").unwrap_or(Value::Null)),
        "browser" | "platform" | "isMobile" => {
            vm.fail("NativeError", format!("Session.{}() is not supported by the native VM yet", name))
        }
        _ => vm.fail("NameException", format!("Session has no property or method '{}'", name)),
    }
}

fn flush(data: &Rc<RefCell<OMap>>) {
    let mut d = data.borrow_mut();
    let token = dget(&d, "_token");
    let meta = dget(&d, "_meta");
    d.clear();
    dput(&mut d, "_flash_old", list(vec![]));
    dput(&mut d, "_flash_new", list(vec![]));
    if let Some(t) = token {
        if !matches!(&t, Value::Null) && !matches!(&t, Value::Str(x) if x.s.is_empty()) {
            dput(&mut d, "_token", t);
        }
    }
    if let Some(m) = meta {
        if !matches!(m, Value::Null) {
            dput(&mut d, "_meta", m);
        }
    }
}

fn regenerate(ss: &RefCell<SessState>) {
    let mut b = ss.borrow_mut();
    if b.old_id.is_none() && !b.is_new {
        b.old_id = Some(b.id.clone());
    }
    b.id = new_session_id();
    drm(&mut b.data.borrow_mut(), "_token");
}

// ===========================================================================
// Request
// ===========================================================================

const REQUEST_MAKE_OPTIONS: [&str; 7] = ["headers", "body", "json", "form", "files", "cookies", "ip"];

fn build_request(vm: &mut Vm, method: &str, url: &str, opts: &OMap, fname: &str) -> R<Value> {
    let method = method.to_ascii_uppercase();
    if method.is_empty() || !method.chars().all(|c| c.is_ascii_uppercase()) {
        return vm.fail("ValueException", format!("{}(): invalid HTTP method '{}'", fname, method));
    }
    let opts = str_keys(vm, opts)?;
    for (k, _) in &opts {
        if !REQUEST_MAKE_OPTIONS.contains(&k.as_str()) {
            return vm.fail("ValueException",
                format!("{}(): unknown option \"{}\" (use {})", fname, k, REQUEST_MAKE_OPTIONS.join(", ")));
        }
    }
    let get = |k: &str| opts.iter().find(|(x, _)| x == k).map(|(_, v)| v.clone()).filter(|v| !matches!(v, Value::Null));
    let (path, query) = split_url(url);
    let mut headers = match get("headers") {
        Some(v) => {
            let m = dict_of(vm, &v, &format!("{} option headers", fname), 1)?;
            check_headers(vm, &m, fname)?
        }
        None => Vec::new(),
    };
    if get("files").is_some() {
        return vm.fail("NativeError", format!("{}(): file uploads are not supported by the native VM yet", fname));
    }
    let mut body = String::new();
    let mut form: Option<Value> = None;
    if let Some(f) = get("form") {
        let m = dict_of(vm, &f, &format!("{} option form", fname), 1)?;
        let mut pairs = Vec::new();
        for (k, v) in str_keys(vm, &m)? {
            if matches!(v, Value::Web(_)) {
                return vm.fail("NativeError", format!("{}(): file uploads are not supported by the native VM yet", fname));
            }
            pairs.push((k, vm.display(&v, false)?));
        }
        body = urlencode(&pairs);
        form = Some(pairs_dict(&pairs));
        if lookup(&headers, "Content-Type").is_none() {
            headers.push(("Content-Type".into(), "application/x-www-form-urlencoded".into()));
        }
    } else if let Some(j) = get("json") {
        body = crate::builtins::json_encode_value(vm, &j, None, 0)?;
        if let Ok(p) = crate::json::parse(&body) {
            let v = crate::builtins::json_decode_j(&p);
            if matches!(v, Value::Dict(_)) {
                form = Some(v);
            }
        }
        if lookup(&headers, "Content-Type").is_none() {
            headers.push(("Content-Type".into(), "application/json".into()));
        }
    } else if let Some(b) = get("body") {
        body = str_of(vm, &b, &format!("{} option body", fname), 1)?;
        let ctype = lookup(&headers, "Content-Type").unwrap_or_default();
        if ctype.contains("application/x-www-form-urlencoded") {
            form = Some(pairs_dict(&flat_query(&body)));
        } else if ctype.contains("json") && !body.trim().is_empty() {
            if let Ok(p) = crate::json::parse(&body) {
                form = Some(crate::builtins::json_decode_j(&p));
            }
        }
    }
    if let Some(c) = get("cookies") {
        let m = dict_of(vm, &c, &format!("{} option cookies", fname), 1)?;
        if m.len() > 0 {
            let mut jar = Vec::new();
            for (k, v) in str_keys(vm, &m)? {
                let v = vm.display(&v, false)?;
                jar.push(format!("{}={}", k, quote(&v, COOKIE_SAFE, false)));
            }
            let jar = jar.join("; ");
            match headers.iter_mut().find(|(k, _)| k.eq_ignore_ascii_case("Cookie")) {
                Some(h) => h.1 = format!("{}; {}", h.1, jar),
                None => headers.push(("Cookie".into(), jar)),
            }
        }
    }
    let ip = match get("ip") {
        Some(v) => str_of(vm, &v, &format!("{} option ip", fname), 1)?,
        None => "127.0.0.1".into(),
    };
    let full = if query.is_empty() { path.clone() } else { format!("{}?{}", path, query) };
    Ok(web(WebObj::Request(RefCell::new(ReqState {
        method, path, url: full, body, headers, query: flat_query(&query), form, ip,
        params: Vec::new(), route: None, session: None, cookies: None,
    }))))
}

fn req_all(st: &ReqState) -> OMap {
    let mut out = OMap::new(false);
    for (k, v) in &st.query {
        dput(&mut out, k, s(v.clone()));
    }
    if let Some(Value::Dict(f)) = &st.form {
        for (k, v) in f.borrow().iter() {
            if let Some(hk) = hkey(k, false) {
                out.insert(hk, k.clone(), v.clone());
            }
        }
    }
    drm(&mut out, "_token");
    drm(&mut out, "_method");
    out
}

pub fn web_get_attr(w: &Rc<WebObj>, name: &str) -> Option<Value> {
    if let WebObj::Request(st) = &**w {
        let st = st.borrow();
        return match name {
            "method" => Some(s(st.method.clone())),
            "path" => Some(s(st.path.clone())),
            "url" => Some(s(st.url.clone())),
            "body" => Some(s(st.body.clone())),
            "headers" => Some(pairs_dict(&st.headers)),
            _ => None,
        };
    }
    None
}

fn request_method(vm: &mut Vm, w: &Rc<WebObj>, name: &str, a: Vec<Value>) -> R<Value> {
    let WebObj::Request(st) = &**w else { unreachable!() };
    let fname = format!("Request.{}", name);
    let dflt = |i: usize| a.get(i).cloned().unwrap_or(Value::Null);
    match name {
        "all" => Ok(dict(req_all(&st.borrow()))),
        "input" => {
            let k = need_str(vm, &a, 0, &fname)?;
            Ok(dget(&req_all(&st.borrow()), &k).unwrap_or_else(|| dflt(1)))
        }
        "query" => {
            if a.is_empty() {
                return Ok(pairs_dict(&st.borrow().query));
            }
            let k = need_str(vm, &a, 0, &fname)?;
            Ok(st.borrow().query.iter().find(|(x, _)| *x == k).map(|(_, v)| s(v.clone())).unwrap_or_else(|| dflt(1)))
        }
        "only" => {
            let keys = match a.first() {
                Some(Value::List(l)) => l.borrow().clone(),
                v => {
                    let tn = v.map(type_name).unwrap_or_else(|| "null".into());
                    return vm.fail("TypeException", format!("Request.only() argument 1 must be list, got {}", tn));
                }
            };
            let all = req_all(&st.borrow());
            let mut out = OMap::new(false);
            for k in keys {
                if let Some(hk) = hkey(&k, false) {
                    if let Some(v) = all.get(&hk) {
                        out.insert(hk, k.clone(), v.clone());
                    }
                }
            }
            Ok(dict(out))
        }
        "has" => {
            let k = need_str(vm, &a, 0, &fname)?;
            Ok(Value::Bool(req_all(&st.borrow()).contains(&key(&k))))
        }
        "json" => {
            let body = st.borrow().body.clone();
            if body.is_empty() {
                return Ok(Value::Null);
            }
            match crate::json::parse(&body) {
                Ok(j) => Ok(crate::builtins::json_decode_j(&j)),
                Err(e) => vm.fail("JsonException", format!("request body is not valid JSON: {}", e)),
            }
        }
        "header" => {
            let k = need_str(vm, &a, 0, &fname)?;
            Ok(lookup(&st.borrow().headers, &k).map(s).unwrap_or_else(|| dflt(1)))
        }
        "ip" => Ok(s(st.borrow().ip.clone())),
        "isJson" => Ok(Value::Bool(lookup(&st.borrow().headers, "Content-Type").unwrap_or_default().contains("json"))),
        "wantsJson" => Ok(Value::Bool(wants_json(&st.borrow()))),
        "param" => {
            let k = need_str(vm, &a, 0, &fname)?;
            Ok(st.borrow().params.iter().find(|(x, _)| *x == k).map(|(_, v)| s(v.clone())).unwrap_or_else(|| dflt(1)))
        }
        "params" => Ok(pairs_dict(&st.borrow().params)),
        "routeName" => {
            let r = st.borrow().route;
            Ok(match r {
                Some(i) => ROUTER.with(|rt| rt.borrow().routes[i].name.clone()).map(s).unwrap_or(Value::Null),
                None => Value::Null,
            })
        }
        "cookie" => {
            let k = need_str(vm, &a, 0, &fname)?;
            Ok(req_cookies(w).into_iter().find(|(x, _)| *x == k).map(|(_, v)| s(v)).unwrap_or_else(|| dflt(1)))
        }
        "cookies" => Ok(pairs_dict(&req_cookies(w))),
        "session" => start_session(vm, w),
        "validate" | "file" | "hasFile" | "files" | "user" => {
            vm.fail("NativeError", format!("Request.{}() is not supported by the native VM yet", name))
        }
        _ => vm.fail("NameException", format!("Request has no property or method '{}'", name)),
    }
}

// ===========================================================================
// Response cookies
// ===========================================================================

pub fn rsp_cookie_method(vm: &mut Vm, rsp: &Rc<RefCell<HttpRspState>>, name: &str, a: Vec<Value>) -> R<Option<Value>> {
    match name {
        "withCookie" => {
            if a.len() < 2 || a.len() > 3 {
                return vm.fail("TypeException", format!("Response.withCookie() expects 2 to 3 arguments, got {}", a.len()));
            }
            let n = need_str(vm, &a, 0, "Response.withCookie")?;
            let v = need_str(vm, &a, 1, "Response.withCookie")?;
            let opts = opt_dict(vm, &a, 2, "Response.withCookie")?;
            let h = cookie_header(vm, &s(n), &s(v), &opts, "Response.withCookie")?;
            rsp.borrow_mut().cookies.push(h);
            Ok(Some(Value::HttpRsp(rsp.clone())))
        }
        "withoutCookie" => {
            let h = cookie_forget(vm, &a, "Cookie.forget")?;
            rsp.borrow_mut().cookies.push(h);
            Ok(Some(Value::HttpRsp(rsp.clone())))
        }
        "cookies" => Ok(Some(str_list(&rsp.borrow().cookies))),
        _ => Ok(None),
    }
}

fn make_rsp(status: i64, body: String, headers: Vec<(String, String)>) -> Value {
    Value::HttpRsp(Rc::new(RefCell::new(HttpRspState { status, body, headers, cookies: Vec::new() })))
}

fn rsp_json(vm: &mut Vm, v: Value, status: i64) -> R<Value> {
    crate::stdlib2::rsp_op(vm, RspOp::Json, vec![v, Value::Int(status)])
}

fn error_response(vm: &mut Vm, req: &Rc<WebObj>, status: i64, message: Option<String>, extra: Option<(&str, Value)>) -> R<Value> {
    let message = message.unwrap_or_else(|| status_text(status).to_string());
    let json = match &**req {
        WebObj::Request(st) => wants_json(&st.borrow()),
        _ => false,
    };
    if json {
        let mut m = OMap::new(false);
        dput(&mut m, "message", s(message));
        if let Some((k, v)) = extra {
            dput(&mut m, k, v);
        }
        return rsp_json(vm, dict(m), status);
    }
    Ok(make_rsp(status, message, vec![("Content-Type".into(), "text/plain; charset=utf-8".into())]))
}

fn to_response(vm: &mut Vm, v: Value) -> R<Value> {
    match v {
        Value::HttpRsp(_) => Ok(v),
        Value::Null => Ok(make_rsp(204, String::new(), vec![])),
        Value::Str(t) => Ok(make_rsp(200, t.s.clone(), vec![("Content-Type".into(), "text/html; charset=utf-8".into())])),
        Value::Dict(_) | Value::List(_) => rsp_json(vm, v, 200),
        _ => vm.fail("TypeException",
            format!("a request handler must return Response, string, dict, list or null — got {}", type_name(&v))),
    }
}

// ===========================================================================
// Route — the router
// ===========================================================================

const HTTP_METHODS: [&str; 6] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"];
const RESOURCE_ACTIONS: [(&str, &[&str], &str); 7] = [
    ("index", &["GET"], ""),
    ("create", &["GET"], "/create"),
    ("store", &["POST"], ""),
    ("show", &["GET"], "/{id}"),
    ("edit", &["GET"], "/{id}/edit"),
    ("update", &["PUT", "PATCH"], "/{id}"),
    ("destroy", &["DELETE"], "/{id}"),
];

struct RouteDef {
    methods: Vec<String>,
    path: String,
    handler: Value,
    middleware: Vec<Value>,
    name_prefix: String,
    name: Option<String>,
    wheres: Vec<(String, String)>,
    regex: Option<Rc<Regex>>,
}

#[derive(Default)]
struct RouterState {
    routes: Vec<RouteDef>,
    names: HashMap<String, usize>,
    aliases: HashMap<String, Value>,
    global_mw: Vec<Value>,
    groups: Vec<(String, Vec<Value>, String)>,
    fallback: Option<Value>,
}

fn sort_methods(ms: &mut Vec<String>) {
    ms.sort_by_key(|m| HTTP_METHODS.iter().position(|x| x == m).unwrap_or(99));
    ms.dedup();
}

/// {name} / {name?} placeholders: (start, end, name, optional)
fn path_params(path: &str) -> Vec<(usize, usize, String, bool)> {
    let b = path.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'{' {
            let mut j = i + 1;
            if j < b.len() && (b[j].is_ascii_alphabetic() || b[j] == b'_') {
                j += 1;
                while j < b.len() && (b[j].is_ascii_alphanumeric() || b[j] == b'_') {
                    j += 1;
                }
                let name = path[i + 1..j].to_string();
                let opt = j < b.len() && b[j] == b'?';
                let k = if opt { j + 1 } else { j };
                if k < b.len() && b[k] == b'}' {
                    out.push((i, k + 1, name, opt));
                    i = k + 1;
                    continue;
                }
            }
        }
        i += 1;
    }
    out
}

fn join_path(parts: &[&str]) -> String {
    let segs: Vec<&str> = parts.iter().map(|p| p.trim_matches('/')).filter(|p| !p.is_empty()).collect();
    let path = format!("/{}", segs.join("/"));
    // `:id` / `:id?` become `{id}` / `{id?}`
    path.split('/').map(|seg| {
        if let Some(rest) = seg.strip_prefix(':') {
            let (name, opt) = match rest.strip_suffix('?') {
                Some(n) => (n, true),
                None => (rest, false),
            };
            let ok = name.chars().next().map(|c| c.is_ascii_alphabetic() || c == '_').unwrap_or(false)
                && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_');
            if ok {
                return format!("{{{}{}}}", name, if opt { "?" } else { "" });
            }
        }
        seg.to_string()
    }).collect::<Vec<_>>().join("/")
}

fn route_regex(vm: &mut Vm, r: &RouteDef) -> R<Rc<Regex>> {
    if let Some(rx) = &r.regex {
        return Ok(rx.clone());
    }
    let mut out = String::new();
    let mut pos = 0;
    for (st, en, name, opt) in path_params(&r.path) {
        let lit = &r.path[pos..st];
        let pat = r.wheres.iter().find(|(k, _)| *k == name).map(|(_, v)| v.clone()).unwrap_or_else(|| "[^/]+".into());
        if opt && lit.ends_with('/') {
            out += &regex_escape(&lit[..lit.len() - 1]);
            out += &format!("(?:/(?P<{}>{}))?", name, pat);
        } else {
            out += &regex_escape(lit);
            out += &format!("(?P<{}>{}){}", name, pat, if opt { "?" } else { "" });
        }
        pos = en;
    }
    out += &regex_escape(&r.path[pos..]);
    match parse_regex(&format!("^{}$", out)) {
        Ok(rx) => Ok(Rc::new(rx)),
        Err(e) => vm.fail("ValueException", format!("route {}: invalid where() pattern: {}", r.path, e)),
    }
}

fn is_class_handler(v: &Value) -> Option<(Rc<Class>, String)> {
    if let Value::List(l) = v {
        let l = l.borrow();
        if l.len() == 2 {
            if let (Value::Class(c), Value::Str(m)) = (&l[0], &l[1]) {
                return Some((c.clone(), m.s.clone()));
            }
        }
    }
    None
}

fn check_handler(vm: &mut Vm, h: &Value, fname: &str) -> R<Value> {
    if is_callable(h) {
        return Ok(h.clone());
    }
    if let Some((cls, m)) = is_class_handler(h) {
        match cls.methods.get(m.as_str()) {
            None => return vm.fail("NameException", format!("{}(): class {} has no method '{}'", fname, cls.name, m)),
            Some(md) if md.vis != Vis::Public => {
                return vm.fail("TypeException",
                    format!("{}(): {}.{}() is {} — route handlers must be #[Public]", fname, cls.name, m, md.vis.name()));
            }
            _ => return Ok(h.clone()),
        }
    }
    vm.fail("TypeException",
        format!("{}(): a route handler must be a function or #[ControllerClass, \"method\"], got {}", fname, type_name(h)))
}

fn mw_list(vm: &mut Vm, v: &Value, fname: &str) -> R<Vec<Value>> {
    let items = match v {
        Value::Null => return Ok(Vec::new()),
        Value::List(l) => l.borrow().clone(),
        v => vec![v.clone()],
    };
    for m in &items {
        if !(matches!(m, Value::Str(_) | Value::Class(_)) || is_callable(m)) {
            return vm.fail("TypeException",
                format!("{}(): middleware must be a name, a class or a function, got {}", fname, type_name(m)));
        }
    }
    Ok(items)
}

fn add_route(vm: &mut Vm, methods: Vec<String>, path: &str, handler: &Value, fname: &str) -> R<Value> {
    let handler = check_handler(vm, handler, fname)?;
    let (prefix, mws, names) = ROUTER.with(|r| {
        let r = r.borrow();
        let pre: Vec<&str> = r.groups.iter().map(|g| g.0.as_str()).collect();
        (join_path(&pre), r.groups.iter().flat_map(|g| g.1.clone()).collect::<Vec<_>>(),
         r.groups.iter().map(|g| g.2.as_str()).collect::<String>())
    });
    let full = join_path(&[&prefix, path]);
    for (_, _, name, _) in path_params(&full) {
        if full.matches(&format!("{{{}", name)).count() > 1 {
            return vm.fail("ValueException", format!("{}(): route parameter {{{}}} appears twice in {}", fname, name, full));
        }
    }
    let mut methods = methods;
    sort_methods(&mut methods);
    let idx = ROUTER.with(|r| {
        let mut r = r.borrow_mut();
        r.routes.push(RouteDef { methods, path: full, handler, middleware: mws, name_prefix: names, name: None,
                                 wheres: Vec::new(), regex: None });
        r.routes.len() - 1
    });
    Ok(web(WebObj::Route(idx)))
}

fn route_url(vm: &mut Vm, a: &[Value]) -> R<Value> {
    let name = need_str(vm, a, 0, "Route.url")?;
    let pm = opt_dict(vm, a, 1, "Route.url")?;
    let mut params = str_keys(vm, &pm)?;
    let Some(path) = ROUTER.with(|r| { let r = r.borrow(); r.names.get(&name).map(|&i| r.routes[i].path.clone()) }) else {
        return vm.fail("NameException", format!("Route.url(): no route named \"{}\"", name));
    };
    let mut out = String::new();
    let mut pos = 0;
    for (st, en, key, opt) in path_params(&path) {
        out += &path[pos..st];
        pos = en;
        let found = params.iter().position(|(k, v)| *k == key && !matches!(v, Value::Null));
        match found {
            Some(i) => {
                let (_, v) = params.remove(i);
                let t = vm.display(&v, false)?;
                out += &quote(&t, "", false);
            }
            None => {
                if let Some(i) = params.iter().position(|(k, _)| *k == key) {
                    params.remove(i);
                }
                if !opt {
                    return vm.fail("ValueException", format!("Route.url(): route \"{}\" needs a value for {{{}}}", name, key));
                }
            }
        }
    }
    out += &path[pos..];
    let mut clean = String::new();
    for c in out.chars() {
        if c == '/' && clean.ends_with('/') {
            continue;
        }
        clean.push(c);
    }
    if clean.len() > 1 {
        clean = clean.trim_end_matches('/').to_string();
    }
    if !params.is_empty() {
        let mut q = Vec::new();
        for (k, v) in params {
            q.push((k, vm.display(&v, false)?));
        }
        clean += &format!("?{}", urlencode(&q));
    }
    Ok(s(clean))
}

fn route_list(vm: &mut Vm) -> R<Value> {
    let rows: Vec<(Vec<String>, String, Option<String>, Value, Vec<Value>)> = ROUTER.with(|r| {
        r.borrow().routes.iter().map(|d| (d.methods.clone(), d.path.clone(), d.name.clone(), d.handler.clone(), d.middleware.clone())).collect()
    });
    let mut out = Vec::new();
    for (methods, path, name, h, mws) in rows {
        let action = match is_class_handler(&h) {
            Some((c, m)) => format!("{}.{}", c.name, m),
            None => "function".to_string(),
        };
        let mws: Vec<Value> = mws.iter().map(|m| match m {
            Value::Str(_) => m.clone(),
            Value::Class(c) => s(c.name.to_string()),
            _ => s(type_name(m)),
        }).collect();
        let mut row = OMap::new(false);
        dput(&mut row, "methods", str_list(&methods));
        dput(&mut row, "path", s(path));
        dput(&mut row, "name", name.map(s).unwrap_or(Value::Null));
        dput(&mut row, "action", s(action));
        dput(&mut row, "middleware", list(mws));
        out.push(dict(row));
    }
    let _ = vm;
    Ok(list(out))
}

fn route_where(vm: &mut Vm, idx: usize, a: &[Value]) -> R<()> {
    let pairs: Vec<(Value, Value)> = if a.len() == 1 {
        match &a[0] {
            Value::Dict(d) => d.borrow().iter().cloned().collect(),
            _ => return vm.fail("TypeException", "Route.where() expects (param, pattern) or a dict"),
        }
    } else if a.len() == 2 {
        vec![(a[0].clone(), a[1].clone())]
    } else {
        return vm.fail("TypeException", "Route.where() expects (param, pattern) or a dict");
    };
    for (k, v) in pairs {
        let k = str_of(vm, &k, "Route.where", 1)?;
        let v = str_of(vm, &v, "Route.where", 2)?;
        let path = ROUTER.with(|r| r.borrow().routes[idx].path.clone());
        if !path.contains(&format!("{{{}", k)) {
            return vm.fail("ValueException", format!("Route.where(): route {} has no parameter {{{}}}", path, k));
        }
        if let Err(e) = parse_regex(&v) {
            return vm.fail("ValueException", format!("Route.where(): invalid pattern \"{}\": {}", v, e));
        }
        if v.replace("(?:", "").contains('(') {
            return vm.fail("ValueException", format!("Route.where(): use (?:...) instead of (...) in \"{}\"", v));
        }
        ROUTER.with(|r| {
            let mut r = r.borrow_mut();
            let d = &mut r.routes[idx];
            set_pair(&mut d.wheres, &k, v);
            d.regex = None;
        });
    }
    Ok(())
}

fn route_method(vm: &mut Vm, w: &Rc<WebObj>, idx: usize, name: &str, a: Vec<Value>) -> R<Value> {
    let this = Value::Web(w.clone());
    match name {
        "name" => {
            let n = need_str(vm, &a, 0, "Route.name")?;
            ROUTER.with(|r| {
                let mut r = r.borrow_mut();
                let full = format!("{}{}", r.routes[idx].name_prefix, n);
                r.routes[idx].name = Some(full.clone());
                r.names.insert(full, idx);
            });
            Ok(this)
        }
        "middleware" => {
            let ms = mw_list(vm, a.first().unwrap_or(&Value::Null), "Route.middleware")?;
            ROUTER.with(|r| r.borrow_mut().routes[idx].middleware.extend(ms));
            Ok(this)
        }
        "where" => {
            route_where(vm, idx, &a)?;
            Ok(this)
        }
        "whereNumber" => {
            let k = need_str(vm, &a, 0, "Route.whereNumber")?;
            route_where(vm, idx, &[s(k), s("[0-9]+")])?;
            Ok(this)
        }
        "prefix" => {
            let p = need_str(vm, &a, 0, "Route.prefix")?;
            ROUTER.with(|r| {
                let mut r = r.borrow_mut();
                let d = &mut r.routes[idx];
                d.path = join_path(&[&p, &d.path]);
                d.regex = None;
            });
            Ok(this)
        }
        "path" => Ok(s(ROUTER.with(|r| r.borrow().routes[idx].path.clone()))),
        "methods" => Ok(str_list(&ROUTER.with(|r| r.borrow().routes[idx].methods.clone()))),
        "routeName" => Ok(ROUTER.with(|r| r.borrow().routes[idx].name.clone()).map(s).unwrap_or(Value::Null)),
        _ => vm.fail("NameException", format!("Route has no property or method '{}'", name)),
    }
}

// ---- dispatching -------------------------------------------------------------

fn fn_params(f: &Value) -> Option<Rc<crate::code::Proto>> {
    match f {
        Value::Func(c) => Some(c.proto.clone()),
        Value::Bound(b) => match &b.func {
            Value::Func(c) => Some(c.proto.clone()),
            _ => None,
        },
        _ => None,
    }
}

/// -> Ok(None) when a route parameter does not convert (404)
fn convert_param(vm: &mut Vm, raw: Option<String>, t: Option<&Ty>, pname: &str) -> R<Option<Value>> {
    let Some(raw) = raw else { return Ok(Some(Value::Null)) };
    let k = t.map(|t| &t.k);
    match k {
        Some(TyK::Int) => {
            let d = raw.strip_prefix('-').unwrap_or(&raw);
            if d.is_empty() || !d.chars().all(|c| c.is_ascii_digit()) {
                return Ok(None);
            }
            Ok(raw.parse::<i64>().ok().map(Value::Int))
        }
        Some(TyK::Float) => Ok(raw.trim().parse::<f64>().ok().map(Value::Float)),
        Some(TyK::Bool) => Ok(match raw.as_str() {
            "true" | "1" => Some(Value::Bool(true)),
            "false" | "0" => Some(Value::Bool(false)),
            _ => None,
        }),
        None | Some(TyK::Str) => Ok(Some(s(raw))),
        Some(TyK::Any) if t.map(|t| &*t.name == "any").unwrap_or(true) => Ok(Some(s(raw))),
        _ => {
            let tn = t.map(|t| t.name.to_string()).unwrap_or_default();
            vm.fail("TypeException",
                format!("route parameter '{}' must be int, float, bool, string or any — got {}", pname, tn))
        }
    }
}

fn short(n: &str) -> &str {
    n.rsplit('\\').next().unwrap_or(n)
}

fn call_with_injection(vm: &mut Vm, f: &Value, req: &Rc<WebObj>) -> R<Option<Value>> {
    let reqv = Value::Web(req.clone());
    let Some(proto) = fn_params(f) else {
        return vm.call_value(f, vec![reqv]).map(Some);
    };
    let WebObj::Request(st) = &**req else { unreachable!() };
    let (mut route_params, route) = {
        let b = st.borrow();
        (b.params.clone(), b.route)
    };
    let route_path = route.map(|i| ROUTER.with(|r| r.borrow().routes[i].path.clone()));
    let declared: Vec<String> = route_path.as_deref().map(|p| path_params(p).into_iter().map(|x| x.2).collect()).unwrap_or_default();
    let mut args = Vec::new();
    let mut kwargs: Option<Vec<(Rc<str>, Value)>> = None;
    for p in proto.params.iter() {
        if p.variadic {
            break;
        }
        let mut give = |v: Value, kw: &mut Option<Vec<(Rc<str>, Value)>>| match kw {
            None => args.push(v),
            Some(k) => k.push((p.name.clone(), v)),
        };
        let tname = p.ty.as_ref().map(|t| short(&t.name).to_string());
        if tname.as_deref() == Some("Request") {
            give(reqv.clone(), &mut kwargs);
            continue;
        }
        let cls = match p.ty.as_ref().map(|t| &t.k) {
            Some(TyK::Class(id)) => Some(vm.classes[*id].clone()),
            _ => None,
        };
        if let Some(c) = &cls {
            if !route_params.iter().any(|(k, _)| **k == *p.name) && !declared.iter().any(|k| **k == *p.name) {
                let obj = vm.make_instance(c, Vec::new())?;
                give(obj, &mut kwargs);
                continue;
            }
        }
        let raw = if let Some(i) = route_params.iter().position(|(k, _)| **k == *p.name) {
            Some(route_params.remove(i).1)
        } else if !route_params.is_empty() {
            Some(route_params.remove(0).1)
        } else if p.default.is_some() {
            if kwargs.is_none() {
                kwargs = Some(Vec::new());
            }
            continue;
        } else if declared.iter().any(|k| **k == *p.name) {
            None
        } else {
            let where_ = match &route_path {
                Some(path) => format!("route {}", path),
                None => "this route".to_string(),
            };
            return vm.fail("TypeException", format!("handler parameter '{}' has no matching {{{}}} in {} (handlers take Request and route parameters)",
                p.name, p.name, where_));
        };
        match convert_param(vm, raw, p.ty.as_ref(), &p.name)? {
            Some(v) => give(v, &mut kwargs),
            None => return Ok(None),
        }
    }
    let kw = kwargs.filter(|k| !k.is_empty());
    vm.call_value_kw(f, args, kw).map(Some)
}

fn resolve_handler(vm: &mut Vm, h: &Value) -> R<Value> {
    if let Some((cls, m)) = is_class_handler(h) {
        let md = cls.methods.get(m.as_str()).cloned().unwrap();
        if md.is_static {
            return Ok(md.func.clone());
        }
        let obj = vm.make_instance(&cls, Vec::new())?;
        return Ok(Value::Bound(Rc::new(Bound { this: obj, func: md.func.clone() })));
    }
    Ok(h.clone())
}

fn resolve_middleware(vm: &mut Vm, mw: &Value) -> R<(Value, Vec<Value>)> {
    let mut extra = Vec::new();
    let mut mw = mw.clone();
    if let Value::Str(t) = &mw {
        let (name, rest) = match t.s.split_once(':') {
            Some((a, b)) => (a.to_string(), b.to_string()),
            None => (t.s.clone(), String::new()),
        };
        let Some(found) = ROUTER.with(|r| r.borrow().aliases.get(&name).cloned()) else {
            return vm.fail("NameException",
                format!("unknown middleware \"{}\" — register it with Route.alias(\"{}\", ...)", name, name));
        };
        if !rest.is_empty() {
            extra = rest.split(',').map(|x| s(x.to_string())).collect();
        }
        mw = found;
    }
    if let Value::Class(c) = &mw {
        mw = vm.make_instance(&c.clone(), Vec::new())?;
    }
    if let Value::Obj(o) = &mw {
        let Some(m) = o.cls.methods.get("handle").cloned() else {
            return vm.fail("TypeException",
                format!("middleware class {} needs a handle(Request req, any next) method", o.cls.name));
        };
        mw = Value::Bound(Rc::new(Bound { this: mw.clone(), func: m.func.clone() }));
    }
    let want = fn_params(&mw).map(|p| p.params.len()).unwrap_or(2 + extra.len());
    if want != 2 + extra.len() {
        if !extra.is_empty() {
            return vm.fail("TypeException", format!("middleware got {} parameter(s) after ':' but its handler takes {} arguments (Request, next, then one string per parameter)",
                extra.len(), want));
        }
        return vm.fail("TypeException", format!("a middleware function must take (Request req, any next), it takes {} argument(s)", want));
    }
    Ok((mw, extra))
}

/// Turn Http.abort() and validation failures into responses.
fn guarded(vm: &mut Vm, req: &Rc<WebObj>, r: R<Option<Value>>) -> R<Value> {
    match r {
        Ok(Some(v)) => to_response(vm, v),
        Ok(None) => error_response(vm, req, 404, None, None),
        Err(Flow::Throw(o)) => {
            let abort = vm.class_by_name.get("AbortException").map(|&i| vm.classes[i].clone());
            let valid = vm.class_by_name.get("ValidationException").map(|&i| vm.classes[i].clone());
            let msg = match o.field("message") {
                Value::Str(t) => Some(t.s.clone()),
                Value::Null => None,
                v => Some(crate::builtins::plain_display(&v)),
            };
            if let Some(a) = abort {
                if o.cls.is_subclass_of(&a) {
                    let status = match o.field("status") {
                        Value::Int(n) => n,
                        _ => 500,
                    };
                    return error_response(vm, req, status, msg, None);
                }
            }
            if let Some(vc) = valid {
                if o.cls.is_subclass_of(&vc) {
                    let errors = match o.field("errors") {
                        Value::Null => dict(OMap::new(false)),
                        v => v,
                    };
                    return error_response(vm, req, 422, msg, Some(("errors", errors)));
                }
            }
            Err(Flow::Throw(o))
        }
        Err(e) => Err(e),
    }
}

fn run_pipeline(vm: &mut Vm, req: &Rc<WebObj>, mws: Rc<Vec<Value>>, k: usize, stage: Stage) -> R<Value> {
    if k == mws.len() {
        let r = final_step(vm, req, stage);
        return guarded(vm, req, r);
    }
    let (f, extra) = resolve_middleware(vm, &mws[k])?;
    let next = Value::Bound(Rc::new(Bound {
        this: web(WebObj::Next(NextState { mws: mws.clone(), k: k + 1, stage })),
        func: native_value(Builtin::Web("next"), "next", 1, Some(1)),
    }));
    let mut args = vec![Value::Web(req.clone()), next];
    args.extend(extra);
    let r = vm.call_value(&f, args).map(Some);
    guarded(vm, req, r)
}

fn final_step(vm: &mut Vm, req: &Rc<WebObj>, stage: Stage) -> R<Option<Value>> {
    match stage {
        Stage::Route(idx) => {
            let h = ROUTER.with(|r| r.borrow().routes[idx].handler.clone());
            let f = resolve_handler(vm, &h)?;
            call_with_injection(vm, &f, req)
        }
        Stage::Global => {
            let WebObj::Request(st) = &**req else { unreachable!() };
            let (path, method) = {
                let b = st.borrow();
                let mut p = b.path.clone();
                if p.len() > 1 {
                    p = p.trim_end_matches('/').to_string();
                    if p.is_empty() {
                        p = "/".into();
                    }
                }
                (p, b.method.to_ascii_uppercase())
            };
            let n = ROUTER.with(|r| r.borrow().routes.len());
            let mut allowed: Vec<String> = Vec::new();
            for i in 0..n {
                let rx = {
                    let cached = ROUTER.with(|r| r.borrow().routes[i].regex.clone());
                    match cached {
                        Some(rx) => rx,
                        None => {
                            let rx = ROUTER.with(|r| {
                                let r = r.borrow();
                                let d = &r.routes[i];
                                (d.path.clone(), d.wheres.clone())
                            });
                            let tmp = RouteDef { methods: vec![], path: rx.0, handler: Value::Null, middleware: vec![],
                                                 name_prefix: String::new(), name: None, wheres: rx.1, regex: None };
                            let rx = route_regex(vm, &tmp)?;
                            ROUTER.with(|r| r.borrow_mut().routes[i].regex = Some(rx.clone()));
                            rx
                        }
                    }
                };
                let Some(m) = regex_search(&rx, &path) else { continue };
                let methods = ROUTER.with(|r| r.borrow().routes[i].methods.clone());
                if methods.contains(&method) || (method == "HEAD" && methods.iter().any(|x| x == "GET")) {
                    let route_path = ROUTER.with(|r| r.borrow().routes[i].path.clone());
                    let mut params = Vec::new();
                    for (_, _, name, _) in path_params(&route_path) {
                        if let Some(v) = m.named.get(&name) {
                            params.push((name.clone(), unquote(v, false)));
                        }
                    }
                    {
                        let mut b = st.borrow_mut();
                        b.params = params;
                        b.route = Some(i);
                    }
                    let mws = ROUTER.with(|r| r.borrow().routes[i].middleware.clone());
                    return run_pipeline(vm, req, Rc::new(mws), 0, Stage::Route(i)).map(Some);
                }
                allowed.extend(methods);
            }
            if !allowed.is_empty() {
                sort_methods(&mut allowed);
                let res = error_response(vm, req, 405, None, None)?;
                if let Value::HttpRsp(r) = &res {
                    r.borrow_mut().headers.push(("Allow".into(), allowed.join(", ")));
                }
                return Ok(Some(res));
            }
            let fb = ROUTER.with(|r| r.borrow().fallback.clone());
            match fb {
                Some(h) => {
                    let f = resolve_handler(vm, &h)?;
                    call_with_injection(vm, &f, req)
                }
                None => Ok(None),
            }
        }
    }
}

fn spoof_method(st: &RefCell<ReqState>) {
    let mut b = st.borrow_mut();
    if b.method.to_ascii_uppercase() != "POST" {
        return;
    }
    let mut want = match &b.form {
        Some(Value::Dict(d)) => match dget(&d.borrow(), "_method") {
            Some(Value::Str(t)) if !t.s.is_empty() => Some(t.s.clone()),
            _ => None,
        },
        _ => None,
    };
    if want.is_none() {
        want = lookup(&b.headers, "X-HTTP-Method-Override");
    }
    if let Some(w) = want {
        let w = w.to_ascii_uppercase();
        if ["PUT", "PATCH", "DELETE"].contains(&w.as_str()) {
            b.method = w;
        }
    }
}

fn dispatch(vm: &mut Vm, req: &Rc<WebObj>) -> R<Value> {
    if let WebObj::Request(st) = &**req {
        spoof_method(st);
    }
    let mws = ROUTER.with(|r| r.borrow().global_mw.clone());
    run_pipeline(vm, req, Rc::new(mws), 0, Stage::Global)
}

fn as_request(vm: &mut Vm, v: Option<&Value>, fname: &str) -> R<Rc<WebObj>> {
    match v {
        Some(Value::Web(w)) if matches!(**w, WebObj::Request(_)) => Ok(w.clone()),
        v => {
            let tn = v.map(type_name).unwrap_or_else(|| "null".into());
            vm.fail("TypeException", format!("{}() argument 1 must be Request, got {}", fname, tn))
        }
    }
}

// ===========================================================================
// TestClient
// ===========================================================================

fn client_send(vm: &mut Vm, cs: &RefCell<ClientState>, method: &str, path: &str, data: Option<Value>, opts: OMap, fname: &str) -> R<Value> {
    let items = str_keys(vm, &opts)?;
    for (k, _) in &items {
        if !["headers", "form", "json", "body", "files", "cookies"].contains(&k.as_str()) {
            return vm.fail("ValueException",
                format!("{}(): unknown option \"{}\" (use headers, form, json, body, files, cookies)", fname, k));
        }
    }
    let mut o = OMap::new(false);
    let mut form_flag: Option<Value> = None;
    for (k, v) in &items {
        if k == "form" {
            form_flag = Some(v.clone());
        } else if k != "headers" && k != "cookies" {
            dput(&mut o, k, v.clone());
        }
    }
    let mut headers = OMap::new(false);
    for (k, v) in &cs.borrow().headers {
        dput(&mut headers, k, s(v.clone()));
    }
    if let Some((_, h)) = items.iter().find(|(k, _)| k == "headers") {
        if !matches!(h, Value::Null) {
            let m = dict_of(vm, h, &format!("{} option headers", fname), 1)?;
            for (k, v) in m.iter() {
                if let Some(hk) = hkey(k, false) {
                    headers.insert(hk, k.clone(), v.clone());
                }
            }
        }
    }
    dput(&mut o, "headers", dict(headers));
    match data {
        Some(Value::Null) | None => {
            if let Some(f) = form_flag {
                if !matches!(f, Value::Bool(true)) {
                    dput(&mut o, "form", f);
                }
            }
        }
        Some(Value::Str(t)) => {
            if let Some(f) = form_flag {
                dput(&mut o, "form", f);
            }
            dput(&mut o, "body", Value::Str(t));
        }
        Some(d) => {
            if matches!(form_flag, Some(Value::Bool(true))) {
                dput(&mut o, "form", d);
            } else {
                dput(&mut o, "json", d);
            }
        }
    }
    let mut cookies = OMap::new(false);
    for (k, v) in &cs.borrow().cookies {
        dput(&mut cookies, k, s(v.clone()));
    }
    if let Some((_, c)) = items.iter().find(|(k, _)| k == "cookies") {
        if let Value::Dict(d) = c {
            for (k, v) in d.borrow().iter() {
                if let Some(hk) = hkey(k, false) {
                    cookies.insert(hk, k.clone(), v.clone());
                }
            }
        }
    }
    dput(&mut o, "cookies", dict(cookies));
    let req = build_request(vm, method, path, &o, fname)?;
    let Value::Web(rw) = &req else { unreachable!() };
    let res = dispatch(vm, rw)?;
    for header in finish_request(vm, rw, &res)? {
        let first = header.split(';').next().unwrap_or("");
        let (name, value) = first.split_once('=').unwrap_or((first, ""));
        let mut c = cs.borrow_mut();
        if header.contains("Max-Age=0") {
            c.cookies.retain(|(k, _)| k != name);
        } else {
            set_pair(&mut c.cookies, name, unquote(value, false));
        }
    }
    Ok(res)
}

fn client_method(vm: &mut Vm, w: &Rc<WebObj>, name: &str, a: Vec<Value>) -> R<Value> {
    let WebObj::Client(cs) = &**w else { unreachable!() };
    let fname = format!("TestClient.{}", name);
    let arity = |vm: &mut Vm, lo: usize, hi: usize| -> R<()> {
        if a.len() < lo || a.len() > hi {
            let exp = if lo == hi { format!("{} argument{}", lo, if lo == 1 { "" } else { "s" }) } else { format!("{} to {} arguments", lo, hi) };
            return vm.fail("TypeException", format!("{}() expects {}, got {}", fname, exp, a.len()));
        }
        Ok(())
    };
    match name {
        "get" | "delete" => {
            arity(vm, 1, 2)?;
            let path = need_str(vm, &a, 0, &fname)?;
            let opts = opt_dict(vm, &a, 1, &fname)?;
            client_send(vm, cs, &name.to_ascii_uppercase(), &path, None, opts, &fname)
        }
        "post" | "put" | "patch" => {
            arity(vm, 1, 3)?;
            let path = need_str(vm, &a, 0, &fname)?;
            let opts = opt_dict(vm, &a, 2, &fname)?;
            client_send(vm, cs, &name.to_ascii_uppercase(), &path, a.get(1).cloned(), opts, &fname)
        }
        "request" => {
            arity(vm, 2, 3)?;
            let method = need_str(vm, &a, 0, &fname)?;
            let path = need_str(vm, &a, 1, &fname)?;
            let opts = opt_dict(vm, &a, 2, &fname)?;
            client_send(vm, cs, &method, &path, None, opts, &fname)
        }
        "withHeaders" => {
            arity(vm, 1, 1)?;
            let m = dict_of(vm, &a[0], &fname, 1)?;
            let hs = check_headers(vm, &m, &fname)?;
            let mut c = cs.borrow_mut();
            for (k, v) in hs {
                set_pair(&mut c.headers, &k, v);
            }
            Ok(Value::Web(w.clone()))
        }
        "withCsrfChecks" => Ok(Value::Web(w.clone())),
        "cookies" => Ok(pairs_dict(&cs.borrow().cookies)),
        "clearCookies" => {
            cs.borrow_mut().cookies.clear();
            Ok(Value::Null)
        }
        "actingAs" | "withToken" => vm.fail("NativeError", format!("TestClient.{}() is not supported by the native VM yet", name)),
        _ => vm.fail("NameException", format!("TestClient has no property or method '{}'", name)),
    }
}

// ===========================================================================
// entry points
// ===========================================================================

/// Module members: (builtin name, min args, max args)
pub fn member(module: &str, name: &str) -> Option<(&'static str, usize, Option<usize>)> {
    let t: &[(&'static str, usize, usize)] = match module {
        "Route" => &[("Route.get", 2, 2), ("Route.post", 2, 2), ("Route.put", 2, 2), ("Route.patch", 2, 2),
                     ("Route.delete", 2, 2), ("Route.options", 2, 2), ("Route.any", 2, 2), ("Route.match", 3, 3),
                     ("Route.group", 2, 2), ("Route.resource", 2, 3), ("Route.alias", 2, 2),
                     ("Route.globalMiddleware", 1, 1), ("Route.fallback", 1, 1), ("Route.dispatch", 1, 1),
                     ("Route.url", 1, 2), ("Route.has", 1, 1), ("Route.list", 0, 0), ("Route.clear", 0, 0),
                     ("Route.serve", 0, 1)],
        "Cookie" => &[("Cookie.make", 2, 3), ("Cookie.forget", 1, 2), ("Cookie.parse", 1, 1)],
        "Test" => &[("Test.client", 0, 0)],
        "Request" => &[("Request.make", 2, 3)],
        "Session" => &[("Session.config", 1, 1)],
        "Http" => &[("Http.abort", 1, 2)],
        _ => return None,
    };
    t.iter().find(|(n, _, _)| n.split_once('.').map(|x| x.1) == Some(name)).map(|(n, lo, hi)| (*n, *lo, Some(*hi)))
}

pub fn call(vm: &mut Vm, name: &'static str, a: Vec<Value>, this: Option<Value>) -> R<Value> {
    match name {
        "next" => {
            let Some(Value::Web(w)) = this else { unreachable!() };
            let WebObj::Next(ns) = &*w else { unreachable!() };
            let ns = ns.clone();
            let req = as_request(vm, a.first(), "next")?;
            run_pipeline(vm, &req, ns.mws, ns.k, ns.stage)
        }
        "TestClient.new" => {
            if !a.is_empty() {
                return vm.fail("TypeException", format!("TestClient() expects 0 arguments, got {}", a.len()));
            }
            Ok(web(WebObj::Client(RefCell::new(ClientState::default()))))
        }
        "Test.client" => Ok(web(WebObj::Client(RefCell::new(ClientState::default())))),
        "Http.abort" => {
            let status = need_int(vm, &a[0], "Http.abort", 1)?;
            if !(400..=599).contains(&status) {
                return vm.fail("ValueException", format!("Http.abort() status must be 400-599, got {}", status));
            }
            let msg = match a.get(1) {
                None | Some(Value::Null) => status_text(status).to_string(),
                Some(v) => str_of(vm, v, "Http.abort", 2)?,
            };
            Err(vm.exc_with_field("AbortException", msg, "status", Value::Int(status)))
        }
        "Cookie.make" => {
            let v = need_str(vm, &a, 1, "Cookie.make")?;
            let opts = opt_dict(vm, &a, 2, "Cookie.make")?;
            Ok(s(cookie_header(vm, &a[0], &s(v), &opts, "Cookie.make")?))
        }
        "Cookie.forget" => Ok(s(cookie_forget(vm, &a, "Cookie.forget")?)),
        "Cookie.parse" => {
            let t = need_str(vm, &a, 0, "Cookie.parse")?;
            Ok(pairs_dict(&parse_cookie_header(&t)))
        }
        "Request.make" => {
            let m = need_str(vm, &a, 0, "Request.make")?;
            let u = need_str(vm, &a, 1, "Request.make")?;
            let opts = opt_dict(vm, &a, 2, "Request.make")?;
            build_request(vm, &m, &u, &opts, "Request.make")
        }
        "Session.config" => {
            let m = dict_of(vm, &a[0], "Session.config", 1)?;
            let cur = session_settings(vm)?;
            let mut cfg = SessCfg { driver: cur.driver.clone(), lifetime: cur.lifetime, cookie: cur.cookie.clone(),
                                    secure: cur.secure, same_site: cur.same_site.clone() };
            apply_session_opts(vm, &mut cfg, &m, "Session.config", true)?;
            SESS_CFG.with(|c| *c.borrow_mut() = Some(Rc::new(cfg)));
            Ok(Value::Null)
        }
        "Route.get" | "Route.post" | "Route.put" | "Route.patch" | "Route.delete" | "Route.options" | "Route.any" => {
            let verb = &name[6..];
            let path = need_str(vm, &a, 0, name)?;
            let methods: Vec<String> = if verb == "any" {
                HTTP_METHODS.iter().map(|m| m.to_string()).collect()
            } else {
                vec![verb.to_ascii_uppercase()]
            };
            add_route(vm, methods, &path, &a[1], name)
        }
        "Route.match" => {
            let ms = match &a[0] {
                Value::List(l) => l.borrow().clone(),
                v => return vm.fail("TypeException", format!("Route.match() argument 1 must be list, got {}", type_name(v))),
            };
            let mut methods = Vec::new();
            for m in ms {
                let m = str_of(vm, &m, "Route.match", 1)?.to_ascii_uppercase();
                if !HTTP_METHODS.contains(&m.as_str()) {
                    return vm.fail("ValueException", format!("Route.match(): unknown HTTP method \"{}\"", m));
                }
                methods.push(m);
            }
            let path = need_str(vm, &a, 1, "Route.match")?;
            add_route(vm, methods, &path, &a[2], "Route.match")
        }
        "Route.group" => {
            let opts = dict_of(vm, &a[0], "Route.group", 1)?;
            let items = str_keys(vm, &opts)?;
            let (mut prefix, mut gname, mut mws) = (String::new(), String::new(), Vec::new());
            for (k, v) in items {
                match k.as_str() {
                    "prefix" => prefix = str_of(vm, &v, "Route.group option prefix", 1)?,
                    "name" => gname = str_of(vm, &v, "Route.group option name", 1)?,
                    "middleware" => mws = mw_list(vm, &v, "Route.group")?,
                    _ => return vm.fail("ValueException", format!("Route.group(): unknown option \"{}\" (use prefix, middleware, name)", k)),
                }
            }
            if !is_callable(&a[1]) {
                return vm.fail("TypeException", format!("Route.group() needs a function as argument 2, got {}", type_name(&a[1])));
            }
            ROUTER.with(|r| r.borrow_mut().groups.push((prefix, mws, gname)));
            let r = vm.call_value(&a[1], vec![]);
            ROUTER.with(|r| r.borrow_mut().groups.pop());
            r.map(|_| Value::Null)
        }
        "Route.resource" => {
            let rname = need_str(vm, &a, 0, "Route.resource")?.trim_matches('/').to_string();
            let cls = match &a[1] {
                Value::Class(c) => c.clone(),
                v => return vm.fail("TypeException", format!("Route.resource() argument 2 must be class, got {}", type_name(v))),
            };
            let opts = opt_dict(vm, &a, 2, "Route.resource")?;
            let names_of = |v: Option<Value>| -> Option<Vec<String>> {
                match v {
                    Some(Value::List(l)) => Some(l.borrow().iter().map(crate::builtins::plain_display).collect()),
                    _ => None,
                }
            };
            let only = names_of(dget(&opts, "only"));
            let except = names_of(dget(&opts, "except")).unwrap_or_default();
            let mut added = Vec::new();
            for (action, methods, suffix) in RESOURCE_ACTIONS {
                if only.as_ref().map(|o| !o.iter().any(|x| x == action)).unwrap_or(false) || except.iter().any(|x| x == action) {
                    continue;
                }
                match cls.methods.get(action) {
                    Some(m) if m.vis == Vis::Public => {}
                    _ => continue,
                }
                let h = list(vec![Value::Class(cls.clone()), s(action)]);
                let rv = add_route(vm, methods.iter().map(|m| m.to_string()).collect(), &format!("{}{}", rname, suffix), &h, "Route.resource")?;
                if let Value::Web(w) = &rv {
                    if let WebObj::Route(i) = **w {
                        ROUTER.with(|r| {
                            let mut r = r.borrow_mut();
                            let full = format!("{}{}.{}", r.routes[i].name_prefix, rname.replace('/', "."), action);
                            r.routes[i].name = Some(full.clone());
                            r.names.insert(full, i);
                        });
                    }
                }
                added.push(rv);
            }
            if added.is_empty() {
                return vm.fail("ValueException",
                    format!("Route.resource(): {} has none of the methods index, create, store, show, edit, update, destroy", cls.name));
            }
            Ok(list(added))
        }
        "Route.alias" => {
            let n = need_str(vm, &a, 0, "Route.alias")?;
            let ok = n.chars().next().map(|c| c.is_ascii_alphabetic() || c == '_').unwrap_or(false)
                && n.chars().all(|c| c.is_ascii_alphanumeric() || "_.-".contains(c));
            if !ok {
                return vm.fail("ValueException", format!("Route.alias(): invalid middleware name \"{}\"", n));
            }
            if matches!(a[1], Value::Str(_)) || mw_list(vm, &a[1], "Route.alias")?.is_empty() {
                return vm.fail("TypeException", "Route.alias() needs a middleware class or function as argument 2");
            }
            ROUTER.with(|r| r.borrow_mut().aliases.insert(n, a[1].clone()));
            Ok(Value::Null)
        }
        "Route.globalMiddleware" => {
            let ms = mw_list(vm, &a[0], "Route.globalMiddleware")?;
            ROUTER.with(|r| r.borrow_mut().global_mw.extend(ms));
            Ok(Value::Null)
        }
        "Route.fallback" => {
            let h = check_handler(vm, &a[0], "Route.fallback")?;
            ROUTER.with(|r| r.borrow_mut().fallback = Some(h));
            Ok(Value::Null)
        }
        "Route.dispatch" => {
            let req = as_request(vm, a.first(), "Route.dispatch")?;
            dispatch(vm, &req)
        }
        "Route.url" => route_url(vm, &a),
        "Route.has" => {
            let n = need_str(vm, &a, 0, "Route.has")?;
            Ok(Value::Bool(ROUTER.with(|r| r.borrow().names.contains_key(&n))))
        }
        "Route.list" => route_list(vm),
        "Route.clear" => {
            ROUTER.with(|r| *r.borrow_mut() = RouterState::default());
            Ok(Value::Null)
        }
        "Route.serve" => vm.fail("NativeError", "Route.serve() is not supported by the native VM yet"),
        _ => vm.fail("NativeError", format!("{}() is not supported by the native VM yet", name)),
    }
}

pub fn web_method(vm: &mut Vm, w: &Rc<WebObj>, name: &str, a: Vec<Value>) -> R<Value> {
    match &**w {
        WebObj::Request(_) => request_method(vm, w, name, a),
        WebObj::Route(i) => route_method(vm, w, *i, name, a),
        WebObj::Session(ss) => session_method(vm, ss, name, a),
        WebObj::Client(_) => client_method(vm, w, name, a),
        WebObj::Next(_) => vm.fail("NameException", format!("karo has no method '{}'", name)),
    }
}
