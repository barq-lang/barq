//! stdlib batch 3 (v0.33): Xml, Mail, Queue.

use std::cell::RefCell;
use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_int, c_void};
use std::rc::Rc;

use crate::code::{XmlOp, MailOp, QueueOp};
use crate::json;
use crate::value::{Value, BStr, OMap, hkey, s, list, type_name};
use crate::vm::{Vm, R, Flow};

// Simple JSON encoder (stdlib3 internal)
fn j_encode(j: &json::J) -> String {
    match j {
        json::J::Null => "null".to_string(),
        json::J::Bool(b) => if *b { "true".to_string() } else { "false".to_string() },
        json::J::Int(n) => n.to_string(),
        json::J::Num(f) => crate::builtins::format_float(*f),
        json::J::Str(s) => {
            let mut out = String::from('"');
            for c in s.chars() {
                match c {
                    '"' => out.push_str("\\\""),
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    c if (c as u32) < 32 => out.push_str(&format!("\\u{:04x}", c as u32)),
                    c => out.push(c),
                }
            }
            out.push('"');
            out
        }
        json::J::Arr(items) => {
            let parts: Vec<String> = items.iter().map(j_encode).collect();
            format!("[{}]", parts.join(","))
        }
        json::J::Obj(fields) => {
            let parts: Vec<String> = fields.iter().map(|(k, v)| {
                let ks = j_encode(&json::J::Str(k.clone()));
                format!("{}:{}", ks, j_encode(v))
            }).collect();
            format!("{{{}}}", parts.join(","))
        }
    }
}

fn j_has(j: &json::J, key: &str) -> bool {
    if let json::J::Obj(fields) = j {
        fields.iter().any(|(k, _)| k == key)
    } else { false }
}

// ===========================================================================
// XML
// ===========================================================================

pub struct XmlNodeState {
    pub name: String,
    pub attrs: Vec<(String, String)>,
    pub text: String,
    pub children: Vec<Value>,
}

fn new_node(name: String, attrs: Vec<(String, String)>, text: String) -> Value {
    Value::XmlNode(Rc::new(RefCell::new(XmlNodeState { name, attrs, text, children: Vec::new() })))
}

// ---- parser ----------------------------------------------------------------

struct XmlParser {
    bytes: Vec<u8>,
    pos: usize,
    line: u32,
    line_start: usize,
}

impl XmlParser {
    fn new(text: &str) -> Self {
        XmlParser { bytes: text.as_bytes().to_vec(), pos: 0, line: 1, line_start: 0 }
    }
    fn col(&self) -> usize { self.pos - self.line_start + 1 }
    fn peek(&self) -> Option<u8> { self.bytes.get(self.pos).copied() }
    fn advance(&mut self) {
        if self.pos < self.bytes.len() {
            if self.bytes[self.pos] == b'\n' { self.line += 1; self.line_start = self.pos + 1; }
            self.pos += 1;
        }
    }
    fn expect_byte(&mut self, b: u8) -> Result<(), String> {
        if self.peek() == Some(b) { self.advance(); Ok(()) }
        else { Err(format!("expected '{}' at line {}, column {}", b as char, self.line, self.col())) }
    }
    fn skip_whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ') | Some(b'\t') | Some(b'\r') | Some(b'\n')) { self.advance(); }
    }
    fn read_name(&mut self) -> Result<String, String> {
        let start = self.pos;
        match self.peek() {
            Some(b) if is_name_start(b) => self.advance(),
            _ => return Err(format!("expected XML name at line {}, column {}", self.line, self.col())),
        }
        while matches!(self.peek(), Some(b) if is_name_char(b)) { self.advance(); }
        Ok(String::from_utf8_lossy(&self.bytes[start..self.pos]).into_owned())
    }
    fn read_attr_value(&mut self, quote: u8) -> Result<String, String> {
        self.advance(); // skip opening quote
        let mut s = String::new();
        loop {
            match self.peek() {
                None => return Err(format!("unterminated attribute at line {}, column {}", self.line, self.col())),
                Some(b) if b == quote => { self.advance(); break; }
                Some(b'&') => { let ent = self.read_entity()?; s.push_str(&ent); }
                Some(b) => { s.push(b as char); self.advance(); }
            }
        }
        Ok(s)
    }
    fn read_entity(&mut self) -> Result<String, String> {
        self.advance(); // skip '&'
        if self.peek() == Some(b'#') {
            self.advance();
            let (radix, start) = if self.peek() == Some(b'x') { self.advance(); (16u32, self.pos) } else { (10, self.pos) };
            while matches!(self.peek(), Some(b'0'..=b'9') | Some(b'a'..=b'f') | Some(b'A'..=b'F')) { self.advance(); }
            let digits = String::from_utf8_lossy(&self.bytes[start..self.pos]).into_owned();
            self.expect_byte(b';')?;
            let cp = u32::from_str_radix(&digits, radix).unwrap_or(0xFFFD);
            return Ok(char::from_u32(cp).unwrap_or('\u{FFFD}').to_string());
        }
        let name = self.read_name()?;
        self.expect_byte(b';')?;
        Ok(match name.as_str() {
            "amp" => "&".to_string(), "lt" => "<".to_string(), "gt" => ">".to_string(),
            "quot" => "\"".to_string(), "apos" => "'".to_string(),
            _ => format!("&{};", name),
        })
    }
    fn skip_comment(&mut self) -> Result<(), String> {
        // already consumed <!--
        loop {
            match self.peek() {
                None => return Err(format!("unterminated comment at line {}, column {}", self.line, self.col())),
                Some(b'-') => {
                    self.advance();
                    if self.peek() == Some(b'-') {
                        self.advance();
                        return self.expect_byte(b'>');
                    }
                }
                _ => { self.advance(); }
            }
        }
    }
    fn skip_pi(&mut self) -> Result<(), String> {
        // already consumed <?
        loop {
            match self.peek() {
                None => return Err(format!("unterminated PI at line {}, column {}", self.line, self.col())),
                Some(b'?') => {
                    self.advance();
                    if self.peek() == Some(b'>') { self.advance(); return Ok(()); }
                }
                _ => { self.advance(); }
            }
        }
    }
    fn skip_doctype(&mut self) -> Result<(), String> {
        // already consumed <!DOCTYPE
        // scan for '[' (internal subset) or '>'
        loop {
            match self.peek() {
                None => return Err(format!("unterminated DOCTYPE at line {}, column {}", self.line, self.col())),
                Some(b'[') => {
                    self.advance();
                    // scan internal subset for ENTITY declarations
                    loop {
                        match self.peek() {
                            None => return Err(format!("unterminated DOCTYPE subset at line {}, column {}", self.line, self.col())),
                            Some(b']') => { self.advance(); break; }
                            Some(b'<') => {
                                self.advance();
                                // check for <!ENTITY
                                if self.remaining_starts_with(b"!ENTITY") {
                                    return Err("<!ENTITY> declarations are not allowed (they can be used for attacks)".to_string());
                                }
                                // skip anything else inside subset
                                while !matches!(self.peek(), Some(b'>') | None) { self.advance(); }
                                if self.peek() == Some(b'>') { self.advance(); }
                            }
                            _ => { self.advance(); }
                        }
                    }
                }
                Some(b'>') => { self.advance(); return Ok(()); }
                _ => { self.advance(); }
            }
        }
    }
    fn remaining_starts_with(&self, pat: &[u8]) -> bool {
        self.bytes[self.pos..].starts_with(pat)
    }
    fn read_cdata(&mut self) -> Result<String, String> {
        // already consumed <![CDATA[
        let mut s = String::new();
        loop {
            match self.peek() {
                None => return Err(format!("unterminated CDATA at line {}, column {}", self.line, self.col())),
                Some(b']') if self.remaining_starts_with(b"]]>") => {
                    self.pos += 3;
                    break;
                }
                Some(b) => { s.push(b as char); self.advance(); }
            }
        }
        Ok(s)
    }

    fn parse_document(&mut self) -> Result<Value, String> {
        self.skip_prolog()?;
        let root = self.parse_element()?;
        self.skip_whitespace();
        // skip trailing PIs/comments
        while self.peek() == Some(b'<') {
            let saved = self.pos;
            self.advance();
            if self.remaining_starts_with(b"!--") {
                self.pos += 3;
                self.skip_comment()?;
            } else if self.remaining_starts_with(b"?") {
                self.advance();
                self.skip_pi()?;
            } else {
                self.pos = saved;
                break;
            }
            self.skip_whitespace();
        }
        Ok(root)
    }

    fn skip_prolog(&mut self) -> Result<(), String> {
        self.skip_whitespace();
        loop {
            if self.remaining_starts_with(b"<?xml") {
                self.advance(); self.advance();
                self.skip_pi()?;
                self.skip_whitespace();
            } else if self.remaining_starts_with(b"<!--") {
                self.pos += 4;
                self.skip_comment()?;
                self.skip_whitespace();
            } else if self.remaining_starts_with(b"<!DOCTYPE") {
                self.pos += 9;
                self.skip_doctype()?;
                self.skip_whitespace();
            } else if self.remaining_starts_with(b"<?") {
                self.advance(); self.advance();
                self.skip_pi()?;
                self.skip_whitespace();
            } else {
                break;
            }
        }
        Ok(())
    }

    fn parse_element(&mut self) -> Result<Value, String> {
        self.expect_byte(b'<')?;
        let name = self.read_name()?;
        let mut attrs = Vec::new();
        loop {
            self.skip_whitespace();
            match self.peek() {
                Some(b'>') => { self.advance(); break; }
                Some(b'/') => {
                    self.advance();
                    self.expect_byte(b'>')?;
                    return Ok(new_node(name, attrs, String::new()));
                }
                Some(b) if is_name_start(b) => {
                    let aname = self.read_name()?;
                    self.skip_whitespace();
                    self.expect_byte(b'=')?;
                    self.skip_whitespace();
                    let quote = self.peek().unwrap_or(b'"');
                    if quote != b'"' && quote != b'\'' {
                        return Err(format!("expected quote in attribute at line {}, column {}", self.line, self.col()));
                    }
                    let val = self.read_attr_value(quote)?;
                    attrs.push((aname, val));
                }
                _ => return Err(format!("unexpected character in element at line {}, column {}", self.line, self.col())),
            }
        }
        let node = new_node(name.clone(), attrs, String::new());
        let mut text_parts: Vec<String> = Vec::new();
        loop {
            if self.peek() == Some(b'<') {
                // peek ahead
                if self.remaining_starts_with(b"</") {
                    // end tag
                    self.advance(); self.advance();
                    self.skip_whitespace();
                    // record column at start of name for error reporting
                    let end_name = self.read_name()?;
                    self.skip_whitespace();
                    self.expect_byte(b'>')?;
                    if end_name != name {
                        // find column just before name — approximate
                        return Err(format!("mismatched tag at line {}, column {}", self.line, self.col() - end_name.len() - 1));
                    }
                    break;
                } else if self.remaining_starts_with(b"<!--") {
                    self.pos += 4;
                    self.skip_comment()?;
                } else if self.remaining_starts_with(b"<![CDATA[") {
                    self.pos += 9;
                    let cd = self.read_cdata()?;
                    text_parts.push(cd);
                } else if self.remaining_starts_with(b"<?") {
                    self.advance(); self.advance();
                    self.skip_pi()?;
                } else {
                    // child element
                    let child = self.parse_element()?;
                    if let Value::XmlNode(ref nd) = node {
                        nd.borrow_mut().children.push(child);
                    }
                }
            } else if self.peek() == Some(b'&') {
                let ent = self.read_entity()?;
                text_parts.push(ent);
            } else if let Some(b) = self.peek() {
                text_parts.push((b as char).to_string());
                self.advance();
            } else {
                return Err(format!("unexpected end of input at line {}, column {}", self.line, self.col()));
            }
        }
        let text = text_parts.join("").trim().to_string();
        if let Value::XmlNode(ref nd) = node {
            nd.borrow_mut().text = text;
        }
        Ok(node)
    }
}

