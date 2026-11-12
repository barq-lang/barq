//! Templates in the native VM (v0.34): view(), View.render/string/share/...
//!
//! barq/native.py parses every file under views/ (and every literal
//! View.string() template) with the reference template parser and puts the
//! trees in the bundle, so syntax errors read the same on every engine. The
//! compiler turns each {expression} into a small function whose free names are
//! looked up at run time in the template scope (`TEnv`), see Op::TplName.

use crate::ast::{self, Expr, Pos};
use crate::code::{Proto, ViewOp};
use crate::json::J;
use crate::value::*;
use crate::vm::{Vm, R};
use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

// ===========================================================================
// template trees (X = ast::Expr when loaded, Rc<Proto> once compiled)
// ===========================================================================

pub enum TVal<X> {
    Str(Rc<str>),
    Expr(X),
}

pub enum TNode<X> {
    Text(Rc<str>),
    Out(X, bool),
    If(Vec<(X, Vec<TNode<X>>)>, Option<Vec<TNode<X>>>),
    Each { key: Option<Rc<str>>, var: Rc<str>, expr: X, body: Vec<TNode<X>>, empty: Option<Vec<TNode<X>>> },
    Include(TVal<X>, Option<TVal<X>>),
    Section(Rc<str>, Vec<TNode<X>>),
    Yield(Rc<str>, Option<Vec<TNode<X>>>),
    Let(Vec<(Rc<str>, TVal<X>)>),
}

pub struct TplOk<X> {
    pub nodes: Vec<TNode<X>>,
    pub extends: Option<(TVal<X>, Pos)>,
}

pub struct TplDef<X> {
    #[allow(dead_code)]
    pub file: Rc<str>,
    /// Err: the template does not parse (message, position in the template)
    pub body: Result<TplOk<X>, (String, Pos)>,
}

pub struct TplBundle<X> {
    pub views: Vec<(String, TplDef<X>)>,
    pub strings: Vec<(String, TplDef<X>)>,
}

impl<X> TplBundle<X> {
    pub fn empty() -> TplBundle<X> {
        TplBundle { views: Vec::new(), strings: Vec::new() }
    }
}

// -- loading from the bundle JSON --------------------------------------------

fn jpos(j: &J) -> Pos {
    (j.get("l").int() as u32, j.get("c").int() as u32)
}

fn load_val(j: &J) -> TVal<Expr> {
    if j.get("e").is_null() {
        TVal::Str(j.get("s").str().into())
    } else {
        TVal::Expr(ast::expr(j.get("e")))
    }
}

fn load_nodes(j: &J) -> Vec<TNode<Expr>> {
    j.arr().iter().map(load_node).collect()
}

fn opt_nodes(j: &J) -> Option<Vec<TNode<Expr>>> {
    if j.is_null() { None } else { Some(load_nodes(j)) }
}

fn load_node(j: &J) -> TNode<Expr> {
    match j.get("t").str() {
        "text" => TNode::Text(j.get("s").str().into()),
        "out" => TNode::Out(ast::expr(j.get("e")), j.get("raw").bool()),
        "if" => TNode::If(
            j.get("br").arr().iter().map(|b| (ast::expr(&b.arr()[0]), load_nodes(&b.arr()[1]))).collect(),
            opt_nodes(j.get("else")),
        ),
        "each" => TNode::Each {
            key: j.get("key").opt_str().map(|s| s.into()),
            var: j.get("var").str().into(),
            expr: ast::expr(j.get("e")),
            body: load_nodes(j.get("body")),
            empty: opt_nodes(j.get("empty")),
        },
        "include" => TNode::Include(load_val(j.get("f")), if j.get("w").is_null() { None } else { Some(load_val(j.get("w"))) }),
        "section" => TNode::Section(j.get("name").str().into(), load_nodes(j.get("body"))),
        "yield" => TNode::Yield(j.get("name").str().into(), opt_nodes(j.get("body"))),
        _ => TNode::Let(j.get("pairs").arr().iter().map(|p| (p.arr()[0].str().into(), load_val(&p.arr()[1]))).collect()),
    }
}

