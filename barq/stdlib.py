"""Standard library modules, batch 1 (v0.3.0):
Json, IO, Path, Env, Hash, Crypto, Uuid, Regex, Date, Arr, Log, Cache.

Every module is a NativeModule registered as a global (so `Json.encode(...)`
works directly) and is also reachable as `use Barq\\Json`.
All of it uses only the Python standard library.
"""

import base64
import calendar
import datetime as dt
import hashlib
import hmac as _hmac
import json
import math
import os
import re
import secrets
import sys
import tempfile
import time
import uuid

from .builtins import LIST_METHODS, VARIADIC, call, need, need_bool
from .runtime import BarqObject, BarqSet, NativeFunction, NativeModule, to_display, type_name, values_equal

# Exception classes added by this module: (name, parent)
STDLIB_EXCEPTIONS = [
    ("JsonException", "ValueException"),
    ("RegexException", "ValueException"),
    ("DateException", "ValueException"),
    ("CryptoException", "Exception"),
]


def module(name, table):
    """table: {member: (fn(i, args, node), min_args, max_args) | constant}"""
    members = {}
    for key, spec in table.items():
        if isinstance(spec, tuple) and callable(spec[0]):
            fn, lo, hi = spec
            members[key] = NativeFunction(f"{name}.{key}", fn, lo, hi)
        else:
            members[key] = spec
    return NativeModule(name, members)


def arg(i, a, k, kinds, fname, n, default=None):
    """Argument k (0-based) with a type check, or default when it's missing."""
    if len(a) <= k:
        return default
    return need(i, a[k], kinds, fname, k + 1, n)


# ===========================================================================
# Json
# ===========================================================================