fn is_name_start(b: u8) -> bool {
    matches!(b, b'A'..=b'Z' | b'a'..=b'z' | b'_') || b > 127
}
fn is_name_char(b: u8) -> bool {
    matches!(b, b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'-' | b'.' | b':') || b > 127
}

fn xml_parse(vm: &mut Vm, text: &str, fname: &str) -> R<Value> {
    let mut parser = XmlParser::new(text);
    match parser.parse_document() {
        Ok(v) => Ok(v),
        Err(msg) => vm.fail("XmlException", format!("{}(): {}", fname, msg)),
    }
}

// ---- xml escape / serialize ------------------------------------------------

fn xml_escape_text(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            _ => out.push(c),
        }
    }
    out
}

fn xml_escape_attr(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\n' => out.push_str("&#10;"),
            '\r' => out.push_str("&#13;"),
            '\t' => out.push_str("&#9;"),
            _ => out.push(c),
        }
    }
    out
}

fn xml_escape_full(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '<' => out.push_str("&lt;"),
            '"' => out.push_str("&quot;"),
            '&' => out.push_str("&amp;"),
            '>' => out.push_str("&gt;"),
            _ => out.push(c),
        }
    }
    out
}

fn is_valid_xml_name(s: &str) -> bool {
    let mut bytes = s.bytes();
    match bytes.next() {
        Some(b) if is_name_start(b) => {}
        _ => return false,
    }
    bytes.all(|b| is_name_char(b))
}

fn scalar_to_string(vm: &mut Vm, v: &Value, what: &str) -> R<String> {
    match v {
        Value::Null => Ok(String::new()),
        Value::Str(s) => Ok(s.s.clone()),
        Value::Int(n) => Ok(n.to_string()),
        Value::Float(f) => Ok(crate::builtins::format_float(*f)),
        Value::Bool(b) => Ok(if *b { "true".into() } else { "false".into() }),
        other => vm.fail("TypeException", format!("XML {} must be a string or number, got {}", what, type_name(other))),
    }
}

fn xml_serialize(vm: &mut Vm, node_val: &Value, pretty: bool, depth: usize, seen: &mut Vec<*const ()>) -> R<String> {
    let nd_rc = match node_val {
        Value::XmlNode(rc) => rc.clone(),
        other => return vm.fail("TypeException", format!("XmlNode.children element must be XmlNode, got {}", type_name(other))),
    };
    let ptr = Rc::as_ptr(&nd_rc) as *const ();
    if seen.contains(&ptr) {
        return vm.fail("XmlException", "an XmlNode cannot contain itself");
    }
    seen.push(ptr);
    let (name, out_str, text, children) = {
        let nd = nd_rc.borrow();
        let name = nd.name.clone();
        if !is_valid_xml_name(&name) {
            seen.pop();
            return vm.fail("XmlException", format!("invalid XML element name {:?}", name));
        }
        let mut out = format!("<{}", name);
        for (aname, aval) in &nd.attrs {
            if !is_valid_xml_name(aname) {
                seen.pop();
                return vm.fail("XmlException", format!("invalid XML attribute name {:?}", aname));
            }
            out.push_str(&format!(" {}=\"{}\"", aname, xml_escape_attr(aval)));
        }
        let text = xml_escape_text(&nd.text);
        let children = nd.children.clone();
        (name, out, text, children)
    };
    let out = out_str;
    if children.is_empty() {
        seen.pop();
        if text.is_empty() {
            return Ok(format!("{}>", out) + &format!("</{}>", name));
        }
        return Ok(format!("{}>", out) + &text + &format!("</{}>", name));
    }
    let pad = if pretty { format!("\n{}", "    ".repeat(depth + 1)) } else { String::new() };
    let end_pad = if pretty { format!("\n{}", "    ".repeat(depth)) } else { String::new() };
    let mut inner = if !text.is_empty() { pad.clone() + &text } else { String::new() };
    for child in &children {
        inner.push_str(&pad);
        inner.push_str(&xml_serialize(vm, child, pretty, depth + 1, seen)?);
    }
    seen.pop();
    Ok(format!("{}>", out) + &inner + &end_pad + &format!("</{}>", name))
}

// ---- node_to_value ---------------------------------------------------------

fn node_to_value(node_rc: &Rc<RefCell<XmlNodeState>>) -> Value {
    let nd = node_rc.borrow();
    let attrs = &nd.attrs;
    let children = &nd.children;
    let text = &nd.text;
    if attrs.is_empty() && children.is_empty() {
        return s(text.clone());
    }
    let mut m = OMap::new(false);
    for (k, v) in attrs {
        let key = format!("@{}", k);
        m.insert(hkey(&s(key.clone()), false).unwrap(), s(key), s(v.clone()));
    }
    // count child tag names
    let mut counts: HashMap<String, usize> = HashMap::new();
    for child in children {
        if let Value::XmlNode(ref cn) = child {
            let cname = cn.borrow().name.clone();
            *counts.entry(cname).or_insert(0) += 1;
        }
    }
    for child in children {
        if let Value::XmlNode(ref cn) = child {
            let cname = cn.borrow().name.clone();
            let cval = node_to_value(cn);
            if counts[&cname] > 1 {
                // append to list
                let key_hk = hkey(&s(cname.clone()), false).unwrap();
                if let Some(existing) = m.get(&key_hk) {
                    if let Value::List(lst) = existing {
                        lst.borrow_mut().push(cval);
                        continue;
                    }
                }
                // first occurrence — create list
                let lst = list(vec![cval]);
                m.insert(key_hk, s(cname), lst);
            } else {
                m.insert(hkey(&s(cname.clone()), false).unwrap(), s(cname), cval);
            }
        }
    }
    if !text.is_empty() {
        m.insert(hkey(&s("#text".to_string()), false).unwrap(), s("#text".to_string()), s(text.clone()));
    }
    Value::Dict(Rc::new(RefCell::new(m)))
}

// ---- value_to_node ---------------------------------------------------------

fn value_to_node_inner(vm: &mut Vm, name: &str, value: &Value) -> R<Value> {
    if !is_valid_xml_name(name) {
        return vm.fail("XmlException", format!("Xml.fromDict(): invalid element name {:?}", name));
    }
    let node = new_node(name.to_string(), Vec::new(), String::new());
    match value {
        Value::Dict(dm) => {
            let pairs: Vec<(Value, Value)> = dm.borrow().iter().cloned().collect();
            for (k, v) in &pairs {
                let ks = match k { Value::Str(ss) => ss.s.clone(), other => {
                    return vm.fail("XmlException", format!("Xml.fromDict(): keys must be strings, got {}", type_name(other)));
                }};
                if ks.starts_with('@') {
                    let aname = &ks[1..];
                    let aval = scalar_to_string(vm, v, "attribute values")?;
                    if let Value::XmlNode(ref nd) = node {
                        nd.borrow_mut().attrs.push((aname.to_string(), aval));
                    }
                } else if ks == "#text" {
                    let tv = scalar_to_string(vm, v, "text")?;
                    if let Value::XmlNode(ref nd) = node {
                        nd.borrow_mut().text = tv;
                    }
                } else {
                    match v {
                        Value::List(lst) => {
                            let items = lst.borrow().clone();
                            for item in &items {
                                let child = value_to_node_inner(vm, &ks, item)?;
                                if let Value::XmlNode(ref nd) = node {
                                    nd.borrow_mut().children.push(child);
                                }
                            }
                        }
                        other => {
                            let child = value_to_node_inner(vm, &ks, other)?;
                            if let Value::XmlNode(ref nd) = node {
                                nd.borrow_mut().children.push(child);
                            }
                        }
                    }
                }
            }
        }
        Value::List(_) => {
            return vm.fail("XmlException", format!("Xml.fromDict(): a list must be the value of a key (the element name to repeat), at <{}>", name));
        }
        other => {
            let tv = scalar_to_string(vm, other, "text")?;
            if let Value::XmlNode(ref nd) = node {
                nd.borrow_mut().text = tv;
            }
        }
    }
    Ok(node)
}

// ---- xml_select (find / findAll) -------------------------------------------

fn xml_descendants(node_rc: &Rc<RefCell<XmlNodeState>>) -> Vec<Value> {
    let children = node_rc.borrow().children.clone();
    let mut out = Vec::new();
    for child in children {
        if let Value::XmlNode(ref cn) = child {
            out.push(child.clone());
            out.extend(xml_descendants(cn));
        }
    }
    out
}

fn xml_select(node_rc: &Rc<RefCell<XmlNodeState>>, path: &str) -> Vec<Value> {
    let mut current: Vec<Value> = vec![Value::XmlNode(node_rc.clone())];
    let mut desc = false;
    for seg in path.split('/') {
        if seg.is_empty() {
            desc = true;
            continue;
        }
        let mut nxt = Vec::new();
        for cur in &current {
            if let Value::XmlNode(ref cn) = cur {
                let pool: Vec<Value> = if desc {
                    xml_descendants(cn)
                } else {
                    cn.borrow().children.clone()
                };
                for item in pool {
                    if let Value::XmlNode(ref child) = item {
                        let cname = child.borrow().name.clone();
                        if seg == "*" || cname == seg {
                            nxt.push(item.clone());
                        }
                    }
                }
            }
        }
        current = nxt;
        desc = false;
    }
    current
}

// ---- public xml ops --------------------------------------------------------

pub fn xml_op(vm: &mut Vm, op: XmlOp, args: Vec<Value>) -> R<Value> {
    match op {
        XmlOp::Parse => {
            let text = match args.into_iter().next() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Xml.parse() argument 1 must be string, got {}", type_name(&other))),
                None => return vm.fail("TypeException", "Xml.parse() expects 1 argument, got 0"),
            };
            xml_parse(vm, &text, "Xml.parse")
        }
        XmlOp::Valid => {
            let text = match args.into_iter().next() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Xml.valid() argument 1 must be string, got {}", type_name(&other))),
                None => return vm.fail("TypeException", "Xml.valid() expects 1 argument, got 0"),
            };
            let mut parser = XmlParser::new(&text);
            Ok(Value::Bool(parser.parse_document().is_ok()))
        }
        XmlOp::ToDict => {
            let text = match args.into_iter().next() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Xml.toDict() argument 1 must be string, got {}", type_name(&other))),
                None => return vm.fail("TypeException", "Xml.toDict() expects 1 argument, got 0"),
            };
            let root = xml_parse(vm, &text, "Xml.toDict")?;
            if let Value::XmlNode(ref nd) = root {
                let name = nd.borrow().name.clone();
                let inner = node_to_value(nd);
                let mut m = OMap::new(false);
                m.insert(hkey(&s(name.clone()), false).unwrap(), s(name), inner);
                Ok(Value::Dict(Rc::new(RefCell::new(m))))
            } else {
                vm.fail("XmlException", "Xml.toDict(): internal error")
            }
        }
        XmlOp::FromDict => {
            let mut args = args;
            let name = match args.first() {
                Some(Value::Str(ss)) => ss.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Xml.fromDict() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "Xml.fromDict() expects at least 2 arguments"),
            };
            let value = if args.len() > 1 { args.remove(1) } else {
                return vm.fail("TypeException", "Xml.fromDict() expects at least 2 arguments");
            };
            let pretty = matches!(args.get(2), Some(Value::Bool(true)));
            let node = value_to_node_inner(vm, &name, &value)?;
            xml_serialize(vm, &node, pretty, 0, &mut Vec::new()).map(s)
        }
        XmlOp::Element => {
            let mut args = args;
            let name = match args.first() {
                Some(Value::Str(ss)) => ss.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Xml.element() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "Xml.element() expects at least 1 argument"),
            };
            if !is_valid_xml_name(&name) {
                return vm.fail("XmlException", format!("Xml.element(): invalid element name {:?}", name));
            }
            let attrs_val = args.get(1).cloned().unwrap_or(Value::Null);
            let text_val = args.get(2).cloned().unwrap_or(Value::Null);
            let mut attrs = Vec::new();
            if let Value::Dict(dm) = &attrs_val {
                let pairs: Vec<(Value, Value)> = dm.borrow().iter().cloned().collect();
                for (k, v) in &pairs {
                    let ks = match k { Value::Str(ss) => ss.s.clone(), other => {
                        return vm.fail("TypeException", format!("Xml.element(): attribute keys must be strings, got {}", type_name(other)));
                    }};
                    attrs.push((ks, scalar_to_string(vm, v, "attribute values")?));
                }
            }
            let text = scalar_to_string(vm, &text_val, "text")?;
            Ok(new_node(name, attrs, text))
        }
        XmlOp::Escape => {
            let text = match args.into_iter().next() {
                Some(Value::Str(s)) => s.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Xml.escape() argument 1 must be string, got {}", type_name(&other))),
                None => return vm.fail("TypeException", "Xml.escape() expects 1 argument, got 0"),
            };
            Ok(s(xml_escape_full(&text)))
        }
    }
}

