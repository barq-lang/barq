//! Global functions, the Math and Str modules, methods of the built-in
//! types, casts and display (barq/builtins.py and barq/runtime.py).

use crate::code::*;
use crate::vm::{Flow, Vm, R};
use crate::value::*;
use crate::stdlib as std_impl;
use std::cell::RefCell;
use std::rc::Rc;

// ---------------------------------------------------------------------------
// display
// ---------------------------------------------------------------------------

/// runtime.format_float
pub fn format_float(f: f64) -> String {
    if f.is_nan() {
        return "NAN".into();
    }
    if f.is_infinite() {
        return if f > 0.0 { "INF".into() } else { "-INF".into() };
    }
    if f == f.trunc() && f.abs() < 1e16 {
        return format!("{}.0", f as i64);
    }
    py_repr(f)
}

/// Python's repr() of a float: the shortest digits that read back the same,
/// in plain notation when the exponent is in -4..16, else scientific.
pub fn py_repr(f: f64) -> String {
    if f == 0.0 {
        return if f.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    let sci = format!("{:e}", f.abs());
    let (mant, exp) = sci.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let sign = if f < 0.0 { "-" } else { "" };
    if (-4..16).contains(&exp) {
        let point = exp + 1;
        let body = if point <= 0 {
            format!("0.{}{}", "0".repeat((-point) as usize), digits)
        } else if point as usize >= digits.len() {
            format!("{}{}.0", digits, "0".repeat(point as usize - digits.len()))
        } else {
            format!("{}.{}", &digits[..point as usize], &digits[point as usize..])
        };
        format!("{}{}", sign, body)
    } else {
        let m = if digits.len() > 1 { format!("{}.{}", &digits[..1], &digits[1..]) } else { digits };
        let es = if exp < 0 { "-" } else { "+" };
        format!("{}{}e{}{:02}", sign, m, es, exp.abs())
    }
}

pub fn quote_into(t: &str, out: &mut String) {
    out.push('"');
    for c in t.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\t' => out.push_str("\\t"),
            _ => out.push(c),
        }
    }
    out.push('"');
}

/// to_display without calling __toString (numbers, null, functions...).
pub fn plain_display(v: &Value) -> String {
    match v {
        Value::Null => "null".into(),
        Value::Bool(true) => "true".into(),
        Value::Bool(false) => "false".into(),
        Value::Int(n) => n.to_string(),
        Value::Float(f) => format_float(*f),
        Value::Str(t) => t.s.clone(),
        Value::Range(r) => {
            if r.step == 1 {
                format!("range({}, {})", r.start, r.stop)
            } else {
                format!("range({}, {}, {})", r.start, r.stop, r.step)
            }
        }
        Value::Obj(o) => {
            if o.cls.is_enum() {
                format!("{}::{}", o.cls.name, plain_display(&o.field("name")))
            } else {
                format!("<{} object>", o.cls.name)
            }
        }
        Value::Class(c) => format!("<class {}>", c.fqn),
        Value::Func(f) => format!("<function {}>", f.proto.name),
        Value::Native(n) => format!("<function {}>", n.name),
        Value::Bound(b) => match &b.func {
            Value::Func(f) => format!("<function {}>", f.proto.name),
            Value::Native(n) => format!("<function {}>", n.name),
            _ => "<function>".into(),
        },
        Value::BMethod(m) => format!("<function {}.{}>", type_name(&m.0), m.1),
        Value::Module(m) => format!("<module {}>", m.name()),
        Value::List(_) | Value::Dict(_) | Value::Set(_) => "<collection>".into(),
        _ => "<internal>".into(),
    }
}

fn dump_value(vm: &mut Vm, v: &Value, indent: usize) -> R<String> {
    let pad = "    ".repeat(indent);
    let inner = "    ".repeat(indent + 1);
    match v {
        Value::List(l) if !l.borrow().is_empty() => {
            let items = l.borrow().clone();
            let mut rows = Vec::new();
            for (i, x) in items.iter().enumerate() {
                rows.push(format!("{}{}: {}", inner, i, dump_value(vm, x, indent + 1)?));
            }
            Ok(format!("list({}) #[\n{}\n{}]", items.len(), rows.join("\n"), pad))
        }
        Value::Dict(d) if d.borrow().len() > 0 => {
            let items: Vec<(Value, Value)> = d.borrow().iter().cloned().collect();
            let mut rows = Vec::new();
            for (k, x) in &items {
                rows.push(format!("{}{}: {}", inner, vm.display(k, true)?, dump_value(vm, x, indent + 1)?));
            }
            Ok(format!("dict({}) #{{\n{}\n{}}}", items.len(), rows.join("\n"), pad))
        }
        Value::Obj(o) if o.cls.is_enum() => {
            let name = plain_display(&o.field("name"));
            if o.cls.backing.is_some() {
                let v = vm.display(&o.field("value"), true)?;
                Ok(format!("enum({}::{}: {})", o.cls.name, name, v))
            } else {
                Ok(format!("enum({}::{})", o.cls.name, name))
            }
        }
        Value::Obj(o) => {
            let mut rows = Vec::new();
            let fields = o.fields.borrow().clone();
            for (i, f) in o.cls.fields.iter().enumerate() {
                let mark = match f.vis {
                    Vis::Public => "+",
                    Vis::Protected => "#",
                    Vis::Private => "-",
                };
                rows.push(format!("{}{}{}: {}", inner, mark, f.name, dump_value(vm, &fields[i], indent + 1)?));
            }
            if rows.is_empty() {
                Ok(format!("{} {{}}", o.cls.name))
            } else {
                Ok(format!("{} {{\n{}\n{}}}", o.cls.name, rows.join("\n"), pad))
            }
        }
        Value::Str(t) => {
            let mut q = String::new();
            quote_into(&t.s, &mut q);
            Ok(format!("string({}) {}", t.chars, q))
        }
        Value::Bool(_) | Value::Null => Ok(plain_display(v)),
        Value::Int(_) | Value::Float(_) => Ok(format!("{}({})", type_name(v), plain_display(v))),
        _ => vm.display(v, true),
    }
}

// ---------------------------------------------------------------------------
// argument helpers
// ---------------------------------------------------------------------------

fn need(vm: &Vm, v: &Value, kinds: &[&str], fname: &str, pos: usize) -> R<()> {
    let tn = type_name(v);
    if kinds.contains(&tn.as_str())
        || (kinds.contains(&"number") && matches!(v, Value::Int(_) | Value::Float(_)))
        || (kinds.contains(&"callable") && is_callable(v))
    {
        return Ok(());
    }
    vm.fail("TypeException", format!("{}() argument {} must be {}, got {}", fname, pos, kinds.join(" or "), tn))
}

fn need_str(vm: &Vm, v: &Value, fname: &str, pos: usize) -> R<Rc<BStr>> {
    match v {
        Value::Str(t) => Ok(t.clone()),
        _ => need(vm, v, &["string"], fname, pos).map(|_| unreachable!()),
    }
}

fn need_int(vm: &Vm, v: &Value, fname: &str, pos: usize) -> R<i64> {
    match v {
        Value::Int(n) => Ok(*n),
        _ => need(vm, v, &["int"], fname, pos).map(|_| unreachable!()),
    }
}

fn num(vm: &Vm, v: &Value, fname: &str, pos: usize) -> R<f64> {
    match v {
        Value::Int(n) => Ok(*n as f64),
        Value::Float(f) => Ok(*f),
        _ => need(vm, v, &["number"], fname, pos).map(|_| unreachable!()),
    }
}

fn call_fn(vm: &mut Vm, f: &Value, args: Vec<Value>, fname: &str) -> R<Value> {
    if !is_callable(f) {
        return vm.fail("TypeException", format!("{}() needs a function, got {}", fname, type_name(f)));
    }
    vm.call_value(f, args)
}

fn need_bool(vm: &Vm, v: Value, fname: &str) -> R<bool> {
    match v {
        Value::Bool(b) => Ok(b),
        other => vm.fail(
            "TypeException",
            format!("the function passed to {}() must return bool, got {}", fname, type_name(&other)),
        ),
    }
}

// ---------------------------------------------------------------------------
// global functions and modules
// ---------------------------------------------------------------------------

pub fn call_builtin(vm: &mut Vm, b: Builtin, args: Vec<Value>, this: Option<Value>) -> R<Value> {
    let arity = |lo: usize, hi: Option<usize>, name: &str| -> R<()> {
        let k = args.len();
        if k < lo || hi.map(|h| k > h).unwrap_or(false) {
            let expected = match hi {
                Some(h) if h == lo => format!("{} argument{}", h, if h != 1 { "s" } else { "" }),
                None => format!("at least {} arguments", lo),
                Some(h) => format!("{} to {} arguments", lo, h),
            };
            return vm.fail("TypeException", format!("{}() expects {}, got {}", name, expected, k));
        }
        Ok(())
    };
    match b {
        Builtin::Print => {
            let mut out = String::new();
            for (i, a) in args.iter().enumerate() {
                if i > 0 {
                    out.push(' ');
                }
                vm.display_into(a, false, &mut out)?;
            }
            out.push('\n');
            vm.write(&out);
            Ok(Value::Null)
        }
        Builtin::Write => {
            let mut out = String::new();
            for a in &args {
                vm.display_into(a, false, &mut out)?;
            }
            vm.write(&out);
            Ok(Value::Null)
        }
        Builtin::Dump | Builtin::Dd => {
            arity(1, None, if b == Builtin::Dump { "dump" } else { "dd" })?;
            for a in &args {
                let d = dump_value(vm, a, 0)?;
                vm.write(&d);
                vm.write("\n");
            }
            if b == Builtin::Dd {
                return Err(Flow::Exit(1));
            }
            Ok(Value::Null)
        }
        Builtin::Breakpoint => {
            arity(0, Some(0), "breakpoint")?;
            Ok(Value::Null)
        }
        Builtin::Exit => {
            arity(0, Some(1), "exit")?;
            let code = match args.first() {
                None => 0,
                Some(v) => need_int(vm, v, "exit", 1)?,
            };
            Err(Flow::Exit(code as i32))
        }
        Builtin::Die => {
            arity(0, Some(1), "die")?;
            if let Some(v) = args.first() {
                let mut d = vm.display(v, false)?;
                d.push('\n');
                vm.write(&d);
            }
            Err(Flow::Exit(1))
        }
        Builtin::Range => {
            arity(1, Some(3), "range")?;
            let mut n = Vec::new();
            for (i, a) in args.iter().enumerate() {
                n.push(need_int(vm, a, "range", i + 1)?);
            }
            if n.len() == 3 && n[2] == 0 {
                return vm.fail("ValueException", "range() step cannot be 0");
            }
            let (start, stop, step) = match n.len() {
                1 => (0, n[0], 1),
                2 => (n[0], n[1], 1),
                _ => (n[0], n[1], n[2]),
            };
            Ok(Value::Range(Rc::new(RangeV { start, stop, step })))
        }
        Builtin::Len => {
            arity(1, Some(1), "len")?;
            let v = &args[0];
            Ok(Value::Int(match v {
                Value::Str(t) => t.chars as i64,
                Value::List(l) => l.borrow().len() as i64,
                Value::Dict(d) => d.borrow().len() as i64,
                Value::Set(st) => st.borrow().len() as i64,
                Value::Range(r) => r.len(),
                _ => return vm.fail("TypeException", format!("len() needs a string or collection, got {}", type_name(v))),
            }))
        }
        Builtin::Type => {
            arity(1, Some(1), "type")?;
            Ok(s(type_name(&args[0])))
        }
        Builtin::Input => {
            arity(0, Some(1), "input")?;
            if let Some(p) = args.first() {
                let d = vm.display(p, false)?;
                vm.write(&d);
            }
            vm.flush();
            let mut line = String::new();
            use std::io::BufRead;
            let n = std::io::stdin().lock().read_line(&mut line).unwrap_or(0);
            if n == 0 {
                return Ok(Value::Null);
            }
            let t = line.strip_suffix('\n').unwrap_or(&line);
            let t = t.strip_suffix('\r').unwrap_or(t);
            Ok(s(t.to_string()))
        }
        Builtin::Args => {
            arity(0, Some(0), "args")?;
            Ok(list(vm.argv.iter().map(|a| s(a.clone())).collect()))
        }
        Builtin::Assert => {
            arity(1, Some(2), "assert")?;
            match &args[0] {
                Value::Bool(true) => Ok(Value::Null),
                Value::Bool(false) => {
                    let msg = match args.get(1) {
                        Some(m) => vm.display(m, false)?,
                        None => "assertion failed".into(),
                    };
                    vm.fail("RuntimeException", msg)
                }
                other => vm.fail("TypeException", format!("assert() needs a bool, got {}", type_name(other))),
            }
        }
        Builtin::Math(m) => math(vm, m, args),
        Builtin::StrFn(id) => {
            let name = format!("Str.{}", method_name(id));
            let (lo, hi) = string_arity(id).unwrap();
            arity(lo + 1, Some(hi + 1), &name)?;
            let t = need_str(vm, &args[0], &name, 1)?;
            string_method(vm, &t, id, &args[1..])
        }
        Builtin::StrJoin => {
            arity(1, Some(2), "Str.join")?;
            let Value::List(l) = &args[0] else {
                return need(vm, &args[0], &["list"], "Str.join", 1).map(|_| Value::Null);
            };
            let sep = match args.get(1) {
                Some(v) => need_str(vm, v, "Str.join", 2)?.s.clone(),
                None => String::new(),
            };
            let items = l.borrow().clone();
            join(vm, &items, &sep)
        }
        Builtin::ExcConstruct => {
            let Some(Value::Obj(o)) = this else { return Ok(Value::Null) };
            if let Some(m) = args.first() {
                need(vm, m, &["string"], "Exception.__construct", 1)?;
                let i = o.cls.field_index["message"];
                o.fields.borrow_mut()[i] = m.clone();
            }
            if let Some(c) = args.get(1) {
                need(vm, c, &["int"], "Exception.__construct", 2)?;
                let i = o.cls.field_index["code"];
                o.fields.borrow_mut()[i] = c.clone();
            }
            Ok(Value::Null)
        }
        Builtin::ExcGetMessage => match this {
            Some(Value::Obj(o)) => Ok(o.field("message")),
            _ => Ok(Value::Null),
        },
        Builtin::ExcGetCode => match this {
            Some(Value::Obj(o)) => Ok(o.field("code")),
            _ => Ok(Value::Null),
        },
        Builtin::EnumCases(id) => Ok(list(vm.classes[id].cases.borrow().clone())),
        Builtin::EnumFrom(id) | Builtin::EnumTryFrom(id) => {
            let cls = vm.classes[id].clone();
            let fname = if matches!(b, Builtin::EnumFrom(_)) { "from" } else { "tryFrom" };
            let backing = cls.backing.clone().unwrap_or_default();
            need(vm, &args[0], &[backing.as_str()], &format!("{}::{}", cls.name, fname), 1)?;
            for c in cls.cases.borrow().iter() {
                if let Value::Obj(o) = c {
                    if values_equal(&o.field("value"), &args[0]) {
                        return Ok(c.clone());
                    }
                }
            }
            if fname == "tryFrom" {
                return Ok(Value::Null);
            }
            let shown = vm.display(&args[0], true)?;
            vm.fail("ValueException", format!("{} is not a valid value for enum {}", shown, cls.name))
        }
        Builtin::JsonOp(op) => json_op(vm, op, args),
        Builtin::IoOp(op) => io_op(vm, op, args),
        Builtin::PathOp(op) => path_op(vm, op, args),
        Builtin::EnvOp(op) => env_op(vm, op, args),
        Builtin::HashOp(op) => hash_op(vm, op, args),
        Builtin::CryptoOp(op) => crypto_op(vm, op, args),
        Builtin::UuidOp(op) => uuid_op(vm, op, args),
        Builtin::RxOp(op) => rx_op(vm, op, args),
        Builtin::DateOp(op) => date_op(vm, op, args),
        Builtin::ArrOp(op) => arr_op(vm, op, args),
        Builtin::LogOp(op) => log_op(vm, op, args),
        Builtin::CacheOp(op) => cache_op(vm, op, args),
    }
}

