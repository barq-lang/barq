//! Compiles the checked syntax tree to Barq bytecode.
//!
//! Variables are resolved here: locals get fixed slots, variables a nested
//! function uses become shared cells, top-level variables, functions and
//! classes are globals. Which variables are captured is only known once the
//! nested function is compiled, so a file is compiled again (rarely more than
//! twice) until that set stops growing.

use crate::ast::*;
use crate::code::*;
use crate::value::*;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;

/// The built-in exception classes: (name, parent). Their ids are their places here.
pub const EXCEPTIONS: [(&str, Option<&str>); 11] = [
    ("Exception", None),
    ("RuntimeException", Some("Exception")),
    ("TypeException", Some("Exception")),
    ("CastException", Some("TypeException")),
    ("NameException", Some("Exception")),
    ("ValueException", Some("Exception")),
    ("IndexException", Some("Exception")),
    ("KeyException", Some("Exception")),
    ("NullException", Some("Exception")),
    ("DivisionByZeroException", Some("Exception")),
    ("IOException", Some("Exception")),
];

/// Why a program cannot run: a feature the native VM does not have yet, or
/// a compile error the reference interpreter finds while setting up classes.
pub enum Fail {
    Unsupported(String, Pos),
    Compile(String, Pos),
}

pub enum MemberDef {
    Method { name: Rc<str>, proto: Rc<Proto>, vis: Vis, is_static: bool, is_abstract: bool, required: usize, takes: usize, pos: Pos, from_trait: Option<Rc<str>> },
    Prop { name: Rc<str>, vis: Vis, is_static: bool, ty: Ty, default: FieldDefault, pos: Pos },
    Const { name: Rc<str>, value: Rc<Proto>, pos: Pos },
}

pub struct ClassDef {
    pub id: usize,
    pub name: Rc<str>,
    pub fqn: Rc<str>,
    pub kind: Kind,
    pub is_abstract: bool,
    pub backing: Option<String>,
    pub parent: Option<usize>,
    pub interfaces: Vec<usize>,
    pub cases: Vec<(Rc<str>, Value, Pos)>,
    pub members: Vec<MemberDef>,
    pub pos: Pos,
}

pub struct Program {
    pub main: Rc<Proto>,
    pub nglobals: usize,
    pub functions: Vec<(u32, Rc<Proto>)>,
    pub classes: Vec<ClassDef>,
    /// global slot of every class (built-in exceptions first)
    pub class_globals: Vec<u32>,
    pub global_names: Vec<String>,
}

type Key = (u32, u32, String);

/// A member a class gets from a trait: what it is called there, and a changed visibility.
#[derive(Clone)]
struct TraitItem<'a> {
    m: &'a Member,
    trait_id: usize,
    name: String,
    vis: Option<String>,
}

fn member_name(m: &Member) -> Option<&str> {
    match m {
        Member::Method(f) => Some(&f.name),
        Member::Prop { name, .. } => Some(name),
        Member::Const(n, _, _) => Some(n),
        Member::UseTrait { .. } => None,
    }
}

#[derive(Clone)]
struct Var {
    slot: u16,
    cell: bool,
    ty: Option<Ty>,
    is_const: bool,
}

enum Res {
    Local(u16, bool),
    Up(u16),
    Global(u32),
}

enum Block<'a> {
    Loop { breaks: Vec<usize>, continues: Vec<usize> },
    Switch { breaks: Vec<usize> },
    Try { fin: Option<&'a [Stmt]> },
}

struct FnState<'a> {
    name: Rc<str>,
    display: Rc<str>,
    scopes: Vec<Vec<(String, Var, Key)>>,
    next_slot: u16,
    max_slot: u16,
    code: Vec<Op>,
    pos: Vec<Pos>,
    consts: Vec<Value>,
    strings: Vec<Rc<str>>,
    string_idx: HashMap<String, u32>,
    types: Vec<Ty>,
    names: Vec<Vec<Rc<str>>>,
    errors: Vec<(&'static str, String)>,
    protos: Vec<Rc<Proto>>,
    arg_pos: Vec<Vec<Pos>>,
    upvals: Vec<(String, UpSrc, Option<Ty>, bool)>,
    blocks: Vec<Block<'a>>,
    handlers: Vec<u16>,
    is_main: bool,
    /// has an enclosing function whose variables it can capture
    nested: bool,
    ctx: Option<usize>,
    method: String,
}

impl<'a> FnState<'a> {
    fn new(name: &str, display: &str, ctx: Option<usize>, method: &str) -> FnState<'a> {
        FnState {
            name: name.into(),
            display: display.into(),
            scopes: vec![Vec::new()],
            next_slot: 0,
            max_slot: 0,
            code: Vec::new(),
            pos: Vec::new(),
            consts: Vec::new(),
            strings: Vec::new(),
            string_idx: HashMap::new(),
            types: Vec::new(),
            names: Vec::new(),
            errors: Vec::new(),
            protos: Vec::new(),
            arg_pos: Vec::new(),
            upvals: Vec::new(),
            blocks: Vec::new(),
            handlers: Vec::new(),
            is_main: false,
            nested: false,
            ctx,
            method: method.into(),
        }
    }
}

pub struct Compiler<'a> {
    captured: &'a HashSet<Key>,
    newly: HashSet<Key>,
    fns: Vec<FnState<'a>>,
    globals: HashMap<String, u32>,
    global_meta: HashMap<String, (Option<Ty>, bool)>,
    nglobals: u32,
    class_ids: HashMap<String, usize>,
    class_names: Vec<Rc<str>>,
    class_parent: Vec<Option<usize>>,
    class_globals: Vec<u32>,
    path: String,
    namespace: String,
    err: Option<Fail>,
    decls: Vec<&'a ClassDecl>,
}

pub fn compile(program: &[Stmt], path: &str) -> Result<Program, Fail> {
    let mut captured: HashSet<Key> = HashSet::new();
    for _ in 0..8 {
        let mut c = Compiler::new(&captured, path);
        let result = c.program(program);
        if let Some(e) = c.err.take() {
            return Err(e);
        }
        if c.newly.is_empty() {
            return Ok(result);
        }
        let newly = std::mem::take(&mut c.newly);
        drop(c);
        captured.extend(newly);
    }
    Err(Fail::Unsupported("this program's closures (nested too deeply)".into(), (0, 0)))
}

fn lit_value(l: &Lit) -> Value {
    match l {
        Lit::Null => Value::Null,
        Lit::Bool(b) => Value::Bool(*b),
        Lit::Int(n) => Value::Int(*n),
        Lit::Float(f) => Value::Float(*f),
        Lit::Str(t) => s(t.clone()),
    }
}

pub fn type_text(t: &TypeRef) -> String {
    let q = if t.nullable { "?" } else { "" };
    if t.args.is_empty() {
        format!("{}{}", q, t.name)
    } else {
        let inner: Vec<String> = t.args.iter().map(type_text).collect();
        format!("{}{}<{}>", q, t.name, inner.join(", "))
    }
}

const BUILTIN_FNS: [(&str, Builtin, usize, Option<usize>); 14] = [
    ("print", Builtin::Print, 0, None),
    ("println", Builtin::Print, 0, None),
    ("write", Builtin::Write, 0, None),
    ("dump", Builtin::Dump, 1, None),
    ("dd", Builtin::Dd, 1, None),
    ("breakpoint", Builtin::Breakpoint, 0, Some(0)),
    ("exit", Builtin::Exit, 0, Some(1)),
    ("die", Builtin::Die, 0, Some(1)),
    ("range", Builtin::Range, 1, Some(3)),
    ("len", Builtin::Len, 1, Some(1)),
    ("type", Builtin::Type, 1, Some(1)),
    ("input", Builtin::Input, 0, Some(1)),
    ("args", Builtin::Args, 0, Some(0)),
    ("assert", Builtin::Assert, 1, Some(2)),
];

pub fn builtin_fn(name: &str) -> Option<(Builtin, usize, Option<usize>)> {
    BUILTIN_FNS.iter().find(|b| b.0 == name).map(|b| (b.1, b.2, b.3))
}

/// A member of the Math or Str module: a function (with its argument counts) or a constant.
pub enum ModMember {
    Func(Builtin, usize, Option<usize>),
    Value(Value),
}

pub fn module_member(m: Module, name: &str) -> Option<ModMember> {
    use MathFn::*;
    match m {
        Module::Math => {
            let f = |b, lo, hi| Some(ModMember::Func(Builtin::Math(b), lo, hi));
            match name {
                "PI" => Some(ModMember::Value(Value::Float(std::f64::consts::PI))),
                "E" => Some(ModMember::Value(Value::Float(std::f64::consts::E))),
                "INF" => Some(ModMember::Value(Value::Float(f64::INFINITY))),
                "abs" => f(Abs, 1, Some(1)),
                "round" => f(Round, 1, Some(2)),
                "floor" => f(Floor, 1, Some(1)),
                "ceil" => f(Ceil, 1, Some(1)),
                "pow" => f(Pow, 2, Some(2)),
                "min" => f(Min, 1, None),
                "max" => f(Max, 1, None),
                "random" => f(Random, 0, Some(2)),
                "clamp" => f(Clamp, 3, Some(3)),
                "sqrt" => f(Sqrt, 1, Some(1)),
                "sin" => f(Sin, 1, Some(1)),
                "cos" => f(Cos, 1, Some(1)),
                "tan" => f(Tan, 1, Some(1)),
                "log" => f(Log, 1, Some(1)),
                "log10" => f(Log10, 1, Some(1)),
                "exp" => f(Exp, 1, Some(1)),
                _ => None,
            }
        }
        Module::Str => {
            if name == "join" {
                return Some(ModMember::Func(Builtin::StrJoin, 1, Some(2)));
            }
            let id = method_id(name)?;
            let (lo, hi) = crate::builtins::string_arity(id)?;
            Some(ModMember::Func(Builtin::StrFn(id), lo + 1, Some(hi + 1)))
        }
    }
}