def to_json_value(i, v, n, seen=None):
    """Barq value -> plain Python value json.dumps understands."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            i.throw("JsonException", f"cannot encode {to_display(v)} as JSON", n)
        return v
    seen = seen or set()
    if id(v) in seen:
        i.throw("JsonException", "cannot encode a value that contains itself", n)
    seen = seen | {id(v)}
    if isinstance(v, (list, range)):
        return [to_json_value(i, x, n, seen) for x in v]
    if isinstance(v, BarqSet):
        return [to_json_value(i, x, n, seen) for x in v.values()]
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if not isinstance(k, str) or isinstance(k, bool):
                i.throw("JsonException", f"JSON object keys must be strings, found {type_name(k)} {to_display(k, i, True)}", n)
            out[k] = to_json_value(i, x, n, seen)
        return out
    if isinstance(v, BarqObject) and v.cls.is_enum:
        if v.cls.backing is None:
            i.throw("JsonException", f"cannot encode {v.cls.name}::{v.fields['name']} — only enums with int or string values can be encoded", n)
        return v.fields["value"]
    if isinstance(v, BarqObject):
        from .runtime import BoundMethod
        m = v.cls.find_method("toJson")
        if m is not None:
            return to_json_value(i, i.call_function(BoundMethod(v, m), [], n), n, seen)
        m = v.cls.find_method("toArray")    # models and resources
        if m is not None and (getattr(m, "params", None) == [] or getattr(m, "max_args", 1) == 0):
            return to_json_value(i, i.call_function(BoundMethod(v, m), [], n), n, seen)
        out = {}
        for cls in v.cls.chain():
            for name, p in cls.props.items():
                if p.visibility == "public":
                    out[name] = to_json_value(i, v.fields.get(name), n, seen)
        return out
    i.throw("JsonException", f"cannot encode {type_name(v)} as JSON", n)


def json_encode(i, a, n):
    pretty = arg(i, a, 1, ("bool",), "Json.encode", n, False)
    data = to_json_value(i, a[0], n)
    if pretty:
        return json.dumps(data, ensure_ascii=False, indent=4)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def json_pretty(i, a, n):
    return json.dumps(to_json_value(i, a[0], n), ensure_ascii=False, indent=4)


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def json_decode(i, a, n):
    text = need(i, a[0], ("string",), "Json.decode", 1, n)
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except ValueError as e:
        msg = e.msg if isinstance(e, json.JSONDecodeError) else str(e)
        where = f" at line {e.lineno}, column {e.colno}" if isinstance(e, json.JSONDecodeError) else ""
        i.throw("JsonException", f"invalid JSON: {msg}{where}", n)


def json_valid(i, a, n):
    text = need(i, a[0], ("string",), "Json.valid", 1, n)
    try:
        json.loads(text, parse_constant=_reject_constant)
        return True
    except ValueError:
        return False


JSON = {
    "encode": (json_encode, 1, 2),
    "decode": (json_decode, 1, 1),
    "pretty": (json_pretty, 1, 1),
    "valid": (json_valid, 1, 1),
}


# ===========================================================================
# IO  (paths are relative to the current working directory)
# ===========================================================================

def io_call(i, n, what, path, fn):
    try:
        return fn()
    except FileNotFoundError:
        i.throw("IOException", f"{what}: no such file or folder: {path}", n)
    except IsADirectoryError:
        i.throw("IOException", f"{what}: {path} is a folder, not a file", n)
    except NotADirectoryError:
        i.throw("IOException", f"{what}: {path} is not a folder", n)
    except PermissionError:
        i.throw("IOException", f"{what}: permission denied: {path}", n)
    except FileExistsError:
        i.throw("IOException", f"{what}: already exists: {path}", n)
    except UnicodeDecodeError:
        i.throw("IOException", f"{what}: {path} is not UTF-8 text", n)
    except OSError as e:
        i.throw("IOException", f"{what}: {path}: {e.strerror or e}", n)


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _write(path, text, mode):
    with open(path, mode, encoding="utf-8", newline="") as f:
        f.write(text)


def io_read(i, a, n):
    p = need(i, a[0], ("string",), "IO.readFile", 1, n)
    return io_call(i, n, "IO.readFile", p, lambda: _read(p))


def io_read_lines(i, a, n):
    p = need(i, a[0], ("string",), "IO.readLines", 1, n)
    return io_call(i, n, "IO.readLines", p, lambda: _read(p).splitlines())


def io_write(i, a, n):
    p = need(i, a[0], ("string",), "IO.writeFile", 1, n)
    text = need(i, a[1], ("string",), "IO.writeFile", 2, n)
    io_call(i, n, "IO.writeFile", p, lambda: _write(p, text, "w"))


def io_append(i, a, n):
    p = need(i, a[0], ("string",), "IO.appendFile", 1, n)
    text = need(i, a[1], ("string",), "IO.appendFile", 2, n)
    io_call(i, n, "IO.appendFile", p, lambda: _write(p, text, "a"))


def io_delete(i, a, n):
    p = need(i, a[0], ("string",), "IO.deleteFile", 1, n)
    if not os.path.exists(p):
        return False
    io_call(i, n, "IO.deleteFile", p, lambda: os.remove(p))
    return True


def io_copy(i, a, n):
    import shutil
    src = need(i, a[0], ("string",), "IO.copy", 1, n)
    dst = need(i, a[1], ("string",), "IO.copy", 2, n)
    io_call(i, n, "IO.copy", src, lambda: shutil.copyfile(src, dst))


def io_move(i, a, n):
    src = need(i, a[0], ("string",), "IO.move", 1, n)
    dst = need(i, a[1], ("string",), "IO.move", 2, n)
    io_call(i, n, "IO.move", src, lambda: os.replace(src, dst))


def io_make_dir(i, a, n):
    p = need(i, a[0], ("string",), "IO.makeDir", 1, n)
    io_call(i, n, "IO.makeDir", p, lambda: os.makedirs(p, exist_ok=True))


def io_list_dir(i, a, n):
    p = need(i, a[0], ("string",), "IO.listDir", 1, n) if a else "."
    return io_call(i, n, "IO.listDir", p, lambda: sorted(os.listdir(p)))


def io_remove_dir(i, a, n):
    import shutil
    p = need(i, a[0], ("string",), "IO.removeDir", 1, n)
    if not os.path.exists(p):
        return False
    io_call(i, n, "IO.removeDir", p, lambda: shutil.rmtree(p))
    return True


def io_size(i, a, n):
    p = need(i, a[0], ("string",), "IO.size", 1, n)
    return io_call(i, n, "IO.size", p, lambda: os.path.getsize(p))


def io_modified(i, a, n):
    p = need(i, a[0], ("string",), "IO.modified", 1, n)
    return io_call(i, n, "IO.modified", p, lambda: int(os.path.getmtime(p)))


def io_print(i, a, n):
    i.stdout.write("".join(to_display(x, i) for x in a))
    return None


def io_println(i, a, n):
    i.stdout.write(" ".join(to_display(x, i) for x in a) + "\n")
    return None


def io_error(i, a, n):
    i.stdout.flush()
    sys.stderr.write(" ".join(to_display(x, i) for x in a) + "\n")
    return None


IO = {
    "readFile": (io_read, 1, 1),
    "readLines": (io_read_lines, 1, 1),
    "writeFile": (io_write, 2, 2),
    "appendFile": (io_append, 2, 2),
    "deleteFile": (io_delete, 1, 1),
    "copy": (io_copy, 2, 2),
    "move": (io_move, 2, 2),
    "exists": (lambda i, a, n: os.path.exists(need(i, a[0], ("string",), "IO.exists", 1, n)), 1, 1),
    "isFile": (lambda i, a, n: os.path.isfile(need(i, a[0], ("string",), "IO.isFile", 1, n)), 1, 1),
    "isDir": (lambda i, a, n: os.path.isdir(need(i, a[0], ("string",), "IO.isDir", 1, n)), 1, 1),
    "makeDir": (io_make_dir, 1, 1),
    "listDir": (io_list_dir, 0, 1),
    "removeDir": (io_remove_dir, 1, 1),
    "size": (io_size, 1, 1),
    "modified": (io_modified, 1, 1),
    "print": (io_print, 0, VARIADIC),
    "println": (io_println, 0, VARIADIC),
    "error": (io_error, 0, VARIADIC),
}


# ===========================================================================
# Path  (always uses "/" in results, on every OS)
# ===========================================================================

def slash(p):
    return p.replace("\\", "/") if os.sep == "\\" else p


def path_join(i, a, n):
    parts = [need(i, x, ("string",), "Path.join", k + 1, n) for k, x in enumerate(a)]
    return slash(os.path.join(*parts))


def path_ext(i, a, n):
    p = need(i, a[0], ("string",), "Path.extension", 1, n)
    return os.path.splitext(p)[1].lstrip(".")


def path_stem(i, a, n):
    p = need(i, a[0], ("string",), "Path.stem", 1, n)
    return os.path.splitext(os.path.basename(p))[0]


def str1(fname, fn):
    return (lambda i, a, n: slash(fn(need(i, a[0], ("string",), fname, 1, n))), 1, 1)


PATH = {
    "join": (path_join, 1, VARIADIC),
    "basename": str1("Path.basename", os.path.basename),
    "dirname": str1("Path.dirname", os.path.dirname),
    "extension": (path_ext, 1, 1),
    "stem": (path_stem, 1, 1),
    "absolute": str1("Path.absolute", os.path.abspath),
    "normalize": str1("Path.normalize", os.path.normpath),
    "isAbsolute": (lambda i, a, n: os.path.isabs(need(i, a[0], ("string",), "Path.isAbsolute", 1, n)), 1, 1),
    "cwd": (lambda i, a, n: slash(os.getcwd()), 0, 0),
    "temp": (lambda i, a, n: slash(tempfile.gettempdir()), 0, 0),
    "home": (lambda i, a, n: slash(os.path.expanduser("~")), 0, 0),
    "root": (lambda i, a, n: slash(i.root), 0, 0),
    "sep": "/",
}


# ===========================================================================
# Env
# ===========================================================================

ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*?)\s*$")


def parse_env_value(raw):
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1]
        if raw[0] == '"':
            inner = inner.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        return inner
    # Unquoted: strip a trailing " # comment"
    return re.split(r"\s+#", raw, maxsplit=1)[0]


def env_get(i, a, n):
    name = need(i, a[0], ("string",), "Env.get", 1, n)
    default = a[1] if len(a) > 1 else None
    return os.environ.get(name, default)


def env_set(i, a, n):
    name = need(i, a[0], ("string",), "Env.set", 1, n)
    value = need(i, a[1], ("string",), "Env.set", 2, n)
    os.environ[name] = value
    return None


def env_load(i, a, n):
    """Read KEY=value lines from a .env file. Existing variables win unless
    override is true. Returns the number of variables set."""
    p = need(i, a[0], ("string",), "Env.load", 1, n) if a else os.path.join(i.root, ".env")
    override = arg(i, a, 1, ("bool",), "Env.load", n, False)
    text = io_call(i, n, "Env.load", p, lambda: _read(p))
    count = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = ENV_LINE.match(line)
        if not m:
            i.throw("ValueException", f"Env.load: {p} line {lineno} is not KEY=value", n)
        key, value = m.group(1), parse_env_value(m.group(2))
        if override or key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def env_int(i, a, n):
    name = need(i, a[0], ("string",), "Env.int", 1, n)
    default = arg(i, a, 1, ("int",), "Env.int", n, 0)
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        i.throw("CastException", f"environment variable {name}={raw!r} is not an int", n)


def env_bool(i, a, n):
    name = need(i, a[0], ("string",), "Env.bool", 1, n)
    default = arg(i, a, 1, ("bool",), "Env.bool", n, False)
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    low = raw.strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    i.throw("CastException", f"environment variable {name}={raw!r} is not a bool", n)


ENV = {
    "get": (env_get, 1, 2),
    "int": (env_int, 1, 2),
    "bool": (env_bool, 1, 2),
    "set": (env_set, 2, 2),
    "has": (lambda i, a, n: need(i, a[0], ("string",), "Env.has", 1, n) in os.environ, 1, 1),
    "remove": (lambda i, a, n: os.environ.pop(need(i, a[0], ("string",), "Env.remove", 1, n), None) is not None, 1, 1),
    "all": (lambda i, a, n: dict(sorted(os.environ.items())), 0, 0),
    "load": (env_load, 0, 2),
}


# ===========================================================================
# Hash
# ===========================================================================

PBKDF2_ROUNDS = 310_000


def digest(algo):
    def f(i, a, n):
        data = need(i, a[0], ("string",), f"Hash.{algo}", 1, n)
        return hashlib.new(algo, data.encode("utf-8")).hexdigest()
    return (f, 1, 1)


def hash_hmac(i, a, n):
    key = need(i, a[0], ("string",), "Hash.hmac", 1, n)
    msg = need(i, a[1], ("string",), "Hash.hmac", 2, n)
    algo = arg(i, a, 2, ("string",), "Hash.hmac", n, "sha256")
    if algo not in ("sha256", "sha512", "sha1", "md5"):
        i.throw("ValueException", f"Hash.hmac: unknown algorithm \"{algo}\" (use sha256, sha512, sha1 or md5)", n)
    return _hmac.new(key.encode(), msg.encode(), algo).hexdigest()


def hash_password(i, a, n):
    """Salted PBKDF2-SHA256. Format: $pbkdf2-sha256$rounds$salt$hash"""
    pw = need(i, a[0], ("string",), "Hash.password", 1, n)
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ROUNDS)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return f"$pbkdf2-sha256${PBKDF2_ROUNDS}${b64(salt)}${b64(dk)}"


def hash_verify(i, a, n):
    pw = need(i, a[0], ("string",), "Hash.verify", 1, n)
    stored = need(i, a[1], ("string",), "Hash.verify", 2, n)
    parts = stored.split("$")
    if len(parts) != 5 or parts[1] != "pbkdf2-sha256":
        i.throw("ValueException", "Hash.verify: the second argument is not a hash made by Hash.password()", n)
    try:
        rounds = int(parts[2])
        unb64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        salt, expected = unb64(parts[3]), unb64(parts[4])
    except (ValueError, TypeError):
        i.throw("ValueException", "Hash.verify: the stored hash is damaged", n)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, rounds)
    return _hmac.compare_digest(dk, expected)


def hash_equals(i, a, n):
    x = need(i, a[0], ("string",), "Hash.equals", 1, n)
    y = need(i, a[1], ("string",), "Hash.equals", 2, n)
    return _hmac.compare_digest(x.encode(), y.encode())


HASH = {
    "md5": digest("md5"),
    "sha1": digest("sha1"),
    "sha256": digest("sha256"),
    "sha512": digest("sha512"),
    "hmac": (hash_hmac, 2, 3),
    "password": (hash_password, 1, 1),
    "verify": (hash_verify, 2, 2),
    "equals": (hash_equals, 2, 2),
}


# ===========================================================================
# Crypto  (random values, base64, JWT HS256)
# ===========================================================================

def b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64url(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def crypto_random_int(i, a, n):
    lo = need(i, a[0], ("int",), "Crypto.randomInt", 1, n)
    hi = need(i, a[1], ("int",), "Crypto.randomInt", 2, n)
    if lo > hi:
        i.throw("ValueException", f"Crypto.randomInt: min {lo} is bigger than max {hi}", n)
    return lo + secrets.randbelow(hi - lo + 1)


def crypto_random_hex(i, a, n):
    count = arg(i, a, 0, ("int",), "Crypto.randomHex", n, 32)
    if count < 1:
        i.throw("ValueException", "Crypto.randomHex: byte count must be at least 1", n)
    return secrets.token_hex(count)


def crypto_token(i, a, n):
    count = arg(i, a, 0, ("int",), "Crypto.token", n, 32)
    if count < 1:
        i.throw("ValueException", "Crypto.token: byte count must be at least 1", n)
    return secrets.token_urlsafe(count)


def crypto_b64e(i, a, n):
    s = need(i, a[0], ("string",), "Crypto.base64Encode", 1, n)
    return base64.b64encode(s.encode()).decode()


def crypto_b64d(i, a, n):
    s = need(i, a[0], ("string",), "Crypto.base64Decode", 1, n)
    try:
        return base64.b64decode(s, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        i.throw("CryptoException", "Crypto.base64Decode: input is not valid base64 text", n)


def crypto_jwt_sign(i, a, n):
    payload = need(i, a[0], ("dict",), "Crypto.jwtSign", 1, n)
    secret = need(i, a[1], ("string",), "Crypto.jwtSign", 2, n)
    ttl = arg(i, a, 2, ("int",), "Crypto.jwtSign", n, None)
    body = to_json_value(i, payload, n)
    if ttl is not None:
        now = int(time.time())
        body.setdefault("iat", now)
        body["exp"] = now + ttl
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = b64url(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())
    sig = _hmac.new(secret.encode(), f"{header}.{claims}".encode(), "sha256").digest()
    return f"{header}.{claims}.{b64url(sig)}"


def crypto_jwt_verify(i, a, n):
    """Returns the payload dict, or raises CryptoException."""
    token = need(i, a[0], ("string",), "Crypto.jwtVerify", 1, n)
    secret = need(i, a[1], ("string",), "Crypto.jwtVerify", 2, n)
    parts = token.split(".")
    if len(parts) != 3:
        i.throw("CryptoException", "invalid JWT: expected 3 parts", n)
    try:
        header = json.loads(unb64url(parts[0]))
        payload = json.loads(unb64url(parts[1]))
        sig = unb64url(parts[2])
    except (ValueError, TypeError):
        i.throw("CryptoException", "invalid JWT: cannot decode", n)
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        i.throw("CryptoException", "invalid JWT: only HS256 is supported", n)
    expected = _hmac.new(secret.encode(), f"{parts[0]}.{parts[1]}".encode(), "sha256").digest()
    if not _hmac.compare_digest(sig, expected):
        i.throw("CryptoException", "invalid JWT: bad signature", n)
    if not isinstance(payload, dict):
        i.throw("CryptoException", "invalid JWT: payload is not an object", n)
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) and time.time() >= exp:
        i.throw("CryptoException", "JWT has expired", n)
    return payload


CRYPTO = {
    "randomInt": (crypto_random_int, 2, 2),
    "randomHex": (crypto_random_hex, 0, 1),
    "token": (crypto_token, 0, 1),
    "base64Encode": (crypto_b64e, 1, 1),
    "base64Decode": (crypto_b64d, 1, 1),
    "jwtSign": (crypto_jwt_sign, 2, 3),
    "jwtVerify": (crypto_jwt_verify, 2, 2),
}


# ===========================================================================
# Uuid
# ===========================================================================

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def uuid_v7(i, a, n):
    """Time-ordered UUID (RFC 9562): good as a database primary key."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


