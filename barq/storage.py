"""Framework part 3 (v0.11.0): file storage.

    Storage.put("reports/jan.txt", text)       the default disk ("local": storage/app/)
    Storage.disk("public").url("a.png")        "/storage/a.png" (after `barq storage:link`)
    Storage.fake("public")                     an in-memory disk for tests, with assertExists()

Disks come from config/filesystems.bq:
    #{"default": "local", "disks": #{"local": #{"driver": "local", "root": "storage/app"}, ...}}

Paths are relative to the disk root; ".." can never leave it. File contents
are strings — binary files survive a get()/put() round trip unchanged.
"""

import mimetypes
import os
import posixpath
import shutil
import time

from .builtins import need
from .runtime import BarqThrow, to_display, type_name
from .stdlib import arg, module
from .stdlib2 import make_response, native_class, new_native

DEFAULT_DISKS = {
    "local": {"driver": "local", "root": "storage/app"},
    "public": {"driver": "local", "root": "storage/app/public", "url": "/storage"},
}
DRIVERS = ("local", "memory", "s3")


def settings(i):
    from .stdlib2 import config_lookup
    conf = config_lookup(i, "filesystems")
    disks = dict(DEFAULT_DISKS)
    default = "local"
    if isinstance(conf, dict):
        if isinstance(conf.get("disks"), dict):
            disks.update(conf["disks"])
        default = conf.get("default") or default
    return default, disks


# ===========================================================================
# disks
# ===========================================================================

class DiskState:
    def __init__(self, name, cfg, root):
        self.name = name
        self.driver = cfg.get("driver", "local")
        self.url = cfg.get("url")
        self.root = root            # absolute folder (local driver)
        self.files = {}             # memory driver: path -> (text, mtime)
        self.dirs = set()           # memory driver: explicit directories
        self.s3 = None              # s3 driver: an s3.S3Client


def make_disk(i, name, cfg, n):
    if not isinstance(cfg, dict):
        i.throw("StorageException", f'disk "{name}" must be a dict like #{{"driver": "local", "root": "storage/app"}}', n)
    driver = cfg.get("driver", "local")
    if driver not in DRIVERS:
        i.throw("StorageException", f'disk "{name}": unknown driver {to_display(driver, i, True)} (use "local", '
                '"memory" or "s3")', n)
    root = None
    if driver == "s3":
        for key in ("bucket", "key", "secret"):
            if not isinstance(cfg.get(key), str) or not cfg.get(key):
                i.throw("StorageException", f'disk "{name}" (s3) needs a "{key}" — usually env("AWS_BUCKET"), '
                        'env("AWS_ACCESS_KEY_ID") and env("AWS_SECRET_ACCESS_KEY")', n)
        from .s3 import S3Client
        disk = new_native(i, "Disk", DiskState(name, cfg, None))
        disk.native.s3 = S3Client(cfg)
        return disk
    if driver == "local":
        r = cfg.get("root")
        if not isinstance(r, str) or not r:
            i.throw("StorageException", f'disk "{name}" needs a "root" folder', n)
        root = os.path.abspath(r if os.path.isabs(r) else os.path.join(i.root, r))
    return new_native(i, "Disk", DiskState(name, cfg, root))


def disks(i):
    return i.__dict__.setdefault("_disks", {})


def get_disk(i, name, n):
    default, configured = settings(i)
    name = name or default
    cache = disks(i)
    if name not in cache:
        if name not in configured:
            i.throw("StorageException", f'unknown disk "{name}" (disks: {", ".join(configured)}) — add it to '
                    'config/filesystems.bq under "disks"', n)
        cache[name] = make_disk(i, name, configured[name], n)
    return cache[name]


def clean(i, path, n, fname, allow_root=False):
    """A safe path inside the disk: "a/b.txt" (no leading slash; ".." may never leave the root)."""
    need(i, path, ("string",), fname, 1, n)
    if "\0" in path:
        i.throw("StorageException", f"{fname}(): the path contains a NUL byte", n)
    parts = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                i.throw("StorageException", f'{fname}(): "{path}" leaves the disk root', n)
            parts.pop()
        else:
            parts.append(part)
    if not parts and not allow_root:
        i.throw("StorageException", f"{fname}(): the path is empty", n)
    return "/".join(parts)


def full(st, p):
    return os.path.join(st.root, *p.split("/")) if p else st.root


