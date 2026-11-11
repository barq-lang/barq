//! Storage module: Disk (memory + local), Response, and the disk registry.

use crate::code::StorageOp;
use crate::value::*;
use crate::vm::{Vm, R};
use std::cell::RefCell;
use std::collections::HashMap;
use std::path::PathBuf;
use std::rc::Rc;

// ---------------------------------------------------------------------------
// types
// ---------------------------------------------------------------------------

pub struct MemEntry {
    pub data: Vec<u8>,
    pub mtime: i64,
}

pub enum DiskKind {
    Memory {
        files: HashMap<String, MemEntry>,
        dirs: std::collections::HashSet<String>,
    },
    Local {
        root: PathBuf,
    },
}

pub struct DiskInner {
    pub name: String,
    pub url_base: Option<String>,
    pub kind: DiskKind,
}

pub struct RspState {
    pub status: i64,
    pub headers: Vec<(String, String)>,
}

// ---------------------------------------------------------------------------
// global disk registry (thread-local, single-threaded VM)
// ---------------------------------------------------------------------------

thread_local! {
    static DISKS: RefCell<HashMap<String, Value>> = RefCell::new(HashMap::new());
}

fn get_registered(name: &str) -> Option<Value> {
    DISKS.with(|d| d.borrow().get(name).cloned())
}

fn register_disk(name: &str, disk: Value) {
    DISKS.with(|d| { d.borrow_mut().insert(name.to_string(), disk); });
}

fn registered_names() -> Vec<String> {
    let mut names: Vec<String> = DISKS.with(|d| d.borrow().keys().cloned().collect());
    names.sort();
    names
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

fn now_secs() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

/// Normalise a relative path, rejecting ".." that escapes the root.
/// Returns Ok(normalised) or Err(path that escapes root).
fn normalize(raw: &str) -> Result<String, ()> {
    let replaced = raw.replace('\\', "/");
    let mut parts: Vec<&str> = Vec::new();
    for seg in replaced.split('/') {
        match seg {
            "" | "." => {}
            ".." => { parts.pop().ok_or(())?; }
            s => parts.push(s),
        }
    }
    Ok(parts.join("/"))
}

/// Normalise and reject paths that escape the root or are empty.
fn clean(raw: &str, fname: &str) -> Result<String, String> {
    if raw.contains('\0') {
        return Err(format!("{fname}(): the path contains a NUL byte"));
    }
    normalize(raw).map_err(|_| format!("{fname}(): \"{raw}\" leaves the disk root"))
        .and_then(|p| if p.is_empty() { Err(format!("{fname}(): the path is empty")) } else { Ok(p) })
}

/// URL-encode a path (spaces → %20, etc.; "/" is not encoded).
fn url_encode_path(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            'A'..='Z' | 'a'..='z' | '0'..='9' | '-' | '_' | '.' | '~' | '/' => out.push(c),
            c => {
                let mut buf = [0u8; 4];
                let n = c.encode_utf8(&mut buf).len();
                for b in &buf[..n] {
                    out.push('%');
                    out.push_str(&format!("{:02X}", b));
                }
            }
        }
    }
    out
}

/// Guess MIME type from file extension.
pub fn mime_type(path: &str) -> &'static str {
    let ext = path.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
    match ext.as_str() {
        "txt" | "text" => "text/plain",
        "html" | "htm" => "text/html",
        "css" => "text/css",
        "js" | "mjs" => "application/javascript",
        "json" => "application/json",
        "xml" => "application/xml",
        "csv" => "text/csv",
        "md" => "text/markdown",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "svg" => "image/svg+xml",
        "webp" => "image/webp",
        "ico" => "image/x-icon",
        "bmp" => "image/bmp",
        "pdf" => "application/pdf",
        "zip" => "application/zip",
        "gz" | "gzip" => "application/gzip",
        "tar" => "application/x-tar",
        "mp3" => "audio/mpeg",
        "mp4" => "video/mp4",
        "webm" => "video/webm",
        "ogg" => "audio/ogg",
        "wav" => "audio/wav",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "bq" => "text/plain",
        _ => "application/octet-stream",
    }
}

