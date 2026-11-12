//! stdlib2: Csv, Validation, Config, Response (HttpRsp), Database stub, Http.

use crate::code::*;
use crate::value::*;
use crate::vm::{Vm, R};
use std::cell::RefCell;
use std::rc::Rc;

// ---------------------------------------------------------------------------
// shared state structs (exported to value.rs)
// ---------------------------------------------------------------------------

pub struct HttpRspState {
    pub status: i64,
    pub body: String,
    pub headers: Vec<(String, String)>,
    pub cookies: Vec<String>,
}

pub struct ValidatorState {
    pub errors: Vec<(String, Vec<String>)>,
    pub validated: Vec<(String, Value)>,
}

// ---------------------------------------------------------------------------
// Config state (thread-local, pre-loaded from JSON bundle)
// ---------------------------------------------------------------------------

thread_local! {
    static CONFIG: RefCell<Option<OMap>> = const { RefCell::new(None) };
}

pub fn init_config(j: &crate::json::J) {
    let v = j_to_value(j);
    let m = match v {
        Value::Dict(d) => {
            let m = d.borrow().clone();
            m
        }
        _ => OMap::new(false),
    };
    CONFIG.with(|c| *c.borrow_mut() = Some(m));
}

fn j_to_value(j: &crate::json::J) -> Value {
    crate::builtins::json_decode_j(j)
}

pub fn config_lookup(key: &str) -> (bool, Value) {
    CONFIG.with(|c| {
        let borrow = c.borrow();
        let map = match &*borrow {
            Some(m) => m,
            None => return (false, Value::Null),
        };
        // dot-path traversal
        let parts: Vec<&str> = key.split('.').collect();
        let mut cur: Value = Value::Dict(Rc::new(RefCell::new(map.clone())));
        for part in parts {
            match cur {
                Value::Dict(d) => {
                    let borrow = d.borrow();
                    match borrow.get(&HKey::Str(Rc::new(BStr::new(part.to_string())))) {
                        Some(v) => cur = v.clone(),
                        None => return (false, Value::Null),
                    }
                }
                _ => return (false, Value::Null),
            }
        }
        (true, cur)
    })
}

fn config_set_path(key: &str, value: Value) {
    CONFIG.with(|c| {
        let mut borrow = c.borrow_mut();
        if borrow.is_none() {
            *borrow = Some(OMap::new(false));
        }
        let map = borrow.as_mut().unwrap();
        let parts: Vec<&str> = key.split('.').collect();
        set_nested_config(map, &parts, value);
    });
}

fn set_nested_config(map: &mut OMap, parts: &[&str], value: Value) {
    if parts.is_empty() { return; }
    let key = parts[0];
    let hk = HKey::Str(Rc::new(BStr::new(key.to_string())));
    let sk = s(key.to_string());
    if parts.len() == 1 {
        map.insert(hk, sk, value);
    } else {
        let child = match map.get(&hk) {
            Some(Value::Dict(d)) => d.clone(),
            _ => {
                let d = Rc::new(RefCell::new(OMap::new(false)));
                map.insert(hk, sk, Value::Dict(d.clone()));
                d
            }
        };
        set_nested_config(&mut child.borrow_mut(), &parts[1..], value);
    }
}

// ---------------------------------------------------------------------------
// Config ops
// ---------------------------------------------------------------------------

pub fn config_op(vm: &mut Vm, op: ConfigOp, args: Vec<Value>) -> R<Value> {
    match op {
        ConfigOp::Get => {
            let key = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Config.get() argument 1 must be string, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Config.get() needs a key"),
            };
            let (found, v) = config_lookup(&key);
            if found { Ok(v) } else {
                Ok(args.get(1).cloned().unwrap_or(Value::Null))
            }
        }
        ConfigOp::Set => {
            let key = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                _ => return vm.fail("TypeException", "Config.set() needs a string key"),
            };
            let value = args.get(1).cloned().unwrap_or(Value::Null);
            config_set_path(&key, value);
            Ok(Value::Null)
        }
        ConfigOp::Has => {
            let key = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                _ => return vm.fail("TypeException", "Config.has() needs a string key"),
            };
            let (found, _) = config_lookup(&key);
            Ok(Value::Bool(found))
        }
        ConfigOp::All => {
            let m = CONFIG.with(|c| c.borrow().clone().unwrap_or_else(|| OMap::new(false)));
            Ok(Value::Dict(Rc::new(RefCell::new(m))))
        }
    }
}

// ---------------------------------------------------------------------------
// CSV
// ---------------------------------------------------------------------------

fn csv_escape(s: &str, sep: char) -> String {
    let needs_quote = s.contains(sep) || s.contains('"') || s.contains('\n') || s.contains('\r');
    if !needs_quote {
        return s.to_string();
    }
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        if c == '"' { out.push('"'); }
        out.push(c);
    }
    out.push('"');
    out
}

fn csv_cell(v: &Value, sep: char) -> String {
    match v {
        Value::Null => String::new(),
        Value::Bool(true) => csv_escape("true", sep),
        Value::Bool(false) => csv_escape("false", sep),
        Value::Int(n) => n.to_string(),
        Value::Float(f) => crate::builtins::format_float(*f),
        Value::Str(s) => csv_escape(&s.s, sep),
        _ => csv_escape(&crate::builtins::plain_display(v), sep),
    }
}

fn parse_csv(text: &str, header: bool, sep: char) -> Result<Value, String> {
    let rows = parse_csv_raw(text, sep)?;
    if rows.is_empty() {
        return Ok(list(Vec::new()));
    }
    if !header {
        let items: Vec<Value> = rows.into_iter()
            .map(|row| list(row.into_iter().map(|c| s(c)).collect()))
            .collect();
        return Ok(list(items));
    }
    let headers = rows[0].clone();
    let ncols = headers.len();
    let mut out = Vec::new();
    for (ri, row) in rows[1..].iter().enumerate() {
        if row.len() != ncols {
            return Err(format!("Csv.parse(): row {} has {} fields, the header has {}",
                ri + 2, row.len(), ncols));
        }
        let mut m = OMap::new(false);
        for (k, v) in headers.iter().zip(row.iter()) {
            m.insert(HKey::Str(Rc::new(BStr::new(k.clone()))), s(k.clone()), s(v.clone()));
        }
        out.push(Value::Dict(Rc::new(RefCell::new(m))));
    }
    Ok(list(out))
}

