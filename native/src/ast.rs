//! The syntax tree, read from the JSON form of `barq/nodes.py`.

use crate::json::J;
use std::rc::Rc;

/// (line, column), both 1-based; 0 means unknown.
pub type Pos = (u32, u32);

#[derive(Debug, Clone)]
pub struct TypeRef {
    pub name: String,
    pub args: Vec<TypeRef>,
    pub nullable: bool,
    pub pos: Pos,
}

#[derive(Debug, Clone)]
pub enum Lit {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
}

#[derive(Debug)]
pub enum Part {
    Text(String),
    Expr(Expr),
}

#[derive(Debug)]
pub enum Arg {
    Pos(Expr),
    Named(String, Expr, Pos),
    Spread(Expr, Pos),
}

#[derive(Debug)]
pub struct Expr {
    pub pos: Pos,
    pub k: E,
}

#[derive(Debug)]
pub enum E {
    Lit(Lit),
    Interp(Vec<Part>),
    Name(String),
    This,
    List(Vec<Expr>),
    Dict(Vec<(Expr, Expr)>),
    Set(Vec<Expr>),
    Unary(String, Box<Expr>),
    Binary(String, Box<Expr>, Box<Expr>),
    Logical(String, Box<Expr>, Box<Expr>),
    Compare(String, Box<Expr>, Box<Expr>),
    Is(Box<Expr>, TypeRef),
    Ternary(Box<Expr>, Box<Expr>, Box<Expr>),
    Cast(TypeRef, Box<Expr>),
    Call(Box<Expr>, Vec<Arg>),
    Attr(Box<Expr>, String, bool),
    Index(Box<Expr>, Box<Expr>),
    Static(Box<Expr>, String),
    New(String, Vec<Arg>),
    Lambda(Rc<Func>),
    /// A node kind the native VM does not know (reported when compiled).
    Unknown(String),
}

#[derive(Debug)]
pub struct Param {
    pub ty: Option<TypeRef>,
    pub name: String,
    pub default: Option<Expr>,
    pub variadic: bool,
    pub pos: Pos,
}

#[derive(Debug)]
pub enum Body {
    Block(Vec<Stmt>),
    Expr(Expr),
}

#[derive(Debug)]
pub struct Func {
    pub name: String,
    pub params: Vec<Param>,
    pub ret: Option<TypeRef>,
    pub visibility: Option<String>,
    pub is_static: bool,
    pub is_abstract: bool,
    pub body: Body,
    pub pos: Pos,
}

#[derive(Debug)]
pub struct Handler {
    pub types: Vec<String>,
    pub name: Option<String>,
    pub body: Vec<Stmt>,
    pub pos: Pos,
}

#[derive(Debug)]
pub struct Stmt {
    pub pos: Pos,
    pub k: S,
}

#[derive(Debug)]
pub enum S {
    Var(TypeRef, String, Option<Expr>),
    Const(String, Expr),
    Assign(Expr, String, Expr),
    Expr(Expr),
    If(Vec<(Expr, Vec<Stmt>)>, Option<Vec<Stmt>>),
    For(Option<String>, String, Expr, Vec<Stmt>),
    While(Expr, Vec<Stmt>),
    DoWhile(Vec<Stmt>, Expr),
    Break,
    Continue,
    Return(Option<Expr>),
    Raise(Option<Expr>),
    Switch(Expr, Vec<(Vec<Expr>, Vec<Stmt>)>, Option<Vec<Stmt>>),
    Try(Vec<Stmt>, Vec<Handler>, Option<Vec<Stmt>>),
    Func(Rc<Func>),
    Class(Rc<ClassDecl>),
    Namespace(String),
    Use(Vec<UseImport>),
    Unknown(String),
}

/// One import from a `use` statement: `use App\Models\User` → fqn="App\Models\User", alias=None.
#[derive(Debug, Clone)]
pub struct UseImport {
    pub fqn: String,
    pub alias: Option<String>,
}