pub fn xml_node_method(vm: &mut Vm, node: &Rc<RefCell<XmlNodeState>>, name: &str, args: Vec<Value>) -> R<Value> {
    match name {
        "attr" => {
            let key = match args.first() {
                Some(Value::Str(ss)) => ss.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("XmlNode.attr() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "XmlNode.attr() expects at least 1 argument"),
            };
            let default = args.get(1).cloned().unwrap_or(Value::Null);
            let nd = node.borrow();
            Ok(nd.attrs.iter().find(|(k, _)| k == &key).map(|(_, v)| s(v.clone())).unwrap_or(default))
        }
        "setAttr" => {
            let key = match args.first() {
                Some(Value::Str(ss)) => ss.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("XmlNode.setAttr() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "XmlNode.setAttr() expects 2 arguments"),
            };
            if !is_valid_xml_name(&key) {
                return vm.fail("XmlException", format!("XmlNode.setAttr(): invalid attribute name {:?}", key));
            }
            let val = match args.get(1) {
                Some(v) => scalar_to_string(vm, v, "attribute values")?,
                None => return vm.fail("TypeException", "XmlNode.setAttr() expects 2 arguments"),
            };
            let mut nd = node.borrow_mut();
            if let Some(existing) = nd.attrs.iter_mut().find(|(k, _)| k == &key) {
                existing.1 = val;
            } else {
                nd.attrs.push((key, val));
            }
            Ok(Value::XmlNode(node.clone()))
        }
        "find" => {
            let path = match args.first() {
                Some(Value::Str(ss)) => ss.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("XmlNode.find() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "XmlNode.find() expects 1 argument"),
            };
            let results = xml_select(node, &path);
            Ok(results.into_iter().next().unwrap_or(Value::Null))
        }
        "findAll" => {
            let path = match args.first() {
                Some(Value::Str(ss)) => ss.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("XmlNode.findAll() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "XmlNode.findAll() expects 1 argument"),
            };
            let results = xml_select(node, &path);
            Ok(list(results))
        }
        "textOf" => {
            let path = match args.first() {
                Some(Value::Str(ss)) => ss.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("XmlNode.textOf() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "XmlNode.textOf() expects at least 1 argument"),
            };
            let default = args.get(1).cloned().unwrap_or(Value::Null);
            let results = xml_select(node, &path);
            Ok(match results.first() {
                Some(Value::XmlNode(cn)) => s(cn.borrow().text.clone()),
                _ => default,
            })
        }
        "add" => {
            let child = match args.into_iter().next() {
                Some(v @ Value::XmlNode(_)) => v,
                Some(other) => return vm.fail("TypeException", format!("XmlNode.add() argument 1 must be XmlNode, got {}", type_name(&other))),
                None => return vm.fail("TypeException", "XmlNode.add() expects 1 argument"),
            };
            let child_clone = child.clone();
            node.borrow_mut().children.push(child);
            Ok(child_clone)
        }
        "toDict" => {
            Ok(node_to_value(node))
        }
        "toString" => {
            let pretty = matches!(args.first(), Some(Value::Bool(true)));
            xml_serialize(vm, &Value::XmlNode(node.clone()), pretty, 0, &mut Vec::new()).map(s)
        }
        "__toString" => {
            xml_serialize(vm, &Value::XmlNode(node.clone()), false, 0, &mut Vec::new()).map(s)
        }
        other => vm.fail("NameException", format!("XmlNode has no method '{}'", other)),
    }
}

pub fn xml_node_get_attr(node: &Rc<RefCell<XmlNodeState>>, name: &str) -> Option<Value> {
    match name {
        "name" => Some(s(node.borrow().name.clone())),
        "text" => Some(s(node.borrow().text.clone())),
        "attrs" => {
            let nd = node.borrow();
            let mut m = OMap::new(false);
            for (k, v) in &nd.attrs {
                m.insert(hkey(&s(k.clone()), false).unwrap(), s(k.clone()), s(v.clone()));
            }
            Some(Value::Dict(Rc::new(RefCell::new(m))))
        }
        "children" => {
            let children = node.borrow().children.clone();
            Some(list(children))
        }
        _ => None,
    }
}

pub fn xml_node_set_attr(vm: &mut Vm, node: &Rc<RefCell<XmlNodeState>>, name: &str, value: Value) -> R<()> {
    match name {
        "text" => {
            match value {
                Value::Str(sv) => { node.borrow_mut().text = sv.s.clone(); Ok(()) }
                other => vm.fail("TypeException", format!("XmlNode.text must be a string, got {}", type_name(&other))),
            }
        }
        "name" => {
            match value {
                Value::Str(sv) => { node.borrow_mut().name = sv.s.clone(); Ok(()) }
                other => vm.fail("TypeException", format!("XmlNode.name must be a string, got {}", type_name(&other))),
            }
        }
        other => vm.fail("NameException", format!("XmlNode has no settable property '{}'", other)),
    }
}

// ===========================================================================
// Mail
// ===========================================================================

pub struct MessageState {
    pub to: Vec<String>,
    pub cc: Vec<String>,
    pub bcc: Vec<String>,
    pub reply_to: Vec<String>,
    pub sender: Option<String>,
    pub subject: String,
    pub text: Option<String>,
    pub html: Option<String>,
    pub attachments: Vec<(String, Vec<u8>)>, // (name, data)
}

impl MessageState {
    fn new() -> Self {
        MessageState { to: Vec::new(), cc: Vec::new(), bcc: Vec::new(), reply_to: Vec::new(),
            sender: None, subject: String::new(), text: None, html: None, attachments: Vec::new() }
    }
}

pub struct PendingMailState {
    pub to: Vec<String>,
    pub cc: Vec<String>,
    pub bcc: Vec<String>,
}

struct SentMail {
    to: Vec<String>,
    cc: Vec<String>,
    bcc: Vec<String>,
    from: String,
    subject: String,
    text: Option<String>,
    attachments: Vec<String>,
}

struct MailGlobal {
    driver: String,
    from_addr: String,
    from_name: Option<String>,
    sent: Vec<SentMail>,
}

impl MailGlobal {
    fn new() -> Self {
        MailGlobal { driver: "log".to_string(), from_addr: "noreply@localhost".to_string(), from_name: None, sent: Vec::new() }
    }
}

thread_local! {
    static MAIL_STATE: RefCell<Option<MailGlobal>> = RefCell::new(None);
}

fn mail_ensure() {
    MAIL_STATE.with(|ms| {
        let mut ms = ms.borrow_mut();
        if ms.is_none() {
            *ms = Some(MailGlobal::new());
        }
    });
}

fn validate_email(addr: &str) -> bool {
    // parse "Name <email>" or plain "email"
    let email_part = if let Some(start) = addr.rfind('<') {
        if addr.ends_with('>') { &addr[start+1..addr.len()-1] } else { addr }
    } else { addr };
    // basic: has @, domain has dot after @
    let parts: Vec<&str> = email_part.splitn(2, '@').collect();
    if parts.len() != 2 { return false; }
    let local = parts[0];
    let domain = parts[1];
    if local.is_empty() || domain.is_empty() { return false; }
    if !domain.contains('.') { return false; }
    if domain.starts_with('.') || domain.ends_with('.') { return false; }
    true
}

fn check_address(vm: &mut Vm, addr: &str, fname: &str) -> R<String> {
    if !validate_email(addr) {
        return vm.fail("MailException", format!("{fname}(): invalid email address {:?}", addr));
    }
    Ok(addr.to_string())
}

fn addresses_from_value(vm: &mut Vm, v: Value, fname: &str) -> R<Vec<String>> {
    match v {
        Value::Str(sv) => Ok(vec![check_address(vm, &sv.s, fname)?]),
        Value::List(lst) => {
            let items = lst.borrow().clone();
            let mut out = Vec::new();
            for item in items {
                match item {
                    Value::Str(sv) => out.push(check_address(vm, &sv.s, fname)?),
                    other => return vm.fail("TypeException", format!("{fname}(): email addresses must be strings, got {}", type_name(&other))),
                }
            }
            Ok(out)
        }
        other => vm.fail("TypeException", format!("{fname}(): email address must be a string or list, got {}", type_name(&other))),
    }
}

fn build_message(vm: &mut Vm, content: Value, to: Vec<String>, cc: Vec<String>, bcc: Vec<String>, fname: &str) -> R<MessageState> {
    let msg_rc = Rc::new(RefCell::new(MessageState::new()));
    {
        let mut ms = msg_rc.borrow_mut();
        ms.to = to;
        ms.cc = cc;
        ms.bcc = bcc;
    }
    let msg_val = Value::Message(msg_rc.clone());
    // call build or invoke function
    let is_fn = matches!(content, Value::Func(_) | Value::Native(_) | Value::Bound(_));
    let is_obj_with_build = if let Value::Obj(ref o) = content {
        o.cls.methods.contains_key("build")
    } else { false };
    if is_fn {
        vm.call_value(&content, vec![msg_val])?;
    } else if is_obj_with_build {
        let bound = vm.get_attr(&content, "build")?;
        vm.call_value(&bound, vec![msg_val])?;
    } else {
        return vm.fail("TypeException", format!("{fname}() needs a mailable (an object with a build(Message m) method) or a function, got {}", type_name(&content)));
    }
    {
        let ms = msg_rc.borrow();
        if ms.text.is_none() && ms.html.is_none() {
            return vm.fail("MailException", format!("{fname}(): the message has no body — call text() or html()"));
        }
    }
    let result = msg_rc.borrow().clone_state();
    Ok(result)
}

impl MessageState {
    fn clone_state(&self) -> MessageState {
        MessageState {
            to: self.to.clone(), cc: self.cc.clone(), bcc: self.bcc.clone(),
            reply_to: self.reply_to.clone(), sender: self.sender.clone(),
            subject: self.subject.clone(), text: self.text.clone(), html: self.html.clone(),
            attachments: self.attachments.clone(),
        }
    }
}

fn deliver(vm: &mut Vm, ms: &MessageState) {
    mail_ensure();
    MAIL_STATE.with(|mst| {
        let mut guard = mst.borrow_mut();
        let gs = guard.as_mut().unwrap();
        if gs.driver == "array" {
            let sender = ms.sender.clone().unwrap_or_else(|| {
                if let Some(ref name) = gs.from_name {
                    format!("{} <{}>", name, gs.from_addr)
                } else {
                    gs.from_addr.clone()
                }
            });
            let _ = vm; // vm not needed for array driver
            gs.sent.push(SentMail {
                to: ms.to.clone(), cc: ms.cc.clone(), bcc: ms.bcc.clone(),
                from: sender, subject: ms.subject.clone(),
                text: ms.text.clone(),
                attachments: ms.attachments.iter().map(|(n, _)| n.clone()).collect(),
            });
        }
        // other drivers: no-op in native VM
    });
}

pub fn mail_op(vm: &mut Vm, op: MailOp, args: Vec<Value>) -> R<Value> {
    mail_ensure();
    match op {
        MailOp::Fake => {
            MAIL_STATE.with(|mst| {
                let mut guard = mst.borrow_mut();
                let gs = guard.as_mut().unwrap();
                gs.driver = "array".to_string();
                gs.sent.clear();
            });
            Ok(Value::Null)
        }
        MailOp::Driver => {
            let d = MAIL_STATE.with(|mst| mst.borrow().as_ref().unwrap().driver.clone());
            Ok(s(d))
        }
        MailOp::Sent => {
            let sent = MAIL_STATE.with(|mst| {
                mst.borrow().as_ref().unwrap().sent.iter().map(|m| {
                    let mut om = OMap::new(false);
                    let to_list = list(m.to.iter().map(|x| s(x.clone())).collect());
                    let cc_list = list(m.cc.iter().map(|x| s(x.clone())).collect());
                    let bcc_list = list(m.bcc.iter().map(|x| s(x.clone())).collect());
                    let atts = list(m.attachments.iter().map(|x| s(x.clone())).collect());
                    om.insert(hkey(&s("to"), false).unwrap(), s("to"), to_list);
                    om.insert(hkey(&s("cc"), false).unwrap(), s("cc"), cc_list);
                    om.insert(hkey(&s("bcc"), false).unwrap(), s("bcc"), bcc_list);
                    om.insert(hkey(&s("from"), false).unwrap(), s("from"), s(m.from.clone()));
                    om.insert(hkey(&s("subject"), false).unwrap(), s("subject"), s(m.subject.clone()));
                    om.insert(hkey(&s("text"), false).unwrap(), s("text"), m.text.clone().map(|t| s(t)).unwrap_or(Value::Null));
                    om.insert(hkey(&s("attachments"), false).unwrap(), s("attachments"), atts);
                    Value::Dict(Rc::new(RefCell::new(om)))
                }).collect::<Vec<_>>()
            });
            Ok(list(sent))
        }
        MailOp::To => {
            let v = args.into_iter().next().unwrap_or(Value::Null);
            let addrs = addresses_from_value(vm, v, "Mail.to")?;
            Ok(Value::PendingMail(Rc::new(RefCell::new(PendingMailState { to: addrs, cc: Vec::new(), bcc: Vec::new() }))))
        }
        MailOp::Send => {
            let content = args.into_iter().next().unwrap_or(Value::Null);
            let ms = build_message(vm, content, Vec::new(), Vec::new(), Vec::new(), "Mail.send")?;
            deliver(vm, &ms);
            Ok(Value::Null)
        }
        MailOp::Raw => {
            if args.len() < 2 {
                return vm.fail("TypeException", "Mail.raw() expects 2 arguments");
            }
            let mut args = args;
            let text = match args.remove(0) {
                Value::Str(sv) => sv.s.clone(),
                other => return vm.fail("TypeException", format!("Mail.raw() argument 1 must be string, got {}", type_name(&other))),
            };
            let func = args.remove(0);
            // Build message via func, pre-set text
            let msg_rc = Rc::new(RefCell::new(MessageState::new()));
            msg_rc.borrow_mut().text = Some(text);
            let msg_val = Value::Message(msg_rc.clone());
            let is_fn = matches!(func, Value::Func(_) | Value::Native(_) | Value::Bound(_));
            if !is_fn {
                return vm.fail("TypeException", format!("Mail.raw() needs a function as argument 2, got {}", type_name(&func)));
            }
            vm.call_value(&func, vec![msg_val])?;
            let ms = msg_rc.borrow().clone_state();
            if ms.text.is_none() && ms.html.is_none() {
                return vm.fail("MailException", "Mail.raw(): the message has no body — call text() or html()");
            }
            deliver(vm, &ms);
            Ok(Value::Null)
        }
    }
}

pub fn message_method(vm: &mut Vm, msg: &Rc<RefCell<MessageState>>, name: &str, args: Vec<Value>) -> R<Value> {
    match name {
        "to" | "cc" | "bcc" | "replyTo" => {
            let fname = format!("Message.{}", name);
            let v = args.into_iter().next().unwrap_or(Value::Null);
            let addrs = addresses_from_value(vm, v, &fname)?;
            match name {
                "to" => msg.borrow_mut().to.extend(addrs),
                "cc" => msg.borrow_mut().cc.extend(addrs),
                "bcc" => msg.borrow_mut().bcc.extend(addrs),
                "replyTo" => msg.borrow_mut().reply_to.extend(addrs),
                _ => unreachable!(),
            }
            Ok(Value::Message(msg.clone()))
        }
        "from" => {
            let addr = match args.first() {
                Some(Value::Str(sv)) => sv.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Message.from() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "Message.from() expects at least 1 argument"),
            };
            check_address(vm, &addr, "Message.from")?;
            let name_arg = match args.get(1) {
                Some(Value::Str(sv)) => Some(sv.s.clone()),
                _ => None,
            };
            let formatted = if let Some(ref n) = name_arg {
                format!("{} <{}>", n, addr)
            } else { addr };
            msg.borrow_mut().sender = Some(formatted);
            Ok(Value::Message(msg.clone()))
        }
        "subject" => {
            let subj = match args.first() {
                Some(Value::Str(sv)) => sv.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Message.subject() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "Message.subject() expects 1 argument"),
            };
            msg.borrow_mut().subject = subj;
            Ok(Value::Message(msg.clone()))
        }
        "text" => {
            let txt = match args.first() {
                Some(Value::Str(sv)) => sv.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Message.text() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "Message.text() expects 1 argument"),
            };
            msg.borrow_mut().text = Some(txt);
            Ok(Value::Message(msg.clone()))
        }
        "html" => {
            let html = match args.first() {
                Some(Value::Str(sv)) => sv.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Message.html() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "Message.html() expects 1 argument"),
            };
            msg.borrow_mut().html = Some(html);
            Ok(Value::Message(msg.clone()))
        }
        "attachData" => {
            let data_str = match args.first() {
                Some(Value::Str(sv)) => sv.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Message.attachData() argument 1 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "Message.attachData() expects 2 arguments"),
            };
            let fname = match args.get(1) {
                Some(Value::Str(sv)) => sv.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Message.attachData() argument 2 must be string, got {}", type_name(other))),
                None => return vm.fail("TypeException", "Message.attachData() expects 2 arguments"),
            };
            msg.borrow_mut().attachments.push((fname, data_str.into_bytes()));
            Ok(Value::Message(msg.clone()))
        }
        other => vm.fail("NameException", format!("Message has no method '{}'", other)),
    }
}