fn parse_csv_raw(text: &str, sep: char) -> Result<Vec<Vec<String>>, String> {
    let mut rows: Vec<Vec<String>> = Vec::new();
    let mut row: Vec<String> = Vec::new();
    let mut field = String::new();
    let mut in_quote = false;
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if in_quote {
            if c == '"' {
                if i + 1 < chars.len() && chars[i + 1] == '"' {
                    field.push('"');
                    i += 2;
                    continue;
                }
                in_quote = false;
            } else {
                field.push(c);
            }
        } else if c == '"' {
            in_quote = true;
        } else if c == sep {
            row.push(field.clone());
            field.clear();
        } else if c == '\n' {
            row.push(field.clone());
            field.clear();
            if !row.is_empty() {
                rows.push(row.clone());
            }
            row.clear();
        } else if c != '\r' {
            field.push(c);
        }
        i += 1;
    }
    row.push(field);
    if row.iter().any(|s| !s.is_empty()) {
        rows.push(row);
    }
    Ok(rows)
}

fn encode_csv(vm: &mut Vm, rows: &[Value], sep: char) -> R<String> {
    if rows.is_empty() {
        return Ok(String::new());
    }
    let mut out = String::new();
    // detect if dict rows (use union of keys as header)
    let is_dict = matches!(rows[0], Value::Dict(_));
    if is_dict {
        // collect all keys in order of first appearance
        let mut header: Vec<String> = Vec::new();
        for row in rows {
            if let Value::Dict(d) = row {
                for (k, _) in d.borrow().iter() {
                    if let Value::Str(ks) = k {
                        if !header.contains(&ks.s) {
                            header.push(ks.s.clone());
                        }
                    }
                }
            }
        }
        // header row
        let header_row: Vec<String> = header.iter().map(|k| csv_escape(k, sep)).collect();
        out.push_str(&header_row.join(&sep.to_string()));
        out.push('\n');
        for row in rows {
            if let Value::Dict(d) = row {
                let d = d.borrow();
                let cells: Vec<String> = header.iter().map(|k| {
                    let hk = HKey::Str(Rc::new(BStr::new(k.clone())));
                    match d.get(&hk) {
                        Some(v) => csv_cell(v, sep),
                        None => String::new(),
                    }
                }).collect();
                out.push_str(&cells.join(&sep.to_string()));
                out.push('\n');
            } else {
                return vm.fail("TypeException", "Csv.encode(): all rows must be the same type (dict or list)");
            }
        }
    } else {
        for row in rows {
            if let Value::List(l) = row {
                let cells: Vec<String> = l.borrow().iter().map(|v| csv_cell(v, sep)).collect();
                out.push_str(&cells.join(&sep.to_string()));
                out.push('\n');
            } else {
                return vm.fail("TypeException", "Csv.encode(): rows must be lists or dicts");
            }
        }
    }
    Ok(out)
}

fn csv_sep(vm: &mut Vm, args: &[Value], idx: usize) -> R<char> {
    match args.get(idx) {
        None | Some(Value::Null) => Ok(','),
        Some(Value::Str(s)) if s.chars == 1 => Ok(s.s.chars().next().unwrap()),
        Some(v) => vm.fail("TypeException",
            format!("CSV separator must be a single character, got {}", type_name(v))),
    }
}

pub fn csv_op(vm: &mut Vm, op: CsvOp, args: Vec<Value>) -> R<Value> {
    match op {
        CsvOp::Parse => {
            let text = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Csv.parse() argument 1 must be string, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Csv.parse() needs text"),
            };
            let header = match args.get(1) {
                None | Some(Value::Null) => true,
                Some(Value::Bool(b)) => *b,
                Some(v) => return vm.fail("TypeException",
                    format!("Csv.parse() argument 2 must be bool, got {}", type_name(v))),
            };
            let sep = csv_sep(vm, &args, 2)?;
            parse_csv(&text, header, sep).map_err(|e| vm.exc("CsvException", e))
        }
        CsvOp::Encode => {
            let rows = match args.first() {
                Some(Value::List(l)) => l.borrow().clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Csv.encode() argument 1 must be list, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Csv.encode() needs rows"),
            };
            let sep = csv_sep(vm, &args, 1)?;
            let text = encode_csv(vm, &rows, sep)?;
            Ok(s(text))
        }
        CsvOp::Write => {
            let path = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Csv.write() argument 1 must be string, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Csv.write() needs a path"),
            };
            let rows = match args.get(1) {
                Some(Value::List(l)) => l.borrow().clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Csv.write() argument 2 must be list, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Csv.write() needs rows"),
            };
            let sep = csv_sep(vm, &args, 2)?;
            let nrows = rows.len();
            let text = encode_csv(vm, &rows, sep)?;
            match std::fs::write(&path, &text) {
                Ok(_) => Ok(Value::Int(nrows as i64)),
                Err(e) => vm.fail("IOException", format!("Csv.write(): {}: {}", path, e)),
            }
        }
        CsvOp::Read => {
            let path = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Csv.read() argument 1 must be string, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Csv.read() needs a path"),
            };
            let header = match args.get(1) {
                None | Some(Value::Null) => true,
                Some(Value::Bool(b)) => *b,
                Some(v) => return vm.fail("TypeException",
                    format!("Csv.read() argument 2 must be bool, got {}", type_name(v))),
            };
            let sep = csv_sep(vm, &args, 2)?;
            let text = match std::fs::read_to_string(&path) {
                Ok(t) => t,
                Err(e) => return vm.fail("IOException", format!("Csv.read(): {}: {}", path, e)),
            };
            parse_csv(&text, header, sep).map_err(|e| vm.exc("CsvException", e))
        }
    }
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