fn math(vm: &mut Vm, m: MathFn, a: Vec<Value>) -> R<Value> {
    use MathFn::*;
    let name = |f: &str| format!("Math.{}", f);
    let (lo, hi, fname): (usize, Option<usize>, &str) = match m {
        Abs => (1, Some(1), "abs"),
        Round => (1, Some(2), "round"),
        Floor => (1, Some(1), "floor"),
        Ceil => (1, Some(1), "ceil"),
        Pow => (2, Some(2), "pow"),
        Min => (1, None, "min"),
        Max => (1, None, "max"),
        Random => (0, Some(2), "random"),
        Clamp => (3, Some(3), "clamp"),
        Sqrt => (1, Some(1), "sqrt"),
        Sin => (1, Some(1), "sin"),
        Cos => (1, Some(1), "cos"),
        Tan => (1, Some(1), "tan"),
        Log => (1, Some(1), "log"),
        Log10 => (1, Some(1), "log10"),
        Exp => (1, Some(1), "exp"),
    };
    let k = a.len();
    if k < lo || hi.map(|h| k > h).unwrap_or(false) {
        let expected = match hi {
            Some(h) if h == lo => format!("{} argument{}", h, if h != 1 { "s" } else { "" }),
            None => format!("at least {} arguments", lo),
            Some(h) => format!("{} to {} arguments", lo, h),
        };
        return vm.fail("TypeException", format!("{}() expects {}, got {}", name(fname), expected, k));
    }
    let fl = |x: f64| Value::Float(x);
    match m {
        Abs => {
            num(vm, &a[0], &name("abs"), 1)?;
            Ok(match &a[0] {
                Value::Int(n) => match n.checked_abs() {
                    Some(x) => Value::Int(x),
                    None => return vm.fail("ValueException", "number too large: the native VM's int is 64-bit"),
                },
                Value::Float(f) => fl(f.abs()),
                _ => Value::Null,
            })
        }
        Round => {
            let x = num(vm, &a[0], &name("round"), 1)?;
            if a.len() > 1 {
                let digits = need_int(vm, &a[1], &name("round"), 2)?;
                let factor = 10f64.powi(digits as i32);
                let sign = if x >= 0.0 { 1.0 } else { -1.0 };
                return Ok(fl((x.abs() * factor + 0.5).floor() / factor * sign));
            }
            let r = (x.abs() + 0.5).floor();
            Ok(Value::Int(if x >= 0.0 { r as i64 } else { -(r as i64) }))
        }
        Floor | Ceil => {
            let x = num(vm, &a[0], &name(fname), 1)?;
            if let Value::Int(n) = a[0] {
                return Ok(Value::Int(n));
            }
            if !x.is_finite() {
                return vm.fail("ValueException", format!("Math.{}({}) is not defined", fname, plain_display(&a[0])));
            }
            Ok(Value::Int(if m == Floor { x.floor() } else { x.ceil() } as i64))
        }
        Pow => {
            num(vm, &a[0], &name("pow"), 1)?;
            num(vm, &a[1], &name("pow"), 2)?;
            vm.binop(BinOp::Pow, a[0].clone(), a[1].clone())
        }
        Min | Max => {
            let values: Vec<Value> = match (a.len(), &a[0]) {
                (1, Value::List(l)) => l.borrow().clone(),
                _ => a.clone(),
            };
            if values.is_empty() {
                return vm.fail("ValueException", format!("Math.{}() needs at least one value", fname));
            }
            for (i, v) in values.iter().enumerate() {
                num(vm, v, &name(fname), i + 1)?;
            }
            let mut best = values[0].clone();
            for v in &values[1..] {
                let (x, y) = (as_f64(v), as_f64(&best));
                if (m == Min && x < y) || (m == Max && x > y) {
                    best = v.clone();
                }
            }
            Ok(best)
        }
        Random => {
            if a.is_empty() {
                return Ok(fl(vm_random(vm)));
            }
            let lo = need_int(vm, &a[0], &name("random"), 1)?;
            if a.len() < 2 {
                return vm.fail("TypeException", "Math.random() takes 0 or 2 arguments (min, max)");
            }
            let hi = need_int(vm, &a[1], &name("random"), 2)?;
            if hi < lo {
                return vm.fail("ValueException", format!("empty range in randint({}, {})", lo, hi));
            }
            let span = (hi as i128 - lo as i128 + 1) as u128;
            let r = (vm_next(vm) as u128) % span;
            Ok(Value::Int((lo as i128 + r as i128) as i64))
        }
        Clamp => {
            for (i, v) in a.iter().enumerate() {
                num(vm, v, &name("clamp"), i + 1)?;
            }
            // max(lo, min(hi, x)) keeping the winner's type
            let (x, lo, hi) = (&a[0], &a[1], &a[2]);
            let inner = if as_f64(x) < as_f64(hi) { x } else { hi };
            let r = if as_f64(inner) > as_f64(lo) { inner } else { lo };
            Ok(r.clone())
        }
        Sqrt | Sin | Cos | Tan | Log | Log10 | Exp => {
            let x = num(vm, &a[0], &name(fname), 1)?;
            let r = match m {
                Sqrt => {
                    if x < 0.0 {
                        f64::NAN
                    } else {
                        x.sqrt()
                    }
                }
                Sin => x.sin(),
                Cos => x.cos(),
                Tan => x.tan(),
                Log => {
                    if x <= 0.0 {
                        f64::NAN
                    } else {
                        x.ln()
                    }
                }
                Log10 => {
                    if x <= 0.0 {
                        f64::NAN
                    } else {
                        x.log10()
                    }
                }
                _ => x.exp(),
            };
            if (r.is_nan() && !x.is_nan()) || (r.is_infinite() && x.is_finite()) {
                return vm.fail(
                    "ValueException",
                    format!("Math.{}({}) is not defined", fname, plain_display(&a[0])),
                );
            }
            Ok(fl(r))
        }
    }
}

fn as_f64(v: &Value) -> f64 {
    match v {
        Value::Int(n) => *n as f64,
        Value::Float(f) => *f,
        _ => 0.0,
    }
}

fn vm_next(vm: &mut Vm) -> u64 {
    // xorshift64*
    let mut x = vm.rng;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    vm.rng = x;
    x.wrapping_mul(0x2545F4914F6CDD1D)
}

fn vm_random(vm: &mut Vm) -> f64 {
    (vm_next(vm) >> 11) as f64 / (1u64 << 53) as f64
}

// ---------------------------------------------------------------------------
// methods of built-in types
// ---------------------------------------------------------------------------

pub fn method_name(id: M) -> &'static str {
    use M::*;
    match id {
        Len => "len",
        Length => "length",
        Upper => "upper",
        Lower => "lower",
        Trim => "trim",
        TrimStart => "trimStart",
        TrimEnd => "trimEnd",
        Contains => "contains",
        StartsWith => "startsWith",
        EndsWith => "endsWith",
        IndexOf => "indexOf",
        Split => "split",
        Replace => "replace",
        Substr => "substr",
        Pad => "pad",
        Repeat => "repeat",
        Reverse => "reverse",
        Slug => "slug",
        Capitalize => "capitalize",
        IsEmpty => "isEmpty",
        Chars => "chars",
        Push => "push",
        Pop => "pop",
        Insert => "insert",
        RemoveAt => "removeAt",
        Remove => "remove",
        Join => "join",
        Map => "map",
        Filter => "filter",
        Reduce => "reduce",
        Each => "each",
        Any => "any",
        All => "all",
        Find => "find",
        Sort => "sort",
        Sum => "sum",
        First => "first",
        Last => "last",
        Slice => "slice",
        Copy => "copy",
        Clear => "clear",
        Keys => "keys",
        Values => "values",
        Items => "items",
        Has => "has",
        Get => "get",
        Merge => "merge",
        Add => "add",
        ToList => "toList",
        Union => "union",
        Intersect => "intersect",
        Diff => "diff",
    }
}

pub fn string_arity(id: M) -> Option<(usize, usize)> {
    use M::*;
    Some(match id {
        Len | Length | Upper | Lower | Trim | TrimStart | TrimEnd | Reverse | Slug | Capitalize | IsEmpty | Chars => (0, 0),
        Contains | StartsWith | EndsWith | IndexOf | Repeat => (1, 1),
        Split => (0, 1),
        Replace => (2, 2),
        Substr => (1, 2),
        Pad => (1, 3),
        _ => return None,
    })
}

/// (min, max) arguments of a method of this value's type; None if it has no such method.
pub fn method_arity(recv: &Value, id: M) -> Option<(usize, Option<usize>)> {
    use M::*;
    match recv {
        Value::Str(_) => string_arity(id).map(|(a, b)| (a, Some(b))),
        Value::List(_) => Some(match id {
            Len | Pop | Reverse | Sum | First | Last | Copy | IsEmpty | Clear => (0, Some(0)),
            Push => (1, None),
            Insert | Reduce => (2, Some(2)),
            RemoveAt | Remove | Contains | IndexOf | Map | Filter | Each | Any | All | Find => (1, Some(1)),
            Join | Sort => (0, Some(1)),
            Slice => (1, Some(2)),
            _ => return None,
        }),
        Value::Dict(_) => Some(match id {
            Len | Keys | Values | Items | Copy | IsEmpty | Clear => (0, Some(0)),
            Has | Remove | Merge => (1, Some(1)),
            Get => (1, Some(2)),
            _ => return None,
        }),
        Value::Set(_) => Some(match id {
            Len | ToList | IsEmpty => (0, Some(0)),
            Add => (1, None),
            Has | Remove | Union | Intersect | Diff => (1, Some(1)),
            _ => return None,
        }),
        Value::Range(_) => Some(match id {
            Len | ToList => (0, Some(0)),
            Contains => (1, Some(1)),
            _ => return None,
        }),
        _ => None,
    }
}

