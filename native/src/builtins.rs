//! Global functions, the Math and Str modules, methods of the built-in
//! types, casts and display (barq/builtins.py and barq/runtime.py).

use crate::code::*;
use crate::vm::{Flow, Vm, R};
use crate::value::*;
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