static EMAIL_RE_PAT: &str = r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$";

fn is_email(s: &str) -> bool {
    // simple hand-written check matching the Python EMAIL_RE
    let (local, domain) = match s.split_once('@') {
        Some(p) => p,
        None => return false,
    };
    if local.is_empty() { return false; }
    for c in local.chars() {
        if !matches!(c, 'A'..='Z' | 'a'..='z' | '0'..='9'
            | '.' | '!' | '#' | '$' | '%' | '&' | '\'' | '*' | '+'
            | '/' | '=' | '?' | '^' | '_' | '`' | '{' | '|' | '}' | '~' | '-') {
            return false;
        }
    }
    if domain.is_empty() { return false; }
    let labels: Vec<&str> = domain.split('.').collect();
    if labels.len() < 2 { return false; }
    for label in &labels {
        if label.is_empty() || label.len() > 63 { return false; }
        if label.starts_with('-') || label.ends_with('-') { return false; }
        for c in label.chars() {
            if !matches!(c, 'A'..='Z' | 'a'..='z' | '0'..='9' | '-') { return false; }
        }
    }
    true
}

fn is_url(s: &str) -> bool {
    let lower = s.to_ascii_lowercase();
    if !lower.starts_with("http://") && !lower.starts_with("https://") { return false; }
    let rest = &s[s.find("://").unwrap() + 3..];
    if rest.is_empty() || rest.starts_with('/') { return false; }
    // must have at least one non-whitespace char
    !rest.chars().any(|c| c.is_whitespace())
}

fn is_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    if b.len() != 36 { return false; }
    let dashes = [8, 13, 18, 23];
    for (i, c) in b.iter().enumerate() {
        if dashes.contains(&i) {
            if *c != b'-' { return false; }
        } else if !c.is_ascii_hexdigit() { return false; }
    }
    true
}

fn is_int_str(s: &str) -> bool {
    let s = s.trim();
    if s.is_empty() { return false; }
    let s = if s.starts_with('+') || s.starts_with('-') { &s[1..] } else { s };
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

fn is_num_str(s: &str) -> bool {
    let s = s.trim();
    if s.is_empty() { return false; }
    let s = if s.starts_with('+') || s.starts_with('-') { &s[1..] } else { s };
    if s.is_empty() { return false; }
    // optional integer part, optional decimal part, optional exponent
    let mut rest = s;
    // digits before decimal
    let before: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    rest = &rest[before.len()..];
    if rest.starts_with('.') {
        rest = &rest[1..];
        let after: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
        rest = &rest[after.len()..];
        if before.is_empty() && after.is_empty() { return false; }
    } else if before.is_empty() {
        return false;
    }
    if rest.starts_with('e') || rest.starts_with('E') {
        rest = &rest[1..];
        if rest.starts_with('+') || rest.starts_with('-') { rest = &rest[1..]; }
        if rest.is_empty() || !rest.bytes().all(|b| b.is_ascii_digit()) { return false; }
    } else if !rest.is_empty() {
        return false;
    }
    true
}

fn parse_date_str(s: &str) -> bool {
    let s = s.trim();
    // try YYYY-MM-DD
    if s.len() >= 10 {
        let parts: Vec<&str> = s[..10].split('-').collect();
        if parts.len() == 3 {
            if let (Ok(y), Ok(m), Ok(d)) = (parts[0].parse::<i32>(), parts[1].parse::<u32>(), parts[2].parse::<u32>()) {
                if m >= 1 && m <= 12 && d >= 1 && d <= 31 {
                    // valid enough for the test; more precise check for month day counts
                    let max_day = match m {
                        2 => if y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) { 29 } else { 28 },
                        4 | 6 | 9 | 11 => 30,
                        _ => 31,
                    };
                    if d <= max_day { return true; }
                }
            }
        }
    }
    false
}

fn is_date_value(v: &Value) -> bool {
    match v {
        Value::Int(_) => true,
        Value::Str(s) => parse_date_str(&s.s),
        _ => false,
    }
}

fn value_to_num(v: &Value) -> Option<f64> {
    match v {
        Value::Int(n) => Some(*n as f64),
        Value::Float(f) => Some(*f),
        Value::Str(s) => {
            let t = s.s.trim();
            if is_num_str(t) { t.parse::<f64>().ok() } else { None }
        }
        _ => None,
    }
}

fn value_measure(v: &Value, numeric: bool) -> Option<(&'static str, f64)> {
    match v {
        Value::Int(n) if numeric => Some(("number", *n as f64)),
        Value::Float(f) if numeric => Some(("number", *f)),
        Value::Str(s) => {
            if numeric {
                if let Some(n) = value_to_num(v) {
                    return Some(("number", n));
                }
            }
            Some(("string", s.chars as f64))
        }
        Value::List(l) => Some(("list", l.borrow().len() as f64)),
        Value::Dict(d) => Some(("list", d.borrow().len() as f64)),
        Value::Int(n) => Some(("number", *n as f64)),
        Value::Float(f) => Some(("number", *f)),
        _ => None,
    }
}

fn is_empty_val(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::Str(s) => s.s.trim().is_empty(),
        Value::List(l) => l.borrow().is_empty(),
        Value::Dict(d) => d.borrow().len() == 0,
        _ => false,
    }
}

fn value_display_simple(v: &Value) -> String {
    match v {
        Value::Int(n) => n.to_string(),
        Value::Float(f) => crate::builtins::format_float(*f),
        Value::Bool(b) => b.to_string(),
        Value::Str(s) => s.s.clone(),
        Value::Null => String::new(),
        _ => String::new(),
    }
}

fn value_lookup_path<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    // We can't return references into inner cells safely; return owned
    None // placeholder - we use owned version below
}

