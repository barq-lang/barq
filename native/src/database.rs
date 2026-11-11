//! Database: SQLite query builder, schema, and blueprint for the native VM.
//! Uses system libsqlite3 via FFI (no Cargo dependencies).

use std::cell::RefCell;
use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_int, c_void};
use std::rc::Rc;

use crate::value::{Value, BStr, OMap, hkey, s, list, type_name};
use crate::vm::{Vm, R};

// ---------------------------------------------------------------------------
// SQLite FFI
// ---------------------------------------------------------------------------

pub enum Sqlite3 {}
pub enum Sqlite3Stmt {}

const SQLITE_OK:   c_int = 0;
const SQLITE_ROW:  c_int = 100;
const SQLITE_DONE: c_int = 101;
const SQLITE_OPEN_READWRITE: c_int = 0x2;
const SQLITE_OPEN_CREATE:    c_int = 0x4;
const SQLITE_INTEGER: c_int = 1;
const SQLITE_FLOAT:   c_int = 2;
const SQLITE_TEXT:    c_int = 3;
// SQLITE_NULL = 5

#[link(name = "sqlite3")]
extern "C" {
    fn sqlite3_open_v2(filename: *const c_char, ppDb: *mut *mut Sqlite3, flags: c_int, zVfs: *const c_char) -> c_int;
    fn sqlite3_close(db: *mut Sqlite3) -> c_int;
    fn sqlite3_prepare_v2(db: *mut Sqlite3, zSql: *const c_char, nByte: c_int, ppStmt: *mut *mut Sqlite3Stmt, pzTail: *mut *const c_char) -> c_int;
    fn sqlite3_step(stmt: *mut Sqlite3Stmt) -> c_int;
    fn sqlite3_finalize(stmt: *mut Sqlite3Stmt) -> c_int;
    fn sqlite3_column_count(stmt: *mut Sqlite3Stmt) -> c_int;
    fn sqlite3_column_name(stmt: *mut Sqlite3Stmt, iCol: c_int) -> *const c_char;
    fn sqlite3_column_type(stmt: *mut Sqlite3Stmt, iCol: c_int) -> c_int;
    fn sqlite3_column_int64(stmt: *mut Sqlite3Stmt, iCol: c_int) -> i64;
    fn sqlite3_column_double(stmt: *mut Sqlite3Stmt, iCol: c_int) -> f64;
    fn sqlite3_column_text(stmt: *mut Sqlite3Stmt, iCol: c_int) -> *const u8;
    fn sqlite3_bind_null(stmt: *mut Sqlite3Stmt, idx: c_int) -> c_int;
    fn sqlite3_bind_int64(stmt: *mut Sqlite3Stmt, idx: c_int, val: i64) -> c_int;
    fn sqlite3_bind_double(stmt: *mut Sqlite3Stmt, idx: c_int, val: f64) -> c_int;
    fn sqlite3_bind_text(stmt: *mut Sqlite3Stmt, idx: c_int, val: *const c_char, n: c_int, cb: *mut c_void) -> c_int;
    fn sqlite3_last_insert_rowid(db: *mut Sqlite3) -> i64;
    fn sqlite3_changes(db: *mut Sqlite3) -> c_int;
    fn sqlite3_errmsg(db: *mut Sqlite3) -> *const c_char;
    fn sqlite3_exec(db: *mut Sqlite3, sql: *const c_char, cb: Option<unsafe extern "C" fn(*mut c_void, c_int, *mut *mut c_char, *mut *mut c_char) -> c_int>, arg: *mut c_void, errmsg: *mut *mut c_char) -> c_int;
    fn sqlite3_free(ptr: *mut c_void);
}

const SQLITE_TRANSIENT: *mut c_void = -1_isize as *mut c_void;

// ---------------------------------------------------------------------------
// Low-level SQLite helpers
// ---------------------------------------------------------------------------

unsafe fn sqlite_errmsg_str(db: *mut Sqlite3) -> String {
    CStr::from_ptr(sqlite3_errmsg(db)).to_string_lossy().into_owned()
}

#[derive(Clone)]
enum SqlParam {
    Null,
    Int(i64),
    Float(f64),
    Text(String),
}

fn to_sql_param(vm: &mut Vm, v: Value, ctx: &str) -> R<SqlParam> {
    match v {
        Value::Null => Ok(SqlParam::Null),
        Value::Bool(b) => Ok(SqlParam::Int(if b { 1 } else { 0 })),
        Value::Int(n) => Ok(SqlParam::Int(n)),
        Value::Float(f) => Ok(SqlParam::Float(f)),
        Value::Str(sv) => Ok(SqlParam::Text(sv.s.clone())),
        other => Err(vm.exc("DbException", format!(
            "cannot store {} in a database column ({}) — use Json.encode() for lists and dicts",
            type_name(&other), ctx)))
    }
}

unsafe fn bind_params_to(stmt: *mut Sqlite3Stmt, params: &[SqlParam]) {
    for (i, p) in params.iter().enumerate() {
        let idx = (i + 1) as c_int;
        match p {
            SqlParam::Null => { sqlite3_bind_null(stmt, idx); }
            SqlParam::Int(n) => { sqlite3_bind_int64(stmt, idx, *n); }
            SqlParam::Float(f) => { sqlite3_bind_double(stmt, idx, *f); }
            SqlParam::Text(sv) => {
                let cs = CString::new(sv.as_str()).unwrap_or_default();
                sqlite3_bind_text(stmt, idx, cs.as_ptr(), -1, SQLITE_TRANSIENT);
            }
        }
    }
}

fn exec_ddl(db: *mut Sqlite3, sql: &str) -> Result<(), String> {
    let cs = CString::new(sql).map_err(|e| format!("NUL byte in SQL: {}", e))?;
    unsafe {
        let mut errmsg: *mut c_char = std::ptr::null_mut();
        let rc = sqlite3_exec(db, cs.as_ptr(), None, std::ptr::null_mut(), &mut errmsg);
        if rc != SQLITE_OK {
            let msg = if !errmsg.is_null() {
                let sv = CStr::from_ptr(errmsg).to_string_lossy().into_owned();
                sqlite3_free(errmsg as *mut c_void);
                sv
            } else {
                sqlite_errmsg_str(db)
            };
            return Err(format!("{} — in: {}", msg, sql));
        }
        Ok(())
    }
}

