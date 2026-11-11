//! Runtime values. Collections and objects are shared and reference-counted,
//! like Python's objects in the reference interpreter.

use crate::ast::Pos;
use crate::code::{Builtin, Proto};
use std::cell::RefCell;
use std::collections::HashMap;
use std::hash::{Hash, Hasher};
use std::rc::Rc;

/// A string with its length in characters (Barq indexes strings by character).
pub struct BStr {
    pub s: String,
    pub chars: usize,
}

impl BStr {
    pub fn new(s: String) -> BStr {
        let chars = if s.is_ascii() { s.len() } else { s.chars().count() };
        BStr { s, chars }
    }
    pub fn is_ascii(&self) -> bool {
        self.chars == self.s.len()
    }
}

#[derive(Clone)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(Rc<BStr>),
    List(Rc<RefCell<Vec<Value>>>),
    Dict(Rc<RefCell<OMap>>),
    Set(Rc<RefCell<OMap>>),
    Range(Rc<RangeV>),
    Func(Rc<Closure>),
    Native(Rc<NativeFn>),
    Bound(Rc<Bound>),
    /// A method of a built-in type taken as a value: `nums.push`.
    BMethod(Rc<(Value, Rc<str>)>),
    Class(Rc<Class>),
    Obj(Rc<Obj>),
    Module(Module),
    // -- internal, never seen by Barq code --
    Cell(Rc<RefCell<Value>>),
    Iter(Rc<RefCell<IterState>>),
    /// A global that has not been declared yet, or a missing argument.
    Undef,
    /// exit()/die() travelling through a `finally` block.
    Exit(i32),
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Module {
    Math,
    Str,
}

impl Module {
    pub fn name(self) -> &'static str {
        match self {
            Module::Math => "Math",
            Module::Str => "Str",
        }
    }
}

pub struct RangeV {
    pub start: i64,
    pub stop: i64,
    pub step: i64,
}

impl RangeV {
    pub fn len(&self) -> i64 {
        let (a, b, s) = (self.start as i128, self.stop as i128, self.step as i128);
        let n = if s > 0 {
            if b > a { (b - a + s - 1) / s } else { 0 }
        } else if a > b {
            (a - b - s - 1) / (-s)
        } else {
            0
        };
        n as i64
    }
    pub fn get(&self, i: i64) -> i64 {
        self.start + i * self.step
    }
}

pub struct Closure {
    pub proto: Rc<Proto>,
    pub up: Vec<Rc<RefCell<Value>>>,
}

pub struct NativeFn {
    pub b: Builtin,
    pub name: String,
    pub min: usize,
    /// None: any number of arguments
    pub max: Option<usize>,
    pub owner: Option<usize>,
    #[allow(dead_code)]
    pub is_static: bool,
}

pub struct Bound {
    pub this: Value,
    pub func: Value,
}

pub enum IterState {
    Range { cur: i64, stop: i64, step: i64, i: i64 },
    Items { items: Vec<Value>, i: usize },
    Pairs { items: Vec<(Value, Value)>, i: usize },
}

// ---------------------------------------------------------------------------
// classes and objects
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Vis {
    Public,
    Protected,
    Private,
}

impl Vis {
    pub fn parse(s: &str) -> Vis {
        match s {
            "private" => Vis::Private,
            "protected" => Vis::Protected,
            _ => Vis::Public,
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Vis::Public => "public",
            Vis::Protected => "protected",
            Vis::Private => "private",
        }
    }
}

#[derive(Clone)]
pub enum FieldDefault {
    /// zero value of the type (a fresh list/dict/set when it is one)
    Zero,
    Const(Value),
    Code(Rc<Proto>),
}

#[derive(Clone)]
pub struct FieldInfo {
    pub name: Rc<str>,
    pub ty: Option<Ty>,
    pub vis: Vis,
    pub owner: usize,
    pub default: FieldDefault,
    #[allow(dead_code)]
    pub pos: Pos,
}

pub struct Method {
    #[allow(dead_code)]
    pub name: Rc<str>,
    pub owner: usize,
    pub vis: Vis,
    pub is_static: bool,
    pub is_abstract: bool,
    /// Value::Func or Value::Native
    pub func: Value,
}