pub fn native_value(b: Builtin, name: &str, min: usize, max: Option<usize>) -> Value {
    Value::Native(Rc::new(NativeFn { b, name: name.to_string(), min, max, owner: None, is_static: false }))
}

impl<'a> Compiler<'a> {
    fn new(captured: &'a HashSet<Key>, path: &str) -> Compiler<'a> {
        let mut c = Compiler {
            captured,
            newly: HashSet::new(),
            fns: Vec::new(),
            globals: HashMap::new(),
            global_meta: HashMap::new(),
            nglobals: 0,
            class_ids: HashMap::new(),
            class_names: Vec::new(),
            class_parent: Vec::new(),
            class_globals: Vec::new(),
            path: path.to_string(),
            namespace: String::new(),
            err: None,
            decls: Vec::new(),
        };
        for (i, (name, parent)) in EXCEPTIONS.iter().enumerate() {
            c.class_ids.insert(name.to_string(), i);
            c.class_names.push((*name).into());
            c.class_parent.push(parent.map(|p| EXCEPTIONS.iter().position(|e| e.0 == p).unwrap()));
            let g = c.global(name);
            c.class_globals.push(g);
        }
        c
    }

    fn unsupported(&mut self, what: impl Into<String>, pos: Pos) {
        if self.err.is_none() {
            self.err = Some(Fail::Unsupported(what.into(), pos));
        }
    }

    fn compile_error(&mut self, msg: impl Into<String>, pos: Pos) {
        if self.err.is_none() {
            self.err = Some(Fail::Compile(msg.into(), pos));
        }
    }

    fn global(&mut self, name: &str) -> u32 {
        if let Some(&g) = self.globals.get(name) {
            return g;
        }
        let g = self.nglobals;
        self.nglobals += 1;
        self.globals.insert(name.to_string(), g);
        g
    }

    // -- program ---------------------------------------------------------------

    fn program(&mut self, body: &'a [Stmt]) -> Program {
        // Hoisting: names of top-level functions, classes and variables first.
        let mut class_decls: Vec<&'a ClassDecl> = Vec::new();
        for (i, st) in body.iter().enumerate() {
            match &st.k {
                S::Namespace(n) => {
                    if i == 0 {
                        self.namespace = n.clone();
                    }
                }
                S::Use => self.unsupported("'use' (importing other files and modules)", st.pos),
                S::Class(cd) => {
                    let id = EXCEPTIONS.len() + class_decls.len();
                    let fqn = self.qualify(&cd.name);
                    self.class_ids.insert(cd.name.clone(), id);
                    self.class_ids.insert(fqn.clone(), id);
                    self.class_names.push(cd.name.as_str().into());
                    self.class_parent.push(None);
                    let g = self.global(&cd.name);
                    self.class_globals.push(g);
                    class_decls.push(cd);
                    self.decls.push(cd);
                }
                S::Func(f) => {
                    self.global(&f.name);
                    self.global_meta.insert(f.name.clone(), (None, true));
                }
                S::Var(t, name, _) => {
                    self.global(name);
                    let ty = self.ty(t);
                    self.global_meta.insert(name.clone(), (Some(ty), false));
                }
                S::Const(name, _) => {
                    self.global(name);
                    self.global_meta.insert(name.clone(), (None, true));
                }
                _ => {}
            }
        }
        for (k, cd) in class_decls.iter().enumerate() {
            let id = EXCEPTIONS.len() + k;
            if let Some(p) = &cd.parent {
                match self.class_id(p) {
                    Some(pid) => self.class_parent[id] = Some(pid),
                    None => self.unsupported(format!("the parent class '{}'", p), cd.pos),
                }
            }
        }
        let mut functions = Vec::new();
        for st in body {
            if let S::Func(f) = &st.k {
                let proto = self.function(f, None, false, None);
                functions.push((self.globals[&f.name], proto));
            }
        }
        let mut classes = Vec::new();
        for (k, cd) in class_decls.iter().enumerate() {
            classes.push(self.class(cd, EXCEPTIONS.len() + k));
        }
        let mut main = FnState::new("<main>", "<main>", None, "");
        main.is_main = true;
        main.scopes.clear();
        self.fns.push(main);
        for st in body {
            match &st.k {
                S::Func(_) | S::Class(_) | S::Namespace(_) | S::Use => {}
                _ => self.stmt(st),
            }
        }
        self.emit(Op::Null, (0, 0));
        self.emit(Op::Return, (0, 0));
        let f = self.fns.pop().unwrap();
        let main = self.finish(f, Vec::new(), None, (0, 0), false, false, Vis::Public, false, Vec::new());
        Program {
            main,
            nglobals: self.nglobals as usize,
            functions,
            classes,
            class_globals: self.class_globals.clone(),
            global_names: {
                let mut names = vec![String::new(); self.nglobals as usize];
                for (n, &g) in &self.globals {
                    names[g as usize] = n.clone();
                }
                names
            },
        }
    }

    fn qualify(&self, name: &str) -> String {
        if self.namespace.is_empty() {
            name.to_string()
        } else {
            format!("{}\\{}", self.namespace, name)
        }
    }

    fn class_id(&self, name: &str) -> Option<usize> {
        let n = name.trim_start_matches('\\');
        self.class_ids.get(n).copied().or_else(|| n.rsplit('\\').next().and_then(|short| {
            if self.namespace.is_empty() || !n.contains('\\') {
                None
            } else {
                self.class_ids.get(short).copied()
            }
        }))
    }

    // -- types -----------------------------------------------------------------

    fn ty(&mut self, t: &TypeRef) -> Ty {
        let k = match t.name.as_str() {
            "any" => TyK::Any,
            "int" => TyK::Int,
            "float" => TyK::Float,
            "string" => TyK::Str,
            "bool" => TyK::Bool,
            "list" => TyK::List(t.args.first().map(|a| Box::new(self.ty(a)))),
            "set" => TyK::Set(t.args.first().map(|a| Box::new(self.ty(a)))),
            "dict" => {
                if t.args.len() == 2 {
                    let a = self.ty(&t.args[0]);
                    let b = self.ty(&t.args[1]);
                    TyK::Dict(Some(Box::new((a, b))))
                } else {
                    TyK::Dict(None)
                }
            }
            "range" => TyK::Range,
            "karo" => TyK::Karo,
            "void" => TyK::Void,
            "null" => TyK::Null,
            name => match self.class_id(name) {
                Some(id) => TyK::Class(id),
                None => TyK::Unknown(name.to_string()),
            },
        };
        Ty { k, nullable: t.nullable, name: t.name.as_str().into(), text: type_text(t).into() }
    }

    fn ty_idx(&mut self, t: &TypeRef) -> u32 {
        let ty = self.ty(t);
        let f = self.f();
        f.types.push(ty);
        (f.types.len() - 1) as u32
    }

    // -- functions and classes -------------------------------------------------

    fn f(&mut self) -> &mut FnState<'a> {
        self.fns.last_mut().unwrap()
    }

    /// Compile a function, method or lambda. `owner`: (class id, class name).
    fn function(&mut self, f: &'a Func, owner: Option<(usize, Rc<str>)>, nested: bool, ctx: Option<usize>) -> Rc<Proto> {
        self.function_as(f, owner, nested, ctx, &f.name, None)
    }

    /// `name`: what the function is called (a trait method can be renamed); `vis`: a changed visibility.
    fn function_as(&mut self, f: &'a Func, owner: Option<(usize, Rc<str>)>, nested: bool, ctx: Option<usize>,
                   name: &str, vis_override: Option<Vis>) -> Rc<Proto> {
        let is_lambda = f.name == "<lambda>";
        let display: String = match &owner {
            Some((_, cname)) => format!("{}{}{}", cname, if f.is_static { "::" } else { "." }, name),
            None => name.to_string(),
        };
        let ctx = owner.as_ref().map(|o| o.0).or(ctx);
        let method = if is_lambda {
            self.fns.last().map(|s| s.method.clone()).unwrap_or_default()
        } else {
            name.to_string()
        };
        let mut st = FnState::new(name, &display, ctx, &method);
        st.nested = nested;
        self.fns.push(st);
        let has_this = owner.is_some() && !f.is_static;
        let mut cell_params = Vec::new();
        if has_this {
            let key: Key = (f.pos.0, f.pos.1, "this".into());
            let slot = self.declare("this", None, true, key);
            if self.var_is_cell(slot) {
                cell_params.push(slot);
            }
        }
        let mut params = Vec::new();
        for p in &f.params {
            let ty = p.ty.as_ref().map(|t| self.ty(t));
            let elem = if p.variadic {
                p.ty.as_ref().and_then(|t| t.args.first()).map(|a| self.ty(a))
            } else {
                None
            };
            let key: Key = (p.pos.0, p.pos.1, p.name.clone());
            let slot = self.declare(&p.name, ty.clone(), false, key);
            if self.var_is_cell(slot) {
                cell_params.push(slot);
            }
            let default = p.default.as_ref().map(|d| match &d.k {
                E::Lit(l) => Default::Const(lit_value(l)),
                _ => Default::Code(self.expr_proto(d, &format!("default of {}", p.name), None)),
            });
            params.push(ParamInfo { name: p.name.as_str().into(), ty, default, variadic: p.variadic, elem });
        }
        match &f.body {
            Body::Expr(e) => {
                self.expr(e);
                self.emit(Op::Return, e.pos);
            }
            Body::Block(stmts) => {
                for st in stmts {
                    self.stmt(st);
                }
                let end = f.ret.as_ref().map(|r| r.pos).unwrap_or(f.pos);
                self.emit(Op::ReturnEnd, end);
            }
        }
        let st = self.fns.pop().unwrap();
        let ret = f.ret.as_ref().map(|t| self.ty(t));
        let ret_pos = f.ret.as_ref().map(|r| r.pos).unwrap_or(f.pos);
        let vis = vis_override.unwrap_or_else(|| Vis::parse(f.visibility.as_deref().unwrap_or("public")));
        let upvals: Vec<UpSrc> = st.upvals.iter().map(|u| u.1).collect();
        let mut proto = self.finish_raw(st, params, ret, ret_pos, has_this, is_lambda, vis, cell_params, upvals);
        proto.owner = owner.map(|o| o.0);
        proto.is_static = f.is_static;
        proto.is_abstract = f.is_abstract;
        Rc::new(proto)
    }