def text_of(i, v, n, fname):
    if isinstance(v, str):
        return v
    from .upload import as_text, is_upload, need_valid
    if is_upload(v):
        need_valid(i, v.native, n, fname)
        return as_text(v.native.data)
    i.throw("TypeException", f"{fname}() needs the contents as a string, got {type_name(v)} — use Json.encode() for "
            "lists and dicts", n)


def io_error(i, fname, path, e, n):
    i.throw("StorageException", f"{fname}(): cannot use {path}: {e.strerror or e}", n)


# ---- primitive operations (all drivers) ------------------------------------

def remote(i, fname, n, fn, *args):
    """Run an S3 call; network and S3 errors become StorageException."""
    from .s3 import S3Error
    try:
        return fn(*args)
    except S3Error as e:
        i.throw("StorageException", f"{fname}(): S3 error {e.code}: {e.message}", n)
    except OSError as e:
        i.throw("StorageException", f"{fname}(): cannot reach S3: {getattr(e, 'reason', None) or e}", n)


def s3_dir_exists(st, p):
    keys, prefixes = st.s3.list(p + "/", True)
    return bool(keys or prefixes)

def mem_dirs_of(p):
    parts = p.split("/")[:-1]
    return {"/".join(parts[:k]) for k in range(1, len(parts) + 1)}


def d_exists(st, p, i=None, n=None):
    if st.s3 is not None:
        return remote(i, "Storage.exists", n, lambda: st.s3.head(p) is not None or s3_dir_exists(st, p))
    if st.driver == "memory":
        return p in st.files or p in st.dirs or any(f.startswith(p + "/") for f in st.files)
    return os.path.exists(full(st, p))


def is_file(st, p, i=None, n=None):
    if st.s3 is not None:
        return remote(i, "Storage", n, st.s3.head, p) is not None
    return p in st.files if st.driver == "memory" else os.path.isfile(full(st, p))


def d_read(i, st, p, n, fname):
    if st.s3 is not None:
        data = remote(i, fname, n, st.s3.get, p)
        return data.decode("utf-8", "surrogateescape") if data is not None else None
    if st.driver == "memory":
        entry = st.files.get(p)
        return entry[0] if entry else None
    path = full(st, p)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "surrogateescape")
    except OSError as e:
        io_error(i, fname, p, e, n)


def d_write(i, st, p, text, n, fname):
    if st.s3 is not None:
        remote(i, fname, n, st.s3.put, p, text.encode("utf-8", "surrogateescape"), mime_of(p))
        return
    if st.driver == "memory":
        st.files[p] = (text, int(time.time()))
        st.dirs |= mem_dirs_of(p)
        return
    path = full(st, p)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(text.encode("utf-8", "surrogateescape"))
        os.replace(tmp, path)
    except OSError as e:
        io_error(i, fname, p, e, n)


def d_delete(i, st, p, n):
    if st.s3 is not None:
        if remote(i, "Storage.delete", n, st.s3.head, p) is None:
            return False
        remote(i, "Storage.delete", n, st.s3.delete, p)
        return True
    if st.driver == "memory":
        return st.files.pop(p, None) is not None
    path = full(st, p)
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError as e:
        io_error(i, "Storage.delete", p, e, n)


def listing(i, st, p, dirs, recursive, n):
    """Files (or folders) under p, as disk paths, sorted."""
    prefix = p + "/" if p else ""
    if st.s3 is not None:
        keys, prefixes = remote(i, "Storage.files", n, st.s3.list, prefix, not recursive)
        if not dirs:
            return sorted(k for k in keys if not k.endswith("/"))
        if not recursive:
            return sorted(set(prefixes))
        names = {k.rstrip("/") for k in keys if k.endswith("/")} | {d for k in keys for d in mem_dirs_of(k)}
        return sorted(x for x in names if x.startswith(prefix) and x != p)
    if st.driver == "memory":
        if dirs:
            names = st.dirs | {d for f in st.files for d in mem_dirs_of(f)}
        else:
            names = set(st.files)
        out = [x for x in names if x.startswith(prefix) and x != p
               and (recursive or "/" not in x[len(prefix):])]
        return sorted(out)
    base = full(st, p)
    if not os.path.isdir(base):
        return []
    out = []
    if recursive:
        for folder, subdirs, files in os.walk(base):
            rel = os.path.relpath(folder, st.root).replace(os.sep, "/")
            rel = "" if rel == "." else rel + "/"
            for name in (subdirs if dirs else files):
                if not name.endswith(".tmp"):
                    out.append(rel + name)
    else:
        for name in os.listdir(base):
            if os.path.isdir(os.path.join(base, name)) == dirs and not name.endswith(".tmp"):
                out.append(prefix + name)
    return sorted(out)