pub struct StaticSlot {
    pub owner: usize,
    pub vis: Vis,
    pub ty: Option<Ty>,
    pub is_const: bool,
    pub value: RefCell<Value>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Class,
    Interface,
    Trait,
    Enum,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Class => "class",
            Kind::Interface => "interface",
            Kind::Trait => "trait",
            Kind::Enum => "enum",
        }
    }
}

pub struct Class {
    pub id: usize,
    pub name: Rc<str>,
    pub fqn: Rc<str>,
    pub kind: Kind,
    pub is_abstract: bool,
    pub backing: Option<String>,
    pub parent: Option<Rc<Class>>,
    pub interfaces: Vec<Rc<Class>>,
    /// instance properties, ancestors first
    pub fields: Vec<FieldInfo>,
    pub field_index: HashMap<Rc<str>, usize>,
    /// methods, inherited ones included
    pub methods: HashMap<Rc<str>, Rc<Method>>,
    /// static properties and constants, inherited and interface ones included
    pub statics: HashMap<Rc<str>, Rc<StaticSlot>>,
    /// own static names in declaration order (for messages)
    pub cases: RefCell<Vec<Value>>,
    pub has_get: bool,
    pub has_set: bool,
    pub has_to_string: bool,
}

impl Class {
    pub fn is_enum(&self) -> bool {
        self.kind == Kind::Enum
    }
    pub fn is_subclass_of(&self, other: &Class) -> bool {
        let mut c: Option<&Class> = Some(self);
        while let Some(cls) = c {
            if cls.id == other.id {
                return true;
            }
            if other.kind == Kind::Interface {
                for i in &cls.interfaces {
                    if i.is_subclass_of(other) {
                        return true;
                    }
                }
            }
            c = cls.parent.as_deref();
        }
        false
    }
    pub fn chain(&self) -> Vec<&Class> {
        let mut out = Vec::new();
        let mut c: Option<&Class> = Some(self);
        while let Some(cls) = c {
            out.push(cls);
            c = cls.parent.as_deref();
        }
        out.reverse();
        out
    }
}

pub struct ExcInfo {
    pub pos: Pos,
    pub trace: Vec<(Rc<str>, u32)>,
}

pub struct Obj {
    pub cls: Rc<Class>,
    pub fields: RefCell<Vec<Value>>,
    pub exc: RefCell<Option<Box<ExcInfo>>>,
}

impl Obj {
    pub fn new(cls: Rc<Class>, fields: Vec<Value>) -> Obj {
        Obj { cls, fields: RefCell::new(fields), exc: RefCell::new(None) }
    }
    pub fn field(&self, name: &str) -> Value {
        match self.cls.field_index.get(name) {
            Some(&i) => self.fields.borrow()[i].clone(),
            None => Value::Null,
        }
    }
}

// ---------------------------------------------------------------------------
// types
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub enum TyK {
    Any,
    Int,
    Float,
    Str,
    Bool,
    List(Option<Box<Ty>>),
    Dict(Option<Box<(Ty, Ty)>>),
    Set(Option<Box<Ty>>),
    Range,
    Karo,
    Void,
    Null,
    Class(usize),
    /// a class name the program does not define
    Unknown(String),
}

#[derive(Clone, Debug)]
pub struct Ty {
    pub k: TyK,
    pub nullable: bool,
    pub name: Rc<str>,
    /// type_str(): how the type is written in messages
    pub text: Rc<str>,
}

impl Ty {
    pub fn is_any(&self) -> bool {
        matches!(self.k, TyK::Any)
    }
    pub fn non_nullable(&self) -> bool {
        matches!(self.k, TyK::Int | TyK::Float | TyK::Bool)
    }
}

// ---------------------------------------------------------------------------
// dict and set storage: insertion-ordered hash map
// ---------------------------------------------------------------------------