    /// A function that evaluates one expression (defaults, static values, constants).
    fn expr_proto(&mut self, e: &'a Expr, name: &str, ctx: Option<usize>) -> Rc<Proto> {
        let mut st = FnState::new(name, name, ctx, "");
        st.is_main = false;
        self.fns.push(st);
        self.expr(e);
        self.emit(Op::Return, e.pos);
        let st = self.fns.pop().unwrap();
        self.finish(st, Vec::new(), None, e.pos, false, false, Vis::Public, false, Vec::new())
    }

    #[allow(clippy::too_many_arguments)]
    fn finish(&mut self, st: FnState<'a>, params: Vec<ParamInfo>, ret: Option<Ty>, ret_pos: Pos, has_this: bool,
              is_lambda: bool, vis: Vis, _x: bool, cell_params: Vec<u16>) -> Rc<Proto> {
        let upvals: Vec<UpSrc> = st.upvals.iter().map(|u| u.1).collect();
        Rc::new(self.finish_raw(st, params, ret, ret_pos, has_this, is_lambda, vis, cell_params, upvals))
    }

    #[allow(clippy::too_many_arguments)]
    fn finish_raw(&mut self, st: FnState<'a>, params: Vec<ParamInfo>, ret: Option<Ty>, ret_pos: Pos, has_this: bool,
                  is_lambda: bool, vis: Vis, cell_params: Vec<u16>, upvals: Vec<UpSrc>) -> Proto {
        let simple = params.iter().all(|p| p.default.is_none() && !p.variadic);
        Proto {
            name: st.name,
            display: st.display,
            params,
            simple,
            ret,
            ret_pos,
            code: st.code,
            pos: st.pos,
            consts: st.consts,
            strings: st.strings,
            types: st.types,
            names: st.names,
            errors: st.errors,
            protos: st.protos,
            arg_pos: st.arg_pos,
            nlocals: st.max_slot as usize,
            cell_params,
            upvals,
            owner: None,
            is_static: false,
            is_abstract: false,
            vis,
            ctx: st.ctx,
            has_this,
            is_lambda,
        }
    }

    fn class(&mut self, cd: &'a ClassDecl, id: usize) -> ClassDef {
        let name: Rc<str> = cd.name.as_str().into();
        let kind = match cd.kind.as_str() {
            "interface" => Kind::Interface,
            "trait" => Kind::Trait,
            "enum" => Kind::Enum,
            _ => {
                if cd.is_enum {
                    Kind::Enum
                } else {
                    Kind::Class
                }
            }
        };
        if kind == Kind::Trait {
            // a trait's members are copied into each class that uses it
            return ClassDef {
                id,
                name,
                fqn: self.qualify(&cd.name).into(),
                kind,
                is_abstract: true,
                backing: None,
                parent: None,
                interfaces: Vec::new(),
                cases: Vec::new(),
                members: Vec::new(),
                pos: cd.pos,
            };
        }
        let mut interfaces = Vec::new();
        for i in &cd.interfaces {
            match self.class_id(i) {
                Some(iid) => interfaces.push(iid),
                None => self.unsupported(format!("the interface '{}'", i), cd.pos),
            }
        }
        let mut members = Vec::new();
        for m in &cd.members {
            match m {
                Member::Method(f) => {
                    let proto = self.function(f, Some((id, name.clone())), false, None);
                    let variadic = f.params.last().map(|p| p.variadic).unwrap_or(false);
                    members.push(MemberDef::Method {
                        name: f.name.as_str().into(),
                        proto,
                        vis: Vis::parse(f.visibility.as_deref().unwrap_or("public")),
                        is_static: f.is_static,
                        is_abstract: f.is_abstract,
                        required: f.params.iter().filter(|p| p.default.is_none() && !p.variadic).count(),
                        takes: if variadic { usize::MAX } else { f.params.len() },
                        pos: f.pos,
                        from_trait: None,
                    });
                }
                Member::Prop { vis, is_static, ty, name: pname, value, pos } => {
                    let t = self.ty(ty);
                    let default = match value {
                        None => FieldDefault::Zero,
                        Some(Expr { k: E::Lit(l), .. }) => FieldDefault::Const(lit_value(l)),
                        Some(e) => FieldDefault::Code(self.expr_proto(e, &format!("{}.{}", cd.name, pname), Some(id))),
                    };
                    members.push(MemberDef::Prop {
                        name: pname.as_str().into(),
                        vis: Vis::parse(vis),
                        is_static: *is_static,
                        ty: t,
                        default,
                        pos: *pos,
                    });
                }
                Member::Const(cname, e, pos) => {
                    let value = self.expr_proto(e, &format!("{}::{}", cd.name, cname), Some(id));
                    members.push(MemberDef::Const { name: cname.as_str().into(), value, pos: *pos });
                }
                Member::UseTrait { .. } => {}
            }
        }
        self.add_traits(cd, id, &name, kind, &mut members);
        ClassDef {
            id,
            name,
            fqn: self.qualify(&cd.name).into(),
            kind,
            is_abstract: cd.is_abstract || kind == Kind::Interface || kind == Kind::Trait,
            backing: cd.backing.clone(),
            parent: self.class_parent[id],
            interfaces,
            cases: cd
                .cases
                .iter()
                .map(|(n, v, p)| (n.as_str().into(), v.as_ref().map(lit_value).unwrap_or(Value::Null), *p))
                .collect(),
            members,
            pos: cd.pos,
        }
    }

    // -- traits ----------------------------------------------------------------