/// All ancestor directories of a path, e.g. "a/b/c.txt" → {"a", "a/b"}.
fn parent_dirs(path: &str) -> Vec<String> {
    let parts: Vec<&str> = path.split('/').collect();
    (1..parts.len()).map(|k| parts[..k].join("/")).collect()
}

// ---------------------------------------------------------------------------
// Storage static methods
// ---------------------------------------------------------------------------

pub fn storage_op(vm: &mut Vm, op: StorageOp, args: Vec<Value>) -> R<Value> {
    match op {
        StorageOp::Fake => {
            // Storage.fake(name?) → registers an in-memory disk, returns it
            let name = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) if !matches!(v, Value::Null) => {
                    return vm.fail("TypeException", "Storage.fake() expects a string name or nothing");
                }
                _ => "local".to_string(),
            };
            // Preserve url_base from the existing disk or the default config
            let url_base = match get_registered(&name) {
                Some(Value::Disk(d)) => d.borrow().url_base.clone(),
                _ => {
                    if name == "public" { Some("/storage".to_string()) } else { None }
                }
            };
            let disk = Value::Disk(Rc::new(RefCell::new(DiskInner {
                name: name.clone(),
                url_base,
                kind: DiskKind::Memory { files: HashMap::new(), dirs: std::collections::HashSet::new() },
            })));
            register_disk(&name, disk.clone());
            Ok(disk)
        }
        StorageOp::Disk => {
            // Storage.disk(name?) → get disk from registry
            let name = match args.first() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(v) if !matches!(v, Value::Null) => {
                    return vm.fail("TypeException", "Storage.disk() expects a string name or nothing");
                }
                _ => "local".to_string(),
            };
            match get_registered(&name) {
                Some(d) => Ok(d),
                None => {
                    let known = registered_names().join(", ");
                    let hint = if known.is_empty() {
                        format!("unknown disk \"{name}\" — call Storage.fake(\"{name}\") first or add it to config/filesystems.bq")
                    } else {
                        format!("unknown disk \"{name}\" (disks: {known}) — add it to config/filesystems.bq under \"disks\"")
                    };
                    vm.fail("StorageException", hint)
                }
            }
        }
        StorageOp::Build => {
            // Storage.build(config) → creates a one-off disk
            let cfg = match args.into_iter().next() {
                Some(Value::Dict(d)) => d,
                _ => return vm.fail("TypeException", "Storage.build() needs a config dict"),
            };
            let cfg = cfg.borrow();
            let get_str = |key: &str| -> Option<String> {
                let hk = crate::value::hkey(&crate::value::s(key), false)?;
                match cfg.get(&hk) {
                    Some(Value::Str(s)) => Some(s.s.clone()),
                    _ => None,
                }
            };
            let driver = get_str("driver").unwrap_or_default();
            let name = get_str("name").unwrap_or_else(|| "build".to_string());
            let url_base = get_str("url");
            match driver.as_str() {
                "memory" => {
                    Ok(Value::Disk(Rc::new(RefCell::new(DiskInner {
                        name,
                        url_base,
                        kind: DiskKind::Memory { files: HashMap::new(), dirs: std::collections::HashSet::new() },
                    }))))
                }
                "local" => {
                    let root_str = match get_str("root") {
                        Some(r) => r,
                        None => return vm.fail("StorageException",
                            format!("disk \"{name}\" needs a \"root\" folder")),
                    };
                    let root = if std::path::Path::new(&root_str).is_absolute() {
                        PathBuf::from(&root_str)
                    } else {
                        std::env::current_dir().unwrap_or_default().join(&root_str)
                    };
                    Ok(Value::Disk(Rc::new(RefCell::new(DiskInner {
                        name,
                        url_base,
                        kind: DiskKind::Local { root },
                    }))))
                }
                other => vm.fail("StorageException",
                    format!("disk \"{name}\": unknown driver {:?} (use \"local\", \"memory\" or \"s3\")", other)),
            }
        }
        StorageOp::Put => {
            // Storage.put(path, contents) — shorthand for default disk
            let disk = get_registered("local")
                .ok_or_else(|| vm.exc("StorageException", "no default disk — call Storage.fake() first"))?;
            disk_method_inner(vm, &disk, "put", args)
        }
        StorageOp::Get => {
            let disk = get_registered("local")
                .ok_or_else(|| vm.exc("StorageException", "no default disk — call Storage.fake() first"))?;
            disk_method_inner(vm, &disk, "get", args)
        }
    }
}