fn query_rows_raw(db: *mut Sqlite3, sql: &str, params: &[SqlParam])
    -> Result<Vec<Vec<(String, Value)>>, String>
{
    let cs = CString::new(sql).map_err(|e| format!("NUL byte in SQL: {}", e))?;
    unsafe {
        let mut stmt: *mut Sqlite3Stmt = std::ptr::null_mut();
        let rc = sqlite3_prepare_v2(db, cs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
        if rc != SQLITE_OK {
            return Err(format!("{} — in: {}", sqlite_errmsg_str(db), sql));
        }
        bind_params_to(stmt, params);
        let ncols = sqlite3_column_count(stmt) as usize;
        let mut col_names: Vec<String> = Vec::with_capacity(ncols);
        for i in 0..ncols {
            let name = CStr::from_ptr(sqlite3_column_name(stmt, i as c_int))
                .to_string_lossy().into_owned();
            col_names.push(name);
        }
        let mut rows: Vec<Vec<(String, Value)>> = Vec::new();
        loop {
            let rc = sqlite3_step(stmt);
            if rc == SQLITE_DONE { break; }
            if rc != SQLITE_ROW {
                let msg = format!("{} — in: {}", sqlite_errmsg_str(db), sql);
                sqlite3_finalize(stmt);
                return Err(msg);
            }
            let mut row: Vec<(String, Value)> = Vec::with_capacity(ncols);
            for i in 0..ncols {
                let col_type = sqlite3_column_type(stmt, i as c_int);
                let val = match col_type {
                    SQLITE_INTEGER => Value::Int(sqlite3_column_int64(stmt, i as c_int)),
                    SQLITE_FLOAT => Value::Float(sqlite3_column_double(stmt, i as c_int)),
                    SQLITE_TEXT => {
                        let ptr = sqlite3_column_text(stmt, i as c_int);
                        let sv = if ptr.is_null() { String::new() }
                            else { CStr::from_ptr(ptr as *const c_char).to_string_lossy().into_owned() };
                        Value::Str(Rc::new(BStr::new(sv)))
                    }
                    _ => Value::Null,
                };
                row.push((col_names[i].clone(), val));
            }
            rows.push(row);
        }
        sqlite3_finalize(stmt);
        Ok(rows)
    }
}

// Execute DML and return (last_insert_rowid, changes)
fn exec_dml_raw(db: *mut Sqlite3, sql: &str, params: &[SqlParam])
    -> Result<(i64, i64), String>
{
    query_rows_raw(db, sql, params)?;
    unsafe {
        Ok((sqlite3_last_insert_rowid(db), sqlite3_changes(db) as i64))
    }
}

// ---------------------------------------------------------------------------
// Identifier validation and quoting
// ---------------------------------------------------------------------------

fn is_ident(s: &str) -> bool {
    let mut chars = s.chars();
    match chars.next() {
        None => false,
        Some(c) if !c.is_ascii_alphabetic() && c != '_' => false,
        _ => chars.all(|c| c.is_ascii_alphanumeric() || c == '_'),
    }
}

fn qid(vm: &mut Vm, name: &str) -> R<String> {
    qid_ctx(vm, name, "column")
}

fn qid_ctx(vm: &mut Vm, name: &str, what: &str) -> R<String> {
    // "expr AS alias"
    let lo = name.to_lowercase();
    if let Some(idx) = lo.find(" as ") {
        let left = name[..idx].trim();
        let alias = name[idx + 4..].trim();
        if is_ident(alias) {
            let lq = qid_ctx(vm, left, what)?;
            return Ok(format!("{} AS \"{}\"", lq, alias));
        }
    }
    if name == "*" { return Ok("*".into()); }
    let parts: Vec<&str> = name.split('.').collect();
    let mut out: Vec<String> = Vec::with_capacity(parts.len());
    for (k, p) in parts.iter().enumerate() {
        if *p == "*" && k == parts.len() - 1 && k > 0 {
            out.push("*".into());
        } else if is_ident(p) {
            out.push(format!("\"{}\"", p));
        } else {
            return Err(vm.exc("DbException",
                format!("invalid {} name \"{}\" (letters, digits and _ only)", what, name)));
        }
    }
    Ok(out.join("."))
}

fn check_op(vm: &mut Vm, op: &Value) -> R<String> {
    let sv = match op {
        Value::Str(s) => s.s.clone(),
        other => {
            let display = vm.display(other, true).unwrap_or_else(|_| "?".into());
            return Err(vm.exc("DbException", format!(
                "unknown operator {} (use !=, <, <=, <>, =, >, >=, like, not like)", display)));
        }
    };
    let lo = sv.to_lowercase();
    const VALID: &[&str] = &["=", "!=", "<>", "<", ">", "<=", ">=", "like", "not like"];
    if !VALID.contains(&lo.as_str()) {
        return Err(vm.exc("DbException", format!(
            "unknown operator \"{}\" (use !=, <, <=, <>, =, >, >=, like, not like)", sv)));
    }
    if lo == "like" || lo == "not like" { Ok(lo.to_uppercase()) } else { Ok(sv) }
}

// ---------------------------------------------------------------------------
// Data structures
// ---------------------------------------------------------------------------

pub struct DbState {
    pub driver: String,
    pub name: String,
    pub closed: bool,
    pub conn: *mut Sqlite3,
    pub in_transaction: bool,
}

impl Drop for DbState {
    fn drop(&mut self) {
        if !self.conn.is_null() {
            unsafe { sqlite3_close(self.conn); }
            self.conn = std::ptr::null_mut();
        }
    }
}

unsafe impl Send for DbState {}

struct Where {
    bool_op: &'static str,
    sql: String,
    params: Vec<SqlParam>,
}

struct Join {
    sql: String,
}

pub struct QbState {
    db: Rc<RefCell<DbState>>,
    table: String,
    selects: Vec<String>,
    wheres: Vec<Where>,
    joins: Vec<Join>,
    orders: Vec<String>,
    limit: Option<i64>,
    offset: Option<i64>,
}

pub struct SchemaState {
    pub db: Rc<RefCell<DbState>>,
}

pub struct BpColumn {
    name: String,
    sql_type: String,
    primary: bool,
    nullable: bool,
    default_sql: Option<String>,
    unique: bool,
    references: Option<(String, String)>,
    on_delete: Option<String>,
}

pub enum BpMode { Create, Alter }

pub struct BpState {
    pub mode: BpMode,
    columns: Vec<BpColumn>,
    pending: Option<usize>,
    rename_ops: Vec<(String, String)>,
}

// ---------------------------------------------------------------------------
// Value collection helpers
// ---------------------------------------------------------------------------

fn omap_insert_str(map: &mut OMap, k: &str, v: Value) {
    let key = s(k);
    if let Some(hk) = hkey(&key, false) {
        map.insert(hk, key, v);
    }
}

fn rows_to_value(rows: Vec<Vec<(String, Value)>>) -> Value {
    let items: Vec<Value> = rows.into_iter().map(|row| {
        let mut map = OMap::new(false);
        for (name, val) in row {
            omap_insert_str(&mut map, &name, val);
        }
        Value::Dict(Rc::new(RefCell::new(map)))
    }).collect();
    list(items)
}

// ---------------------------------------------------------------------------
// DB access helpers
// ---------------------------------------------------------------------------

fn get_conn(vm: &mut Vm, db: &Rc<RefCell<DbState>>) -> R<*mut Sqlite3> {
    let borrow = db.borrow();
    if borrow.closed || borrow.conn.is_null() {
        return Err(vm.exc("DbException", "the database connection is closed"));
    }
    Ok(borrow.conn)
}

fn db_exec_ddl(vm: &mut Vm, db: &Rc<RefCell<DbState>>, sql: &str) -> R<()> {
    let conn = get_conn(vm, db)?;
    exec_ddl(conn, sql).map_err(|e| vm.exc("DbException", e))
}

fn db_query_rows(vm: &mut Vm, db: &Rc<RefCell<DbState>>, sql: &str, params: &[SqlParam])
    -> R<Vec<Vec<(String, Value)>>>
{
    let conn = get_conn(vm, db)?;
    query_rows_raw(conn, sql, params).map_err(|e| vm.exc("DbException", e))
}

fn db_exec_dml(vm: &mut Vm, db: &Rc<RefCell<DbState>>, sql: &str, params: &[SqlParam])
    -> R<(i64, i64)>
{
    let conn = get_conn(vm, db)?;
    exec_dml_raw(conn, sql, params).map_err(|e| vm.exc("DbException", e))
}

fn require_str_arg(vm: &mut Vm, args: &[Value], idx: usize, ctx: &str) -> R<String> {
    match args.get(idx) {
        Some(Value::Str(s)) => Ok(s.s.clone()),
        Some(other) => Err(vm.exc("TypeException",
            format!("{} argument {} must be a string, got {}", ctx, idx+1, type_name(other)))),
        None => Err(vm.exc("TypeException",
            format!("{} requires argument {}", ctx, idx+1))),
    }
}

// ---------------------------------------------------------------------------
// WHERE clause building
// ---------------------------------------------------------------------------

fn build_where(wheres: &[Where]) -> (String, Vec<SqlParam>) {
    if wheres.is_empty() {
        return (String::new(), Vec::new());
    }
    let mut parts: Vec<String> = Vec::new();
    let mut params: Vec<SqlParam> = Vec::new();
    for (k, w) in wheres.iter().enumerate() {
        if k == 0 {
            parts.push(w.sql.clone());
        } else {
            parts.push(format!("{} {}", w.bool_op, w.sql));
        }
        params.extend(w.params.iter().cloned());
    }
    (format!(" WHERE {}", parts.join(" ")), params)
}

fn add_where_args(vm: &mut Vm, q: &mut QbState, bool_op: &'static str, args: Vec<Value>) -> R<()> {
    if args.is_empty() {
        return Err(vm.exc("TypeException", "where() needs at least one argument"));
    }
    match args[0].clone() {
        Value::Func(_) | Value::Native(_) | Value::Bound(_) | Value::BMethod(_) => {
            // Grouped sub-query
            let f = args.into_iter().next().unwrap();
            let sub_qb = Rc::new(RefCell::new(QbState {
                db: q.db.clone(),
                table: q.table.clone(),
                selects: Vec::new(),
                wheres: Vec::new(),
                joins: Vec::new(),
                orders: Vec::new(),
                limit: None,
                offset: None,
            }));
            let sub_val = Value::QueryBuilder(sub_qb.clone());
            vm.call_value(&f, vec![sub_val])?;
            let sub = sub_qb.borrow();
            if !sub.wheres.is_empty() {
                let (wsql, wparams) = build_where(&sub.wheres);
                let cond = wsql.trim_start_matches(" WHERE ").to_string();
                q.wheres.push(Where { bool_op, sql: format!("({})", cond), params: wparams });
            }
            Ok(())
        }
        Value::Dict(d) => {
            let pairs: Vec<(String, Value)> = d.borrow().iter().map(|(k, v)| {
                let ks = match k { Value::Str(sv) => sv.s.clone(), _ => String::new() };
                (ks, v.clone())
            }).collect();
            for (ks, v) in pairs {
                let col = qid(vm, &ks)?;
                let param = to_sql_param(vm, v, &ks)?;
                q.wheres.push(Where { bool_op: "AND", sql: format!("{} = ?", col), params: vec![param] });
            }
            Ok(())
        }
        Value::Str(_) => {
            let col_str = match args[0].clone() {
                Value::Str(s) => s.s.clone(),
                _ => unreachable!(),
            };
            let col = qid(vm, &col_str)?;
            let (op, value) = match args.len() {
                1 => return Err(vm.exc("TypeException", "where() expects (column, value) or (column, operator, value)")),
                2 => ("=".to_string(), args.into_iter().nth(1).unwrap()),
                _ => {
                    let op = check_op(vm, &args[1])?;
                    let v = args.into_iter().nth(2).unwrap();
                    (op, v)
                }
            };
            if matches!(value, Value::Null) {
                match op.as_str() {
                    "=" => q.wheres.push(Where { bool_op, sql: format!("{} IS NULL", col), params: vec![] }),
                    "!=" | "<>" => q.wheres.push(Where { bool_op, sql: format!("{} IS NOT NULL", col), params: vec![] }),
                    _ => return Err(vm.exc("DbException", format!("cannot compare with null using '{}'", op))),
                }
                return Ok(());
            }
            let param = to_sql_param(vm, value, &col_str)?;
            q.wheres.push(Where { bool_op, sql: format!("{} {} ?", col, op), params: vec![param] });
            Ok(())
        }
        other => Err(vm.exc("TypeException",
            format!("where() first argument must be a column name, dict, or callable, got {}", type_name(&other)))),
    }
}

// ---------------------------------------------------------------------------
// Blueprint
// ---------------------------------------------------------------------------

fn value_to_sql_literal(v: Value) -> String {
    match v {
        Value::Null => "NULL".into(),
        Value::Bool(b) => if b { "1".into() } else { "0".into() },
        Value::Int(n) => n.to_string(),
        Value::Float(f) => format!("{}", f),
        Value::Str(sv) => format!("'{}'", sv.s.replace('\'', "''")),
        _ => "NULL".into(),
    }
}

fn bp_col_sql(c: &BpColumn) -> String {
    let mut out = format!("\"{}\" {}", c.name, c.sql_type);
    if c.primary {
        out.push_str(" PRIMARY KEY AUTOINCREMENT");
    } else {
        if !c.nullable { out.push_str(" NOT NULL"); }
        if let Some(ref d) = c.default_sql {
            out.push_str(&format!(" DEFAULT {}", d));
        }
        if c.unique { out.push_str(" UNIQUE"); }
        if let Some((ref t, ref col)) = c.references {
            out.push_str(&format!(" REFERENCES \"{}\"(\"{}\")", t, col));
            if let Some(ref od) = c.on_delete {
                out.push_str(&format!(" ON DELETE {}", od.to_uppercase()));
            }
        }
    }
    out
}

pub fn bp_method(vm: &mut Vm, state: &Rc<RefCell<BpState>>, name: &str, args: Vec<Value>) -> R<Value> {
    let bpv = Value::Blueprint(state.clone());
    match name {
        "id" => {
            let col_name = match args.first() {
                Some(Value::Str(sv)) => sv.s.clone(),
                _ => "id".to_string(),
            };
            let mut bp = state.borrow_mut();
            bp.columns.push(BpColumn { name: col_name, sql_type: "INTEGER".into(),
                primary: true, nullable: false, default_sql: None, unique: false,
                references: None, on_delete: None });
            bp.pending = Some(bp.columns.len() - 1);
        }
        "string" | "text" | "longText" | "mediumText" | "char" | "tinyText" => {
            let col = require_str_arg(vm, &args, 0, name)?;
            let mut bp = state.borrow_mut();
            bp.columns.push(BpColumn { name: col, sql_type: "TEXT".into(),
                primary: false, nullable: false, default_sql: None, unique: false,
                references: None, on_delete: None });
            bp.pending = Some(bp.columns.len() - 1);
        }
        "int" | "integer" | "bigInt" | "bigInteger" | "unsignedBigInteger" | "unsignedInteger"
        | "smallInteger" | "tinyInteger" => {
            let col = require_str_arg(vm, &args, 0, name)?;
            let mut bp = state.borrow_mut();
            bp.columns.push(BpColumn { name: col, sql_type: "INTEGER".into(),
                primary: false, nullable: false, default_sql: None, unique: false,
                references: None, on_delete: None });
            bp.pending = Some(bp.columns.len() - 1);
        }
        "bool" | "boolean" => {
            let col = require_str_arg(vm, &args, 0, name)?;
            let mut bp = state.borrow_mut();
            bp.columns.push(BpColumn { name: col, sql_type: "INTEGER".into(),
                primary: false, nullable: false, default_sql: None, unique: false,
                references: None, on_delete: None });
            bp.pending = Some(bp.columns.len() - 1);
        }
        "float" | "double" | "decimal" => {
            let col = require_str_arg(vm, &args, 0, name)?;
            let mut bp = state.borrow_mut();
            bp.columns.push(BpColumn { name: col, sql_type: "REAL".into(),
                primary: false, nullable: false, default_sql: None, unique: false,
                references: None, on_delete: None });
            bp.pending = Some(bp.columns.len() - 1);
        }
        "timestamps" => {
            let mut bp = state.borrow_mut();
            for col_name in &["created_at", "updated_at"] {
                bp.columns.push(BpColumn { name: (*col_name).to_string(), sql_type: "TEXT".into(),
                    primary: false, nullable: true, default_sql: None, unique: false,
                    references: None, on_delete: None });
            }
            bp.pending = None;
        }
        "softDeletes" => {
            let mut bp = state.borrow_mut();
            bp.columns.push(BpColumn { name: "deleted_at".into(), sql_type: "TEXT".into(),
                primary: false, nullable: true, default_sql: None, unique: false,
                references: None, on_delete: None });
            bp.pending = Some(bp.columns.len() - 1);
        }
        "foreignId" => {
            let col = require_str_arg(vm, &args, 0, name)?;
            let mut bp = state.borrow_mut();
            bp.columns.push(BpColumn { name: col, sql_type: "INTEGER".into(),
                primary: false, nullable: false, default_sql: None, unique: false,
                references: None, on_delete: None });
            bp.pending = Some(bp.columns.len() - 1);
        }
        "morphs" => {
            let base = require_str_arg(vm, &args, 0, name)?;
            let mut bp = state.borrow_mut();
            bp.columns.push(BpColumn { name: format!("{}_type", base), sql_type: "TEXT".into(),
                primary: false, nullable: false, default_sql: None, unique: false,
                references: None, on_delete: None });
            bp.columns.push(BpColumn { name: format!("{}_id", base), sql_type: "INTEGER".into(),
                primary: false, nullable: false, default_sql: None, unique: false,
                references: None, on_delete: None });
            bp.pending = None;
        }
        "nullable" => {
            let mut bp = state.borrow_mut();
            if let Some(idx) = bp.pending { bp.columns[idx].nullable = true; }
        }
        "default" => {
            let val = args.into_iter().next().unwrap_or(Value::Null);
            let lit = value_to_sql_literal(val);
            let mut bp = state.borrow_mut();
            if let Some(idx) = bp.pending { bp.columns[idx].default_sql = Some(lit); }
        }
        "unique" => {
            let mut bp = state.borrow_mut();
            if let Some(idx) = bp.pending { bp.columns[idx].unique = true; }
        }
        "constrained" => {
            let mut bp = state.borrow_mut();
            if let Some(idx) = bp.pending {
                let col_name = bp.columns[idx].name.clone();
                let table = if col_name.ends_with("_id") {
                    format!("{}s", &col_name[..col_name.len() - 3])
                } else {
                    col_name
                };
                bp.columns[idx].references = Some((table, "id".into()));
            }
        }
        "onDelete" => {
            let action = require_str_arg(vm, &args, 0, "Blueprint.onDelete")?;
            let mut bp = state.borrow_mut();
            if let Some(idx) = bp.pending { bp.columns[idx].on_delete = Some(action); }
        }
        "renameColumn" => {
            let old = require_str_arg(vm, &args, 0, "Blueprint.renameColumn")?;
            let new = require_str_arg(vm, &args, 1, "Blueprint.renameColumn")?;
            state.borrow_mut().rename_ops.push((old, new));
        }
        // accepted silently
        "primary" | "index" | "dropColumn" | "rememberToken" | "uuid" | "json" | "after" | "first" => {}
        _ => return Err(vm.exc("NativeError",
            format!("Blueprint.{}() is not supported by the native VM yet", name))),
    }
    Ok(bpv)
}

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------

pub fn schema_method(vm: &mut Vm, state: &Rc<RefCell<SchemaState>>, name: &str, args: Vec<Value>) -> R<Value> {
    match name {
        "create" => {
            let table = require_str_arg(vm, &args, 0, "Schema.create")?;
            let callback = args.into_iter().nth(1).unwrap_or(Value::Null);
            let bp_state = Rc::new(RefCell::new(BpState {
                mode: BpMode::Create,
                columns: Vec::new(),
                pending: None,
                rename_ops: Vec::new(),
            }));
            vm.call_value(&callback, vec![Value::Blueprint(bp_state.clone())])?;
            let col_defs: Vec<String> = bp_state.borrow().columns.iter().map(bp_col_sql).collect();
            let sql = format!("CREATE TABLE \"{}\" ({})", table, col_defs.join(", "));
            db_exec_ddl(vm, &state.borrow().db, &sql)?;
            Ok(Value::Null)
        }
        "alter" => {
            let table = require_str_arg(vm, &args, 0, "Schema.alter")?;
            let callback = args.into_iter().nth(1).unwrap_or(Value::Null);
            let bp_state = Rc::new(RefCell::new(BpState {
                mode: BpMode::Alter,
                columns: Vec::new(),
                pending: None,
                rename_ops: Vec::new(),
            }));
            vm.call_value(&callback, vec![Value::Blueprint(bp_state.clone())])?;
            let db = state.borrow().db.clone();
            let (new_cols, renames): (Vec<BpColumn>, Vec<(String, String)>) = {
                let bp = bp_state.borrow();
                let cols: Vec<BpColumn> = bp.columns.iter().map(|c| BpColumn {
                    name: c.name.clone(), sql_type: c.sql_type.clone(), primary: c.primary,
                    nullable: c.nullable, default_sql: c.default_sql.clone(),
                    unique: c.unique, references: c.references.clone(), on_delete: c.on_delete.clone(),
                }).collect();
                let rn: Vec<(String, String)> = bp.rename_ops.clone();
                (cols, rn)
            };
            for col in &new_cols {
                let col_sql = bp_col_sql(col);
                db_exec_ddl(vm, &db, &format!("ALTER TABLE \"{}\" ADD COLUMN {}", table, col_sql))?;
            }
            for (old, new) in &renames {
                db_exec_ddl(vm, &db,
                    &format!("ALTER TABLE \"{}\" RENAME COLUMN \"{}\" TO \"{}\"", table, old, new))?;
            }
            Ok(Value::Null)
        }
        "drop" => {
            let table = require_str_arg(vm, &args, 0, "Schema.drop")?;
            db_exec_ddl(vm, &state.borrow().db, &format!("DROP TABLE \"{}\"", table))?;
            Ok(Value::Null)
        }
        "dropIfExists" => {
            let table = require_str_arg(vm, &args, 0, "Schema.dropIfExists")?;
            db_exec_ddl(vm, &state.borrow().db, &format!("DROP TABLE IF EXISTS \"{}\"", table))?;
            Ok(Value::Null)
        }
        "hasTable" => {
            let table = require_str_arg(vm, &args, 0, "Schema.hasTable")?;
            let db = state.borrow().db.clone();
            let rows = db_query_rows(vm, &db,
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                &[SqlParam::Text(table)])?;
            Ok(Value::Bool(!rows.is_empty()))
        }
        "hasColumn" => {
            let table = require_str_arg(vm, &args, 0, "Schema.hasColumn")?;
            let col = require_str_arg(vm, &args, 1, "Schema.hasColumn")?;
            let db = state.borrow().db.clone();
            let rows = db_query_rows(vm, &db,
                &format!("PRAGMA table_info(\"{}\")", table), &[])?;
            let found = rows.iter().any(|row| row.iter().any(|(k, v)|
                k == "name" && matches!(v, Value::Str(sv) if sv.s == col)));
            Ok(Value::Bool(found))
        }
        "columns" => {
            let table = require_str_arg(vm, &args, 0, "Schema.columns")?;
            let db = state.borrow().db.clone();
            let rows = db_query_rows(vm, &db,
                &format!("PRAGMA table_info(\"{}\")", table), &[])?;
            let names: Vec<Value> = rows.into_iter().map(|row| {
                row.into_iter().find(|(k, _)| k == "name")
                    .map(|(_, v)| v).unwrap_or(Value::Null)
            }).collect();
            Ok(list(names))
        }
        "rename" => {
            let from = require_str_arg(vm, &args, 0, "Schema.rename")?;
            let to = require_str_arg(vm, &args, 1, "Schema.rename")?;
            db_exec_ddl(vm, &state.borrow().db,
                &format!("ALTER TABLE \"{}\" RENAME TO \"{}\"", from, to))?;
            Ok(Value::Null)
        }
        _ => Err(vm.exc("NativeError",
            format!("Schema.{}() is not supported by the native VM yet", name))),
    }
}

// ---------------------------------------------------------------------------
// QueryBuilder
// ---------------------------------------------------------------------------

// Snapshot of QbState fields needed to build SQL (avoids holding a borrow during VM calls)
struct QbSnapshot {
    table: String,
    selects: Vec<String>,
    wheres_sql: String,
    wheres_params: Vec<SqlParam>,
    joins_sql: String,
    orders: Vec<String>,
    limit: Option<i64>,
    offset: Option<i64>,
    db: Rc<RefCell<DbState>>,
}

fn snapshot(q: &QbState) -> QbSnapshot {
    let (wsql, wparams) = build_where(&q.wheres);
    QbSnapshot {
        table: q.table.clone(),
        selects: q.selects.clone(),
        wheres_sql: wsql,
        wheres_params: wparams,
        joins_sql: q.joins.iter().map(|j| j.sql.clone()).collect(),
        orders: q.orders.clone(),
        limit: q.limit,
        offset: q.offset,
        db: q.db.clone(),
    }
}

fn snapshot_to_sql(sn: &QbSnapshot) -> (String, Vec<SqlParam>) {
    let cols = if sn.selects.is_empty() { "*".to_string() } else { sn.selects.join(", ") };
    let mut sql = format!("SELECT {} FROM \"{}\"{}{}",
        cols, sn.table, sn.joins_sql, sn.wheres_sql);
    let mut params = sn.wheres_params.clone();
    if !sn.orders.is_empty() {
        sql.push_str(&format!(" ORDER BY {}", sn.orders.join(", ")));
    }
    if let Some(lim) = sn.limit {
        sql.push_str(" LIMIT ?");
        params.push(SqlParam::Int(lim));
        if let Some(off) = sn.offset {
            sql.push_str(" OFFSET ?");
            params.push(SqlParam::Int(off));
        }
    }
    (sql, params)
}

pub fn qb_method(vm: &mut Vm, state: &Rc<RefCell<QbState>>, name: &str, args: Vec<Value>) -> R<Value> {
    let qbv = Value::QueryBuilder(state.clone());
    match name {
        "where" => {
            let mut q = state.borrow_mut();
            add_where_args(vm, &mut q, "AND", args)?;
            Ok(qbv)
        }
        "orWhere" => {
            let mut q = state.borrow_mut();
            add_where_args(vm, &mut q, "OR", args)?;
            Ok(qbv)
        }
        "whereNull" => {
            let col_str = require_str_arg(vm, &args, 0, "QueryBuilder.whereNull")?;
            let col = qid(vm, &col_str)?;
            state.borrow_mut().wheres.push(Where { bool_op: "AND",
                sql: format!("{} IS NULL", col), params: vec![] });
            Ok(qbv)
        }
        "whereNotNull" => {
            let col_str = require_str_arg(vm, &args, 0, "QueryBuilder.whereNotNull")?;
            let col = qid(vm, &col_str)?;
            state.borrow_mut().wheres.push(Where { bool_op: "AND",
                sql: format!("{} IS NOT NULL", col), params: vec![] });
            Ok(qbv)
        }
        "whereIn" | "whereNotIn" => {
            let col_str = require_str_arg(vm, &args, 0, name)?;
            let col = qid(vm, &col_str)?;
            let negate = name == "whereNotIn";
            let values = match args.into_iter().nth(1) {
                Some(Value::List(l)) => l.borrow().clone(),
                Some(other) => return Err(vm.exc("TypeException",
                    format!("{} values must be a list, got {}", name, type_name(&other)))),
                None => return Err(vm.exc("TypeException", format!("{} requires 2 arguments", name))),
            };
            if values.is_empty() {
                state.borrow_mut().wheres.push(Where { bool_op: "AND",
                    sql: if negate { "1 = 1".into() } else { "0 = 1".into() }, params: vec![] });
            } else {
                let marks = vec!["?"; values.len()].join(", ");
                let mut params = Vec::new();
                for v in values {
                    params.push(to_sql_param(vm, v, "value")?);
                }
                state.borrow_mut().wheres.push(Where { bool_op: "AND",
                    sql: format!("{} {}IN ({})", col, if negate { "NOT " } else { "" }, marks),
                    params });
            }
            Ok(qbv)
        }
        "whereBetween" => {
            let col_str = require_str_arg(vm, &args, 0, "QueryBuilder.whereBetween")?;
            let col = qid(vm, &col_str)?;
            let pair = match args.into_iter().nth(1) {
                Some(Value::List(l)) => l.borrow().clone(),
                _ => return Err(vm.exc("TypeException",
                    "whereBetween() needs a list of 2 values")),
            };
            if pair.len() != 2 {
                return Err(vm.exc("ValueException",
                    "QueryBuilder.whereBetween() needs a list of 2 values, e.g. #[18, 65]"));
            }
            let p0 = to_sql_param(vm, pair[0].clone(), "value")?;
            let p1 = to_sql_param(vm, pair[1].clone(), "value")?;
            state.borrow_mut().wheres.push(Where { bool_op: "AND",
                sql: format!("{} BETWEEN ? AND ?", col), params: vec![p0, p1] });
            Ok(qbv)
        }
        "select" => {
            let cols: R<Vec<String>> = match args.into_iter().next() {
                Some(Value::List(l)) => l.borrow().iter().map(|v| match v {
                    Value::Str(sv) => qid(vm, &sv.s),
                    other => Err(vm.exc("TypeException",
                        format!("select() column must be string, got {}", type_name(other)))),
                }).collect(),
                Some(Value::Str(sv)) => Ok(vec![qid(vm, &sv.s)?]),
                _ => return Err(vm.exc("TypeException", "select() needs a list of column names")),
            };
            state.borrow_mut().selects = cols?;
            Ok(qbv)
        }
        "orderBy" => {
            let col_str = require_str_arg(vm, &args, 0, "QueryBuilder.orderBy")?;
            let col = qid(vm, &col_str)?;
            let dir = match args.into_iter().nth(1) {
                Some(Value::Str(sv)) => if sv.s.to_uppercase() == "DESC" { "DESC" } else { "ASC" },
                _ => "ASC",
            };
            state.borrow_mut().orders.push(format!("{} {}", col, dir));
            Ok(qbv)
        }
        "orderByDesc" => {
            let col_str = require_str_arg(vm, &args, 0, "QueryBuilder.orderByDesc")?;
            let col = qid(vm, &col_str)?;
            state.borrow_mut().orders.push(format!("{} DESC", col));
            Ok(qbv)
        }
        "limit" => {
            let n = match args.first() {
                Some(Value::Int(n)) => *n,
                _ => return Err(vm.exc("TypeException", "limit() needs an int")),
            };
            state.borrow_mut().limit = Some(n);
            Ok(qbv)
        }
        "offset" => {
            let n = match args.first() {
                Some(Value::Int(n)) => *n,
                _ => return Err(vm.exc("TypeException", "offset() needs an int")),
            };
            state.borrow_mut().offset = Some(n);
            Ok(qbv)
        }
        "join" | "leftJoin" => {
            let kind = if name == "join" { "INNER" } else { "LEFT" };
            let table = require_str_arg(vm, &args, 0, name)?;
            let table_q = qid_ctx(vm, &table, "table")?;
            let (first_s, op_s, second_s) = if args.len() >= 4 {
                let f = require_str_arg(vm, &args, 1, name)?;
                let op = check_op(vm, &args[2])?;
                let s2 = require_str_arg(vm, &args, 3, name)?;
                (f, op, s2)
            } else {
                let f = require_str_arg(vm, &args, 1, name)?;
                let s2 = require_str_arg(vm, &args, 2, name)?;
                (f, "=".into(), s2)
            };
            let fq = qid(vm, &first_s)?;
            let sq = qid(vm, &second_s)?;
            state.borrow_mut().joins.push(Join {
                sql: format!(" {} JOIN {} ON {} {} {}", kind, table_q, fq, op_s, sq)
            });
            Ok(qbv)
        }
        // Data retrieval
        "get" => {
            let sn = snapshot(&state.borrow());
            let (sql, params) = snapshot_to_sql(&sn);
            let rows = db_query_rows(vm, &sn.db, &sql, &params)?;
            Ok(rows_to_value(rows))
        }
        "first" => {
            let sn = {
                let q = state.borrow();
                let mut sn = snapshot(&q);
                if sn.limit.is_none() {
                    sn.limit = Some(1);
                }
                sn
            };
            let (sql, params) = snapshot_to_sql(&sn);
            let rows = db_query_rows(vm, &sn.db, &sql, &params)?;
            Ok(match rows.into_iter().next() {
                None => Value::Null,
                Some(row) => {
                    let mut map = OMap::new(false);
                    for (name, val) in row { omap_insert_str(&mut map, &name, val); }
                    Value::Dict(Rc::new(RefCell::new(map)))
                }
            })
        }
        "find" => {
            let id = match args.first() {
                Some(Value::Int(n)) => *n,
                _ => return Err(vm.exc("TypeException", "find() needs an int id")),
            };
            let (table, selects, db) = {
                let q = state.borrow();
                (q.table.clone(), q.selects.clone(), q.db.clone())
            };
            let cols = if selects.is_empty() { "*".to_string() } else { selects.join(", ") };
            let sql = format!("SELECT {} FROM \"{}\" WHERE \"id\" = ? LIMIT 1", cols, table);
            let rows = db_query_rows(vm, &db, &sql, &[SqlParam::Int(id)])?;
            Ok(match rows.into_iter().next() {
                None => Value::Null,
                Some(row) => {
                    let mut map = OMap::new(false);
                    for (name, val) in row { omap_insert_str(&mut map, &name, val); }
                    Value::Dict(Rc::new(RefCell::new(map)))
                }
            })
        }
        "value" => {
            let col_str = require_str_arg(vm, &args, 0, "QueryBuilder.value")?;
            let col = qid(vm, &col_str)?;
            let (table, joins_sql, wheres_sql, wheres_params, db) = {
                let q = state.borrow();
                let (wsql, wparams) = build_where(&q.wheres);
                let jsql: String = q.joins.iter().map(|j| j.sql.clone()).collect();
                (q.table.clone(), jsql, wsql, wparams, q.db.clone())
            };
            let sql = format!("SELECT {} FROM \"{}\"{}{}  LIMIT 1", col, table, joins_sql, wheres_sql);
            let rows = db_query_rows(vm, &db, &sql, &wheres_params)?;
            Ok(rows.into_iter().next()
                .and_then(|row| row.into_iter().next())
                .map(|(_, v)| v).unwrap_or(Value::Null))
        }
        "pluck" => {
            let col_str = require_str_arg(vm, &args, 0, "QueryBuilder.pluck")?;
            let col = qid(vm, &col_str)?;
            let key_col_opt = match args.into_iter().nth(1) {
                Some(Value::Str(sv)) => Some(qid(vm, &sv.s)?),
                _ => None,
            };
            let sn = snapshot(&state.borrow());
            let select_cols = match &key_col_opt {
                Some(kc) => format!("{}, {}", col, kc),
                None => col.clone(),
            };
            let mut sql = format!("SELECT {} FROM \"{}\"{}{}",
                select_cols, sn.table, sn.joins_sql, sn.wheres_sql);
            let mut params = sn.wheres_params.clone();
            if !sn.orders.is_empty() {
                sql.push_str(&format!(" ORDER BY {}", sn.orders.join(", ")));
            }
            if let Some(lim) = sn.limit {
                sql.push_str(" LIMIT ?"); params.push(SqlParam::Int(lim));
                if let Some(off) = sn.offset {
                    sql.push_str(" OFFSET ?"); params.push(SqlParam::Int(off));
                }
            }
            let rows = db_query_rows(vm, &sn.db, &sql, &params)?;
            if key_col_opt.is_some() {
                let mut map = OMap::new(false);
                for row in rows {
                    let mut it = row.into_iter();
                    let val = it.next().map(|(_, v)| v).unwrap_or(Value::Null);
                    let key = it.next().map(|(_, v)| v).unwrap_or(Value::Null);
                    if let Some(hk) = hkey(&key, false) {
                        map.insert(hk, key, val);
                    }
                }
                Ok(Value::Dict(Rc::new(RefCell::new(map))))
            } else {
                let items: Vec<Value> = rows.into_iter()
                    .map(|row| row.into_iter().next().map(|(_, v)| v).unwrap_or(Value::Null))
                    .collect();
                Ok(list(items))
            }
        }
        "count" => {
            let (table, joins_sql, wheres_sql, wheres_params, db) = {
                let q = state.borrow();
                let (wsql, wparams) = build_where(&q.wheres);
                let jsql: String = q.joins.iter().map(|j| j.sql.clone()).collect();
                (q.table.clone(), jsql, wsql, wparams, q.db.clone())
            };
            let sql = format!("SELECT COUNT(*) FROM \"{}\"{}{}",  table, joins_sql, wheres_sql);
            let rows = db_query_rows(vm, &db, &sql, &wheres_params)?;
            Ok(rows.into_iter().next()
                .and_then(|r| r.into_iter().next()).map(|(_, v)| v)
                .unwrap_or(Value::Int(0)))
        }
        "sum" | "avg" | "max" | "min" => {
            let col_str = require_str_arg(vm, &args, 0, name)?;
            let col = qid(vm, &col_str)?;
            let fn_up = name.to_uppercase();
            let (table, joins_sql, wheres_sql, wheres_params, db) = {
                let q = state.borrow();
                let (wsql, wparams) = build_where(&q.wheres);
                let jsql: String = q.joins.iter().map(|j| j.sql.clone()).collect();
                (q.table.clone(), jsql, wsql, wparams, q.db.clone())
            };
            let sql = format!("SELECT {}({}) FROM \"{}\"{}{}",
                fn_up, col, table, joins_sql, wheres_sql);
            let rows = db_query_rows(vm, &db, &sql, &wheres_params)?;
            Ok(rows.into_iter().next()
                .and_then(|r| r.into_iter().next()).map(|(_, v)| v)
                .unwrap_or(Value::Null))
        }
        "exists" => {
            let (table, joins_sql, wheres_sql, wheres_params, db) = {
                let q = state.borrow();
                let (wsql, wparams) = build_where(&q.wheres);
                let jsql: String = q.joins.iter().map(|j| j.sql.clone()).collect();
                (q.table.clone(), jsql, wsql, wparams, q.db.clone())
            };
            let sql = format!("SELECT EXISTS(SELECT 1 FROM \"{}\"{}{})", table, joins_sql, wheres_sql);
            let rows = db_query_rows(vm, &db, &sql, &wheres_params)?;
            let exists = rows.into_iter().next()
                .and_then(|r| r.into_iter().next())
                .map(|(_, v)| matches!(v, Value::Int(n) if n != 0))
                .unwrap_or(false);
            Ok(Value::Bool(exists))
        }
        "paginate" => {
            let page = match args.first() {
                Some(Value::Int(n)) => *n,
                _ => return Err(vm.exc("TypeException", "paginate() needs page as first argument")),
            };
            let per = match args.get(1) {
                Some(Value::Int(n)) => *n,
                _ => 15,
            };
            if page < 1 || per < 1 {
                return Err(vm.exc("ValueException",
                    "QueryBuilder.paginate() needs page >= 1 and perPage >= 1"));
            }
            let (table, wheres_sql, wheres_params, joins_sql, orders, selects, db) = {
                let q = state.borrow();
                let (wsql, wparams) = build_where(&q.wheres);
                (q.table.clone(), wsql, wparams,
                 q.joins.iter().map(|j| j.sql.clone()).collect::<String>(),
                 q.orders.clone(), q.selects.clone(), q.db.clone())
            };
            // Count total
            let count_sql = format!("SELECT COUNT(*) FROM \"{}\"{}{}",
                table, joins_sql, wheres_sql);
            let total_rows = db_query_rows(vm, &db, &count_sql, &wheres_params)?;
            let total: i64 = total_rows.into_iter().next()
                .and_then(|r| r.into_iter().next())
                .map(|(_, v)| match v { Value::Int(n) => n, _ => 0 })
                .unwrap_or(0);
            let last_page = std::cmp::max(1, (total + per - 1) / per);
            let offset = (page - 1) * per;
            // Build data SQL
            let cols = if selects.is_empty() { "*".to_string() } else { selects.join(", ") };
            let mut data_sql = format!("SELECT {} FROM \"{}\"{}{}",
                cols, table, joins_sql, wheres_sql);
            let mut data_params = wheres_params;
            if !orders.is_empty() {
                data_sql.push_str(&format!(" ORDER BY {}", orders.join(", ")));
            }
            data_sql.push_str(" LIMIT ?"); data_params.push(SqlParam::Int(per));
            data_sql.push_str(" OFFSET ?"); data_params.push(SqlParam::Int(offset));
            let data_rows = db_query_rows(vm, &db, &data_sql, &data_params)?;
            let data = rows_to_value(data_rows);
            let mut map = OMap::new(false);
            omap_insert_str(&mut map, "data", data);
            omap_insert_str(&mut map, "total", Value::Int(total));
            omap_insert_str(&mut map, "page", Value::Int(page));
            omap_insert_str(&mut map, "perPage", Value::Int(per));
            omap_insert_str(&mut map, "lastPage", Value::Int(last_page));
            Ok(Value::Dict(Rc::new(RefCell::new(map))))
        }
        "toSql" => {
            let (sql, _) = snapshot_to_sql(&snapshot(&state.borrow()));
            Ok(s(sql))
        }
        "insert" => {
            let entries = match args.into_iter().next() {
                Some(Value::Dict(d)) => {
                    d.borrow().iter().map(|(k, v)| {
                        let ks = match k { Value::Str(sv) => sv.s.clone(), _ => String::new() };
                        (ks, v.clone())
                    }).collect::<Vec<_>>()
                }
                _ => return Err(vm.exc("TypeException", "insert() needs a dict")),
            };
            let (table, db) = {
                let q = state.borrow();
                (q.table.clone(), q.db.clone())
            };
            let cols: R<Vec<String>> = entries.iter().map(|(k, _)| qid(vm, k)).collect();
            let cols = cols?;
            let marks = vec!["?"; entries.len()].join(", ");
            let sql = format!("INSERT INTO \"{}\" ({}) VALUES ({})", table, cols.join(", "), marks);
            let mut params = Vec::new();
            for (k, v) in &entries {
                params.push(to_sql_param(vm, v.clone(), &format!("column {}", k))?);
            }
            db_exec_dml(vm, &db, &sql, &params)?;
            Ok(Value::Null)
        }
        "insertGetId" => {
            let entries = match args.into_iter().next() {
                Some(Value::Dict(d)) => {
                    d.borrow().iter().map(|(k, v)| {
                        let ks = match k { Value::Str(sv) => sv.s.clone(), _ => String::new() };
                        (ks, v.clone())
                    }).collect::<Vec<_>>()
                }
                _ => return Err(vm.exc("TypeException", "insertGetId() needs a dict")),
            };
            let (table, db) = {
                let q = state.borrow();
                (q.table.clone(), q.db.clone())
            };
            let cols: R<Vec<String>> = entries.iter().map(|(k, _)| qid(vm, k)).collect();
            let cols = cols?;
            let marks = vec!["?"; entries.len()].join(", ");
            let sql = format!("INSERT INTO \"{}\" ({}) VALUES ({})", table, cols.join(", "), marks);
            let mut params = Vec::new();
            for (k, v) in &entries {
                params.push(to_sql_param(vm, v.clone(), &format!("column {}", k))?);
            }
            let (last_id, _) = db_exec_dml(vm, &db, &sql, &params)?;
            Ok(Value::Int(last_id))
        }
        "insertMany" => {
            let rows_list = match args.into_iter().next() {
                Some(Value::List(l)) => l.borrow().clone(),
                _ => return Err(vm.exc("TypeException", "insertMany() needs a list of dicts")),
            };
            if rows_list.is_empty() {
                return Ok(Value::Int(0));
            }
            // Get keys from first row
            let keys: Vec<String> = match &rows_list[0] {
                Value::Dict(d) => d.borrow().iter().map(|(k, _)| {
                    match k { Value::Str(sv) => sv.s.clone(), _ => String::new() }
                }).collect(),
                _ => return Err(vm.exc("TypeException", "insertMany() needs a list of dicts")),
            };
            let (table, db) = { let q = state.borrow(); (q.table.clone(), q.db.clone()) };
            let cols: R<Vec<String>> = keys.iter().map(|k| qid(vm, k)).collect();
            let cols = cols?;
            let marks = vec!["?"; keys.len()].join(", ");
            let sql = format!("INSERT INTO \"{}\" ({}) VALUES ({})", table, cols.join(", "), marks);
            let mut count = 0i64;
            for row_v in &rows_list {
                let row_map = match row_v {
                    Value::Dict(d) => d.borrow().clone(),
                    _ => return Err(vm.exc("TypeException", "insertMany() needs a list of dicts")),
                };
                let mut params = Vec::new();
                for k in &keys {
                    let v = row_map.iter()
                        .find(|(rk, _)| matches!(rk, Value::Str(sv) if &sv.s == k))
                        .map(|(_, v)| v.clone()).unwrap_or(Value::Null);
                    params.push(to_sql_param(vm, v, &format!("column {}", k))?);
                }
                db_exec_dml(vm, &db, &sql, &params)?;
                count += 1;
            }
            Ok(Value::Int(count))
        }
        "update" => {
            let entries = match args.into_iter().next() {
                Some(Value::Dict(d)) => {
                    d.borrow().iter().map(|(k, v)| {
                        let ks = match k { Value::Str(sv) => sv.s.clone(), _ => String::new() };
                        (ks, v.clone())
                    }).collect::<Vec<_>>()
                }
                _ => return Err(vm.exc("TypeException", "update() needs a dict")),
            };
            if entries.is_empty() { return Ok(Value::Int(0)); }
            let (table, wheres_sql, wheres_params, db) = {
                let q = state.borrow();
                let (wsql, wparams) = build_where(&q.wheres);
                (q.table.clone(), wsql, wparams, q.db.clone())
            };
            let sets: R<Vec<String>> = entries.iter().map(|(k, _)| {
                qid(vm, k).map(|q| format!("{} = ?", q))
            }).collect();
            let sets = sets?;
            let sql = format!("UPDATE \"{}\" SET {}{}", table, sets.join(", "), wheres_sql);
            let mut params = Vec::new();
            for (k, v) in &entries {
                params.push(to_sql_param(vm, v.clone(), &format!("column {}", k))?);
            }
            params.extend(wheres_params);
            let (_, changes) = db_exec_dml(vm, &db, &sql, &params)?;
            Ok(Value::Int(changes))
        }
        "delete" => {
            let (table, wheres_sql, wheres_params, db) = {
                let q = state.borrow();
                let (wsql, wparams) = build_where(&q.wheres);
                (q.table.clone(), wsql, wparams, q.db.clone())
            };
            let sql = format!("DELETE FROM \"{}\"{}",  table, wheres_sql);
            let (_, changes) = db_exec_dml(vm, &db, &sql, &wheres_params)?;
            Ok(Value::Int(changes))
        }
        "increment" | "decrement" => {
            let col_str = require_str_arg(vm, &args, 0, name)?;
            let col = qid(vm, &col_str)?;
            let amount: i64 = match args.get(1) {
                Some(Value::Int(n)) => *n,
                Some(Value::Float(f)) => *f as i64,
                _ => 1,
            };
            let sign = if name == "increment" { "+" } else { "-" };
            let (table, wheres_sql, wheres_params, db) = {
                let q = state.borrow();
                let (wsql, wparams) = build_where(&q.wheres);
                (q.table.clone(), wsql, wparams, q.db.clone())
            };
            let sql = format!("UPDATE \"{}\" SET {} = {} {} ?{}",
                table, col, col, sign, wheres_sql);
            let mut params = vec![SqlParam::Int(amount)];
            params.extend(wheres_params);
            db_exec_dml(vm, &db, &sql, &params)?;
            Ok(Value::Null)
        }
        "distinct" => Ok(qbv),
        _ => Err(vm.exc("NativeError",
            format!("QueryBuilder.{}() is not supported by the native VM yet", name))),
    }
}

// ---------------------------------------------------------------------------
// Database top-level methods
// ---------------------------------------------------------------------------

pub fn db_connect(vm: &mut Vm, args: Vec<Value>) -> R<Value> {
    let (driver, _name, path) = match args.first() {
        None | Some(Value::Null) => ("sqlite".to_string(), "sqlite".to_string(), ":memory:".to_string()),
        Some(Value::Str(sv)) => {
            let d = sv.s.clone();
            let path = match args.get(1) {
                Some(Value::Dict(d2)) => d2.borrow().iter()
                    .find(|(k, _)| matches!(k, Value::Str(sv) if sv.s == "path"))
                    .map(|(_, v)| match v { Value::Str(sv) => sv.s.clone(), _ => ":memory:".to_string() })
                    .unwrap_or_else(|| ":memory:".to_string()),
                Some(Value::Str(sv)) => sv.s.clone(),
                _ => ":memory:".to_string(),
            };
            (d.clone(), d, path)
        }
        Some(v) => return Err(vm.exc("TypeException",
            format!("Database.connect() driver must be string, got {}", type_name(v)))),
    };
    if driver != "sqlite" {
        return Err(vm.exc("DbException",
            format!("driver \"{}\" is not supported — only sqlite is available in the native VM", driver)));
    }
    unsafe {
        let cs = CString::new(path.as_str())
            .map_err(|_| vm.exc("DbException", "invalid database path"))?;
        let mut db_ptr: *mut Sqlite3 = std::ptr::null_mut();
        let rc = sqlite3_open_v2(cs.as_ptr(), &mut db_ptr,
            SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, std::ptr::null());
        if rc != SQLITE_OK || db_ptr.is_null() {
            let msg = if !db_ptr.is_null() { sqlite_errmsg_str(db_ptr) }
                      else { "failed to open database".to_string() };
            if !db_ptr.is_null() { sqlite3_close(db_ptr); }
            return Err(vm.exc("DbException", msg));
        }
        exec_ddl(db_ptr, "PRAGMA foreign_keys = ON").ok();
        Ok(Value::Database(Rc::new(RefCell::new(DbState {
            driver: "sqlite".to_string(),
            name: path,
            closed: false,
            conn: db_ptr,
            in_transaction: false,
        }))))
    }
}

pub fn db_get_attr(state: &Rc<RefCell<DbState>>, name: &str) -> Option<Value> {
    match name {
        "driver" => Some(s(state.borrow().driver.clone())),
        "name"   => Some(s(state.borrow().name.clone())),
        _ => None,
    }
}

pub fn db_method(vm: &mut Vm, state: &Rc<RefCell<DbState>>, name: &str, args: Vec<Value>) -> R<Value> {
    if state.borrow().closed && name != "close" {
        return Err(vm.exc("DbException", "the database connection is closed"));
    }
    match name {
        "table" => {
            let table = require_str_arg(vm, &args, 0, "Database.table")?;
            qid_ctx(vm, &table, "table")?; // validate name
            Ok(Value::QueryBuilder(Rc::new(RefCell::new(QbState {
                db: state.clone(),
                table,
                selects: Vec::new(),
                wheres: Vec::new(),
                joins: Vec::new(),
                orders: Vec::new(),
                limit: None,
                offset: None,
            }))))
        }
        "schema" => {
            Ok(Value::Schema(Rc::new(RefCell::new(SchemaState { db: state.clone() }))))
        }
        "tables" => {
            let rows = db_query_rows(vm, state,
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name", &[])?;
            let names: Vec<Value> = rows.into_iter()
                .map(|r| r.into_iter().next().map(|(_, v)| v).unwrap_or(Value::Null))
                .collect();
            Ok(list(names))
        }
        "raw" => {
            let sql = require_str_arg(vm, &args, 0, "Database.raw")?;
            let bind_list = match args.into_iter().nth(1) {
                Some(Value::List(l)) => l.borrow().clone(),
                _ => Vec::new(),
            };
            let mut params = Vec::new();
            for v in bind_list { params.push(to_sql_param(vm, v, "value")?); }
            let rows = db_query_rows(vm, state, &sql, &params)?;
            Ok(rows_to_value(rows))
        }
        "rawExec" => {
            let sql = require_str_arg(vm, &args, 0, "Database.rawExec")?;
            let bind_list = match args.into_iter().nth(1) {
                Some(Value::List(l)) => l.borrow().clone(),
                _ => Vec::new(),
            };
            let mut params = Vec::new();
            for v in bind_list { params.push(to_sql_param(vm, v, "value")?); }
            let (_, changes) = db_exec_dml(vm, state, &sql, &params)?;
            Ok(Value::Int(changes))
        }
        "transaction" => {
            let callback = args.into_iter().next().unwrap_or(Value::Null);
            db_exec_ddl(vm, state, "BEGIN")?;
            state.borrow_mut().in_transaction = true;
            match vm.call_value(&callback, vec![]) {
                Ok(_) => {
                    state.borrow_mut().in_transaction = false;
                    db_exec_ddl(vm, state, "COMMIT")?;
                    Ok(Value::Null)
                }
                Err(e) => {
                    state.borrow_mut().in_transaction = false;
                    let _ = db_exec_ddl(vm, state, "ROLLBACK");
                    Err(e)
                }
            }
        }
        "beginTransaction" => {
            db_exec_ddl(vm, state, "BEGIN")?;
            state.borrow_mut().in_transaction = true;
            Ok(Value::Null)
        }
        "commit" => {
            state.borrow_mut().in_transaction = false;
            db_exec_ddl(vm, state, "COMMIT")?;
            Ok(Value::Null)
        }
        "rollback" => {
            state.borrow_mut().in_transaction = false;
            db_exec_ddl(vm, state, "ROLLBACK")?;
            Ok(Value::Null)
        }
        "inTransaction" => Ok(Value::Bool(state.borrow().in_transaction)),
        "close" => {
            let mut borrow = state.borrow_mut();
            if !borrow.conn.is_null() {
                unsafe { sqlite3_close(borrow.conn); }
                borrow.conn = std::ptr::null_mut();
            }
            borrow.closed = true;
            Ok(Value::Null)
        }
        _ => Err(vm.exc("NativeError",
            format!("Database.{}() is not supported by the native VM yet", name))),
    }
}