fn load_def(j: &J) -> TplDef<Expr> {
    let file: Rc<str> = j.get("file").str().into();
    if !j.get("ok").bool() {
        return TplDef { file, body: Err((j.get("msg").str().to_string(), jpos(j))) };
    }
    let extends = if j.get("extends").is_null() {
        None
    } else {
        Some((load_val(j.get("extends")), (j.get("el").int() as u32, j.get("ec").int() as u32)))
    };
    TplDef { file, body: Ok(TplOk { nodes: load_nodes(j.get("nodes")), extends }) }
}

fn load_map(j: &J) -> Vec<(String, TplDef<Expr>)> {
    match j {
        J::Obj(fields) => fields.iter().map(|(k, v)| (k.clone(), load_def(v))).collect(),
        _ => Vec::new(),
    }
}

pub fn load_bundle(j: &J) -> TplBundle<Expr> {
    TplBundle { views: load_map(j.get("views")), strings: load_map(j.get("strings")) }
}

// -- compiling ---------------------------------------------------------------

pub fn map_bundle<'a, P>(b: &'a TplBundle<Expr>, f: &mut dyn FnMut(&'a Expr) -> P) -> TplBundle<P> {
    let mut defs = |list: &'a [(String, TplDef<Expr>)], f: &mut dyn FnMut(&'a Expr) -> P| {
        list.iter().map(|(k, d)| (k.clone(), map_def(d, f))).collect::<Vec<_>>()
    };
    let views = defs(&b.views, f);
    let strings = defs(&b.strings, f);
    TplBundle { views, strings }
}

fn map_def<'a, P>(d: &'a TplDef<Expr>, f: &mut dyn FnMut(&'a Expr) -> P) -> TplDef<P> {
    let body = match &d.body {
        Err(e) => Err(e.clone()),
        Ok(ok) => Ok(TplOk {
            nodes: map_nodes(&ok.nodes, f),
            extends: ok.extends.as_ref().map(|(v, p)| (map_val(v, f), *p)),
        }),
    };
    TplDef { file: d.file.clone(), body }
}

fn map_val<'a, P>(v: &'a TVal<Expr>, f: &mut dyn FnMut(&'a Expr) -> P) -> TVal<P> {
    match v {
        TVal::Str(s) => TVal::Str(s.clone()),
        TVal::Expr(e) => TVal::Expr(f(e)),
    }
}

fn map_nodes<'a, P>(nodes: &'a [TNode<Expr>], f: &mut dyn FnMut(&'a Expr) -> P) -> Vec<TNode<P>> {
    nodes.iter().map(|n| map_node(n, f)).collect()
}

fn map_node<'a, P>(n: &'a TNode<Expr>, f: &mut dyn FnMut(&'a Expr) -> P) -> TNode<P> {
    match n {
        TNode::Text(t) => TNode::Text(t.clone()),
        TNode::Out(e, raw) => TNode::Out(f(e), *raw),
        TNode::If(branches, other) => TNode::If(
            branches.iter().map(|(c, b)| (f(c), map_nodes(b, f))).collect(),
            other.as_ref().map(|b| map_nodes(b, f)),
        ),
        TNode::Each { key, var, expr, body, empty } => TNode::Each {
            key: key.clone(),
            var: var.clone(),
            expr: f(expr),
            body: map_nodes(body, f),
            empty: empty.as_ref().map(|b| map_nodes(b, f)),
        },
        TNode::Include(file, with) => TNode::Include(map_val(file, f), with.as_ref().map(|w| map_val(w, f))),
        TNode::Section(name, body) => TNode::Section(name.clone(), map_nodes(body, f)),
        TNode::Yield(name, body) => TNode::Yield(name.clone(), body.as_ref().map(|b| map_nodes(b, f))),
        TNode::Let(pairs) => TNode::Let(pairs.iter().map(|(k, v)| (k.clone(), map_val(v, f))).collect()),
    }
}

type Tpl = TplDef<Rc<Proto>>;