#[derive(Debug)]
pub enum Member {
    Method(Rc<Func>),
    Prop { vis: String, is_static: bool, ty: TypeRef, name: String, value: Option<Expr>, pos: Pos },
    Const(String, Expr, Pos),
    UseTrait { names: Vec<String>, rules: Vec<TraitRule>, pos: Pos },
}

/// A rule under `use A, B` in a class body:
///   A::hello insteadof B   |   B::hello as helloB   |   hello as protected
#[derive(Debug)]
pub struct TraitRule {
    pub trait_name: Option<String>,
    pub method: String,
    pub insteadof: Option<Vec<String>>,
    pub alias: Option<String>,
    pub vis: Option<String>,
    pub pos: Pos,
}

#[derive(Debug)]
pub struct ClassDecl {
    pub name: String,
    pub parent: Option<String>,
    pub is_abstract: bool,
    pub members: Vec<Member>,
    pub is_enum: bool,
    pub backing: Option<String>,
    pub cases: Vec<(String, Option<Lit>, Pos)>,
    pub interfaces: Vec<String>,
    pub kind: String,
    pub pos: Pos,
}

// ---------------------------------------------------------------------------

fn pos(j: &J) -> Pos {
    (j.get("l").int() as u32, j.get("c").int() as u32)
}

fn kind(j: &J) -> &str {
    j.get("k").str()
}

pub fn type_ref(j: &J) -> TypeRef {
    TypeRef {
        name: j.get("name").str().to_string(),
        args: j.get("args").arr().iter().map(type_ref).collect(),
        nullable: j.get("nullable").bool(),
        pos: pos(j),
    }
}

fn opt_type(j: &J) -> Option<TypeRef> {
    if j.is_null() {
        None
    } else {
        Some(type_ref(j))
    }
}

pub fn lit(j: &J) -> Lit {
    match j {
        J::Null => Lit::Null,
        J::Bool(b) => Lit::Bool(*b),
        J::Int(n) => Lit::Int(*n),
        J::Num(f) => Lit::Float(*f),
        J::Str(s) => Lit::Str(s.clone()),
        J::Obj(_) => match kind(j) {
            "Float" => Lit::Float(parse_py_float(j.get("v").str())),
            "Literal" => lit(j.get("value")),
            _ => Lit::Null,
        },
        J::Arr(_) => Lit::Null,
    }
}

fn parse_py_float(s: &str) -> f64 {
    match s {
        "inf" => f64::INFINITY,
        "-inf" => f64::NEG_INFINITY,
        "nan" => f64::NAN,
        _ => s.parse().unwrap_or(0.0),
    }
}

fn exprs(j: &J) -> Vec<Expr> {
    j.arr().iter().map(expr).collect()
}

fn bx(j: &J) -> Box<Expr> {
    Box::new(expr(j))
}

fn args(j: &J) -> Vec<Arg> {
    j.arr()
        .iter()
        .map(|a| match kind(a) {
            "NamedArg" => Arg::Named(a.get("name").str().to_string(), expr(a.get("value")), pos(a)),
            "Spread" => Arg::Spread(expr(a.get("value")), pos(a)),
            _ => Arg::Pos(expr(a)),
        })
        .collect()
}