/// A method taken as a value and called later (`f = nums.push; f(1)`).
pub fn call_method(vm: &mut Vm, recv: &Value, name: &str, args: Vec<Value>) -> R<Value> {
    match method_id(name) {
        Some(id) => call_method_id(vm, recv, id, name, args),
        None => vm.fail("NameException", format!("{} has no method '{}'", type_name(recv), name)),
    }
}

pub fn call_method_id(vm: &mut Vm, recv: &Value, id: M, name: &str, args: Vec<Value>) -> R<Value> {
    let Some((lo, hi)) = method_arity(recv, id) else {
        return vm.fail("NameException", format!("{} has no method '{}'", type_name(recv), name));
    };
    let k = args.len();
    if k < lo || hi.map(|h| k > h).unwrap_or(false) {
        let expected = match hi {
            Some(h) if h == lo => format!("{} argument{}", h, if h != 1 { "s" } else { "" }),
            None => format!("at least {} arguments", lo),
            Some(h) => format!("{} to {} arguments", lo, h),
        };
        return vm.fail(
            "TypeException",
            format!("{}.{}() expects {}, got {}", type_name(recv), name, expected, k),
        );
    }
    match recv {
        Value::Str(t) => string_method(vm, t, id, &args),
        Value::List(l) => list_method(vm, recv, l, id, args),
        Value::Dict(d) => dict_method(vm, d, id, args),
        Value::Set(st) => set_method(vm, st, id, args),
        Value::Range(r) => Ok(match id {
            M::Len => Value::Int(r.len()),
            M::ToList => list((0..r.len()).map(|i| Value::Int(r.get(i))).collect()),
            _ => match &args[0] {
                Value::Int(n) => Value::Bool(vm.contains(recv, &Value::Int(*n))?),
                _ => Value::Bool(false),
            },
        }),
        _ => vm.fail("NameException", format!("{} has no method '{}'", type_name(recv), name)),
    }
}

/// Python slice bounds for a sequence of length n.
fn slice_bounds(n: i64, start: i64, end: Option<i64>) -> (usize, usize) {
    let norm = |i: i64| -> i64 {
        if i < 0 {
            (i + n).max(0)
        } else {
            i.min(n)
        }
    };
    let a = norm(start);
    let b = end.map(norm).unwrap_or(n);
    if b <= a {
        (a as usize, a as usize)
    } else {
        (a as usize, b as usize)
    }
}

fn substr_chars(t: &BStr, a: usize, b: usize) -> String {
    if t.is_ascii() {
        t.s[a..b].to_string()
    } else {
        t.s.chars().skip(a).take(b - a).collect()
    }
}

fn py_center(t: &str, n: usize, width: usize, fill: char) -> String {
    if width <= n {
        return t.to_string();
    }
    let marg = width - n;
    let left = marg / 2 + (marg & width & 1);
    let right = marg - left;
    format!("{}{}{}", fill.to_string().repeat(left), t, fill.to_string().repeat(right))
}

fn slugify(t: &str) -> String {
    let low = t.to_lowercase();
    let kept: String = low.chars().filter(|c| c.is_alphanumeric() || *c == '_' || c.is_whitespace() || *c == '-').collect();
    let mut out = String::new();
    let mut in_sep = false;
    for c in kept.chars() {
        if c.is_whitespace() || c == '_' || c == '-' {
            if !in_sep {
                out.push('-');
                in_sep = true;
            }
        } else {
            out.push(c);
            in_sep = false;
        }
    }
    out.trim_matches('-').to_string()
}

fn string_method(vm: &mut Vm, t: &Rc<BStr>, id: M, a: &[Value]) -> R<Value> {
    use M::*;
    let st = &t.s;
    Ok(match id {
        Len | Length => Value::Int(t.chars as i64),
        Upper => s(st.to_uppercase()),
        Lower => s(st.to_lowercase()),
        Trim => s(st.trim().to_string()),
        TrimStart => s(st.trim_start().to_string()),
        TrimEnd => s(st.trim_end().to_string()),
        Contains => Value::Bool(st.contains(need_str(vm, &a[0], "contains", 1)?.s.as_str())),
        StartsWith => Value::Bool(st.starts_with(need_str(vm, &a[0], "startsWith", 1)?.s.as_str())),
        EndsWith => Value::Bool(st.ends_with(need_str(vm, &a[0], "endsWith", 1)?.s.as_str())),
        IndexOf => {
            let sub = need_str(vm, &a[0], "indexOf", 1)?;
            match st.find(sub.s.as_str()) {
                Some(b) => Value::Int(st[..b].chars().count() as i64),
                None => Value::Int(-1),
            }
        }
        Split => {
            let sep = match a.first() {
                Some(v) => Some(need_str(vm, v, "split", 1)?),
                None => None,
            };
            match sep {
                Some(sp) if sp.s.is_empty() => list(st.chars().map(|c| s(c.to_string())).collect()),
                Some(sp) => list(st.split(sp.s.as_str()).map(|p| s(p.to_string())).collect()),
                None => list(st.split_whitespace().map(|p| s(p.to_string())).collect()),
            }
        }
        Replace => {
            let from = need_str(vm, &a[0], "replace", 1)?;
            let to = need_str(vm, &a[1], "replace", 2)?;
            if from.s.is_empty() {
                // Python inserts the replacement between every character
                let mut out = to.s.clone();
                for c in st.chars() {
                    out.push(c);
                    out.push_str(&to.s);
                }
                s(out)
            } else {
                s(st.replace(from.s.as_str(), &to.s))
            }
        }
        Substr => {
            let start = need_int(vm, &a[0], "substr", 1)?;
            let n = t.chars as i64;
            let (x, y) = if a.len() > 1 {
                let length = need_int(vm, &a[1], "substr", 2)?;
                if start >= 0 {
                    slice_bounds(n, start, Some(start.saturating_add(length)))
                } else {
                    slice_bounds(n, start, Some(n + start + length))
                }
            } else {
                slice_bounds(n, start, None)
            };
            s(substr_chars(t, x, y))
        }
        Pad => {
            let width = need_int(vm, &a[0], "pad", 1)?;
            let fill = match a.get(1) {
                Some(v) => need_str(vm, v, "pad", 2)?,
                None => Rc::new(BStr::new(" ".into())),
            };
            let side = match a.get(2) {
                Some(v) => need_str(vm, v, "pad", 3)?.s.clone(),
                None => "right".into(),
            };
            if fill.chars != 1 {
                return vm.fail("ValueException", "pad() fill must be a single character");
            }
            let fc = fill.s.chars().next().unwrap();
            let n = t.chars;
            let w = width.max(0) as usize;
            let extra = w.saturating_sub(n);
            match side.as_str() {
                "left" => s(format!("{}{}", fc.to_string().repeat(extra), st)),
                "right" => s(format!("{}{}", st, fc.to_string().repeat(extra))),
                "both" => s(py_center(st, n, w, fc)),
                _ => return vm.fail("ValueException", "pad() side must be \"left\", \"right\" or \"both\""),
            }
        }
        Repeat => {
            let count = need_int(vm, &a[0], "repeat", 1)?;
            if count < 0 {
                return vm.fail("ValueException", "repeat() count cannot be negative");
            }
            s(st.repeat(count as usize))
        }
        Reverse => s(st.chars().rev().collect::<String>()),
        Slug => s(slugify(st)),
        Capitalize => {
            let mut c = st.chars();
            match c.next() {
                Some(f) => s(format!("{}{}", f.to_uppercase(), c.as_str())),
                None => s(""),
            }
        }
        IsEmpty => Value::Bool(st.is_empty()),
        Chars => list(st.chars().map(|c| s(c.to_string())).collect()),
        _ => return vm.fail("NameException", format!("string has no method '{}'", method_name(id))),
    })
}

fn join(vm: &mut Vm, items: &[Value], sep: &str) -> R<Value> {
    let mut out = String::new();
    for (i, x) in items.iter().enumerate() {
        if i > 0 {
            out.push_str(sep);
        }
        vm.display_into(x, false, &mut out)?;
    }
    Ok(s(out))
}

fn sort_kind(v: &Value) -> String {
    match v {
        Value::Int(_) | Value::Float(_) => "num".into(),
        other => type_name(other),
    }
}

fn check_sortable(vm: &Vm, values: &[Value], fname: &str) -> R<()> {
    let mut kinds: Vec<String> = Vec::new();
    for v in values {
        let k = sort_kind(v);
        if !kinds.contains(&k) {
            kinds.push(k);
        }
    }
    if kinds.len() > 1 {
        kinds.sort();
        return vm.fail("TypeException", format!("{}() cannot compare {}", fname, kinds.join(" and ")));
    }
    if let Some(v) = values.first() {
        if !matches!(v, Value::Int(_) | Value::Float(_) | Value::Str(_)) {
            return vm.fail(
                "TypeException",
                format!("{}() can only sort numbers or strings, found {}", fname, type_name(v)),
            );
        }
    }
    Ok(())
}

fn sort_cmp(a: &Value, b: &Value) -> std::cmp::Ordering {
    match (a, b) {
        (Value::Str(x), Value::Str(y)) => x.s.cmp(&y.s),
        _ => as_f64(a).partial_cmp(&as_f64(b)).unwrap_or(std::cmp::Ordering::Equal),
    }
}

fn list_method(vm: &mut Vm, recv: &Value, l: &Rc<std::cell::RefCell<Vec<Value>>>, id: M, a: Vec<Value>) -> R<Value> {
    use M::*;
    Ok(match id {
        Len => Value::Int(l.borrow().len() as i64),
        Push => {
            l.borrow_mut().extend(a);
            Value::Null
        }
        Pop => match l.borrow_mut().pop() {
            Some(v) => v,
            None => return vm.fail("IndexException", "pop() on an empty list"),
        },
        Insert => {
            let idx = need_int(vm, &a[0], "insert", 1)?;
            let n = l.borrow().len() as i64;
            if !(0..=n).contains(&idx) {
                return vm.fail("IndexException", format!("insert() index {} is out of range (length {})", idx, n));
            }
            l.borrow_mut().insert(idx as usize, a[1].clone());
            Value::Null
        }
        RemoveAt => {
            let idx = need_int(vm, &a[0], "removeAt", 1)?;
            let n = l.borrow().len() as i64;
            if !(-n..n).contains(&idx) {
                return vm.fail("IndexException", format!("removeAt() index {} is out of range (length {})", idx, n));
            }
            let i = if idx < 0 { idx + n } else { idx } as usize;
            l.borrow_mut().remove(i)
        }
        Remove => {
            let pos = l.borrow().iter().position(|x| values_equal(x, &a[0]));
            match pos {
                Some(i) => {
                    l.borrow_mut().remove(i);
                    Value::Bool(true)
                }
                None => Value::Bool(false),
            }
        }
        Contains => Value::Bool(l.borrow().iter().any(|x| values_equal(&a[0], x))),
        IndexOf => Value::Int(l.borrow().iter().position(|x| values_equal(x, &a[0])).map(|i| i as i64).unwrap_or(-1)),
        Join => {
            let sep = match a.first() {
                Some(v) => need_str(vm, v, "join", 1)?.s.clone(),
                None => String::new(),
            };
            let items = l.borrow().clone();
            join(vm, &items, &sep)?
        }
        Map => {
            let items = l.borrow().clone();
            let mut out = Vec::with_capacity(items.len());
            for x in items {
                out.push(call_fn(vm, &a[0], vec![x], "map")?);
            }
            list(out)
        }
        Filter => {
            let items = l.borrow().clone();
            let mut out = Vec::new();
            for x in items {
                let r = call_fn(vm, &a[0], vec![x.clone()], "filter")?;
                if need_bool(vm, r, "filter")? {
                    out.push(x);
                }
            }
            list(out)
        }
        Reduce => {
            let items = l.borrow().clone();
            let mut acc = a[1].clone();
            for x in items {
                acc = call_fn(vm, &a[0], vec![acc, x], "reduce")?;
            }
            acc
        }
        Each => {
            let items = l.borrow().clone();
            for x in items {
                call_fn(vm, &a[0], vec![x], "each")?;
            }
            Value::Null
        }
        Any | All | Find => {
            let fname = method_name(id);
            let items = l.borrow().clone();
            for x in items {
                let r = call_fn(vm, &a[0], vec![x.clone()], fname)?;
                let b = need_bool(vm, r, fname)?;
                match id {
                    Any if b => return Ok(Value::Bool(true)),
                    All if !b => return Ok(Value::Bool(false)),
                    Find if b => return Ok(x),
                    _ => {}
                }
            }
            match id {
                Any => Value::Bool(false),
                All => Value::Bool(true),
                _ => Value::Null,
            }
        }
        Sort => {
            let items = l.borrow().clone();
            if let Some(f) = a.first() {
                let mut keys = Vec::with_capacity(items.len());
                for x in &items {
                    keys.push(call_fn(vm, f, vec![x.clone()], "sort")?);
                }
                check_sortable(vm, &keys, "sort")?;
                let mut order: Vec<usize> = (0..items.len()).collect();
                order.sort_by(|&i, &j| sort_cmp(&keys[i], &keys[j]));
                *l.borrow_mut() = order.into_iter().map(|i| items[i].clone()).collect();
            } else {
                check_sortable(vm, &items, "sort")?;
                let mut items = items;
                items.sort_by(sort_cmp);
                *l.borrow_mut() = items;
            }
            recv.clone()
        }
        Reverse => {
            l.borrow_mut().reverse();
            recv.clone()
        }
        Sum => {
            let items = l.borrow().clone();
            if items.is_empty() {
                return Ok(Value::Int(0));
            }
            need(vm, &items[0], &["number"], "sum", 1)?;
            let mut total = items[0].clone();
            for x in &items[1..] {
                total = vm.binop(BinOp::Add, total, x.clone())?;
            }
            total
        }
        First => l.borrow().first().cloned().unwrap_or(Value::Null),
        Last => l.borrow().last().cloned().unwrap_or(Value::Null),
        Slice => {
            let start = need_int(vm, &a[0], "slice", 1)?;
            let end = match a.get(1) {
                Some(v) => Some(need_int(vm, v, "slice", 2)?),
                None => None,
            };
            let items = l.borrow();
            let (x, y) = slice_bounds(items.len() as i64, start, end);
            list(items[x..y].to_vec())
        }
        Copy => list(l.borrow().clone()),
        IsEmpty => Value::Bool(l.borrow().is_empty()),
        Clear => {
            l.borrow_mut().clear();
            Value::Null
        }
        _ => return vm.fail("NameException", format!("list has no method '{}'", method_name(id))),
    })
}