# ===========================================================================
# Disk methods
# ===========================================================================

def ds(this):
    return this.native


def k_put(i, a, n, this):
    p = clean(i, a[0], n, "Storage.put")
    d_write(i, ds(this), p, text_of(i, a[1], n, "Storage.put"), n, "Storage.put")
    return True


def k_get(i, a, n, this):
    return d_read(i, ds(this), clean(i, a[0], n, "Storage.get"), n, "Storage.get")


def k_json(i, a, n, this):
    from .stdlib import json_decode
    text = k_get(i, a, n, this)
    return json_decode(i, [text], n) if text is not None else None


def k_add(where):
    fname = "Storage." + where

    def fn(i, a, n, this):
        st = ds(this)
        p = clean(i, a[0], n, fname)
        text = text_of(i, a[1], n, fname)
        sep = arg(i, a, 2, ("string",), fname, n, "\n")
        old = d_read(i, st, p, n, fname)
        if old is None or old == "":
            new = text
        else:
            new = old + sep + text if where == "append" else text + sep + old
        d_write(i, st, p, new, n, fname)
        return True
    return fn


def k_delete(i, a, n, this):
    paths = a[0] if isinstance(a[0], list) else [a[0]]
    ok = True
    for p in paths:
        ok = d_delete(i, ds(this), clean(i, p, n, "Storage.delete"), n) and ok
    return ok


def k_copy_move(move):
    fname = "Storage.move" if move else "Storage.copy"

    def fn(i, a, n, this):
        st = ds(this)
        src, dst = clean(i, a[0], n, fname), clean(i, a[1], n, fname)
        if st.s3 is not None:
            if remote(i, fname, n, st.s3.head, src) is None:
                i.throw("StorageException", f"{fname}(): {src} does not exist", n)
            remote(i, fname, n, st.s3.copy, src, dst)
            if move and src != dst:
                remote(i, fname, n, st.s3.delete, src)
            return True
        text = d_read(i, st, src, n, fname)
        if text is None:
            i.throw("StorageException", f"{fname}(): {src} does not exist", n)
        d_write(i, st, dst, text, n, fname)
        if move:
            d_delete(i, st, src, n)
        return True
    return fn


def need_file(i, st, p, n, fname):
    if not is_file(st, p, i, n):
        i.throw("StorageException", f"{fname}(): {p} does not exist", n)


def k_size(i, a, n, this):
    st = ds(this)
    p = clean(i, a[0], n, "Storage.size")
    if st.s3 is not None:
        h = remote(i, "Storage.size", n, st.s3.head, p)
        if h is None:
            i.throw("StorageException", f"Storage.size(): {p} does not exist", n)
        return int(h.get("content-length") or 0)
    need_file(i, st, p, n, "Storage.size")
    if st.driver == "memory":
        return len(st.files[p][0].encode("utf-8", "surrogateescape"))
    return os.path.getsize(full(st, p))


def k_modified(i, a, n, this):
    st = ds(this)
    p = clean(i, a[0], n, "Storage.lastModified")
    if st.s3 is not None:
        from .s3 import modified_of
        h = remote(i, "Storage.lastModified", n, st.s3.head, p)
        if h is None:
            i.throw("StorageException", f"Storage.lastModified(): {p} does not exist", n)
        return modified_of(h)
    need_file(i, st, p, n, "Storage.lastModified")
    if st.driver == "memory":
        return st.files[p][1]
    return int(os.path.getmtime(full(st, p)))


def mime_of(p):
    return mimetypes.guess_type(p)[0] or "application/octet-stream"


def k_mime(i, a, n, this):
    p = clean(i, a[0], n, "Storage.mime")
    need_file(i, ds(this), p, n, "Storage.mime")
    return mime_of(p)


def k_path(i, a, n, this):
    st = ds(this)
    p = clean(i, arg(i, a, 0, ("string",), "Storage.path", n, ""), n, "Storage.path", allow_root=True)
    if st.driver == "memory":
        i.throw("StorageException", f'Storage.path(): disk "{st.name}" lives in memory and has no folder', n)
    if st.s3 is not None:
        i.throw("StorageException", f'Storage.path(): disk "{st.name}" is an S3 bucket and has no local folder — '
                'use url() or temporaryUrl()', n)
    return full(st, p)