UUID = {
    "v4": (lambda i, a, n: str(uuid.uuid4()), 0, 0),
    "v7": (uuid_v7, 0, 0),
    "isValid": (lambda i, a, n: bool(UUID_RE.match(need(i, a[0], ("string",), "Uuid.isValid", 1, n))), 1, 1),
}


# ===========================================================================
# Regex  — patterns are "/body/flags" (flags: i m s x) or a bare body.
#           Write them in 'single quotes' so backslashes stay as they are.
# ===========================================================================

_regex_cache = {}


def compile_regex(i, pattern, n, fname):
    need(i, pattern, ("string",), fname, 1, n)
    hit = _regex_cache.get(pattern)
    if hit is not None:
        return hit
    body, flags = pattern, 0
    if len(pattern) >= 2 and pattern[0] == "/" and pattern.rfind("/") > 0:
        end = pattern.rfind("/")
        body, mods = pattern[1:end], pattern[end + 1:]
        for ch in mods:
            if ch not in "imsx":
                i.throw("RegexException", f"{fname}: unknown regex flag '{ch}' (use i, m, s or x)", n)
            flags |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X}[ch]
    try:
        rx = re.compile(body, flags)
    except re.error as e:
        i.throw("RegexException", f"{fname}: bad pattern {pattern!r}: {e}", n)
    _regex_cache[pattern] = rx
    return rx