pub fn expr(j: &J) -> Expr {
    let p = pos(j);
    let s = |f: &str| j.get(f).str().to_string();
    let k = match kind(j) {
        "Literal" => {
            let v = j.get("value");
            if let J::Obj(_) = v {
                if kind(v) == "BigInt" {
                    E::Unknown("an integer literal that does not fit in 64 bits".into())
                } else {
                    E::Lit(lit(v))
                }
            } else {
                E::Lit(lit(v))
            }
        }
        "Interp" => E::Interp(
            j.get("parts")
                .arr()
                .iter()
                .map(|x| match x {
                    J::Str(t) => Part::Text(t.clone()),
                    _ => Part::Expr(expr(x)),
                })
                .collect(),
        ),
        "Name" => E::Name(s("id")),
        "This" => E::This,
        "ListLit" => E::List(exprs(j.get("items"))),
        "SetLit" => E::Set(exprs(j.get("items"))),
        "DictLit" => E::Dict(
            j.get("pairs")
                .arr()
                .iter()
                .map(|kv| (expr(&kv.arr()[0]), expr(&kv.arr()[1])))
                .collect(),
        ),
        "Unary" => E::Unary(s("op"), bx(j.get("operand"))),
        "Binary" => E::Binary(s("op"), bx(j.get("left")), bx(j.get("right"))),
        "Logical" => E::Logical(s("op"), bx(j.get("left")), bx(j.get("right"))),
        "Compare" => E::Compare(s("op"), bx(j.get("left")), bx(j.get("right"))),
        "IsType" => E::Is(bx(j.get("expr")), type_ref(j.get("type"))),
        "Ternary" => E::Ternary(bx(j.get("cond")), bx(j.get("then")), bx(j.get("other"))),
        "Cast" => E::Cast(type_ref(j.get("type")), bx(j.get("expr"))),
        "Call" => E::Call(bx(j.get("func")), args(j.get("args"))),
        "Attr" => E::Attr(bx(j.get("obj")), s("name"), j.get("safe").bool()),
        "Index" => E::Index(bx(j.get("obj")), bx(j.get("index"))),
        "StaticRef" => {
            let t = j.get("target");
            let target = match t {
                J::Str(name) => Expr { pos: p, k: E::Name(name.clone()) },
                _ => expr(t),
            };
            E::Static(Box::new(target), s("name"))
        }
        "New" => E::New(s("cls"), args(j.get("args"))),
        "Lambda" => {
            let is_expr = j.get("is_expr").bool();
            E::Lambda(Rc::new(Func {
                name: "<lambda>".into(),
                params: params(j.get("params")),
                ret: opt_type(j.get("ret")),
                visibility: None,
                is_static: false,
                is_abstract: false,
                body: if is_expr { Body::Expr(expr(j.get("body"))) } else { Body::Block(block(j.get("body"))) },
                pos: p,
            }))
        }
        other => E::Unknown(format!("the expression '{}'", other)),
    };
    Expr { pos: p, k }
}

fn params(j: &J) -> Vec<Param> {
    j.arr()
        .iter()
        .map(|p| Param {
            ty: opt_type(p.get("type")),
            name: p.get("name").str().to_string(),
            default: if p.get("default").is_null() { None } else { Some(expr(p.get("default"))) },
            variadic: p.get("variadic").bool(),
            pos: pos(p),
        })
        .collect()
}

pub fn func(j: &J) -> Rc<Func> {
    let body = j.get("body");
    Rc::new(Func {
        name: j.get("name").str().to_string(),
        params: params(j.get("params")),
        ret: opt_type(j.get("ret")),
        visibility: j.get("visibility").opt_str(),
        is_static: j.get("is_static").bool(),
        is_abstract: j.get("is_abstract").bool(),
        body: Body::Block(if body.is_null() { Vec::new() } else { block(body) }),
        pos: pos(j),
    })
}

pub fn block(j: &J) -> Vec<Stmt> {
    j.arr().iter().map(stmt).collect()
}

fn opt_block(j: &J) -> Option<Vec<Stmt>> {
    if j.is_null() {
        None
    } else {
        Some(block(j))
    }
}

fn opt_expr(j: &J) -> Option<Expr> {
    if j.is_null() {
        None
    } else {
        Some(expr(j))
    }
}