fn lookup_path_owned(v: &Value, key: &str) -> (bool, Value) {
    let parts: Vec<&str> = key.split('.').collect();
    let mut cur = v.clone();
    for part in parts {
        match cur {
            Value::Dict(d) => {
                let hk = HKey::Str(Rc::new(BStr::new(part.to_string())));
                match d.borrow().get(&hk).cloned() {
                    Some(nxt) => cur = nxt,
                    None => return (false, Value::Null),
                }
            }
            _ => return (false, Value::Null),
        }
    }
    (true, cur)
}

#[derive(Debug)]
struct ValidationRule {
    name: String,
    args: Vec<String>,
}

fn split_rules(spec: &Value) -> Result<Vec<ValidationRule>, String> {
    let rule_strs: Vec<String> = match spec {
        Value::Str(s) => {
            let mut parts = Vec::new();
            let mut rest = s.s.as_str();
            while !rest.is_empty() {
                if rest.to_ascii_lowercase().starts_with("regex:") {
                    parts.push(rest.to_string());
                    break;
                }
                match rest.find('|') {
                    Some(i) => { parts.push(rest[..i].to_string()); rest = &rest[i+1..]; }
                    None => { parts.push(rest.to_string()); break; }
                }
            }
            parts
        }
        Value::List(l) => {
            l.borrow().iter().map(|v| match v {
                Value::Str(s) => Ok(s.s.clone()),
                _ => Err(format!("each validation rule must be a string, got {}", type_name(v))),
            }).collect::<Result<Vec<_>, _>>()?
        }
        _ => return Err(format!("validation rules must be a string or list, got {}", type_name(spec))),
    };

    let canonical: std::collections::HashMap<&str, &str> = [
        ("required","required"),("nullable","nullable"),("sometimes","sometimes"),
        ("string","string"),("int","int"),("integer","int"),("numeric","numeric"),
        ("float","numeric"),("bool","bool"),("boolean","bool"),
        ("email","email"),("url","url"),("uuid","uuid"),("alpha","alpha"),
        ("alphanum","alphaNum"),("alphadash","alphaDash"),("min","min"),
        ("max","max"),("between","between"),("size","size"),("in","in"),
        ("notin","notIn"),("regex","regex"),("same","same"),("different","different"),
        ("confirmed","confirmed"),("date","date"),("list","list"),("array","list"),
        ("dict","dict"),("accepted","accepted"),("digits","digits"),
        ("startswith","startsWith"),("endswith","endsWith"),("json","json"),
        ("unique","unique"),("exists","exists"),("file","file"),("image","image"),
        ("mimes","mimes"),("mimetypes","mimetypes"),("extensions","extensions"),
        ("dimensions","dimensions"),
    ].iter().cloned().collect();

    let mut out = Vec::new();
    for part in rule_strs {
        let part = part.trim().to_string();
        if part.is_empty() { continue; }
        let (name_part, args_part) = match part.find(':') {
            Some(i) => (&part[..i], &part[i+1..]),
            None => (part.as_str(), ""),
        };
        let key = name_part.replace('_', "").to_ascii_lowercase();
        let canon = match canonical.get(key.as_str()) {
            Some(&c) => c.to_string(),
            None => return Err(format!("unknown validation rule \"{}\"", name_part)),
        };
        let args: Vec<String> = if canon == "regex" {
            vec![args_part.to_string()]
        } else if args_part.is_empty() {
            vec![]
        } else {
            args_part.split(',').map(|a| a.trim().to_string()).collect()
        };
        out.push(ValidationRule { name: canon, args });
    }
    Ok(out)
}

fn msg_template(key: &str, label: &str, ph: &[(&str, &str)]) -> String {
    let tmpl = match key {
        "required" => "The :field field is required.",
        "string" => "The :field field must be a string.",
        "int" => "The :field field must be an integer.",
        "numeric" => "The :field field must be a number.",
        "bool" => "The :field field must be true or false.",
        "email" => "The :field field must be a valid email address.",
        "url" => "The :field field must be a valid URL.",
        "uuid" => "The :field field must be a valid UUID.",
        "alpha" => "The :field field must only contain letters.",
        "alphaNum" => "The :field field must only contain letters and numbers.",
        "alphaDash" => "The :field field must only contain letters, numbers, dashes and underscores.",
        "min.number" => "The :field field must be at least :min.",
        "min.string" => "The :field field must be at least :min characters.",
        "min.list" => "The :field field must have at least :min items.",
        "max.number" => "The :field field must not be greater than :max.",
        "max.string" => "The :field field must not be longer than :max characters.",
        "max.list" => "The :field field must not have more than :max items.",
        "between.number" => "The :field field must be between :min and :max.",
        "between.string" => "The :field field must be between :min and :max characters.",
        "between.list" => "The :field field must have between :min and :max items.",
        "size.number" => "The :field field must be :size.",
        "size.string" => "The :field field must be :size characters.",
        "size.list" => "The :field field must contain :size items.",
        "in" | "notIn" => "The selected :field is invalid.",
        "regex" => "The :field field format is invalid.",
        "same" => "The :field field must match :other.",
        "different" => "The :field field must be different from :other.",
        "confirmed" => "The :field field confirmation does not match.",
        "date" => "The :field field must be a valid date.",
        "list" => "The :field field must be a list.",
        "dict" => "The :field field must be a dict.",
        "accepted" => "The :field field must be accepted.",
        "digits" => "The :field field must be :digits digits.",
        "startsWith" => "The :field field must start with :values.",
        "endsWith" => "The :field field must end with :values.",
        "json" => "The :field field must be valid JSON.",
        _ => "The :field field is invalid.",
    };
    let mut out = tmpl.replace(":field", label);
    for (k, v) in ph {
        out = out.replace(&format!(":{}", k), v);
    }
    out
}