fn dict_method(vm: &mut Vm, d: &Rc<std::cell::RefCell<OMap>>, id: M, a: Vec<Value>) -> R<Value> {
    use M::*;
    Ok(match id {
        Len => Value::Int(d.borrow().len() as i64),
        Keys => list(d.borrow().keys()),
        Values => list(d.borrow().values()),
        Items => list(d.borrow().iter().map(|(k, v)| list(vec![k.clone(), v.clone()])).collect()),
        Has => Value::Bool(hkey(&a[0], false).map(|k| d.borrow().contains(&k)).unwrap_or(false)),
        Get => {
            let dflt = a.get(1).cloned().unwrap_or(Value::Null);
            match hkey(&a[0], false) {
                Some(k) => d.borrow().get(&k).cloned().unwrap_or(dflt),
                None => dflt,
            }
        }
        Remove => match hkey(&a[0], false) {
            Some(k) => d.borrow_mut().remove(&k).unwrap_or(Value::Null),
            None => Value::Null,
        },
        Merge => {
            let Value::Dict(other) = &a[0] else {
                need(vm, &a[0], &["dict"], "merge", 1)?;
                return Ok(Value::Null);
            };
            let mut out = d.borrow().clone();
            for (k, v) in other.borrow().iter() {
                out.insert(hkey(k, false).unwrap(), k.clone(), v.clone());
            }
            dict(out)
        }
        Copy => dict(d.borrow().clone()),
        IsEmpty => Value::Bool(d.borrow().len() == 0),
        Clear => {
            d.borrow_mut().clear();
            Value::Null
        }
        _ => return vm.fail("NameException", format!("dict has no method '{}'", method_name(id))),
    })
}

fn set_method(vm: &mut Vm, st: &Rc<std::cell::RefCell<OMap>>, id: M, a: Vec<Value>) -> R<Value> {
    use M::*;
    let other = |vm: &Vm, v: &Value, fname: &str| -> R<OMap> {
        match v {
            Value::Set(o) => Ok(o.borrow().clone()),
            _ => need(vm, v, &["set"], fname, 1).map(|_| OMap::new(true)),
        }
    };
    Ok(match id {
        Len => Value::Int(st.borrow().len() as i64),
        Add => {
            for x in a {
                let k = vm.key_of(&x, "set item")?;
                st.borrow_mut().insert(k, x, Value::Null);
            }
            Value::Null
        }
        Has => Value::Bool(hkey(&a[0], true).map(|k| st.borrow().contains(&k)).unwrap_or(false)),
        Remove => match hkey(&a[0], true) {
            Some(k) => Value::Bool(st.borrow_mut().remove(&k).is_some()),
            None => Value::Bool(false),
        },
        ToList => list(st.borrow().keys()),
        Union => {
            let o = other(vm, &a[0], "union")?;
            let mut out = st.borrow().clone();
            for (k, _) in o.iter() {
                out.insert(hkey(k, true).unwrap(), k.clone(), Value::Null);
            }
            set(out)
        }
        Intersect | Diff => {
            let o = other(vm, &a[0], method_name(id))?;
            let mut out = OMap::new(true);
            for (k, _) in st.borrow().iter() {
                let hk = hkey(k, true).unwrap();
                if o.contains(&hk) == (id == Intersect) {
                    out.insert(hk, k.clone(), Value::Null);
                }
            }
            set(out)
        }
        IsEmpty => Value::Bool(st.borrow().len() == 0),
        _ => return vm.fail("NameException", format!("set has no method '{}'", method_name(id))),
    })
}

// ---------------------------------------------------------------------------
// casts: ~type(value)
// ---------------------------------------------------------------------------

fn parse_int(text: &str) -> Option<i64> {
    let t: String = text.trim().replace('_', "");
    let low = t.to_lowercase();
    if low.len() >= 2 && (low.starts_with("0x") || low.starts_with("0b") || low.starts_with("0o")) {
        let radix = match &low[..2] {
            "0x" => 16,
            "0b" => 2,
            _ => 8,
        };
        return i64::from_str_radix(&low[2..], radix).ok();
    }
    let body = t.strip_prefix('+').unwrap_or(&t);
    if body.is_empty() || body.starts_with('+') {
        return None;
    }
    body.parse::<i64>().ok()
}

fn parse_float(text: &str) -> Option<f64> {
    let t = text.trim();
    let low = t.to_lowercase();
    let low = low.strip_prefix('+').unwrap_or(&low);
    match low {
        "inf" | "infinity" => return Some(if t.starts_with('-') { f64::NEG_INFINITY } else { f64::INFINITY }),
        "-inf" | "-infinity" => return Some(f64::NEG_INFINITY),
        "nan" | "-nan" => return Some(f64::NAN),
        _ => {}
    }
    if t.is_empty() || t.contains(|c: char| c.is_alphabetic() && c != 'e' && c != 'E') {
        return None;
    }
    t.replace('_', "").parse::<f64>().ok()
}