pub fn pending_method(vm: &mut Vm, pending: &Rc<RefCell<PendingMailState>>, name: &str, args: Vec<Value>) -> R<Value> {
    match name {
        "cc" => {
            let v = args.into_iter().next().unwrap_or(Value::Null);
            let addrs = addresses_from_value(vm, v, "PendingMail.cc")?;
            pending.borrow_mut().cc.extend(addrs);
            Ok(Value::PendingMail(pending.clone()))
        }
        "bcc" => {
            let v = args.into_iter().next().unwrap_or(Value::Null);
            let addrs = addresses_from_value(vm, v, "PendingMail.bcc")?;
            pending.borrow_mut().bcc.extend(addrs);
            Ok(Value::PendingMail(pending.clone()))
        }
        "send" => {
            let content = args.into_iter().next().unwrap_or(Value::Null);
            let (to, cc, bcc) = {
                let p = pending.borrow();
                (p.to.clone(), p.cc.clone(), p.bcc.clone())
            };
            let ms = build_message(vm, content, to, cc, bcc, "Mail.send")?;
            deliver(vm, &ms);
            Ok(Value::Null)
        }
        "queue" => {
            let content = args.first().cloned().unwrap_or(Value::Null);
            let queue_name = match args.get(1) {
                Some(Value::Str(sv)) => Some(sv.s.clone()),
                _ => None,
            };
            let (to, cc, bcc) = {
                let p = pending.borrow();
                (p.to.clone(), p.cc.clone(), p.bcc.clone())
            };
            let ms = build_message(vm, content, to, cc, bcc, "Mail.queue")?;
            // serialize mail state to a marker dict for queue
            let mail_job = MailJob { ms };
            queue_push_mail(vm, mail_job, queue_name.as_deref(), 0)?;
            Ok(Value::Null)
        }
        other => vm.fail("NameException", format!("PendingMail has no method '{}'", other)),
    }
}

// ===========================================================================
// Queue
// ===========================================================================

struct MailJob {
    ms: MessageState,
}

pub struct JobQueueState {
    pub name: String,
}

#[derive(Clone)]
enum MemJob {
    Object(Value),
    Func(Value),
    Mail(StoredMail),
}

#[derive(Clone)]
struct StoredMail {
    to: Vec<String>, cc: Vec<String>, bcc: Vec<String>,
    sender: Option<String>, subject: String,
    text: Option<String>, html: Option<String>,
    attachments: Vec<(String, Vec<u8>)>,
}

struct MemJobRec {
    id: i64,
    queue: String,
    job: MemJob,
    attempts: i32,
    available_at: f64,
    reserved: bool,
}

struct FailedJobRec {
    id: i64,
    queue: String,
    job: MemJob,
    error: String,
}

struct QueueGlobal {
    driver: String,
    path: String,
    default_queue: String,
    jobs: Vec<MemJobRec>,
    failed: Vec<FailedJobRec>,
    next_id: i64,
    fake: Option<Vec<FakeJob>>,
    db: *mut Sqlite3Queue,
}

#[derive(Clone)]
struct FakeJob {
    job_label: String,
    queue: String,
    delay: i64,
}

unsafe impl Send for QueueGlobal {}

pub enum Sqlite3Queue {}

#[link(name = "sqlite3")]
extern "C" {
    fn sqlite3_open_v2(filename: *const c_char, ppDb: *mut *mut Sqlite3Queue, flags: c_int, zVfs: *const c_char) -> c_int;
    fn sqlite3_close(db: *mut Sqlite3Queue) -> c_int;
    fn sqlite3_exec(db: *mut Sqlite3Queue, sql: *const c_char, cb: Option<unsafe extern "C" fn(*mut c_void, c_int, *mut *mut c_char, *mut *mut c_char) -> c_int>, arg: *mut c_void, errmsg: *mut *mut c_char) -> c_int;
    fn sqlite3_prepare_v2(db: *mut Sqlite3Queue, zSql: *const c_char, nByte: c_int, ppStmt: *mut *mut Sqlite3QueueStmt, pzTail: *mut *const c_char) -> c_int;
    fn sqlite3_step(stmt: *mut Sqlite3QueueStmt) -> c_int;
    fn sqlite3_finalize(stmt: *mut Sqlite3QueueStmt) -> c_int;
    fn sqlite3_column_int64(stmt: *mut Sqlite3QueueStmt, iCol: c_int) -> i64;
    fn sqlite3_column_double(stmt: *mut Sqlite3QueueStmt, iCol: c_int) -> f64;
    fn sqlite3_column_text(stmt: *mut Sqlite3QueueStmt, iCol: c_int) -> *const u8;
    fn sqlite3_last_insert_rowid(db: *mut Sqlite3Queue) -> i64;
    fn sqlite3_changes(db: *mut Sqlite3Queue) -> c_int;
    fn sqlite3_errmsg(db: *mut Sqlite3Queue) -> *const c_char;
    fn sqlite3_bind_null(stmt: *mut Sqlite3QueueStmt, idx: c_int) -> c_int;
    fn sqlite3_bind_int64(stmt: *mut Sqlite3QueueStmt, idx: c_int, val: i64) -> c_int;
    fn sqlite3_bind_double(stmt: *mut Sqlite3QueueStmt, idx: c_int, val: f64) -> c_int;
    fn sqlite3_bind_text(stmt: *mut Sqlite3QueueStmt, idx: c_int, val: *const c_char, n: c_int, cb: *mut c_void) -> c_int;
    fn sqlite3_free(ptr: *mut c_void);
}
pub enum Sqlite3QueueStmt {}