// ---------------------------------------------------------------------------
// Disk instance method dispatch
// ---------------------------------------------------------------------------

pub fn disk_method(vm: &mut Vm, recv: &Value, name: &str, args: Vec<Value>) -> R<Value> {
    disk_method_inner(vm, recv, name, args)
}

fn disk_method_inner(vm: &mut Vm, recv: &Value, name: &str, args: Vec<Value>) -> R<Value> {
    let disk_rc = match recv {
        Value::Disk(d) => d.clone(),
        _ => unreachable!(),
    };

    match name {
        "name" => Ok(crate::value::s(disk_rc.borrow().name.clone())),

        "put" => {
            let path = need_disk_path(vm, &args, 1, "Storage.put")?;
            let contents = match args.get(1) {
                Some(Value::Str(s)) => s.s.as_bytes().to_vec(),
                Some(v) => return vm.fail("TypeException",
                    format!("Storage.put() needs the contents as a string, got {} — use Json.encode() for lists and dicts",
                        type_name(v))),
                None => return vm.fail("TypeException", "Storage.put() needs a path and contents"),
            };
            let mtime = now_secs();
            let mut d = disk_rc.borrow_mut();
            match &mut d.kind {
                DiskKind::Memory { files, .. } => {
                    files.insert(path, MemEntry { data: contents, mtime });
                }
                DiskKind::Local { root } => {
                    let full = root.join(&path);
                    if let Some(parent) = full.parent() {
                        std::fs::create_dir_all(parent).map_err(|e|
                            vm.exc("StorageException", format!("Storage.put(): {e}")))?;
                    }
                    std::fs::write(&full, &contents).map_err(|e|
                        vm.exc("StorageException", format!("Storage.put(): {e}")))?;
                }
            }
            Ok(Value::Bool(true))
        }

        "get" => {
            let path = need_disk_path(vm, &args, 1, "Storage.get")?;
            let d = disk_rc.borrow();
            match mem_read(&d, &path) {
                Some(bytes) => Ok(crate::value::s(String::from_utf8_lossy(&bytes).into_owned())),
                None => Ok(Value::Null),
            }
        }

        "exists" => {
            let path = need_disk_path(vm, &args, 1, "Storage.exists")?;
            Ok(Value::Bool(disk_exists(&disk_rc.borrow(), &path)))
        }

        "missing" => {
            let path = need_disk_path(vm, &args, 1, "Storage.missing")?;
            Ok(Value::Bool(!disk_exists(&disk_rc.borrow(), &path)))
        }

        "size" => {
            let path = need_disk_path(vm, &args, 1, "Storage.size")?;
            let d = disk_rc.borrow();
            match mem_read(&d, &path) {
                Some(bytes) => Ok(Value::Int(bytes.len() as i64)),
                None => vm.fail("StorageException",
                    format!("Storage.size(): {path} does not exist")),
            }
        }

        "mime" => {
            let path = need_disk_path(vm, &args, 1, "Storage.mime")?;
            Ok(crate::value::s(mime_type(&path)))
        }

        "append" => {
            let path = need_disk_path(vm, &args, 1, "Storage.append")?;
            let text = need_str_arg(vm, &args, 1, "Storage.append")?;
            let sep = match args.get(2) {
                Some(Value::Str(s)) => s.s.clone(),
                _ => "\n".to_string(),
            };
            let mtime = now_secs();
            let mut d = disk_rc.borrow_mut();
            match &mut d.kind {
                DiskKind::Memory { files, .. } => {
                    let new_data = match files.get(&path) {
                        Some(e) if !e.data.is_empty() => {
                            let mut v = e.data.clone();
                            v.extend_from_slice(sep.as_bytes());
                            v.extend_from_slice(text.as_bytes());
                            v
                        }
                        _ => text.into_bytes(),
                    };
                    files.insert(path, MemEntry { data: new_data, mtime });
                }
                DiskKind::Local { root } => {
                    let full = root.join(&path);
                    if let Some(parent) = full.parent() {
                        std::fs::create_dir_all(parent).map_err(|e|
                            vm.exc("StorageException", format!("Storage.append(): {e}")))?;
                    }
                    let existing = std::fs::read(&full).unwrap_or_default();
                    let new_data = if existing.is_empty() {
                        text.into_bytes()
                    } else {
                        let mut v = existing;
                        v.extend_from_slice(sep.as_bytes());
                        v.extend_from_slice(text.as_bytes());
                        v
                    };
                    std::fs::write(&full, &new_data).map_err(|e|
                        vm.exc("StorageException", format!("Storage.append(): {e}")))?;
                }
            }
            Ok(Value::Bool(true))
        }

        "prepend" => {
            let path = need_disk_path(vm, &args, 1, "Storage.prepend")?;
            let text = need_str_arg(vm, &args, 1, "Storage.prepend")?;
            let sep = match args.get(2) {
                Some(Value::Str(s)) => s.s.clone(),
                _ => "\n".to_string(),
            };
            let mtime = now_secs();
            let mut d = disk_rc.borrow_mut();
            match &mut d.kind {
                DiskKind::Memory { files, .. } => {
                    let new_data = match files.get(&path) {
                        Some(e) if !e.data.is_empty() => {
                            let mut v = text.into_bytes();
                            v.extend_from_slice(sep.as_bytes());
                            v.extend_from_slice(&e.data);
                            v
                        }
                        _ => text.into_bytes(),
                    };
                    files.insert(path, MemEntry { data: new_data, mtime });
                }
                DiskKind::Local { root } => {
                    let full = root.join(&path);
                    let existing = std::fs::read(&full).unwrap_or_default();
                    let new_data = if existing.is_empty() {
                        text.into_bytes()
                    } else {
                        let mut v = text.into_bytes();
                        v.extend_from_slice(sep.as_bytes());
                        v.extend_from_slice(&existing);
                        v
                    };
                    std::fs::write(&full, &new_data).map_err(|e|
                        vm.exc("StorageException", format!("Storage.prepend(): {e}")))?;
                }
            }
            Ok(Value::Bool(true))
        }

        "json" => {
            let path = need_disk_path(vm, &args, 1, "Storage.json")?;
            let d = disk_rc.borrow();
            match mem_read(&d, &path) {
                Some(bytes) => {
                    let s = String::from_utf8_lossy(&bytes);
                    match crate::json::parse(&s) {
                        Ok(j) => Ok(crate::builtins::json_decode_j(&j)),
                        Err(_) => Ok(Value::Null),
                    }
                }
                None => Ok(Value::Null),
            }
        }

        "copy" => {
            let from = need_disk_path(vm, &args, 1, "Storage.copy")?;
            let to = need_disk_path_arg(vm, &args, 1, "Storage.copy")?;
            let mtime = now_secs();
            let data = {
                let d = disk_rc.borrow();
                match mem_read(&d, &from) {
                    Some(b) => b,
                    None => return vm.fail("StorageException",
                        format!("Storage.copy(): {from} does not exist")),
                }
            };
            let mut d = disk_rc.borrow_mut();
            match &mut d.kind {
                DiskKind::Memory { files, .. } => {
                    files.insert(to, MemEntry { data, mtime });
                }
                DiskKind::Local { root } => {
                    let dst = root.join(&to);
                    if let Some(parent) = dst.parent() {
                        std::fs::create_dir_all(parent).map_err(|e|
                            vm.exc("StorageException", format!("Storage.copy(): {e}")))?;
                    }
                    std::fs::write(&dst, &data).map_err(|e|
                        vm.exc("StorageException", format!("Storage.copy(): {e}")))?;
                }
            }
            Ok(Value::Null)
        }

        "move" => {
            let from = need_disk_path(vm, &args, 1, "Storage.move")?;
            let to = need_disk_path_arg(vm, &args, 1, "Storage.move")?;
            let (data, mtime) = {
                let d = disk_rc.borrow();
                match mem_read(&d, &from) {
                    Some(b) => (b, now_secs()),
                    None => return vm.fail("StorageException",
                        format!("Storage.move(): {from} does not exist")),
                }
            };
            let mut d = disk_rc.borrow_mut();
            match &mut d.kind {
                DiskKind::Memory { files, dirs: _ } => {
                    files.remove(&from);
                    files.insert(to, MemEntry { data, mtime });
                }
                DiskKind::Local { root } => {
                    let src = root.join(&from);
                    let dst = root.join(&to);
                    if let Some(parent) = dst.parent() {
                        std::fs::create_dir_all(parent).map_err(|e|
                            vm.exc("StorageException", format!("Storage.move(): {e}")))?;
                    }
                    std::fs::rename(&src, &dst).map_err(|e|
                        vm.exc("StorageException", format!("Storage.move(): {e}")))?;
                }
            }
            Ok(Value::Null)
        }

        "makeDirectory" => {
            let path = need_disk_path(vm, &args, 1, "Storage.makeDirectory")?;
            let mut d = disk_rc.borrow_mut();
            match &mut d.kind {
                DiskKind::Memory { dirs, .. } => {
                    // Record all ancestors too
                    for ancestor in parent_dirs(&path) {
                        dirs.insert(ancestor);
                    }
                    dirs.insert(path);
                }
                DiskKind::Local { root } => {
                    std::fs::create_dir_all(root.join(&path)).map_err(|e|
                        vm.exc("StorageException", format!("Storage.makeDirectory(): {e}")))?;
                }
            }
            Ok(Value::Null)
        }

        "files" => {
            let dir = opt_disk_path(vm, &args, 0, "Storage.files")?;
            let recursive = match args.get(1) {
                Some(Value::Bool(b)) => *b,
                Some(v) if !matches!(v, Value::Null) => return vm.fail("TypeException",
                    format!("Storage.files() argument 2 must be bool, got {}", type_name(v))),
                _ => false,
            };
            Ok(listing(&disk_rc.borrow(), dir.as_deref(), false, recursive))
        }

        "allFiles" => {
            let dir = opt_disk_path(vm, &args, 0, "Storage.allFiles")?;
            Ok(listing(&disk_rc.borrow(), dir.as_deref(), false, true))
        }

        "directories" => {
            let dir = opt_disk_path(vm, &args, 0, "Storage.directories")?;
            Ok(listing(&disk_rc.borrow(), dir.as_deref(), true, false))
        }

        "allDirectories" => {
            let dir = opt_disk_path(vm, &args, 0, "Storage.allDirectories")?;
            Ok(listing(&disk_rc.borrow(), dir.as_deref(), true, true))
        }

        "lastModified" => {
            let path = need_disk_path(vm, &args, 1, "Storage.lastModified")?;
            let d = disk_rc.borrow();
            match &d.kind {
                DiskKind::Memory { files, .. } => match files.get(&path) {
                    Some(e) => Ok(Value::Int(e.mtime)),
                    None => vm.fail("StorageException",
                        format!("Storage.lastModified(): {path} does not exist")),
                },
                DiskKind::Local { root } => {
                    let full = root.join(&path);
                    let meta = std::fs::metadata(&full).map_err(|e|
                        vm.exc("StorageException", format!("Storage.lastModified(): {e}")))?;
                    let mtime = meta.modified().map_err(|e|
                        vm.exc("StorageException", format!("Storage.lastModified(): {e}")))?;
                    use std::time::UNIX_EPOCH;
                    let ts = mtime.duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
                    Ok(Value::Int(ts))
                }
            }
        }

        "delete" => {
            let first = args.first().cloned().unwrap_or(Value::Null);
            let paths = match &first {
                Value::Str(_) => vec![need_disk_path(vm, &args, 1, "Storage.delete")?],
                Value::List(l) => {
                    let mut out = Vec::new();
                    for item in l.borrow().iter() {
                        match item {
                            Value::Str(s) => match clean(&s.s, "Storage.delete") {
                                Ok(p) => out.push(p),
                                Err(e) => return vm.fail("StorageException", e),
                            },
                            v => return vm.fail("TypeException",
                                format!("Storage.delete() path list must contain strings, got {}", type_name(v))),
                        }
                    }
                    out
                }
                v if !matches!(v, Value::Null) => return vm.fail("TypeException",
                    format!("Storage.delete() needs a path or list of paths, got {}", type_name(v))),
                _ => return vm.fail("TypeException", "Storage.delete() needs a path"),
            };
            let mut any_deleted = false;
            let mut d = disk_rc.borrow_mut();
            for path in paths {
                match &mut d.kind {
                    DiskKind::Memory { files, .. } => {
                        if files.remove(&path).is_some() {
                            any_deleted = true;
                        }
                    }
                    DiskKind::Local { root } => {
                        let full = root.join(&path);
                        if full.is_file() {
                            if std::fs::remove_file(&full).is_ok() {
                                any_deleted = true;
                            }
                        }
                    }
                }
            }
            Ok(Value::Bool(any_deleted))
        }

        "deleteDirectory" => {
            let path = need_disk_path(vm, &args, 1, "Storage.deleteDirectory")?;
            let mut d = disk_rc.borrow_mut();
            match &mut d.kind {
                DiskKind::Memory { files, dirs } => {
                    let prefix = format!("{path}/");
                    let n_before = files.len() + dirs.len();
                    files.retain(|k, _| k != &path && !k.starts_with(&prefix));
                    dirs.retain(|k| k != &path && !k.starts_with(&prefix));
                    Ok(Value::Bool(files.len() + dirs.len() < n_before))
                }
                DiskKind::Local { root } => {
                    let full = root.join(&path);
                    if full.is_dir() {
                        std::fs::remove_dir_all(&full).map_err(|e|
                            vm.exc("StorageException", format!("Storage.deleteDirectory(): {e}")))?;
                        Ok(Value::Bool(true))
                    } else {
                        Ok(Value::Bool(false))
                    }
                }
            }
        }

        "assertExists" => {
            let path = need_disk_path(vm, &args, 1, "Storage.assertExists")?;
            if disk_exists(&disk_rc.borrow(), &path) {
                Ok(Value::Null)
            } else {
                let name = disk_rc.borrow().name.clone();
                vm.fail("AssertionException",
                    format!("expected {path} on disk \"{name}\""))
            }
        }

        "assertMissing" => {
            let path = need_disk_path(vm, &args, 1, "Storage.assertMissing")?;
            if !disk_exists(&disk_rc.borrow(), &path) {
                Ok(Value::Null)
            } else {
                let name = disk_rc.borrow().name.clone();
                vm.fail("AssertionException",
                    format!("did not expect {path} on disk \"{name}\""))
            }
        }

        "path" => {
            let d = disk_rc.borrow();
            match &d.kind {
                DiskKind::Memory { .. } => {
                    vm.fail("StorageException",
                        format!("Storage.path(): disk \"{}\" lives in memory and has no folder", d.name))
                }
                DiskKind::Local { root } => {
                    if args.is_empty() || matches!(args.first(), Some(Value::Null)) {
                        Ok(crate::value::s(root.to_string_lossy().into_owned()))
                    } else {
                        let rel = need_disk_path(vm, &args, 1, "Storage.path")?;
                        Ok(crate::value::s(root.join(&rel).to_string_lossy().into_owned()))
                    }
                }
            }
        }

        "url" => {
            let path = need_disk_path(vm, &args, 1, "Storage.url")?;
            let d = disk_rc.borrow();
            match &d.url_base {
                Some(base) => {
                    let encoded = url_encode_path(&path);
                    Ok(crate::value::s(format!("{}/{}", base.trim_end_matches('/'), encoded)))
                }
                None => vm.fail("StorageException",
                    format!("Storage.url(): disk \"{}\" has no public URL — store public files on the \"public\" disk, or give this disk a \"url\" in config/filesystems.bq",
                        d.name)),
            }
        }

        "download" => {
            let path = need_disk_path(vm, &args, 1, "Storage.download")?;
            let filename = match args.get(1) {
                Some(Value::Str(s)) => s.s.clone(),
                _ => path.rsplit('/').next().unwrap_or(&path).to_string(),
            };
            let ct = mime_type(&path);
            let cd = format!("attachment; filename=\"{filename}\"");
            Ok(Value::Response(Rc::new(RspState {
                status: 200,
                headers: vec![
                    ("Content-Disposition".to_string(), cd),
                    ("Content-Type".to_string(), ct.to_string()),
                ],
            })))
        }

        "response" => {
            let path = need_disk_path(vm, &args, 1, "Storage.response")?;
            let ct = mime_type(&path);
            Ok(Value::Response(Rc::new(RspState {
                status: 200,
                headers: vec![("Content-Type".to_string(), ct.to_string())],
            })))
        }

        _ => vm.fail("NameException", format!("Disk has no method '{name}'")),
    }
}