def k_url(i, a, n, this):
    st = ds(this)
    p = clean(i, a[0], n, "Storage.url")
    if st.s3 is not None:
        return st.s3.public_url(p)
    if not st.url:
        i.throw("StorageException", f'Storage.url(): disk "{st.name}" has no public URL — store public files on '
                'the "public" disk, or give this disk a "url" in config/filesystems.bq', n)
    from urllib.parse import quote
    return st.url.rstrip("/") + "/" + quote(p)


def k_temporary_url(i, a, n, this):
    """A link that works for `seconds` (S3 presigned GET; 1 second to 7 days)."""
    st = ds(this)
    p = clean(i, a[0], n, "Storage.temporaryUrl")
    seconds = need(i, a[1], ("int",), "Storage.temporaryUrl", 2, n)
    if not 1 <= seconds <= 604800:
        i.throw("ValueException", "Storage.temporaryUrl(): seconds must be between 1 and 604800 (7 days)", n)
    if st.s3 is None:
        i.throw("StorageException", f'Storage.temporaryUrl(): disk "{st.name}" ({st.driver}) has no temporary URLs '
                '— only s3 disks do; for local files use a signed route (URL.temporarySignedRoute)', n)
    return st.s3.temporary_url(p, seconds)


def k_put_file(named):
    """putFile(folder, file[, options]) / putFileAs(folder, file, name[, options]) -> the stored path."""
    fname = "Storage.putFileAs" if named else "Storage.putFile"

    def fn(i, a, n, this):
        from .upload import hash_name, is_upload, store_on
        folder = need(i, a[0], ("string",), fname, 1, n)
        f = a[1]
        if not is_upload(f):
            i.throw("TypeException", f"{fname}() argument 2 must be UploadedFile, got {type_name(f)}", n)
        name = need(i, a[2], ("string",), fname, 3, n) if named else hash_name(f.native)
        return store_on(i, this, folder, name, f.native, n, fname)
    return fn


def k_list(dirs, recursive):
    fname = "Storage." + ("allDirectories" if dirs and recursive else "directories" if dirs else
                          "allFiles" if recursive else "files")

    def fn(i, a, n, this):
        p = clean(i, arg(i, a, 0, ("string",), fname, n, ""), n, fname, allow_root=True)
        deep = recursive or (not dirs and arg(i, a, 1, ("bool",), fname, n, False))
        return listing(i, ds(this), p, dirs, deep, n)
    return fn


def k_make_dir(i, a, n, this):
    st = ds(this)
    p = clean(i, a[0], n, "Storage.makeDirectory")
    if st.s3 is not None:
        remote(i, "Storage.makeDirectory", n, st.s3.put, p + "/", b"", "application/x-directory")
        return True
    if st.driver == "memory":
        st.dirs |= mem_dirs_of(p + "/x")
        return True
    try:
        os.makedirs(full(st, p), exist_ok=True)
    except OSError as e:
        io_error(i, "Storage.makeDirectory", p, e, n)
    return True


def k_delete_dir(i, a, n, this):
    st = ds(this)
    p = clean(i, a[0], n, "Storage.deleteDirectory")
    if st.s3 is not None:
        keys, _ = remote(i, "Storage.deleteDirectory", n, st.s3.list, p + "/", False)
        for k in keys:
            remote(i, "Storage.deleteDirectory", n, st.s3.delete, k)
        return bool(keys)
    if st.driver == "memory":
        gone = [f for f in st.files if f.startswith(p + "/")]
        for f in gone:
            del st.files[f]
        before = len(st.dirs)
        st.dirs = {d for d in st.dirs if d != p and not d.startswith(p + "/")}
        return bool(gone) or len(st.dirs) != before
    path = full(st, p)
    if not os.path.isdir(path):
        return False
    try:
        shutil.rmtree(path)
    except OSError as e:
        io_error(i, "Storage.deleteDirectory", p, e, n)
    return True


