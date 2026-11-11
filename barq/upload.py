"""File uploads (v0.17.0): multipart requests, UploadedFile, file validation rules.

    UploadedFile photo = req.file("photo")       null when no file was sent
    string path = photo.store("avatars")         "avatars/<40 random chars>.png" on the default disk
    photo.storeAs("avatars", "user-42.png", "public")
    req.validate(#{"photo": "required|image|max:2048|dimensions:min_width=100"})

The file type comes from the file's first bytes, not from the name the browser
sent, so "shell.php" holding a PNG is stored as ".png". Contents are kept in
memory (strings with surrogateescape, like Storage), limited by
config filesystems.max_upload (KB per file, default 10240) and
filesystems.max_post (KB per request body, default 20480).
"""

import mimetypes
import posixpath
import re
import secrets
import struct
import zlib

from .builtins import need
from .runtime import BarqObject, type_name
from .stdlib import arg
from .stdlib2 import native_class, new_native

MAX_UPLOAD_KB = 10240
MAX_POST_KB = 20480
IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/bmp", "image/webp")
# file types whose name tells more than their bytes (all of these are zip files)
ZIP_FAMILY = {"docx", "xlsx", "pptx", "odt", "ods", "odp", "jar", "epub", "apk"}
EXTENSIONS = {
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/bmp": "bmp", "image/webp": "webp",
    "image/svg+xml": "svg", "application/pdf": "pdf", "application/zip": "zip", "application/gzip": "gz",
    "audio/mpeg": "mp3", "audio/ogg": "ogg", "video/mp4": "mp4", "text/plain": "txt", "text/csv": "csv",
    "application/json": "json", "text/html": "html", "text/markdown": "md", "application/xml": "xml",
    "text/xml": "xml", "application/octet-stream": "bin",
}
TEXT_LIKE = ("text/", "application/json", "application/xml", "image/svg+xml", "application/javascript")


def settings(i):
    from .stdlib2 import config_lookup
    conf = config_lookup(i, "filesystems")
    conf = conf if isinstance(conf, dict) else {}
    per_file = conf.get("max_upload", MAX_UPLOAD_KB)
    per_post = conf.get("max_post", MAX_POST_KB)
    ok = lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0  # noqa: E731
    return (per_file if ok(per_file) else MAX_UPLOAD_KB), (per_post if ok(per_post) else MAX_POST_KB)


# ===========================================================================
# the file itself
# ===========================================================================

class UploadState:
    def __init__(self, name, client_mime, data, error=None):
        self.name = name                # file name as sent, folders removed
        self.client_mime = client_mime  # Content-Type the browser sent
        self.data = data                # bytes
        self.error = error              # None, or why the upload is unusable
        self._mime = None

    @property
    def mime(self):
        if self._mime is None:
            self._mime = sniff(self.data, self.name, self.client_mime)
        return self._mime

    @property
    def client_ext(self):
        return posixpath.splitext(self.name)[1][1:].lower()

    @property
    def ext(self):
        m = self.mime
        if m == "application/zip" and self.client_ext in ZIP_FAMILY:
            return self.client_ext
        if m in ("image/jpeg",) and self.client_ext == "jpeg":
            return "jpeg"
        if m.startswith("text/") or m in TEXT_LIKE:
            guessed = mimetypes.guess_type("x." + self.client_ext)[0] if self.client_ext else None
            if guessed == m:
                return self.client_ext
        return EXTENSIONS.get(m) or (mimetypes.guess_extension(m) or ".bin")[1:]


def is_upload(v):
    return isinstance(v, BarqObject) and isinstance(v.native, UploadState)


def clean_name(name):
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f").strip()
    return name[:255]