// ===========================================================================
// run-time state
// ===========================================================================

struct State {
    views: HashMap<String, Rc<Tpl>>,
    strings: HashMap<String, Rc<Tpl>>,
    root: std::path::PathBuf,
    /// the views folder, relative to the program's folder
    folder: String,
    shared: Vec<(Rc<str>, Value)>,
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State {
        views: HashMap::new(),
        strings: HashMap::new(),
        root: std::path::PathBuf::new(),
        folder: "views".into(),
        shared: Vec::new(),
    });
}

pub fn install(b: TplBundle<Rc<Proto>>, main_file: &str) {
    STATE.with(|st| {
        let mut st = st.borrow_mut();
        st.views = b.views.into_iter().map(|(k, d)| (k, Rc::new(d))).collect();
        st.strings = b.strings.into_iter().map(|(k, d)| (k, Rc::new(d))).collect();
        st.root = std::path::Path::new(main_file).parent().map(|p| p.to_path_buf()).unwrap_or_default();
    });
}

/// A template scope: variables of one level, then the enclosing level.
pub struct TEnv {
    vars: RefCell<Vec<(Rc<str>, Value)>>,
    parent: Option<Rc<TEnv>>,
}

impl TEnv {
    fn new(parent: Option<Rc<TEnv>>) -> Rc<TEnv> {
        Rc::new(TEnv { vars: RefCell::new(Vec::new()), parent })
    }

    pub fn find(&self, name: &str) -> Option<Value> {
        let mut e = self;
        loop {
            if let Some((_, v)) = e.vars.borrow().iter().find(|(k, _)| &**k == name) {
                return Some(v.clone());
            }
            match &e.parent {
                Some(p) => e = p,
                None => return None,
            }
        }
    }

    fn declare(&self, name: &str, v: Value) {
        let mut vars = self.vars.borrow_mut();
        match vars.iter_mut().find(|(k, _)| &**k == name) {
            Some(slot) => slot.1 = v,
            None => vars.push((name.into(), v)),
        }
    }
}

fn is_ident(t: &str) -> bool {
    let mut cs = t.chars();
    match cs.next() {
        Some(c) if c.is_alphabetic() || c == '_' => {}
        _ => return false,
    }
    cs.all(|c| c.is_alphanumeric() || c == '_')
}

fn key(name: &str) -> HKey {
    hkey(&s(name), false).unwrap()
}

// ===========================================================================
// Op::TplGet: `a.b` reads dict keys too
// ===========================================================================

pub fn tpl_get(vm: &mut Vm, obj: Value, name: &str, safe: bool) -> R<Value> {
    if let Value::Dict(d) = &obj {
        let d = d.borrow();
        if let Some(v) = d.get(&key(name)) {
            return Ok(v.clone());
        }
        if safe {
            return Ok(Value::Null);
        }
        let keys: Vec<String> = d.keys().iter().filter_map(|k| match k {
            Value::Str(t) => Some(format!("\"{}\"", t.s)),
            _ => None,
        }).take(8).collect();
        let listed = if d.len() == 0 { " (it is empty)".to_string() } else if keys.is_empty() { String::new() } else { format!(" (keys: {})", keys.join(", ")) };
        return vm.fail("NameException", format!("dict has no key \"{}\"{} — use ?.{} if it may be missing", name, listed, name));
    }
    if matches!(obj, Value::Null) && safe {
        return Ok(Value::Null);
    }
    vm.get_attr(&obj, name)
}

// ===========================================================================
// rendering
// ===========================================================================

const MAX_DEPTH: usize = 40;

struct Ctx {
    sections: HashMap<Rc<str>, String>,
    depth: usize,
}

fn ev(vm: &mut Vm, p: &Rc<Proto>, env: &Rc<TEnv>) -> R<Value> {
    let prev = vm.tpl_env.replace(env.clone());
    let r = vm.run_expr(p);
    vm.tpl_env = prev;
    r
}