def k_response(download):
    fname = "Storage.download" if download else "Storage.response"

    def fn(i, a, n, this):
        st = ds(this)
        p = clean(i, a[0], n, fname)
        text = d_read(i, st, p, n, fname)
        if text is None:
            from .web import http_abort
            http_abort(i, [404], n)
        headers = {"Content-Type": mime_of(p)}
        if download:
            name = arg(i, a, 1, ("string",), fname, n, None) or posixpath.basename(p)
            headers["Content-Disposition"] = 'attachment; filename="' + name.replace('"', "") + '"'
        return make_response(i, text, 200, headers)
    return fn


def fail(i, message, n):
    obj = i.make_exception("AssertionException", message, n)
    raise BarqThrow(obj)


def k_assert(exists):
    fname = "Storage.assertExists" if exists else "Storage.assertMissing"

    def fn(i, a, n, this):
        st = ds(this)
        for p in (a[0] if isinstance(a[0], list) else [a[0]]):
            p = clean(i, p, n, fname)
            if d_exists(st, p, i, n) != exists:
                fail(i, f'{"expected" if exists else "did not expect"} {p} on disk "{st.name}"', n)
        return None
    return fn


DISK_METHODS = {
    "put": (k_put, 2, 2),
    "get": (k_get, 1, 1),
    "json": (k_json, 1, 1),
    "exists": (lambda i, a, n, this: d_exists(ds(this), clean(i, a[0], n, "Storage.exists"), i, n), 1, 1),
    "missing": (lambda i, a, n, this: not d_exists(ds(this), clean(i, a[0], n, "Storage.missing"), i, n), 1, 1),
    "append": (k_add("append"), 2, 3),
    "prepend": (k_add("prepend"), 2, 3),
    "delete": (k_delete, 1, 1),
    "copy": (k_copy_move(False), 2, 2),
    "move": (k_copy_move(True), 2, 2),
    "size": (k_size, 1, 1),
    "lastModified": (k_modified, 1, 1),
    "mime": (k_mime, 1, 1),
    "path": (k_path, 0, 1),
    "url": (k_url, 1, 1),
    "temporaryUrl": (k_temporary_url, 2, 2),
    "putFile": (k_put_file(False), 2, 3),
    "putFileAs": (k_put_file(True), 3, 4),
    "files": (k_list(False, False), 0, 2),
    "allFiles": (k_list(False, True), 0, 1),
    "directories": (k_list(True, False), 0, 1),
    "allDirectories": (k_list(True, True), 0, 1),
    "makeDirectory": (k_make_dir, 1, 1),
    "deleteDirectory": (k_delete_dir, 1, 1),
    "response": (k_response(False), 1, 1),
    "download": (k_response(True), 1, 2),
    "assertExists": (k_assert(True), 1, 1),
    "assertMissing": (k_assert(False), 1, 1),
    "name": (lambda i, a, n, this: ds(this).name, 0, 0),
}


# ===========================================================================
# Storage module: the default disk, plus disk(), fake(), build()
# ===========================================================================

def on_default(fn):
    return lambda i, a, n: fn(i, a, n, get_disk(i, None, n))


def s_disk(i, a, n):
    return get_disk(i, arg(i, a, 0, ("string",), "Storage.disk", n, None), n)


def s_fake(i, a, n):
    name = arg(i, a, 0, ("string",), "Storage.fake", n, None) or settings(i)[0]
    old = disks(i).get(name)
    url = old.native.url if old is not None else settings(i)[1].get(name, {}).get("url") \
        if isinstance(settings(i)[1].get(name), dict) else None
    disk = make_disk(i, name, {"driver": "memory", "url": url}, n)
    disks(i)[name] = disk
    return disk


def s_build(i, a, n):
    cfg = need(i, a[0], ("dict",), "Storage.build", 1, n)
    return make_disk(i, "build", cfg, n)


STORAGE = {k: (on_default(fn), lo, hi) for k, (fn, lo, hi) in DISK_METHODS.items()}
STORAGE.update({"disk": (s_disk, 0, 1), "fake": (s_fake, 0, 1), "build": (s_build, 1, 1)})


def install(i):
    from .runtime import BarqClass
    g = i.builtins
    cls = BarqClass("StorageException", "StorageException", None, parent=i.classes["Exception"], native=True)
    i.classes["StorageException"] = cls
    g.declare("StorageException", cls, None, const=True)
    native_class(i, "Disk", None, DISK_METHODS)
    g.declare("Storage", module("Storage", STORAGE), None, const=True)
    from .stdlib2 import STDLIB_ALIASES
    STDLIB_ALIASES.update({"Storage\\Storage": "Storage", "Storage\\Disk": "Disk"})
