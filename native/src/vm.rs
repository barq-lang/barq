//! The bytecode interpreter. Behaviour, messages included, follows the
//! reference interpreter (barq/interpreter.py): both must print the same.

use crate::ast::Pos;
use crate::code::*;
use crate::compiler::{ClassDef, MemberDef, Program, EXCEPTIONS};
use crate::value::*;
use std::cell::RefCell;
use std::collections::HashMap;
use std::io::Write;
use std::rc::Rc;

pub const MAX_CALL_DEPTH: usize = 5000;

pub enum Flow {
    Throw(Rc<Obj>),
    Exit(i32),
    /// an error in the VM itself, or a compile error found while setting up classes
    Fatal(String),
    Compile(String, Pos),
}

pub type R<T> = Result<T, Flow>;

struct Handler {
    catch: u32,
    fin: u32,
    sp: usize,
}

pub struct Vm {
    pub stack: Vec<Value>,
    pub globals: Vec<Value>,
    pub global_names: Vec<String>,
    pub classes: Vec<Rc<Class>>,
    pub class_by_name: HashMap<String, usize>,
    pub calls: Vec<(Rc<str>, u32)>,
    pub ctx: Option<usize>,
    pub call_line: u32,
    pub argv: Vec<String>,
    pub path: String,
    pub lines: Vec<String>,
    pub out: Vec<u8>,
    pub rng: u64,
    pub color: bool,
    /// the call being made: its function and arg_pos index, for argument error positions
    pub site: (*const Proto, u32),
    /// a position for the error being raised, when it is not the instruction's
    pub err_at: Option<Pos>,
}

