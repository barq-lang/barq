//! Barq bytecode: the instruction set and compiled functions.
//!
//! The VM is stack based. Each call gets a window of the VM's value stack:
//! its local slots first (`this` in slot 0 for methods, then the parameters,
//! then other locals), then the operands. Statements leave the operand part
//! empty, so jumps between statements never have to tidy the stack.

use crate::ast::Pos;
use crate::value::{Ty, Value, Vis};
use std::rc::Rc;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum BinOp {
    Add,
    Sub,
    Mul,
    Div,
    Mod,
    Pow,
}

impl BinOp {
    pub fn parse(op: &str) -> Option<BinOp> {
        Some(match op {
            "+" => BinOp::Add,
            "-" => BinOp::Sub,
            "*" => BinOp::Mul,
            "/" => BinOp::Div,
            "%" => BinOp::Mod,
            "**" => BinOp::Pow,
            _ => return None,
        })
    }
    pub fn text(self) -> &'static str {
        match self {
            BinOp::Add => "+",
            BinOp::Sub => "-",
            BinOp::Mul => "*",
            BinOp::Div => "/",
            BinOp::Mod => "%",
            BinOp::Pow => "**",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum CmpOp {
    Eq,
    Ne,
    Lt,
    Gt,
    Le,
    Ge,
    In,
    NotIn,
}

impl CmpOp {
    pub fn parse(op: &str) -> Option<CmpOp> {
        Some(match op {
            "==" => CmpOp::Eq,
            "!=" => CmpOp::Ne,
            "<" => CmpOp::Lt,
            ">" => CmpOp::Gt,
            "<=" => CmpOp::Le,
            ">=" => CmpOp::Ge,
            "in" => CmpOp::In,
            "not in" => CmpOp::NotIn,
            _ => return None,
        })
    }
    pub fn text(self) -> &'static str {
        match self {
            CmpOp::Eq => "==",
            CmpOp::Ne => "!=",
            CmpOp::Lt => "<",
            CmpOp::Gt => ">",
            CmpOp::Le => "<=",
            CmpOp::Ge => ">=",
            CmpOp::In => "in",
            CmpOp::NotIn => "not in",
        }
    }
}

/// Which condition a bool check is for, for the error message.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum CondKind {
    Cond,
    While,
    Ternary,
    AndLeft,
    AndRight,
    OrLeft,
    OrRight,
    /// a value known to be bool (switch label comparisons)
    Raw,
    /// <if condition="{...}"> in a template
    Tpl,
}

impl CondKind {
    pub fn what(self) -> &'static str {
        match self {
            CondKind::Cond | CondKind::Raw => "condition",
            CondKind::While => "while condition",
            CondKind::Ternary => "ternary condition",
            CondKind::AndLeft => "left side of 'and'",
            CondKind::AndRight => "right side of 'and'",
            CondKind::OrLeft => "left side of 'or'",
            CondKind::OrRight => "right side of 'or'",
            CondKind::Tpl => "<if> condition",
        }
    }
}

pub const NONE: u32 = u32::MAX;