def sniff(data, name, client_mime):
    """The file type from its first bytes; text files fall back to the name."""
    head = data[:64]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM" and len(data) > 26 and struct.unpack("<I", data[2:6])[0] in (0, len(data)):
        return "image/bmp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "application/zip"
    if head[:2] == b"\x1f\x8b":
        return "application/gzip"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "audio/mpeg"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    sample = data[:4096]
    if b"\0" in sample:
        return "application/octet-stream"
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.start < len(sample) - 4:          # not just a character cut at the end of the sample
            return "application/octet-stream"
        text = sample[:e.start].decode("utf-8")
    low = text.lstrip("﻿ \t\r\n").lower()
    if low.startswith("<svg") or (low.startswith("<?xml") and "<svg" in low[:1024]):
        return "image/svg+xml"
    guessed = mimetypes.guess_type(name)[0] if name else None
    if guessed and (guessed.startswith("text/") or guessed in TEXT_LIKE):
        return guessed
    return "text/plain"


def dimensions(data, mime):
    """(width, height) for png/jpeg/gif/bmp/webp, else None."""
    try:
        if mime == "image/png" and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if mime == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if mime == "image/bmp":
            w, h = struct.unpack("<ii", data[18:26])
            return w, abs(h)
        if mime == "image/webp":
            kind = data[12:16]
            if kind == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
            if kind == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
        if mime == "image/jpeg":
            k = 2
            while k + 9 < len(data):
                if data[k] != 0xFF:
                    k += 1
                    continue
                marker = data[k + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                    k += 1 if marker == 0xFF else 2
                    continue
                size = struct.unpack(">H", data[k + 2:k + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[k + 5:k + 9])
                    return w, h
                k += 2 + size
    except (struct.error, IndexError):
        return None
    return None


def as_text(data):
    return data.decode("utf-8", "surrogateescape")


def new_upload(i, name, client_mime, data, error=None):
    return new_native(i, "UploadedFile", UploadState(clean_name(name), client_mime or "application/octet-stream",
                                                     data, error))


# ===========================================================================
# multipart/form-data
# ===========================================================================

def boundary_of(ctype):
    m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', ctype or "", re.I)
    return (m.group(1) or m.group(2)) if m else None


def header_params(value):
    """'form-data; name="a"; filename="b.png"' -> {"name": "a", "filename": "b.png"}"""
    out = {}
    for m in re.finditer(r';\s*([\w*-]+)=(?:"((?:[^"\\]|\\.)*)"|([^;]*))', value):
        key = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else (m.group(3) or "").strip()
        if m.group(2) is not None:
            val = val.replace('\\"', '"')     # browsers send backslashes as-is (old IE: C:\\pics\\me.png)
        if key == "filename*" and "''" in val:         # RFC 5987: utf-8''na%C3%AFve.txt
            from urllib.parse import unquote
            charset, _, enc = val.partition("''")
            key, val = "filename", unquote(enc, encoding=charset or "utf-8", errors="replace")
        if key == "filename" or key not in out:
            out[key] = val
    return out


def put_value(target, name, value):
    """name[] collects a list; anything else: the last value wins."""
    if name.endswith("[]"):
        target.setdefault(name[:-2], [])
        if isinstance(target[name[:-2]], list):
            target[name[:-2]].append(value)
        else:
            target[name[:-2]] = [value]
    else:
        target[name] = value


def parse_multipart(i, raw, ctype):
    """-> (fields, files). Files over the size limit come back with an error set."""
    boundary = boundary_of(ctype)
    fields, files = {}, {}
    if not boundary:
        return fields, files
    per_file = settings(i)[0] * 1024
    delim = b"--" + boundary.encode("latin-1")
    chunks = raw.split(delim)
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        head, sep, body = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if body.endswith(b"\r\n"):
            body = body[:-2]
        headers = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        disp = header_params(headers.get("content-disposition", ""))
        name = disp.get("name")
        if not name:
            continue
        if "filename" in disp:
            if disp["filename"] == "" and not body:     # a file input left empty
                continue
            error = None
            if len(body) > per_file:
                error = f"The file is larger than the upload limit of {per_file // 1024} KB."
                body = b""
            put_value(files, name, new_upload(i, disp["filename"], headers.get("content-type"), body, error))
        else:
            put_value(fields, name, body.decode("utf-8", "replace"))
    return fields, files


def encode_multipart(i, fields, n, fname):
    """dict (values: strings, numbers, UploadedFile, or lists of them) -> (bytes, content type)."""
    from .runtime import to_display
    boundary = "----BarqBoundary" + secrets.token_hex(12)
    out = []

    def part(name, value):
        if is_upload(value):
            st = value.native
            fname_ = st.name.replace('"', "%22")
            out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{fname_}"\r\n'
                       f'Content-Type: {st.client_mime}\r\n\r\n'.encode())
            out.append(st.data)
            out.append(b"\r\n")
        elif value is None:
            part(name, "")
        elif isinstance(value, (dict, BarqObject)):
            i.throw("TypeException", f"{fname}(): cannot send {type_name(value)} as the form field \"{name}\"", n)
        else:
            out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            out.append(to_display(value, i).encode("utf-8") + b"\r\n")

    for k, v in fields.items():
        if isinstance(v, list):
            for x in v:
                part(f"{k}[]", x)
        else:
            part(str(k), v)
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def has_uploads(data):
    if not isinstance(data, dict):
        return False
    return any(is_upload(v) or (isinstance(v, list) and any(is_upload(x) for x in v)) for v in data.values())


# ===========================================================================
# UploadedFile methods
# ===========================================================================

def st_of(this):
    return this.native


def need_valid(i, st, n, fname):
    if st.error:
        i.throw("StorageException", f"{fname}(): the upload failed: {st.error}", n)


def hash_name(st):
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(40)) + "." + st.ext