const SQLITE_OK_Q: c_int = 0;
const SQLITE_ROW_Q: c_int = 100;
const SQLITE_DONE_Q: c_int = 101;
const SQLITE_OPEN_READWRITE_Q: c_int = 0x2;
const SQLITE_OPEN_CREATE_Q: c_int = 0x4;
const SQLITE_TRANSIENT_Q: *mut c_void = -1_isize as *mut c_void;

unsafe fn q_errmsg(db: *mut Sqlite3Queue) -> String {
    CStr::from_ptr(sqlite3_errmsg(db)).to_string_lossy().into_owned()
}

unsafe fn q_exec(db: *mut Sqlite3Queue, sql: &str) -> Result<(), String> {
    let cs = match CString::new(sql) { Ok(c) => c, Err(e) => return Err(e.to_string()) };
    let mut errmsg: *mut c_char = std::ptr::null_mut();
    let rc = sqlite3_exec(db, cs.as_ptr(), None, std::ptr::null_mut(), &mut errmsg);
    if rc != SQLITE_OK_Q {
        let msg = if !errmsg.is_null() {
            let sv = CStr::from_ptr(errmsg).to_string_lossy().into_owned();
            sqlite3_free(errmsg as *mut c_void);
            sv
        } else { q_errmsg(db) };
        return Err(msg);
    }
    Ok(())
}

thread_local! {
    static QUEUE_STATE: RefCell<Option<QueueGlobal>> = RefCell::new(None);
}

fn queue_ensure() {
    QUEUE_STATE.with(|qs| {
        let mut guard = qs.borrow_mut();
        if guard.is_none() {
            *guard = Some(QueueGlobal {
                driver: "sync".to_string(),
                path: "storage/queue.db".to_string(),
                default_queue: "default".to_string(),
                jobs: Vec::new(), failed: Vec::new(), next_id: 1,
                fake: None, db: std::ptr::null_mut(),
            });
        }
    });
}

fn queue_open_db(vm: &mut Vm, path: &str) -> R<*mut Sqlite3Queue> {
    let flags = SQLITE_OPEN_READWRITE_Q | SQLITE_OPEN_CREATE_Q;
    let mut db: *mut Sqlite3Queue = std::ptr::null_mut();
    let cs = CString::new(path).map_err(|e| vm.exc("QueueException", format!("invalid path: {}", e)))?;
    let rc = unsafe { sqlite3_open_v2(cs.as_ptr(), &mut db, flags, std::ptr::null()) };
    if rc != SQLITE_OK_Q {
        return vm.fail("QueueException", format!("cannot open queue database {}: rc={}", path, rc));
    }
    let schema = "CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, queue TEXT NOT NULL, \
        payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL, \
        reserved_at REAL, created_at REAL NOT NULL); \
        CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (queue, available_at); \
        CREATE TABLE IF NOT EXISTS failed_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, queue TEXT NOT NULL, \
        payload TEXT NOT NULL, error TEXT NOT NULL, failed_at REAL NOT NULL);";
    if let Err(e) = unsafe { q_exec(db, schema) } {
        return vm.fail("QueueException", format!("cannot init queue database: {}", e));
    }
    Ok(db)
}

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn job_label_mem(job: &MemJob) -> String {
    match job {
        MemJob::Object(Value::Obj(o)) => o.cls.name.to_string(),
        MemJob::Func(_) => "function".to_string(),
        MemJob::Mail(m) => format!("Mail: {}", m.subject),
        _ => "job".to_string(),
    }
}

fn job_label_val(val: &Value) -> String {
    match val {
        Value::Obj(o) => o.cls.name.to_string(),
        Value::Func(_) | Value::Native(_) | Value::Bound(_) => "function".to_string(),
        _ => "job".to_string(),
    }
}

fn job_tries(job: &MemJob) -> i32 {
    if let MemJob::Object(Value::Obj(o)) = job {
        if let Value::Int(n) = o.field("tries") { return n as i32; }
    }
    1
}

fn run_mem_job(vm: &mut Vm, job: &MemJob) -> R<()> {
    match job {
        MemJob::Object(v) => {
            let handle = vm.get_attr(v, "handle")?;
            vm.call_value(&handle, vec![])?;
        }
        MemJob::Func(v) => { vm.call_value(v, vec![])?; }
        MemJob::Mail(m) => {
            let ms = MessageState {
                to: m.to.clone(), cc: m.cc.clone(), bcc: m.bcc.clone(),
                reply_to: Vec::new(), sender: m.sender.clone(),
                subject: m.subject.clone(), text: m.text.clone(), html: m.html.clone(),
                attachments: m.attachments.clone(),
            };
            deliver(vm, &ms);
        }
    }
    Ok(())
}

fn mem_job_from_value(vm: &mut Vm, val: Value, fname: &str) -> R<MemJob> {
    match &val {
        Value::Obj(o) => {
            if !o.cls.methods.contains_key("handle") {
                return vm.fail("TypeException", format!("{fname}() needs a job — an object with a handle() method, or a function — got {}", o.cls.name));
            }
            Ok(MemJob::Object(val))
        }
        Value::Func(_) | Value::Native(_) | Value::Bound(_) => Ok(MemJob::Func(val)),
        other => vm.fail("TypeException", format!("{fname}() needs a job — an object with a handle() method, or a function — got {}", type_name(other))),
    }
}

// serialize a Value for the DB queue payload (JSON)
fn serialize_for_db(vm: &mut Vm, val: &Value, fname: &str) -> R<String> {
    let j = val_to_j(vm, val, fname)?;
    Ok(j_encode(&j))
}

fn val_to_j(vm: &mut Vm, val: &Value, fname: &str) -> R<json::J> {
    match val {
        Value::Null => Ok(json::J::Null),
        Value::Bool(b) => Ok(json::J::Bool(*b)),
        Value::Int(n) => Ok(json::J::Int(*n)),
        Value::Float(f) => Ok(json::J::Num(*f)),
        Value::Str(sv) => Ok(json::J::Str(sv.s.clone())),
        Value::List(lst) => {
            let items = lst.borrow().clone();
            let mut out = Vec::new();
            for item in &items { out.push(val_to_j(vm, item, fname)?); }
            Ok(json::J::Arr(out))
        }
        Value::Dict(dm) => {
            let pairs: Vec<(Value, Value)> = dm.borrow().iter().cloned().collect();
            let mut entries = Vec::new();
            for (k, v) in &pairs {
                let ks = val_to_j(vm, k, fname)?;
                let vs = val_to_j(vm, v, fname)?;
                entries.push((ks, vs));
            }
            let arr: Vec<json::J> = entries.iter().map(|(k, v)| json::J::Arr(vec![k.clone(), v.clone()])).collect();
            Ok(json::J::Obj(vec![("__dict".to_string(), json::J::Arr(arr))]))
        }
        Value::Obj(o) if o.cls.is_enum() => {
            let case_name = crate::builtins::plain_display(&o.field("name"));
            Ok(json::J::Obj(vec![
                ("__enum".to_string(), json::J::Str(o.cls.fqn.to_string())),
                ("case".to_string(), json::J::Str(case_name)),
            ]))
        }
        Value::Obj(o) => {
            let fqn = o.cls.fqn.to_string();
            let field_names: Vec<String> = o.cls.fields.iter().map(|f| f.name.to_string()).collect();
            let field_vals: Vec<Value> = {
                let fields_ref = o.fields.borrow();
                o.cls.fields.iter().enumerate().map(|(i, _)| fields_ref[i].clone()).collect()
            };
            let mut fields_vec = Vec::new();
            for (k, v) in field_names.iter().zip(field_vals.iter()) {
                fields_vec.push((k.clone(), val_to_j(vm, v, fname)?));
            }
            Ok(json::J::Obj(vec![
                ("__obj".to_string(), json::J::Str(fqn)),
                ("fields".to_string(), json::J::Obj(fields_vec)),
            ]))
        }
        other => vm.fail("QueueException", format!("cannot store {} in a queued job ({}) — the database queue keeps ints, floats, strings, bools, lists, dicts, enums and your own objects", type_name(other), fname)),
    }
}

fn deserialize_from_j(vm: &mut Vm, j: &json::J) -> R<Value> {
    match j {
        json::J::Null => Ok(Value::Null),
        json::J::Bool(b) => Ok(Value::Bool(*b)),
        json::J::Int(n) => Ok(Value::Int(*n)),
        json::J::Num(f) => Ok(Value::Float(*f)),
        json::J::Str(sv) => Ok(s(sv.clone())),
        json::J::Arr(items) => {
            let mut out = Vec::new();
            for item in items { out.push(deserialize_from_j(vm, item)?); }
            Ok(list(out))
        }
        json::J::Obj(_) => {
            if let json::J::Arr(entries) = j.get("__dict") {
                let mut om = OMap::new(false);
                for entry in entries {
                    if let json::J::Arr(kv) = entry {
                        let k = deserialize_from_j(vm, kv.get(0).unwrap_or(&json::J::Null))?;
                        let v = deserialize_from_j(vm, kv.get(1).unwrap_or(&json::J::Null))?;
                        om.insert(hkey(&k, false).unwrap(), k, v);
                    }
                }
                return Ok(Value::Dict(Rc::new(RefCell::new(om))));
            }
            if j_has(j, "__enum") {
                let fqn = j.get("__enum").opt_str().unwrap_or_default();
                let case_name = j.get("case").opt_str().unwrap_or_default();
                if let Some(&cls_id) = vm.class_by_name.get(&fqn) {
                    let cls = vm.classes[cls_id].clone();
                    let cases = cls.cases.borrow().clone();
                    for case_val in &cases {
                        if let Value::Obj(o) = case_val {
                            if crate::builtins::plain_display(&o.field("name")) == case_name {
                                return Ok(case_val.clone());
                            }
                        }
                    }
                }
                return vm.fail("QueueException", format!("queued enum case {}::{} not found", fqn, case_name));
            }
            if j_has(j, "__obj") {
                let fqn = j.get("__obj").opt_str().unwrap_or_default();
                if let Some(&cls_id) = vm.class_by_name.get(&fqn) {
                    let cls = vm.classes[cls_id].clone();
                    let obj = vm.bare(&cls)?;
                    if let Value::Obj(ref o) = obj {
                        if let json::J::Obj(ref field_pairs) = j.get("fields") {
                            for (k, v_j) in field_pairs {
                                let v = deserialize_from_j(vm, v_j)?;
                                if let Some(&fi) = o.cls.field_index.get(k.as_str()) {
                                    o.fields.borrow_mut()[fi] = v;
                                }
                            }
                        }
                    }
                    return Ok(obj);
                }
                return vm.fail("QueueException", format!("queued job class {} not found", fqn));
            }
            if j_has(j, "__mail") {
                return Ok(Value::Null); // mail jobs not deserialized here
            }
            // plain object → dict
            let mut om = OMap::new(false);
            if let json::J::Obj(ref pairs) = j {
                for (k, v) in pairs {
                    let v_val = deserialize_from_j(vm, v)?;
                    om.insert(hkey(&s(k.clone()), false).unwrap(), s(k.clone()), v_val);
                }
            }
            Ok(Value::Dict(Rc::new(RefCell::new(om))))
        }
    }
}

