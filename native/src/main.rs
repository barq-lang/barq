//! barq-native: the native Barq VM (Phase 6).
//!
//! Usage: barq-native <tree> [arguments for the program...]
//! `barq run --native` writes the checked syntax tree of a program to a file
//! (cached as .barq/cache/<file>.bqn) and starts this program on it.

mod ast;
mod builtins;
mod code;
mod compiler;
mod database;
mod json;
mod stdlib;
mod stdlib2;
mod stdlib3;
mod storage;
mod value;
mod view;
mod vm;

use std::io::Write;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 || args[1] == "--help" || args[1] == "-h" {
        eprintln!("usage: barq-native <tree> [args...]   (normally started by: barq run --native file.bq)");
        std::process::exit(2);
    }
    if args[1] == "--sizes" {
        println!("Value {} Op {} Flow {}", std::mem::size_of::<value::Value>(), std::mem::size_of::<code::Op>(), std::mem::size_of::<vm::Flow>());
        return;
    }
    if args[1] == "--bench" {
        bench();
        return;
    }
    if args[1] == "--version" {
        println!("barq-native {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let path = args[1].clone();
    // Deep Barq recursion needs a deep native stack.
    let child = std::thread::Builder::new()
        .stack_size(1 << 30)
        .spawn(move || run(&path))
        .expect("cannot start the VM thread");
    let code = child.join().unwrap_or(70);
    std::process::exit(code);
}

/// Usage: barq-native <tree> [program arguments...]
fn run(tree_path: &str) -> i32 {
    let mut text = match std::fs::read_to_string(tree_path) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("barq-native: cannot read {}: {}", tree_path, e);
            return 2;
        }
    };
    if text.starts_with('#') {
        // the cache header line written by barq/native.py
        let end = text.find('\n').map(|i| i + 1).unwrap_or(text.len());
        text.drain(..end);
    }
    let data = match json::parse(&text) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("barq-native: {}", e);
            return 2;
        }
    };
    drop(text);
    let fmt = data.get("format").int();
    if fmt < 1 || fmt > 4 {
        eprintln!("barq-native: this tree was written by a different Barq version (format {}); rebuild native/", fmt);
        return 2;
    }
    // FORMAT 3: pre-loaded config data
    if fmt >= 3 {
        let config_j = data.get("config");
        if !matches!(config_j, json::J::Null) {
            crate::stdlib2::init_config(config_j);
        }
    }
    let display = data.get("path").str().to_string();
    let file = data.get("file").str().to_string();
    let source = data.get("source").str().to_string();
    let argv: Vec<String> = std::env::args().skip(2).collect();
    // Parse module trees (format 2+): each is {file, display, source, program}.
    let modules_json = data.get("modules").arr().to_vec();
    let module_bodies: Vec<(Vec<ast::Stmt>, String, String)> = modules_json.iter().map(|m| {
        let body = ast::block(m.get("program").get("body"));
        let disp = m.get("display").str().to_string();
        let src = m.get("source").str().to_string();
        (body, disp, src)
    }).collect();
    let body = ast::block(data.get("program").get("body"));
    let tpl_src = view::load_bundle(data.get("templates"));
    drop(data);
    let module_data: Vec<compiler::ModuleData<'_>> = module_bodies.iter().map(|(b, d, _)| compiler::ModuleData { body: b, display: d }).collect();
    let color = std::env::var("BARQ_COLOR").map(|v| v == "1").unwrap_or(false);
    let mut program = match compiler::compile(&module_data, &body, &tpl_src, &file) {
        Ok(p) => p,
        Err(compiler::Fail::Compile(msg, pos)) => {
            let vm = vm::Vm::new(display, &source, argv, Vec::new());
            let (red, reset) = if color { ("\x1b[1;31m", "\x1b[0m") } else { ("", "") };
            eprintln!("{}CompileError:{} {}", red, reset, msg);
            eprintln!("{}", vm.format_location(pos.0, pos.1));
            return 1;
        }
        Err(compiler::Fail::Unsupported(what, pos)) => {
            let vm = vm::Vm::new(display, &source, argv, Vec::new());
            let (red, reset) = if color { ("\x1b[1;31m", "\x1b[0m") } else { ("", "") };
            eprintln!("{}NativeError:{} {} is not supported by the native VM yet — run it without --native", red, reset, what);
            if pos.0 > 0 {
                eprintln!("{}", vm.format_location(pos.0, pos.1));
            }
            return 1;
        }
    };
    if std::env::var("BARQ_NATIVE_DIS").is_ok() {
        for mm in &program.module_mains {
            dis(mm, 0);
        }
        dis(&program.main, 0);
        for (_, p) in &program.functions {
            dis(p, 0);
        }
        for c in &program.classes {
            for m in &c.members {
                if let compiler::MemberDef::Method { proto, .. } = m {
                    dis(proto, 0);
                }
            }
        }
    }
    // For multi-file programs, the module sources are separate; use the main source for location reporting.
    let _module_sources: Vec<&str> = module_bodies.iter().map(|(_, _, s)| s.as_str()).collect();
    view::install(std::mem::replace(&mut program.templates, view::TplBundle::empty()), &file);
    let mut vm = vm::Vm::new(display, &source, argv, program.global_names.clone());
    let result = vm.load(&program).and_then(|_| {
        for mm in &program.module_mains {
            vm.run_main(mm)?;
        }
        vm.run_main(&program.main)
    });
    vm.flush();
    match result {
        Ok(()) => 0,
        Err(vm::Flow::Exit(c)) => c,
        Err(vm::Flow::Throw(o)) => {
            let msg = vm.report_uncaught(&o);
            let _ = std::io::stderr().write_all(msg.as_bytes());
            1
        }
        Err(vm::Flow::Compile(msg, pos)) => {
            let (red, reset) = if color { ("\x1b[1;31m", "\x1b[0m") } else { ("", "") };
            eprintln!("{}CompileError:{} {}", red, reset, msg);
            eprintln!("{}", vm.format_location(pos.0, pos.1));
            1
        }
        Err(vm::Flow::Fatal(msg)) => {
            eprintln!("barq-native: internal error: {}", msg);
            70
        }
    }
}

