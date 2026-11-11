//! A small JSON reader for the syntax tree that `barq/native.py` writes.

#[derive(Debug, Clone)]
pub enum J {
    Null,
    Bool(bool),
    Int(i64),
    Num(f64),
    Str(String),
    Arr(Vec<J>),
    Obj(Vec<(String, J)>),
}

impl J {
    pub fn get(&self, key: &str) -> &J {
        static NULL: J = J::Null;
        if let J::Obj(fields) = self {
            for (k, v) in fields {
                if k == key {
                    return v;
                }
            }
        }
        &NULL
    }
    pub fn is_null(&self) -> bool {
        matches!(self, J::Null)
    }
    pub fn str(&self) -> &str {
        match self {
            J::Str(s) => s,
            _ => "",
        }
    }
    pub fn opt_str(&self) -> Option<String> {
        match self {
            J::Str(s) => Some(s.clone()),
            _ => None,
        }
    }
    pub fn int(&self) -> i64 {
        match self {
            J::Int(n) => *n,
            J::Num(f) => *f as i64,
            _ => 0,
        }
    }
    pub fn bool(&self) -> bool {
        matches!(self, J::Bool(true))
    }
    pub fn arr(&self) -> &[J] {
        match self {
            J::Arr(a) => a,
            _ => &[],
        }
    }
}

pub fn parse(text: &str) -> Result<J, String> {
    let mut p = Parser { s: text.as_bytes(), i: 0 };
    let v = p.value()?;
    p.ws();
    if p.i != p.s.len() {
        return Err(format!("trailing data at byte {}", p.i));
    }
    Ok(v)
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\n' | b'\r' | b'\t') {
            self.i += 1;
        }
    }

    fn err<T>(&self, what: &str) -> Result<T, String> {
        // Compute 1-based line and column
        let before = &self.s[..self.i.min(self.s.len())];
        let line = before.iter().filter(|&&b| b == b'\n').count() + 1;
        let last_nl = before.iter().rposition(|&b| b == b'\n').map(|p| p + 1).unwrap_or(0);
        let col = self.i - last_nl + 1;
        Err(format!("{} at line {}, column {}", what, line, col))
    }

    fn value(&mut self) -> Result<J, String> {
        self.ws();
        if self.i >= self.s.len() {
            return self.err("Expecting value");
        }
        match self.s[self.i] {
            b'{' => {
                self.i += 1;
                let mut fields = Vec::new();
                self.ws();
                if self.peek() == b'}' {
                    self.i += 1;
                    return Ok(J::Obj(fields));
                }
                loop {
                    self.ws();
                    let k = self.string()?;
                    self.ws();
                    if self.peek() != b':' {
                        return self.err("Expecting ':' delimiter");
                    }
                    self.i += 1;
                    let v = self.value()?;
                    fields.push((k, v));
                    self.ws();
                    match self.peek() {
                        b',' => self.i += 1,
                        b'}' => {
                            self.i += 1;
                            return Ok(J::Obj(fields));
                        }
                        _ => return self.err("Expecting ',' delimiter or '}'"),
                    }
                }
            }
            b'[' => {
                self.i += 1;
                let mut items = Vec::new();
                self.ws();
                if self.peek() == b']' {
                    self.i += 1;
                    return Ok(J::Arr(items));
                }
                loop {
                    items.push(self.value()?);
                    self.ws();
                    match self.peek() {
                        b',' => self.i += 1,
                        b']' => {
                            self.i += 1;
                            return Ok(J::Arr(items));
                        }
                        _ => return self.err("Expecting ',' delimiter or ']'"),
                    }
                }
            }
            b'"' => Ok(J::Str(self.string()?)),
            b't' => self.word("true", J::Bool(true)),
            b'f' => self.word("false", J::Bool(false)),
            b'n' => self.word("null", J::Null),
            _ => self.number(),
        }
    }

    fn peek(&self) -> u8 {
        if self.i < self.s.len() {
            self.s[self.i]
        } else {
            0
        }
    }

    fn word(&mut self, w: &str, v: J) -> Result<J, String> {
        if self.s[self.i..].starts_with(w.as_bytes()) {
            self.i += w.len();
            Ok(v)
        } else {
            self.err("Expecting value")
        }
    }

    fn number(&mut self) -> Result<J, String> {
        let start = self.i;
        let mut float = false;
        while self.i < self.s.len() {
            match self.s[self.i] {
                b'0'..=b'9' | b'-' | b'+' => {}
                b'.' | b'e' | b'E' => float = true,
                _ => break,
            }
            self.i += 1;
        }
        let text = std::str::from_utf8(&self.s[start..self.i]).unwrap();
        if !float {
            if let Ok(n) = text.parse::<i64>() {
                return Ok(J::Int(n));
            }
        }
        if text.is_empty() {
            return self.err("Expecting value");
        }
        match text.parse::<f64>() {
            Ok(f) => Ok(J::Num(f)),
            Err(_) => self.err("Expecting value"),
        }
    }

    fn string(&mut self) -> Result<String, String> {
        if self.peek() != b'"' {
            return self.err("expected string");
        }
        self.i += 1;
        let mut out: Vec<u8> = Vec::new();
        loop {
            if self.i >= self.s.len() {
                return self.err("unterminated string");
            }
            let c = self.s[self.i];
            self.i += 1;
            match c {
                b'"' => break,
                b'\\' => {
                    let e = self.peek();
                    self.i += 1;
                    match e {
                        b'"' => out.push(b'"'),
                        b'\\' => out.push(b'\\'),
                        b'/' => out.push(b'/'),
                        b'b' => out.push(8),
                        b'f' => out.push(12),
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'u' => {
                            let mut cp = self.hex4()?;
                            if (0xD800..0xDC00).contains(&cp) && self.s[self.i..].starts_with(b"\\u") {
                                self.i += 2;
                                let lo = self.hex4()?;
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                            }
                            let ch = char::from_u32(cp).unwrap_or('\u{FFFD}');
                            let mut buf = [0u8; 4];
                            out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                        }
                        _ => return self.err("bad escape"),
                    }
                }
                _ => out.push(c),
            }
        }
        String::from_utf8(out).or_else(|_| self.err("bad UTF-8"))
    }

    fn hex4(&mut self) -> Result<u32, String> {
        if self.i + 4 > self.s.len() {
            return self.err("short \\u escape");
        }
        let text = std::str::from_utf8(&self.s[self.i..self.i + 4]).unwrap_or("");
        self.i += 4;
        u32::from_str_radix(text, 16).or_else(|_| self.err("bad \\u escape"))
    }
}