/// Hash key of a value. Dicts follow Python: 1, 1.0 and true are one key.
/// Sets keep true and 1 apart (runtime.hash_key).
#[derive(Clone)]
pub enum HKey {
    Null,
    Int(i64),
    Float(u64),
    Bool(bool),
    Str(Rc<BStr>),
    Obj(usize),
}

impl PartialEq for HKey {
    fn eq(&self, o: &HKey) -> bool {
        match (self, o) {
            (HKey::Null, HKey::Null) => true,
            (HKey::Int(a), HKey::Int(b)) => a == b,
            (HKey::Float(a), HKey::Float(b)) => a == b,
            (HKey::Bool(a), HKey::Bool(b)) => a == b,
            (HKey::Str(a), HKey::Str(b)) => a.s == b.s,
            (HKey::Obj(a), HKey::Obj(b)) => a == b,
            _ => false,
        }
    }
}
impl Eq for HKey {}

impl Hash for HKey {
    fn hash<H: Hasher>(&self, h: &mut H) {
        match self {
            HKey::Null => 0u8.hash(h),
            HKey::Int(n) => {
                1u8.hash(h);
                n.hash(h)
            }
            HKey::Float(b) => {
                2u8.hash(h);
                b.hash(h)
            }
            HKey::Bool(b) => {
                3u8.hash(h);
                b.hash(h)
            }
            HKey::Str(s) => {
                4u8.hash(h);
                s.s.hash(h)
            }
            HKey::Obj(p) => {
                5u8.hash(h);
                p.hash(h)
            }
        }
    }
}

/// None when the value cannot be a key (lists, dicts, functions...).
pub fn hkey(v: &Value, set_mode: bool) -> Option<HKey> {
    Some(match v {
        Value::Null => HKey::Null,
        Value::Bool(b) => {
            if set_mode {
                HKey::Bool(*b)
            } else {
                HKey::Int(*b as i64)
            }
        }
        Value::Int(n) => HKey::Int(*n),
        Value::Float(f) => {
            if f.fract() == 0.0 && f.abs() < 9.2e18 {
                HKey::Int(*f as i64)
            } else {
                HKey::Float(f.to_bits())
            }
        }
        Value::Str(s) => HKey::Str(s.clone()),
        Value::Obj(o) => HKey::Obj(Rc::as_ptr(o) as *const u8 as usize),
        _ => return None,
    })
}

#[derive(Clone, Default)]
pub struct OMap {
    entries: Vec<Option<(Value, Value)>>,
    index: HashMap<HKey, usize>,
    pub set_mode: bool,
}