def rx_args(i, a, n, fname):
    return compile_regex(i, a[0], n, fname), need(i, a[1], ("string",), fname, 2, n)


def rx_match(i, a, n):
    rx, text = rx_args(i, a, n, "Regex.match")
    return rx.search(text) is not None


def rx_capture(i, a, n):
    """First match: #[whole, group1, ...] or null. Missing groups are null."""
    rx, text = rx_args(i, a, n, "Regex.capture")
    m = rx.search(text)
    if m is None:
        return None
    return [m.group(0)] + list(m.groups())


def rx_named(i, a, n):
    rx, text = rx_args(i, a, n, "Regex.named")
    m = rx.search(text)
    return None if m is None else m.groupdict()


def rx_all(i, a, n):
    rx, text = rx_args(i, a, n, "Regex.matchAll")
    return [[m.group(0)] + list(m.groups()) for m in rx.finditer(text)]


def rx_replace(i, a, n):
    """Regex.replace(pattern, replacement, text). Replacement may use $1 / ${name},
    or be a function: kr(list<string> m): string => ..."""
    rx = compile_regex(i, a[0], n, "Regex.replace")
    repl = a[1]
    text = need(i, a[2], ("string",), "Regex.replace", 3, n)
    limit = arg(i, a, 3, ("int",), "Regex.replace", n, 0)
    if type_name(repl) == "function":
        def fn(m):
            out = call(i, repl, [[m.group(0)] + list(m.groups())], n, "Regex.replace")
            if not isinstance(out, str):
                i.throw("TypeException", f"the Regex.replace() callback must return string, got {type_name(out)}", n)
            return out
        return rx.sub(fn, text, count=limit)
    need(i, repl, ("string", "callable"), "Regex.replace", 2, n)
    # Translate $1 and ${name} into Python's \g<...>; "$$" is a literal "$".
    py = re.sub(r"\$\$|\$\{(\w+)\}|\$(\d+)|\\",
                lambda m: "$" if m.group(0) == "$$" else "\\\\" if m.group(0) == "\\"
                else f"\\g<{m.group(1) or m.group(2)}>", repl)
    try:
        return rx.sub(py, text, count=limit)
    except (re.error, IndexError) as e:
        i.throw("RegexException", f"Regex.replace: bad replacement {repl!r}: {e}", n)