fn check_rule(rule: &ValidationRule, value: &Value, data: &Value, field: &str, numeric: bool) -> Option<(String, Vec<(&'static str, String)>)> {
    let name = rule.name.as_str();
    let args = &rule.args;
    let ok = match name {
        "string" => matches!(value, Value::Str(_)),
        "int" => match value {
            Value::Int(_) => true,
            Value::Float(_) => false,
            Value::Str(s) => is_int_str(s.s.trim()),
            _ => false,
        },
        "numeric" => match value {
            Value::Int(_) | Value::Float(_) => true,
            Value::Str(s) => is_num_str(s.s.trim()),
            _ => false,
        },
        "bool" => match value {
            Value::Bool(_) => true,
            Value::Int(n) => *n == 0 || *n == 1,
            Value::Str(s) => matches!(s.s.to_ascii_lowercase().as_str(), "true" | "false" | "0" | "1"),
            _ => false,
        },
        "accepted" => match value {
            Value::Bool(true) => true,
            Value::Int(1) => true,
            Value::Str(s) => matches!(s.s.to_ascii_lowercase().as_str(), "yes" | "on" | "1" | "true"),
            _ => false,
        },
        "email" => matches!(value, Value::Str(s) if is_email(&s.s)),
        "url" => matches!(value, Value::Str(s) if is_url(&s.s)),
        "uuid" => matches!(value, Value::Str(s) if is_uuid(&s.s)),
        "alpha" => matches!(value, Value::Str(s) if s.s.chars().all(|c| c.is_alphabetic())),
        "alphaNum" => matches!(value, Value::Str(s) if s.s.chars().all(|c| c.is_alphanumeric())),
        "alphaDash" => matches!(value, Value::Str(s) if s.s.chars().all(|c| c.is_alphanumeric() || c == '-' || c == '_')),
        "date" => is_date_value(value),
        "list" => matches!(value, Value::List(_)),
        "dict" => matches!(value, Value::Dict(_)),
        "json" => matches!(value, Value::Str(s) if crate::json::parse(&s.s).is_ok()),
        "digits" => {
            let n: usize = args[0].parse::<f64>().map(|f| f as usize).unwrap_or(0);
            match value {
                Value::Str(s) => s.s.bytes().all(|b| b.is_ascii_digit()) && s.s.len() == n,
                Value::Int(i) => *i >= 0 && i.to_string().len() == n,
                _ => false,
            }
        }
        "in" => {
            let sv = value_display_simple(value);
            args.contains(&sv)
        }
        "notIn" => {
            let sv = value_display_simple(value);
            !args.contains(&sv)
        }
        "startsWith" => matches!(value, Value::Str(s) if args.iter().any(|a| s.s.starts_with(a.as_str()))),
        "endsWith" => matches!(value, Value::Str(s) if args.iter().any(|a| s.s.ends_with(a.as_str()))),
        "regex" => {
            if let Value::Str(sv) = value {
                let pat = args[0].as_str();
                // pat may be /pattern/flags or bare pattern
                let (re_pat, _flags) = if pat.starts_with('/') {
                    let end = pat[1..].rfind('/').map(|i| i + 1).unwrap_or(pat.len() - 1);
                    (&pat[1..end], &pat[end+1..])
                } else {
                    (pat, "")
                };
                match crate::stdlib::parse_regex(re_pat) {
                    Ok(rx) => crate::stdlib::regex_search(&rx, &sv.s).is_some(),
                    Err(_) => false,
                }
            } else { false }
        }
        "same" => {
            let (found, other) = lookup_path_owned(data, &args[0]);
            if !found { return Some(("same".to_string(), vec![])); }
            values_equal(value, &other)
        }
        "different" => {
            let (found, other) = lookup_path_owned(data, &args[0]);
            if !found { return None; }
            !values_equal(value, &other)
        }
        "confirmed" => {
            let key2 = format!("{}_confirmation", field);
            let (found, other) = lookup_path_owned(data, &key2);
            if !found { return Some(("confirmed".to_string(), vec![])); }
            values_equal(value, &other)
        }
        "min" | "max" | "size" | "between" => {
            let (kind, sz) = match value_measure(value, numeric) {
                Some(x) => x,
                None => return Some((format!("{}.string", name), vec![])),
            };
            let min_v: f64 = args[0].parse().unwrap_or(0.0);
            let max_v: f64 = if name == "between" { args[1].parse().unwrap_or(0.0) } else { min_v };
            let pass = match name {
                "min" => sz >= min_v,
                "max" => sz <= min_v,
                "size" => sz == min_v,
                "between" => sz >= min_v && sz <= max_v,
                _ => unreachable!(),
            };
            if pass { return None; }
            let key = format!("{}.{}", name, kind);
            let ph = vec![("min", args[0].clone()), ("max", args.last().unwrap_or(&args[0]).clone()), ("size", args[0].clone())];
            return Some((key, ph.into_iter().map(|(k, v)| (k, v)).collect()));
        }
        _ => return None,
    };
    if ok { None } else { Some((name.to_string(), vec![])) }
}

fn run_validation(vm: &mut Vm, data: &Value, rules_dict: &Value, messages_dict: &Value) -> R<ValidatorState> {
    let rules_map = match rules_dict {
        Value::Dict(d) => d.clone(),
        v => return vm.fail("TypeException",
            format!("Validation.make() argument 2 must be dict, got {}", type_name(v))),
    };
    let msgs_map = match messages_dict {
        Value::Dict(d) => Some(d.clone()),
        Value::Null => None,
        v => return vm.fail("TypeException",
            format!("Validation.make() argument 3 must be dict, got {}", type_name(v))),
    };

    let mut errors: Vec<(String, Vec<String>)> = Vec::new();
    let mut validated: Vec<(String, Value)> = Vec::new();

    let rules_borrow = rules_map.borrow();
    for (field_v, spec) in rules_borrow.iter() {
        let field = match field_v {
            Value::Str(s) => s.s.clone(),
            _ => return vm.fail("TypeException", "validation field names must be strings"),
        };
        let parsed = split_rules(spec).map_err(|e| vm.exc("ValueException", e))?;
        let names: std::collections::HashSet<&str> = parsed.iter().map(|r| r.name.as_str()).collect();
        let (present, value) = lookup_path_owned(data, &field);
        let label = field.replace('_', " ").replace('.', " ");
        let mut fails: Vec<String> = Vec::new();

        let custom_msg = |rule: &str| -> Option<String> {
            if let Some(ref msgs) = msgs_map {
                let key1 = format!("{}.{}", field, rule);
                let hk1 = HKey::Str(Rc::new(BStr::new(key1)));
                if let Some(Value::Str(s)) = msgs.borrow().get(&hk1) {
                    return Some(s.s.clone());
                }
                let hk2 = HKey::Str(Rc::new(BStr::new(rule.to_string())));
                if let Some(Value::Str(s)) = msgs.borrow().get(&hk2) {
                    return Some(s.s.clone());
                }
            }
            None
        };

        let make_msg = |key: &str, ph: &Vec<(&'static str, String)>, rule: &str| -> String {
            if let Some(custom) = custom_msg(rule) {
                let mut out = custom.replace(":field", &label);
                for (k, v) in ph { out = out.replace(&format!(":{}", k), v); }
                return out;
            }
            msg_template(key, &label, &ph.iter().map(|(k, v)| (*k, v.as_str())).collect::<Vec<_>>())
        };

        if names.contains("sometimes") && !present {
            continue;
        }
        if is_empty_val(&value) {
            if names.contains("required") {
                fails.push(make_msg("required", &vec![], "required"));
            } else if names.contains("accepted") {
                fails.push(make_msg("accepted", &vec![], "accepted"));
            }
            if !fails.is_empty() {
                errors.push((field, fails));
            } else if present {
                validated.push((field, value));
            }
            continue;
        }
        let numeric = names.iter().any(|n| matches!(*n, "int" | "integer" | "numeric" | "float"));
        for rule in &parsed {
            if matches!(rule.name.as_str(), "required" | "nullable" | "sometimes") { continue; }
            if let Some((key, ph)) = check_rule(rule, &value, data, &field, numeric) {
                fails.push(make_msg(&key, &ph, &rule.name));
            }
        }
        if !fails.is_empty() {
            errors.push((field, fails));
        } else {
            validated.push((field, value));
        }
    }
    Ok(ValidatorState { errors, validated })
}

fn validator_to_value(state: ValidatorState) -> Value {
    Value::Validator(Rc::new(state))
}

fn errors_to_dict(errors: &Vec<(String, Vec<String>)>) -> Value {
    let mut m = OMap::new(false);
    for (field, msgs) in errors {
        let hk = HKey::Str(Rc::new(BStr::new(field.clone())));
        let msgs_list: Vec<Value> = msgs.iter().map(|m| s(m.clone())).collect();
        m.insert(hk, s(field.clone()), list(msgs_list));
    }
    Value::Dict(Rc::new(RefCell::new(m)))
}

fn validated_to_dict(validated: &Vec<(String, Value)>) -> Value {
    let mut m = OMap::new(false);
    for (field, val) in validated {
        let hk = HKey::Str(Rc::new(BStr::new(field.clone())));
        m.insert(hk, s(field.clone()), val.clone());
    }
    Value::Dict(Rc::new(RefCell::new(m)))
}

pub fn validation_op(vm: &mut Vm, op: ValidationOp, args: Vec<Value>) -> R<Value> {
    match op {
        ValidationOp::Make => {
            let data = args.first().cloned().unwrap_or(Value::Null);
            let rules = args.get(1).cloned().unwrap_or(Value::Null);
            let msgs = args.get(2).cloned().unwrap_or(Value::Null);
            let state = run_validation(vm, &data, &rules, &msgs)?;
            Ok(validator_to_value(state))
        }
        ValidationOp::Check => {
            let value = args.first().cloned().unwrap_or(Value::Null);
            let rules = args.get(1).cloned().unwrap_or(Value::Null);
            let data = {
                let mut m = OMap::new(false);
                m.insert(HKey::Str(Rc::new(BStr::new("value".to_string()))),
                    s("value"), value);
                Value::Dict(Rc::new(RefCell::new(m)))
            };
            let rules_dict = {
                let mut m = OMap::new(false);
                m.insert(HKey::Str(Rc::new(BStr::new("value".to_string()))),
                    s("value"), rules);
                Value::Dict(Rc::new(RefCell::new(m)))
            };
            let state = run_validation(vm, &data, &rules_dict, &Value::Null)?;
            Ok(Value::Bool(state.errors.is_empty()))
        }
        ValidationOp::Validate => {
            let data = args.first().cloned().unwrap_or(Value::Null);
            let rules = args.get(1).cloned().unwrap_or(Value::Null);
            let msgs = args.get(2).cloned().unwrap_or(Value::Null);
            let state = run_validation(vm, &data, &rules, &msgs)?;
            if !state.errors.is_empty() {
                let first_msg = state.errors[0].1.first().cloned().unwrap_or_default();
                let total: usize = state.errors.iter().map(|(_, v)| v.len()).sum();
                let more = total - 1;
                let msg = if more > 0 {
                    format!("{} (and {} more error{})", first_msg, more, if more == 1 { "" } else { "s" })
                } else {
                    first_msg
                };
                let errors_dict = errors_to_dict(&state.errors);
                return Err(vm.exc_with_field("ValidationException", &msg, "errors", errors_dict));
            }
            Ok(validated_to_dict(&state.validated))
        }
    }
}

pub fn validator_get_attr(state: &ValidatorState, name: &str) -> Option<Value> {
    None // accessed via methods
}

pub fn validator_method(vm: &mut Vm, state: &Rc<ValidatorState>, name: &str, args: Vec<Value>) -> R<Value> {
    match name {
        "fails" => Ok(Value::Bool(!state.errors.is_empty())),
        "passes" => Ok(Value::Bool(state.errors.is_empty())),
        "has" => {
            let field = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Validator.has() needs a string, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Validator.has() needs a field name"),
            };
            Ok(Value::Bool(state.errors.iter().any(|(f, _)| f == &field)))
        }
        "first" => {
            match args.first() {
                Some(Value::Str(s)) => {
                    let field = &s.s;
                    Ok(state.errors.iter().find(|(f, _)| f == field)
                        .and_then(|(_, msgs)| msgs.first())
                        .map(|m| crate::value::s(m.clone()))
                        .unwrap_or(Value::Null))
                }
                None | Some(Value::Null) => {
                    Ok(state.errors.first()
                        .and_then(|(_, msgs)| msgs.first())
                        .map(|m| crate::value::s(m.clone()))
                        .unwrap_or(Value::Null))
                }
                Some(v) => vm.fail("TypeException",
                    format!("Validator.first() needs a string, got {}", type_name(v))),
            }
        }
        "errors" => Ok(errors_to_dict(&state.errors)),
        "messages" => {
            let msgs: Vec<Value> = state.errors.iter()
                .flat_map(|(_, msgs)| msgs.iter().map(|m| crate::value::s(m.clone())))
                .collect();
            Ok(list(msgs))
        }
        "validated" => Ok(validated_to_dict(&state.validated)),
        _ => vm.fail("NameException", format!("Validator has no method '{}'", name)),
    }
}