impl OMap {
    pub fn new(set_mode: bool) -> OMap {
        OMap { entries: Vec::new(), index: HashMap::new(), set_mode }
    }
    pub fn len(&self) -> usize {
        self.index.len()
    }
    pub fn get(&self, k: &HKey) -> Option<&Value> {
        self.index.get(k).and_then(|&i| self.entries[i].as_ref().map(|e| &e.1))
    }
    pub fn contains(&self, k: &HKey) -> bool {
        self.index.contains_key(k)
    }
    /// Insert or replace. A replaced entry keeps its place and its original key.
    pub fn insert(&mut self, hk: HKey, key: Value, value: Value) {
        if let Some(&i) = self.index.get(&hk) {
            if let Some(e) = self.entries[i].as_mut() {
                e.1 = value;
            }
            return;
        }
        self.index.insert(hk, self.entries.len());
        self.entries.push(Some((key, value)));
    }
    pub fn remove(&mut self, k: &HKey) -> Option<Value> {
        let i = self.index.remove(k)?;
        let e = self.entries[i].take();
        if self.entries.len() > 32 && self.index.len() * 2 < self.entries.len() {
            self.compact();
        }
        e.map(|e| e.1)
    }
    fn compact(&mut self) {
        let old = std::mem::take(&mut self.entries);
        self.index.clear();
        for e in old.into_iter().flatten() {
            let hk = hkey(&e.0, self.set_mode).unwrap();
            self.index.insert(hk, self.entries.len());
            self.entries.push(Some(e));
        }
    }
    pub fn clear(&mut self) {
        self.entries.clear();
        self.index.clear();
    }
    pub fn iter(&self) -> impl Iterator<Item = &(Value, Value)> {
        self.entries.iter().flatten()
    }
    pub fn keys(&self) -> Vec<Value> {
        self.iter().map(|e| e.0.clone()).collect()
    }
    pub fn values(&self) -> Vec<Value> {
        self.iter().map(|e| e.1.clone()).collect()
    }
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

pub fn s(text: impl Into<String>) -> Value {
    Value::Str(Rc::new(BStr::new(text.into())))
}

pub fn list(items: Vec<Value>) -> Value {
    Value::List(Rc::new(RefCell::new(items)))
}

pub fn dict(m: OMap) -> Value {
    Value::Dict(Rc::new(RefCell::new(m)))
}

pub fn set(m: OMap) -> Value {
    Value::Set(Rc::new(RefCell::new(m)))
}

pub fn type_name(v: &Value) -> String {
    match v {
        Value::Null => "null".into(),
        Value::Bool(_) => "bool".into(),
        Value::Int(_) => "int".into(),
        Value::Float(_) => "float".into(),
        Value::Str(_) => "string".into(),
        Value::List(_) => "list".into(),
        Value::Dict(_) => "dict".into(),
        Value::Set(_) => "set".into(),
        Value::Range(_) => "range".into(),
        Value::Obj(o) => o.cls.name.to_string(),
        Value::Class(_) => "class".into(),
        Value::Func(_) | Value::Native(_) | Value::Bound(_) | Value::BMethod(_) => "karo".into(),
        Value::Module(_) => "module".into(),
        _ => "internal".into(),
    }
}

pub fn is_callable(v: &Value) -> bool {
    matches!(v, Value::Func(_) | Value::Native(_) | Value::Bound(_) | Value::BMethod(_))
}

/// Strict equality (runtime.values_equal): different types are never equal,
/// except int and float, which compare by value.
pub fn values_equal(a: &Value, b: &Value) -> bool {
    use Value::*;
    match (a, b) {
        (Null, Null) => true,
        (Null, _) | (_, Null) => false,
        (Bool(x), Bool(y)) => x == y,
        (Bool(_), _) | (_, Bool(_)) => false,
        (Int(x), Int(y)) => x == y,
        (Int(x), Float(y)) => (*x as f64) == *y,
        (Float(x), Int(y)) => *x == (*y as f64),
        (Float(x), Float(y)) => x == y,
        (Str(x), Str(y)) => Rc::ptr_eq(x, y) || x.s == y.s,
        (List(x), List(y)) => {
            if Rc::ptr_eq(x, y) {
                return true;
            }
            let (x, y) = (x.borrow(), y.borrow());
            x.len() == y.len() && x.iter().zip(y.iter()).all(|(p, q)| values_equal(p, q))
        }
        (Dict(x), Dict(y)) => {
            if Rc::ptr_eq(x, y) {
                return true;
            }
            let (x, y) = (x.borrow(), y.borrow());
            if x.len() != y.len() {
                return false;
            }
            for (k, v) in x.iter() {
                match hkey(k, false).and_then(|h| y.get(&h).cloned()) {
                    Some(w) if values_equal(v, &w) => {}
                    _ => return false,
                }
            }
            true
        }
        (Set(x), Set(y)) => {
            let (x, y) = (x.borrow(), y.borrow());
            x.len() == y.len() && x.iter().all(|(k, _)| hkey(k, true).map(|h| y.contains(&h)).unwrap_or(false))
        }
        (Range(x), Range(y)) => {
            let (n, m) = (x.len(), y.len());
            n == m && (n == 0 || (x.start == y.start && (n == 1 || x.step == y.step)))
        }
        (Obj(x), Obj(y)) => Rc::ptr_eq(x, y),
        (Class(x), Class(y)) => x.id == y.id,
        (Func(x), Func(y)) => Rc::ptr_eq(x, y),
        (Native(x), Native(y)) => Rc::ptr_eq(x, y),
        (Bound(x), Bound(y)) => Rc::ptr_eq(x, y),
        (Module(x), Module(y)) => x == y,
        _ => false,
    }
}