def rx_split(i, a, n):
    rx, text = rx_args(i, a, n, "Regex.split")
    return [p for p in rx.split(text) if p is not None] if rx.groups == 0 else rx.split(text)


def rx_escape(i, a, n):
    return re.escape(need(i, a[0], ("string",), "Regex.escape", 1, n))


REGEX = {
    "match": (rx_match, 2, 2),
    "capture": (rx_capture, 2, 2),
    "named": (rx_named, 2, 2),
    "matchAll": (rx_all, 2, 2),
    "replace": (rx_replace, 3, 4),
    "split": (rx_split, 2, 2),
    "escape": (rx_escape, 1, 1),
}


# ===========================================================================
# Date — values are Unix timestamps (int seconds, UTC). Formatting and
#        parsing use UTC unless you pass "local" as the timezone argument.
# ===========================================================================

UNIT_SECONDS = {
    "second": 1, "seconds": 1, "minute": 60, "minutes": 60, "hour": 3600, "hours": 3600,
    "day": 86400, "days": 86400, "week": 604800, "weeks": 604800,
}
MONTH_UNITS = {"month": 1, "months": 1, "year": 12, "years": 12}


def tz_arg(i, a, k, fname, n):
    tz = arg(i, a, k, ("string",), fname, n, "utc")
    if tz.lower() == "utc":
        return dt.timezone.utc
    if tz.lower() == "local":
        return None
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", tz)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return dt.timezone(sign * dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz)
    except Exception:
        i.throw("DateException", f'{fname}: unknown timezone "{tz}" (use "utc", "local", "+05:00" or a name like "Asia/Karachi")', n)


def to_dt(ts, tz):
    d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    return d.astimezone(tz) if tz is not None else d.astimezone()


def php_format(d, fmt):
    """PHP-style date letters: Y y m n d j H G h g i s A a D l M F N w U T e P
    Backslash escapes a letter: "\\T" (write it as '\\T' in single quotes)."""
    out = []
    esc = False
    for ch in fmt:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        h12 = d.hour % 12 or 12
        off = d.utcoffset() or dt.timedelta(0)
        mins = int(off.total_seconds() // 60)
        table = {
            "Y": lambda: f"{d.year:04d}", "y": lambda: f"{d.year % 100:02d}",
            "m": lambda: f"{d.month:02d}", "n": lambda: str(d.month),
            "d": lambda: f"{d.day:02d}", "j": lambda: str(d.day),
            "H": lambda: f"{d.hour:02d}", "G": lambda: str(d.hour),
            "h": lambda: f"{h12:02d}", "g": lambda: str(h12),
            "i": lambda: f"{d.minute:02d}", "s": lambda: f"{d.second:02d}",
            "A": lambda: "AM" if d.hour < 12 else "PM", "a": lambda: "am" if d.hour < 12 else "pm",
            "D": lambda: calendar.day_abbr[d.weekday()], "l": lambda: calendar.day_name[d.weekday()],
            "M": lambda: calendar.month_abbr[d.month], "F": lambda: calendar.month_name[d.month],
            "N": lambda: str(d.isoweekday()), "w": lambda: str(d.isoweekday() % 7),
            "U": lambda: str(int(d.timestamp())),
            "P": lambda: f"{'+' if mins >= 0 else '-'}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}",
            "T": lambda: d.tzname() or "",
            "e": lambda: str(d.tzinfo) if d.tzinfo else "",
        }
        f = table.get(ch)
        out.append(f() if f else ch)
    if esc:
        out.append("\\")
    return "".join(out)


DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y/%m/%d", "%d.%m.%Y"]


def date_parse(i, a, n):
    text = need(i, a[0], ("string",), "Date.parse", 1, n).strip()
    tz = tz_arg(i, a, 1, "Date.parse", n)
    d = None
    try:
        d = dt.datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    except ValueError:
        for f in DATE_FORMATS:
            try:
                d = dt.datetime.strptime(text, f)
                break
            except ValueError:
                pass
    if d is None:
        i.throw("DateException", f'Date.parse: cannot read "{text}" as a date (try "2026-01-15" or "2026-01-15 14:30:00")', n)
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz) if tz is not None else d.astimezone()
    return int(d.timestamp())