#[derive(Clone, Copy, Debug)]
pub enum Op {
    /// template name lookup: push the variable and jump, or fall through to the static lookup
    TplName(u32, u32),
    /// a template name found nowhere: NameException, or null when soft (left of ??)
    TplMissing(u32, bool),
    /// template `a.b`: a dict key or an attribute (bool: ?. / left of ??)
    TplGet(u32, bool),
    Const(u32),
    Null,
    True,
    False,
    Int(i32),
    Load(u16),
    Store(u16),
    LoadCell(u16),
    StoreCell(u16),
    /// pop a value into a new cell in the slot (a captured variable is declared)
    DeclCell(u16),
    /// put a new empty cell in the slot
    NewCell(u16),
    LoadUp(u16),
    StoreUp(u16),
    LoadGlobal(u32),
    StoreGlobal(u32),
    Pop,
    Swap,
    Bin(BinOp),
    Neg,
    Plus,
    Not,
    Cmp(CmpOp),
    /// `x is T`: types[idx]
    Is(u32),
    Cast(u32),
    /// type-check the top value against types[ty]; strings[what] names it
    Check(u32, u32),
    Jump(u32),
    JumpFalse(u32, CondKind),
    JumpTrue(u32, CondKind),
    /// check the top is bool (it stays on the stack)
    CheckBool(CondKind),
    /// peek: jump if false/true and keep the value, else pop it
    JumpFalseKeep(u32),
    JumpTrueKeep(u32),
    /// `??`: peek; not null → jump keeping it, null → pop
    JumpNotNull(u32),
    /// `?.`: peek; null → jump keeping it
    JumpNull(u32),
    List(u32),
    /// pop a list and append its items to the list under it (`*xs` in a call)
    Extend,
    Dict(u32),
    Set(u32),
    Interp(u32),
    /// callee and argc arguments on the stack
    /// argc; arg_pos[idx] has each argument's position
    Call(u16, u32),
    /// callee, a list of positional arguments, then one value per name in names[idx]
    CallEx(u32, u32),
    CallBuiltin(crate::code::Builtin, u16),
    /// receiver and argc arguments; strings[name]; safe for `a?.m()`
    CallMethod(u32, u16, bool, u32),
    /// receiver, positional list, named values; strings[name], names[idx]
    CallMethodEx(u32, u32, bool, u32),
    GetAttr(u32),
    SetAttr(u32),
    SetAttrOp(u32, BinOp),
    GetIndex,
    SetIndex,
    SetIndexOp(BinOp),
    /// class (or module) on the stack; strings[name]; via `parent::`
    GetStatic(u32, bool),
    SetStatic(u32),
    SetStaticOp(u32, BinOp),
    /// classes[id] with argc arguments
    New(u32, u16, u32),
    NewEx(u32, u32, u32),
    Closure(u32),
    Return,
    /// the end of a function body
    ReturnEnd,
    Throw,
    /// throw again without a new position (bare `raise`)
    Rethrow,
    /// end of a finally block entered by an error: carry on with it
    Resume,
    /// catch target, finally target (NONE when absent)
    Try(u32, u32),
    PopTry,
    /// pop an iterable, keep its iterator in the slot; pairs = `for k, v in`
    IterInit(u16, bool),
    /// push the next item, or jump when done
    IterNext(u16, u32),
    IterNext2(u16, u32),
    /// IterNext that stores the item straight into a local slot
    IterNextStore(u16, u16, u32),
    /// `local op= value`: pops the value, updates the slot; types[ty] checks
    /// the result (NONE: no check), strings[what] names the variable
    LocalOp(u16, BinOp, u32, u32),
    /// throw errors[idx] (an exception class name and message)
    Error(u32),
    /// `except T1, T2`: is the exception on top one of names[idx]? push bool
    Matches(u32),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MathFn {
    Abs,
    Round,
    Floor,
    Ceil,
    Pow,
    Min,
    Max,
    Random,
    Clamp,
    Sqrt,
    Sin,
    Cos,
    Tan,
    Log,
    Log10,
    Exp,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JsonOp { Encode, Decode, Valid, Pretty }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IoOp { ReadFile, WriteFile, AppendFile, ReadLines, MakeDir, ListDir, Exists, IsDir, DeleteFile, RemoveDir, Size }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PathOp { Join, Basename, Extension, Stem, Dirname, Temp }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EnvOp { Get, Set, Bool, Has, Int, Load }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HashOp { Md5, Sha256, Sha1, Sha512, Hmac, Password, Verify }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CryptoOp { Base64Encode, Base64Decode, JwtSign, JwtVerify, RandomInt, RandomHex }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UuidOp { V4, V7, IsValid }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RxOp { Match, Capture, Named, MatchAll, Replace, Split, Escape }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DateOp { Make, Format, Iso, Now, Add, Diff, Parse, StartOf, Parts, Ago, IsLeapYear, DaysInMonth }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ArrOp { Sort, Unique, Flatten, Chunk, Max, Avg, Zip, Merge, Pluck, CountBy, GroupBy, Partition }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LogOp { Debug, Info, Warning, Error, Critical, Warn, SetLevel, SetFile }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CacheOp { Remember, Has, Get, Forget, Increment, Set }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StorageOp { Fake, Disk, Build, Put, Get }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CsvOp { Parse, Encode, Write, Read }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ValidationOp { Make, Check, Validate }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConfigOp { Get, Set, Has, All }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RspOp { Json, Make, Html, Text, Redirect, NoContent, New }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DbOp { Connect }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HttpOp { Get, Post, Put, Patch, Delete, Request, Fake, Recorded, Restore }

/// Methods of the built-in types (also the functions of the Str module).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum M {
    Len,
    Length,
    Upper,
    Lower,
    Trim,
    TrimStart,
    TrimEnd,
    Contains,
    StartsWith,
    EndsWith,
    IndexOf,
    Split,
    Replace,
    Substr,
    Pad,
    Repeat,
    Reverse,
    Slug,
    Capitalize,
    IsEmpty,
    Chars,
    Push,
    Pop,
    Insert,
    RemoveAt,
    Remove,
    Join,
    Map,
    Filter,
    Reduce,
    Each,
    Any,
    All,
    Find,
    Sort,
    Sum,
    First,
    Last,
    Slice,
    Copy,
    Clear,
    Keys,
    Values,
    Items,
    Has,
    Get,
    Merge,
    Add,
    ToList,
    Union,
    Intersect,
    Diff,
}

pub fn method_id(name: &str) -> Option<M> {
    use M::*;
    Some(match name {
        "len" => Len,
        "length" => Length,
        "upper" => Upper,
        "lower" => Lower,
        "trim" => Trim,
        "trimStart" => TrimStart,
        "trimEnd" => TrimEnd,
        "contains" => Contains,
        "startsWith" => StartsWith,
        "endsWith" => EndsWith,
        "indexOf" => IndexOf,
        "split" => Split,
        "replace" => Replace,
        "substr" => Substr,
        "pad" => Pad,
        "repeat" => Repeat,
        "reverse" => Reverse,
        "slug" => Slug,
        "capitalize" => Capitalize,
        "isEmpty" => IsEmpty,
        "chars" => Chars,
        "push" => Push,
        "pop" => Pop,
        "insert" => Insert,
        "removeAt" => RemoveAt,
        "remove" => Remove,
        "join" => Join,
        "map" => Map,
        "filter" => Filter,
        "reduce" => Reduce,
        "each" => Each,
        "any" => Any,
        "all" => All,
        "find" => Find,
        "sort" => Sort,
        "sum" => Sum,
        "first" => First,
        "last" => Last,
        "slice" => Slice,
        "copy" => Copy,
        "clear" => Clear,
        "keys" => Keys,
        "values" => Values,
        "items" => Items,
        "has" => Has,
        "get" => Get,
        "merge" => Merge,
        "add" => Add,
        "toList" => ToList,
        "union" => Union,
        "intersect" => Intersect,
        "diff" => Diff,
        _ => return None,
    })
}

/// Magic helper library — global functions from barq/magic.py (v0.7.0+)
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MagicOp {
    // type checks
    IsNull, IsStr, IsInt, IsFloat, IsNum, IsBool, IsList, IsDict,
    IsFn, IsObj, IsSet, IsNumeric, IsEmpty, NotNull,
    // number helpers
    IsOdd, IsEven, Between, Clamp, Sign,
    // list helpers
    InList, HasKey, ListSearch, ListMap, ListFilter, ListReduce, ListEach,
    ListFlatten, ListUnique, ListChunk, ListZip, ListSum, ListCount,
    ListAny, ListAll, ListFind, ListFirst, ListLast, ListSlice,
    ListMerge, ListReverse, ListSort, ListContains,
    // dict helpers
    DictMerge, DictKeys, DictValues, DictHas, DictGet, DictPick, DictOmit,
    // string helpers
    StrLen, StrUpper, StrLower, StrTrim, StrLtrim, StrRtrim, StrReplace,
    StrContains, StrStarts, StrEnds, StrSplit, StrJoin, StrPad, StrRepeat,
    StrSlug, StrReverse, StrSubstr, StrIndex, StrLastIndex, StrCount,
    StrWords, StrWordCount, StrCapitalize, StrTitle, StrFormat, StrIsEmpty,
    StrToList, StrLines, StrBefore, StrAfter, StrBetween,
    // regex helpers
    RxMatch, RxTest, RxFind, RxFindAll, RxReplace, RxSplit, RxCount,
    // reflection
    HasMethod, HasProp, HasClass, ClassOf, InstanceOf, PropNames, MethodNames, ToDict,
    // value helpers
    Coalesce, Val, Clone, ToStr, ToInt, ToFloat, ToBool, ToList,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Builtin {
    Print,
    Write,
    Dump,
    Dd,
    Breakpoint,
    Exit,
    Die,
    Range,
    Len,
    Type,
    Input,
    Args,
    Assert,
    Math(MathFn),
    StrFn(M),
    StrJoin,
    ExcConstruct,
    ExcGetMessage,
    ExcGetCode,
    EnumCases(usize),
    EnumFrom(usize),
    EnumTryFrom(usize),
    JsonOp(JsonOp),
    IoOp(IoOp),
    PathOp(PathOp),
    EnvOp(EnvOp),
    HashOp(HashOp),
    CryptoOp(CryptoOp),
    UuidOp(UuidOp),
    RxOp(RxOp),
    DateOp(DateOp),
    ArrOp(ArrOp),
    LogOp(LogOp),
    CacheOp(CacheOp),
    MagicOp(MagicOp),
    StorageOp(StorageOp),
    CsvOp(CsvOp),
    ValidationOp(ValidationOp),
    ConfigOp(ConfigOp),
    RspOp(RspOp),
    DbOp(DbOp),
    HttpOp(HttpOp),
    XmlOp(XmlOp),
    MailOp(MailOp),
    QueueOp(QueueOp),
    ViewOp(ViewOp),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ViewOp { View, Render, Str, Exists, Share, Shared, Path, Escape, Json, ClearCache }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum XmlOp { Parse, Element, ToDict, FromDict, Valid, Escape }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MailOp { Fake, To, Send, Raw, Driver, Sent }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum QueueOp { Push, Config, On, Later, Work, Failed, Retry, FlushFailed, Clear, Size, Fake, Pushed, Driver }

pub enum Default {
    Const(Value),
    Code(Rc<Proto>),
}

pub struct ParamInfo {
    pub name: Rc<str>,
    pub ty: Option<Ty>,
    pub default: Option<Default>,
    pub variadic: bool,
    /// element type of a *variadic parameter
    pub elem: Option<Ty>,
}

#[derive(Clone, Copy, Debug)]
pub enum UpSrc {
    Local(u16),
    Up(u16),
}

pub struct Proto {
    pub name: Rc<str>,
    /// Class.name, Class::name or name, for messages and traces
    pub display: Rc<str>,
    pub params: Vec<ParamInfo>,
    /// every parameter is passed positionally: no defaults, no *variadic
    pub simple: bool,
    pub ret: Option<Ty>,
    #[allow(dead_code)]
    pub ret_pos: Pos,
    pub code: Vec<Op>,
    pub pos: Vec<Pos>,
    pub consts: Vec<Value>,
    pub strings: Vec<Rc<str>>,
    pub types: Vec<Ty>,
    pub names: Vec<Vec<Rc<str>>>,
    pub errors: Vec<(&'static str, String)>,
    /// functions defined inside this one (lambdas, nested karo)
    pub protos: Vec<Rc<Proto>>,
    /// positions of the arguments of each call (as written, named ones included)
    pub arg_pos: Vec<Vec<Pos>>,
    pub nlocals: usize,
    /// parameter slots (this included) to move into cells when the call starts
    pub cell_params: Vec<u16>,
    /// where closures made from this function find their captured variables
    pub upvals: Vec<UpSrc>,
    pub owner: Option<usize>,
    pub is_static: bool,
    pub is_abstract: bool,
    #[allow(dead_code)]
    pub vis: Vis,
    /// class whose code this is (lambdas inside methods share it)
    pub ctx: Option<usize>,
    /// `this` lives in slot 0
    pub has_this: bool,
    #[allow(dead_code)]
    pub is_lambda: bool,
}