fn value_of(vm: &mut Vm, v: &TVal<Rc<Proto>>, env: &Rc<TEnv>) -> R<Value> {
    match v {
        TVal::Str(t) => Ok(s(t.to_string())),
        TVal::Expr(p) => ev(vm, p, env),
    }
}

fn display(vm: &mut Vm, v: &Value) -> R<String> {
    match v {
        Value::Null => Ok(String::new()),
        Value::Str(t) => Ok(t.s.clone()),
        _ => vm.display(v, false),
    }
}

pub fn escape(t: &str) -> String {
    let mut out = String::with_capacity(t.len() + 8);
    for c in t.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            _ => out.push(c),
        }
    }
    out
}

fn render_template(vm: &mut Vm, tpl: &Rc<Tpl>, env: &Rc<TEnv>, ctx: &mut Ctx) -> R<String> {
    if ctx.depth > MAX_DEPTH {
        return vm.fail("TemplateException", "views include or extend each other too deeply (a loop?)");
    }
    let ok = match &tpl.body {
        Ok(ok) => ok,
        Err((msg, _)) => return vm.fail("TemplateException", msg.clone()),
    };
    if let Some((layout, _)) = &ok.extends {
        for node in &ok.nodes {
            match node {
                TNode::Section(name, body) => {
                    if !ctx.sections.contains_key(name) {
                        let mut out = String::new();
                        render_nodes(vm, body, env, ctx, &mut out)?;
                        ctx.sections.insert(name.clone(), out);
                    }
                }
                TNode::Let(_) => {
                    let mut sink = String::new();
                    render_nodes(vm, std::slice::from_ref(node), env, ctx, &mut sink)?;
                }
                _ => {}
            }
        }
        let name = value_of(vm, layout, env)?;
        let parent = load(vm, &name)?;
        let mut sub = Ctx { sections: std::mem::take(&mut ctx.sections), depth: ctx.depth + 1 };
        let r = render_template(vm, &parent, env, &mut sub);
        ctx.sections = sub.sections;
        return r;
    }
    let mut out = String::new();
    render_nodes(vm, &ok.nodes, env, ctx, &mut out)?;
    Ok(out)
}