def disk_arg(i, a, k, fname, n):
    """A disk name, or #{"disk": "public"} as Laravel writes it."""
    from .storage import get_disk
    if len(a) <= k or a[k] is None:
        return get_disk(i, None, n)
    v = a[k]
    if isinstance(v, dict):
        v = v.get("disk")
    if isinstance(v, BarqObject) and v.cls.name == "Disk":
        return v
    need(i, v, ("string",), fname, k + 1, n)
    return get_disk(i, v, n)


def store_on(i, disk, folder, name, st, n, fname):
    from .storage import clean, d_write
    need_valid(i, st, n, fname)
    folder = clean(i, folder, n, fname, allow_root=True)
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        i.throw("StorageException", f'{fname}(): "{name}" is not a file name', n)
    path = (folder + "/" if folder else "") + name
    d_write(i, disk.native, clean(i, path, n, fname), as_text(st.data), n, fname)
    return path


def u_store(public, named):
    fname = "UploadedFile." + ("storePublicly" if public else "store") + ("As" if named else "")

    def fn(i, a, n, this):
        st = st_of(this)
        folder = arg(i, a, 0, ("string",), fname, n, "")
        if named:
            name = need(i, a[1], ("string",), fname, 2, n)
            k = 2
        else:
            name, k = hash_name(st), 1
        if public:
            from .storage import get_disk
            disk = get_disk(i, "public", n)
        else:
            disk = disk_arg(i, a, k, fname, n)
        return store_on(i, disk, folder, name, st, n, fname)
    return fn


def u_dimensions(i, a, n, this):
    d = dimensions(st_of(this).data, st_of(this).mime)
    return {"width": d[0], "height": d[1]} if d else None


def u_to_json(i, a, n, this):
    st = st_of(this)
    return {"name": st.name, "mime": st.mime, "size": len(st.data)}