def date_format(i, a, n):
    ts = need(i, a[0], ("int",), "Date.format", 1, n)
    fmt = arg(i, a, 1, ("string",), "Date.format", n, "Y-m-d H:i:s")
    return php_format(to_dt(ts, tz_arg(i, a, 2, "Date.format", n)), fmt)


def date_make(i, a, n):
    vals = [need(i, x, ("int",), "Date.make", k + 1, n) for k, x in enumerate(a)]
    y, mo, d, h, mi, s = (vals + [1, 1, 0, 0, 0])[:6] if len(vals) >= 1 else (1970, 1, 1, 0, 0, 0)
    try:
        return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone.utc).timestamp())
    except ValueError as e:
        i.throw("DateException", f"Date.make: {e}", n)


def date_add(i, a, n):
    ts = need(i, a[0], ("int",), "Date.add", 1, n)
    amount = need(i, a[1], ("int",), "Date.add", 2, n)
    unit = need(i, a[2], ("string",), "Date.add", 3, n).lower()
    if unit in UNIT_SECONDS:
        return ts + amount * UNIT_SECONDS[unit]
    if unit in MONTH_UNITS:
        d = to_dt(ts, dt.timezone.utc)
        total = d.year * 12 + (d.month - 1) + amount * MONTH_UNITS[unit]
        y, m = divmod(total, 12)
        day = min(d.day, calendar.monthrange(y, m + 1)[1])  # Jan 31 + 1 month -> Feb 28/29
        return int(d.replace(year=y, month=m + 1, day=day).timestamp())
    i.throw("DateException", f'Date.add: unknown unit "{unit}" (use seconds, minutes, hours, days, weeks, months, years)', n)