fn render_nodes(vm: &mut Vm, nodes: &[TNode<Rc<Proto>>], env: &Rc<TEnv>, ctx: &mut Ctx, out: &mut String) -> R<()> {
    for node in nodes {
        match node {
            TNode::Text(t) => out.push_str(t),
            TNode::Out(p, raw) => {
                let v = ev(vm, p, env)?;
                let text = display(vm, &v)?;
                if *raw {
                    out.push_str(&text);
                } else {
                    out.push_str(&escape(&text));
                }
            }
            TNode::If(branches, other) => {
                let mut done = false;
                for (test, body) in branches {
                    let v = ev(vm, test, env)?;
                    match v {
                        Value::Bool(true) => {
                            render_nodes(vm, body, env, ctx, out)?;
                            done = true;
                            break;
                        }
                        Value::Bool(false) => {}
                        other => return vm.cond_error(&other, crate::code::CondKind::Tpl),
                    }
                }
                if !done {
                    if let Some(body) = other {
                        render_nodes(vm, body, env, ctx, out)?;
                    }
                }
            }
            TNode::Each { key, var, expr, body, empty } => {
                let v = ev(vm, expr, env)?;
                let items = items_of(vm, v, key.is_some())?;
                if items.is_empty() {
                    if let Some(e) = empty {
                        render_nodes(vm, e, env, ctx, out)?;
                    }
                    continue;
                }
                let parent = match env.find("loop") {
                    Some(Value::Dict(d)) if d.borrow().contains(&self::key("count")) => Value::Dict(d),
                    _ => Value::Null,
                };
                let count = items.len();
                for (k, (kv, item)) in items.into_iter().enumerate() {
                    let e = TEnv::new(Some(env.clone()));
                    if let Some(kname) = key {
                        e.declare(kname, kv);
                    }
                    e.declare(var, item);
                    let mut m = OMap::new(false);
                    let n = k + 1;
                    for (name, val) in [
                        ("index", Value::Int(k as i64)),
                        ("number", Value::Int(n as i64)),
                        ("count", Value::Int(count as i64)),
                        ("first", Value::Bool(k == 0)),
                        ("last", Value::Bool(k == count - 1)),
                        ("even", Value::Bool(n % 2 == 0)),
                        ("odd", Value::Bool(n % 2 == 1)),
                        ("parent", parent.clone()),
                    ] {
                        m.insert(self::key(name), s(name), val);
                    }
                    e.declare("loop", dict(m));
                    render_nodes(vm, body, &e, ctx, out)?;
                }
            }
            TNode::Include(file, with) => {
                let name = value_of(vm, file, env)?;
                let child = TEnv::new(Some(env.clone()));
                if let Some(w) = with {
                    let extra = value_of(vm, w, env)?;
                    let Value::Dict(d) = &extra else {
                        return vm.fail("TypeException", format!("<include with=\"{{...}}\"> needs a dict, got {}", type_name(&extra)));
                    };
                    let pairs: Vec<(Value, Value)> = d.borrow().iter().cloned().collect();
                    for (k, v) in pairs {
                        match &k {
                            Value::Str(t) if is_ident(&t.s) => child.declare(&t.s, v),
                            _ => {
                                let shown = vm.display(&k, true)?;
                                return vm.fail("ValueException", format!("<include with>: variable names must be identifiers, got {}", shown));
                            }
                        }
                    }
                }
                let sub = load(vm, &name)?;
                let mut c = Ctx { sections: ctx.sections.clone(), depth: ctx.depth + 1 };
                let text = render_template(vm, &sub, &child, &mut c)?;
                out.push_str(&text);
            }
            TNode::Section(name, body) => match ctx.sections.get(name) {
                Some(t) => out.push_str(t),
                None => render_nodes(vm, body, env, ctx, out)?,
            },
            TNode::Yield(name, body) => {
                if let Some(t) = ctx.sections.get(name) {
                    out.push_str(t);
                } else if let Some(b) = body {
                    render_nodes(vm, b, env, ctx, out)?;
                }
            }
            TNode::Let(pairs) => {
                for (name, v) in pairs {
                    let val = value_of(vm, v, env)?;
                    env.declare(name, val);
                }
            }
        }
    }
    Ok(())
}

/// What <each> loops over: (key, item) pairs; the key is null without `k, v`.
fn items_of(vm: &mut Vm, v: Value, pairs: bool) -> R<Vec<(Value, Value)>> {
    Ok(match vm.make_iter(v, pairs)? {
        IterState::Range { cur, stop, step, .. } => {
            let mut out = Vec::new();
            let mut x = cur;
            let mut i = 0;
            while (step > 0 && x < stop) || (step < 0 && x > stop) {
                out.push((Value::Int(i), Value::Int(x)));
                x += step;
                i += 1;
            }
            out
        }
        IterState::Items { items, .. } => items.into_iter().map(|x| (Value::Null, x)).collect(),
        IterState::Pairs { items, .. } => items,
    })
}

// -- finding views -------------------------------------------------------------

/// "users.index" -> ["users", "index.bq"]
fn view_parts(vm: &Vm, name: &Value) -> R<Vec<String>> {
    let Value::Str(t) = name else {
        return vm.fail("TypeException", format!("a view name must be a string, got {}", type_name(name)));
    };
    let trimmed = t.s.trim();
    let rel = if trimmed.ends_with(".bq") { trimmed.to_string() } else { format!("{}.bq", trimmed.replace('.', "/")) };
    let parts: Vec<String> = rel.replace('\\', "/").split('/').map(|p| p.to_string()).collect();
    if trimmed.is_empty() || rel.starts_with('/') || parts.iter().any(|p| p.is_empty() || p == ".." || p == ".") {
        return vm.fail("ValueException", format!("invalid view name \"{}\" — use dots or slashes, e.g. \"users.index\"", t.s));
    }
    Ok(parts)
}