fn queue_push_mail(vm: &mut Vm, mail: MailJob, queue_name: Option<&str>, delay: i64) -> R<Option<i64>> {
    queue_ensure();
    let (driver, def_q, is_fake) = QUEUE_STATE.with(|qs| {
        let guard = qs.borrow();
        let gs = guard.as_ref().unwrap();
        (gs.driver.clone(), gs.default_queue.clone(), gs.fake.is_some())
    });
    let qname = queue_name.unwrap_or(&def_q).to_string();
    let subj = mail.ms.subject.clone();
    if is_fake {
        QUEUE_STATE.with(|qs| {
            let mut guard = qs.borrow_mut();
            let gs = guard.as_mut().unwrap();
            gs.fake.as_mut().unwrap().push(FakeJob { job_label: format!("Mail: {}", subj), queue: qname.clone(), delay });
        });
        let idx = QUEUE_STATE.with(|qs| qs.borrow().as_ref().unwrap().fake.as_ref().unwrap().len() as i64);
        return Ok(Some(idx));
    }
    if driver == "sync" {
        deliver(vm, &mail.ms);
        return Ok(None);
    }
    if driver == "memory" {
        let now = now_secs();
        QUEUE_STATE.with(|qs| {
            let mut guard = qs.borrow_mut();
            let gs = guard.as_mut().unwrap();
            let id = gs.next_id; gs.next_id += 1;
            let stored = StoredMail {
                to: mail.ms.to.clone(), cc: mail.ms.cc.clone(), bcc: mail.ms.bcc.clone(),
                sender: mail.ms.sender.clone(), subject: mail.ms.subject.clone(),
                text: mail.ms.text.clone(), html: mail.ms.html.clone(),
                attachments: mail.ms.attachments.clone(),
            };
            gs.jobs.push(MemJobRec { id, queue: qname.clone(), job: MemJob::Mail(stored), attempts: 0, available_at: now + delay as f64, reserved: false });
        });
        let id = QUEUE_STATE.with(|qs| qs.borrow().as_ref().unwrap().next_id - 1);
        return Ok(Some(id));
    }
    // database — not implemented for mail
    vm.fail("QueueException", "Mail.queue(): database driver not supported for mail jobs in native VM")
}

pub fn queue_op(vm: &mut Vm, op: QueueOp, args: Vec<Value>) -> R<Value> {
    queue_ensure();
    match op {
        QueueOp::Driver => {
            let d = QUEUE_STATE.with(|qs| qs.borrow().as_ref().unwrap().driver.clone());
            Ok(s(d))
        }
        QueueOp::Config => {
            let opts = match args.into_iter().next() {
                Some(Value::Dict(dm)) => dm,
                Some(other) => return vm.fail("TypeException", format!("Queue.config() argument 1 must be dict, got {}", type_name(&other))),
                None => return vm.fail("TypeException", "Queue.config() expects 1 argument"),
            };
            let pairs: Vec<(Value, Value)> = opts.borrow().iter().cloned().collect();
            QUEUE_STATE.with(|qs| -> R<()> {
                let mut guard = qs.borrow_mut();
                let gs = guard.as_mut().unwrap();
                // close old db if open
                if !gs.db.is_null() {
                    unsafe { sqlite3_close(gs.db); }
                    gs.db = std::ptr::null_mut();
                }
                for (k, v) in &pairs {
                    let ks = match k { Value::Str(ss) => ss.s.clone(), _ => continue };
                    let vs = match v { Value::Str(ss) => ss.s.clone(), _ => continue };
                    match ks.as_str() {
                        "driver" => gs.driver = vs,
                        "path" => gs.path = vs,
                        "queue" => gs.default_queue = vs,
                        _ => {}
                    }
                }
                Ok(())
            })?;
            Ok(Value::Null)
        }
        QueueOp::Push => {
            let job_val = args.first().cloned().unwrap_or(Value::Null);
            let queue_name = match args.get(1) {
                Some(Value::Str(sv)) => Some(sv.s.clone()),
                _ => None,
            };
            queue_push(vm, job_val, queue_name.as_deref(), 0, "Queue.push")
        }
        QueueOp::Later => {
            if args.len() < 2 { return vm.fail("TypeException", "Queue.later() expects at least 2 arguments"); }
            let delay = match &args[0] {
                Value::Int(n) => *n,
                other => return vm.fail("TypeException", format!("Queue.later() argument 1 must be int, got {}", type_name(other))),
            };
            let job_val = args[1].clone();
            let queue_name = match args.get(2) {
                Some(Value::Str(sv)) => Some(sv.s.clone()),
                _ => None,
            };
            queue_push(vm, job_val, queue_name.as_deref(), delay, "Queue.later")
        }
        QueueOp::On => {
            let qname = match args.into_iter().next() {
                Some(Value::Str(sv)) => sv.s.clone(),
                Some(other) => return vm.fail("TypeException", format!("Queue.on() argument 1 must be string, got {}", type_name(&other))),
                None => return vm.fail("TypeException", "Queue.on() expects 1 argument"),
            };
            Ok(Value::JobQueue(Rc::new(RefCell::new(JobQueueState { name: qname }))))
        }
        QueueOp::Work => {
            let opts = args.first().cloned().unwrap_or(Value::Null);
            queue_work(vm, opts, None)
        }
        QueueOp::Size => {
            let q = match args.first() {
                Some(Value::Str(sv)) => Some(sv.s.clone()),
                _ => None,
            };
            queue_size(vm, q.as_deref())
        }
        QueueOp::Failed => {
            queue_failed(vm)
        }
        QueueOp::Retry => {
            let target = args.into_iter().next().unwrap_or(Value::Null);
            queue_retry(vm, target)
        }
        QueueOp::FlushFailed => {
            queue_flush_failed(vm)
        }
        QueueOp::Clear => {
            let q = match args.first() {
                Some(Value::Str(sv)) => Some(sv.s.clone()),
                _ => None,
            };
            queue_clear(vm, q.as_deref())
        }
        QueueOp::Fake => {
            QUEUE_STATE.with(|qs| {
                let mut guard = qs.borrow_mut();
                let gs = guard.as_mut().unwrap();
                gs.fake = Some(Vec::new());
            });
            Ok(Value::Null)
        }
        QueueOp::Pushed => {
            let fake = QUEUE_STATE.with(|qs| {
                qs.borrow().as_ref().unwrap().fake.clone()
            });
            match fake {
                None => vm.fail("QueueException", "Queue.pushed() only works after Queue.fake()"),
                Some(jobs) => {
                    let items: Vec<Value> = jobs.iter().map(|fj| {
                        let mut om = OMap::new(false);
                        om.insert(hkey(&s("job"), false).unwrap(), s("job"), s(fj.job_label.clone()));
                        om.insert(hkey(&s("queue"), false).unwrap(), s("queue"), s(fj.queue.clone()));
                        om.insert(hkey(&s("delay"), false).unwrap(), s("delay"), Value::Int(fj.delay));
                        Value::Dict(Rc::new(RefCell::new(om)))
                    }).collect();
                    Ok(list(items))
                }
            }
        }
    }
}