// ---------------------------------------------------------------------------
// Response instance method dispatch
// ---------------------------------------------------------------------------

pub fn response_get_attr(rsp: &Rc<RspState>, name: &str) -> Option<Value> {
    match name {
        "status" => Some(Value::Int(rsp.status)),
        _ => None,
    }
}

pub fn response_method(vm: &mut Vm, recv: &Value, name: &str, args: Vec<Value>) -> R<Value> {
    let rsp = match recv {
        Value::Response(r) => r.clone(),
        _ => unreachable!(),
    };
    match name {
        "header" => {
            let key = match args.first() {
                Some(Value::Str(s)) => s.s.to_ascii_lowercase(),
                _ => return vm.fail("TypeException", "Response.header() needs a string key"),
            };
            for (k, v) in &rsp.headers {
                if k.to_ascii_lowercase() == key {
                    return Ok(crate::value::s(v.clone()));
                }
            }
            Ok(Value::Null)
        }
        _ => vm.fail("NameException", format!("Response has no method '{name}'")),
    }
}

// ---------------------------------------------------------------------------
// private helpers
// ---------------------------------------------------------------------------

/// Parse arg[0] as a disk-safe path (clean).
fn need_disk_path(vm: &Vm, args: &[Value], _argn: usize, fname: &str) -> Result<String, crate::vm::Flow> {
    match args.first() {
        Some(Value::Str(s)) => clean(&s.s, fname).map_err(|e| vm.exc("StorageException", e)),
        Some(v) => Err(vm.exc("TypeException",
            format!("{fname}() argument 1 must be string, got {}", type_name(v)))),
        None => Err(vm.exc("TypeException", format!("{fname}() needs a path"))),
    }
}