// ---------------------------------------------------------------------------
// Response (HttpRsp)
// ---------------------------------------------------------------------------

fn check_status(vm: &mut Vm, status: i64) -> R<i64> {
    if !(100..=599).contains(&status) {
        return vm.fail("ValueException", format!("HTTP status must be 100-599, got {}", status));
    }
    Ok(status)
}

fn header_lookup(headers: &[(String, String)], name: &str) -> Option<String> {
    let lower = name.to_ascii_lowercase();
    headers.iter().find(|(k, _)| k.to_ascii_lowercase() == lower).map(|(_, v)| v.clone())
}

fn make_rsp(status: i64, body: String, headers: Vec<(String, String)>) -> Value {
    Value::HttpRsp(Rc::new(RefCell::new(HttpRspState { status, body, headers, cookies: Vec::new() })))
}

fn resp_make_inner(vm: &mut Vm, body: String, status: i64, mut headers: Vec<(String, String)>) -> R<Value> {
    let status = check_status(vm, status)?;
    if header_lookup(&headers, "Content-Type").is_none() {
        headers.insert(0, ("Content-Type".to_string(), "text/html; charset=utf-8".to_string()));
    }
    Ok(make_rsp(status, body, headers))
}

pub fn rsp_op(vm: &mut Vm, op: RspOp, args: Vec<Value>) -> R<Value> {
    match op {
        RspOp::Json => {
            let status = match args.get(1) {
                None | Some(Value::Null) => 200,
                Some(Value::Int(n)) => *n,
                Some(v) => return vm.fail("TypeException",
                    format!("Response.json() status must be int, got {}", type_name(v))),
            };
            let status = check_status(vm, status)?;
            let body = crate::builtins::json_encode_value(vm, &args[0], None, 0)?;
            Ok(make_rsp(status, body,
                vec![("Content-Type".to_string(), "application/json".to_string())]))
        }
        RspOp::Make => {
            let body = match args.first() {
                None | Some(Value::Null) => String::new(),
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Response.make() body must be string, got {}", type_name(v))),
            };
            let status = match args.get(1) {
                None | Some(Value::Null) => 200,
                Some(Value::Int(n)) => *n,
                Some(v) => return vm.fail("TypeException",
                    format!("Response.make() status must be int, got {}", type_name(v))),
            };
            let headers: Vec<(String, String)> = match args.get(2) {
                None | Some(Value::Null) => vec![],
                Some(Value::Dict(d)) => {
                    d.borrow().iter().filter_map(|(k, v)| match (k, v) {
                        (Value::Str(ks), Value::Str(vs)) => Some((ks.s.clone(), vs.s.clone())),
                        _ => None,
                    }).collect()
                }
                Some(v) => return vm.fail("TypeException",
                    format!("Response.make() headers must be dict, got {}", type_name(v))),
            };
            resp_make_inner(vm, body, status, headers)
        }
        RspOp::Html => {
            let body = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Response.html() body must be string, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Response.html() needs a body"),
            };
            let status = match args.get(1) {
                None | Some(Value::Null) => 200,
                Some(Value::Int(n)) => *n,
                Some(v) => return vm.fail("TypeException",
                    format!("Response.html() status must be int, got {}", type_name(v))),
            };
            let status = check_status(vm, status)?;
            Ok(make_rsp(status, body,
                vec![("Content-Type".to_string(), "text/html; charset=utf-8".to_string())]))
        }
        RspOp::Text => {
            let body = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Response.text() body must be string, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Response.text() needs a body"),
            };
            let status = match args.get(1) {
                None | Some(Value::Null) => 200,
                Some(Value::Int(n)) => *n,
                Some(v) => return vm.fail("TypeException",
                    format!("Response.text() status must be int, got {}", type_name(v))),
            };
            let status = check_status(vm, status)?;
            Ok(make_rsp(status, body,
                vec![("Content-Type".to_string(), "text/plain; charset=utf-8".to_string())]))
        }
        RspOp::Redirect => {
            let url = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Response.redirect() url must be string, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Response.redirect() needs a URL"),
            };
            let status = match args.get(1) {
                None | Some(Value::Null) => 302,
                Some(Value::Int(n)) => *n,
                Some(v) => return vm.fail("TypeException",
                    format!("Response.redirect() status must be int, got {}", type_name(v))),
            };
            if ![301i64, 302, 303, 307, 308].contains(&status) {
                return vm.fail("ValueException",
                    format!("Response.redirect() status must be 301, 302, 303, 307 or 308, got {}", status));
            }
            Ok(make_rsp(status, String::new(), vec![("Location".to_string(), url)]))
        }
        RspOp::NoContent => {
            Ok(make_rsp(204, String::new(), vec![]))
        }
        RspOp::New => {
            // constructor: new Response(body, status, headers?)
            let body = match args.first() {
                None | Some(Value::Null) => String::new(),
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Response constructor body must be string, got {}", type_name(v))),
            };
            let status = match args.get(1) {
                None | Some(Value::Null) => 200,
                Some(Value::Int(n)) => *n,
                Some(v) => return vm.fail("TypeException",
                    format!("Response constructor status must be int, got {}", type_name(v))),
            };
            let headers: Vec<(String, String)> = match args.get(2) {
                None | Some(Value::Null) => vec![],
                Some(Value::Dict(d)) => {
                    d.borrow().iter().filter_map(|(k, v)| match (k, v) {
                        (Value::Str(ks), Value::Str(vs)) => Some((ks.s.clone(), vs.s.clone())),
                        _ => None,
                    }).collect()
                }
                Some(v) => return vm.fail("TypeException",
                    format!("Response constructor headers must be dict, got {}", type_name(v))),
            };
            resp_make_inner(vm, body, status, headers)
        }
    }
}