UPLOAD_METHODS = {
    "name": (lambda i, a, n, this: st_of(this).name, 0, 0),
    "clientName": (lambda i, a, n, this: st_of(this).name, 0, 0),
    "clientExtension": (lambda i, a, n, this: st_of(this).client_ext, 0, 0),
    "clientMime": (lambda i, a, n, this: st_of(this).client_mime, 0, 0),
    "extension": (lambda i, a, n, this: st_of(this).ext, 0, 0),
    "mime": (lambda i, a, n, this: st_of(this).mime, 0, 0),
    "size": (lambda i, a, n, this: len(st_of(this).data), 0, 0),
    "get": (lambda i, a, n, this: as_text(st_of(this).data), 0, 0),
    "isValid": (lambda i, a, n, this: st_of(this).error is None, 0, 0),
    "error": (lambda i, a, n, this: st_of(this).error, 0, 0),
    "isImage": (lambda i, a, n, this: st_of(this).mime in IMAGE_TYPES, 0, 0),
    "dimensions": (u_dimensions, 0, 0),
    "hashName": (lambda i, a, n, this: hash_name(st_of(this)), 0, 0),
    "store": (u_store(False, False), 0, 2),
    "storeAs": (u_store(False, True), 2, 3),
    "storePublicly": (u_store(True, False), 0, 1),
    "storePubliclyAs": (u_store(True, True), 2, 2),
    "toJson": (u_to_json, 0, 0),
}


# ---- fakes for tests ---------------------------------------------------------

def f_fake(i, a, n):
    name = need(i, a[0], ("string",), "UploadedFile.fake", 1, n)
    body = a[1] if len(a) > 1 else ""
    if isinstance(body, int) and not isinstance(body, bool):
        if body < 0:
            i.throw("ValueException", "UploadedFile.fake(): the size in KB cannot be negative", n)
        data = b"\0" * (body * 1024)
    else:
        data = need(i, body, ("string",), "UploadedFile.fake", 2, n).encode("utf-8", "surrogateescape")
    mime = arg(i, a, 2, ("string",), "UploadedFile.fake", n, None) or \
        mimetypes.guess_type(name)[0] or "application/octet-stream"
    return new_upload(i, name, mime, data)


def png_bytes(w, h):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    raw = b"".join(b"\0" + b"\x80\x80\x80" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def f_fake_image(i, a, n):
    name = need(i, a[0], ("string",), "UploadedFile.fakeImage", 1, n)
    w = arg(i, a, 1, ("int",), "UploadedFile.fakeImage", n, 10)
    h = arg(i, a, 2, ("int",), "UploadedFile.fakeImage", n, 10)
    if not (0 < w <= 10000 and 0 < h <= 10000):
        i.throw("ValueException", "UploadedFile.fakeImage(): width and height must be between 1 and 10000", n)
    ext = posixpath.splitext(name)[1].lower()
    if ext in (".jpg", ".jpeg"):     # header-only JPEG: enough for type and dimension checks
        data = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
                b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", h, w) + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
                b"\xff\xd9")
        mime = "image/jpeg"
    elif ext == ".gif":
        data = b"GIF89a" + struct.pack("<HH", w, h) + b"\x00\x00\x00" + b",\x00\x00\x00\x00" + \
            struct.pack("<HH", w, h) + b"\x00\x02\x02D\x01\x00;"
        mime = "image/gif"
    else:
        data, mime = png_bytes(w, h), "image/png"
    return new_upload(i, name, mime, data)


UPLOAD_STATICS = {
    "fake": (f_fake, 1, 3),
    "fakeImage": (f_fake_image, 1, 3),
}


# ===========================================================================
# Request additions
# ===========================================================================

def req_files(this):
    st = this.native
    return getattr(st, "files", None) or {}


def r_file(i, a, n, this):
    key = need(i, a[0], ("string",), "Request.file", 1, n)
    return req_files(this).get(key, a[1] if len(a) > 1 else None)


def r_has_file(i, a, n, this):
    v = req_files(this).get(need(i, a[0], ("string",), "Request.hasFile", 1, n))
    if isinstance(v, list):
        return any(is_upload(x) and x.native.error is None for x in v)
    return is_upload(v) and v.native.error is None


REQUEST_UPLOAD = {
    "file": (r_file, 1, 2),
    "hasFile": (r_has_file, 1, 1),
    "files": (lambda i, a, n, this: dict(req_files(this)), 0, 0),
}


# ===========================================================================
# validation rules: file, image, mimes, mimetypes, extensions, dimensions
# (min/max/size/between measure files in kilobytes — see stdlib2.measure)
# ===========================================================================