/// Parse arg[1] as a disk-safe path.
fn need_disk_path_arg(vm: &Vm, args: &[Value], _argn: usize, fname: &str) -> Result<String, crate::vm::Flow> {
    match args.get(1) {
        Some(Value::Str(s)) => clean(&s.s, fname).map_err(|e| vm.exc("StorageException", e)),
        Some(v) => Err(vm.exc("TypeException",
            format!("{fname}() argument 2 must be string, got {}", type_name(v)))),
        None => Err(vm.exc("TypeException", format!("{fname}() needs two paths"))),
    }
}

/// Parse an optional first string arg as a disk-safe path.
fn opt_disk_path(vm: &Vm, args: &[Value], _argn: usize, fname: &str) -> Result<Option<String>, crate::vm::Flow> {
    match args.first() {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Str(s)) => Ok(Some(clean(&s.s, fname).map_err(|e| vm.exc("StorageException", e))?)),
        Some(v) => Err(vm.exc("TypeException",
            format!("{fname}() argument 1 must be string, got {}", type_name(v)))),
    }
}

/// Parse arg[1] as a string (for append/prepend content).
fn need_str_arg(vm: &Vm, args: &[Value], _argn: usize, fname: &str) -> Result<String, crate::vm::Flow> {
    match args.get(1) {
        Some(Value::Str(s)) => Ok(s.s.clone()),
        Some(v) => Err(vm.exc("TypeException",
            format!("{fname}() argument 2 must be string, got {}", type_name(v)))),
        None => Err(vm.exc("TypeException", format!("{fname}() needs contents"))),
    }
}