pub fn http_rsp_get_attr(rsp: &Rc<RefCell<HttpRspState>>, name: &str) -> Option<Value> {
    let r = rsp.borrow();
    match name {
        "status" => Some(Value::Int(r.status)),
        "body" => Some(s(r.body.clone())),
        "headers" => {
            let mut m = OMap::new(false);
            for (k, v) in &r.headers {
                let hk = HKey::Str(Rc::new(BStr::new(k.clone())));
                m.insert(hk, s(k.clone()), s(v.clone()));
            }
            Some(Value::Dict(Rc::new(RefCell::new(m))))
        }
        _ => None,
    }
}

pub fn http_rsp_method(vm: &mut Vm, rsp: &Rc<RefCell<HttpRspState>>, name: &str, args: Vec<Value>) -> R<Value> {
    if let Some(v) = crate::web::rsp_cookie_method(vm, rsp, name, args.clone())? {
        return Ok(v);
    }
    match name {
        "ok" => Ok(Value::Bool({
            let r = rsp.borrow(); r.status >= 200 && r.status < 300
        })),
        "failed" => Ok(Value::Bool(rsp.borrow().status >= 400)),
        "header" => {
            let key = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Response.header() needs a string, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Response.header() needs a key"),
            };
            let r = rsp.borrow();
            match header_lookup(&r.headers, &key) {
                Some(v) => Ok(s(v)),
                None => Ok(args.get(1).cloned().unwrap_or(Value::Null)),
            }
        }
        "withHeader" => {
            let key = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Response.withHeader() name must be string, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Response.withHeader() needs name and value"),
            };
            let val = match args.get(1) {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) => return vm.fail("TypeException",
                    format!("Response.withHeader() value must be string, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Response.withHeader() needs a value"),
            };
            rsp.borrow_mut().headers.push((key, val));
            Ok(Value::HttpRsp(rsp.clone()))
        }
        "withStatus" => {
            let status = match args.first() {
                Some(Value::Int(n)) => *n,
                Some(v) => return vm.fail("TypeException",
                    format!("Response.withStatus() needs int, got {}", type_name(v))),
                None => return vm.fail("TypeException", "Response.withStatus() needs a status"),
            };
            let status = check_status(vm, status)?;
            rsp.borrow_mut().status = status;
            Ok(Value::HttpRsp(rsp.clone()))
        }
        "json" => {
            let body = rsp.borrow().body.clone();
            match crate::json::parse(&body) {
                Ok(j) => Ok(crate::builtins::json_decode_j(&j)),
                Err(e) => vm.fail("JsonException", format!("response body is not valid JSON: {}", e)),
            }
        }
        _ => vm.fail("NameException", format!("Response has no method '{}'", name)),
    }
}