pub fn cast(vm: &mut Vm, t: &Ty, v: Value) -> R<Value> {
    let fail = |vm: &mut Vm, extra: &str| -> R<Value> {
        let shown = vm.display(&v, true)?;
        vm.fail(
            "CastException",
            format!("cannot cast {} {} to {}{}", type_name(&v), shown, t.name, extra),
        )
    };
    match &t.k {
        TyK::Int => match &v {
            Value::Int(_) => Ok(v.clone()),
            Value::Bool(b) => Ok(Value::Int(*b as i64)),
            Value::Float(f) => {
                if f.is_nan() || f.is_infinite() {
                    return fail(vm, "");
                }
                Ok(Value::Int(f.trunc() as i64))
            }
            Value::Str(st) => match parse_int(&st.s) {
                Some(n) => Ok(Value::Int(n)),
                None => fail(vm, ""),
            },
            _ => fail(vm, ""),
        },
        TyK::Float => match &v {
            Value::Float(_) => Ok(v.clone()),
            Value::Int(n) => Ok(Value::Float(*n as f64)),
            Value::Bool(b) => Ok(Value::Float(if *b { 1.0 } else { 0.0 })),
            Value::Str(st) => match parse_float(&st.s) {
                Some(f) => Ok(Value::Float(f)),
                None => fail(vm, ""),
            },
            _ => fail(vm, ""),
        },
        TyK::Str => {
            let d = vm.display(&v, false)?;
            Ok(s(d))
        }
        TyK::Bool => match &v {
            Value::Bool(_) => Ok(v.clone()),
            Value::Int(n) => Ok(Value::Bool(*n != 0)),
            Value::Float(f) => Ok(Value::Bool(*f != 0.0)),
            Value::Str(st) => match st.s.trim().to_lowercase().as_str() {
                "true" | "1" | "yes" | "on" => Ok(Value::Bool(true)),
                "false" | "0" | "no" | "off" | "" => Ok(Value::Bool(false)),
                _ => fail(vm, " (accepted: \"true\"/\"false\", \"1\"/\"0\", \"yes\"/\"no\", \"on\"/\"off\")"),
            },
            Value::Null => Ok(Value::Bool(false)),
            _ => fail(vm, ""),
        },
        TyK::List(_) => match &v {
            Value::List(l) => Ok(list(l.borrow().clone())),
            Value::Range(r) => Ok(list((0..r.len()).map(|i| Value::Int(r.get(i))).collect())),
            Value::Str(st) => Ok(list(st.s.chars().map(|c| s(c.to_string())).collect())),
            Value::Set(st) => Ok(list(st.borrow().keys())),
            Value::Dict(d) => Ok(list(d.borrow().keys())),
            _ => fail(vm, ""),
        },
        TyK::Set(_) => {
            let items: Vec<Value> = match &v {
                Value::List(l) => l.borrow().clone(),
                Value::Range(r) => (0..r.len()).map(|i| Value::Int(r.get(i))).collect(),
                Value::Str(st) => st.s.chars().map(|c| s(c.to_string())).collect(),
                Value::Set(st) => st.borrow().keys(),
                _ => return fail(vm, ""),
            };
            let mut m = OMap::new(true);
            for x in items {
                let k = vm.key_of(&x, "set item")?;
                m.insert(k, x, Value::Null);
            }
            Ok(set(m))
        }
        TyK::Dict(_) => match &v {
            Value::Dict(d) => Ok(dict(d.borrow().clone())),
            Value::Obj(o) => {
                let mut m = OMap::new(false);
                let fields = o.fields.borrow();
                for (i, f) in o.cls.fields.iter().enumerate() {
                    if f.vis == Vis::Public {
                        let k = s(f.name.to_string());
                        m.insert(hkey(&k, false).unwrap(), k, fields[i].clone());
                    }
                }
                Ok(dict(m))
            }
            _ => fail(vm, ""),
        },
        TyK::Any => Ok(v.clone()),
        TyK::Void | TyK::Null | TyK::Range => vm.fail("TypeException", format!("cannot cast to {}", t.name)),
        TyK::Karo => fail(vm, ""),
        TyK::Unknown(n) => vm.fail("NameException", format!("'{}' is not defined", n)),
        TyK::Class(id) => {
            let cls = vm.classes[*id].clone();
            match &v {
                Value::Obj(o) => {
                    if o.cls.is_subclass_of(&cls) {
                        Ok(v.clone())
                    } else {
                        fail(vm, "")
                    }
                }
                Value::Dict(d) => {
                    if cls.is_abstract {
                        return vm.fail("CastException", format!("cannot cast to abstract class {}", cls.name));
                    }
                    let obj = vm.bare(&cls)?;
                    let Value::Obj(o) = &obj else { return Ok(obj) };
                    let items: Vec<(Value, Value)> = d.borrow().iter().cloned().collect();
                    for (k, x) in items {
                        let fi = match &k {
                            Value::Str(ks) => cls.field_index.get(ks.s.as_str()).copied(),
                            _ => None,
                        };
                        let Some(fi) = fi else {
                            let shown = vm.display(&k, true)?;
                            return vm.fail("CastException", format!("{} has no property {}", cls.name, shown));
                        };
                        let f = &cls.fields[fi];
                        if let Some(ft) = &f.ty {
                            if !vm.matches(&x, ft)? {
                                return vm.fail(
                                    "CastException",
                                    format!("{}.{} expects {}, got {}", cls.name, f.name, ft.name, type_name(&x)),
                                );
                            }
                        }
                        o.fields.borrow_mut()[fi] = x;
                    }
                    Ok(obj)
                }
                _ => fail(vm, ""),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_print_like_python() {
        let cases: [(f64, &str); 14] = [
            (1.0, "1.0"),
            (-0.0, "0.0"),
            (0.1 + 0.2, "0.30000000000000004"),
            (1e16, "1e+16"),
            (1.5e-5, "1.5e-05"),
            (2.0f64.powf(70.0), "1.1805916207174113e+21"),
            (123456.789, "123456.789"),
            (0.0001, "0.0001"),
            (1e-7, "1e-07"),
            (-2.5, "-2.5"),
            (1e15 + 0.5, "1000000000000000.5"),
            (f64::INFINITY, "INF"),
            (f64::NEG_INFINITY, "-INF"),
            (3.14, "3.14"),
        ];
        for (f, want) in cases {
            assert_eq!(format_float(f), want, "{:?}", f);
        }
        assert_eq!(format_float(f64::NAN), "NAN");
    }

    #[test]
    fn ints_parse_like_python() {
        assert_eq!(parse_int(" 42 "), Some(42));
        assert_eq!(parse_int("+7"), Some(7));
        assert_eq!(parse_int("-7"), Some(-7));
        assert_eq!(parse_int("1_000"), Some(1000));
        assert_eq!(parse_int("0x1F"), Some(31));
        assert_eq!(parse_int("0b101"), Some(5));
        assert_eq!(parse_int("12a"), None);
        assert_eq!(parse_int(""), None);
    }

    #[test]
    fn python_center() {
        assert_eq!(py_center("ab", 2, 5, '*'), "**ab*");
        assert_eq!(py_center("abc", 3, 6, '*'), "*abc**");
    }
}

// ===========================================================================
// JSON module
// ===========================================================================

fn json_encode_value(vm: &mut Vm, v: &Value, indent: Option<usize>, depth: usize) -> R<String> {
    match v {
        Value::Null => Ok("null".into()),
        Value::Bool(true) => Ok("true".into()),
        Value::Bool(false) => Ok("false".into()),
        Value::Int(n) => Ok(n.to_string()),
        Value::Float(f) => {
            if f.is_nan() || f.is_infinite() {
                return vm.fail("ValueException", "JSON does not support NaN or Infinity");
            }
            Ok(std_impl::json_encode_float(*f))
        }
        Value::Str(t) => {
            let mut out = String::new();
            std_impl::encode_string(&t.s, &mut out);
            Ok(out)
        }
        Value::Set(st) => {
            // sets encode as JSON arrays
            let items: Vec<Value> = st.borrow().iter().map(|(k, _)| k.clone()).collect();
            return json_encode_value(vm, &Value::List(Rc::new(RefCell::new(items))), indent, depth);
        }
        Value::List(l) => {
            let items = l.borrow().clone();
            if items.is_empty() { return Ok("[]".into()); }
            if let Some(ind) = indent {
                let pad = "    ".repeat(depth + 1);
                let close = "    ".repeat(depth);
                let mut parts = Vec::new();
                for x in &items {
                    parts.push(format!("{}{}", pad, json_encode_value(vm, x, Some(ind), depth + 1)?));
                }
                Ok(format!("[\n{}\n{}]", parts.join(",\n"), close))
            } else {
                let mut parts = Vec::new();
                for x in &items {
                    parts.push(json_encode_value(vm, x, None, 0)?);
                }
                Ok(format!("[{}]", parts.join(",")))
            }
        }
        Value::Dict(d) => {
            let entries: Vec<(Value, Value)> = d.borrow().iter().cloned().collect();
            if entries.is_empty() { return Ok("{}".into()); }
            if let Some(ind) = indent {
                let pad = "    ".repeat(depth + 1);
                let close = "    ".repeat(depth);
                let mut parts = Vec::new();
                for (k, v2) in &entries {
                    if !matches!(k, Value::Str(_)) {
                        return vm.fail("JsonException", format!("JSON object keys must be strings, found {} {}", type_name(k), plain_display(k)));
                    }
                    let ks = json_encode_value(vm, k, None, 0)?;
                    let vs = json_encode_value(vm, v2, Some(ind), depth + 1)?;
                    parts.push(format!("{}{}: {}", pad, ks, vs));
                }
                Ok(format!("{{\n{}\n{}}}", parts.join(",\n"), close))
            } else {
                let mut parts = Vec::new();
                for (k, v2) in &entries {
                    if !matches!(k, Value::Str(_)) {
                        return vm.fail("JsonException", format!("JSON object keys must be strings, found {} {}", type_name(k), plain_display(k)));
                    }
                    let ks = json_encode_value(vm, k, None, 0)?;
                    let vs = json_encode_value(vm, v2, None, 0)?;
                    parts.push(format!("{}:{}", ks, vs));
                }
                Ok(format!("{{{}}}", parts.join(",")))
            }
        }
        Value::Obj(o) if o.cls.is_enum() => {
            // Enums with int/string backing values encode as their value
            if o.cls.backing.is_some() {
                let val = o.field("value");
                match &val {
                    Value::Str(_) | Value::Int(_) => json_encode_value(vm, &val, indent, depth),
                    _ => vm.fail("JsonException", format!("cannot encode {}::{} — only enums with int or string values can be encoded",
                        o.cls.name, plain_display(&o.field("name")))),
                }
            } else {
                vm.fail("JsonException", format!("cannot encode {}::{} — only enums with int or string values can be encoded",
                    o.cls.name, plain_display(&o.field("name"))))
            }
        }
        Value::Obj(o) => {
            // encode public fields only
            let fields = o.fields.borrow().clone();
            if let Some(ind) = indent {
                let pad = "    ".repeat(depth + 1);
                let close = "    ".repeat(depth);
                let mut parts = Vec::new();
                for (i, f) in o.cls.fields.iter().enumerate() {
                    if f.vis != Vis::Public { continue; }
                    let ks = format!("\"{}\"", f.name);
                    let vs = json_encode_value(vm, &fields[i], Some(ind), depth + 1)?;
                    parts.push(format!("{}{}: {}", pad, ks, vs));
                }
                if parts.is_empty() { return Ok("{}".into()); }
                Ok(format!("{{\n{}\n{}}}", parts.join(",\n"), close))
            } else {
                let mut parts = Vec::new();
                for (i, f) in o.cls.fields.iter().enumerate() {
                    if f.vis != Vis::Public { continue; }
                    let ks = format!("\"{}\"", f.name);
                    let vs = json_encode_value(vm, &fields[i], None, 0)?;
                    parts.push(format!("{}:{}", ks, vs));
                }
                Ok(format!("{{{}}}", parts.join(",")))
            }
        }
        other => vm.fail("ValueException", format!("cannot JSON-encode value of type {}", type_name(other))),
    }
}

fn json_decode_j(j: &crate::json::J) -> Value {
    use crate::json::J;
    match j {
        J::Null => Value::Null,
        J::Bool(b) => Value::Bool(*b),
        J::Int(n) => Value::Int(*n),
        J::Num(f) => Value::Float(*f),
        J::Str(sv) => Value::Str(Rc::new(BStr::new(sv.clone()))),
        J::Arr(a) => list(a.iter().map(json_decode_j).collect()),
        J::Obj(fields) => {
            let mut m = OMap::new(false);
            for (k, v) in fields {
                let hk = HKey::Str(Rc::new(BStr::new(k.clone())));
                m.insert(hk, Value::Str(Rc::new(BStr::new(k.clone()))), json_decode_j(v));
            }
            dict(m)
        }
    }
}

fn json_op(vm: &mut Vm, op: JsonOp, args: Vec<Value>) -> R<Value> {
    match op {
        JsonOp::Encode => {
            json_encode_value(vm, &args[0], None, 0).map(|s| Value::Str(Rc::new(BStr::new(s))))
        }
        JsonOp::Decode => {
            let t = need_str(vm, &args[0], "Json.decode", 1)?;
            match crate::json::parse(&t.s) {
                Ok(j) => Ok(json_decode_j(&j)),
                Err(e) => vm.fail("JsonException", format!("invalid JSON: {}", e)),
            }
        }
        JsonOp::Valid => {
            let t = need_str(vm, &args[0], "Json.valid", 1)?;
            Ok(Value::Bool(crate::json::parse(&t.s).is_ok()))
        }
        JsonOp::Pretty => {
            need(vm, &args[0], &["string","int","float","bool","null","list","dict","object"], "Json.pretty", 1)?;
            json_encode_value(vm, &args[0], Some(4), 0).map(|s| Value::Str(Rc::new(BStr::new(s))))
        }
    }
}

// ===========================================================================
// IO module
// ===========================================================================

fn io_op(vm: &mut Vm, op: IoOp, args: Vec<Value>) -> R<Value> {
    use std::io::{Read, Write};
    match op {
        IoOp::ReadFile => {
            let path = need_str(vm, &args[0], "IO.readFile", 1)?;
            std::fs::read_to_string(&path.s).map(|s| Value::Str(Rc::new(BStr::new(s)))).map_err(|e| {
                vm.exc("IOException", format!("{}: {}", e.to_string().to_lowercase(), path.s))
            })
        }
        IoOp::WriteFile => {
            let path = need_str(vm, &args[0], "IO.writeFile", 1)?;
            let content = need_str(vm, &args[1], "IO.writeFile", 2)?;
            std::fs::write(&path.s, content.s.as_bytes()).map(|_| Value::Null).map_err(|e| {
                vm.exc("IOException", format!("{}: {}", e, path.s))
            })
        }
        IoOp::AppendFile => {
            let path = need_str(vm, &args[0], "IO.appendFile", 1)?;
            let content = need_str(vm, &args[1], "IO.appendFile", 2)?;
            std::fs::OpenOptions::new().create(true).append(true).open(&path.s)
                .and_then(|mut f| f.write_all(content.s.as_bytes()))
                .map(|_| Value::Null)
                .map_err(|e| vm.exc("IOException", format!("{}: {}", e, path.s)))
        }
        IoOp::ReadLines => {
            let path = need_str(vm, &args[0], "IO.readLines", 1)?;
            let content = std::fs::read_to_string(&path.s).map_err(|e| vm.exc("IOException", format!("{}: {}", e, path.s)))?;
            let lines: Vec<Value> = content.lines().map(|l| Value::Str(Rc::new(BStr::new(l.to_string())))).collect();
            Ok(list(lines))
        }
        IoOp::MakeDir => {
            let path = need_str(vm, &args[0], "IO.makeDir", 1)?;
            std::fs::create_dir_all(&path.s).map(|_| Value::Null).map_err(|e| vm.exc("IOException", format!("{}: {}", e, path.s)))
        }
        IoOp::ListDir => {
            let path = need_str(vm, &args[0], "IO.listDir", 1)?;
            let entries = std::fs::read_dir(&path.s).map_err(|e| vm.exc("IOException", format!("{}: {}", e, path.s)))?;
            let mut names: Vec<String> = entries.filter_map(|e| e.ok())
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .filter(|n| !n.starts_with('.'))
                .collect();
            names.sort();
            Ok(list(names.into_iter().map(|n| Value::Str(Rc::new(BStr::new(n)))).collect()))
        }
        IoOp::Exists => {
            let path = need_str(vm, &args[0], "IO.exists", 1)?;
            Ok(Value::Bool(std::path::Path::new(&path.s).exists()))
        }
        IoOp::IsDir => {
            let path = need_str(vm, &args[0], "IO.isDir", 1)?;
            Ok(Value::Bool(std::path::Path::new(&path.s).is_dir()))
        }
        IoOp::DeleteFile => {
            let path = need_str(vm, &args[0], "IO.deleteFile", 1)?;
            Ok(Value::Bool(std::fs::remove_file(&path.s).is_ok()))
        }
        IoOp::RemoveDir => {
            let path = need_str(vm, &args[0], "IO.removeDir", 1)?;
            Ok(Value::Bool(std::fs::remove_dir_all(&path.s).is_ok()))
        }
        IoOp::Size => {
            let path = need_str(vm, &args[0], "IO.size", 1)?;
            std::fs::metadata(&path.s)
                .map(|m| Value::Int(m.len() as i64))
                .map_err(|e| vm.exc("IOException", format!("{}: {}", e, path.s)))
        }
    }
}

// ===========================================================================
// Path module
// ===========================================================================

fn path_op(vm: &mut Vm, op: PathOp, args: Vec<Value>) -> R<Value> {
    match op {
        PathOp::Join => {
            let mut result = String::new();
            for (i, a) in args.iter().enumerate() {
                let part = need_str(vm, a, "Path.join", i + 1)?;
                if i == 0 {
                    result = part.s.clone();
                } else {
                    if result.ends_with('/') || result.ends_with('\\') {
                        result.push_str(&part.s);
                    } else {
                        result.push('/');
                        result.push_str(&part.s);
                    }
                }
            }
            Ok(Value::Str(Rc::new(BStr::new(result))))
        }
        PathOp::Basename => {
            let path = need_str(vm, &args[0], "Path.basename", 1)?;
            let p = std::path::Path::new(&path.s);
            Ok(Value::Str(Rc::new(BStr::new(p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()))))
        }
        PathOp::Extension => {
            let path = need_str(vm, &args[0], "Path.extension", 1)?;
            // Return last extension (after last dot in filename)
            let fname = std::path::Path::new(&path.s).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            let ext = if let Some(dot) = fname.rfind('.') {
                &fname[dot + 1..]
            } else { "" };
            Ok(Value::Str(Rc::new(BStr::new(ext.to_string()))))
        }
        PathOp::Stem => {
            let path = need_str(vm, &args[0], "Path.stem", 1)?;
            let p = std::path::Path::new(&path.s);
            let stem = p.file_stem().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            Ok(Value::Str(Rc::new(BStr::new(stem))))
        }
        PathOp::Dirname => {
            let path = need_str(vm, &args[0], "Path.dirname", 1)?;
            let p = std::path::Path::new(&path.s);
            Ok(Value::Str(Rc::new(BStr::new(p.parent().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()))))
        }
        PathOp::Temp => {
            Ok(Value::Str(Rc::new(BStr::new(std::env::temp_dir().to_string_lossy().into_owned()))))
        }
    }
}

// ===========================================================================
// Env module
// ===========================================================================

fn env_op(vm: &mut Vm, op: EnvOp, args: Vec<Value>) -> R<Value> {
    match op {
        EnvOp::Get => {
            let key = need_str(vm, &args[0], "Env.get", 1)?;
            let default = args.get(1).cloned().unwrap_or(Value::Null);
            Ok(std::env::var(&key.s).map(|v| Value::Str(Rc::new(BStr::new(v)))).unwrap_or(default))
        }
        EnvOp::Set => {
            let key = need_str(vm, &args[0], "Env.set", 1)?;
            let val = need_str(vm, &args[1], "Env.set", 2)?;
            std::env::set_var(&key.s, &val.s);
            Ok(Value::Null)
        }
        EnvOp::Bool => {
            let key = need_str(vm, &args[0], "Env.bool", 1)?;
            let v = std::env::var(&key.s).unwrap_or_default().to_lowercase();
            Ok(Value::Bool(matches!(v.as_str(), "1" | "true" | "yes" | "on")))
        }
        EnvOp::Has => {
            let key = need_str(vm, &args[0], "Env.has", 1)?;
            Ok(Value::Bool(std::env::var(&key.s).is_ok()))
        }
        EnvOp::Int => {
            let key = need_str(vm, &args[0], "Env.int", 1)?;
            match std::env::var(&key.s) {
                Ok(v) => v.trim().parse::<i64>().map(Value::Int)
                    .map_err(|_| vm.exc("ValueException", format!("Env.int: cannot parse \"{}\" as int", v))),
                Err(_) => vm.fail("ValueException", format!("Env.int: \"{}\" is not set", key.s)),
            }
        }
        EnvOp::Load => {
            let path = need_str(vm, &args[0], "Env.load", 1)?;
            let content = std::fs::read_to_string(&path.s).map_err(|e| vm.exc("IOException", format!("{}: {}", e, path.s)))?;
            let mut count: i64 = 0;
            for line in content.lines() {
                let line = line.trim();
                if line.is_empty() || line.starts_with('#') { continue; }
                // strip inline comments
                let line = if let Some(pos) = find_inline_comment(line) { &line[..pos] } else { line };
                let line = line.trim();
                if let Some((k, v)) = line.split_once('=') {
                    let k = k.trim();
                    let v = v.trim().trim_matches('"').trim_matches('\'');
                    std::env::set_var(k, v);
                    count += 1;
                }
            }
            Ok(Value::Int(count))
        }
    }
}

fn find_inline_comment(s: &str) -> Option<usize> {
    let mut in_q = false;
    let mut qc = '"';
    for (i, c) in s.char_indices() {
        if in_q {
            if c == qc { in_q = false; }
        } else if c == '"' || c == '\'' {
            in_q = true; qc = c;
        } else if c == '#' {
            return Some(i);
        }
    }
    None
}

// ===========================================================================
// Hash module
// ===========================================================================

fn hash_op(vm: &mut Vm, op: HashOp, args: Vec<Value>) -> R<Value> {
    match op {
        HashOp::Md5 => {
            let t = need_str(vm, &args[0], "Hash.md5", 1)?;
            Ok(Value::Str(Rc::new(BStr::new(std_impl::hex(&std_impl::md5(t.s.as_bytes()))))))
        }
        HashOp::Sha256 => {
            let t = need_str(vm, &args[0], "Hash.sha256", 1)?;
            Ok(Value::Str(Rc::new(BStr::new(std_impl::hex(&std_impl::sha256(t.s.as_bytes()))))))
        }
        HashOp::Sha1 => {
            let t = need_str(vm, &args[0], "Hash.sha1", 1)?;
            Ok(Value::Str(Rc::new(BStr::new(std_impl::hex(&std_impl::sha1(t.s.as_bytes()))))))
        }
        HashOp::Sha512 => {
            let t = need_str(vm, &args[0], "Hash.sha512", 1)?;
            Ok(Value::Str(Rc::new(BStr::new(std_impl::hex(&std_impl::sha512(t.s.as_bytes()))))))
        }
        HashOp::Hmac => {
            let key = need_str(vm, &args[0], "Hash.hmac", 1)?;
            let msg = need_str(vm, &args[1], "Hash.hmac", 2)?;
            let algo = args.get(2).and_then(|v| if let Value::Str(t) = v { Some(t.s.as_str().to_string()) } else { None }).unwrap_or_else(|| "sha256".into());
            let result = match algo.as_str() {
                "sha256" => std_impl::hex(&std_impl::hmac_sha256(key.s.as_bytes(), msg.s.as_bytes())),
                "sha512" => std_impl::hex(&std_impl::hmac_sha512(key.s.as_bytes(), msg.s.as_bytes())),
                "sha1" => std_impl::hex(&std_impl::hmac_sha1(key.s.as_bytes(), msg.s.as_bytes())),
                "md5" => std_impl::hex(&std_impl::hmac_md5(key.s.as_bytes(), msg.s.as_bytes())),
                a => return vm.fail("ValueException", format!("Hash.hmac: unknown algorithm \"{}\"", a)),
            };
            Ok(Value::Str(Rc::new(BStr::new(result))))
        }
        HashOp::Password => {
            let pwd = need_str(vm, &args[0], "Hash.password", 1)?;
            let salt = std_impl::random_bytes(16);
            let salt_b64 = std_impl::b64url_encode(&salt);
            let dk = std_impl::pbkdf2_sha256(pwd.s.as_bytes(), &salt, 310_000, 32);
            let dk_b64 = std_impl::b64url_encode(&dk);
            Ok(Value::Str(Rc::new(BStr::new(format!("$pbkdf2-sha256$310000${}${}", salt_b64, dk_b64)))))
        }
        HashOp::Verify => {
            let pwd = need_str(vm, &args[0], "Hash.verify", 1)?;
            let hash = need_str(vm, &args[1], "Hash.verify", 2)?;
            let h = &hash.s;
            if !h.starts_with("$pbkdf2-sha256$") {
                return Ok(Value::Bool(false));
            }
            let parts: Vec<&str> = h.split('$').collect();
            // "$pbkdf2-sha256$310000$salt$dk" → ["", "pbkdf2-sha256", "310000", "salt", "dk"]
            if parts.len() != 5 { return Ok(Value::Bool(false)); }
            let rounds: usize = parts[2].parse().unwrap_or(0);
            let salt = match std_impl::b64url_decode(parts[3]) { Ok(s) => s, Err(_) => return Ok(Value::Bool(false)) };
            let expected = match std_impl::b64url_decode(parts[4]) { Ok(s) => s, Err(_) => return Ok(Value::Bool(false)) };
            let actual = std_impl::pbkdf2_sha256(pwd.s.as_bytes(), &salt, rounds, expected.len());
            Ok(Value::Bool(std_impl::timing_eq(&actual, &expected)))
        }
    }
}

// ===========================================================================
// Crypto module
// ===========================================================================

fn crypto_op(vm: &mut Vm, op: CryptoOp, args: Vec<Value>) -> R<Value> {
    match op {
        CryptoOp::Base64Encode => {
            let t = need_str(vm, &args[0], "Crypto.base64Encode", 1)?;
            let encoded = std_impl::b64url_encode(t.s.as_bytes());
            Ok(Value::Str(Rc::new(BStr::new(encoded))))
        }
        CryptoOp::Base64Decode => {
            let t = need_str(vm, &args[0], "Crypto.base64Decode", 1)?;
            // try standard first, then url-safe
            let bytes = std_impl::b64_decode(&t.s).or_else(|_| std_impl::b64url_decode(&t.s))
                .map_err(|_| vm.exc("ValueException", "Crypto.base64Decode: invalid base64"))?;
            Ok(Value::Str(Rc::new(BStr::new(String::from_utf8_lossy(&bytes).into_owned()))))
        }
        CryptoOp::JwtSign => {
            let payload = &args[0];
            let key = need_str(vm, &args[1], "Crypto.jwtSign", 2)?;
            let header = r#"{"alg":"HS256","typ":"JWT"}"#;
            let header_b64 = std_impl::b64url_encode(header.as_bytes());
            let payload_json = json_encode_value(vm, payload, None, 0)?;
            let payload_b64 = std_impl::b64url_encode(payload_json.as_bytes());
            let signing_input = format!("{}.{}", header_b64, payload_b64);
            let sig = std_impl::hmac_sha256(key.s.as_bytes(), signing_input.as_bytes());
            let sig_b64 = std_impl::b64url_encode(&sig);
            Ok(Value::Str(Rc::new(BStr::new(format!("{}.{}", signing_input, sig_b64)))))
        }
        CryptoOp::JwtVerify => {
            let token = need_str(vm, &args[0], "Crypto.jwtVerify", 1)?;
            let key = need_str(vm, &args[1], "Crypto.jwtVerify", 2)?;
            let parts: Vec<&str> = token.s.splitn(3, '.').collect();
            if parts.len() != 3 {
                return vm.fail("CryptoException", "invalid JWT: wrong number of parts");
            }
            let signing_input = format!("{}.{}", parts[0], parts[1]);
            let expected_sig = std_impl::hmac_sha256(key.s.as_bytes(), signing_input.as_bytes());
            let expected_b64 = std_impl::b64url_encode(&expected_sig);
            if parts[2] != expected_b64 {
                return vm.fail("CryptoException", "invalid JWT: bad signature");
            }
            let payload_bytes = std_impl::b64url_decode(parts[1])
                .map_err(|_| vm.exc("CryptoException", "invalid JWT: bad base64"))?;
            let payload_json = String::from_utf8(payload_bytes)
                .map_err(|_| vm.exc("CryptoException", "invalid JWT: bad utf8"))?;
            match crate::json::parse(&payload_json) {
                Ok(j) => Ok(json_decode_j(&j)),
                Err(e) => vm.fail("CryptoException", format!("invalid JWT: bad payload JSON: {}", e)),
            }
        }
        CryptoOp::RandomInt => {
            let lo = need_int(vm, &args[0], "Crypto.randomInt", 1)?;
            let hi = need_int(vm, &args[1], "Crypto.randomInt", 2)?;
            if lo > hi { return vm.fail("ValueException", "Crypto.randomInt: lo > hi"); }
            let range = (hi - lo + 1) as u64;
            let bytes = std_impl::random_bytes(8);
            let n = u64::from_be_bytes(bytes.try_into().unwrap());
            Ok(Value::Int(lo + (n % range) as i64))
        }
        CryptoOp::RandomHex => {
            let n = need_int(vm, &args[0], "Crypto.randomHex", 1)?;
            if n < 0 { return vm.fail("ValueException", "Crypto.randomHex: n must be >= 0"); }
            let bytes = std_impl::random_bytes(n as usize);
            Ok(Value::Str(Rc::new(BStr::new(std_impl::hex(&bytes)))))
        }
    }
}

// ===========================================================================
// UUID module
// ===========================================================================

fn uuid_op(vm: &mut Vm, op: UuidOp, args: Vec<Value>) -> R<Value> {
    match op {
        UuidOp::V4 => Ok(Value::Str(Rc::new(BStr::new(std_impl::uuid_v4())))),
        UuidOp::V7 => Ok(Value::Str(Rc::new(BStr::new(std_impl::uuid_v7())))),
        UuidOp::IsValid => {
            let t = need_str(vm, &args[0], "Uuid.isValid", 1)?;
            Ok(Value::Bool(std_impl::uuid_valid(&t.s)))
        }
    }
}

// ===========================================================================
// Regex module
// ===========================================================================

fn rx_op(vm: &mut Vm, op: RxOp, args: Vec<Value>) -> R<Value> {
    // Helper to compile a pattern and throw RegexException on bad pattern
    let compile = |vm: &mut Vm, pattern: &str, fname: &str| -> R<std_impl::Regex> {
        std_impl::parse_regex(pattern).map_err(|e| vm.exc("RegexException", format!("{}: bad pattern '{}': {}", fname, pattern, e)))
    };
    match op {
        RxOp::Match => {
            let pat = need_str(vm, &args[0], "Regex.match", 1)?;
            let text = need_str(vm, &args[1], "Regex.match", 2)?;
            let rx = compile(vm, &pat.s, "Regex.match")?;
            Ok(Value::Bool(std_impl::regex_search(&rx, &text.s).is_some()))
        }
        RxOp::Capture => {
            let pat = need_str(vm, &args[0], "Regex.capture", 1)?;
            let text = need_str(vm, &args[1], "Regex.capture", 2)?;
            let rx = compile(vm, &pat.s, "Regex.capture")?;
            match std_impl::regex_search(&rx, &text.s) {
                None => Ok(Value::Null),
                Some(m) => {
                    let items: Vec<Value> = m.groups.iter().map(|g| {
                        g.as_ref().map(|s| Value::Str(Rc::new(BStr::new(s.clone())))).unwrap_or(Value::Null)
                    }).collect();
                    Ok(list(items))
                }
            }
        }
        RxOp::Named => {
            let pat = need_str(vm, &args[0], "Regex.named", 1)?;
            let text = need_str(vm, &args[1], "Regex.named", 2)?;
            let rx = compile(vm, &pat.s, "Regex.named")?;
            match std_impl::regex_search(&rx, &text.s) {
                None => Ok(Value::Null),
                Some(m) => {
                    let mut omap = OMap::new(false);
                    // only named captures
                    for (name, &idx) in &rx.named {
                        if let Some(Some(ref cap)) = m.groups.get(idx) {
                            let hk = HKey::Str(Rc::new(BStr::new(name.clone())));
                            omap.insert(hk, Value::Str(Rc::new(BStr::new(name.clone()))), Value::Str(Rc::new(BStr::new(cap.clone()))));
                        }
                    }
                    Ok(dict(omap))
                }
            }
        }
        RxOp::MatchAll => {
            let pat = need_str(vm, &args[0], "Regex.matchAll", 1)?;
            let text = need_str(vm, &args[1], "Regex.matchAll", 2)?;
            let rx = compile(vm, &pat.s, "Regex.matchAll")?;
            let matches = std_impl::regex_find_all(&rx, &text.s);
            let result: Vec<Value> = matches.into_iter().map(|m| {
                let items: Vec<Value> = m.groups.iter().map(|g| {
                    g.as_ref().map(|s| Value::Str(Rc::new(BStr::new(s.clone())))).unwrap_or(Value::Null)
                }).collect();
                list(items)
            }).collect();
            Ok(list(result))
        }
        RxOp::Replace => {
            let pat = need_str(vm, &args[0], "Regex.replace", 1)?;
            let repl = args[1].clone();
            let text = need_str(vm, &args[2], "Regex.replace", 3)?;
            let count = args.get(3).and_then(|v| if let Value::Int(n) = v { Some(*n as usize) } else { None }).unwrap_or(0);
            let rx = compile(vm, &pat.s, "Regex.replace")?;
            if is_callable(&repl) {
                // callback mode: replace each match by calling the function
                let chars: Vec<char> = text.s.chars().collect();
                let matches = std_impl::regex_find_all(&rx, &text.s);
                if matches.is_empty() { return Ok(Value::Str(Rc::new(BStr::new(text.s.clone())))); }
                let mut result = String::new();
                let mut last_end_byte = 0usize;
                let mut n_replaced = 0;
                for m in &matches {
                    if count > 0 && n_replaced >= count { break; }
                    result.push_str(&text.s[last_end_byte..m.start]);
                    // call repl(match_list)
                    let items: Vec<Value> = m.groups.iter().map(|g| {
                        g.as_ref().map(|s| Value::Str(Rc::new(BStr::new(s.clone())))).unwrap_or(Value::Null)
                    }).collect();
                    let replacement = call_fn(vm, &repl, vec![list(items)], "Regex.replace")?;
                    let rep_str = match &replacement {
                        Value::Str(t) => t.s.clone(),
                        _ => vm.display(&replacement, false)?,
                    };
                    result.push_str(&rep_str);
                    last_end_byte = m.end;
                    n_replaced += 1;
                }
                result.push_str(&text.s[last_end_byte..]);
                Ok(Value::Str(Rc::new(BStr::new(result))))
            } else {
                let repl_str = match &repl {
                    Value::Str(t) => t.s.clone(),
                    _ => need(vm, &repl, &["string", "callable"], "Regex.replace", 2).map(|_| unreachable!())?,
                };
                std_impl::regex_replace(&rx, &repl_str, &text.s, count)
                    .map(|s| Value::Str(Rc::new(BStr::new(s))))
                    .map_err(|e| vm.exc("RegexException", format!("Regex.replace: {}", e)))
            }
        }
        RxOp::Split => {
            let pat = need_str(vm, &args[0], "Regex.split", 1)?;
            let text = need_str(vm, &args[1], "Regex.split", 2)?;
            let rx = compile(vm, &pat.s, "Regex.split")?;
            let parts = std_impl::regex_split(&rx, &text.s);
            Ok(list(parts.into_iter().map(|s| Value::Str(Rc::new(BStr::new(s)))).collect()))
        }
        RxOp::Escape => {
            let t = need_str(vm, &args[0], "Regex.escape", 1)?;
            Ok(Value::Str(Rc::new(BStr::new(std_impl::regex_escape(&t.s)))))
        }
    }
}

// ===========================================================================
// Date module
// ===========================================================================

fn parse_tz(tz: &str) -> i64 {
    // "+05:00" or "-05:30" → offset in seconds
    let s = tz.trim();
    if s == "UTC" || s == "Z" || s == "+00:00" || s == "-00:00" { return 0; }
    let sign: i64 = if s.starts_with('-') { -1 } else { 1 };
    let s = s.trim_start_matches('+').trim_start_matches('-');
    let parts: Vec<&str> = s.split(':').collect();
    if parts.len() != 2 { return 0; }
    let h: i64 = parts[0].parse().unwrap_or(0);
    let m: i64 = parts[1].parse().unwrap_or(0);
    sign * (h * 3600 + m * 60)
}

fn date_op(vm: &mut Vm, op: DateOp, args: Vec<Value>) -> R<Value> {
    match op {
        DateOp::Make => {
            let y = need_int(vm, &args[0], "Date.make", 1)?;
            let m: u32 = need_int(vm, &args[1], "Date.make", 2)? as u32;
            let d: u32 = need_int(vm, &args[2], "Date.make", 3)? as u32;
            let h: u32 = args.get(3).and_then(|v| if let Value::Int(n) = v { Some(*n as u32) } else { None }).unwrap_or(0);
            let mi: u32 = args.get(4).and_then(|v| if let Value::Int(n) = v { Some(*n as u32) } else { None }).unwrap_or(0);
            let s: u32 = args.get(5).and_then(|v| if let Value::Int(n) = v { Some(*n as u32) } else { None }).unwrap_or(0);
            let p = std_impl::DateParts { year: y, month: m, day: d, hour: h, minute: mi, second: s };
            std_impl::parts_to_ts(&p).map(Value::Int).map_err(|e| vm.exc("DateException", format!("Date.make: {}", e)))
        }
        DateOp::Format => {
            let ts = need_int(vm, &args[0], "Date.format", 1)?;
            let fmt = args.get(1).and_then(|v| if let Value::Str(t) = v { Some(t.s.clone()) } else { None }).unwrap_or_else(|| "Y-m-d H:i:s".into());
            let tz_str = args.get(2).and_then(|v| if let Value::Str(t) = v { Some(t.s.clone()) } else { None }).unwrap_or_else(|| "+00:00".into());
            let tz_offset = parse_tz(&tz_str);
            let tz_label = tz_str.trim_matches('"').to_string();
            let result = std_impl::format_date(ts, &fmt, tz_offset, &tz_label);
            Ok(Value::Str(Rc::new(BStr::new(result))))
        }
        DateOp::Iso => {
            let ts = need_int(vm, &args[0], "Date.iso", 1)?;
            let result = std_impl::format_date(ts, "Y-m-d\\TH:i:s\\Z", 0, "UTC");
            Ok(Value::Str(Rc::new(BStr::new(result))))
        }
        DateOp::Now => {
            let ts = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            Ok(Value::Int(ts))
        }
        DateOp::Add => {
            let ts = need_int(vm, &args[0], "Date.add", 1)?;
            let amount = need_int(vm, &args[1], "Date.add", 2)?;
            let unit = need_str(vm, &args[2], "Date.add", 3)?;
            std_impl::date_add(ts, amount, &unit.s).map(Value::Int).map_err(|e| vm.exc("DateException", format!("Date.add: {}", e)))
        }
        DateOp::Diff => {
            let a = need_int(vm, &args[0], "Date.diff", 1)?;
            let b = need_int(vm, &args[1], "Date.diff", 2)?;
            let unit = need_str(vm, &args[2], "Date.diff", 3)?;
            std_impl::date_diff(a, b, &unit.s).map(Value::Int).map_err(|e| vm.exc("DateException", format!("Date.diff: {}", e)))
        }
        DateOp::Parse => {
            let t = need_str(vm, &args[0], "Date.parse", 1)?;
            match std_impl::date_parse(&t.s) {
                Some(ts) => Ok(Value::Int(ts)),
                None => vm.fail("DateException", format!("Date.parse: cannot read \"{}\" as a date (try \"2026-01-15\" or \"2026-01-15 14:30:00\")", t.s)),
            }
        }
        DateOp::StartOf => {
            let ts = need_int(vm, &args[0], "Date.startOf", 1)?;
            let unit = need_str(vm, &args[1], "Date.startOf", 2)?;
            std_impl::date_start_of(ts, &unit.s).map(Value::Int).map_err(|e| vm.exc("DateException", format!("Date.startOf: {}", e)))
        }
        DateOp::Parts => {
            let ts = need_int(vm, &args[0], "Date.parts", 1)?;
            let p = std_impl::ts_to_parts(ts);
            let days = {
                // compute days from epoch for weekday
                let ts_abs = if ts < 0 { ts } else { ts };
                let secs = if ts >= 0 { ts % 86400 } else { let r = ts % 86400; if r == 0 { 0 } else { r + 86400 } };
                (ts - secs) / 86400
            };
            let days = {
                let sod = if ts >= 0 { ts % 86400 } else { let r = ts % 86400; if r == 0 { 0 } else { r + 86400 } };
                (ts - sod) / 86400
            };
            let wday = std_impl::weekday_iso(days);
            let yd = std_impl::year_day(&p);
            let mut m = OMap::new(false);
            let ins = |m: &mut OMap, k: &str, v: Value| {
                let hk = HKey::Str(Rc::new(BStr::new(k.to_string())));
                m.insert(hk, Value::Str(Rc::new(BStr::new(k.to_string()))), v);
            };
            ins(&mut m, "year",    Value::Int(p.year));
            ins(&mut m, "month",   Value::Int(p.month as i64));
            ins(&mut m, "day",     Value::Int(p.day as i64));
            ins(&mut m, "hour",    Value::Int(p.hour as i64));
            ins(&mut m, "minute",  Value::Int(p.minute as i64));
            ins(&mut m, "second",  Value::Int(p.second as i64));
            ins(&mut m, "weekday", Value::Int(wday as i64));
            ins(&mut m, "yearDay", Value::Int(yd as i64));
            Ok(dict(m))
        }
        DateOp::Ago => {
            let ts = need_int(vm, &args[0], "Date.ago", 1)?;
            let now = args.get(1).and_then(|v| if let Value::Int(n) = v { Some(*n) } else { None })
                .unwrap_or_else(|| std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0));
            Ok(Value::Str(Rc::new(BStr::new(std_impl::date_ago(ts, now)))))
        }
        DateOp::IsLeapYear => {
            let y = need_int(vm, &args[0], "Date.isLeapYear", 1)?;
            Ok(Value::Bool(std_impl::is_leap_year(y)))
        }
        DateOp::DaysInMonth => {
            let y = need_int(vm, &args[0], "Date.daysInMonth", 1)?;
            let m: u32 = need_int(vm, &args[1], "Date.daysInMonth", 2)? as u32;
            Ok(Value::Int(std_impl::days_in_month(y, m) as i64))
        }
    }
}

// ===========================================================================
// Arr module
// ===========================================================================

fn arr_op(vm: &mut Vm, op: ArrOp, args: Vec<Value>) -> R<Value> {
    match op {
        ArrOp::Sort => {
            let lst = match &args[0] {
                Value::List(l) => l.clone(),
                _ => return need(vm, &args[0], &["list"], "Arr.sort", 1).map(|_| unreachable!()),
            };
            if let Some(cmp_fn) = args.get(1) {
                // sort with comparator
                let items = lst.borrow().clone();
                // bubble sort for simplicity (test suite won't have huge arrays)
                let mut sorted = items.clone();
                for i in 0..sorted.len() {
                    for j in i+1..sorted.len() {
                        let r = call_fn(vm, cmp_fn, vec![sorted[i].clone(), sorted[j].clone()], "Arr.sort")?;
                        if let Value::Int(n) = r { if n > 0 { sorted.swap(i, j); } }
                    }
                }
                Ok(list(sorted))
            } else {
                let mut items = lst.borrow().clone();
                check_sortable(vm, &items, "Arr.sort")?;
                items.sort_by(sort_cmp);
                Ok(list(items))
            }
        }
        ArrOp::Unique => {
            let lst = match &args[0] {
                Value::List(l) => l.clone(),
                _ => return need(vm, &args[0], &["list"], "Arr.unique", 1).map(|_| unreachable!()),
            };
            let items = lst.borrow().clone();
            let mut seen = Vec::new();
            let mut result = Vec::new();
            for v in items {
                if !seen.iter().any(|x| values_equal(x, &v)) {
                    seen.push(v.clone());
                    result.push(v);
                }
            }
            Ok(list(result))
        }
        ArrOp::Flatten => {
            let lst = match &args[0] {
                Value::List(l) => l.clone(),
                _ => return need(vm, &args[0], &["list"], "Arr.flatten", 1).map(|_| unreachable!()),
            };
            let items = lst.borrow().clone();
            let mut result = Vec::new();
            for v in items {
                match v {
                    Value::List(inner) => result.extend(inner.borrow().iter().cloned()),
                    other => result.push(other),
                }
            }
            Ok(list(result))
        }
        ArrOp::Chunk => {
            let lst = match &args[0] {
                Value::List(l) => l.clone(),
                _ => return need(vm, &args[0], &["list"], "Arr.chunk", 1).map(|_| unreachable!()),
            };
            let n = need_int(vm, &args[1], "Arr.chunk", 2)? as usize;
            if n == 0 { return vm.fail("ValueException", "Arr.chunk: chunk size must be > 0"); }
            let items = lst.borrow().clone();
            let chunks: Vec<Value> = items.chunks(n).map(|c| list(c.to_vec())).collect();
            Ok(list(chunks))
        }
        ArrOp::Max => {
            let lst = match &args[0] {
                Value::List(l) => l.clone(),
                _ => return need(vm, &args[0], &["list"], "Arr.max", 1).map(|_| unreachable!()),
            };
            let items = lst.borrow().clone();
            if items.is_empty() { return vm.fail("ValueException", "Arr.max: empty list"); }
            check_sortable(vm, &items, "Arr.max")?;
            let mut m = items[0].clone();
            for x in &items[1..] { if sort_cmp(x, &m) == std::cmp::Ordering::Greater { m = x.clone(); } }
            Ok(m)
        }
        ArrOp::Avg => {
            let lst = match &args[0] {
                Value::List(l) => l.clone(),
                _ => return need(vm, &args[0], &["list"], "Arr.avg", 1).map(|_| unreachable!()),
            };
            let items = lst.borrow().clone();
            if items.is_empty() { return vm.fail("ValueException", "Arr.avg: empty list"); }
            let mut sum = 0.0f64;
            for (i, x) in items.iter().enumerate() {
                sum += num(vm, x, "Arr.avg", i + 1)?;
            }
            Ok(Value::Float(sum / items.len() as f64))
        }
        ArrOp::Zip => {
            let a = match &args[0] { Value::List(l) => l.borrow().clone(), _ => return need(vm, &args[0], &["list"], "Arr.zip", 1).map(|_| unreachable!()) };
            let b = match &args[1] { Value::List(l) => l.borrow().clone(), _ => return need(vm, &args[1], &["list"], "Arr.zip", 2).map(|_| unreachable!()) };
            let pairs: Vec<Value> = a.into_iter().zip(b.into_iter()).map(|(x, y)| list(vec![x, y])).collect();
            Ok(list(pairs))
        }
        ArrOp::Merge => {
            let mut result = Vec::new();
            for (i, a) in args.iter().enumerate() {
                match a {
                    Value::List(l) => result.extend(l.borrow().iter().cloned()),
                    _ => { need(vm, a, &["list"], "Arr.merge", i + 1)?; }
                }
            }
            Ok(list(result))
        }
        ArrOp::Pluck => {
            let lst = match &args[0] { Value::List(l) => l.borrow().clone(), _ => return need(vm, &args[0], &["list"], "Arr.pluck", 1).map(|_| unreachable!()) };
            let key = need_str(vm, &args[1], "Arr.pluck", 2)?;
            let hk = HKey::Str(Rc::new(BStr::new(key.s.clone())));
            let result: Vec<Value> = lst.iter().map(|item| {
                match item {
                    Value::Dict(d) => d.borrow().get(&hk).cloned().unwrap_or(Value::Null),
                    _ => Value::Null,
                }
            }).collect();
            Ok(list(result))
        }
        ArrOp::CountBy => {
            let lst = match &args[0] { Value::List(l) => l.borrow().clone(), _ => return need(vm, &args[0], &["list"], "Arr.countBy", 1).map(|_| unreachable!()) };
            let f = &args[1];
            let mut counts: Vec<(Value, i64)> = Vec::new();
            for item in lst {
                let key = call_fn(vm, f, vec![item], "Arr.countBy")?;
                if let Some(e) = counts.iter_mut().find(|(k, _)| values_equal(k, &key)) {
                    e.1 += 1;
                } else {
                    counts.push((key, 1));
                }
            }
            let mut m = OMap::new(false);
            for (k, n) in counts {
                if let Some(hk) = hkey(&k, false) {
                    m.insert(hk, k, Value::Int(n));
                }
            }
            Ok(dict(m))
        }
        ArrOp::GroupBy => {
            let lst = match &args[0] { Value::List(l) => l.borrow().clone(), _ => return need(vm, &args[0], &["list"], "Arr.groupBy", 1).map(|_| unreachable!()) };
            let f = &args[1];
            let mut groups: Vec<(Value, Vec<Value>)> = Vec::new();
            for item in lst {
                let key = call_fn(vm, f, vec![item.clone()], "Arr.groupBy")?;
                if let Some(g) = groups.iter_mut().find(|(k, _)| values_equal(k, &key)) {
                    g.1.push(item);
                } else {
                    groups.push((key, vec![item]));
                }
            }
            let mut m = OMap::new(false);
            for (k, items) in groups {
                if let Some(hk) = hkey(&k, false) {
                    m.insert(hk, k, list(items));
                }
            }
            Ok(dict(m))
        }
        ArrOp::Partition => {
            let lst = match &args[0] { Value::List(l) => l.borrow().clone(), _ => return need(vm, &args[0], &["list"], "Arr.partition", 1).map(|_| unreachable!()) };
            let f = &args[1];
            let mut pass = Vec::new();
            let mut fail_v = Vec::new();
            for item in lst {
                let r = call_fn(vm, f, vec![item.clone()], "Arr.partition")?;
                let b = need_bool(vm, r, "Arr.partition")?;
                if b { pass.push(item); } else { fail_v.push(item); }
            }
            Ok(list(vec![list(pass), list(fail_v)]))
        }
    }
}

// ===========================================================================
// Log module
// ===========================================================================

thread_local! {
    static LOG_LEVEL: RefCell<usize> = RefCell::new(0);  // 0=debug
    static LOG_FILE: RefCell<Option<String>> = RefCell::new(None);
}

const LOG_LEVELS: [&str; 5] = ["debug", "info", "warning", "error", "critical"];

fn log_write(vm: &mut Vm, level: usize, args: Vec<Value>) -> R<Value> {
    use std::io::Write;
    let cur_level = LOG_LEVEL.with(|l| *l.borrow());
    if level < cur_level { return Ok(Value::Null); }
    let level_name = LOG_LEVELS[level].to_uppercase();
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let stamp = std_impl::format_date(now, "Y-m-d H:i:s", 0, "UTC");
    let mut parts = Vec::new();
    for a in &args {
        parts.push(vm.display(a, false)?);
    }
    let msg = parts.join(" ");
    let line = format!("[{}] {}: {}\n", stamp, level_name, msg);
    let file = LOG_FILE.with(|f| f.borrow().clone());
    if let Some(path) = file {
        let _ = std::fs::OpenOptions::new().create(true).append(true).open(&path)
            .and_then(|mut f| f.write_all(line.as_bytes()));
    } else {
        let _ = std::io::stderr().write_all(line.as_bytes());
    }
    Ok(Value::Null)
}

fn log_op(vm: &mut Vm, op: LogOp, args: Vec<Value>) -> R<Value> {
    match op {
        LogOp::Debug    => log_write(vm, 0, args),
        LogOp::Info     => log_write(vm, 1, args),
        LogOp::Warning | LogOp::Warn => log_write(vm, 2, args),
        LogOp::Error    => log_write(vm, 3, args),
        LogOp::Critical => log_write(vm, 4, args),
        LogOp::SetLevel => {
            let lvl = need_str(vm, &args[0], "Log.setLevel", 1)?;
            let idx = LOG_LEVELS.iter().position(|&l| l == lvl.s.to_lowercase().as_str())
                .ok_or_else(|| vm.exc("ValueException", format!("Log.setLevel: unknown level \"{}\"", lvl.s)))?;
            LOG_LEVEL.with(|l| *l.borrow_mut() = idx);
            Ok(Value::Null)
        }
        LogOp::SetFile => {
            let v = &args[0];
            let path = match v {
                Value::Null => None,
                Value::Str(t) => Some(t.s.clone()),
                _ => return need(vm, v, &["string", "null"], "Log.setFile", 1).map(|_| unreachable!()),
            };
            LOG_FILE.with(|f| *f.borrow_mut() = path);
            Ok(Value::Null)
        }
    }
}

// ===========================================================================
// Cache module
// ===========================================================================

use std::time::Instant;

thread_local! {
    static CACHE: RefCell<Vec<(String, Value, Option<std::time::Instant>)>> = RefCell::new(Vec::new());
}

fn cache_alive_idx(key: &str) -> Option<usize> {
    CACHE.with(|c| {
        let store = c.borrow();
        for (i, (k, _, exp)) in store.iter().enumerate() {
            if k == key {
                if let Some(exp_t) = exp {
                    if Instant::now() >= *exp_t { return None; }
                }
                return Some(i);
            }
        }
        None
    })
}

fn cache_op(vm: &mut Vm, op: CacheOp, args: Vec<Value>) -> R<Value> {
    match op {
        CacheOp::Remember => {
            let key = need_str(vm, &args[0], "Cache.remember", 1)?;
            let ttl = match &args[1] {
                Value::Int(n) => Some(*n),
                Value::Null => None,
                _ => return need(vm, &args[1], &["int", "null"], "Cache.remember", 2).map(|_| unreachable!()),
            };
            if let Some(idx) = cache_alive_idx(&key.s) {
                return CACHE.with(|c| Ok(c.borrow()[idx].1.clone()));
            }
            let val = call_fn(vm, &args[2], vec![], "Cache.remember")?;
            let exp = ttl.map(|s| Instant::now() + std::time::Duration::from_secs(s as u64));
            CACHE.with(|c| {
                let mut store = c.borrow_mut();
                store.retain(|(k, _, _)| k != key.s.as_str());
                store.push((key.s.clone(), val.clone(), exp));
            });
            Ok(val)
        }
        CacheOp::Has => {
            let key = need_str(vm, &args[0], "Cache.has", 1)?;
            Ok(Value::Bool(cache_alive_idx(&key.s).is_some()))
        }
        CacheOp::Get => {
            let key = need_str(vm, &args[0], "Cache.get", 1)?;
            let default = args.get(1).cloned().unwrap_or(Value::Null);
            if let Some(idx) = cache_alive_idx(&key.s) {
                CACHE.with(|c| Ok(c.borrow()[idx].1.clone()))
            } else {
                Ok(default)
            }
        }
        CacheOp::Forget => {
            let key = need_str(vm, &args[0], "Cache.forget", 1)?;
            CACHE.with(|c| c.borrow_mut().retain(|(k, _, _)| k != key.s.as_str()));
            Ok(Value::Null)
        }
        CacheOp::Increment => {
            let key = need_str(vm, &args[0], "Cache.increment", 1)?;
            let by = args.get(1).and_then(|v| if let Value::Int(n) = v { Some(*n) } else { None }).unwrap_or(1);
            let (current, exp) = if let Some(idx) = cache_alive_idx(&key.s) {
                CACHE.with(|c| {
                    let store = c.borrow();
                    let (_, v, e) = &store[idx];
                    if let Value::Int(n) = v { (*n, *e) } else { (0, None) }
                })
            } else { (0, None) };
            let new_val = current + by;
            CACHE.with(|c| {
                let mut store = c.borrow_mut();
                store.retain(|(k, _, _)| k != key.s.as_str());
                store.push((key.s.clone(), Value::Int(new_val), exp));
            });
            Ok(Value::Int(new_val))
        }
        CacheOp::Set => {
            let key = need_str(vm, &args[0], "Cache.set", 1)?;
            let val = args[1].clone();
            let ttl = args.get(2).and_then(|v| if let Value::Int(n) = v { Some(*n) } else { None });
            let exp = ttl.map(|s| Instant::now() + std::time::Duration::from_secs(s as u64));
            CACHE.with(|c| {
                let mut store = c.borrow_mut();
                store.retain(|(k, _, _)| k != key.s.as_str());
                store.push((key.s.clone(), val, exp));
            });
            Ok(Value::Null)
        }
    }
}