fn dis(p: &code::Proto, depth: usize) {
    let pad = "  ".repeat(depth);
    eprintln!("{}== {} (locals {}, upvals {:?}, cells {:?})", pad, p.display, p.nlocals, p.upvals, p.cell_params);
    for (i, op) in p.code.iter().enumerate() {
        eprintln!("{}{:4} {:?}  @{}:{}", pad, i, op, p.pos[i].0, p.pos[i].1);
    }
    for q in &p.protos {
        dis(q, depth + 1);
    }
}

/// barq-native --bench: time the dispatch loop on a hand-written counting loop.
fn bench() {
    use code::*;
    use std::rc::Rc;
    let n = 10_000_000;
    let pad: usize = std::env::var("PAD").ok().and_then(|v| v.parse().ok()).unwrap_or(0);
    let kind = std::env::var("PADOP").unwrap_or_else(|_| "nullpop".into());
    let mut ops = vec![Op::Int(0), Op::Store(0), Op::Load(0), Op::Int(n), Op::Cmp(CmpOp::Lt), Op::JumpFalse(0, CondKind::Cond)];
    for _ in 0..pad {
        match kind.as_str() {
            "loadstore" => { ops.push(Op::Load(1)); ops.push(Op::Store(1)); }
            "jump" => { let at = ops.len() as u32; ops.push(Op::Jump(at + 1)); ops.push(Op::Jump(at + 2)); }
            _ => { ops.push(Op::Null); ops.push(Op::Pop); }
        }
    }
    ops.extend([Op::Load(0), Op::Int(1), Op::Bin(BinOp::Add), Op::Store(0), Op::Jump(2)]);
    let end = ops.len() as u32;
    ops[5] = Op::JumpFalse(end, CondKind::Cond);
    ops.extend([Op::Null, Op::Return]);
    let per = 9 + 2 * pad;
    let len = ops.len();
    let proto = Rc::new(Proto {
        name: "bench".into(), display: "bench".into(), params: vec![], simple: true, ret: None, ret_pos: (0, 0),
        code: ops, pos: vec![(0, 0); len], consts: vec![], strings: vec![], types: vec![], names: vec![], errors: vec![],
        protos: vec![], arg_pos: vec![], nlocals: 2, cell_params: vec![], upvals: vec![], owner: None, is_static: false, is_abstract: false,
        vis: value::Vis::Public, ctx: None, has_this: false, is_lambda: false,
    });
    let mut vm = vm::Vm::new("bench".into(), "", vec![], vec![]);
    let t = std::time::Instant::now();
    let _ = vm.run_expr(&proto);
    let dt = t.elapsed();
    println!("{} iterations x {} ops: {:?} = {:.2} ns/op, {:.1} ns/iteration", n, per, dt, dt.as_nanos() as f64 / (n as f64 * per as f64), dt.as_nanos() as f64 / n as f64);
}