    fn decl_of(&self, id: usize) -> Option<&'a ClassDecl> {
        id.checked_sub(EXCEPTIONS.len()).and_then(|k| self.decls.get(k)).copied()
    }

    /// The members a class or trait gets from its `use` lines (nested traits
    /// included), after the `insteadof` / `as` rules. interpreter.trait_members
    fn trait_members(&mut self, decl: &'a ClassDecl, stack: &[usize]) -> Option<Vec<TraitItem<'a>>> {
        let mut used: Vec<(usize, Vec<TraitItem<'a>>)> = Vec::new();
        let mut rules: Vec<&'a TraitRule> = Vec::new();
        for m in &decl.members {
            let Member::UseTrait { names, rules: rs, pos } = m else { continue };
            rules.extend(rs.iter());
            for tname in names {
                let Some(tid) = self.class_id(tname) else {
                    self.unsupported(format!("the trait '{}'", tname), *pos);
                    return None;
                };
                let tdecl = self.decl_of(tid);
                let tkind = tdecl.map(|d| d.kind.as_str()).unwrap_or("class");
                let tshort = self.class_names[tid].clone();
                if tkind != "trait" {
                    let hint = match tkind {
                        "interface" => format!(" — write 'implements {}' after the class name", tshort),
                        "class" => format!(" — write 'extends {}' after the class name", tshort),
                        _ => String::new(),
                    };
                    self.compile_error(format!("{} is not a trait{}", tshort, hint), *pos);
                    return None;
                }
                if stack.contains(&tid) {
                    self.compile_error(format!("trait {} uses itself", tshort), *pos);
                    return None;
                }
                let tdecl = tdecl.unwrap();
                let own: Vec<TraitItem<'a>> = tdecl
                    .members
                    .iter()
                    .filter_map(|m| member_name(m).map(|n| TraitItem { m, trait_id: tid, name: n.to_string(), vis: None }))
                    .collect();
                let mut inner_stack = stack.to_vec();
                inner_stack.push(tid);
                let inner = self.trait_members(tdecl, &inner_stack)?;
                let mut items: Vec<TraitItem<'a>> =
                    inner.into_iter().filter(|x| !own.iter().any(|o| o.name == x.name)).collect();
                items.extend(own);
                used.push((tid, items));
            }
        }
        if !rules.is_empty() {
            self.apply_trait_rules(decl, &mut used, &rules)?;
        }
        Some(used.into_iter().flat_map(|(_, items)| items).collect())
    }

    fn apply_trait_rules(&mut self, decl: &'a ClassDecl, used: &mut [(usize, Vec<TraitItem<'a>>)], rules: &[&'a TraitRule]) -> Option<()> {
        fn methods_named<'b>(items: &[TraitItem<'b>], name: &str) -> Vec<usize> {
            (0..items.len()).filter(|&k| items[k].name == name && matches!(items[k].m, Member::Method(_))).collect()
        }
        let find_trait = |c: &mut Self, used: &[(usize, Vec<TraitItem<'a>>)], name: &str, rule: &TraitRule| -> Option<usize> {
            let Some(id) = c.class_id(name) else {
                c.unsupported(format!("the trait '{}'", name), rule.pos);
                return None;
            };
            if let Some(i) = used.iter().position(|u| u.0 == id) {
                return Some(i);
            }
            let tn = c.class_names[id].clone();
            c.compile_error(format!("trait {} is not used by {} — add it to the 'use' line", tn, decl.name), rule.pos);
            None
        };
        let no_such = |c: &mut Self, used: &[(usize, Vec<TraitItem<'a>>)], i: usize, rule: &TraitRule| {
            let tn = c.class_names[used[i].0].clone();
            if used[i].1.iter().any(|x| x.name == rule.method) {
                c.compile_error(
                    format!("'{}' in trait {} is not a method — only methods can be chosen or renamed", rule.method, tn),
                    rule.pos,
                );
            } else {
                c.compile_error(format!("trait {} has no method '{}()'", tn, rule.method), rule.pos);
            }
        };
        // insteadof first, so `as` rules see which method won
        let before: Vec<Vec<TraitItem<'a>>> = used.iter().map(|u| u.1.clone()).collect();
        for rule in rules {
            let Some(others) = &rule.insteadof else { continue };
            let keep = find_trait(self, used, rule.trait_name.as_deref().unwrap_or(""), rule)?;
            if methods_named(&used[keep].1, &rule.method).is_empty() {
                no_such(self, used, keep, rule);
                return None;
            }
            for other in others {
                let j = find_trait(self, used, other, rule)?;
                if j == keep {
                    let tn = self.class_names[used[j].0].clone();
                    self.compile_error(format!("{}::{} cannot be used instead of itself", tn, rule.method), rule.pos);
                    return None;
                }
                if methods_named(&used[j].1, &rule.method).is_empty() {
                    no_such(self, used, j, rule);
                    return None;
                }
                used[j].1.retain(|x| !(x.name == rule.method && matches!(x.m, Member::Method(_))));
            }
        }
        for rule in rules {
            if rule.insteadof.is_some() {
                continue;
            }
            let from_before = rule.alias.is_some();
            let (i, item_idx) = if let Some(tn) = &rule.trait_name {
                let i = find_trait(self, used, tn, rule)?;
                let list = if from_before { &before[i] } else { &used[i].1 };
                let found = methods_named(list, &rule.method);
                if found.is_empty() {
                    no_such(self, used, i, rule);
                    return None;
                }
                (i, found[0])
            } else {
                let mut hits: Vec<(usize, usize)> = Vec::new();
                for i in 0..used.len() {
                    let list = if from_before { &before[i] } else { &used[i].1 };
                    for k in methods_named(list, &rule.method) {
                        hits.push((i, k));
                    }
                }
                if hits.is_empty() {
                    self.compile_error(format!("no trait used by {} has a method '{}()'", decl.name, rule.method), rule.pos);
                    return None;
                }
                let first_m = {
                    let list = if from_before { &before[hits[0].0] } else { &used[hits[0].0].1 };
                    list[hits[0].1].m as *const Member
                };
                let distinct = hits.iter().any(|(i, k)| {
                    let list = if from_before { &before[*i] } else { &used[*i].1 };
                    !std::ptr::eq(list[*k].m, first_m)
                });
                if distinct {
                    let a = self.class_names[used[hits[0].0].0].clone();
                    let b = self.class_names[used[hits[1].0].0].clone();
                    self.compile_error(
                        format!("'{}' comes from both trait {} and trait {} — write {}::{} as ...", rule.method, a, b, a, rule.method),
                        rule.pos,
                    );
                    return None;
                }
                hits[0]
            };
            let item = if from_before { before[i][item_idx].clone() } else { used[i].1[item_idx].clone() };
            if let Some(alias) = &rule.alias {
                if let Member::Method(f) = item.m {
                    if f.is_abstract {
                        self.compile_error(format!("abstract method '{}()' cannot be given another name", rule.method), rule.pos);
                        return None;
                    }
                }
                used[i].1.push(TraitItem { m: item.m, trait_id: item.trait_id, name: alias.clone(), vis: rule.vis.clone().or(item.vis) });
            } else {
                used[i].1[item_idx].vis = rule.vis.clone();
            }
        }
        Some(())
    }

    /// Whether class id (or a parent) has a non-abstract method `name`.
    fn has_concrete(&mut self, id: usize, name: &str) -> bool {
        let mut cur = Some(id);
        while let Some(c) = cur {
            match self.decl_of(c) {
                None => {
                    if ["__construct", "getMessage", "getCode"].contains(&name) {
                        return true;
                    }
                }
                Some(d) => {
                    for m in &d.members {
                        if let Member::Method(f) = m {
                            if f.name == name {
                                return !f.is_abstract;
                            }
                        }
                    }
                    let items = self.trait_members(d, &[]).unwrap_or_default();
                    if let Some(it) = items.iter().rev().find(|x| x.name == name) {
                        if let Member::Method(f) = it.m {
                            if !f.is_abstract {
                                return true;
                            }
                        }
                    }
                }
            }
            cur = self.class_parent[c];
        }
        false
    }

    /// interpreter.add_traits: trait members join the class as if written there.
    fn add_traits(&mut self, cd: &'a ClassDecl, id: usize, cname: &Rc<str>, kind: Kind, members: &mut Vec<MemberDef>) {
        let Some(items) = self.trait_members(cd, &[]) else { return };
        if items.is_empty() {
            return;
        }
        let own: Vec<&str> = cd.members.iter().filter_map(member_name).collect();
        let mut came_from: HashMap<String, (*const Member, usize)> = HashMap::new();
        let mut placed: HashMap<String, usize> = HashMap::new();
        for it in items {
            let tname = self.class_names[it.trait_id].clone();
            if own.contains(&it.name.as_str()) {
                if let Member::Prop { .. } = it.m {
                    self.compile_error(format!("property '{}' is declared in both {} and trait {}", it.name, cd.name, tname), cd.pos);
                    return;
                }
                continue;
            }
            if let Some(&(seen_m, seen_t)) = came_from.get(&it.name) {
                if std::ptr::eq(seen_m, it.m) {
                    continue;
                }
                // SAFETY: seen_m points into the syntax tree, which outlives the compiler.
                let seen = unsafe { &*seen_m };
                match (seen, it.m) {
                    (Member::Method(sf), Member::Method(mf)) => {
                        if mf.is_abstract {
                            continue;
                        }
                        if !sf.is_abstract {
                            let a = self.class_names[seen_t].clone();
                            self.compile_error(
                                format!(
                                    "method '{n}()' comes from both trait {a} and trait {b} — choose one under the 'use' line ({a}::{n} insteadof {b}) or write {n}() in {c}",
                                    n = it.name, a = a, b = tname, c = cd.name
                                ),
                                cd.pos,
                            );
                            return;
                        }
                    }
                    _ => {
                        let a = self.class_names[seen_t].clone();
                        self.compile_error(format!("'{}' comes from both trait {} and trait {}", it.name, a, tname), cd.pos);
                        return;
                    }
                }
            }
            came_from.insert(it.name.clone(), (it.m as *const Member, it.trait_id));
            let def = match it.m {
                Member::Method(f) => {
                    if f.is_abstract {
                        if let Some(p) = self.class_parent[id] {
                            if self.has_concrete(p, &it.name) {
                                continue;
                            }
                        }
                    }
                    let vis = Vis::parse(it.vis.as_deref().or(f.visibility.as_deref()).unwrap_or("public"));
                    let proto = self.function_as(f, Some((id, cname.clone())), false, None, &it.name, Some(vis));
                    let variadic = f.params.last().map(|p| p.variadic).unwrap_or(false);
                    MemberDef::Method {
                        name: it.name.as_str().into(),
                        proto,
                        vis,
                        is_static: f.is_static,
                        is_abstract: f.is_abstract,
                        required: f.params.iter().filter(|p| p.default.is_none() && !p.variadic).count(),
                        takes: if variadic { usize::MAX } else { f.params.len() },
                        pos: f.pos,
                        from_trait: Some(tname.clone()),
                    }
                }
                Member::Prop { vis, is_static, ty, name: pname, value, pos } => {
                    if kind == Kind::Enum {
                        self.compile_error(format!("enum {} cannot use trait {}: it has property '{}'", cd.name, tname, pname), cd.pos);
                        return;
                    }
                    let t = self.ty(ty);
                    let default = match value {
                        None => FieldDefault::Zero,
                        Some(Expr { k: E::Lit(l), .. }) => FieldDefault::Const(lit_value(l)),
                        Some(e) => FieldDefault::Code(self.expr_proto(e, &format!("{}.{}", cd.name, pname), Some(id))),
                    };
                    MemberDef::Prop { name: pname.as_str().into(), vis: Vis::parse(vis), is_static: *is_static, ty: t, default, pos: *pos }
                }
                Member::Const(n, e, pos) => {
                    let value = self.expr_proto(e, &format!("{}::{}", cd.name, n), Some(id));
                    MemberDef::Const { name: n.as_str().into(), value, pos: *pos }
                }
                Member::UseTrait { .. } => continue,
            };
            match placed.get(&it.name) {
                Some(&k) => members[k] = def,
                None => {
                    placed.insert(it.name.clone(), members.len());
                    members.push(def);
                }
            }
        }
    }

    // -- emitting --------------------------------------------------------------

    fn emit(&mut self, op: Op, pos: Pos) -> usize {
        let f = self.f();
        f.code.push(op);
        f.pos.push(pos);
        f.code.len() - 1
    }

    fn here(&mut self) -> u32 {
        self.f().code.len() as u32
    }

    fn patch(&mut self, at: usize, target: u32) {
        let op = &mut self.f().code[at];
        *op = match *op {
            Op::Jump(_) => Op::Jump(target),
            Op::JumpFalse(_, k) => Op::JumpFalse(target, k),
            Op::JumpTrue(_, k) => Op::JumpTrue(target, k),
            Op::JumpFalseKeep(_) => Op::JumpFalseKeep(target),
            Op::JumpTrueKeep(_) => Op::JumpTrueKeep(target),
            Op::JumpNotNull(_) => Op::JumpNotNull(target),
            Op::JumpNull(_) => Op::JumpNull(target),
            Op::IterNext(s, _) => Op::IterNext(s, target),
            Op::IterNext2(s, _) => Op::IterNext2(s, target),
            Op::IterNextStore(s, v, _) => Op::IterNextStore(s, v, target),
            other => other,
        };
    }

    fn patch_here(&mut self, at: usize) {
        let h = self.here();
        self.patch(at, h);
    }

    fn konst(&mut self, v: Value, pos: Pos) {
        let f = self.f();
        f.consts.push(v);
        let i = (f.consts.len() - 1) as u32;
        self.emit(Op::Const(i), pos);
    }

    fn string(&mut self, text: &str) -> u32 {
        let f = self.f();
        if let Some(&i) = f.string_idx.get(text) {
            return i;
        }
        f.strings.push(text.into());
        let i = (f.strings.len() - 1) as u32;
        f.string_idx.insert(text.to_string(), i);
        i
    }

    fn names(&mut self, names: Vec<Rc<str>>) -> u32 {
        let f = self.f();
        f.names.push(names);
        (f.names.len() - 1) as u32
    }

    fn error(&mut self, cls: &'static str, msg: String, pos: Pos) {
        let f = self.f();
        f.errors.push((cls, msg));
        let i = (f.errors.len() - 1) as u32;
        self.emit(Op::Error(i), pos);
    }

    // -- scopes and variables --------------------------------------------------

    fn push_scope(&mut self) {
        self.f().scopes.push(Vec::new());
    }

    fn pop_scope(&mut self) {
        let f = self.f();
        if let Some(scope) = f.scopes.pop() {
            if let Some(first) = scope.iter().map(|v| v.1.slot).min() {
                f.next_slot = f.next_slot.min(first);
            }
        }
    }

    fn alloc(&mut self) -> u16 {
        let f = self.f();
        let s = f.next_slot;
        f.next_slot += 1;
        f.max_slot = f.max_slot.max(f.next_slot);
        if let Some(scope) = f.scopes.last_mut() {
            scope.push((String::new(), Var { slot: s, cell: false, ty: None, is_const: false }, (0, 0, String::new())));
        }
        s
    }

    fn at_top(&mut self) -> bool {
        let f = self.f();
        f.is_main && f.scopes.is_empty()
    }

    /// Declare a local variable in the innermost scope; returns its slot.
    fn declare(&mut self, name: &str, ty: Option<Ty>, is_const: bool, key: Key) -> u16 {
        let cell = self.captured.contains(&key);
        let f = self.f();
        let slot = f.next_slot;
        f.next_slot += 1;
        f.max_slot = f.max_slot.max(f.next_slot);
        f.scopes.last_mut().unwrap().push((name.to_string(), Var { slot, cell, ty, is_const }, key));
        slot
    }

    fn var_is_cell(&mut self, slot: u16) -> bool {
        let f = self.f();
        f.scopes.iter().flatten().any(|v| v.1.slot == slot && v.1.cell && !v.0.is_empty())
    }

    /// Store the value on top of the stack into a newly declared variable.
    fn declare_store(&mut self, name: &str, ty: Option<Ty>, is_const: bool, pos: Pos) {
        if self.at_top() {
            let g = self.global(name);
            self.emit(Op::StoreGlobal(g), pos);
            return;
        }
        let key: Key = (pos.0, pos.1, name.to_string());
        let slot = self.declare(name, ty, is_const, key.clone());
        if self.captured.contains(&key) {
            self.emit(Op::DeclCell(slot), pos);
        } else {
            self.emit(Op::Store(slot), pos);
        }
    }

    fn resolve_in(&mut self, depth: usize, name: &str) -> Option<(Res, Option<Ty>, bool)> {
        {
            let f = &self.fns[depth];
            for scope in f.scopes.iter().rev() {
                for (n, v, _) in scope.iter().rev() {
                    if n == name {
                        return Some((Res::Local(v.slot, v.cell), v.ty.clone(), v.is_const));
                    }
                }
            }
            for (i, u) in f.upvals.iter().enumerate() {
                if u.0 == name {
                    return Some((Res::Up(i as u16), u.2.clone(), u.3));
                }
            }
        }
        if self.fns[depth].nested && depth > 0 {
            let (res, ty, is_const) = self.resolve_in(depth - 1, name)?;
            let src = match res {
                Res::Global(g) => return Some((Res::Global(g), ty, is_const)),
                Res::Local(slot, cell) => {
                    if !cell {
                        let key = self.fns[depth - 1]
                            .scopes
                            .iter()
                            .flatten()
                            .rev()
                            .find(|v| v.0 == name && v.1.slot == slot)
                            .map(|v| v.2.clone());
                        if let Some(k) = key {
                            self.newly.insert(k);
                        }
                    }
                    UpSrc::Local(slot)
                }
                Res::Up(i) => UpSrc::Up(i),
            };
            let f = &mut self.fns[depth];
            f.upvals.push((name.to_string(), src, ty.clone(), is_const));
            return Some((Res::Up((f.upvals.len() - 1) as u16), ty, is_const));
        }
        if let Some(&g) = self.globals.get(name) {
            let (ty, is_const) = self.global_meta.get(name).cloned().unwrap_or((None, true));
            return Some((Res::Global(g), ty, is_const));
        }
        None
    }

    fn resolve(&mut self, name: &str) -> Option<(Res, Option<Ty>, bool)> {
        let d = self.fns.len() - 1;
        self.resolve_in(d, name)
    }

    fn load(&mut self, r: &Res, pos: Pos) {
        let op = match *r {
            Res::Local(s, false) => Op::Load(s),
            Res::Local(s, true) => Op::LoadCell(s),
            Res::Up(i) => Op::LoadUp(i),
            Res::Global(g) => Op::LoadGlobal(g),
        };
        self.emit(op, pos);
    }

    fn store(&mut self, r: &Res, pos: Pos) {
        let op = match *r {
            Res::Local(s, false) => Op::Store(s),
            Res::Local(s, true) => Op::StoreCell(s),
            Res::Up(i) => Op::StoreUp(i),
            Res::Global(g) => Op::StoreGlobal(g),
        };
        self.emit(op, pos);
    }

    // -- statements ------------------------------------------------------------

    fn block(&mut self, stmts: &'a [Stmt]) {
        self.push_scope();
        for st in stmts {
            self.stmt(st);
        }
        self.pop_scope();
    }

    fn stmt(&mut self, st: &'a Stmt) {
        let pos = st.pos;
        match &st.k {
            S::Var(t, name, value) => {
                if t.name == "void" {
                    self.error("TypeException", "variables cannot have type void".into(), pos);
                    return;
                }
                let ty = self.ty(t);
                match value {
                    Some(v) => {
                        self.expr(v);
                        let literal_ok = !ty.nullable && matches!(
                            (&ty.k, &v.k),
                            (TyK::Int, E::Lit(Lit::Int(_)))
                                | (TyK::Float, E::Lit(Lit::Float(_)))
                                | (TyK::Str, E::Lit(Lit::Str(_)))
                                | (TyK::Str, E::Interp(_))
                                | (TyK::Bool, E::Lit(Lit::Bool(_)))
                        );
                        if !ty.is_any() && !literal_ok {
                            let ti = self.ty_idx(t);
                            let w = self.string(&format!("variable '{}'", name));
                            self.emit(Op::Check(ti, w), v.pos);
                        }
                    }
                    None => self.zero(&ty, pos),
                }
                self.declare_store(name, Some(ty), false, pos);
            }
            S::Const(name, value) => {
                self.expr(value);
                self.declare_store(name, None, true, pos);
            }
            S::Assign(target, op, value) => self.assign(target, op, value, pos),
            S::Expr(e) => {
                self.expr(e);
                self.emit(Op::Pop, pos);
            }
            S::If(branches, other) => {
                let mut ends = Vec::new();
                for (cond, body) in branches {
                    self.expr(cond);
                    let j = self.emit(Op::JumpFalse(0, CondKind::Cond), cond.pos);
                    self.block(body);
                    ends.push(self.emit(Op::Jump(0), pos));
                    self.patch_here(j);
                }
                if let Some(o) = other {
                    self.block(o);
                }
                for e in ends {
                    self.patch_here(e);
                }
            }
            S::For(key, var, iterable, body) => {
                self.push_scope();
                let it = self.alloc();
                self.expr(iterable);
                self.emit(Op::IterInit(it, key.is_some()), iterable.pos);
                let top = self.here();
                let var_key: Key = (pos.0, pos.1, var.clone());
                let fused = key.is_none() && !self.captured.contains(&var_key);
                let next;
                self.push_scope();
                if fused {
                    let slot = self.declare(var, None, false, var_key);
                    next = self.emit(Op::IterNextStore(it, slot, 0), pos);
                } else {
                    next = if key.is_some() {
                        self.emit(Op::IterNext2(it, 0), pos)
                    } else {
                        self.emit(Op::IterNext(it, 0), pos)
                    };
                    self.declare_store(var, None, false, pos);
                }
                if let Some(k) = key {
                    // the key is under the value
                    let kpos = (pos.0, pos.1 + 1);
                    self.declare_store(k, None, false, kpos);
                }
                self.f().blocks.push(Block::Loop { breaks: Vec::new(), continues: Vec::new() });
                for s in body {
                    self.stmt(s);
                }
                let blk = self.f().blocks.pop();
                self.pop_scope();
                self.emit(Op::Jump(top), pos);
                self.patch_here(next);
                if let Some(Block::Loop { breaks, continues }) = blk {
                    for b in breaks {
                        self.patch_here(b);
                    }
                    for c in continues {
                        self.patch(c, top);
                    }
                }
                self.pop_scope();
            }
            S::While(cond, body) => {
                let top = self.here();
                self.expr(cond);
                let j = self.emit(Op::JumpFalse(0, CondKind::While), cond.pos);
                self.f().blocks.push(Block::Loop { breaks: Vec::new(), continues: Vec::new() });
                self.block(body);
                let blk = self.f().blocks.pop();
                self.emit(Op::Jump(top), pos);
                self.patch_here(j);
                if let Some(Block::Loop { breaks, continues }) = blk {
                    for b in breaks {
                        self.patch_here(b);
                    }
                    for c in continues {
                        self.patch(c, top);
                    }
                }
            }
            S::DoWhile(body, cond) => {
                let top = self.here();
                self.f().blocks.push(Block::Loop { breaks: Vec::new(), continues: Vec::new() });
                self.block(body);
                let blk = self.f().blocks.pop();
                let cont = self.here();
                self.expr(cond);
                self.emit(Op::JumpTrue(top, CondKind::While), cond.pos);
                if let Some(Block::Loop { breaks, continues }) = blk {
                    for b in breaks {
                        self.patch_here(b);
                    }
                    for c in continues {
                        self.patch(c, cont);
                    }
                }
            }
            S::Break | S::Continue => self.jump_out(matches!(st.k, S::Break), pos),
            S::Return(value) => {
                match value {
                    Some(v) => self.expr(v),
                    None => {
                        self.emit(Op::Null, pos);
                    }
                }
                let vpos = value.as_ref().map(|v| v.pos).unwrap_or(pos);
                if self.f().blocks.iter().any(|b| matches!(b, Block::Try { .. })) {
                    let tmp = self.alloc();
                    self.emit(Op::Store(tmp), pos);
                    let n = self.f().blocks.len();
                    for i in (0..n).rev() {
                        self.leave_block(i, pos);
                    }
                    self.emit(Op::Load(tmp), pos);
                }
                self.emit(Op::Return, vpos);
            }
            S::Raise(value) => match value {
                Some(v) => {
                    self.expr(v);
                    self.emit(Op::Throw, pos);
                }
                None => match self.f().handlers.last().copied() {
                    Some(h) => {
                        self.emit(Op::Load(h), pos);
                        self.emit(Op::Rethrow, pos);
                    }
                    None => self.error(
                        "RuntimeException",
                        "'raise' with no value can only be used inside 'except'".into(),
                        pos,
                    ),
                },
            },
            S::Switch(subject, cases, default) => {
                self.push_scope();
                let subj = self.alloc();
                self.expr(subject);
                self.emit(Op::Store(subj), subject.pos);
                let mut to_body: Vec<Vec<usize>> = Vec::new();
                for (labels, _) in cases {
                    let mut js = Vec::new();
                    for l in labels {
                        self.emit(Op::Load(subj), l.pos);
                        self.expr(l);
                        self.emit(Op::Cmp(CmpOp::Eq), l.pos);
                        js.push(self.emit(Op::JumpTrue(0, CondKind::Raw), l.pos));
                    }
                    to_body.push(js);
                }
                let to_default = self.emit(Op::Jump(0), pos);
                self.f().blocks.push(Block::Switch { breaks: Vec::new() });
                let mut ends = Vec::new();
                for ((_, body), js) in cases.iter().zip(to_body) {
                    for j in js {
                        self.patch_here(j);
                    }
                    self.block(body);
                    ends.push(self.emit(Op::Jump(0), pos));
                }
                self.patch_here(to_default);
                if let Some(d) = default {
                    self.block(d);
                }
                let blk = self.f().blocks.pop();
                for e in ends {
                    self.patch_here(e);
                }
                if let Some(Block::Switch { breaks }) = blk {
                    for b in breaks {
                        self.patch_here(b);
                    }
                }
                self.pop_scope();
            }
            S::Try(body, handlers, fin) => self.try_stmt(body, handlers, fin.as_deref(), pos),
            S::Func(f) => {
                let key: Key = (f.pos.0, f.pos.1, f.name.clone());
                if self.at_top() {
                    return;
                }
                let cell = self.captured.contains(&key);
                let slot = self.declare(&f.name, None, true, key);
                if cell {
                    self.emit(Op::NewCell(slot), pos);
                }
                let ctx = self.f().ctx;
                let proto = self.function(f, None, true, ctx);
                let fs = self.f();
                fs.protos.push(proto);
                let idx = (fs.protos.len() - 1) as u32;
                self.emit(Op::Closure(idx), pos);
                self.emit(if cell { Op::StoreCell(slot) } else { Op::Store(slot) }, pos);
            }
            S::Class(_) => self.error("NameException", "'class' is only allowed at the top level of a file".into(), pos),
            S::Namespace(_) => {
                self.error("NameException", "'namespace' is only allowed at the top level of a file".into(), pos)
            }
            S::Use => self.unsupported("'use' (importing other files and modules)", pos),
            S::Unknown(k) => self.unsupported(format!("the statement '{}'", k), pos),
        }
    }

    fn zero(&mut self, ty: &Ty, pos: Pos) {
        if ty.nullable {
            self.emit(Op::Null, pos);
            return;
        }
        match ty.k {
            TyK::Int => {
                self.emit(Op::Int(0), pos);
            }
            TyK::Float => self.konst(Value::Float(0.0), pos),
            TyK::Str => self.konst(s(""), pos),
            TyK::Bool => {
                self.emit(Op::False, pos);
            }
            TyK::List(_) => {
                self.emit(Op::List(0), pos);
            }
            TyK::Dict(_) => {
                self.emit(Op::Dict(0), pos);
            }
            TyK::Set(_) => {
                self.emit(Op::Set(0), pos);
            }
            _ => {
                self.emit(Op::Null, pos);
            }
        }
    }

    /// Leave block i (going out through it): a try runs its finally.
    fn leave_block(&mut self, i: usize, pos: Pos) {
        let fin = match &self.f().blocks[i] {
            Block::Try { fin } => Some(*fin),
            _ => None,
        };
        if let Some(fin) = fin {
            self.emit(Op::PopTry, pos);
            if let Some(stmts) = fin {
                let saved = self.f().blocks.split_off(i);
                let saved_h = self.f().handlers.clone();
                self.block(stmts);
                self.f().handlers = saved_h;
                self.f().blocks.extend(saved);
            }
        }
    }

    fn jump_out(&mut self, is_break: bool, pos: Pos) {
        let n = self.f().blocks.len();
        for i in (0..n).rev() {
            let kind = match &self.f().blocks[i] {
                Block::Loop { .. } => 0,
                Block::Switch { .. } => 1,
                Block::Try { .. } => 2,
            };
            if kind == 0 || (kind == 1 && is_break) {
                let j = self.emit(Op::Jump(0), pos);
                match &mut self.f().blocks[i] {
                    Block::Loop { breaks, continues } => {
                        if is_break {
                            breaks.push(j)
                        } else {
                            continues.push(j)
                        }
                    }
                    Block::Switch { breaks } => breaks.push(j),
                    _ => {}
                }
                return;
            }
            self.leave_block(i, pos);
        }
        self.error("RuntimeException", "'break' or 'continue' used outside of a loop".into(), pos);
    }

    fn try_stmt(&mut self, body: &'a [Stmt], handlers: &'a [Handler], fin: Option<&'a [Stmt]>, pos: Pos) {
        let has_catch = !handlers.is_empty();
        let t = self.emit(Op::Try(NONE, NONE), pos);
        self.f().blocks.push(Block::Try { fin });
        self.block(body);
        self.f().blocks.pop();
        self.emit(Op::PopTry, pos);
        let mut to_ok = vec![self.emit(Op::Jump(0), pos)];
        let mut catch_at = NONE;
        let mut to_fin_exc = Vec::new();
        if has_catch {
            catch_at = self.here();
            self.push_scope();
            let h = self.alloc();
            self.emit(Op::Store(h), pos);
            if fin.is_some() {
                to_fin_exc.push(self.emit(Op::Try(NONE, NONE), pos));
                self.f().blocks.push(Block::Try { fin });
            }
            self.f().handlers.push(h);
            for hd in handlers {
                let mut skip = None;
                if !hd.types.is_empty() {
                    self.emit(Op::Load(h), hd.pos);
                    let names: Vec<Rc<str>> = hd.types.iter().map(|t| t.as_str().into()).collect();
                    let ni = self.names(names);
                    self.emit(Op::Matches(ni), hd.pos);
                    skip = Some(self.emit(Op::JumpFalse(0, CondKind::Raw), hd.pos));
                }
                self.push_scope();
                if let Some(name) = &hd.name {
                    self.emit(Op::Load(h), hd.pos);
                    self.declare_store(name, None, false, hd.pos);
                }
                for st in &hd.body {
                    self.stmt(st);
                }
                self.pop_scope();
                if fin.is_some() {
                    self.emit(Op::PopTry, pos);
                }
                to_ok.push(self.emit(Op::Jump(0), pos));
                if let Some(sj) = skip {
                    self.patch_here(sj);
                }
            }
            self.f().handlers.pop();
            if fin.is_some() {
                self.f().blocks.pop();
                self.emit(Op::PopTry, pos);
                self.emit(Op::Load(h), pos);
                to_fin_exc.push(self.emit(Op::Jump(0), pos));
            } else {
                self.emit(Op::Load(h), pos);
                self.emit(Op::Rethrow, pos);
            }
            self.pop_scope();
        }
        let mut fin_at = NONE;
        if let Some(stmts) = fin {
            fin_at = self.here();
            self.push_scope();
            let e = self.alloc();
            self.emit(Op::Store(e), pos);
            self.block(stmts);
            self.emit(Op::Load(e), pos);
            self.emit(Op::Resume, pos);
            self.pop_scope();
        }
        let ok = self.here();
        for j in to_ok {
            self.patch(j, ok);
        }
        if let Some(stmts) = fin {
            self.block(stmts);
        }
        self.f().code[t] = Op::Try(catch_at, fin_at);
        for j in to_fin_exc {
            match self.f().code[j] {
                Op::Try(_, _) => self.f().code[j] = Op::Try(NONE, fin_at),
                _ => self.patch(j, fin_at),
            }
        }
    }

    fn guess_type(e: &Expr) -> String {
        match &e.k {
            E::Lit(Lit::Null) => "any".into(),
            E::Lit(Lit::Bool(_)) => "bool".into(),
            E::Lit(Lit::Int(_)) => "int".into(),
            E::Lit(Lit::Float(_)) => "float".into(),
            E::Lit(Lit::Str(_)) | E::Interp(_) => "string".into(),
            E::List(_) => "list<...>".into(),
            E::Dict(_) => "dict<..., ...>".into(),
            E::New(c, _) => c.rsplit('\\').next().unwrap_or(c).to_string(),
            _ => "any".into(),
        }
    }

    fn assign(&mut self, target: &'a Expr, op: &str, value: &'a Expr, pos: Pos) {
        let bin = if op == "=" { None } else { BinOp::parse(&op[..op.len() - 1]) };
        match &target.k {
            E::Name(name) => {
                let Some((res, ty, is_const)) = self.resolve(name) else {
                    let msg = if op == "=" {
                        format!("'{}' is not declared — give it a type, e.g. '{} {} = ...'", name, Self::guess_type(value), name)
                    } else {
                        format!("'{}' is not declared", name)
                    };
                    self.error("NameException", msg, target.pos);
                    return;
                };
                if is_const {
                    self.error("TypeException", format!("cannot reassign constant '{}'", name), target.pos);
                    return;
                }
                self.expr(value);
                if let (Some(b), Res::Local(slot, false)) = (bin, &res) {
                    let (ti, w) = match &ty {
                        Some(t) if !t.is_any() => {
                            let t = t.clone();
                            let f = self.f();
                            f.types.push(t);
                            let ti = (f.types.len() - 1) as u32;
                            (ti, self.string(&format!("variable '{}'", name)))
                        }
                        _ => (NONE, 0),
                    };
                    self.emit(Op::LocalOp(*slot, b, ti, w), pos);
                    return;
                }
                if let Some(b) = bin {
                    self.load(&res, target.pos);
                    self.emit(Op::Swap, pos);
                    self.emit(Op::Bin(b), pos);
                }
                if let Some(t) = ty {
                    if !t.is_any() {
                        let f = self.f();
                        f.types.push(t);
                        let ti = (f.types.len() - 1) as u32;
                        let w = self.string(&format!("variable '{}'", name));
                        self.emit(Op::Check(ti, w), value.pos);
                    }
                }
                self.store(&res, target.pos);
            }
            E::Attr(obj, name, _) => {
                self.expr(obj);
                self.expr(value);
                let n = self.string(name);
                match bin {
                    None => self.emit(Op::SetAttr(n), target.pos),
                    Some(b) => self.emit(Op::SetAttrOp(n, b), target.pos),
                };
            }
            E::Index(obj, key) => {
                self.expr(obj);
                self.expr(key);
                self.expr(value);
                match bin {
                    None => self.emit(Op::SetIndex, target.pos),
                    Some(b) => self.emit(Op::SetIndexOp(b), target.pos),
                };
            }
            E::Static(t, name) => {
                if !self.static_target(t, target.pos) {
                    return;
                }
                self.emit(Op::Swap, pos);
                self.emit(Op::Pop, pos);
                self.expr(value);
                let n = self.string(name);
                match bin {
                    None => self.emit(Op::SetStatic(n), target.pos),
                    Some(b) => self.emit(Op::SetStaticOp(n, b), target.pos),
                };
            }
            _ => self.unsupported("this kind of assignment target", target.pos),
        }
    }

    // -- expressions -----------------------------------------------------------

    fn this(&mut self, pos: Pos) {
        match self.resolve("this") {
            Some((r, _, _)) => self.load(&r, pos),
            None => self.error(
                "NameException",
                "'this' can only be used inside a (non-static) method".into(),
                pos,
            ),
        }
    }

    /// Push `this` (or null) and then the class for `X::name`. False when it is an error.
    fn static_target(&mut self, target: &'a Expr, pos: Pos) -> bool {
        let ctx = self.f().ctx;
        if ctx.is_some() && self.resolve("this").is_some() {
            self.this(pos);
        } else {
            self.emit(Op::Null, pos);
        }
        if let E::Name(n) = &target.k {
            if n == "self" || n == "parent" {
                let Some(c) = ctx else {
                    self.error("NameException", format!("'{}::' can only be used inside a class", n), pos);
                    return false;
                };
                let id = if n == "self" {
                    c
                } else {
                    match self.class_parent[c] {
                        Some(p) => p,
                        None => {
                            let cname = self.class_names[c].clone();
                            self.error("NameException", format!("{} has no parent class", cname), pos);
                            return false;
                        }
                    }
                };
                let g = self.class_globals[id];
                self.emit(Op::LoadGlobal(g), pos);
                return true;
            }
        }
        self.expr(target);
        true
    }

    fn module_of(&mut self, e: &Expr) -> Option<Module> {
        if let E::Name(n) = &e.k {
            let m = match n.as_str() {
                "Math" => Module::Math,
                "Str" => Module::Str,
                _ => return None,
            };
            if self.resolve(n).is_none() {
                return Some(m);
            }
        }
        None
    }

    fn expr(&mut self, e: &'a Expr) {
        let pos = e.pos;
        match &e.k {
            E::Lit(l) => match l {
                Lit::Null => {
                    self.emit(Op::Null, pos);
                }
                Lit::Bool(true) => {
                    self.emit(Op::True, pos);
                }
                Lit::Bool(false) => {
                    self.emit(Op::False, pos);
                }
                Lit::Int(n) if *n >= i32::MIN as i64 && *n <= i32::MAX as i64 => {
                    self.emit(Op::Int(*n as i32), pos);
                }
                _ => self.konst(lit_value(l), pos),
            },
            E::Interp(parts) => {
                for p in parts {
                    match p {
                        Part::Text(t) => self.konst(s(t.clone()), pos),
                        Part::Expr(x) => self.expr(x),
                    }
                }
                self.emit(Op::Interp(parts.len() as u32), pos);
            }
            E::Name(n) => self.name(n, pos),
            E::This => self.this(pos),
            E::List(items) => {
                for x in items {
                    self.expr(x);
                }
                self.emit(Op::List(items.len() as u32), pos);
            }
            E::Set(items) => {
                for x in items {
                    self.expr(x);
                }
                self.emit(Op::Set(items.len() as u32), pos);
            }
            E::Dict(pairs) => {
                for (k, v) in pairs {
                    self.expr(k);
                    self.expr(v);
                }
                self.emit(Op::Dict(pairs.len() as u32), pos);
            }
            E::Unary(op, x) => {
                self.expr(x);
                let o = match op.as_str() {
                    "not" => Op::Not,
                    "-" => Op::Neg,
                    _ => Op::Plus,
                };
                self.emit(o, pos);
            }
            E::Binary(op, a, b) => {
                self.expr(a);
                self.expr(b);
                match BinOp::parse(op) {
                    Some(o) => {
                        self.emit(Op::Bin(o), pos);
                    }
                    None => self.unsupported(format!("the operator '{}'", op), pos),
                }
            }
            E::Logical(op, a, b) => {
                self.expr(a);
                match op.as_str() {
                    "??" => {
                        let j = self.emit(Op::JumpNotNull(0), pos);
                        self.expr(b);
                        self.patch_here(j);
                    }
                    "and" => {
                        self.emit(Op::CheckBool(CondKind::AndLeft), a.pos);
                        let j = self.emit(Op::JumpFalseKeep(0), pos);
                        self.expr(b);
                        self.emit(Op::CheckBool(CondKind::AndRight), b.pos);
                        self.patch_here(j);
                    }
                    _ => {
                        self.emit(Op::CheckBool(CondKind::OrLeft), a.pos);
                        let j = self.emit(Op::JumpTrueKeep(0), pos);
                        self.expr(b);
                        self.emit(Op::CheckBool(CondKind::OrRight), b.pos);
                        self.patch_here(j);
                    }
                }
            }
            E::Compare(op, a, b) => {
                self.expr(a);
                self.expr(b);
                match CmpOp::parse(op) {
                    Some(o) => {
                        self.emit(Op::Cmp(o), pos);
                    }
                    None => self.unsupported(format!("the comparison '{}'", op), pos),
                }
            }
            E::Is(x, t) => {
                self.expr(x);
                let ti = self.ty_idx(t);
                self.emit(Op::Is(ti), pos);
            }
            E::Ternary(c, a, b) => {
                self.expr(c);
                let j = self.emit(Op::JumpFalse(0, CondKind::Ternary), c.pos);
                self.expr(a);
                let end = self.emit(Op::Jump(0), pos);
                self.patch_here(j);
                self.expr(b);
                self.patch_here(end);
            }
            E::Cast(t, x) => {
                self.expr(x);
                let ti = self.ty_idx(t);
                self.emit(Op::Cast(ti), pos);
            }
            E::Call(func, args) => self.call(func, args, pos),
            E::Attr(obj, name, safe) => {
                if let Some(m) = self.module_of(obj) {
                    match module_member(m, name) {
                        Some(ModMember::Value(v)) => self.konst(v, pos),
                        Some(ModMember::Func(b, lo, hi)) => {
                            self.konst(native_value(b, &format!("{}.{}", m.name(), name), lo, hi), pos)
                        }
                        None => self.unsupported(format!("{}.{}", m.name(), name), pos),
                    }
                    return;
                }
                self.expr(obj);
                let j = if *safe { Some(self.emit(Op::JumpNull(0), pos)) } else { None };
                let n = self.string(name);
                self.emit(Op::GetAttr(n), pos);
                if let Some(j) = j {
                    self.patch_here(j);
                }
            }
            E::Index(obj, key) => {
                self.expr(obj);
                self.expr(key);
                self.emit(Op::GetIndex, pos);
            }
            E::Static(target, name) => {
                if let Some(m) = self.module_of(target) {
                    match module_member(m, name) {
                        Some(ModMember::Value(v)) => self.konst(v, pos),
                        Some(ModMember::Func(b, lo, hi)) => {
                            self.konst(native_value(b, &format!("{}.{}", m.name(), name), lo, hi), pos)
                        }
                        None => self.unsupported(format!("{}::{}", m.name(), name), pos),
                    }
                    return;
                }
                if self.static_target(target, pos) {
                    let via_parent = matches!(&target.k, E::Name(n) if n == "parent");
                    let n = self.string(name);
                    self.emit(Op::GetStatic(n, via_parent), pos);
                }
            }
            E::New(cls, args) => {
                let Some(id) = self.class_id(cls) else {
                    self.unsupported(format!("the class '{}'", cls), pos);
                    return;
                };
                let ap = self.arg_positions(args);
                match self.args(args) {
                    Ok(n) => self.emit(Op::New(id as u32, n, ap), pos),
                    Err(names) => self.emit(Op::NewEx(id as u32, names, ap), pos),
                };
            }
            E::Lambda(f) => {
                let ctx = self.f().ctx;
                let proto = self.function(f, None, true, ctx);
                let fs = self.f();
                fs.protos.push(proto);
                let idx = (fs.protos.len() - 1) as u32;
                self.emit(Op::Closure(idx), pos);
            }
            E::Unknown(what) => self.unsupported(what.clone(), pos),
        }
    }

    fn name(&mut self, n: &str, pos: Pos) {
        if let Some((r, _, _)) = self.resolve(n) {
            self.load(&r, pos);
            return;
        }
        if let Some((b, lo, hi)) = builtin_fn(n) {
            self.konst(native_value(b, n, lo, hi), pos);
            return;
        }
        match n {
            "Math" => return self.konst(Value::Module(Module::Math), pos),
            "Str" => return self.konst(Value::Module(Module::Str), pos),
            "__line__" => return self.konst(Value::Int(pos.0 as i64), pos),
            "__file__" => {
                let p = self.path.clone();
                return self.konst(s(p), pos);
            }
            "__namespace__" => {
                let ns = self.namespace.clone();
                return self.konst(s(ns), pos);
            }
            "__class__" => {
                let c = self.f().ctx.map(|c| self.class_names[c].to_string()).unwrap_or_default();
                return self.konst(s(c), pos);
            }
            "__method__" => {
                let m = self.f().method.clone();
                return self.konst(s(m), pos);
            }
            _ => {}
        }
        if let Some(id) = self.class_id(n) {
            let g = self.class_globals[id];
            self.emit(Op::LoadGlobal(g), pos);
            return;
        }
        self.unsupported(format!("'{}'", n), pos);
    }

    /// Push call arguments. Ok(count) for plain positional ones; Err(names)
    /// when there are named or spread ones: then a list of the positional
    /// values is pushed, followed by one value per name.
    fn args(&mut self, args: &'a [Arg]) -> Result<u16, u32> {
        if args.iter().all(|a| matches!(a, Arg::Pos(_))) {
            for a in args {
                if let Arg::Pos(x) = a {
                    self.expr(x);
                }
            }
            return Ok(args.len() as u16);
        }
        let first = match &args[0] {
            Arg::Pos(x) => x.pos,
            Arg::Named(_, _, p) | Arg::Spread(_, p) => *p,
        };
        self.emit(Op::List(0), first);
        for a in args {
            match a {
                Arg::Pos(x) => {
                    self.expr(x);
                    self.emit(Op::List(1), x.pos);
                    self.emit(Op::Extend, x.pos);
                }
                Arg::Spread(x, p) => {
                    self.expr(x);
                    self.emit(Op::Extend, *p);
                }
                Arg::Named(..) => {}
            }
        }
        let mut names = Vec::new();
        for a in args {
            if let Arg::Named(n, x, _) = a {
                self.expr(x);
                names.push(n.as_str().into());
            }
        }
        Err(self.names(names))
    }

    fn arg_positions(&mut self, args: &'a [Arg]) -> u32 {
        let ps: Vec<Pos> = args
            .iter()
            .map(|a| match a {
                Arg::Pos(x) => x.pos,
                Arg::Named(_, _, p) | Arg::Spread(_, p) => *p,
            })
            .collect();
        let f = self.f();
        f.arg_pos.push(ps);
        (f.arg_pos.len() - 1) as u32
    }

    fn call(&mut self, func: &'a Expr, args: &'a [Arg], pos: Pos) {
        match &func.k {
            E::Name(n) if self.resolve(n).is_none() => {
                if let Some((b, _, _)) = builtin_fn(n) {
                    match self.args(args) {
                        Ok(argc) => {
                            self.emit(Op::CallBuiltin(b, argc), pos);
                        }
                        Err(_) => self.error(
                            "TypeException",
                            format!("{}() is built in and takes positional arguments only — named arguments work with functions written in Barq", n),
                            pos,
                        ),
                    }
                    return;
                }
            }
            E::Attr(obj, name, safe) => {
                if let Some(m) = self.module_of(obj) {
                    match module_member(m, name) {
                        Some(ModMember::Func(b, _, _)) => match self.args(args) {
                            Ok(argc) => {
                                self.emit(Op::CallBuiltin(b, argc), pos);
                            }
                            Err(_) => self.error(
                                "TypeException",
                                format!("{}.{}() is built in and takes positional arguments only — named arguments work with functions written in Barq", m.name(), name),
                                pos,
                            ),
                        },
                        Some(ModMember::Value(v)) => {
                            self.konst(v, pos);
                            match self.args(args) {
                                Ok(argc) => self.emit(Op::Call(argc, NONE), pos),
                                Err(names) => self.emit(Op::CallEx(names, NONE), pos),
                            };
                        }
                        None => self.unsupported(format!("{}.{}", m.name(), name), pos),
                    }
                    return;
                }
                self.expr(obj);
                let n = self.string(name);
                let ap = self.arg_positions(args);
                match self.args(args) {
                    Ok(argc) => self.emit(Op::CallMethod(n, argc, *safe, ap), pos),
                    Err(names) => self.emit(Op::CallMethodEx(n, names, *safe, ap), pos),
                };
                return;
            }
            _ => {}
        }
        self.expr(func);
        let ap = self.arg_positions(args);
        match self.args(args) {
            Ok(argc) => self.emit(Op::Call(argc, ap), pos),
            Err(names) => self.emit(Op::CallEx(names, ap), pos),
        };
    }
}