// ---------------------------------------------------------------------------
// Database (delegated to crate::database)
// ---------------------------------------------------------------------------

pub fn db_op(vm: &mut Vm, op: DbOp, args: Vec<Value>) -> R<Value> {
    match op {
        DbOp::Connect => crate::database::db_connect(vm, args),
    }
}

pub fn db_get_attr(state: &Rc<RefCell<crate::database::DbState>>, name: &str) -> Option<Value> {
    crate::database::db_get_attr(state, name)
}

pub fn db_method(vm: &mut Vm, state: &Rc<RefCell<crate::database::DbState>>, name: &str, args: Vec<Value>) -> R<Value> {
    crate::database::db_method(vm, state, name, args)
}

// ---------------------------------------------------------------------------
// Http
// ---------------------------------------------------------------------------

pub fn http_op(vm: &mut Vm, op: HttpOp, args: Vec<Value>) -> R<Value> {
    let (method, has_body) = match op {
        HttpOp::Get => ("GET", false),
        HttpOp::Post => ("POST", true),
        HttpOp::Put => ("PUT", true),
        HttpOp::Patch => ("PATCH", true),
        HttpOp::Delete => ("DELETE", false),
        HttpOp::Request => {
            return vm.fail("NativeError",
                "Http.request() is not supported by the native VM yet");
        }
        HttpOp::Fake | HttpOp::Recorded | HttpOp::Restore => {
            return vm.fail("NativeError",
                "Http.fake() is not supported by the native VM yet");
        }
    };
    let fname = format!("Http.{}", method.to_ascii_lowercase());
    let url = match args.first() {
        Some(Value::Str(s)) => s.s.clone(),
        Some(v) => return vm.fail("TypeException",
            format!("{}() url must be string, got {}", fname, type_name(v))),
        None => return vm.fail("TypeException", format!("{}() needs a URL", fname)),
    };
    if !url.to_ascii_lowercase().starts_with("http://") && !url.to_ascii_lowercase().starts_with("https://") {
        return vm.fail("HttpException",
            format!("{}(): URL must start with http:// or https://, got {:?}", fname, url));
    }
    vm.fail("NativeError",
        format!("{}() HTTP requests are not supported by the native VM yet", fname))
}