def date_diff(i, a, n):
    """Whole units from a to b (b - a), truncated toward zero."""
    x = need(i, a[0], ("int",), "Date.diff", 1, n)
    y = need(i, a[1], ("int",), "Date.diff", 2, n)
    unit = arg(i, a, 2, ("string",), "Date.diff", n, "seconds").lower()
    if unit in UNIT_SECONDS:
        return int((y - x) / UNIT_SECONDS[unit])
    if unit in MONTH_UNITS:
        da, db = to_dt(x, dt.timezone.utc), to_dt(y, dt.timezone.utc)
        sign = 1
        if db < da:
            da, db, sign = db, da, -1
        months = (db.year - da.year) * 12 + db.month - da.month
        if (db.day, db.time()) < (da.day, da.time()):
            months -= 1
        return sign * (months // MONTH_UNITS[unit])
    i.throw("DateException", f'Date.diff: unknown unit "{unit}"', n)


def date_parts(i, a, n):
    ts = need(i, a[0], ("int",), "Date.parts", 1, n)
    d = to_dt(ts, tz_arg(i, a, 1, "Date.parts", n))
    return {"year": d.year, "month": d.month, "day": d.day, "hour": d.hour, "minute": d.minute,
            "second": d.second, "weekday": d.isoweekday(), "yearDay": d.timetuple().tm_yday}


def date_start_of(i, a, n):
    ts = need(i, a[0], ("int",), "Date.startOf", 1, n)
    unit = need(i, a[1], ("string",), "Date.startOf", 2, n).lower()
    d = to_dt(ts, dt.timezone.utc)
    if unit == "minute":
        d = d.replace(second=0)
    elif unit == "hour":
        d = d.replace(minute=0, second=0)
    elif unit == "day":
        d = d.replace(hour=0, minute=0, second=0)
    elif unit == "week":
        d = (d - dt.timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0)
    elif unit == "month":
        d = d.replace(day=1, hour=0, minute=0, second=0)
    elif unit == "year":
        d = d.replace(month=1, day=1, hour=0, minute=0, second=0)
    else:
        i.throw("DateException", f'Date.startOf: unknown unit "{unit}" (use minute, hour, day, week, month, year)', n)
    return int(d.replace(microsecond=0).timestamp())


def date_ago(i, a, n):
    ts = need(i, a[0], ("int",), "Date.ago", 1, n)
    now = arg(i, a, 1, ("int",), "Date.ago", n, int(time.time()))
    secs = now - ts
    future = secs < 0
    secs = abs(secs)
    if secs < 45:
        return "just now"
    for size, word in ((31536000, "year"), (2592000, "month"), (604800, "week"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size:
            k = round(secs / size) if size == 60 else secs // size
            label = f"{k} {word}{'s' if k != 1 else ''}"
            return f"in {label}" if future else f"{label} ago"
    return "just now"


DATE = {
    "now": (lambda i, a, n: int(time.time()), 0, 0),
    "nowMs": (lambda i, a, n: time.time_ns() // 1_000_000, 0, 0),
    "today": (lambda i, a, n: int(dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()), 0, 0),
    "make": (date_make, 1, 6),
    "parse": (date_parse, 1, 2),
    "format": (date_format, 1, 3),
    "iso": (lambda i, a, n: to_dt(need(i, a[0], ("int",), "Date.iso", 1, n), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), 1, 1),
    "add": (date_add, 3, 3),
    "sub": (lambda i, a, n: date_add(i, [a[0], -need(i, a[1], ("int",), "Date.sub", 2, n), a[2]], n), 3, 3),
    "diff": (date_diff, 2, 3),
    "parts": (date_parts, 1, 2),
    "startOf": (date_start_of, 2, 2),
    "ago": (date_ago, 1, 2),
    "isLeapYear": (lambda i, a, n: calendar.isleap(need(i, a[0], ("int",), "Date.isLeapYear", 1, n)), 1, 1),
    "daysInMonth": (lambda i, a, n: calendar.monthrange(need(i, a[0], ("int",), "Date.daysInMonth", 1, n),
                                                        need(i, a[1], ("int",), "Date.daysInMonth", 2, n))[1], 2, 2),
    "sleep": (lambda i, a, n: time.sleep(need(i, a[0], ("number",), "Date.sleep", 1, n)), 1, 1),
}


# ===========================================================================
# Arr — list helpers as functions. Unlike list methods, Arr.sort/reverse
#        return a new list and leave the original alone.
# ===========================================================================

def wrap_list_method(name, copy_first=False):
    impl, lo, hi = LIST_METHODS[name]

    def f(i, a, n):
        lst = need(i, a[0], ("list",), f"Arr.{name}", 1, n)
        return impl(i, list(lst) if copy_first else lst, a[1:], n)
    return (f, lo + 1, hi if hi == VARIADIC else hi + 1)


def arr_unique(i, a, n):
    lst = need(i, a[0], ("list",), "Arr.unique", 1, n)
    out = []
    for x in lst:
        if not any(values_equal(x, y) for y in out):
            out.append(x)
    return out


def arr_flatten(i, a, n):
    lst = need(i, a[0], ("list",), "Arr.flatten", 1, n)
    depth = arg(i, a, 1, ("int",), "Arr.flatten", n, 1)

    def go(items, d):
        out = []
        for x in items:
            if isinstance(x, list) and d > 0:
                out.extend(go(x, d - 1))
            else:
                out.append(x)
        return out
    return go(lst, depth)


def arr_merge(i, a, n):
    out = []
    for k, x in enumerate(a):
        out.extend(need(i, x, ("list",), "Arr.merge", k + 1, n))
    return out


def arr_chunk(i, a, n):
    lst = need(i, a[0], ("list",), "Arr.chunk", 1, n)
    size = need(i, a[1], ("int",), "Arr.chunk", 2, n)
    if size < 1:
        i.throw("ValueException", "Arr.chunk: size must be at least 1", n)
    return [lst[k:k + size] for k in range(0, len(lst), size)]


def arr_zip(i, a, n):
    lists = [need(i, x, ("list",), "Arr.zip", k + 1, n) for k, x in enumerate(a)]
    return [list(t) for t in zip(*lists)]


def arr_group_by(i, a, n):
    lst = need(i, a[0], ("list",), "Arr.groupBy", 1, n)
    out = {}
    for x in lst:
        key = call(i, a[1], [x], n, "Arr.groupBy")
        out.setdefault(i.key_of(key, n, "group key"), []).append(x)
    return out


def arr_key_by(i, a, n):
    lst = need(i, a[0], ("list",), "Arr.keyBy", 1, n)
    return {i.key_of(call(i, a[1], [x], n, "Arr.keyBy"), n, "key"): x for x in lst}


def arr_pluck(i, a, n):
    lst = need(i, a[0], ("list",), "Arr.pluck", 1, n)
    key = a[1]
    out = []
    for x in lst:
        if isinstance(x, dict):
            out.append(x.get(key))
        elif isinstance(x, BarqObject) and isinstance(key, str):
            out.append(i.get_attr(x, key, n, i.module.env if i.module else None))
        else:
            i.throw("TypeException", f"Arr.pluck needs a list of dicts or objects, found {type_name(x)}", n)
    return out


def arr_count_by(i, a, n):
    lst = need(i, a[0], ("list",), "Arr.countBy", 1, n)
    out = {}
    for x in lst:
        k = i.key_of(call(i, a[1], [x], n, "Arr.countBy"), n, "key")
        out[k] = out.get(k, 0) + 1
    return out


def arr_partition(i, a, n):
    lst = need(i, a[0], ("list",), "Arr.partition", 1, n)
    yes, no = [], []
    for x in lst:
        (yes if need_bool(i, call(i, a[1], [x], n, "Arr.partition"), "Arr.partition", n) else no).append(x)
    return [yes, no]


def arr_minmax(fname, pick):
    def f(i, a, n):
        lst = need(i, a[0], ("list",), f"Arr.{fname}", 1, n)
        if not lst:
            return None
        for k, x in enumerate(lst):
            need(i, x, ("number",), f"Arr.{fname}", 1, n)
        return pick(lst)
    return (f, 1, 1)


def arr_avg(i, a, n):
    lst = need(i, a[0], ("list",), "Arr.avg", 1, n)
    if not lst:
        return None
    for x in lst:
        need(i, x, ("number",), "Arr.avg", 1, n)
    return sum(lst) / len(lst)


def arr_fill(i, a, n):
    count = need(i, a[0], ("int",), "Arr.fill", 1, n)
    return [a[1]] * max(count, 0)


ARR = {name: wrap_list_method(name) for name in (
    "push", "pop", "insert", "removeAt", "remove", "contains", "indexOf", "join", "map", "filter",
    "reduce", "each", "any", "all", "find", "sum", "first", "last", "slice", "copy", "isEmpty")}
ARR.update({
    "sort": wrap_list_method("sort", copy_first=True),
    "reverse": wrap_list_method("reverse", copy_first=True),
    "count": (lambda i, a, n: len(need(i, a[0], ("list", "dict", "set"), "Arr.count", 1, n)), 1, 1),
    "unique": (arr_unique, 1, 1),
    "flatten": (arr_flatten, 1, 2),
    "merge": (arr_merge, 1, VARIADIC),
    "chunk": (arr_chunk, 2, 2),
    "zip": (arr_zip, 2, VARIADIC),
    "groupBy": (arr_group_by, 2, 2),
    "keyBy": (arr_key_by, 2, 2),
    "countBy": (arr_count_by, 2, 2),
    "pluck": (arr_pluck, 2, 2),
    "partition": (arr_partition, 2, 2),
    "min": arr_minmax("min", min),
    "max": arr_minmax("max", max),
    "avg": (arr_avg, 1, 1),
    "fill": (arr_fill, 2, 2),
    "keys": (lambda i, a, n: list(need(i, a[0], ("dict",), "Arr.keys", 1, n).keys()), 1, 1),
    "values": (lambda i, a, n: list(need(i, a[0], ("dict",), "Arr.values", 1, n).values()), 1, 1),
})


# ===========================================================================
# Log — writes "[2026-11-05 14:03:00] INFO: message" to stderr or a file
# ===========================================================================

LOG_LEVELS = ["debug", "info", "warning", "error", "critical"]


class LogState:
    level = "debug"
    file = None


def log_write(level):
    def f(i, a, n):
        msg = " ".join(to_display(x, i) for x in a)
        if LOG_LEVELS.index(level) < LOG_LEVELS.index(LogState.level):
            return None
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        line = f"[{stamp}] {level.upper()}: {msg}\n"
        if LogState.file:
            io_call(i, n, "Log", LogState.file, lambda: _write(LogState.file, line, "a"))
        else:
            i.stdout.flush()
            sys.stderr.write(line)
            sys.stderr.flush()
        return None
    return (f, 1, VARIADIC)


def log_set_level(i, a, n):
    lvl = need(i, a[0], ("string",), "Log.setLevel", 1, n).lower()
    if lvl not in LOG_LEVELS:
        i.throw("ValueException", f'Log.setLevel: unknown level "{lvl}" (use {", ".join(LOG_LEVELS)})', n)
    LogState.level = lvl
    return None


def log_set_file(i, a, n):
    LogState.file = need(i, a[0], ("string", "null"), "Log.setFile", 1, n)
    return None


LOG = {lvl: log_write(lvl) for lvl in LOG_LEVELS}
LOG.update({
    "warn": log_write("warning"),
    "setLevel": (log_set_level, 1, 1),
    "setFile": (log_set_file, 1, 1),
})


# ===========================================================================
# Cache — in-memory, per process. TTL in seconds.
# ===========================================================================

class CacheStore:
    data = {}   # key -> (value, expires_at or None)


def cache_alive(key):
    hit = CacheStore.data.get(key)
    if hit is None:
        return None
    if hit[1] is not None and time.monotonic() >= hit[1]:
        del CacheStore.data[key]
        return None
    return hit


def cache_set(i, a, n):
    key = need(i, a[0], ("string",), "Cache.set", 1, n)
    ttl = arg(i, a, 2, ("int",), "Cache.set", n, None)
    CacheStore.data[key] = (a[1], None if ttl is None else time.monotonic() + ttl)
    return None


def cache_get(i, a, n):
    hit = cache_alive(need(i, a[0], ("string",), "Cache.get", 1, n))
    return hit[0] if hit else (a[1] if len(a) > 1 else None)


def cache_remember(i, a, n):
    key = need(i, a[0], ("string",), "Cache.remember", 1, n)
    ttl = need(i, a[1], ("int", "null"), "Cache.remember", 2, n)
    hit = cache_alive(key)
    if hit:
        return hit[0]
    value = call(i, a[2], [], n, "Cache.remember")
    CacheStore.data[key] = (value, None if ttl is None else time.monotonic() + ttl)
    return value


def cache_increment(i, a, n):
    key = need(i, a[0], ("string",), "Cache.increment", 1, n)
    by = arg(i, a, 1, ("int",), "Cache.increment", n, 1)
    hit = cache_alive(key)
    current = hit[0] if hit else 0
    need(i, current, ("int",), "Cache.increment (stored value)", 1, n)
    CacheStore.data[key] = (current + by, hit[1] if hit else None)
    return current + by


CACHE = {
    "set": (cache_set, 2, 3),
    "forever": (lambda i, a, n: cache_set(i, [a[0], a[1]], n), 2, 2),
    "get": (cache_get, 1, 2),
    "has": (lambda i, a, n: cache_alive(need(i, a[0], ("string",), "Cache.has", 1, n)) is not None, 1, 1),
    "forget": (lambda i, a, n: CacheStore.data.pop(need(i, a[0], ("string",), "Cache.forget", 1, n), None) is not None, 1, 1),
    "remember": (cache_remember, 3, 3),
    "increment": (cache_increment, 1, 2),
    "flush": (lambda i, a, n: CacheStore.data.clear(), 0, 0),
}


# ===========================================================================
# install
# ===========================================================================

MODULES = {
    "Json": JSON, "IO": IO, "Path": PATH, "Env": ENV, "Hash": HASH, "Crypto": CRYPTO,
    "Uuid": UUID, "Regex": REGEX, "Date": DATE, "Arr": ARR, "Log": LOG, "Cache": CACHE,
}


def install(i):
    from .builtins import BarqClass
    for name, parent in STDLIB_EXCEPTIONS:
        cls = BarqClass(name, name, None, parent=i.classes[parent], native=True)
        i.classes[name] = cls
        i.builtins.declare(name, cls, None, const=True)
    for name, table in MODULES.items():
        i.builtins.declare(name, module(name, table), None, const=True)