fn load(vm: &mut Vm, name: &Value) -> R<Rc<Tpl>> {
    let parts = view_parts(vm, name)?;
    let rel = parts.join("/");
    let (hit, folder, root) = STATE.with(|st| {
        let st = st.borrow();
        let hit = if st.folder == "views" { st.views.get(&rel).cloned() } else { None };
        (hit, st.folder.clone(), st.root.clone())
    });
    if let Some(t) = hit {
        return Ok(t);
    }
    let shown = name.clone();
    let shown = match &shown { Value::Str(t) => t.s.clone(), _ => String::new() };
    let full = root.join(&folder).join(&rel);
    if full.is_file() {
        return vm.fail("TemplateException", format!(
            "view \"{}\" ({}/{}) was not there when the program started — the native VM reads views/ once, run it without --native",
            shown, folder, rel));
    }
    vm.fail("TemplateException", format!("view \"{}\" not found (looked for {}/{})", shown, folder, rel))
}

fn scope(vm: &mut Vm, data: &Value, fname: &str) -> R<Rc<TEnv>> {
    let base = TEnv::new(None);
    base.declare("json", crate::compiler::native_value(crate::code::Builtin::ViewOp(ViewOp::Json), "json", 1, Some(1)));
    let shared = TEnv::new(Some(base));
    STATE.with(|st| {
        for (k, v) in &st.borrow().shared {
            shared.declare(k, v.clone());
        }
    });
    let env = TEnv::new(Some(shared));
    if let Value::Dict(d) = data {
        let pairs: Vec<(Value, Value)> = d.borrow().iter().cloned().collect();
        for (k, v) in pairs {
            match &k {
                Value::Str(t) if is_ident(&t.s) => env.declare(&t.s, v),
                _ => {
                    let shown = vm.display(&k, true)?;
                    return vm.fail("ValueException", format!("{}(): template variable names must be identifiers, got {}", fname, shown));
                }
            }
        }
    }
    Ok(env)
}

fn render(vm: &mut Vm, name: &Value, data: &Value, fname: &str) -> R<String> {
    let tpl = load(vm, name)?;
    let env = scope(vm, data, fname)?;
    render_template(vm, &tpl, &env, &mut Ctx { sections: HashMap::new(), depth: 0 })
}

// ===========================================================================
// the API
// ===========================================================================

fn need(vm: &Vm, a: &[Value], k: usize, kinds: &[&str], fname: &str) -> R<Value> {
    let v = a.get(k).cloned().unwrap_or(Value::Null);
    let tn = type_name(&v);
    if kinds.contains(&tn.as_str()) {
        return Ok(v);
    }
    vm.fail("TypeException", format!("{}() argument {} must be {}, got {}", fname, k + 1, kinds.join(" or "), tn))
}

fn str_arg(vm: &Vm, a: &[Value], k: usize, fname: &str) -> R<String> {
    match need(vm, a, k, &["string"], fname)? {
        Value::Str(t) => Ok(t.s.clone()),
        _ => unreachable!(),
    }
}

pub fn safe_json(vm: &mut Vm, v: &Value) -> R<String> {
    let text = crate::builtins::json_encode_value(vm, v, None, 0)?;
    Ok(text.replace('<', "\\u003c").replace('>', "\\u003e").replace('&', "\\u0026")
        .replace('\u{2028}', "\\u2028").replace('\u{2029}', "\\u2029"))
}