pub fn stmt(j: &J) -> Stmt {
    let p = pos(j);
    let k = match kind(j) {
        "VarDecl" => S::Var(type_ref(j.get("type")), j.get("name").str().into(), opt_expr(j.get("value"))),
        "ConstDecl" => S::Const(j.get("name").str().into(), expr(j.get("value"))),
        "Assign" => S::Assign(expr(j.get("target")), j.get("op").str().into(), expr(j.get("value"))),
        "ExprStmt" => S::Expr(expr(j.get("expr"))),
        "If" => S::If(
            j.get("branches").arr().iter().map(|b| (expr(&b.arr()[0]), block(&b.arr()[1]))).collect(),
            opt_block(j.get("other")),
        ),
        "For" => S::For(
            j.get("key").opt_str(),
            j.get("var").str().into(),
            expr(j.get("iterable")),
            block(j.get("body")),
        ),
        "While" => S::While(expr(j.get("cond")), block(j.get("body"))),
        "DoWhile" => S::DoWhile(block(j.get("body")), expr(j.get("cond"))),
        "Break" => S::Break,
        "Continue" => S::Continue,
        "Return" => S::Return(opt_expr(j.get("value"))),
        "Raise" => S::Raise(opt_expr(j.get("value"))),
        "Switch" => S::Switch(
            expr(j.get("subject")),
            j.get("cases")
                .arr()
                .iter()
                .map(|c| (exprs(&c.arr()[0]), block(&c.arr()[1])))
                .collect(),
            opt_block(j.get("default")),
        ),
        "Try" => S::Try(
            block(j.get("body")),
            j.get("handlers")
                .arr()
                .iter()
                .map(|h| {
                    let a = h.arr();
                    let hp = a.get(3).map(|x| (x.arr()[0].int() as u32, x.arr()[1].int() as u32)).unwrap_or(p);
                    Handler {
                        types: a[0].arr().iter().map(|t| t.str().to_string()).collect(),
                        name: a[1].opt_str(),
                        body: block(&a[2]),
                        pos: hp,
                    }
                })
                .collect(),
            opt_block(j.get("final")),
        ),
        "FuncDecl" => S::Func(func(j)),
        "ClassDecl" => S::Class(Rc::new(class_decl(j))),
        "Namespace" => S::Namespace(j.get("name").str().into()),
        "Use" => {
            let imports = j.get("imports").arr().iter().map(|item| {
                let pair = item.arr();
                UseImport {
                    fqn: pair[0].str().to_string(),
                    alias: pair.get(1).and_then(|v| if v.is_null() { None } else { Some(v.str().to_string()) }),
                }
            }).collect();
            S::Use(imports)
        }
        other => S::Unknown(other.to_string()),
    };
    Stmt { pos: p, k }
}

fn class_decl(j: &J) -> ClassDecl {
    let members = j
        .get("members")
        .arr()
        .iter()
        .map(|m| match kind(m) {
            "FuncDecl" => Member::Method(func(m)),
            "Property" => Member::Prop {
                vis: m.get("visibility").opt_str().unwrap_or_else(|| "public".into()),
                is_static: m.get("is_static").bool(),
                ty: type_ref(m.get("type")),
                name: m.get("name").str().into(),
                value: opt_expr(m.get("value")),
                pos: pos(m),
            },
            "ConstDecl" => Member::Const(m.get("name").str().into(), expr(m.get("value")), pos(m)),
            _ => Member::UseTrait {
                names: m.get("names").arr().iter().map(|x| x.str().to_string()).collect(),
                rules: m
                    .get("rules")
                    .arr()
                    .iter()
                    .map(|r| TraitRule {
                        trait_name: r.get("trait").opt_str(),
                        method: r.get("method").str().to_string(),
                        insteadof: if r.get("insteadof").is_null() {
                            None
                        } else {
                            Some(r.get("insteadof").arr().iter().map(|x| x.str().to_string()).collect())
                        },
                        alias: r.get("alias").opt_str(),
                        vis: r.get("visibility").opt_str(),
                        pos: pos(r),
                    })
                    .collect(),
                pos: pos(m),
            },
        })
        .collect();
    ClassDecl {
        name: j.get("name").str().into(),
        parent: j.get("parent").opt_str(),
        is_abstract: j.get("is_abstract").bool(),
        members,
        is_enum: j.get("is_enum").bool(),
        backing: j.get("backing").opt_str(),
        cases: j
            .get("cases")
            .arr()
            .iter()
            .map(|c| {
                let v = c.get("value");
                (c.get("name").str().to_string(), if v.is_null() { None } else { Some(lit(v)) }, pos(c))
            })
            .collect(),
        interfaces: j.get("interfaces").arr().iter().map(|x| x.str().to_string()).collect(),
        kind: j.get("kind").opt_str().unwrap_or_else(|| "class".into()),
        pos: pos(j),
    }
}