impl Vm {
    pub fn new(path: String, source: &str, argv: Vec<String>, global_names: Vec<String>) -> Vm {
        let seed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0x9E3779B97F4A7C15);
        let text = source.replace("\r\n", "\n").replace('\r', "\n");
        Vm {
            stack: Vec::with_capacity(1024),
            globals: Vec::new(),
            global_names,
            classes: Vec::new(),
            class_by_name: HashMap::new(),
            calls: Vec::new(),
            ctx: None,
            call_line: 0,
            argv,
            path,
            lines: text.split('\n').map(|l| l.to_string()).collect(),
            out: Vec::with_capacity(1 << 16),
            rng: seed | 1,
            color: std::env::var("BARQ_COLOR").map(|v| v == "1").unwrap_or(false),
            site: (std::ptr::null(), NONE),
            err_at: None,
        }
    }

    // =====================================================================
    // output
    // =====================================================================

    pub fn write(&mut self, text: &str) {
        self.out.extend_from_slice(text.as_bytes());
        if self.out.len() > 1 << 16 {
            self.flush();
        }
    }

    pub fn flush(&mut self) {
        if !self.out.is_empty() {
            let mut o = std::io::stdout().lock();
            let _ = o.write_all(&self.out);
            let _ = o.flush();
            self.out.clear();
        }
    }

    // =====================================================================
    // errors
    // =====================================================================

    pub fn exc(&self, cls: &str, msg: impl Into<String>) -> Flow {
        let id = *self.class_by_name.get(cls).unwrap_or(&0);
        let c = self.classes[id].clone();
        let mut fields = vec![Value::Null; c.fields.len()];
        if let Some(&i) = c.field_index.get("message") {
            fields[i] = s(msg.into());
        }
        if let Some(&i) = c.field_index.get("code") {
            fields[i] = Value::Int(0);
        }
        Flow::Throw(Rc::new(Obj::new(c, fields)))
    }

    pub fn fail<T>(&self, cls: &str, msg: impl Into<String>) -> R<T> {
        Err(self.exc(cls, msg))
    }

    fn stamp(&self, o: &Rc<Obj>, pos: Pos) {
        let mut e = o.exc.borrow_mut();
        if e.is_none() {
            *e = Some(Box::new(ExcInfo { pos, trace: self.calls.clone() }));
        }
    }

    pub fn format_location(&self, line: u32, col: u32) -> String {
        let (red, dim, reset) = if self.color { ("\x1b[31m", "\x1b[2m", "\x1b[0m") } else { ("", "", "") };
        let mut out = format!("  {}at{} {}:{}", dim, reset, self.path, line);
        if col > 0 {
            out.push_str(&format!(":{}", col));
        }
        if line > 0 {
            if let Some(text) = self.lines.get(line as usize - 1) {
                if !text.trim().is_empty() {
                    let gutter = format!("{:>4} | ", line);
                    out.push_str(&format!("\n{}{}{}{}", dim, gutter, reset, text));
                    if col > 0 {
                        out.push_str(&format!(
                            "\n{}{}^{}",
                            " ".repeat(gutter.chars().count() + col as usize - 1),
                            red,
                            reset
                        ));
                    }
                }
            }
        }
        out
    }

    /// cli.report_uncaught
    pub fn report_uncaught(&self, o: &Rc<Obj>) -> String {
        let (red, dim, reset) = if self.color { ("\x1b[1;31m", "\x1b[2m", "\x1b[0m") } else { ("", "", "") };
        let message = match o.field("message") {
            Value::Str(t) => t.s.clone(),
            Value::Null => "null".into(),
            v => crate::builtins::plain_display(&v),
        };
        let mut out = format!("{}Uncaught {}:{} {}\n", red, o.cls.name, reset, message);
        if let Some(info) = o.exc.borrow().as_ref() {
            out.push_str(&self.format_location(info.pos.0, info.pos.1));
            out.push('\n');
            let lines: Vec<String> = info
                .trace
                .iter()
                .rev()
                .map(|(name, line)| format!("  {}in{} {}() {}called from{} {}:{}\n", dim, reset, name, dim, reset, self.path, line))
                .collect();
            let mut i = 0;
            while i < lines.len() {
                let mut j = i;
                while j < lines.len() && lines[j] == lines[i] {
                    j += 1;
                }
                let run = j - i;
                for l in &lines[i..i + run.min(3)] {
                    out.push_str(l);
                }
                if run > 3 {
                    out.push_str(&format!("  {}... the same call {} more times{}\n", dim, run - 3, reset));
                }
                i = j;
            }
        }
        out
    }

    // =====================================================================
    // setting up classes and globals
    // =====================================================================

    pub fn load(&mut self, prog: &Program) -> R<()> {
        self.globals = vec![Value::Undef; prog.nglobals];
        // built-in exception classes
        for (id, (name, parent)) in EXCEPTIONS.iter().enumerate() {
            let parent = parent.map(|p| self.classes[*self.class_by_name.get(p).unwrap()].clone());
            let mut cls = Class {
                id,
                name: (*name).into(),
                fqn: (*name).into(),
                kind: Kind::Class,
                is_abstract: false,
                backing: None,
                parent: parent.clone(),
                interfaces: Vec::new(),
                fields: Vec::new(),
                field_index: HashMap::new(),
                methods: HashMap::new(),
                statics: HashMap::new(),
                cases: RefCell::new(Vec::new()),
                has_get: false,
                has_set: false,
                has_to_string: false,
            };
            match &parent {
                Some(p) => {
                    cls.fields = p.fields.clone();
                    cls.field_index = p.field_index.clone();
                    cls.methods = p.methods.clone();
                }
                None => {
                    let t = |n: &str, k: TyK| Ty { k, nullable: false, name: n.into(), text: n.into() };
                    for (fname, ty) in [("message", t("string", TyK::Str)), ("code", t("int", TyK::Int))] {
                        cls.field_index.insert(fname.into(), cls.fields.len());
                        cls.fields.push(FieldInfo {
                            name: fname.into(),
                            ty: Some(ty),
                            vis: Vis::Public,
                            owner: 0,
                            default: FieldDefault::Zero,
                            pos: (0, 0),
                        });
                    }
                    for (mname, b, lo, hi) in [
                        ("__construct", Builtin::ExcConstruct, 0, 2),
                        ("getMessage", Builtin::ExcGetMessage, 0, 0),
                        ("getCode", Builtin::ExcGetCode, 0, 0),
                    ] {
                        let f = Value::Native(Rc::new(NativeFn {
                            b,
                            name: mname.to_string(),
                            min: lo,
                            max: Some(hi),
                            owner: Some(0),
                            is_static: false,
                        }));
                        cls.methods.insert(
                            mname.into(),
                            Rc::new(Method { name: mname.into(), owner: 0, vis: Vis::Public, is_static: false, is_abstract: false, func: f }),
                        );
                    }
                }
            }
            let rc = Rc::new(cls);
            self.class_by_name.insert(name.to_string(), id);
            self.classes.push(rc.clone());
            self.globals[prog.class_globals[id] as usize] = Value::Class(rc);
        }
        for (g, proto) in &prog.functions {
            self.globals[*g as usize] = Value::Func(Rc::new(Closure { proto: proto.clone(), up: Vec::new() }));
        }
        // user classes: parents and interfaces first
        let mut done = vec![false; prog.classes.len()];
        for k in 0..prog.classes.len() {
            self.finish_class(prog, k, &mut done, &mut Vec::new())?;
        }
        Ok(())
    }

    fn finish_class(&mut self, prog: &Program, k: usize, done: &mut Vec<bool>, stack: &mut Vec<usize>) -> R<()> {
        if done[k] {
            return Ok(());
        }
        let def: &ClassDef = &prog.classes[k];
        if stack.contains(&k) {
            return Err(Flow::Compile(format!("class {} cannot extend itself", def.name), def.pos));
        }
        stack.push(k);
        let base = EXCEPTIONS.len();
        let deps: Vec<usize> = def.parent.iter().chain(def.interfaces.iter()).copied().collect();
        for d in deps {
            if d >= base {
                self.finish_class(prog, d - base, done, stack)?;
            }
        }
        stack.pop();
        done[k] = true;
        let compile = |msg: String| Err(Flow::Compile(msg, def.pos));
        let parent = def.parent.map(|p| self.classes[p].clone());
        if let Some(p) = &parent {
            match p.kind {
                Kind::Interface => {
                    return compile(format!("{} is an interface — write 'class {} implements {}'", p.name, def.name, p.name))
                }
                Kind::Enum => return compile(format!("class {} cannot extend enum {}", def.name, p.name)),
                _ => {}
            }
        }
        let mut interfaces = Vec::new();
        for &i in &def.interfaces {
            let iface = self.classes[i].clone();
            if iface.kind != Kind::Interface {
                if def.kind == Kind::Interface {
                    return compile(format!(
                        "interface {} can only extend interfaces, and {} is a {}",
                        def.name,
                        iface.name,
                        iface.kind.name()
                    ));
                }
                let hint = if iface.kind == Kind::Class {
                    format!(" — a class extends it: class {} extends {}", def.name, iface.name)
                } else {
                    String::new()
                };
                return compile(format!("{} is not an interface{}", iface.name, hint));
            }
            interfaces.push(iface);
        }
        let id = def.id;
        let mut fields: Vec<FieldInfo> = parent.as_ref().map(|p| p.fields.clone()).unwrap_or_default();
        let mut field_index: HashMap<Rc<str>, usize> = parent.as_ref().map(|p| p.field_index.clone()).unwrap_or_default();
        let mut methods: HashMap<Rc<str>, Rc<Method>> = parent.as_ref().map(|p| p.methods.clone()).unwrap_or_default();
        let mut statics: HashMap<Rc<str>, Rc<StaticSlot>> = parent.as_ref().map(|p| p.statics.clone()).unwrap_or_default();
        for iface in &interfaces {
            for (n, sl) in &iface.statics {
                statics.insert(n.clone(), sl.clone());
            }
        }
        let mut add_field = |f: FieldInfo, fields: &mut Vec<FieldInfo>| match field_index.get(&f.name) {
            Some(&i) => fields[i] = f,
            None => {
                field_index.insert(f.name.clone(), fields.len());
                fields.push(f);
            }
        };
        let is_enum = def.kind == Kind::Enum;
        let mut own_methods: Vec<Rc<str>> = Vec::new();
        if is_enum {
            let t = |n: &str| Ty {
                k: if n == "int" { TyK::Int } else { TyK::Str },
                nullable: false,
                name: n.into(),
                text: n.into(),
            };
            add_field(
                FieldInfo { name: "name".into(), ty: Some(t("string")), vis: Vis::Public, owner: id, default: FieldDefault::Zero, pos: def.pos },
                &mut fields,
            );
            if let Some(b) = &def.backing {
                add_field(
                    FieldInfo { name: "value".into(), ty: Some(t(b)), vis: Vis::Public, owner: id, default: FieldDefault::Zero, pos: def.pos },
                    &mut fields,
                );
            }
            let mut natives = vec![("cases", Builtin::EnumCases(id), 0)];
            if def.backing.is_some() {
                natives.push(("from", Builtin::EnumFrom(id), 1));
                natives.push(("tryFrom", Builtin::EnumTryFrom(id), 1));
            }
            for (mname, b, n) in natives {
                let f = Value::Native(Rc::new(NativeFn {
                    b,
                    name: format!("{}::{}", def.name, mname),
                    min: n,
                    max: Some(n),
                    owner: Some(id),
                    is_static: true,
                }));
                methods.insert(mname.into(), Rc::new(Method { name: mname.into(), owner: id, vis: Vis::Public, is_static: true, is_abstract: false, func: f }));
            }
        }
        // statics are evaluated after the class exists, in declaration order
        let mut static_inits: Vec<(Rc<str>, Rc<StaticSlot>, Option<Rc<Proto>>, Option<Value>, Option<Ty>, Pos)> = Vec::new();
        if is_enum {
            for (cname, _, _) in &def.cases {
                if def.members.iter().any(|m| matches!(m, MemberDef::Const { name, .. } if name == cname)) {
                    return compile(format!("'{}' is already a constant of enum {}", cname, def.name));
                }
                let slot = Rc::new(StaticSlot { owner: id, vis: Vis::Public, ty: None, is_const: true, value: RefCell::new(Value::Null) });
                statics.insert(cname.clone(), slot);
            }
        }
        for m in &def.members {
            match m {
                MemberDef::Method { name, proto, vis, is_static, is_abstract, pos, from_trait, .. } => {
                    if own_methods.contains(name) && from_trait.is_none() {
                        return Err(Flow::Compile(format!("method '{}' is declared twice in {}", name, def.name), *pos));
                    }
                    if *is_abstract && !def.is_abstract && from_trait.is_none() {
                        return Err(Flow::Compile(
                            format!("class {} has abstract method '{}' so it must be declared 'abstract class'", def.name, name),
                            *pos,
                        ));
                    }
                    own_methods.push(name.clone());
                    let f = Value::Func(Rc::new(Closure { proto: proto.clone(), up: Vec::new() }));
                    methods.insert(
                        name.clone(),
                        Rc::new(Method { name: name.clone(), owner: id, vis: *vis, is_static: *is_static, is_abstract: *is_abstract, func: f }),
                    );
                }
                MemberDef::Prop { name, vis, is_static, ty, default, pos } => {
                    if *is_static {
                        let slot = Rc::new(StaticSlot { owner: id, vis: *vis, ty: Some(ty.clone()), is_const: false, value: RefCell::new(Value::Null) });
                        statics.insert(name.clone(), slot.clone());
                        let (code, value) = match default {
                            FieldDefault::Zero => (None, None),
                            FieldDefault::Const(v) => (None, Some(v.clone())),
                            FieldDefault::Code(p) => (Some(p.clone()), None),
                        };
                        static_inits.push((name.clone(), slot, code, value, Some(ty.clone()), *pos));
                    } else {
                        add_field(
                            FieldInfo { name: name.clone(), ty: Some(ty.clone()), vis: *vis, owner: id, default: default.clone(), pos: *pos },
                            &mut fields,
                        );
                    }
                }
                MemberDef::Const { name, value, pos } => {
                    let slot = Rc::new(StaticSlot { owner: id, vis: Vis::Public, ty: None, is_const: true, value: RefCell::new(Value::Null) });
                    statics.insert(name.clone(), slot.clone());
                    static_inits.push((name.clone(), slot, Some(value.clone()), None, None, *pos));
                }
            }
        }
        let has_get = methods.contains_key("__get");
        let has_set = methods.contains_key("__set");
        let has_to_string = methods.contains_key("__toString");
        let cls = Rc::new(Class {
            id,
            name: def.name.clone(),
            fqn: def.fqn.clone(),
            kind: def.kind,
            is_abstract: def.is_abstract,
            backing: def.backing.clone(),
            parent,
            interfaces,
            fields,
            field_index,
            methods,
            statics,
            cases: RefCell::new(Vec::new()),
            has_get,
            has_set,
            has_to_string,
        });
        while self.classes.len() <= id {
            self.classes.push(cls.clone());
        }
        self.classes[id] = cls.clone();
        self.class_by_name.insert(def.name.to_string(), id);
        self.class_by_name.insert(def.fqn.to_string(), id);
        self.globals[prog.class_globals[id] as usize] = Value::Class(cls.clone());
        if is_enum {
            let mut cases = Vec::new();
            for (cname, value, _) in &def.cases {
                let mut f = vec![Value::Null; cls.fields.len()];
                f[cls.field_index["name"]] = s(cname.to_string());
                if def.backing.is_some() {
                    f[cls.field_index["value"]] = value.clone();
                }
                let obj = Value::Obj(Rc::new(Obj::new(cls.clone(), f)));
                *cls.statics[cname].value.borrow_mut() = obj.clone();
                cases.push(obj);
            }
            *cls.cases.borrow_mut() = cases;
        }
        for (name, slot, code, value, ty, pos) in static_inits {
            let v = match (code, value) {
                (Some(p), _) => {
                    let saved = self.ctx;
                    self.ctx = Some(id);
                    let r = self.run_expr(&p);
                    self.ctx = saved;
                    r?
                }
                (None, Some(v)) => v,
                (None, None) => match &ty {
                    Some(t) => self.zero_value(t),
                    None => Value::Null,
                },
            };
            if let Some(t) = &ty {
                if let Err(e) = self.check_assign(&v, t, &format!("static property '{}::{}'", def.name, name)) {
                    if let Flow::Throw(o) = &e {
                        self.stamp(o, pos);
                    }
                    return Err(e);
                }
            }
            *slot.value.borrow_mut() = v;
        }
        if !cls.is_abstract {
            // abstract methods left unimplemented
            for c in cls.chain() {
                let _ = c;
            }
            let mut missing: Vec<(Rc<str>, String)> = Vec::new();
            for c in cls.chain() {
                let cdef = if c.id >= base { Some(&prog.classes[c.id - base]) } else { None };
                if let Some(cd) = cdef {
                    for m in &cd.members {
                        if let MemberDef::Method { name, is_abstract, from_trait, .. } = m {
                            missing.retain(|x| &x.0 != name);
                            if *is_abstract {
                                let source = match from_trait {
                                    Some(t) => format!("trait {}", t),
                                    None => c.name.to_string(),
                                };
                                missing.push((name.clone(), source));
                            }
                        }
                    }
                }
            }
            if let Some((name, source)) = missing.first() {
                return compile(format!("class {} must implement abstract method '{}()' from {}", def.name, name, source));
            }
            // interfaces
            let mut all: Vec<Rc<Class>> = Vec::new();
            let mut todo: Vec<Rc<Class>> = Vec::new();
            for c in cls.chain() {
                todo.extend(c.interfaces.iter().cloned());
            }
            while !todo.is_empty() {
                let i = todo.remove(0);
                if !all.iter().any(|x| x.id == i.id) {
                    todo.extend(i.interfaces.iter().cloned());
                    all.push(i);
                }
            }
            for iface in all {
                let idef = &prog.classes[iface.id - base];
                for m in &idef.members {
                    if let MemberDef::Method { name, is_static, required, takes, .. } = m {
                        let Some(imp) = cls.methods.get(name).filter(|x| !x.is_abstract) else {
                            return compile(format!(
                                "class {} must implement method '{}()' from interface {}",
                                def.name, name, iface.name
                            ));
                        };
                        if imp.vis != Vis::Public {
                            return compile(format!("{}.{}() must be public: interface {} declares it", def.name, name, iface.name));
                        }
                        if imp.is_static != *is_static {
                            let want = if *is_static { "static" } else { "not static" };
                            return compile(format!("{}.{}() must be {}, as in interface {}", def.name, name, want, iface.name));
                        }
                        if let Value::Func(cl) = &imp.func {
                            let ps = &cl.proto.params;
                            let itakes = if ps.last().map(|p| p.variadic).unwrap_or(false) { usize::MAX } else { ps.len() };
                            let ireq = ps.iter().filter(|p| p.default.is_none() && !p.variadic).count();
                            if itakes < *takes || ireq > *required {
                                return compile(format!(
                                    "{}.{}() must accept the arguments of interface {}: it takes {}, the interface {}",
                                    def.name,
                                    name,
                                    iface.name,
                                    arity_text(ps.len(), ireq, itakes == usize::MAX),
                                    arity_text(if *takes == usize::MAX { 0 } else { *takes }, *required, *takes == usize::MAX)
                                ));
                            }
                        }
                    }
                }
            }
        }
        Ok(())
    }

    pub fn run_expr(&mut self, p: &Rc<Proto>) -> R<Value> {
        let cl = Rc::new(Closure { proto: p.clone(), up: Vec::new() });
        let base = self.stack.len();
        let r = self.run_closure(&cl, base);
        self.stack.truncate(base);
        r
    }

    pub fn run_main(&mut self, main: &Rc<Proto>) -> R<()> {
        let cl = Rc::new(Closure { proto: main.clone(), up: Vec::new() });
        let base = self.stack.len();
        self.run_closure(&cl, base)?;
        Ok(())
    }

    // =====================================================================
    // the interpreter loop
    // =====================================================================

    pub fn run_closure(&mut self, cl: &Rc<Closure>, base: usize) -> R<Value> {
        let proto = cl.proto.clone();
        let need = base + proto.nlocals;
        if self.stack.len() < need {
            self.stack.resize(need, Value::Null);
        }
        for &sl in &proto.cell_params {
            let i = base + sl as usize;
            let v = std::mem::replace(&mut self.stack[i], Value::Null);
            self.stack[i] = Value::Cell(Rc::new(RefCell::new(v)));
        }
        let mut ip = 0usize;
        let mut handlers: Vec<Handler> = Vec::new();
        loop {
            match self.exec(&proto, cl, base, &mut ip, &mut handlers) {
                Ok(v) => return Ok(v),
                Err(flow) => {
                    if let Flow::Throw(o) = &flow {
                        let at = if ip > 0 { proto.pos[ip - 1] } else { (0, 0) };
                        let pos = self.err_at.take().unwrap_or(at);
                        self.stamp(o, pos);
                    }
                    let mut flow = Some(flow);
                    loop {
                        let Some(h) = handlers.pop() else {
                            return Err(flow.unwrap());
                        };
                        let f = flow.take().unwrap();
                        match f {
                            Flow::Throw(o) if h.catch != NONE => {
                                self.stack.truncate(h.sp);
                                self.stack.push(Value::Obj(o));
                                ip = h.catch as usize;
                                break;
                            }
                            Flow::Throw(o) if h.fin != NONE => {
                                self.stack.truncate(h.sp);
                                self.stack.push(Value::Obj(o));
                                ip = h.fin as usize;
                                break;
                            }
                            Flow::Exit(c) if h.fin != NONE => {
                                self.stack.truncate(h.sp);
                                self.stack.push(Value::Exit(c));
                                ip = h.fin as usize;
                                break;
                            }
                            Flow::Fatal(_) | Flow::Compile(..) => return Err(f),
                            other => flow = Some(other),
                        }
                    }
                }
            }
        }
    }

    #[inline(always)]
    fn pop(&mut self) -> Value {
        match self.stack.pop() {
            Some(v) => v,
            None => Value::Null,
        }
    }

    #[inline(always)]
    fn push(&mut self, v: Value) {
        self.stack.push(v);
    }

    /// Drop the top n values, which the caller knows hold no references.
    #[inline(always)]
    fn drop_plain(&mut self, n: usize) {
        let len = self.stack.len();
        debug_assert!(self.stack[len - n..].iter().all(is_plain));
        // SAFETY: the values are ints, floats, bools or null: nothing to free.
        unsafe { self.stack.set_len(len - n) }
    }

    /// Store into a slot; skips running drop code when the old value is plain.
    #[inline(always)]
    fn put(&mut self, i: usize, v: Value) {
        let slot = &mut self.stack[i];
        if is_plain(slot) {
            // SAFETY: the old value owns nothing.
            unsafe { std::ptr::write(slot, v) }
        } else {
            *slot = v;
        }
    }

    fn exec(&mut self, proto: &Rc<Proto>, cl: &Rc<Closure>, base: usize, ip: &mut usize, handlers: &mut Vec<Handler>) -> R<Value> {
        let code = &proto.code[..];
        let mut pc = *ip;
        loop {
            let op = code[pc];
            pc += 1;
            *ip = pc;
            match op {
                Op::Const(i) => {
                    let v = proto.consts[i as usize].clone();
                    self.push(v)
                }
                Op::Null => self.push(Value::Null),
                Op::True => self.push(Value::Bool(true)),
                Op::False => self.push(Value::Bool(false)),
                Op::Int(n) => self.push(Value::Int(n as i64)),
                Op::Load(sl) => {
                    let v = clone_fast(&self.stack[base + sl as usize]);
                    self.push(v)
                }
                Op::Store(sl) => {
                    let v = self.pop();
                    self.put(base + sl as usize, v);
                }
                Op::LoadCell(sl) => {
                    let v = match &self.stack[base + sl as usize] {
                        Value::Cell(c) => c.borrow().clone(),
                        other => other.clone(),
                    };
                    self.push(v)
                }
                Op::StoreCell(sl) => {
                    let v = self.pop();
                    let i = base + sl as usize;
                    match &self.stack[i] {
                        Value::Cell(c) => *c.borrow_mut() = v,
                        _ => self.stack[i] = v,
                    }
                }
                Op::DeclCell(sl) => {
                    let v = self.pop();
                    self.stack[base + sl as usize] = Value::Cell(Rc::new(RefCell::new(v)));
                }
                Op::NewCell(sl) => {
                    self.stack[base + sl as usize] = Value::Cell(Rc::new(RefCell::new(Value::Null)));
                }
                Op::LoadUp(i) => {
                    let v = cl.up[i as usize].borrow().clone();
                    self.push(v)
                }
                Op::StoreUp(i) => {
                    let v = self.pop();
                    *cl.up[i as usize].borrow_mut() = v;
                }
                Op::LoadGlobal(g) => {
                    let v = self.globals[g as usize].clone();
                    if let Value::Undef = v {
                        return self.fail("NameException", format!("'{}' is not defined", self.global_names[g as usize]));
                    }
                    self.push(v)
                }
                Op::StoreGlobal(g) => {
                    let v = self.pop();
                    self.globals[g as usize] = v;
                }
                Op::Pop => {
                    if self.stack.last().map(is_plain).unwrap_or(false) {
                        self.drop_plain(1);
                    } else {
                        self.stack.pop();
                    }
                }
                Op::Swap => {
                    let n = self.stack.len();
                    self.stack.swap(n - 1, n - 2);
                }
                Op::Bin(o) => {
                    let n = self.stack.len();
                    // fast paths for int and float maths
                    let fast = match (&self.stack[n - 2], &self.stack[n - 1], o) {
                        (Value::Int(a), Value::Int(b), BinOp::Add) => a.checked_add(*b).map(Value::Int),
                        (Value::Int(a), Value::Int(b), BinOp::Sub) => a.checked_sub(*b).map(Value::Int),
                        (Value::Int(a), Value::Int(b), BinOp::Mul) => a.checked_mul(*b).map(Value::Int),
                        (Value::Int(a), Value::Int(b), BinOp::Mod) if *b != 0 => a.checked_rem(*b).map(Value::Int),
                        (Value::Int(a), Value::Int(b), BinOp::Div) if *b != 0 => a.checked_div(*b).map(Value::Int),
                        (Value::Float(a), Value::Float(b), BinOp::Add) => Some(Value::Float(a + b)),
                        (Value::Float(a), Value::Float(b), BinOp::Sub) => Some(Value::Float(a - b)),
                        (Value::Float(a), Value::Float(b), BinOp::Mul) => Some(Value::Float(a * b)),
                        _ => None,
                    };
                    match fast {
                        Some(v) => {
                            self.drop_plain(2);
                            self.push(v);
                        }
                        None => {
                            let b = self.pop();
                            let a = self.pop();
                            let v = self.binop(o, a, b)?;
                            self.push(v);
                        }
                    }
                }
                Op::Neg => {
                    let v = self.pop();
                    let r = match v {
                        Value::Int(n) => match n.checked_neg() {
                            Some(m) => Value::Int(m),
                            None => return self.overflow(),
                        },
                        Value::Float(f) => Value::Float(-f),
                        other => {
                            return self.fail("TypeException", format!("unary '-' needs a number, got {}", type_name(&other)))
                        }
                    };
                    self.push(r)
                }
                Op::Plus => {
                    let v = self.pop();
                    match v {
                        Value::Int(_) | Value::Float(_) => self.push(v),
                        other => {
                            return self.fail("TypeException", format!("unary '+' needs a number, got {}", type_name(&other)))
                        }
                    }
                }
                Op::Not => match self.pop() {
                    Value::Bool(b) => self.push(Value::Bool(!b)),
                    other => return self.fail("TypeException", format!("'not' needs a bool, got {}", type_name(&other))),
                },
                Op::Cmp(o) => {
                    let n = self.stack.len();
                    let fast = match (&self.stack[n - 2], &self.stack[n - 1]) {
                        (Value::Int(a), Value::Int(b)) => match o {
                            CmpOp::Eq => Some(a == b),
                            CmpOp::Ne => Some(a != b),
                            CmpOp::Lt => Some(a < b),
                            CmpOp::Gt => Some(a > b),
                            CmpOp::Le => Some(a <= b),
                            CmpOp::Ge => Some(a >= b),
                            _ => None,
                        },
                        _ => None,
                    };
                    match fast {
                        Some(b) => {
                            self.drop_plain(2);
                            self.push(Value::Bool(b));
                        }
                        None => {
                            let b = self.pop();
                            let a = self.pop();
                            let r = self.compare(o, &a, &b)?;
                            self.push(Value::Bool(r));
                        }
                    }
                }
                Op::Is(t) => {
                    let v = self.pop();
                    let ty = &proto.types[t as usize];
                    let r = match (&ty.k, &v) {
                        (TyK::Null, _) => matches!(v, Value::Null),
                        (_, Value::Null) => ty.nullable,
                        _ => self.matches(&v, ty)?,
                    };
                    self.push(Value::Bool(r))
                }
                Op::Cast(t) => {
                    let v = self.pop();
                    let r = crate::builtins::cast(self, &proto.types[t as usize], v)?;
                    self.push(r)
                }
                Op::Check(t, w) => {
                    let ty = &proto.types[t as usize];
                    let v = self.stack.last().unwrap();
                    let ok = match (&ty.k, v) {
                        (TyK::Int, Value::Int(_)) | (TyK::Float, Value::Float(_)) | (TyK::Str, Value::Str(_)) | (TyK::Bool, Value::Bool(_)) => true,
                        _ => {
                            let v = v.clone();
                            self.matches(&v, ty)?
                        }
                    };
                    if !ok {
                        let v = self.stack.last().unwrap().clone();
                        self.check_assign(&v, ty, &proto.strings[w as usize])?;
                    }
                }
                Op::Jump(t) => pc = t as usize,
                Op::JumpFalse(t, k) => match self.pop() {
                    Value::Bool(false) => pc = t as usize,
                    Value::Bool(true) => {}
                    other => return self.cond_error(&other, k),
                },
                Op::JumpTrue(t, k) => match self.pop() {
                    Value::Bool(true) => pc = t as usize,
                    Value::Bool(false) => {}
                    other => return self.cond_error(&other, k),
                },
                Op::CheckBool(k) => {
                    if !matches!(self.stack.last(), Some(Value::Bool(_))) {
                        let v = self.pop();
                        return self.cond_error(&v, k);
                    }
                }
                Op::JumpFalseKeep(t) => {
                    if matches!(self.stack.last(), Some(Value::Bool(false))) {
                        pc = t as usize;
                    } else {
                        self.stack.pop();
                    }
                }
                Op::JumpTrueKeep(t) => {
                    if matches!(self.stack.last(), Some(Value::Bool(true))) {
                        pc = t as usize;
                    } else {
                        self.stack.pop();
                    }
                }
                Op::JumpNotNull(t) => {
                    if matches!(self.stack.last(), Some(Value::Null)) {
                        self.stack.pop();
                    } else {
                        pc = t as usize;
                    }
                }
                Op::JumpNull(t) => {
                    if matches!(self.stack.last(), Some(Value::Null)) {
                        pc = t as usize;
                    }
                }
                Op::List(n) => {
                    let at = self.stack.len() - n as usize;
                    let items = self.stack.split_off(at);
                    self.push(list(items))
                }
                Op::Extend => {
                    let x = self.pop();
                    let Value::List(src) = x else {
                        return self.fail(
                            "TypeException",
                            format!("only a list can be spread into arguments with *, got {}", type_name(&x)),
                        );
                    };
                    if let Some(Value::List(dst)) = self.stack.last() {
                        let items: Vec<Value> = src.borrow().clone();
                        dst.borrow_mut().extend(items);
                    }
                }
                Op::Dict(n) => {
                    let at = self.stack.len() - 2 * n as usize;
                    let items = self.stack.split_off(at);
                    let mut m = OMap::new(false);
                    let mut it = items.into_iter();
                    while let (Some(k), Some(v)) = (it.next(), it.next()) {
                        let hk = self.key_of(&k, "dict key")?;
                        m.insert(hk, k, v);
                    }
                    self.push(dict(m))
                }
                Op::Set(n) => {
                    let at = self.stack.len() - n as usize;
                    let items = self.stack.split_off(at);
                    let mut m = OMap::new(true);
                    for v in items {
                        let hk = self.key_of(&v, "set item")?;
                        m.insert(hk, v, Value::Null);
                    }
                    self.push(set(m))
                }
                Op::Interp(n) => {
                    let at = self.stack.len() - n as usize;
                    let parts = self.stack.split_off(at);
                    let mut out = String::new();
                    for p in &parts {
                        match p {
                            Value::Str(t) => out.push_str(&t.s),
                            other => {
                                let d = self.display(other, false)?;
                                out.push_str(&d)
                            }
                        }
                    }
                    self.push(s(out))
                }
                Op::Call(argc, ap) => {
                    self.call_line = proto.pos[pc - 1].0;
                    self.site = (Rc::as_ptr(proto), ap);
                    let ci = self.stack.len() - argc as usize - 1;
                    self.call_at(ci, argc as usize, None)?;
                }
                Op::CallEx(ni, ap) => {
                    self.call_line = proto.pos[pc - 1].0;
                    self.site = (Rc::as_ptr(proto), ap);
                    let names = &proto.names[ni as usize];
                    let kw = self.take_kwargs(names);
                    let ci = self.spread_positional()?;
                    let argc = self.stack.len() - ci - 1;
                    self.call_at(ci, argc, Some(kw))?;
                }
                Op::CallBuiltin(b, argc) => {
                    self.call_line = proto.pos[pc - 1].0;
                    let at = self.stack.len() - argc as usize;
                    let args = self.stack.split_off(at);
                    let r = crate::builtins::call_builtin(self, b, args, None)?;
                    self.push(r)
                }
                Op::CallMethod(n, argc, safe, ap) => {
                    self.call_line = proto.pos[pc - 1].0;
                    self.site = (Rc::as_ptr(proto), ap);
                    let ri = self.stack.len() - argc as usize - 1;
                    let name = proto.strings[n as usize].clone();
                    self.call_method(ri, &name, argc as usize, None, safe)?;
                }
                Op::CallMethodEx(n, ni, safe, ap) => {
                    self.call_line = proto.pos[pc - 1].0;
                    self.site = (Rc::as_ptr(proto), ap);
                    let names = &proto.names[ni as usize];
                    let kw = self.take_kwargs(names);
                    let ri = self.spread_positional()?;
                    let argc = self.stack.len() - ri - 1;
                    let name = proto.strings[n as usize].clone();
                    self.call_method(ri, &name, argc, Some(kw), safe)?;
                }
                Op::GetAttr(n) => {
                    let o = self.pop();
                    let v = self.get_attr(&o, &proto.strings[n as usize])?;
                    self.push(v)
                }
                Op::SetAttr(n) => {
                    let v = self.pop();
                    let o = self.pop();
                    self.set_attr(&o, &proto.strings[n as usize], v)?;
                }
                Op::SetAttrOp(n, b) => {
                    let v = self.pop();
                    let o = self.pop();
                    let name = &proto.strings[n as usize];
                    let old = self.get_attr(&o, name)?;
                    let new = self.binop(b, old, v)?;
                    self.set_attr(&o, name, new)?;
                }
                Op::GetIndex => {
                    let k = self.pop();
                    let c = self.pop();
                    let v = self.index(&c, &k)?;
                    self.push(v)
                }
                Op::SetIndex => {
                    let v = self.pop();
                    let k = self.pop();
                    let c = self.pop();
                    self.set_index(&c, k, v)?;
                }
                Op::SetIndexOp(b) => {
                    let v = self.pop();
                    let k = self.pop();
                    let c = self.pop();
                    let old = self.index(&c, &k)?;
                    let new = self.binop(b, old, v)?;
                    self.set_index(&c, k, new)?;
                }
                Op::GetStatic(n, via_parent) => {
                    let c = self.pop();
                    let this = self.pop();
                    let v = self.get_static(&c, &proto.strings[n as usize], &this, via_parent)?;
                    self.push(v)
                }
                Op::SetStatic(n) => {
                    let v = self.pop();
                    let c = self.pop();
                    self.set_static(&c, &proto.strings[n as usize], v)?;
                }
                Op::SetStaticOp(n, b) => {
                    let v = self.pop();
                    let c = self.pop();
                    let name = &proto.strings[n as usize];
                    let old = self.get_static(&c, name, &Value::Null, false)?;
                    let new = self.binop(b, old, v)?;
                    self.set_static(&c, name, new)?;
                }
                Op::New(id, argc, ap) => {
                    self.call_line = proto.pos[pc - 1].0;
                    self.site = (Rc::as_ptr(proto), ap);
                    let cls = self.classes[id as usize].clone();
                    let at = self.stack.len() - argc as usize;
                    let obj = self.instantiate(&cls, at, None)?;
                    self.push(obj)
                }
                Op::NewEx(id, ni, ap) => {
                    self.call_line = proto.pos[pc - 1].0;
                    self.site = (Rc::as_ptr(proto), ap);
                    let names = &proto.names[ni as usize];
                    let kw = self.take_kwargs(names);
                    let lst = self.pop();
                    let at = self.stack.len();
                    if let Value::List(l) = lst {
                        let items = l.borrow().clone();
                        self.stack.extend(items);
                    }
                    let cls = self.classes[id as usize].clone();
                    let obj = self.instantiate(&cls, at, Some(kw))?;
                    self.push(obj)
                }
                Op::Closure(i) => {
                    let p = proto.protos[i as usize].clone();
                    let mut up = Vec::with_capacity(p.upvals.len());
                    for src in &p.upvals {
                        match *src {
                            UpSrc::Local(sl) => {
                                let idx = base + sl as usize;
                                let c = match &self.stack[idx] {
                                    Value::Cell(c) => c.clone(),
                                    other => {
                                        let c = Rc::new(RefCell::new(other.clone()));
                                        self.stack[idx] = Value::Cell(c.clone());
                                        c
                                    }
                                };
                                up.push(c);
                            }
                            UpSrc::Up(u) => up.push(cl.up[u as usize].clone()),
                        }
                    }
                    self.push(Value::Func(Rc::new(Closure { proto: p, up })))
                }
                Op::Return => {
                    let v = self.pop();
                    if let Some(ret) = &proto.ret {
                        self.check_return(proto, ret, &v)?;
                    }
                    return Ok(v);
                }
                Op::ReturnEnd => {
                    if let Some(ret) = &proto.ret {
                        if !matches!(ret.k, TyK::Any | TyK::Void) {
                            return self.fail(
                                "TypeException",
                                format!("{}() must return {} but reached the end without 'return'", proto.display, ret.text),
                            );
                        }
                    }
                    return Ok(Value::Null);
                }
                Op::Throw => {
                    let v = self.pop();
                    match &v {
                        Value::Obj(o) if o.cls.is_subclass_of(&self.classes[0]) => return Err(Flow::Throw(o.clone())),
                        _ => {
                            return self.fail(
                                "TypeException",
                                format!("only Exception objects can be raised, got {}", type_name(&v)),
                            )
                        }
                    }
                }
                Op::Rethrow | Op::Resume => match self.pop() {
                    Value::Obj(o) => return Err(Flow::Throw(o)),
                    Value::Exit(c) => return Err(Flow::Exit(c)),
                    _ => return Err(Flow::Fatal("bad rethrow".into())),
                },
                Op::Try(c, f) => handlers.push(Handler { catch: c, fin: f, sp: self.stack.len() }),
                Op::PopTry => {
                    handlers.pop();
                }
                Op::IterInit(sl, pairs) => {
                    let v = self.pop();
                    let it = self.make_iter(v, pairs)?;
                    self.stack[base + sl as usize] = Value::Iter(Rc::new(RefCell::new(it)));
                }
                Op::IterNext(sl, t) => {
                    let next = match &self.stack[base + sl as usize] {
                        Value::Iter(it) => {
                            let mut it = it.borrow_mut();
                            match &mut *it {
                                IterState::Range { cur, stop, step, .. } => {
                                    if (*step > 0 && *cur < *stop) || (*step < 0 && *cur > *stop) {
                                        let v = *cur;
                                        *cur += *step;
                                        Some(Value::Int(v))
                                    } else {
                                        None
                                    }
                                }
                                IterState::Items { items, i } => {
                                    if *i < items.len() {
                                        *i += 1;
                                        Some(std::mem::replace(&mut items[*i - 1], Value::Null))
                                    } else {
                                        None
                                    }
                                }
                                IterState::Pairs { .. } => None,
                            }
                        }
                        _ => None,
                    };
                    match next {
                        Some(v) => self.push(v),
                        None => pc = t as usize,
                    }
                }
                Op::IterNext2(sl, t) => {
                    let next = match &self.stack[base + sl as usize] {
                        Value::Iter(it) => {
                            let mut it = it.borrow_mut();
                            match &mut *it {
                                IterState::Range { cur, stop, step, i } => {
                                    if (*step > 0 && *cur < *stop) || (*step < 0 && *cur > *stop) {
                                        let v = *cur;
                                        *cur += *step;
                                        *i += 1;
                                        Some((Value::Int(*i - 1), Value::Int(v)))
                                    } else {
                                        None
                                    }
                                }
                                IterState::Pairs { items, i } => {
                                    if *i < items.len() {
                                        *i += 1;
                                        Some(std::mem::replace(&mut items[*i - 1], (Value::Null, Value::Null)))
                                    } else {
                                        None
                                    }
                                }
                                IterState::Items { .. } => None,
                            }
                        }
                        _ => None,
                    };
                    match next {
                        Some((k, v)) => {
                            self.push(k);
                            self.push(v);
                        }
                        None => pc = t as usize,
                    }
                }
                Op::IterNextStore(sl, var, t) => {
                    let next = match &self.stack[base + sl as usize] {
                        Value::Iter(it) => {
                            let mut it = it.borrow_mut();
                            match &mut *it {
                                IterState::Range { cur, stop, step, .. } => {
                                    if (*step > 0 && *cur < *stop) || (*step < 0 && *cur > *stop) {
                                        let v = *cur;
                                        *cur += *step;
                                        Some(Value::Int(v))
                                    } else {
                                        None
                                    }
                                }
                                IterState::Items { items, i } => {
                                    if *i < items.len() {
                                        *i += 1;
                                        Some(std::mem::replace(&mut items[*i - 1], Value::Null))
                                    } else {
                                        None
                                    }
                                }
                                IterState::Pairs { .. } => None,
                            }
                        }
                        _ => None,
                    };
                    match next {
                        Some(v) => self.put(base + var as usize, v),
                        None => pc = t as usize,
                    }
                }
                Op::LocalOp(sl, b, ti, w) => {
                    let v = self.pop();
                    let i = base + sl as usize;
                    let fast = match (&self.stack[i], &v, b) {
                        (Value::Int(x), Value::Int(y), BinOp::Add) => x.checked_add(*y).map(Value::Int),
                        (Value::Int(x), Value::Int(y), BinOp::Sub) => x.checked_sub(*y).map(Value::Int),
                        (Value::Float(x), Value::Float(y), BinOp::Add) => Some(Value::Float(x + y)),
                        (Value::Float(x), Value::Float(y), BinOp::Sub) => Some(Value::Float(x - y)),
                        _ => None,
                    };
                    let r = match fast {
                        Some(r) => r,
                        None => {
                            let old = std::mem::replace(&mut self.stack[i], Value::Null);
                            let r = self.binop(b, old.clone(), v);
                            match r {
                                Ok(r) => {
                                    if ti != NONE {
                                        let ty = &proto.types[ti as usize];
                                        if !self.matches(&r, ty)? {
                                            self.stack[i] = old;
                                            self.check_assign(&r, ty, &proto.strings[w as usize])?;
                                        }
                                    }
                                    r
                                }
                                Err(e) => {
                                    self.stack[i] = old;
                                    return Err(e);
                                }
                            }
                        }
                    };
                    self.put(i, r);
                }
                Op::Error(i) => {
                    let (cls, msg) = &proto.errors[i as usize];
                    return self.fail(cls, msg.clone());
                }
                Op::Matches(ni) => {
                    let v = self.pop();
                    let Value::Obj(o) = &v else {
                        self.push(Value::Bool(false));
                        continue;
                    };
                    let mut hit = false;
                    for n in &proto.names[ni as usize] {
                        let name = n.trim_start_matches('\\');
                        let id = match self.class_by_name.get(name) {
                            Some(&id) => id,
                            None => match name.rsplit('\\').next().and_then(|sn| self.class_by_name.get(sn)) {
                                Some(&id) => id,
                                None => return self.fail("NameException", format!("'{}' is not defined", n)),
                            },
                        };
                        if o.cls.is_subclass_of(&self.classes[id]) {
                            hit = true;
                            break;
                        }
                    }
                    self.push(Value::Bool(hit))
                }
            }
        }
    }

    fn overflow<T>(&self) -> R<T> {
        self.fail("ValueException", "number too large: the native VM's int is 64-bit")
    }

    fn take_kwargs(&mut self, names: &[Rc<str>]) -> Vec<(Rc<str>, Value)> {
        let at = self.stack.len() - names.len();
        let vals = self.stack.split_off(at);
        names.iter().cloned().zip(vals).collect()
    }

    /// The top of the stack is a list of positional arguments: spread it onto
    /// the stack. Returns the index of the callee (or receiver) under it.
    fn spread_positional(&mut self) -> R<usize> {
        let lst = self.pop();
        let ci = self.stack.len() - 1;
        if let Value::List(l) = lst {
            let items = l.borrow().clone();
            self.stack.extend(items);
        }
        Ok(ci)
    }

    fn cond_error<T>(&self, v: &Value, k: CondKind) -> R<T> {
        let eg = match v {
            Value::Null => "'x != null'",
            Value::Int(_) | Value::Float(_) => "'x != 0'",
            Value::Str(_) => "'s != \"\"'",
            Value::List(_) | Value::Dict(_) | Value::Set(_) => "'items.len() > 0'",
            _ => "'x != null'",
        };
        self.fail(
            "TypeException",
            format!("{} must be bool, got {} — compare explicitly, e.g. {}", k.what(), type_name(v), eg),
        )
    }

    fn check_return(&self, proto: &Proto, ret: &Ty, v: &Value) -> R<()> {
        match ret.k {
            TyK::Any => Ok(()),
            TyK::Void => {
                if matches!(v, Value::Null) {
                    Ok(())
                } else {
                    self.fail("TypeException", format!("{}() is #[void] but returned {}", proto.display, type_name(v)))
                }
            }
            _ => {
                if self.matches(v, ret)? {
                    Ok(())
                } else {
                    self.check_assign(v, ret, &format!("return value of {}()", proto.display))
                }
            }
        }
    }

    // =====================================================================
    // types
    // =====================================================================

    pub fn matches(&self, v: &Value, t: &Ty) -> R<bool> {
        if let TyK::Any = t.k {
            return Ok(true);
        }
        if let Value::Null = v {
            return Ok(!t.non_nullable() || t.nullable);
        }
        Ok(match &t.k {
            TyK::Any => true,
            TyK::Int => matches!(v, Value::Int(_)),
            TyK::Float => matches!(v, Value::Float(_)),
            TyK::Str => matches!(v, Value::Str(_)),
            TyK::Bool => matches!(v, Value::Bool(_)),
            TyK::List(e) => match v {
                Value::List(l) => match e {
                    None => true,
                    Some(et) => {
                        for x in l.borrow().iter() {
                            if !self.matches(x, et)? {
                                return Ok(false);
                            }
                        }
                        true
                    }
                },
                _ => false,
            },
            TyK::Dict(kv) => match v {
                Value::Dict(d) => match kv {
                    None => true,
                    Some(kv) => {
                        for (k, x) in d.borrow().iter() {
                            if !self.matches(k, &kv.0)? || !self.matches(x, &kv.1)? {
                                return Ok(false);
                            }
                        }
                        true
                    }
                },
                _ => false,
            },
            TyK::Set(e) => match v {
                Value::Set(st) => match e {
                    None => true,
                    Some(et) => {
                        for (x, _) in st.borrow().iter() {
                            if !self.matches(x, et)? {
                                return Ok(false);
                            }
                        }
                        true
                    }
                },
                _ => false,
            },
            TyK::Range => matches!(v, Value::Range(_)),
            TyK::Karo => is_callable(v),
            TyK::Void | TyK::Null => false,
            TyK::Class(id) => match v {
                Value::Obj(o) => o.cls.is_subclass_of(&self.classes[*id]),
                _ => false,
            },
            TyK::Unknown(n) => return self.fail("NameException", format!("'{}' is not defined", n)),
        })
    }

    fn describe(&self, v: &Value) -> String {
        if let Value::List(l) = v {
            let l = l.borrow();
            if !l.is_empty() {
                let mut kinds: Vec<String> = Vec::new();
                for x in l.iter() {
                    let d = self.describe(x);
                    if !kinds.contains(&d) {
                        kinds.push(d);
                    }
                }
                return if kinds.len() == 1 { format!("list<{}>", kinds[0]) } else { "list<mixed>".into() };
            }
        }
        type_name(v)
    }

    pub fn check_assign(&self, v: &Value, t: &Ty, what: &str) -> R<()> {
        if self.matches(v, t)? {
            return Ok(());
        }
        if matches!(v, Value::Null) && t.non_nullable() {
            return self.fail(
                "TypeException",
                format!("{} is {} and cannot be null — make it ?{} to allow null", what, t.text, t.text),
            );
        }
        let got = self.describe(v);
        let prim = ["int", "float", "string", "bool"];
        let hint = if prim.contains(&&*t.name) && prim.contains(&got.as_str()) {
            format!(" — convert it with ~{}(...)", t.name)
        } else {
            String::new()
        };
        self.fail("TypeException", format!("{} expects {}, got {}{}", what, t.text, got, hint))
    }

    pub fn zero_value(&self, t: &Ty) -> Value {
        if t.nullable {
            return Value::Null;
        }
        match t.k {
            TyK::Int => Value::Int(0),
            TyK::Float => Value::Float(0.0),
            TyK::Str => s(""),
            TyK::Bool => Value::Bool(false),
            TyK::List(_) => list(Vec::new()),
            TyK::Dict(_) => dict(OMap::new(false)),
            TyK::Set(_) => set(OMap::new(true)),
            _ => Value::Null,
        }
    }

    pub fn key_of(&self, v: &Value, what: &str) -> R<HKey> {
        match hkey(v, what == "set item") {
            Some(k) => Ok(k),
            None => self.fail("TypeException", format!("{} cannot be used as a {}", type_name(v), what)),
        }
    }

    // =====================================================================
    // operators
    // =====================================================================

    pub fn binop(&self, o: BinOp, a: Value, b: Value) -> R<Value> {
        match (&a, &b) {
            (Value::Int(x), Value::Int(y)) => {
                let (x, y) = (*x, *y);
                let r = match o {
                    BinOp::Add => x.checked_add(y),
                    BinOp::Sub => x.checked_sub(y),
                    BinOp::Mul => x.checked_mul(y),
                    BinOp::Div => {
                        if y == 0 {
                            return self.fail("DivisionByZeroException", "division by zero");
                        }
                        x.checked_div(y)
                    }
                    BinOp::Mod => {
                        if y == 0 {
                            return self.fail("DivisionByZeroException", "modulo by zero");
                        }
                        Some(x.checked_rem(y).unwrap_or(0))
                    }
                    BinOp::Pow => {
                        if y < 0 {
                            return self.fail(
                                "TypeException",
                                "int ** negative int is not an int — use floats: ~float(x) ** ~float(y)",
                            );
                        }
                        if y > u32::MAX as i64 {
                            None
                        } else {
                            x.checked_pow(y as u32)
                        }
                    }
                };
                match r {
                    Some(n) => Ok(Value::Int(n)),
                    None => self.overflow(),
                }
            }
            (Value::Float(x), Value::Float(y)) => {
                let (x, y) = (*x, *y);
                Ok(Value::Float(match o {
                    BinOp::Add => x + y,
                    BinOp::Sub => x - y,
                    BinOp::Mul => x * y,
                    BinOp::Div => {
                        if y == 0.0 {
                            return self.fail("DivisionByZeroException", "division by zero");
                        }
                        x / y
                    }
                    BinOp::Mod => {
                        if y == 0.0 {
                            return self.fail("DivisionByZeroException", "modulo by zero");
                        }
                        x % y
                    }
                    BinOp::Pow => {
                        let r = x.powf(y);
                        if r.is_infinite() && x.is_finite() && y.is_finite() {
                            return self.fail("ValueException", "number too large");
                        }
                        r
                    }
                }))
            }
            (Value::Str(x), Value::Str(y)) if o == BinOp::Add => {
                let mut out = String::with_capacity(x.s.len() + y.s.len());
                out.push_str(&x.s);
                out.push_str(&y.s);
                Ok(Value::Str(Rc::new(BStr { chars: x.chars + y.chars, s: out })))
            }
            (Value::List(x), Value::List(y)) if o == BinOp::Add => {
                let mut out = x.borrow().clone();
                out.extend(y.borrow().iter().cloned());
                Ok(list(out))
            }
            _ => {
                let (na, nb) = (type_name(&a), type_name(&b));
                let hint = if (na == "int" && nb == "float") || (na == "float" && nb == "int") {
                    " — convert one side with ~float(...) or ~int(...)"
                } else if o == BinOp::Add && (na == "string" || nb == "string") {
                    " — use ~string(...) or interpolation: \"{x}\""
                } else {
                    ""
                };
                self.fail("TypeException", format!("cannot apply '{}' to {} and {}{}", o.text(), na, nb, hint))
            }
        }
    }

    pub fn compare(&self, o: CmpOp, a: &Value, b: &Value) -> R<bool> {
        match o {
            CmpOp::Eq => return Ok(values_equal(a, b)),
            CmpOp::Ne => return Ok(!values_equal(a, b)),
            CmpOp::In => return self.contains(b, a),
            CmpOp::NotIn => return self.contains(b, a).map(|r| !r),
            _ => {}
        }
        let ord = match (a, b) {
            (Value::Int(x), Value::Int(y)) => x.partial_cmp(y),
            (Value::Int(x), Value::Float(y)) => (*x as f64).partial_cmp(y),
            (Value::Float(x), Value::Int(y)) => x.partial_cmp(&(*y as f64)),
            (Value::Float(x), Value::Float(y)) => x.partial_cmp(y),
            (Value::Str(x), Value::Str(y)) => Some(x.s.cmp(&y.s)),
            _ => {
                return self.fail(
                    "TypeException",
                    format!("cannot compare {} {} {}", type_name(a), o.text(), type_name(b)),
                )
            }
        };
        use std::cmp::Ordering::*;
        Ok(match ord {
            None => false,
            Some(c) => match o {
                CmpOp::Lt => c == Less,
                CmpOp::Gt => c == Greater,
                CmpOp::Le => c != Greater,
                _ => c != Less,
            },
        })
    }

    pub fn contains(&self, container: &Value, item: &Value) -> R<bool> {
        match container {
            Value::Str(st) => match item {
                Value::Str(i) => Ok(st.s.contains(i.s.as_str())),
                _ => self.fail("TypeException", format!("'in' on a string needs a string, got {}", type_name(item))),
            },
            Value::List(l) => Ok(l.borrow().iter().any(|x| values_equal(item, x))),
            Value::Range(r) => Ok(match item {
                Value::Int(n) => range_has(r, *n),
                Value::Float(f) if f.fract() == 0.0 => range_has(r, *f as i64),
                Value::Bool(b) => range_has(r, *b as i64),
                _ => false,
            }),
            Value::Dict(d) => Ok(hkey(item, false).map(|k| d.borrow().contains(&k)).unwrap_or(false)),
            Value::Set(st) => Ok(hkey(item, true).map(|k| st.borrow().contains(&k)).unwrap_or(false)),
            _ => self.fail(
                "TypeException",
                format!("'in' needs a list, dict, set or string, got {}", type_name(container)),
            ),
        }
    }

    fn make_iter(&self, v: Value, pairs: bool) -> R<IterState> {
        Ok(match &v {
            Value::Range(r) => IterState::Range { cur: r.start, stop: r.stop, step: r.step, i: 0 },
            Value::List(l) => {
                let items = l.borrow().clone();
                if pairs {
                    IterState::Pairs { items: items.into_iter().enumerate().map(|(i, x)| (Value::Int(i as i64), x)).collect(), i: 0 }
                } else {
                    IterState::Items { items, i: 0 }
                }
            }
            Value::Dict(d) => {
                let d = d.borrow();
                if pairs {
                    IterState::Pairs { items: d.iter().map(|(k, x)| (k.clone(), x.clone())).collect(), i: 0 }
                } else {
                    IterState::Items { items: d.keys(), i: 0 }
                }
            }
            Value::Set(st) => {
                let items = st.borrow().keys();
                if pairs {
                    IterState::Pairs { items: items.into_iter().enumerate().map(|(i, x)| (Value::Int(i as i64), x)).collect(), i: 0 }
                } else {
                    IterState::Items { items, i: 0 }
                }
            }
            Value::Str(t) => {
                let chars: Vec<Value> = t.s.chars().map(|c| s(c.to_string())).collect();
                if pairs {
                    IterState::Pairs { items: chars.into_iter().enumerate().map(|(i, x)| (Value::Int(i as i64), x)).collect(), i: 0 }
                } else {
                    IterState::Items { items: chars, i: 0 }
                }
            }
            _ => return self.fail("TypeException", format!("cannot loop over {}", type_name(&v))),
        })
    }

    // =====================================================================
    // calls
    // =====================================================================

    /// The callee is at stack[ci] with argc arguments after it. Leaves the
    /// result in its place.
    pub fn call_at(&mut self, ci: usize, argc: usize, kw: Option<Vec<(Rc<str>, Value)>>) -> R<()> {
        let callee = self.stack[ci].clone();
        match &callee {
            Value::Func(cl) => {
                let p = &cl.proto;
                if p.owner.is_some() && !p.is_static {
                    return self.fail(
                        "TypeException",
                        format!("method '{}' needs an object; call it as obj.{}()", p.name, p.name),
                    );
                }
                self.invoke(cl, ci, argc, kw, false)
            }
            Value::Bound(b) => match &b.func {
                Value::Func(cl) => {
                    self.stack[ci] = b.this.clone();
                    self.invoke(cl, ci, argc, kw, true)
                }
                Value::Native(n) => {
                    let args = self.stack.split_off(ci + 1);
                    self.stack.truncate(ci);
                    if kw.as_ref().map(|k| !k.is_empty()).unwrap_or(false) {
                        return self.no_named(n);
                    }
                    let r = self.call_native(n, args, Some(b.this.clone()))?;
                    self.push(r);
                    Ok(())
                }
                _ => self.fail("TypeException", "this value is not callable"),
            },
            Value::Native(n) => {
                let args = self.stack.split_off(ci + 1);
                self.stack.truncate(ci);
                if kw.as_ref().map(|k| !k.is_empty()).unwrap_or(false) {
                    return self.no_named(n);
                }
                let r = self.call_native(n, args, None)?;
                self.push(r);
                Ok(())
            }
            Value::BMethod(bm) => {
                let args = self.stack.split_off(ci + 1);
                self.stack.truncate(ci);
                if kw.as_ref().map(|k| !k.is_empty()).unwrap_or(false) {
                    let tn = type_name(&bm.0);
                    return self.fail(
                        "TypeException",
                        format!("{}.{}() is built in and takes positional arguments only — named arguments work with functions written in Barq", tn, bm.1),
                    );
                }
                let r = crate::builtins::call_method(self, &bm.0, &bm.1, args)?;
                self.push(r);
                Ok(())
            }
            Value::Class(c) => self.fail("TypeException", format!("use 'new {}(...)' to create an object", c.name)),
            other => self.fail("TypeException", format!("{} is not callable", type_name(other))),
        }
    }

    fn no_named<T>(&self, n: &NativeFn) -> R<T> {
        let name = match n.owner {
            Some(o) if !n.name.contains("::") => format!("{}.{}", self.classes[o].name, n.name),
            _ => n.name.clone(),
        };
        self.fail(
            "TypeException",
            format!("{}() is built in and takes positional arguments only — named arguments work with functions written in Barq", name),
        )
    }

    pub fn call_native(&mut self, n: &NativeFn, args: Vec<Value>, this: Option<Value>) -> R<Value> {
        let k = args.len();
        if k < n.min || n.max.map(|m| k > m).unwrap_or(false) {
            let expected = match n.max {
                Some(m) if m == n.min => format!("{} argument{}", m, if m != 1 { "s" } else { "" }),
                None => format!("at least {} arguments", n.min),
                Some(m) => format!("{} to {} arguments", n.min, m),
            };
            return self.fail("TypeException", format!("{}() expects {}, got {}", n.name, expected, k));
        }
        crate::builtins::call_builtin(self, n.b, args, this)
    }

    /// Run a Barq function. Its arguments are on the stack after index ci;
    /// with_this: stack[ci] is `this`.
    fn invoke(&mut self, cl: &Rc<Closure>, ci: usize, argc: usize, kw: Option<Vec<(Rc<str>, Value)>>, with_this: bool) -> R<()> {
        let p = cl.proto.clone();
        if p.is_abstract {
            return self.fail("TypeException", format!("cannot call abstract method {}()", p.display));
        }
        let base = if p.has_this { ci } else { ci + 1 };
        if p.has_this && !with_this {
            self.stack[ci] = Value::Null;
        }
        let bound = kw.is_some() || !p.simple;
        if bound {
            let args = self.stack.split_off(ci + 1);
            let vals = self.bind_args(&p, args, kw.unwrap_or_default())?;
            self.stack.extend(vals);
        } else {
            if argc != p.params.len() {
                let n = p.params.len();
                return self.fail(
                    "TypeException",
                    format!("{}() expects {} argument{}, got {}", p.display, n, if n != 1 { "s" } else { "" }, argc),
                );
            }
            for (i, prm) in p.params.iter().enumerate() {
                if let Some(t) = &prm.ty {
                    let v = &self.stack[ci + 1 + i];
                    let ok = match (&t.k, v) {
                        (TyK::Any, _) | (TyK::Int, Value::Int(_)) | (TyK::Float, Value::Float(_)) | (TyK::Str, Value::Str(_)) | (TyK::Bool, Value::Bool(_)) => true,
                        _ => self.matches(v, t)?,
                    };
                    if !ok {
                        let v = v.clone();
                        let r = self.check_assign(&v, t, &format!("argument '{}' of {}()", prm.name, p.display));
                        if r.is_err() {
                            self.at_arg(Some(i));
                        }
                        r?;
                    }
                }
            }
        }
        if self.calls.len() >= MAX_CALL_DEPTH {
            return self.fail(
                "RuntimeException",
                format!("maximum call depth ({}) exceeded — infinite recursion?", MAX_CALL_DEPTH),
            );
        }
        if !p.has_this && !with_this {
            // a lambda or function: drop the callee so slot 0 is the first parameter
        }
        self.calls.push((p.display.clone(), self.call_line));
        let saved = self.ctx;
        self.ctx = p.ctx;
        let r = self.run_closure(cl, base);
        self.ctx = saved;
        self.calls.pop();
        let v = r?;
        self.stack.truncate(ci);
        self.push(v);
        Ok(())
    }

    /// Point the error being raised at argument k of the current call.
    fn at_arg(&mut self, k: Option<usize>) {
        let (p, idx) = self.site;
        if p.is_null() || idx == NONE {
            return;
        }
        if let Some(k) = k {
            // SAFETY: the calling function is running, so its Proto is alive.
            let ps = unsafe { &(*p).arg_pos };
            if let Some(pos) = ps.get(idx as usize).and_then(|v| v.get(k)) {
                self.err_at = Some(*pos);
            }
        }
    }

    fn bind_args(&mut self, p: &Rc<Proto>, args: Vec<Value>, kw: Vec<(Rc<str>, Value)>) -> R<Vec<Value>> {
        let params = &p.params;
        let name = &p.display;
        let n = params.len();
        let variadic = n > 0 && params[n - 1].variadic;
        let fixed = if variadic { n - 1 } else { n };
        let npos = args.len();
        let given = npos + kw.len();
        let arity = arity_text(
            n,
            params.iter().filter(|x| x.default.is_none() && !x.variadic).count(),
            variadic,
        );
        if npos > fixed && !variadic {
            return self.fail("TypeException", format!("{}() expects {}, got {}", name, arity, given));
        }
        let take = npos.min(fixed);
        let mut it = args.into_iter();
        let mut out: Vec<Value> = Vec::with_capacity(n);
        for _ in 0..take {
            out.push(it.next().unwrap());
        }
        while out.len() < fixed {
            out.push(Value::Undef);
        }
        let rest: Vec<Value> = it.collect();
        let has_kw = !kw.is_empty();
        // which written argument each parameter got its value from
        let mut src: Vec<Option<usize>> = (0..fixed).map(|i| if i < take { Some(i) } else { None }).collect();
        for (k, (key, value)) in kw.into_iter().enumerate() {
            let Some(i) = params.iter().position(|x| *x.name == *key) else {
                let names: Vec<&str> = params.iter().map(|x| &*x.name).collect();
                let names = if names.is_empty() { "none".to_string() } else { names.join(", ") };
                self.at_arg(Some(npos + k));
                return self.fail(
                    "TypeException",
                    format!("{}() has no parameter named '{}' (parameters: {})", name, key, names),
                );
            };
            if params[i].variadic {
                self.at_arg(Some(npos + k));
                return self.fail(
                    "TypeException",
                    format!("{}(): the variadic parameter '{}' cannot be passed by name", name, key),
                );
            }
            if !matches!(out[i], Value::Undef) {
                self.at_arg(Some(npos + k));
                return self.fail("TypeException", format!("{}() got two values for '{}'", name, key));
            }
            out[i] = value;
            src[i] = Some(npos + k);
        }
        for i in 0..fixed {
            let prm = &params[i];
            if matches!(out[i], Value::Undef) {
                let Some(d) = &prm.default else {
                    let msg = if has_kw {
                        format!("{}() is missing argument '{}'", name, prm.name)
                    } else {
                        format!("{}() expects {}, got {}", name, arity, given)
                    };
                    return self.fail("TypeException", msg);
                };
                let v = match d {
                    Default::Const(v) => v.clone(),
                    Default::Code(dp) => {
                        let saved = self.ctx;
                        self.ctx = None;
                        let r = self.run_expr(dp);
                        self.ctx = saved;
                        r?
                    }
                };
                if let Some(t) = &prm.ty {
                    if !self.matches(&v, t)? {
                        self.check_assign(&v, t, &format!("default of parameter '{}' of {}()", prm.name, name))?;
                    }
                }
                out[i] = v;
            } else if let Some(t) = &prm.ty {
                let v = out[i].clone();
                if !self.matches(&v, t)? {
                    self.at_arg(src[i]);
                    self.check_assign(&v, t, &format!("argument '{}' of {}()", prm.name, name))?;
                }
            }
        }
        if variadic {
            let et = params[n - 1].elem.clone();
            if let Some(et) = et {
                for (j, v) in rest.iter().enumerate() {
                    if !self.matches(v, &et)? {
                        self.at_arg(Some(fixed + j));
                        self.check_assign(v, &et, &format!("argument {} of {}()", fixed + j + 1, name))?;
                    }
                }
            }
            out.push(list(rest));
        }
        Ok(out)
    }

    /// Call any function value with arguments (used by built-ins like map).
    pub fn call_value(&mut self, f: &Value, args: Vec<Value>) -> R<Value> {
        let ci = self.stack.len();
        let argc = args.len();
        self.stack.push(f.clone());
        self.stack.extend(args);
        match self.call_at(ci, argc, None) {
            Ok(()) => Ok(self.stack.pop().unwrap_or(Value::Null)),
            Err(e) => {
                self.stack.truncate(ci);
                Err(e)
            }
        }
    }

    fn can_access(&self, vis: Vis, owner: usize) -> bool {
        match vis {
            Vis::Public => true,
            _ => match self.ctx {
                None => false,
                Some(c) => {
                    if vis == Vis::Private {
                        c == owner
                    } else {
                        self.classes[c].is_subclass_of(&self.classes[owner]) || self.classes[owner].is_subclass_of(&self.classes[c])
                    }
                }
            },
        }
    }

    /// recv.name(args): the receiver is at stack[ri], argc arguments after it.
    fn call_method(&mut self, ri: usize, name: &Rc<str>, argc: usize, kw: Option<Vec<(Rc<str>, Value)>>, safe: bool) -> R<()> {
        let recv = self.stack[ri].clone();
        match &recv {
            Value::Obj(o) => {
                if let Some(&fi) = o.cls.field_index.get(name) {
                    let f = &o.cls.fields[fi];
                    if f.vis != Vis::Public && !self.can_access(f.vis, f.owner) {
                        return self.fail(
                            "TypeException",
                            format!("cannot read {} property '{}' of {}", f.vis.name(), name, o.cls.name),
                        );
                    }
                    let v = o.fields.borrow()[fi].clone();
                    self.stack[ri] = v;
                    return self.call_at(ri, argc, kw);
                }
                if let Some(m) = o.cls.methods.get(name).cloned() {
                    if m.vis != Vis::Public && !self.can_access(m.vis, m.owner) {
                        return self.fail(
                            "TypeException",
                            format!("cannot call {} method '{}()' of {}", m.vis.name(), name, o.cls.name),
                        );
                    }
                    if m.is_static {
                        self.stack[ri] = m.func.clone();
                        return self.call_at(ri, argc, kw);
                    }
                    return match &m.func {
                        Value::Func(cl) => self.invoke(cl, ri, argc, kw, true),
                        Value::Native(n) => {
                            let args = self.stack.split_off(ri + 1);
                            self.stack.truncate(ri);
                            let r = self.call_native(n, args, Some(recv.clone()))?;
                            self.push(r);
                            Ok(())
                        }
                        _ => self.fail("TypeException", "bad method"),
                    };
                }
                if o.cls.has_get {
                    let v = self.get_attr(&recv, name)?;
                    self.stack[ri] = v;
                    return self.call_at(ri, argc, kw);
                }
                self.fail("NameException", format!("{} has no property or method '{}'", o.cls.name, name))
            }
            Value::Null => {
                if safe {
                    return self.fail("TypeException", "null is not callable");
                }
                self.fail(
                    "NullException",
                    format!("cannot read '{}' of null — check for null first or use '?.'", name),
                )
            }
            Value::Class(_) | Value::Module(_) => {
                let v = self.get_attr(&recv, name)?;
                self.stack[ri] = v;
                self.call_at(ri, argc, kw)
            }
            Value::Disk(_) => {
                if kw.as_ref().map(|k| !k.is_empty()).unwrap_or(false) {
                    return self.fail("TypeException", format!("Disk.{}() takes positional arguments only", name));
                }
                let args = self.stack.split_off(ri + 1);
                self.stack.truncate(ri);
                let r = crate::storage::disk_method(self, &recv, name, args)?;
                self.push(r);
                Ok(())
            }
            Value::Response(_) => {
                match name.as_ref() {
                    "header" => {
                        if kw.as_ref().map(|k| !k.is_empty()).unwrap_or(false) {
                            return self.fail("TypeException", "Response.header() takes positional arguments only");
                        }
                        let args = self.stack.split_off(ri + 1);
                        self.stack.truncate(ri);
                        let r = crate::storage::response_method(self, &recv, "header", args)?;
                        self.push(r);
                        Ok(())
                    }
                    _ => self.fail("NameException", format!("Response has no method '{}'", name)),
                }
            }
            _ => {
                let Some(id) = method_id(name) else {
                    return self.no_method(&recv, name);
                };
                if crate::builtins::method_arity(&recv, id).is_none() {
                    return self.no_method(&recv, name);
                }
                if kw.as_ref().map(|k| !k.is_empty()).unwrap_or(false) {
                    return self.fail(
                        "TypeException",
                        format!("{}.{}() is built in and takes positional arguments only — named arguments work with functions written in Barq", type_name(&recv), name),
                    );
                }
                let args = self.stack.split_off(ri + 1);
                self.stack.truncate(ri);
                let r = crate::builtins::call_method_id(self, &recv, id, name, args)?;
                self.push(r);
                Ok(())
            }
        }
    }

    fn no_method<T>(&self, recv: &Value, name: &str) -> R<T> {
        if let Value::Dict(_) = recv {
            return self.fail(
                "NameException",
                format!("dict has no method '{}' — read values with d[\"{}\"]", name, name),
            );
        }
        self.fail("NameException", format!("{} has no method '{}'", type_name(recv), name))
    }

    // =====================================================================
    // objects
    // =====================================================================

    fn new_object(&mut self, cls: &Rc<Class>) -> R<Value> {
        if cls.is_abstract {
            let what = match cls.kind {
                Kind::Interface => "interface",
                Kind::Trait => "trait",
                _ => "abstract class",
            };
            return self.fail("TypeException", format!("cannot create an instance of {} {}", what, cls.name));
        }
        self.bare(cls)
    }

    /// An object with its property defaults, before __construct runs.
    pub fn bare(&mut self, cls: &Rc<Class>) -> R<Value> {
        if cls.is_enum() {
            let first = cls
                .cases
                .borrow()
                .first()
                .map(|c| if let Value::Obj(o) = c { crate::builtins::plain_display(&o.field("name")) } else { String::new() })
                .unwrap_or_else(|| "Case".into());
            return self.fail(
                "TypeException",
                format!("cannot create an instance of enum {} — use a case such as {}::{}", cls.name, cls.name, first),
            );
        }
        let mut fields = Vec::with_capacity(cls.fields.len());
        for f in &cls.fields {
            let v = match &f.default {
                FieldDefault::Zero => match &f.ty {
                    Some(t) => self.zero_value(t),
                    None => Value::Null,
                },
                FieldDefault::Const(v) => v.clone(),
                FieldDefault::Code(p) => {
                    let saved = self.ctx;
                    self.ctx = None;
                    let r = self.run_expr(p);
                    self.ctx = saved;
                    r?
                }
            };
            if let Some(t) = &f.ty {
                if !matches!(f.default, FieldDefault::Zero) && !self.matches(&v, t)? {
                    let owner = self.classes[f.owner].name.clone();
                    self.check_assign(&v, t, &format!("default of property '{}.{}'", owner, f.name))?;
                }
            }
            fields.push(v);
        }
        Ok(Value::Obj(Rc::new(Obj::new(cls.clone(), fields))))
    }

    /// new Cls(args): the arguments are on the stack from index at.
    fn instantiate(&mut self, cls: &Rc<Class>, at: usize, kw: Option<Vec<(Rc<str>, Value)>>) -> R<Value> {
        let obj = match self.new_object(cls) {
            Ok(o) => o,
            Err(e) => {
                self.stack.truncate(at);
                return Err(e);
            }
        };
        let argc = self.stack.len() - at;
        match cls.methods.get("__construct").cloned() {
            Some(m) => {
                if m.vis != Vis::Public && !self.can_access(m.vis, m.owner) {
                    self.stack.truncate(at);
                    return self.fail("TypeException", format!("the constructor of {} is {}", cls.name, m.vis.name()));
                }
                match &m.func {
                    Value::Func(cl) => {
                        self.stack.insert(at, obj.clone());
                        self.invoke(cl, at, argc, kw, true)?;
                        self.stack.pop();
                    }
                    Value::Native(n) => {
                        let args = self.stack.split_off(at);
                        self.call_native(n, args, Some(obj.clone()))?;
                    }
                    _ => {}
                }
            }
            None => {
                let nkw = kw.as_ref().map(|k| k.len()).unwrap_or(0);
                if argc > 0 || nkw > 0 {
                    self.stack.truncate(at);
                    return self.fail(
                        "TypeException",
                        format!("{} has no __construct() but {} argument(s) were given", cls.name, argc),
                    );
                }
            }
        }
        Ok(obj)
    }

    pub fn get_attr(&mut self, obj: &Value, name: &str) -> R<Value> {
        match obj {
            Value::Obj(o) => {
                let cls = &o.cls;
                if let Some(&fi) = cls.field_index.get(name) {
                    let f = &cls.fields[fi];
                    if f.vis != Vis::Public && !self.can_access(f.vis, f.owner) {
                        return self.fail("TypeException", format!("cannot read {} property '{}' of {}", f.vis.name(), name, cls.name));
                    }
                    return Ok(o.fields.borrow()[fi].clone());
                }
                if let Some(m) = cls.methods.get(name) {
                    if m.vis != Vis::Public && !self.can_access(m.vis, m.owner) {
                        return self.fail("TypeException", format!("cannot call {} method '{}()' of {}", m.vis.name(), name, cls.name));
                    }
                    if m.is_static {
                        return Ok(m.func.clone());
                    }
                    return Ok(Value::Bound(Rc::new(Bound { this: obj.clone(), func: m.func.clone() })));
                }
                if cls.has_get {
                    let g = cls.methods["__get"].func.clone();
                    let b = Value::Bound(Rc::new(Bound { this: obj.clone(), func: g }));
                    return self.call_value(&b, vec![s(name)]);
                }
                self.fail("NameException", format!("{} has no property or method '{}'", cls.name, name))
            }
            Value::Class(_) => self.get_static(obj, name, &Value::Null, false),
            Value::Module(m) => match crate::compiler::module_member(*m, name) {
                Some(crate::compiler::ModMember::Value(v)) => Ok(v),
                Some(crate::compiler::ModMember::Func(b, lo, hi)) => {
                    Ok(crate::compiler::native_value(b, &format!("{}.{}", m.name(), name), lo, hi))
                }
                None => self.fail("NameException", format!("{} has no member '{}'", m.name(), name)),
            },
            Value::Disk(_) => {
                Ok(Value::BMethod(Rc::new((obj.clone(), name.into()))))
            }
            Value::Response(r) => match crate::storage::response_get_attr(r, name) {
                Some(v) => Ok(v),
                None => Ok(Value::BMethod(Rc::new((obj.clone(), name.into())))),
            },
            Value::Null => self.fail(
                "NullException",
                format!("cannot read '{}' of null — check for null first or use '?.'", name),
            ),
            _ => {
                if let Some(id) = method_id(name) {
                    if crate::builtins::method_arity(obj, id).is_some() {
                        return Ok(Value::BMethod(Rc::new((obj.clone(), name.into()))));
                    }
                }
                self.no_method(obj, name)
            }
        }
    }

    pub fn set_attr(&mut self, obj: &Value, name: &str, v: Value) -> R<()> {
        match obj {
            Value::Obj(o) => {
                let cls = o.cls.clone();
                if cls.is_enum() {
                    let shown = self.display(obj, false)?;
                    return self.fail("TypeException", format!("enum cases are read-only: cannot set '{}' on {}", name, shown));
                }
                let Some(&fi) = cls.field_index.get(name) else {
                    if cls.methods.contains_key(name) {
                        return self.fail("TypeException", format!("cannot assign to method '{}' of {}", name, cls.name));
                    }
                    if cls.has_set {
                        let f = cls.methods["__set"].func.clone();
                        let b = Value::Bound(Rc::new(Bound { this: obj.clone(), func: f }));
                        self.call_value(&b, vec![s(name), v])?;
                        return Ok(());
                    }
                    return self.fail(
                        "NameException",
                        format!("{} has no property '{}' — declare it in the class body, e.g. 'public any {}'", cls.name, name, name),
                    );
                };
                let f = &cls.fields[fi];
                if f.vis != Vis::Public && !self.can_access(f.vis, f.owner) {
                    return self.fail("TypeException", format!("cannot write {} property '{}' of {}", f.vis.name(), name, cls.name));
                }
                if let Some(t) = &f.ty {
                    let ok = match (&t.k, &v) {
                        (TyK::Any, _) | (TyK::Int, Value::Int(_)) | (TyK::Float, Value::Float(_)) | (TyK::Str, Value::Str(_)) | (TyK::Bool, Value::Bool(_)) => true,
                        _ => self.matches(&v, t)?,
                    };
                    if !ok {
                        self.check_assign(&v, t, &format!("property '{}.{}'", cls.name, name))?;
                    }
                }
                o.fields.borrow_mut()[fi] = v;
                Ok(())
            }
            Value::Class(_) => self.set_static(obj, name, v),
            Value::Null => self.fail("NullException", format!("cannot set '{}' on null", name)),
            other => self.fail("TypeException", format!("cannot set '{}' on {}", name, type_name(other))),
        }
    }

    pub fn get_static(&mut self, c: &Value, name: &str, this: &Value, _via_parent: bool) -> R<Value> {
        let cls = match c {
            Value::Class(cls) => cls.clone(),
            Value::Module(_) => return self.get_attr(c, name),
            other => return self.fail("TypeException", format!("'::' needs a class on the left, got {}", type_name(other))),
        };
        if let Some(sl) = cls.statics.get(name) {
            if sl.vis != Vis::Public && !self.can_access(sl.vis, sl.owner) {
                return self.fail("TypeException", format!("cannot read {} static '{}' of {}", sl.vis.name(), name, cls.name));
            }
            return Ok(sl.value.borrow().clone());
        }
        if let Some(m) = cls.methods.get(name) {
            if m.vis != Vis::Public && !self.can_access(m.vis, m.owner) {
                return self.fail("TypeException", format!("cannot call {} method '{}()' of {}", m.vis.name(), name, cls.name));
            }
            if m.is_static {
                return Ok(m.func.clone());
            }
            if let Value::Obj(t) = this {
                if self.ctx.is_some() && t.cls.is_subclass_of(&cls) {
                    return Ok(Value::Bound(Rc::new(Bound { this: this.clone(), func: m.func.clone() })));
                }
            }
            return self.fail("TypeException", format!("{}.{}() is not static; call it on an object", cls.name, name));
        }
        if cls.field_index.contains_key(name) {
            return self.fail(
                "TypeException",
                format!("'{}' is an instance property of {}; read it from an object", name, cls.name),
            );
        }
        self.fail("NameException", format!("{} has no static member '{}'", cls.name, name))
    }

    pub fn set_static(&mut self, c: &Value, name: &str, v: Value) -> R<()> {
        let Value::Class(cls) = c else {
            return self.fail("TypeException", format!("'::' needs a class on the left, got {}", type_name(c)));
        };
        let Some(sl) = cls.statics.get(name).cloned() else {
            return self.fail("NameException", format!("{} has no static property '{}'", cls.name, name));
        };
        if sl.is_const {
            return self.fail("TypeException", format!("cannot reassign constant {}::{}", cls.name, name));
        }
        if sl.vis != Vis::Public && !self.can_access(sl.vis, sl.owner) {
            return self.fail("TypeException", format!("cannot write {} static '{}' of {}", sl.vis.name(), name, cls.name));
        }
        if let Some(t) = &sl.ty {
            self.check_assign(&v, t, &format!("static property '{}::{}'", cls.name, name))?;
        }
        *sl.value.borrow_mut() = v;
        Ok(())
    }

    pub fn index(&mut self, c: &Value, k: &Value) -> R<Value> {
        match c {
            Value::List(_) | Value::Str(_) | Value::Range(_) => {
                let Value::Int(i) = k else {
                    return self.fail("TypeException", format!("{} index must be int, got {}", type_name(c), type_name(k)));
                };
                let n = match c {
                    Value::List(l) => l.borrow().len() as i64,
                    Value::Str(t) => t.chars as i64,
                    Value::Range(r) => r.len(),
                    _ => 0,
                };
                if !(-n <= *i && *i < n) {
                    return self.fail("IndexException", format!("index {} is out of range (length {})", i, n));
                }
                let i = if *i < 0 { *i + n } else { *i } as usize;
                Ok(match c {
                    Value::List(l) => l.borrow()[i].clone(),
                    Value::Str(t) => {
                        if t.is_ascii() {
                            s(t.s[i..i + 1].to_string())
                        } else {
                            s(t.s.chars().nth(i).unwrap().to_string())
                        }
                    }
                    Value::Range(r) => Value::Int(r.get(i as i64)),
                    _ => Value::Null,
                })
            }
            Value::Dict(d) => {
                let Some(hk) = hkey(k, false) else {
                    return self.fail("TypeException", format!("{} cannot be a dict key", type_name(k)));
                };
                if let Some(v) = d.borrow().get(&hk) {
                    return Ok(v.clone());
                }
                let shown = self.display(k, true)?;
                self.fail(
                    "KeyException",
                    format!("key {} not found — use d.get(key, default) for optional keys", shown),
                )
            }
            Value::Null => self.fail("NullException", "cannot index null"),
            _ => self.fail("TypeException", format!("{} cannot be indexed with []", type_name(c))),
        }
    }

    pub fn set_index(&mut self, c: &Value, k: Value, v: Value) -> R<()> {
        match c {
            Value::List(l) => {
                let Value::Int(i) = k else {
                    return self.fail("TypeException", format!("list index must be int, got {}", type_name(&k)));
                };
                let n = l.borrow().len() as i64;
                if !(-n <= i && i < n) {
                    return self.fail(
                        "IndexException",
                        format!("index {} is out of range (length {}) — use push() to add items", i, n),
                    );
                }
                let i = if i < 0 { i + n } else { i } as usize;
                l.borrow_mut()[i] = v;
                Ok(())
            }
            Value::Dict(d) => {
                let hk = self.key_of(&k, "dict key")?;
                d.borrow_mut().insert(hk, k, v);
                Ok(())
            }
            Value::Str(_) => self.fail("TypeException", "strings cannot be changed in place"),
            _ => self.fail("TypeException", format!("{} does not support [] assignment", type_name(c))),
        }
    }

    // =====================================================================
    // display
    // =====================================================================

    pub fn display(&mut self, v: &Value, quote: bool) -> R<String> {
        let mut out = String::new();
        self.display_into(v, quote, &mut out)?;
        Ok(out)
    }

    pub fn display_into(&mut self, v: &Value, quote: bool, out: &mut String) -> R<()> {
        match v {
            Value::Str(t) => {
                if quote {
                    crate::builtins::quote_into(&t.s, out);
                } else {
                    out.push_str(&t.s);
                }
            }
            Value::List(l) => {
                out.push_str("#[");
                let items = l.borrow().clone();
                for (i, x) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    self.display_into(x, true, out)?;
                }
                out.push(']');
            }
            Value::Dict(d) => {
                out.push_str("#{");
                let items: Vec<(Value, Value)> = d.borrow().iter().cloned().collect();
                for (i, (k, x)) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    self.display_into(k, true, out)?;
                    out.push_str(": ");
                    self.display_into(x, true, out)?;
                }
                out.push('}');
            }
            Value::Set(st) => {
                out.push_str("#(");
                let items = st.borrow().keys();
                for (i, x) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    self.display_into(x, true, out)?;
                }
                out.push(')');
            }
            Value::Obj(o) => {
                if o.cls.has_to_string {
                    let f = o.cls.methods["__toString"].func.clone();
                    let b = Value::Bound(Rc::new(Bound { this: v.clone(), func: f }));
                    let r = self.call_value(&b, Vec::new())?;
                    match r {
                        Value::Str(t) => out.push_str(&t.s),
                        other => {
                            return self.fail(
                                "TypeException",
                                format!("{}.__toString() must return string, got {}", o.cls.name, type_name(&other)),
                            )
                        }
                    }
                } else {
                    out.push_str(&crate::builtins::plain_display(v));
                }
            }
            _ => out.push_str(&crate::builtins::plain_display(v)),
        }
        Ok(())
    }
}

#[inline(always)]
pub fn is_plain(v: &Value) -> bool {
    matches!(v, Value::Null | Value::Bool(_) | Value::Int(_) | Value::Float(_) | Value::Undef | Value::Exit(_))
}

#[inline(always)]
pub fn clone_fast(v: &Value) -> Value {
    match v {
        Value::Int(n) => Value::Int(*n),
        Value::Float(f) => Value::Float(*f),
        Value::Bool(b) => Value::Bool(*b),
        Value::Null => Value::Null,
        other => other.clone(),
    }
}

fn range_has(r: &RangeV, n: i64) -> bool {
    if r.step > 0 {
        n >= r.start && n < r.stop && (n - r.start) % r.step == 0
    } else {
        n <= r.start && n > r.stop && (r.start - n) % (-r.step) == 0
    }
}

/// interpreter.arity_text: "2 arguments", "1 to 3 arguments", "at least 1 argument"
pub fn arity_text(n: usize, required: usize, variadic: bool) -> String {
    let plural = |k: usize| if k == 1 { "" } else { "s" };
    if variadic {
        return format!("at least {} argument{}", required, plural(required));
    }
    if required == n {
        format!("{} argument{}", n, plural(n))
    } else {
        format!("{} to {} arguments", required, n)
    }
}