pub fn view_op(vm: &mut Vm, op: ViewOp, a: Vec<Value>) -> R<Value> {
    match op {
        ViewOp::View => {
            let name = need(vm, &a, 0, &["string"], "view")?;
            let data = need(vm, &a, 1, &["dict", "null"], "view")?;
            let status = match a.get(2) {
                None => 200,
                Some(_) => match need(vm, &a, 2, &["int"], "view")? {
                    Value::Int(n) => n,
                    _ => unreachable!(),
                },
            };
            if !(100..=599).contains(&status) {
                return vm.fail("ValueException", format!("HTTP status must be 100-599, got {}", status));
            }
            let body = render(vm, &name, &data, "view")?;
            Ok(Value::HttpRsp(Rc::new(RefCell::new(crate::stdlib2::HttpRspState {
                status,
                body,
                headers: vec![("Content-Type".into(), "text/html; charset=utf-8".into())],
            }))))
        }
        ViewOp::Render => {
            let name = need(vm, &a, 0, &["string"], "View.render")?;
            let data = need(vm, &a, 1, &["dict", "null"], "View.render")?;
            Ok(s(render(vm, &name, &data, "View.render")?))
        }
        ViewOp::Str => {
            let text = str_arg(vm, &a, 0, "View.string")?;
            let data = need(vm, &a, 1, &["dict", "null"], "View.string")?;
            let tpl = STATE.with(|st| st.borrow().strings.get(&text).cloned());
            let Some(tpl) = tpl else {
                return vm.fail("TemplateException",
                    "View.string(): the native VM only renders templates written as a string literal in the program — run it without --native");
            };
            let env = scope(vm, &data, "View.string")?;
            Ok(s(render_template(vm, &tpl, &env, &mut Ctx { sections: HashMap::new(), depth: 0 })?))
        }
        ViewOp::Exists => {
            let name = need(vm, &a, 0, &["string"], "View.exists")?;
            let parts = view_parts(vm, &name)?;
            let full = STATE.with(|st| {
                let st = st.borrow();
                st.root.join(&st.folder).join(parts.join("/"))
            });
            Ok(Value::Bool(full.is_file()))
        }
        ViewOp::Share => {
            let first = a.first().cloned().unwrap_or(Value::Null);
            let items: Vec<(Value, Value)> = match &first {
                Value::Dict(d) => {
                    if a.len() > 1 {
                        return vm.fail("TypeException", "View.share(dict) takes one argument");
                    }
                    d.borrow().iter().cloned().collect()
                }
                Value::Str(_) => {
                    if a.len() < 2 {
                        return vm.fail("TypeException", "View.share(name, value) needs a value");
                    }
                    vec![(first.clone(), a[1].clone())]
                }
                other => return vm.fail("TypeException", format!("View.share() argument 1 must be string or dict, got {}", type_name(other))),
            };
            for (k, v) in items {
                let name = match &k {
                    Value::Str(t) if is_ident(&t.s) => t.s.clone(),
                    _ => {
                        let shown = vm.display(&k, true)?;
                        return vm.fail("ValueException", format!("View.share(): names must be identifiers, got {}", shown));
                    }
                };
                STATE.with(|st| {
                    let mut st = st.borrow_mut();
                    match st.shared.iter_mut().find(|(n, _)| **n == *name) {
                        Some(slot) => slot.1 = v,
                        None => st.shared.push((name.as_str().into(), v)),
                    }
                });
            }
            Ok(Value::Null)
        }
        ViewOp::Shared => {
            let mut m = OMap::new(false);
            STATE.with(|st| {
                for (k, v) in &st.borrow().shared {
                    m.insert(key(k), s(k.to_string()), v.clone());
                }
            });
            Ok(dict(m))
        }
        ViewOp::Path => {
            if !a.is_empty() {
                let folder = str_arg(vm, &a, 0, "View.path")?;
                STATE.with(|st| {
                    let mut st = st.borrow_mut();
                    let p = std::path::Path::new(&folder);
                    let rel = if p.is_absolute() {
                        p.strip_prefix(&st.root).map(|r| r.to_string_lossy().to_string()).unwrap_or(folder.clone())
                    } else {
                        folder.clone()
                    };
                    st.folder = rel.trim_end_matches('/').replace('\\', "/");
                });
            }
            Ok(s(STATE.with(|st| st.borrow().folder.clone())))
        }
        ViewOp::Escape => {
            let v = a.first().cloned().unwrap_or(Value::Null);
            let text = display(vm, &v)?;
            Ok(s(escape(&text)))
        }
        ViewOp::Json => {
            let v = a.first().cloned().unwrap_or(Value::Null);
            Ok(s(safe_json(vm, &v)?))
        }
        ViewOp::ClearCache => Ok(Value::Null),
    }
}