MESSAGES = {
    "file": "The :field field must be a file.",
    "uploaded": "The :field failed to upload.",
    "image": "The :field field must be an image.",
    "mimes": "The :field field must be a file of type: :values.",
    "mimetypes": "The :field field must be a file of type: :values.",
    "extensions": "The :field field must have one of the following extensions: :values.",
    "dimensions": "The :field field has invalid image dimensions.",
    "min.file": "The :field field must be at least :min kilobytes.",
    "max.file": "The :field field must not be greater than :max kilobytes.",
    "between.file": "The :field field must be between :min and :max kilobytes.",
    "size.file": "The :field field must be :size kilobytes.",
}
DIMENSION_KEYS = ("width", "height", "min_width", "min_height", "max_width", "max_height", "ratio")


def check_dimension_args(i, args, n):
    for a in args:
        k, sep, v = a.partition("=")
        if not sep or k not in DIMENSION_KEYS:
            i.throw("ValueException", f'validation rule "dimensions": unknown option "{a}" (use '
                    f'{", ".join(DIMENSION_KEYS)}, e.g. dimensions:min_width=100,ratio=3/2)', n)
        if k == "ratio":
            if not re.match(r"^\d+(\.\d+)?(/\d+(\.\d+)?)?$", v) or float(v.split("/")[-1]) == 0:
                i.throw("ValueException", f'validation rule "dimensions": ratio must look like 3/2 or 1.5, got "{v}"', n)
        elif not v.isdigit():
            i.throw("ValueException", f'validation rule "dimensions": {k} needs a whole number, got "{v}"', n)


def dimensions_ok(st, args):
    d = dimensions(st.data, st.mime)
    if d is None:
        return False
    w, h = d
    for a in args:
        k, _, v = a.partition("=")
        if k == "ratio":
            top, _, bottom = v.partition("/")
            want = float(top) / float(bottom or 1)
            if h == 0 or abs(w / h - want) > 1 / max(w, h, 1):
                return False
            continue
        v = int(v)
        if (k == "width" and w != v) or (k == "height" and h != v) or (k == "min_width" and w < v) or \
                (k == "min_height" and h < v) or (k == "max_width" and w > v) or (k == "max_height" and h > v):
            return False
    return True


def check_rule(rule, args, value):
    """None if the rule passes, else (message key, placeholders). Only called for the file rules."""
    if not is_upload(value):
        return ("file" if rule == "file" else rule), {"values": ", ".join(args)}
    st = value.native
    if st.error:
        return "uploaded", {}
    ph = {"values": ", ".join(args)}
    if rule == "file":
        return None
    if rule == "image":
        ok = st.mime in IMAGE_TYPES or ("allow_svg" in args and st.mime == "image/svg+xml")
        return None if ok else ("image", {})
    if rule == "mimes":
        want = {x.lower().lstrip(".") for x in args}
        if "jpg" in want or "jpeg" in want:
            want |= {"jpg", "jpeg"}
        return None if st.ext in want else ("mimes", ph)
    if rule == "mimetypes":
        m = st.mime
        ok = any(m == x or (x.endswith("/*") and m.startswith(x[:-1])) for x in args)
        return None if ok else ("mimetypes", ph)
    if rule == "extensions":
        return None if st.client_ext in {x.lower().lstrip(".") for x in args} else ("extensions", ph)
    if rule == "dimensions":
        return None if dimensions_ok(st, args) else ("dimensions", {})
    return None


def safe_value(v):
    """Keep uploads out of flashed input (old())."""
    return not (is_upload(v) or (isinstance(v, list) and any(is_upload(x) for x in v)))


# ===========================================================================
# install
# ===========================================================================

def install(i):
    native_class(i, "UploadedFile", None, UPLOAD_METHODS, UPLOAD_STATICS)
    from .web import add_natives
    add_natives(i, "Request", REQUEST_UPLOAD)
    from .stdlib2 import STDLIB_ALIASES
    STDLIB_ALIASES.update({"Http\\UploadedFile": "UploadedFile"})