/// Read file bytes from a disk (memory or local).
fn mem_read(d: &DiskInner, path: &str) -> Option<Vec<u8>> {
    match &d.kind {
        DiskKind::Memory { files, .. } => files.get(path).map(|e| e.data.clone()),
        DiskKind::Local { root } => std::fs::read(root.join(path)).ok(),
    }
}

/// Check if a path exists (file or directory).
fn disk_exists(d: &DiskInner, path: &str) -> bool {
    match &d.kind {
        DiskKind::Memory { files, dirs } => {
            files.contains_key(path)
                || dirs.contains(path)
                || files.keys().any(|f| f.starts_with(&format!("{path}/")))
        }
        DiskKind::Local { root } => root.join(path).exists(),
    }
}

/// List files or directories under dir (None = root), optionally recursive.
fn listing(d: &DiskInner, dir: Option<&str>, list_dirs: bool, recursive: bool) -> Value {
    let prefix = dir.map(|d| format!("{d}/")).unwrap_or_default();

    let mut names: Vec<String> = match &d.kind {
        DiskKind::Memory { files, dirs } => {
            if list_dirs {
                let explicit: std::collections::HashSet<String> = dirs.clone();
                let implied: std::collections::HashSet<String> = files.keys()
                    .flat_map(|f| parent_dirs(f))
                    .collect();
                explicit.union(&implied).cloned().collect()
            } else {
                files.keys().cloned().collect()
            }
        }
        DiskKind::Local { root } => {
            let base = if prefix.is_empty() { root.clone() } else { root.join(prefix.trim_end_matches('/')) };
            if !base.is_dir() {
                return crate::value::list(Vec::new());
            }
            let mut out = Vec::new();
            collect_local(&base, root, list_dirs, recursive, &mut out);
            return crate::value::list(out.into_iter().map(crate::value::s).collect());
        }
    };

    names.retain(|x| {
        if !x.starts_with(&prefix) || x == dir.unwrap_or("") {
            return false;
        }
        let rest = &x[prefix.len()..];
        recursive || !rest.contains('/')
    });
    names.sort();
    crate::value::list(names.into_iter().map(crate::value::s).collect())
}

/// Recursively collect files or dirs from a local path, paths relative to root.
fn collect_local(base: &std::path::Path, root: &std::path::Path, list_dirs: bool, recursive: bool, out: &mut Vec<String>) {
    let entries = match std::fs::read_dir(base) {
        Ok(e) => e,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let rel = match path.strip_prefix(root) {
            Ok(r) => r.to_string_lossy().replace('\\', "/"),
            Err(_) => continue,
        };
        if path.is_dir() {
            if list_dirs {
                out.push(rel.clone());
            }
            if recursive {
                collect_local(&path, root, list_dirs, recursive, out);
            }
        } else if !list_dirs {
            out.push(rel);
        }
    }
    out.sort();
}