fn queue_push(vm: &mut Vm, job_val: Value, queue_name: Option<&str>, delay: i64, fname: &str) -> R<Value> {
    let (driver, def_q, is_fake) = QUEUE_STATE.with(|qs| {
        let guard = qs.borrow();
        let gs = guard.as_ref().unwrap();
        (gs.driver.clone(), gs.default_queue.clone(), gs.fake.is_some())
    });
    let qname = queue_name.unwrap_or(&def_q).to_string();

    if is_fake {
        let label = job_label_val(&job_val);
        // validate job type
        validate_job_for_fake(vm, &job_val, fname)?;
        QUEUE_STATE.with(|qs| {
            let mut guard = qs.borrow_mut();
            let gs = guard.as_mut().unwrap();
            gs.fake.as_mut().unwrap().push(FakeJob { job_label: label, queue: qname, delay });
        });
        let idx = QUEUE_STATE.with(|qs| qs.borrow().as_ref().unwrap().fake.as_ref().unwrap().len() as i64);
        return Ok(Value::Int(idx));
    }

    if driver == "sync" {
        // validate & run immediately
        let mem_job = mem_job_from_value(vm, job_val, fname)?;
        run_mem_job(vm, &mem_job)?;
        return Ok(Value::Null);
    }

    if driver == "memory" {
        let mem_job = mem_job_from_value(vm, job_val, fname)?;
        let now = now_secs();
        QUEUE_STATE.with(|qs| {
            let mut guard = qs.borrow_mut();
            let gs = guard.as_mut().unwrap();
            let id = gs.next_id; gs.next_id += 1;
            gs.jobs.push(MemJobRec { id, queue: qname, job: mem_job, attempts: 0, available_at: now + delay as f64, reserved: false });
        });
        let id = QUEUE_STATE.with(|qs| qs.borrow().as_ref().unwrap().next_id - 1);
        return Ok(Value::Int(id));
    }

    // database driver
    if matches!(job_val, Value::Func(_) | Value::Native(_) | Value::Bound(_)) {
        return vm.fail("QueueException", format!("{fname}(): functions can't be stored in the database queue — use a job class"));
    }
    let mem_job = mem_job_from_value(vm, job_val.clone(), fname)?;
    let payload = serialize_for_db(vm, &job_val, fname)?;
    let db = get_or_open_db(vm)?;
    let now = now_secs();
    let insert_sql = "INSERT INTO jobs (queue, payload, available_at, created_at) VALUES (?, ?, ?, ?)";
    let cs = CString::new(insert_sql).unwrap();
    let (rowid, _) = unsafe {
        let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
        let rc = sqlite3_prepare_v2(db, cs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
        if rc != SQLITE_OK_Q {
            return vm.fail("QueueException", format!("queue db error: {}", q_errmsg(db)));
        }
        let qcs = CString::new(qname.as_str()).unwrap();
        let pcs = CString::new(payload.as_str()).unwrap();
        sqlite3_bind_text(stmt, 1, qcs.as_ptr(), -1, SQLITE_TRANSIENT_Q);
        sqlite3_bind_text(stmt, 2, pcs.as_ptr(), -1, SQLITE_TRANSIENT_Q);
        sqlite3_bind_double(stmt, 3, now + delay as f64);
        sqlite3_bind_double(stmt, 4, now);
        sqlite3_step(stmt);
        let rowid = sqlite3_last_insert_rowid(db);
        sqlite3_finalize(stmt);
        (rowid, 0)
    };
    Ok(Value::Int(rowid))
}

fn validate_job_for_fake(vm: &mut Vm, val: &Value, fname: &str) -> R<()> {
    match val {
        Value::Obj(o) if o.cls.methods.contains_key("handle") => Ok(()),
        Value::Func(_) | Value::Native(_) | Value::Bound(_) => Ok(()),
        Value::Obj(o) => vm.fail("TypeException", format!("{fname}() needs a job — an object with a handle() method, or a function — got {}", o.cls.name)),
        other => vm.fail("TypeException", format!("{fname}() needs a job — an object with a handle() method, or a function — got {}", type_name(other))),
    }
}

fn get_or_open_db(vm: &mut Vm) -> R<*mut Sqlite3Queue> {
    let (db_ptr, path) = QUEUE_STATE.with(|qs| {
        let guard = qs.borrow();
        let gs = guard.as_ref().unwrap();
        (gs.db, gs.path.clone())
    });
    if !db_ptr.is_null() { return Ok(db_ptr); }
    let db = queue_open_db(vm, &path)?;
    QUEUE_STATE.with(|qs| {
        let mut guard = qs.borrow_mut();
        guard.as_mut().unwrap().db = db;
    });
    Ok(db)
}

fn queue_work(vm: &mut Vm, opts: Value, fixed_queue: Option<&str>) -> R<Value> {
    let (driver, def_q) = QUEUE_STATE.with(|qs| {
        let guard = qs.borrow();
        let gs = guard.as_ref().unwrap();
        (gs.driver.clone(), gs.default_queue.clone())
    });
    if driver == "sync" { return Ok(Value::Int(0)); }
    let fake_mode = QUEUE_STATE.with(|qs| qs.borrow().as_ref().unwrap().fake.is_some());
    if fake_mode { return Ok(Value::Int(0)); }

    // parse opts
    let (queues, quiet) = if let Value::Dict(dm) = &opts {
        let pairs: Vec<(Value, Value)> = dm.borrow().iter().cloned().collect();
        let mut qs_list: Option<Vec<String>> = None;
        let mut quiet = false;
        for (k, v) in &pairs {
            if let Value::Str(ks) = k {
                match ks.s.as_str() {
                    "queue" => {
                        match v {
                            Value::Str(sv) => qs_list = Some(vec![sv.s.clone()]),
                            Value::List(lst) => {
                                let items = lst.borrow().clone();
                                qs_list = Some(items.iter().filter_map(|x| if let Value::Str(sv) = x { Some(sv.s.clone()) } else { None }).collect());
                            }
                            _ => {}
                        }
                    }
                    "quiet" => { if let Value::Bool(b) = v { quiet = *b; } }
                    _ => {}
                }
            }
        }
        (qs_list.unwrap_or_else(|| vec![fixed_queue.map(|s| s.to_string()).unwrap_or(def_q.clone())]), quiet)
    } else {
        (vec![fixed_queue.map(|s| s.to_string()).unwrap_or(def_q.clone())], false)
    };

    let mut done = 0i64;
    if driver == "memory" {
        loop {
            // reserve next job
            let now = now_secs();
            let rec_idx = QUEUE_STATE.with(|qs| {
                let guard = qs.borrow();
                let gs = guard.as_ref().unwrap();
                gs.jobs.iter().position(|r| queues.contains(&r.queue) && r.available_at <= now && !r.reserved)
            });
            let Some(idx) = rec_idx else { break };
            // mark reserved and increment attempts
            QUEUE_STATE.with(|qs| {
                let mut guard = qs.borrow_mut();
                let gs = guard.as_mut().unwrap();
                gs.jobs[idx].reserved = true;
                gs.jobs[idx].attempts += 1;
            });
            // get a copy of the job to run
            let (id, job_clone, queue_name, attempts) = QUEUE_STATE.with(|qs| {
                let guard = qs.borrow();
                let gs = guard.as_ref().unwrap();
                let r = &gs.jobs[idx];
                (r.id, r.job.clone(), r.queue.clone(), r.attempts)
            });
            done += 1;
            match run_mem_job(vm, &job_clone) {
                Ok(_) => {
                    // remove from jobs
                    QUEUE_STATE.with(|qs| {
                        let mut guard = qs.borrow_mut();
                        let gs = guard.as_mut().unwrap();
                        gs.jobs.retain(|r| r.id != id);
                    });
                }
                Err(Flow::Throw(exc)) => {
                    let tries = job_tries(&job_clone);
                    let error_msg = {
                        let name = exc.cls.name.clone();
                        let msg = crate::builtins::plain_display(&exc.field("message"));
                        format!("{}: {}", name, msg)
                    };
                    if attempts < tries {
                        // retry: unreserve
                        QUEUE_STATE.with(|qs| {
                            let mut guard = qs.borrow_mut();
                            let gs = guard.as_mut().unwrap();
                            if let Some(r) = gs.jobs.iter_mut().find(|r| r.id == id) {
                                r.reserved = false;
                                r.available_at = now_secs();
                            }
                        });
                    } else {
                        // permanent failure
                        QUEUE_STATE.with(|qs| {
                            let mut guard = qs.borrow_mut();
                            let gs = guard.as_mut().unwrap();
                            gs.jobs.retain(|r| r.id != id);
                            let failed_id = gs.failed.len() as i64 + 1;
                            gs.failed.push(FailedJobRec { id: failed_id, queue: queue_name.clone(), job: job_clone.clone(), error: error_msg.clone() });
                        });
                        // call failed() if available
                        if let MemJob::Object(Value::Obj(o)) = &job_clone {
                            if o.cls.methods.contains_key("failed") {
                                let obj_val = Value::Obj(o.clone());
                                if let Ok(bound) = vm.get_attr(&obj_val, "failed") {
                                    let _ = vm.call_value(&bound, vec![Value::Obj(exc)]);
                                }
                            }
                        }
                    }
                }
                Err(other) => return Err(other),
            }
        }
        return Ok(Value::Int(done));
    }

    // database driver
    let db = get_or_open_db(vm)?;
    loop {
        let now = now_secs();
        let marks: String = queues.iter().enumerate().map(|(i, _)| if i == 0 { "?".to_string() } else { ",?".to_string() }).collect();
        let select_sql = format!("SELECT id, queue, payload, attempts FROM jobs WHERE queue IN ({marks}) AND available_at <= ? AND (reserved_at IS NULL OR reserved_at < ?) ORDER BY id LIMIT 1");
        let cs = CString::new(select_sql.as_str()).unwrap();
        let (row_id, row_queue, row_payload, row_attempts) = unsafe {
            let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
            let rc = sqlite3_prepare_v2(db, cs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
            if rc != SQLITE_OK_Q { break; }
            let mut bind_idx = 1i32;
            for q in &queues {
                let qcs = CString::new(q.as_str()).unwrap();
                sqlite3_bind_text(stmt, bind_idx, qcs.as_ptr(), -1, SQLITE_TRANSIENT_Q);
                bind_idx += 1;
            }
            sqlite3_bind_double(stmt, bind_idx, now);
            sqlite3_bind_double(stmt, bind_idx + 1, now - 600.0);
            let rc2 = sqlite3_step(stmt);
            if rc2 != SQLITE_ROW_Q {
                sqlite3_finalize(stmt);
                break;
            }
            let rid = sqlite3_column_int64(stmt, 0);
            let rq_ptr = sqlite3_column_text(stmt, 1);
            let rq = if rq_ptr.is_null() { String::new() } else { CStr::from_ptr(rq_ptr as *const c_char).to_string_lossy().into_owned() };
            let rp_ptr = sqlite3_column_text(stmt, 2);
            let rp = if rp_ptr.is_null() { String::new() } else { CStr::from_ptr(rp_ptr as *const c_char).to_string_lossy().into_owned() };
            let ra = sqlite3_column_int64(stmt, 3);
            sqlite3_finalize(stmt);
            (rid, rq, rp, ra)
        };
        // reserve
        let upd_sql = "UPDATE jobs SET reserved_at = ?, attempts = attempts + 1 WHERE id = ?";
        let ucs = CString::new(upd_sql).unwrap();
        unsafe {
            let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
            sqlite3_prepare_v2(db, ucs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
            sqlite3_bind_double(stmt, 1, now);
            sqlite3_bind_int64(stmt, 2, row_id);
            sqlite3_step(stmt);
            sqlite3_finalize(stmt);
        }
        // deserialize payload
        let j = json::parse(&row_payload).unwrap_or(json::J::Null);
        let job_val = deserialize_from_j(vm, &j)?;
        let attempts = row_attempts + 1;
        done += 1;
        // Run job
        let run_result = match &job_val {
            Value::Obj(o) if o.cls.methods.contains_key("handle") => {
                let handle = vm.get_attr(&job_val, "handle")?;
                vm.call_value(&handle, vec![])
            }
            _ => Ok(Value::Null),
        };
        match run_result {
            Ok(_) => {
                let del_sql = "DELETE FROM jobs WHERE id = ?";
                let dcs = CString::new(del_sql).unwrap();
                unsafe {
                    let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
                    sqlite3_prepare_v2(db, dcs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
                    sqlite3_bind_int64(stmt, 1, row_id);
                    sqlite3_step(stmt);
                    sqlite3_finalize(stmt);
                }
            }
            Err(Flow::Throw(exc)) => {
                let tries = if let Value::Obj(ref o) = job_val {
                    if let Value::Int(n) = o.field("tries") { n as i32 } else { 1 }
                } else { 1 };
                let error_msg = {
                    let name = exc.cls.name.clone();
                    let msg = crate::builtins::plain_display(&exc.field("message"));
                    format!("{}: {}", name, msg)
                };
                if attempts < tries as i64 {
                    let upd2 = "UPDATE jobs SET reserved_at = NULL, available_at = ? WHERE id = ?";
                    let u2cs = CString::new(upd2).unwrap();
                    unsafe {
                        let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
                        sqlite3_prepare_v2(db, u2cs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
                        sqlite3_bind_double(stmt, 1, now_secs());
                        sqlite3_bind_int64(stmt, 2, row_id);
                        sqlite3_step(stmt);
                        sqlite3_finalize(stmt);
                    }
                } else {
                    let del_sql = "DELETE FROM jobs WHERE id = ?";
                    let dcs = CString::new(del_sql).unwrap();
                    unsafe {
                        let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
                        sqlite3_prepare_v2(db, dcs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
                        sqlite3_bind_int64(stmt, 1, row_id);
                        sqlite3_step(stmt);
                        sqlite3_finalize(stmt);
                    }
                    let ins_failed = "INSERT INTO failed_jobs (queue, payload, error, failed_at) VALUES (?, ?, ?, ?)";
                    let ics = CString::new(ins_failed).unwrap();
                    unsafe {
                        let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
                        sqlite3_prepare_v2(db, ics.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
                        let qcs = CString::new(row_queue.as_str()).unwrap();
                        let pcs = CString::new(row_payload.as_str()).unwrap();
                        let ecs = CString::new(error_msg.as_str()).unwrap();
                        sqlite3_bind_text(stmt, 1, qcs.as_ptr(), -1, SQLITE_TRANSIENT_Q);
                        sqlite3_bind_text(stmt, 2, pcs.as_ptr(), -1, SQLITE_TRANSIENT_Q);
                        sqlite3_bind_text(stmt, 3, ecs.as_ptr(), -1, SQLITE_TRANSIENT_Q);
                        sqlite3_bind_double(stmt, 4, now_secs());
                        sqlite3_step(stmt);
                        sqlite3_finalize(stmt);
                    }
                    // call failed() if available
                    if let Value::Obj(ref o) = job_val {
                        if o.cls.methods.contains_key("failed") {
                            if let Ok(bound) = vm.get_attr(&job_val, "failed") {
                                let _ = vm.call_value(&bound, vec![Value::Obj(exc)]);
                            }
                        }
                    }
                }
            }
            Err(other) => return Err(other),
        }
    }
    Ok(Value::Int(done))
}

fn queue_size(vm: &mut Vm, queue_name: Option<&str>) -> R<Value> {
    let (driver, def_q) = QUEUE_STATE.with(|qs| {
        let guard = qs.borrow();
        let gs = guard.as_ref().unwrap();
        (gs.driver.clone(), gs.default_queue.clone())
    });
    let qname = queue_name.unwrap_or(&def_q).to_string();
    if driver == "sync" { return Ok(Value::Int(0)); }
    if driver == "memory" {
        let count = QUEUE_STATE.with(|qs| {
            qs.borrow().as_ref().unwrap().jobs.iter().filter(|r| r.queue == qname).count() as i64
        });
        return Ok(Value::Int(count));
    }
    let db = get_or_open_db(vm)?;
    let count = unsafe {
        let sql = "SELECT COUNT(*) FROM jobs WHERE queue = ?";
        let cs = CString::new(sql).unwrap();
        let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
        sqlite3_prepare_v2(db, cs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
        let qcs = CString::new(qname.as_str()).unwrap();
        sqlite3_bind_text(stmt, 1, qcs.as_ptr(), -1, SQLITE_TRANSIENT_Q);
        sqlite3_step(stmt);
        let n = sqlite3_column_int64(stmt, 0);
        sqlite3_finalize(stmt);
        n
    };
    Ok(Value::Int(count))
}

fn queue_clear(vm: &mut Vm, queue_name: Option<&str>) -> R<Value> {
    let (driver, def_q) = QUEUE_STATE.with(|qs| {
        let guard = qs.borrow();
        let gs = guard.as_ref().unwrap();
        (gs.driver.clone(), gs.default_queue.clone())
    });
    let qname = queue_name.unwrap_or(&def_q).to_string();
    if driver == "sync" { return Ok(Value::Int(0)); }
    if driver == "memory" {
        let before = QUEUE_STATE.with(|qs| qs.borrow().as_ref().unwrap().jobs.len() as i64);
        QUEUE_STATE.with(|qs| {
            let mut guard = qs.borrow_mut();
            guard.as_mut().unwrap().jobs.retain(|r| r.queue != qname);
        });
        let after = QUEUE_STATE.with(|qs| qs.borrow().as_ref().unwrap().jobs.len() as i64);
        return Ok(Value::Int(before - after));
    }
    let db = get_or_open_db(vm)?;
    let changes = unsafe {
        let sql = "DELETE FROM jobs WHERE queue = ?";
        let cs = CString::new(sql).unwrap();
        let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
        sqlite3_prepare_v2(db, cs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
        let qcs = CString::new(qname.as_str()).unwrap();
        sqlite3_bind_text(stmt, 1, qcs.as_ptr(), -1, SQLITE_TRANSIENT_Q);
        sqlite3_step(stmt);
        let n = sqlite3_changes(db) as i64;
        sqlite3_finalize(stmt);
        n
    };
    Ok(Value::Int(changes))
}

fn queue_failed(vm: &mut Vm) -> R<Value> {
    let driver = QUEUE_STATE.with(|qs| qs.borrow().as_ref().unwrap().driver.clone());
    if driver == "sync" { return Ok(list(Vec::new())); }
    if driver == "memory" {
        let items: Vec<Value> = QUEUE_STATE.with(|qs| {
            qs.borrow().as_ref().unwrap().failed.iter().map(|f| {
                let mut om = OMap::new(false);
                om.insert(hkey(&s("id"), false).unwrap(), s("id"), Value::Int(f.id));
                om.insert(hkey(&s("queue"), false).unwrap(), s("queue"), s(f.queue.clone()));
                om.insert(hkey(&s("job"), false).unwrap(), s("job"), s(job_label_mem(&f.job)));
                om.insert(hkey(&s("error"), false).unwrap(), s("error"), s(f.error.clone()));
                Value::Dict(Rc::new(RefCell::new(om)))
            }).collect()
        });
        return Ok(list(items));
    }
    let db = get_or_open_db(vm)?;
    let rows = unsafe {
        let sql = "SELECT id, queue, payload, error FROM failed_jobs ORDER BY id";
        let cs = CString::new(sql).unwrap();
        let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
        sqlite3_prepare_v2(db, cs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
        let mut rows: Vec<(i64, String, String, String)> = Vec::new();
        loop {
            let rc = sqlite3_step(stmt);
            if rc != SQLITE_ROW_Q { break; }
            let id = sqlite3_column_int64(stmt, 0);
            let qptr = sqlite3_column_text(stmt, 1);
            let q = if qptr.is_null() { String::new() } else { CStr::from_ptr(qptr as *const c_char).to_string_lossy().into_owned() };
            let pptr = sqlite3_column_text(stmt, 2);
            let payload = if pptr.is_null() { String::new() } else { CStr::from_ptr(pptr as *const c_char).to_string_lossy().into_owned() };
            let eptr = sqlite3_column_text(stmt, 3);
            let err = if eptr.is_null() { String::new() } else { CStr::from_ptr(eptr as *const c_char).to_string_lossy().into_owned() };
            rows.push((id, q, payload, err));
        }
        sqlite3_finalize(stmt);
        rows
    };
    let items: Vec<Value> = rows.iter().map(|(id, q, payload, err)| {
        let label = match json::parse(payload) {
            Ok(ref j) if j_has(j, "__obj") => {
                let fqn = j.get("__obj").opt_str().unwrap_or_default();
                fqn.rsplit('\\').next().unwrap_or(&fqn).to_string()
            }
            Ok(ref j) if j_has(j, "__mail") => "Mail".to_string(),
            _ => "job".to_string(),
        };
        let mut om = OMap::new(false);
        om.insert(hkey(&s("id"), false).unwrap(), s("id"), Value::Int(*id));
        om.insert(hkey(&s("queue"), false).unwrap(), s("queue"), s(q.clone()));
        om.insert(hkey(&s("job"), false).unwrap(), s("job"), s(label));
        om.insert(hkey(&s("error"), false).unwrap(), s("error"), s(err.clone()));
        Value::Dict(Rc::new(RefCell::new(om)))
    }).collect();
    Ok(list(items))
}

fn queue_retry(vm: &mut Vm, target: Value) -> R<Value> {
    let driver = QUEUE_STATE.with(|qs| qs.borrow().as_ref().unwrap().driver.clone());
    if driver == "sync" { return Ok(Value::Int(0)); }
    let every = matches!(&target, Value::Str(sv) if sv.s == "all");
    let target_id = if !every {
        match target { Value::Int(n) => n, _ => return vm.fail("TypeException", "Queue.retry() expects an id (int) or \"all\"") }
    } else { 0 };
    if driver == "memory" {
        let now = now_secs();
        let back_jobs: Vec<FailedJobRec> = QUEUE_STATE.with(|qs| {
            let mut guard = qs.borrow_mut();
            let gs = guard.as_mut().unwrap();
            let mut back = Vec::new();
            gs.failed.retain(|f| {
                if every || f.id == target_id {
                    back.push(FailedJobRec { id: f.id, queue: f.queue.clone(), job: f.job.clone(), error: f.error.clone() });
                    false
                } else { true }
            });
            back
        });
        let count = back_jobs.len() as i64;
        QUEUE_STATE.with(|qs| {
            let mut guard = qs.borrow_mut();
            let gs = guard.as_mut().unwrap();
            for f in back_jobs {
                gs.jobs.push(MemJobRec { id: f.id, queue: f.queue, job: f.job, attempts: 0, available_at: now, reserved: false });
            }
        });
        return Ok(Value::Int(count));
    }
    let db = get_or_open_db(vm)?;
    let now = now_secs();
    let count = unsafe {
        let select_sql = if every {
            "SELECT id, queue, payload FROM failed_jobs".to_string()
        } else {
            "SELECT id, queue, payload FROM failed_jobs WHERE id = ?".to_string()
        };
        let cs = CString::new(select_sql.as_str()).unwrap();
        let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
        sqlite3_prepare_v2(db, cs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
        if !every { sqlite3_bind_int64(stmt, 1, target_id); }
        let mut rows: Vec<(i64, String, String)> = Vec::new();
        loop {
            let rc = sqlite3_step(stmt);
            if rc != SQLITE_ROW_Q { break; }
            let fid = sqlite3_column_int64(stmt, 0);
            let qptr = sqlite3_column_text(stmt, 1);
            let q = if qptr.is_null() { String::new() } else { CStr::from_ptr(qptr as *const c_char).to_string_lossy().into_owned() };
            let pptr = sqlite3_column_text(stmt, 2);
            let p = if pptr.is_null() { String::new() } else { CStr::from_ptr(pptr as *const c_char).to_string_lossy().into_owned() };
            rows.push((fid, q, p));
        }
        sqlite3_finalize(stmt);
        for (fid, q, payload) in &rows {
            let ins_sql = "INSERT INTO jobs (queue, payload, available_at, created_at) VALUES (?, ?, ?, ?)";
            let ics = CString::new(ins_sql).unwrap();
            let mut ins_stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
            sqlite3_prepare_v2(db, ics.as_ptr(), -1, &mut ins_stmt, std::ptr::null_mut());
            let qcs = CString::new(q.as_str()).unwrap();
            let pcs = CString::new(payload.as_str()).unwrap();
            sqlite3_bind_text(ins_stmt, 1, qcs.as_ptr(), -1, SQLITE_TRANSIENT_Q);
            sqlite3_bind_text(ins_stmt, 2, pcs.as_ptr(), -1, SQLITE_TRANSIENT_Q);
            sqlite3_bind_double(ins_stmt, 3, now);
            sqlite3_bind_double(ins_stmt, 4, now);
            sqlite3_step(ins_stmt);
            sqlite3_finalize(ins_stmt);
            let del_sql = "DELETE FROM failed_jobs WHERE id = ?";
            let dcs = CString::new(del_sql).unwrap();
            let mut del_stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
            sqlite3_prepare_v2(db, dcs.as_ptr(), -1, &mut del_stmt, std::ptr::null_mut());
            sqlite3_bind_int64(del_stmt, 1, *fid);
            sqlite3_step(del_stmt);
            sqlite3_finalize(del_stmt);
        }
        rows.len() as i64
    };
    Ok(Value::Int(count))
}

fn queue_flush_failed(vm: &mut Vm) -> R<Value> {
    let driver = QUEUE_STATE.with(|qs| qs.borrow().as_ref().unwrap().driver.clone());
    if driver == "sync" { return Ok(Value::Int(0)); }
    if driver == "memory" {
        let count = QUEUE_STATE.with(|qs| {
            let mut guard = qs.borrow_mut();
            let gs = guard.as_mut().unwrap();
            let n = gs.failed.len() as i64;
            gs.failed.clear();
            n
        });
        return Ok(Value::Int(count));
    }
    let db = get_or_open_db(vm)?;
    let count = unsafe {
        let sql = "DELETE FROM failed_jobs";
        let cs = CString::new(sql).unwrap();
        let mut stmt: *mut Sqlite3QueueStmt = std::ptr::null_mut();
        sqlite3_prepare_v2(db, cs.as_ptr(), -1, &mut stmt, std::ptr::null_mut());
        sqlite3_step(stmt);
        let n = sqlite3_changes(db) as i64;
        sqlite3_finalize(stmt);
        n
    };
    Ok(Value::Int(count))
}

pub fn job_queue_method(vm: &mut Vm, jq: &Rc<RefCell<JobQueueState>>, name: &str, args: Vec<Value>) -> R<Value> {
    let qname = jq.borrow().name.clone();
    match name {
        "push" => {
            let job_val = args.into_iter().next().unwrap_or(Value::Null);
            queue_push(vm, job_val, Some(&qname), 0, "JobQueue.push")
        }
        "later" => {
            if args.len() < 2 { return vm.fail("TypeException", "JobQueue.later() expects 2 arguments"); }
            let delay = match &args[0] {
                Value::Int(n) => *n,
                other => return vm.fail("TypeException", format!("JobQueue.later() argument 1 must be int, got {}", type_name(other))),
            };
            let job_val = args[1].clone();
            queue_push(vm, job_val, Some(&qname), delay, "JobQueue.later")
        }
        "size" => queue_size(vm, Some(&qname)),
        "clear" => queue_clear(vm, Some(&qname)),
        "work" => {
            let opts = args.into_iter().next().unwrap_or(Value::Null);
            queue_work(vm, opts, Some(&qname))
        }
        "name" => Ok(s(qname)),
        other => vm.fail("NameException", format!("JobQueue has no method '{}'", other)),
    }
}
