"""Barq language test suite. Run from the repo root:

    python3 tests/test_barq.py

Every program runs twice: on the VM (compiled, the default) and on the
tree-walking interpreter (`--interp`). Both must pass, and both must print
exactly the same thing, errors included.

tests/cases/NAME.bq   must run and print exactly tests/cases/NAME.out
tests/errors/NAME.bq  must fail; its first line is `// expect: <text>` and
                      that text must appear in the error output.
examples/**/*.bq      entry points (files without a namespace) must run cleanly.
tests/http/           Http client and server, run against real local sockets.
tests/testing/        `barq test` on a small project; output must match expected.out.
tests/cli/            a Cli program run with arguments and stdin.
ServeCommand          barq serve without packages.
NewCommand            barq new: each kind of project checks, formats and passes its own tests.
BuildCommand          barq build: dist/ runs alone after being moved or unzipped.
CheckAndPackages      barq check (--json) and barq packages.
Versions              SemVer order, version constraints and barq.pkg parsing.
PackageManager        barq install / update / remove / publish on git repos in a temp folder.
LanguageServer        `barq lsp` driven over stdin/stdout like an editor, on tests/lsp/.
Debugging             `barq debug` with piped commands and `barq dap` driven like an editor,
                      on tests/debug/.
Cache                 .bqc files are written, reused, and refreshed when the source changes.
Formatter             `barq fmt`: the style rules, every .bq file keeps its tokens and
                      formats the same twice, formatted test programs print the same.
NativeEngine          `barq run --native` (the Rust VM in native/) prints exactly what the
                      tree-walker prints, for every program above it can run; skipped when
                      native/ is not built (cargo build --release --manifest-path native/Cargo.toml).

Every barq process here runs with BARQ_PACKAGES=none: the language must work
without barq-framework, barq-auth or barq-permissions. Their own suites live in
their repos.
"""

import glob
import http.cookiejar
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINES = ("vm", "tree")


sys.path.insert(0, ROOT)
from barq import devtest  # noqa: E402
from barq.devtest import barq_env, run_barq, run_both  # noqa: E402,F401

# The language must work with no package installed (barq-framework, barq-auth...).
devtest.PACKAGES = "none"


class GoldenOutput(unittest.TestCase):
    pass


class ExpectedErrors(unittest.TestCase):
    pass


class Examples(unittest.TestCase):
    pass


def add_golden(path):
    name = os.path.splitext(os.path.basename(path))[0]

    def test(self):
        with open(path[:-3] + ".out", encoding="utf-8") as f:
            expected = f.read()
        code, out, err = run_both(self, path)
        self.assertEqual(code, 0, f"exit code {code}\n{err}")
        self.assertEqual(out, expected)
    setattr(GoldenOutput, f"test_{name}", test)


def add_error(path):
    name = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding="utf-8") as f:
        first = f.readline().strip()
    assert first.startswith("// expect:"), f"{path} needs a '// expect:' first line"
    expected = first[len("// expect:"):].strip()

    def test(self):
        code, out, err = run_both(self, path)
        self.assertNotEqual(code, 0, f"expected failure, got success:\n{out}")
        self.assertIn(expected, err)
    setattr(ExpectedErrors, f"test_{name}", test)


def add_example(path):
    name = os.path.relpath(path, os.path.join(ROOT, "examples")).replace(os.sep, "_")[:-3]
    with open(path, encoding="utf-8") as f:
        if any(line.startswith("namespace ") for line in f):
            return  # library file, loaded by an entry point

    def test(self):
        code, out, err = run_both(self, path)
        self.assertEqual(code, 0, f"exit code {code}\n{err}")
    setattr(Examples, f"test_{name}", test)


class NativeEngine(unittest.TestCase):
    pass


# Programs that use what the native VM cannot run yet (it stops with "NativeError: ...
# is not supported by the native VM yet"). Everything else must run natively and
# print exactly what the reference interpreter prints.
NATIVE_NOT_YET = {
    "tests/cases/templates_routes.bq",
    "tests/cases/uploads.bq", "tests/cases/web.bq",
    "examples/notes/main.bq", "examples/shop/main.bq",
}


def add_native(path):
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
    with open(path, encoding="utf-8") as f:
        if any(line.startswith("namespace ") for line in f):
            return  # library file

    def test(self):
        from barq.native import find_binary
        if find_binary() is None:
            self.skipTest("native/ is not built")
        native = run_barq(path, engine="native")
        unsupported = "is not supported by the native VM yet" in native[2]
        if rel in NATIVE_NOT_YET:
            self.assertTrue(unsupported, f"{rel} now runs natively: take it out of NATIVE_NOT_YET")
            return
        self.assertFalse(unsupported, native[2])
        self.assertEqual(native, run_barq(path, engine="tree"), "the native VM and the tree-walker disagree")
    setattr(NativeEngine, "test_" + rel[:-3].replace("/", "_"), test)


for p in sorted(glob.glob(os.path.join(HERE, "cases", "*.bq"))):
    add_golden(p)
for p in sorted(glob.glob(os.path.join(HERE, "errors", "*.bq"))):
    add_error(p)
for p in sorted(glob.glob(os.path.join(ROOT, "examples", "**", "*.bq"), recursive=True)):
    if os.sep + "views" + os.sep not in p:   # templates, not programs
        add_example(p)
for p in (sorted(glob.glob(os.path.join(HERE, "cases", "*.bq"))) + sorted(glob.glob(os.path.join(HERE, "errors", "*.bq")))
          + sorted(glob.glob(os.path.join(ROOT, "benchmarks", "*.bq")))
          + sorted(p for p in glob.glob(os.path.join(ROOT, "examples", "**", "*.bq"), recursive=True)
                   if os.sep + "views" + os.sep not in p)):
    add_native(p)


class BuildCommand(unittest.TestCase):
    """barq build: dist/ runs on its own (no PYTHONPATH, no checkout), after being moved or unzipped."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="barq-build-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def barq(self, *args, cwd):
        return devtest.barq_cli(*args, cwd=cwd)

    def new(self, name, *flags):
        code, _, err = self.barq("new", name, *flags, "--no-packages", cwd=self.dir)
        self.assertEqual(code, 0, err)
        return os.path.join(self.dir, name)

    def launch(self, script, *args):
        """Run a script made by barq build with nothing from this checkout in the environment."""
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.dir, "PYTHON": sys.executable,
               "NO_COLOR": "1"}
        proc = subprocess.run([script, *args], capture_output=True, text=True, env=env, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr

    def test_console_app(self):
        app = self.new("tool", "--console")
        os.makedirs(os.path.join(app, "docs"))
        os.makedirs(os.path.join(app, "storage", "logs"))
        for rel in ("docs/notes.md", "storage/logs/today.log", "storage/.gitkeep", "local.db", "keep.txt"):
            with open(os.path.join(app, rel), "w") as f:
                f.write("x\n")
        with open(os.path.join(app, "barq.json")) as f:
            data = json.load(f)
        data["build"] = {"exclude": ["docs", "README.md"]}
        with open(os.path.join(app, "barq.json"), "w") as f:
            json.dump(data, f)
        code, out, err = self.barq("build", cwd=app)
        self.assertEqual(code, 0, out + err)
        self.assertIn("runs main.bq", out)
        self.assertIn("left out .env", out)
        dist = os.path.join(app, "dist")
        for rel in ("app/main.bq", "app/app/Greeter.bq", "app/keep.txt", "app/barq.json", "app/storage/.gitkeep",
                    "app/.barq/cache/main.bqc", "app/.barq/cache/app/Greeter.bqc", "runtime/barq/barq/vm.py",
                    "runtime/barq/LICENSE", "barq", "barq.cmd", "start", "start.cmd", "build.json"):
            self.assertTrue(os.path.exists(os.path.join(dist, rel)), rel)
        for rel in ("app/tests", "app/.env", "app/docs", "app/README.md", "app/local.db",
                    "app/storage/logs/today.log", "app/dist"):
            self.assertFalse(os.path.exists(os.path.join(dist, rel)), rel)
        self.assertTrue(os.path.isdir(os.path.join(dist, "app/storage/logs")))
        with open(os.path.join(dist, "build.json")) as f:
            m = json.load(f)
        self.assertEqual((m["name"], m["entry"], m["packages"], m["compiled"]),
                         ("tool", {"kind": "console", "file": "main.bq"}, [], 2))
        # Moved somewhere else, it still runs, and its cache follows it.
        moved = os.path.join(self.dir, "server", "tool")
        os.makedirs(os.path.dirname(moved))
        os.rename(dist, moved)
        code, out, err = self.launch(os.path.join(moved, "start"), "Sara")
        self.assertEqual((code, out, err), (0, "Salaam, Sara!\n", ""))
        with open(os.path.join(moved, "app/.barq/cache/main.bqc"), "rb") as f:
            self.assertIn(("<barq>" + os.path.join(moved, "app", "main.bq")).encode(), f.read())
        code, out, _ = self.launch(os.path.join(moved, "barq"), "version")
        self.assertEqual(out, f"barq {devtest_version()}\n")

    def test_web_app_and_zip(self):
        app = self.new("site")
        os.makedirs(os.path.join(app, "storage", "app", "public"))
        self.assertEqual(self.barq("storage:link", cwd=app)[0], 0)
        code, out, err = self.barq("build", "--out", "../out", "--zip", "--include", "tests", "--with-env", cwd=app)
        self.assertEqual(code, 0, out + err)
        out_dir = os.path.join(self.dir, "out")
        self.assertIn("a web app", out)
        self.assertNotIn("left out .env", out)
        for rel in ("app/.env", "app/tests/feature/HomeTest.bq", "app/views/home.bq", "app/public/css/app.css"):
            self.assertTrue(os.path.isfile(os.path.join(out_dir, rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(out_dir, "app/.barq/cache/views")))
        self.assertEqual(os.readlink(os.path.join(out_dir, "app/public/storage")),
                         os.path.join("..", "storage", "app", "public"))
        with open(os.path.join(out_dir, "start")) as f:
            self.assertIn('serve --no-reload --host "${HOST:-127.0.0.1}"', f.read())
        server = os.path.join(self.dir, "server")
        import zipfile
        with zipfile.ZipFile(out_dir + ".zip") as z:
            self.assertEqual(z.getinfo("out/start").external_attr >> 16 & 0o777, 0o755)
            z.extractall(server)
        for name in ("start", "barq"):
            os.chmod(os.path.join(server, "out", name), 0o755)     # extractall drops modes
        code, out, err = self.launch(os.path.join(server, "out", "barq"), "route:list")
        self.assertEqual(code, 0, out + err)
        self.assertIn("HomeController", out)
        code, out, err = self.launch(os.path.join(server, "out", "barq"), "test")
        self.assertEqual(code, 0, out + err)

    def test_refusals(self):
        app = self.new("app", "--console")
        self.assertEqual(self.barq("build", cwd=app)[0], 0)
        with open(os.path.join(app, "dist", "build.json")) as f:
            first = f.read()
        with open(os.path.join(app, "app", "Greeter.bq"), "a") as f:
            f.write("\nint broken = \"text\"\n")
        code, out, err = self.barq("build", cwd=app)
        self.assertEqual(code, 1)
        self.assertIn("Greeter.bq", err)
        self.assertIn("didn't compile", err)
        with open(os.path.join(app, "dist", "build.json")) as f:
            self.assertEqual(f.read(), first)        # the last good build is still there
        self.assertEqual([n for n in os.listdir(app) if n.startswith(".dist")], [])
        mine = os.path.join(app, "mine")
        os.makedirs(mine)
        with open(os.path.join(mine, "notes.txt"), "w") as f:
            f.write("mine\n")
        for args, text in ((("--out", "mine"), "use --force"), (("--out", ".."), "the project itself"),
                           (("--nope",), "unknown option"), (("--entry", "app/Greeter.bq"), "in the project folder"),
                           (("--entry", "gone.bq"), "no such file"), (("--out",), "needs a value")):
            with self.subTest(args=args):
                code, _, err = self.barq("build", *args, cwd=app)
                self.assertEqual(code, 1, err)
                self.assertIn(text, err)
        self.assertTrue(os.path.isfile(os.path.join(mine, "notes.txt")))
        code, _, err = self.barq("build", cwd=self.dir)
        self.assertIn("project folder", err)
        with open(os.path.join(app, "barq.json"), "w") as f:
            json.dump({"name": "app", "require": {"someone/thing": "^1.0"}}, f)
        code, _, err = self.barq("build", cwd=app)
        self.assertIn("run barq install first", err)


def devtest_version():
    from barq import __version__
    return __version__


class Cache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="barq-cache-")
        self.main = os.path.join(self.dir, "main.bq")
        os.makedirs(os.path.join(self.dir, "app"))
        with open(os.path.join(self.dir, "app", "Greeter.bq"), "w", encoding="utf-8") as f:
            f.write("namespace App\n\nclass Greeter\n"
                    "    #[string]\n    karo hi(string n)\n        return \"hi {n}\"\n")
        self.write_main('print(new Greeter().hi("one"))')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_main(self, line):
        with open(self.main, "w", encoding="utf-8") as f:
            f.write("use App\\Greeter\n" + line + "\n")

    def cache_file(self, rel):
        return os.path.join(self.dir, ".barq", "cache", rel)

    def test_written_reused_and_refreshed(self):
        self.assertEqual(run_barq(self.main, cache=True), (0, "hi one\n", ""))
        main_bqc = self.cache_file("main.bqc")
        self.assertTrue(os.path.isfile(main_bqc))
        self.assertTrue(os.path.isfile(self.cache_file(os.path.join("app", "Greeter.bqc"))))
        stamp = os.path.getmtime(main_bqc)
        time.sleep(0.05)
        self.assertEqual(run_barq(self.main, cache=True), (0, "hi one\n", ""))
        self.assertEqual(os.path.getmtime(main_bqc), stamp, "unchanged source must reuse the cache")
        self.write_main('print(new Greeter().hi("two"))')
        self.assertEqual(run_barq(self.main, cache=True), (0, "hi two\n", ""))
        self.assertNotEqual(os.path.getmtime(main_bqc), stamp, "changed source must refresh the cache")

    def test_errors_still_point_at_source(self):
        self.write_main("int n = 0\nprint(10 / n)")
        for _ in range(2):   # first run compiles, second run uses the cache
            code, out, err = run_barq(self.main, cache=True)
            self.assertEqual(code, 1)
            self.assertIn("DivisionByZeroException: division by zero", err)
            self.assertIn("main.bq:3:10", err)

    def test_corrupt_cache_is_ignored(self):
        run_barq(self.main, cache=True)
        with open(self.cache_file("main.bqc"), "wb") as f:
            f.write(b"garbage")
        self.assertEqual(run_barq(self.main, cache=True), (0, "hi one\n", ""))

    def test_compile_command(self):
        proc = subprocess.run([sys.executable, "-m", "barq", "compile", self.dir],
                              capture_output=True, text=True, env=barq_env(cache=True), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("compiled 2 files", proc.stdout)
        self.assertTrue(os.path.isfile(self.cache_file("main.bqc")))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EchoHandler(http.server.BaseHTTPRequestHandler):
    """Answers every request with JSON describing it (for the client test)."""

    def log_message(self, *args):
        pass

    def answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        if self.path.startswith("/missing"):
            status, out = 404, b"no such page"
            ctype = "text/plain"
        else:
            status, ctype = 200, "application/json"
            out = json.dumps({"method": self.command, "path": self.path, "body": body,
                              "token": self.headers.get("X-Token"), "auth": self.headers.get("Authorization"),
                              "ctype": self.headers.get("Content-Type")}).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = answer


class FakeS3(http.server.BaseHTTPRequestHandler):
    """A tiny S3: path-style bucket "media", checks every SigV4 signature (header or presigned)."""
    protocol_version = "HTTP/1.1"
    objects = {}        # key -> (bytes, content type, mtime)
    SECRET = "testsecret"

    def log_message(self, *args):
        pass

    @staticmethod
    def hmac(key, msg):
        import hmac as _hmac, hashlib
        return _hmac.new(key, msg.encode(), hashlib.sha256).digest()

    def signature(self, amz_date, region, canonical):
        import hashlib
        scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
        sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        k = self.hmac(("AWS4" + self.SECRET).encode(), amz_date[:8])
        for part in (region, "s3", "aws4_request"):
            k = self.hmac(k, part)
        import hmac as _hmac
        return _hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()

    def verified(self, body):
        import hashlib, re, urllib.parse as up
        path, _, query = self.path.partition("?")
        q = up.parse_qsl(query, keep_blank_values=True)
        enc = lambda x: up.quote(x, safe="-_.~")  # noqa: E731
        qd = dict(q)
        if "X-Amz-Signature" in qd:
            rest = sorted((k, v) for k, v in q if k != "X-Amz-Signature")
            canonical = "\n".join([self.command, path, "&".join(f"{enc(k)}={enc(v)}" for k, v in rest),
                                   f"host:{self.headers['Host']}\n", "host", "UNSIGNED-PAYLOAD"])
            region = qd["X-Amz-Credential"].split("/")[2]
            return self.signature(qd["X-Amz-Date"], region, canonical) == qd["X-Amz-Signature"]
        m = re.match(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request, "
                     r"SignedHeaders=([^,]+), Signature=([0-9a-f]{64})$", self.headers.get("Authorization") or "")
        if not m or self.headers.get("x-amz-content-sha256") != hashlib.sha256(body).hexdigest():
            return False
        names = m.group(4).split(";")
        if "host" not in names or "x-amz-date" not in names:
            return False
        canonical = "\n".join([self.command, path, "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(q)),
                               "".join(f"{h}:{self.headers[h].strip()}\n" for h in names), m.group(4),
                               self.headers["x-amz-content-sha256"]])
        return self.signature(self.headers["x-amz-date"], m.group(3), canonical) == m.group(5)

    def reply(self, status, body=b"", ctype="application/xml", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def error(self, status, code):
        self.reply(status, f"<Error><Code>{code}</Code><Message>{code} happened</Message></Error>".encode())

    def handle_any(self):
        import email.utils, urllib.parse as up
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self.verified(body):
            return self.error(403, "SignatureDoesNotMatch")
        path, _, query = self.path.partition("?")
        bucket, _, key = up.unquote(path[1:]).partition("/")
        if bucket != "media":
            return self.error(404, "NoSuchBucket")
        objs = FakeS3.objects
        if not key and self.command == "GET":
            q = dict(up.parse_qsl(query, keep_blank_values=True))
            prefix, delim = q.get("prefix", ""), q.get("delimiter")
            entries = set()
            for k in objs:
                if not k.startswith(prefix):
                    continue
                rest = k[len(prefix):]
                if delim and delim in rest:
                    entries.add(("P", prefix + rest.split(delim)[0] + delim))
                else:
                    entries.add(("K", k))
            entries = sorted(entries, key=lambda e: e[1])
            start = int(q.get("continuation-token") or 0)
            page = entries[start:start + 2]       # tiny pages, so paging is exercised
            more = start + 2 < len(entries)
            xml = ['<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
                   f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"]
            if more:
                xml.append(f"<NextContinuationToken>{start + 2}</NextContinuationToken>")
            for kind, name in page:
                esc = name.replace("&", "&amp;").replace("<", "&lt;")
                xml.append(f"<Contents><Key>{esc}</Key></Contents>" if kind == "K" else
                           f"<CommonPrefixes><Prefix>{esc}</Prefix></CommonPrefixes>")
            xml.append("</ListBucketResult>")
            return self.reply(200, "".join(xml).encode())
        if self.command == "PUT":
            src = self.headers.get("x-amz-copy-source")
            if src:
                skey = up.unquote(src).lstrip("/").partition("/")[2]
                if skey not in objs:
                    return self.error(404, "NoSuchKey")
                objs[key] = objs[skey]
                return self.reply(200, b"<CopyObjectResult></CopyObjectResult>")
            objs[key] = (body, self.headers.get("Content-Type") or "binary/octet-stream", time.time())
            return self.reply(200)
        if self.command == "DELETE":
            objs.pop(key, None)
            return self.reply(204)
        if key not in objs:
            return self.error(404, "NoSuchKey")
        data, ctype, mtime = objs[key]
        self.reply(200, data, ctype, {"Last-Modified": email.utils.formatdate(mtime, usegmt=True)})

    do_GET = do_PUT = do_DELETE = do_HEAD = do_POST = handle_any


class S3Disk(unittest.TestCase):
    def test_signature_v4_matches_aws_examples(self):
        import datetime as dt
        sys.path.insert(0, ROOT)
        from barq import s3
        now = dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc)
        key, secret = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        h = s3.sign_headers("GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"},
                            s3.EMPTY_HASH, key, secret, "us-east-1", now)
        self.assertTrue(h["authorization"].endswith(
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))
        url = s3.presign("GET", "https://examplebucket.s3.amazonaws.com/test.txt", key, secret, "us-east-1", now, 86400)
        self.assertTrue(url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"))

    def test_disk(self):
        with open(os.path.join(HERE, "http", "s3.out"), encoding="utf-8") as f:
            expected = f.read()
        for engine in ENGINES:
            with self.subTest(engine=engine):
                FakeS3.objects = {}
                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeS3)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    code, out, err = run_barq(os.path.join(HERE, "http", "s3.bq"),
                                              f"http://127.0.0.1:{server.server_address[1]}", engine=engine)
                finally:
                    server.shutdown()
                    server.server_close()
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(out, expected)
                self.assertNotIn("reports/jan.txt", FakeS3.objects)
                self.assertIn("app/img/x.png", FakeS3.objects)


class Http(unittest.TestCase):
    def test_client(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.client(engine)

    def test_server(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.server(engine)

    def client(self, engine):
        server = http.server.HTTPServer(("127.0.0.1", 0), EchoHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, out, err = run_barq(os.path.join(HERE, "http", "client.bq"), base, engine=engine)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 0, err)
        with open(os.path.join(HERE, "http", "client.out"), encoding="utf-8") as f:
            self.assertEqual(out, f.read())

    def server(self, engine):
        port = free_port()
        requests = [
            ("GET", "/", None, None),
            ("GET", "/hello?name=Barq", None, None),
            ("POST", "/echo?page=2", "a=1&b=two", "application/x-www-form-urlencoded"),
            ("POST", "/users", json.dumps({"name": "A", "email": "bad"}), "application/json"),
            ("POST", "/users", json.dumps({"name": "Alice", "email": "a@b.co", "admin": True}), "application/json"),
            ("GET", "/site.css", None, None),
            ("GET", "/../server.bq", None, None),
            ("GET", "/boom", None, None),
            ("GET", "/old", None, None),   # redirect is followed: 2 requests
        ]
        proc = subprocess.Popen([sys.executable, "-m", "barq", "run", "--interp" if engine == "tree" else "--vm",
                                 os.path.join(HERE, "http", "server.bq"),
                                 str(port), str(len(requests) + 2)],  # + probe + redirect,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=barq_env())
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        pass
                    break
                except OSError:
                    time.sleep(0.05)
            # the connection probe above counts as a request for the server loop
            results = []
            for method, path, body, ctype in requests:
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                             data=body.encode() if body else None,
                                             headers={"Content-Type": ctype, "X-Test": "yes"} if ctype else {"X-Test": "yes"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as res:
                        results.append((res.status, res.read().decode(), res.headers.get("Content-Type")))
                except urllib.error.HTTPError as e:
                    results.append((e.code, e.read().decode(), e.headers.get("Content-Type")))
                    e.close()
            out, err = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(results[0], (200, "<h1>Barq</h1>", "text/html; charset=utf-8"))
        self.assertEqual(json.loads(results[1][1]), {"hello": "Barq", "method": "GET"})
        self.assertEqual(json.loads(results[2][1]), {"all": {"page": "2", "a": "1", "b": "two"}, "agent": "yes", "json": False})
        self.assertEqual(results[3][0], 422)
        self.assertEqual(sorted(json.loads(results[3][1])["errors"]), ["email", "name"])
        self.assertEqual((results[4][0], json.loads(results[4][1])), (201, {"name": "Alice", "email": "a@b.co"}))
        self.assertEqual(results[5], (200, "body { color: green; }\n", "text/css"))
        self.assertEqual(results[6][0], 404)
        self.assertEqual(results[7][0], 500)
        self.assertEqual(json.loads(results[8][1])["hello"], "world")
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out, "server done\n")

    def test_uploads(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.uploads(engine)

    def uploads(self, engine):
        import struct, zlib
        port = free_port()
        folder = tempfile.mkdtemp(prefix="barq-uploads-")
        raw = b"".join(b"\0" + bytes(range(256))[:3 * 7] for _ in range(5))   # every byte value shows up
        chunk = lambda kind, data: (struct.pack(">I", len(data)) + kind + data +  # noqa: E731
                                    struct.pack(">I", zlib.crc32(kind + data)))
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 7, 5, 8, 2, 0, 0, 0)) +
               chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"") + bytes(range(256)))
        b = "XyZBoundary42"
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="title"\r\n\r\nبرق ⚡\r\n'.encode(),
                 f'--{b}\r\nContent-Disposition: form-data; name="tags[]"\r\n\r\na\r\n'.encode(),
                 f'--{b}\r\nContent-Disposition: form-data; name="tags[]"\r\n\r\nb\r\n'.encode(),
                 f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="C:\\pics\\me.php"\r\n'
                 f'Content-Type: application/x-php\r\n\r\n'.encode() + png + b"\r\n",
                 f'--{b}\r\nContent-Disposition: form-data; name="docs[]"; filename="a.txt"\r\n'
                 f'Content-Type: text/plain\r\n\r\n'.encode() + b"line\r\n--not a boundary\r\n" + b"\r\n",
                 f'--{b}\r\nContent-Disposition: form-data; name="docs[]"; filename*=utf-8\'\'na%C3%AFve.txt\r\n'
                 f'\r\n'.encode() + b"two" + b"\r\n",
                 f'--{b}\r\nContent-Disposition: form-data; name="empty"; filename=""\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n\r\n'.encode(),
                 f"--{b}--\r\n".encode()]
        body = b"".join(parts)
        proc = subprocess.Popen([sys.executable, "-m", "barq", "run", "--interp" if engine == "tree" else "--vm",
                                 os.path.join(HERE, "http", "upload_server.bq"), str(port), "3", folder],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=barq_env())
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        pass
                    break
                except OSError:
                    time.sleep(0.05)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/upload", data=body, method="POST",
                                         headers={"Content-Type": f'multipart/form-data; boundary="{b}"'})
            with urllib.request.urlopen(req, timeout=10) as res:
                got = json.loads(res.read().decode())
            big = urllib.request.Request(f"http://127.0.0.1:{port}/upload", data=b"x" * (65 * 1024), method="POST",
                                         headers={"Content-Type": "application/octet-stream"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(big, timeout=10)
            self.assertEqual(caught.exception.code, 413)
            caught.exception.close()
            out, err = proc.communicate(timeout=20)
            self.assertEqual(got, {"title": "برق ⚡", "tags": ["a", "b"], "has": True, "name": "me.php",
                                   "mime": "image/png", "ext": "png", "size": len(png),
                                   "dims": {"width": 7, "height": 5}, "path": "photo.png",
                                   "docs": ["docs/a.txt", "docs/naïve.txt"]})
            with open(os.path.join(folder, "photo.png"), "rb") as f:
                self.assertEqual(f.read(), png)
            with open(os.path.join(folder, "docs", "a.txt"), "rb") as f:
                self.assertEqual(f.read(), b"line\r\n--not a boundary\r\n")
            self.assertEqual((proc.returncode, out), (0, "server done\n"), err)
        finally:
            if proc.poll() is None:
                proc.kill()
            shutil.rmtree(folder, ignore_errors=True)

    def test_router(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.router(engine)

    def router(self, engine):
        port = free_port()
        sessions = tempfile.mkdtemp(prefix="barq-sessions-")
        paths = [("GET", "/", None), ("GET", "/users/42", None), ("GET", "/users/x", None),
                 ("POST", "/login", "name=Alice"), ("GET", "/me", None)]
        total = len(paths) + 2   # + connection probe + the redirect after /login
        proc = subprocess.Popen([sys.executable, "-m", "barq", "run", "--interp" if engine == "tree" else "--vm",
                                 os.path.join(HERE, "http", "router.bq"), str(port), str(total), sessions],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=barq_env())
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        pass
                    break
                except OSError:
                    time.sleep(0.05)
            results = []
            for method, path, body in paths:
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                             data=body.encode() if body else None,
                                             headers={"Content-Type": "application/x-www-form-urlencoded"} if body else {})
                try:
                    with opener.open(req, timeout=10) as res:
                        results.append((res.status, res.read().decode()))
                except urllib.error.HTTPError as e:
                    results.append((e.code, e.read().decode()))
                    e.close()
            out, err = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
            files = os.listdir(sessions)
            shutil.rmtree(sessions, ignore_errors=True)
        self.assertEqual(results[0], (200, "<h1>router</h1>"))
        self.assertEqual(json.loads(results[1][1]), {"id": 42})
        self.assertEqual(results[2][0], 404)
        # POST /login redirects to /me; the session and the cookie come along
        self.assertEqual(json.loads(results[3][1]), {"user": "Alice", "seen": "yes", "url": "/users/9"})
        self.assertEqual(json.loads(results[4][1]), {"user": "Alice", "seen": "yes", "url": "/users/9"})
        self.assertEqual(sorted(c.name for c in jar), ["barq_session", "seen"])
        self.assertTrue(any(f.endswith(".json") for f in files), "the file session driver must write a file")
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out, "router done\n")


class TestCommand(unittest.TestCase):
    """`barq test` on tests/testing: same report on both engines, timing aside."""

    def run_tests(self, engine, *args):
        proc = subprocess.run([sys.executable, "-m", "barq", "test", "--interp" if engine == "tree" else "--vm", *args],
                              capture_output=True, text=True, env=barq_env(), timeout=60,
                              cwd=os.path.join(HERE, "testing"))
        lines = proc.stdout.splitlines()
        if lines and lines[-1].startswith("Tests:"):
            lines[-1] = lines[-1].rsplit("  (", 1)[0]
        return proc.returncode, "\n".join(lines) + "\n", proc.stderr

    def test_report(self):
        with open(os.path.join(HERE, "testing", "expected.out"), encoding="utf-8") as f:
            expected = f.read()
        for engine in ENGINES:
            with self.subTest(engine=engine):
                code, out, err = self.run_tests(engine)
                self.assertEqual(code, 1, err)
                self.assertEqual(out, expected)

    def test_single_folder_passes_or_fails(self):
        code, out, _ = self.run_tests("vm", "tests/feature")
        self.assertEqual(code, 1)
        self.assertIn("Tests: 2 passed, 1 failed", out)


class ServeCommand(unittest.TestCase):
    """barq serve without any package installed."""

    def test_serve_needs_routes(self):
        tmp = tempfile.mkdtemp(prefix="barq-serve-")
        try:
            code, _, err = devtest.barq_cli("serve", "--no-reload", cwd=tmp)
            self.assertEqual(code, 1)
            self.assertIn("no routes", err)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NewCommand(unittest.TestCase):
    """barq new without packages: every kind of project checks, is formatted and passes its tests."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="barq-new-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def barq(self, *args, cwd=None):
        return devtest.barq_cli(*args, cwd=cwd or self.dir)

    def test_each_kind(self):
        expect = {
            "web": ("routes/web.bq", "views/home.bq", "public/css/app.css", "tests/feature/HomeTest.bq",
                    "app/Controllers/Controller.bq"),
            "api": ("routes/api.bq", "app/Controllers/HomeController.bq", "tests/feature/ApiTest.bq",
                    "app/Controllers/Controller.bq"),
            "console": ("main.bq", "app/Greeter.bq", "tests/GreeterTest.bq"),
        }
        for kind, files in expect.items():
            with self.subTest(kind=kind):
                code, out, err = self.barq("new", f"my-{kind}", f"--{kind}")
                self.assertEqual(code, 0, err)
                app = os.path.join(self.dir, f"my-{kind}")
                for rel in files + (".gitignore", ".env", ".env.example", "README.md"):
                    self.assertTrue(os.path.isfile(os.path.join(app, rel)), rel)
                with open(os.path.join(app, ".env")) as f:
                    self.assertRegex(f.read(), r'APP_NAME="My \w+"\n(.*\n)*APP_KEY=base64:[A-Za-z0-9+/=]{44}')
                with open(os.path.join(app, ".gitignore")) as f:
                    self.assertIn("/dist/\n", f.read())          # barq build's output
                self.assertEqual(kind == "web", os.path.exists(os.path.join(app, "views")))
                self.assertEqual(self.barq("check", ".", cwd=app)[0], 0)
                code, out, err = self.barq("fmt", "--check", ".", cwd=app)
                self.assertEqual(code, 0, out + err)
                for engine in ENGINES:
                    code, out, err = self.barq("test", "--interp" if engine == "tree" else "--vm", cwd=app)
                    self.assertEqual(code, 0, out + err)
                    self.assertIn(" passed", out)
                    self.assertNotIn("failed", out)
        code, out, _ = self.barq("run", "main.bq", "Sara", cwd=os.path.join(self.dir, "my-console"))
        self.assertEqual((code, out), (0, "Salaam, Sara!\n"))

    def test_keeps_existing_files(self):
        app = os.path.join(self.dir, "app")
        os.makedirs(os.path.join(app, "routes"))
        with open(os.path.join(app, "routes", "web.bq"), "w") as f:
            f.write("// mine\n")
        code, _, err = self.barq("new", "app")
        self.assertEqual(code, 1)
        self.assertIn("not empty", err)
        code, out, err = self.barq("new", ".", "--force", cwd=app)
        self.assertEqual(code, 0, err)
        self.assertIn("Kept routes/web.bq", out)
        with open(os.path.join(app, "routes", "web.bq")) as f:
            self.assertEqual(f.read(), "// mine\n")
        self.assertTrue(os.path.isfile(os.path.join(app, "views", "home.bq")))
        with open(os.path.join(app, ".env")) as f:
            self.assertIn('APP_NAME="App"', f.read())

    def test_bad_arguments(self):
        for args, text in ((("new",), "usage"), (("new", "a", "b"), "one folder"), (("new", "a", "--nope"), "unknown option"),
                           (("new", "a", "--console", "--auth"), "web project"), (("new", "a", "--auth"), "barq-auth")):
            with self.subTest(args=args):
                code, _, err = self.barq(*args)
                self.assertNotEqual(code, 0)
                self.assertIn(text, err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "a")))


class CliProgram(unittest.TestCase):
    def run_tool(self, engine, args, stdin=""):
        flag = "--interp" if engine == "tree" else "--vm"
        proc = subprocess.run([sys.executable, "-m", "barq", "run", flag, os.path.join(HERE, "cli", "tool.bq"), *args],
                              input=stdin, capture_output=True, text=True, env=barq_env(), timeout=60)
        return proc.returncode, proc.stdout, proc.stderr

    def both(self, args, stdin=""):
        results = {e: self.run_tool(e, args, stdin) for e in ENGINES}
        self.assertEqual(results["vm"], results["tree"], "the VM and the tree-walker disagree")
        return results["vm"]

    def test_options_and_prompts(self):
        code, out, err = self.both(["greet", "x", "y", "--name=Ali", "-v", "--no-color", "--dry-run"], "Sara\ny\n2\n")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], '#["greet", "x", "y"] #{"name": "Ali", "v": true, "color": false, "dry-run": true}')
        self.assertEqual(lines[1], "Ali default true false true")
        self.assertIn("> SALAAM, SARA!", out)
        self.assertIn("extra args: x,y", out)

    def test_defaults_on_empty_input(self):
        code, out, _ = self.both(["greet"], "\n\n\n")
        self.assertIn("Hello, stranger", out)

    def test_table_errors_and_help(self):
        code, out, _ = self.both(["table"])
        self.assertEqual(code, 0)
        self.assertIn("| 2  | Bob Khan |", out)
        code, out, err = self.both(["fail"])
        self.assertEqual((code, err), (3, "something broke\ncareful\n"))
        code, out, _ = self.both(["help"])
        self.assertEqual(code, 0)
        self.assertIn("  table  Print a table", out)
        code, _, err = self.both(["nope"])
        self.assertEqual(code, 1)
        self.assertIn('Unknown command "nope"', err)


FMT_IN = """\
use Zeta\\B
use App\\Models\\User


// helper
#[Public]
#[ list<int> ]
karo   f( int a,int b=-1 , *int rest ):list<int>
  int x=a+b*-2
  ?int y   =null
  dict<string,list<int>> d=#{ "a" : #[1,2] }
  if(x>1 and not(y==null))
      return #[x , -x]
  string s = x>0?"pos":"neg"   // trailing
  z = ~ int( "3" )
  r = User.where("a",1)
        .orderBy( "b" )
  g = kr( int n ):int=>n*2
  h = list_map(xs, karo(int n): int
          return n * 2
  )
  /* two
     lines */
  return f(a: 1, *xs)
class A extends B
  public static int n=0
  karo m()
      return this.n?.x ?? User::class
main()
"""

FMT_OUT = """\
use App\\Models\\User
use Zeta\\B

// helper
#[Public]
#[list<int>]
karo f(int a, int b = -1, *int rest): list<int>
    int x = a + b * -2
    ?int y = null
    dict<string, list<int>> d = #{"a": #[1, 2]}
    if (x > 1 and not (y == null))
        return #[x, -x]
    string s = x > 0 ? "pos" : "neg"   // trailing
    z = ~int("3")
    r = User.where("a", 1)
        .orderBy("b")
    g = kr(int n): int => n * 2
    h = list_map(xs, karo(int n): int
        return n * 2
    )
    /* two
       lines */
    return f(a: 1, *xs)

class A extends B
    public static int n = 0
    karo m()
        return this.n?.x ?? User::class

main()
"""


class ModuleFiles(unittest.TestCase):
    """Where `use A\\B\\C` looks for its file."""

    def test_lowercase_folders(self):
        # Database\Seeders\X lives in database/seeders/X.bq (what make:seeder writes), even on a
        # case-sensitive file system; App\Models\X in app/Models/X.bq.
        d = tempfile.mkdtemp(prefix="barq-use-")
        self.addCleanup(shutil.rmtree, d, True)
        files = {"database/seeders/Seed.bq": "namespace Database\\Seeders\n\nclass Seed\n    string name = \"seed\"\n",
                 "app/Models/Thing.bq": "namespace App\\Models\n\nclass Thing\n    string name = \"thing\"\n",
                 "main.bq": "use Database\\Seeders\\Seed\nuse App\\Models\\Thing\nprint(new Seed().name, new Thing().name)\n"}
        for rel, text in files.items():
            os.makedirs(os.path.join(d, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(d, rel), "w") as f:
                f.write(text)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                proc = subprocess.run([sys.executable, "-m", "barq", "run", "--interp" if engine == "tree" else "--vm",
                                       "main.bq"], capture_output=True, text=True, env=barq_env(), timeout=60, cwd=d)
                self.assertEqual((proc.returncode, proc.stdout), (0, "seed thing\n"), proc.stderr)


    def test_compile_a_folder_finds_parent_classes(self):
        # `barq compile app` resolves classes from the project root (from the file's namespace),
        # so a controller extends its base class from the same namespace, as in `barq run`.
        d = tempfile.mkdtemp(prefix="barq-compile-")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "app", "Controllers", "Admin"))
        files = {"app/Controllers/Controller.bq": "namespace App\\Controllers\n\nabstract class Controller\n",
                 "app/Controllers/HomeController.bq": "namespace App\\Controllers\n\nclass HomeController extends Controller\n",
                 "app/Controllers/Admin/UserController.bq": "namespace App\\Controllers\\Admin\n\n"
                 "use App\\Controllers\\Controller\n\nclass UserController extends Controller\n"}
        for rel, text in files.items():
            with open(os.path.join(d, rel), "w") as f:
                f.write(text)
        for cwd, target in ((d, "app"), (os.path.join(d, "app"), "Controllers"), (d, "app/Controllers/HomeController.bq")):
            proc = subprocess.run([sys.executable, "-m", "barq", "compile", target], capture_output=True, text=True,
                                  env=barq_env(), timeout=60, cwd=cwd)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class Formatter(unittest.TestCase):
    """`barq fmt`."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROOT)
        from barq import fmt
        cls.fmt = fmt

    def fmt_cli(self, *args, cwd=None, stdin=None):
        proc = subprocess.run([sys.executable, "-m", "barq", "fmt", *args], capture_output=True, text=True,
                              env=barq_env(), timeout=60, cwd=cwd, input=stdin)
        return proc.returncode, proc.stdout, proc.stderr

    def test_style(self):
        self.assertEqual(self.fmt.format_text(FMT_IN), FMT_OUT)
        self.assertEqual(self.fmt.format_text(FMT_OUT), FMT_OUT)
        self.assertEqual(self.fmt.format_text("if (x)\r\n\tprint(1)\r\n\r\n\r\n"), "if (x)\n    print(1)\n")
        self.assertEqual(self.fmt.format_text("if (x)\n    print(1)\n", indent=2), "if (x)\n  print(1)\n")
        self.assertEqual(self.fmt.format_text("use B\nuse A\n", sort_imports=False), "use B\nuse A\n")
        # Comparisons keep their spaces; type arguments lose them.
        self.assertEqual(self.fmt.format_text("bool b = a < c and d>e\nlist<list<int>> m = #[]\n"),
                         "bool b = a < c and d > e\nlist<list<int>> m = #[]\n")
        # A keyword after a dot is a member name: no keyword spacing.
        self.assertEqual(self.fmt.format_text('if Regex.match("a", b)\n    print(A::match, x?.default)\n'),
                         'if Regex.match("a", b)\n    print(A::match, x?.default)\n')
        # Strings, including """ ones, are copied exactly.
        src = 'string s = """\n    keep   {a+b}\n      this\n    """\nprint( "x  {a+b}  y" )\n'
        self.assertEqual(self.fmt.format_text(src), src.replace('print( "x  {a+b}  y" )', 'print("x  {a+b}  y")'))

    def test_every_file_keeps_its_tokens_and_is_stable(self):
        count = 0
        for path in sorted(glob.glob(os.path.join(ROOT, "**", "*.bq"), recursive=True)):
            if os.sep + "views" + os.sep in path:
                continue
            with open(path, encoding="utf-8") as f:
                text = f.read()
            try:
                once = self.fmt.format_text(text, path)
            except self.fmt.FormatError:
                self.fail(f"the formatter refused {path}")
            except Exception:
                continue   # files in tests/errors/ that do not lex
            with self.subTest(path=path):
                self.assertEqual(self.fmt.format_text(once, path), once, "formatting twice changes the file")
            count += 1
        self.assertGreater(count, 100)

    def test_formatted_programs_print_the_same(self):
        tmp = tempfile.mkdtemp(prefix="barq-fmt-")
        try:
            cases = os.path.join(tmp, "cases")
            shutil.copytree(os.path.join(HERE, "cases"), cases)
            code, out, err = self.fmt_cli(cases)
            self.assertEqual(code, 0, err)
            self.assertIn("reformatted", out)
            for path in sorted(glob.glob(os.path.join(cases, "*.bq"))):
                name = os.path.basename(path)
                if name == "vm_semantics.bq":
                    continue   # prints __line__
                with self.subTest(name=name):
                    with open(path[:-3] + ".out", encoding="utf-8") as f:
                        expected = f.read()
                    code, out, err = run_barq(path)
                    self.assertEqual(out + err, expected)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_command(self):
        tmp = tempfile.mkdtemp(prefix="barq-fmt-")
        try:
            os.makedirs(os.path.join(tmp, "views"))
            with open(os.path.join(tmp, "a.bq"), "w") as f:
                f.write("x=1\n")
            with open(os.path.join(tmp, "b.bq"), "w") as f:
                f.write("y = 2\n")
            with open(os.path.join(tmp, "views", "page.bq"), "w") as f:
                f.write("<p>{x}</p>\n")
            code, out, err = self.fmt_cli("--check", cwd=tmp)
            self.assertEqual((code, out.strip()), (1, os.path.join(".", "a.bq")))
            self.assertIn("1 file would be reformatted", err)
            code, out, _ = self.fmt_cli("--diff", "a.bq", cwd=tmp)
            self.assertEqual(code, 1)
            self.assertIn("-x=1\n+x = 1\n", out)
            code, out, _ = self.fmt_cli(cwd=tmp)
            self.assertEqual((code, out), (0, "reformatted 1 file\n"))
            with open(os.path.join(tmp, "a.bq")) as f:
                self.assertEqual(f.read(), "x = 1\n")
            with open(os.path.join(tmp, "views", "page.bq")) as f:
                self.assertEqual(f.read(), "<p>{x}</p>\n")
            self.assertEqual(self.fmt_cli("--check", cwd=tmp)[0], 0)
            self.assertEqual(self.fmt_cli("-", stdin="if(a)\n  b( )\n")[1], "if (a)\n    b()\n")
            code, _, err = self.fmt_cli("-", stdin="x = (1\n")
            self.assertEqual(code, 1)
            self.assertIn("never closed", err)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CheckAndPackages(unittest.TestCase):
    """barq check (used by editors) and barq packages."""

    def test_check(self):
        code, out, err = devtest.barq_cli("check", os.path.join(ROOT, "examples"))
        self.assertEqual(code, 0, err)
        self.assertIn("checked, no errors", out)
        code, out, _ = devtest.barq_cli("check", "--json", "--path", "t.bq", "-", stdin='int x = "a"\nprint(y)\n')
        self.assertEqual(code, 1)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([(r["path"], r["line"], r["col"], r["kind"]) for r in rows],
                         [("t.bq", 1, 9, "CompileError"), ("t.bq", 2, 7, "CompileError")])
        self.assertEqual(rows[1]["message"], "'y' is not defined")
        code, out, _ = devtest.barq_cli("check", "--json", "-", stdin="x = (1\n")
        self.assertEqual(json.loads(out)["kind"], "SyntaxError")

    def test_no_packages(self):
        code, out, _ = devtest.barq_cli("packages")
        self.assertEqual(code, 0)
        self.assertIn("No Barq packages installed", out)
        code, _, err = devtest.barq_cli("migrate")
        self.assertEqual(code, 2)
        self.assertIn("it comes with barq-framework", err)
        self.assertNotIn("Framework commands", devtest.barq_cli("help")[1])


class LspClient:
    """Talks to `barq lsp` the way an editor does: Content-Length framed JSON-RPC."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.proc = subprocess.Popen([sys.executable, "-m", "barq", "lsp"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=barq_env())
        self.next_id = 0
        self.notes = []
        self.inbox = __import__("queue").Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.caps = self.request("initialize", {"rootUri": self.uri(""), "capabilities": {}})["capabilities"]
        self.notify("initialized", {})

    def uri(self, rel):
        from barq.lsp import path_to_uri
        return path_to_uri(os.path.join(self.root, rel))

    def _read(self):
        f = self.proc.stdout
        while True:
            head = f.readline()
            if not head:
                return
            size = int(head.split(b":")[1])
            f.readline()
            self.inbox.put(json.loads(f.read(size)))

    def _send(self, msg):
        data = json.dumps(msg).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        self.proc.stdin.flush()

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params):
        self.next_id += 1
        mid = self.next_id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        while True:
            msg = self.inbox.get(timeout=30)
            if msg.get("id") == mid:
                return msg["result"] if "result" in msg else {"error": msg["error"]}
            self.notes.append(msg)

    def diagnostics(self, uri):
        while True:
            for m in self.notes:
                if m.get("method") == "textDocument/publishDiagnostics" and m["params"]["uri"] == uri:
                    self.notes.remove(m)
                    return m["params"]["diagnostics"]
            self.notes.append(self.inbox.get(timeout=30))

    def open(self, rel, text=None):
        if text is None:
            with open(os.path.join(self.root, rel), encoding="utf-8") as f:
                text = f.read()
        uri = self.uri(rel)
        self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "barq", "version": 1, "text": text}})
        return uri

    def change(self, uri, text, version=2):
        self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version},
                                               "contentChanges": [{"text": text}]})

    def at(self, method, uri, line, char, **extra):
        return self.request(method, {"textDocument": {"uri": uri}, "position": {"line": line, "character": char}, **extra})

    def labels(self, uri, line, char):
        return [i["label"] for i in self.at("textDocument/completion", uri, line, char)["items"]]

    def close(self):
        self.request("shutdown", None)
        self.notify("exit", None)
        code = self.proc.wait(10)
        self.proc.stdout.close()
        self.proc.stderr.close()
        self.proc.stdin.close()
        return code


class LanguageServer(unittest.TestCase):
    """barq lsp on the small project in tests/lsp/ (App\\Models\\User, App\\Services\\Greeter, main.bq)."""

    @classmethod
    def setUpClass(cls):
        cls.c = LspClient(os.path.join(HERE, "lsp"))
        cls.greeter = cls.c.open("app/Services/Greeter.bq")
        cls.main = cls.c.open("main.bq")
        cls.c.diagnostics(cls.greeter)
        cls.c.diagnostics(cls.main)

    @classmethod
    def tearDownClass(cls):
        assert cls.c.close() == 0

    def test_capabilities(self):
        for cap in ("hoverProvider", "definitionProvider", "referencesProvider", "completionProvider",
                    "documentSymbolProvider", "documentFormattingProvider", "signatureHelpProvider",
                    "renameProvider", "workspaceSymbolProvider"):
            self.assertIn(cap, self.c.caps)

    def test_diagnostics(self):
        # columns are UTF-16 units: the emoji counts as two
        uri = self.c.open("diag.bq", 'string s = "🙂" + y\n')
        d = self.c.diagnostics(uri)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["message"], "'y' is not defined")
        self.assertEqual(d[0]["range"]["start"], {"line": 0, "character": 18})
        self.c.change(uri, 'string s = "🙂"\n')
        self.assertEqual(self.c.diagnostics(uri), [])

    def test_hover_and_definition_across_files(self):
        # line 15: `string out = this.user.greet("x")`
        h = self.c.at("textDocument/hover", self.greeter, 15, 32)["contents"]["value"]
        self.assertIn("karo greet(string other, int times = 1): string", h)
        self.assertIn("Says hi to someone.", h)
        loc = self.c.at("textDocument/definition", self.greeter, 15, 32)
        self.assertTrue(loc["uri"].endswith("app/Models/User.bq"))
        self.assertEqual(loc["range"]["start"], {"line": 11, "character": 9})
        # people[0].name: list<User> element
        h = self.c.at("textDocument/hover", self.greeter, 17, 36)["contents"]["value"]
        self.assertIn('string name = ""', h)
        # the class on a `use` line
        loc = self.c.at("textDocument/definition", self.main, 1, 18)
        self.assertTrue(loc["uri"].endswith("app/Services/Greeter.bq"))
        h = self.c.at("textDocument/hover", self.main, 5, 6)["contents"]["value"]
        self.assertIn("karo hello(string who): void", h)
        self.assertIn("Says hello.", h)
        self.assertIn("module Math", self.c.at("textDocument/hover", self.c.open("m.bq", "Math.abs(1)\n"), 0, 1)["contents"]["value"])

    def test_completion(self):
        # members: private `age` and the constant are not offered outside User
        self.assertEqual(sorted(self.c.labels(self.greeter, 15, 31)), ["greet", "name"])
        text = 'Math.\nstring s = "x"\ns.\nUser.\nUser.MAX\nUser u = User.named("a")\nu.\n'
        uri = self.c.open("scratch.bq", text)
        self.assertIn("round", self.c.labels(uri, 0, 5))
        self.assertIn("upper", self.c.labels(uri, 2, 2))
        self.assertIn("named", self.c.labels(uri, 3, 5))
        self.assertEqual(sorted(self.c.labels(uri, 6, 2)), ["greet", "name"])
        # a project class not imported yet comes with its `use` line
        items = self.c.at("textDocument/completion", uri, 4, 2)["items"]
        user = next(i for i in items if i["label"] == "User")
        self.assertEqual(user["additionalTextEdits"][0]["newText"], "use App\\Models\\User\n\n")
        self.assertLessEqual({"print", "str_upper", "Math", "karo"}, {i["label"] for i in items})
        # `use` paths and attributes
        self.assertIn("App\\Models\\User", self.c.labels(self.c.open("u.bq", "use App\\M"), 0, 9))
        self.assertIn("Public", self.c.labels(self.c.open("a.bq", "#[P"), 0, 3))

    def test_completion_while_the_file_has_a_syntax_error(self):
        with open(os.path.join(HERE, "lsp", "app", "Services", "Greeter.bq"), encoding="utf-8") as f:
            lines = f.read().split("\n")
        lines[16:16] = ['        User extra = User.named("b")', "        extra."]
        uri = self.c.open("app/Services/Broken.bq", "\n".join(lines).replace("class Greeter", "class Broken"))
        self.assertTrue(self.c.diagnostics(uri))
        self.assertEqual(sorted(self.c.labels(uri, 17, 14)), ["greet", "name"])
        names = self.c.labels(uri, 17, 8)
        for n in ("extra", "out", "people", "this"):
            self.assertIn(n, names)

    def test_signature_help(self):
        s = self.c.at("textDocument/signatureHelp", self.main, 8, 21)   # User.named("A|li")
        self.assertEqual(s["signatures"][0]["label"], "static karo named(string n): User")
        self.assertEqual(s["activeParameter"], 0)
        uri = self.c.open("sig.bq", 'use App\\Models\\User\nUser u = new User()\nu.greet("a", ')
        s = self.c.at("textDocument/signatureHelp", uri, 2, 13)
        self.assertEqual(s["activeParameter"], 1)
        p = s["signatures"][0]["parameters"][1]["label"]
        self.assertEqual(s["signatures"][0]["label"][p[0]:p[1]], "int times = 1")

    def test_references_rename_symbols(self):
        refs = self.c.at("textDocument/references", self.main, 8, 5, context={"includeDeclaration": True})
        self.assertEqual([(r["range"]["start"]["line"], r["range"]["start"]["character"]) for r in refs],
                         [(8, 5), (9, 24), (10, 6), (11, 14)])
        refs = self.c.at("textDocument/references", self.main, 8, 1, context={"includeDeclaration": True})
        files = {r["uri"].rsplit("/", 1)[-1] for r in refs}     # (other tests' open buffers count too)
        self.assertLessEqual({"User.bq", "Greeter.bq", "main.bq"}, files)
        edit = self.c.at("textDocument/rename", self.main, 8, 5, newName="person")
        self.assertEqual(len(next(iter(edit["changes"].values()))), 4)
        self.assertIn("error", self.c.at("textDocument/rename", self.main, 5, 6, newName="hi"))
        syms = self.c.request("textDocument/documentSymbol", {"textDocument": {"uri": self.greeter}})
        self.assertEqual([(s["name"], [k["name"] for k in s["children"]]) for s in syms],
                         [("Greeter", ["user", "__construct", "run"])])
        found = self.c.request("workspace/symbol", {"query": "greet"})
        self.assertEqual(sorted(s["name"] for s in found), ["Greeter", "greet"])

    def test_formatting(self):
        uri = self.c.open("f.bq", "int x=1\nprint( x )\n")
        edits = self.c.request("textDocument/formatting", {"textDocument": {"uri": uri},
                                                          "options": {"tabSize": 4, "insertSpaces": True}})
        self.assertEqual(edits[0]["newText"], "int x = 1\nprint(x)\n")
        self.assertEqual(self.c.request("textDocument/formatting", {"textDocument": {"uri": self.main},
                                                                    "options": {"tabSize": 4}}), [])


DEBUG_DIR = os.path.join(HERE, "debug")


def debug_session(commands, *args, cwd=DEBUG_DIR, cache=False):
    """Run `barq debug` with commands piped in; returns (exit code, output)."""
    env = barq_env(cache)
    proc = subprocess.run([sys.executable, "-m", "barq", "debug", *args], capture_output=True, text=True,
                          env=env, cwd=cwd, input="".join(c + "\n" for c in commands), timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


class DapClient:
    """Talks to `barq dap` the way an editor does."""

    def __init__(self, cwd=DEBUG_DIR):
        self.proc = subprocess.Popen([sys.executable, "-m", "barq", "dap"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=barq_env(), cwd=cwd)
        self.seq = 0
        self.inbox = __import__("queue").Queue()
        self.events = []
        self.output = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        f = self.proc.stdout
        while True:
            head = f.readline()
            if not head:
                self.inbox.put(None)
                return
            size = int(head.split(b":")[1])
            f.readline()
            self.inbox.put(json.loads(f.read(size)))

    def _next(self):
        msg = self.inbox.get(timeout=30)
        if msg is None:
            raise AssertionError("barq dap closed the connection")
        if msg["type"] == "event" and msg["event"] == "output":
            self.output.append(msg["body"]["output"])
        return msg

    def send(self, command, **arguments):
        self.seq += 1
        data = json.dumps({"seq": self.seq, "type": "request", "command": command,
                           "arguments": arguments}).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        self.proc.stdin.flush()
        return self.seq

    def request(self, command, ok=True, **arguments):
        seq = self.send(command, **arguments)
        while True:
            msg = self._next()
            if msg["type"] == "response" and msg["request_seq"] == seq:
                if ok and not msg["success"]:
                    raise AssertionError(f"{command} failed: {msg.get('message')}")
                return msg if not ok else msg.get("body") or {}
            if msg["type"] == "event":
                self.events.append(msg)

    def event(self, name):
        for i, m in enumerate(self.events):
            if m["event"] == name:
                return self.events.pop(i)["body"] if "body" in m else {}
        while True:
            msg = self._next()
            if msg["type"] == "event" and msg["event"] == name:
                return msg.get("body") or {}
            if msg["type"] == "event":
                self.events.append(msg)

    def frames(self):
        return [(f["name"], f["line"]) for f in self.request("stackTrace", threadId=1)["stackFrames"]]

    def variables(self, ref):
        return {v["name"]: v for v in self.request("variables", variablesReference=ref)["variables"]}

    def locals(self, frame_id=1):
        scopes = self.request("scopes", frameId=frame_id)["scopes"]
        ref = next(s["variablesReference"] for s in scopes if s["name"] == "Locals")
        return ref, self.variables(ref)

    def close(self):
        try:
            self.request("disconnect")
        except AssertionError:
            pass
        code = self.proc.wait(10)
        for f in (self.proc.stdout, self.proc.stderr, self.proc.stdin):
            f.close()
        return code


class Debugging(unittest.TestCase):
    """`barq debug` (terminal) and `barq dap` (editors) on tests/debug/."""

    def test_program_runs_normally(self):
        # breakpoint() does nothing without a debugger, on both engines
        code, out, err = run_both(self, os.path.join(DEBUG_DIR, "main.bq"))
        self.assertEqual(code, 1)
        self.assertEqual(out, "total 3\n60\ncaught too expensive: 500\n")
        self.assertIn("Uncaught ValueException: too expensive: 999", err)

    def test_stepping_and_variables(self):
        code, out = debug_session(["n", "n", "v", "s", "v", "bt", "u", "v", "d", "o", "n", "total = 50",
                                   'total = "x"', "p total * 2", "p nope", "p 1 +", "q"], "main.bq")
        self.assertEqual(code, 0)
        self.assertIn("→ main.bq:8 in <main>\n     8 │ int total = 0", out)     # stops on the first line
        self.assertIn("(barq) s\n→ main.bq:5 in add\n", out)
        self.assertIn("(barq) v\n  a = 0\n  b = 0\n", out)
        self.assertIn("→ #0 add at main.bq:5\n  #1 <main> at main.bq:10\n", out)
        self.assertIn("(barq) u\n→ main.bq:10 in <main>", out)
        self.assertIn("(barq) v\n  total = 0\n  i = 0\n", out)
        self.assertIn("(barq) o\n→ main.bq:10 in <main>", out)     # back on the line of the call
        self.assertIn("(barq) n\n→ main.bq:9 in <main>", out)
        self.assertIn("(barq) total = 50\n50\n", out)
        self.assertIn("error: cannot put a string in 'total' (int)", out)
        self.assertIn("(barq) p total * 2\n100\n", out)
        self.assertIn("error: NameException: 'nope' is not defined", out)
        self.assertIn("error: expected an expression", out)
        self.assertNotIn("total 3", out)     # quit before the print

    def test_breakpoints(self):
        code, out = debug_session(["cl 2", "c", "v", "p this.items", "bt", "b", "b main.bq:16", "c", "c", "c", "c"],
                                  "-c", "-b", "App/Cart.bq:13 if price == 20", "-b", "add", "main.bq")
        self.assertEqual(code, 1)
        self.assertIn("Breakpoint 2 hit\n→ main.bq:5 in add", out)    # `-b add`: first line of add()
        # the condition skips price 10; `this` and the call stack across files
        self.assertIn("Breakpoint 1 hit\n→ App/Cart.bq:13 in Cart.add", out)
        self.assertIn("  this = Cart {items: list(1), owner: \"Ali\"}\n  price = 20\n", out)
        self.assertIn("(barq) p this.items\n#[10]\n", out)
        self.assertIn("→ #0 Cart.add at App/Cart.bq:13\n  #1 <main> at main.bq:14\n", out)
        self.assertIn("(barq) b\n  1: App/Cart.bq:13 if price == 20\n(barq)", out)
        self.assertIn("Breakpoint 3 at main.bq:16\n", out)
        self.assertIn("→ main.bq:15 in <main>\n    15 │ breakpoint()", out)    # the builtin
        self.assertIn("Breakpoint 3 hit\n→ main.bq:16", out)
        self.assertIn("total 3\n", out)
        # uncaught: stops where it was raised, with that frame's variables
        self.assertIn("Uncaught ValueException: too expensive: 999\n→ App/Cart.bq:15 in Cart.add", out)
        self.assertEqual(out.count("Breakpoint 2 hit"), 1)     # cleared after the first hit
        self.assertIn("Program finished (exit code 1)", out)

    def test_breakpoint_lines_move_to_a_statement(self):
        code, out = debug_session(["b 2", "b 7", "b 99", "b nothing", "cl 2", "b", "q"], "main.bq")
        self.assertIn("Breakpoint 1 at main.bq:5\n", out)     # blank line 2: the next statement is in add()
        self.assertIn("Breakpoint 2 at main.bq:8\n", out)
        self.assertIn("Breakpoint 3 at main.bq:99 is not active: no statement on or after this line", out)
        self.assertIn("error: cannot find 'nothing'", out)
        self.assertIn("(barq) b\n  1: main.bq:5\n(barq) q", out)   # 3 is not active, 2 was cleared

    def test_exceptions_all_and_none(self):
        code, out = debug_session(["c", "c", "v", "c"], "--exceptions", "all", "main.bq")
        self.assertIn("(barq) c\n60\nValueException: too expensive: 500\n→ App/Cart.bq:15", out)
        self.assertIn("ValueException: too expensive: 999\n→ App/Cart.bq:15", out)
        self.assertIn("  price = 500\n", out)
        self.assertEqual(out.count("too expensive: 999\n→"), 1)    # not again as uncaught
        code, out = debug_session(["c", "c"], "--exceptions", "none", "main.bq")
        self.assertNotIn("→ App/Cart.bq", out)
        self.assertIn("Uncaught ValueException: too expensive: 999\n  at App/Cart.bq:15", out)

    def test_cached_files_keep_debug_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(DEBUG_DIR, os.path.join(tmp, "p"))
            cwd = os.path.join(tmp, "p")
            for _ in range(2):     # the second run reads .barq/cache/*.bqc
                code, out = debug_session(["-b App/Cart.bq:13".replace("-b ", "b "), "c", "v", "q"],
                                          "main.bq", cwd=cwd, cache=True)
                self.assertIn("  this = Cart {items: #[], owner: \"Ali\"}\n  price = 10\n".replace("#[]", "list(0)"), out)
            self.assertTrue(os.path.isfile(os.path.join(cwd, ".barq", "cache", "App", "Cart.bqc")))

    def test_dap_session(self):
        c = DapClient()
        try:
            caps = c.request("initialize", adapterID="barq", linesStartAt1=True)
            self.assertTrue(caps["supportsConditionalBreakpoints"])
            self.assertEqual([f["filter"] for f in caps["exceptionBreakpointFilters"]], ["uncaught", "all"])
            c.request("launch", program=os.path.join(DEBUG_DIR, "main.bq"))
            c.event("initialized")
            cart = os.path.join(DEBUG_DIR, "App", "Cart.bq")
            bps = c.request("setBreakpoints", source={"path": cart}, breakpoints=[
                {"line": 13, "condition": "price == 20"}, {"line": 19, "logMessage": "total of {this.owner}"},
                {"line": 2}])["breakpoints"]
            self.assertEqual([(b["verified"], b["line"]) for b in bps], [(True, 13), (True, 19), (True, 9)])
            c.request("setExceptionBreakpoints", filters=["uncaught"])
            c.request("configurationDone")

            stop = c.event("stopped")
            self.assertEqual((stop["reason"], stop["hitBreakpointIds"]), ("breakpoint", [bps[2]["id"]]))
            self.assertEqual(c.frames(), [("Cart.__construct", 9), ("<main>", 12)])
            c.request("continue", threadId=1)

            stop = c.event("stopped")
            self.assertEqual(stop["hitBreakpointIds"], [bps[0]["id"]])
            self.assertEqual(c.frames(), [("Cart.add", 13), ("<main>", 14)])
            ref, local = c.locals()
            self.assertEqual(local["price"]["value"], "20")
            self.assertEqual(local["price"]["type"], "int")
            this = c.variables(local["this"]["variablesReference"])
            self.assertEqual(this["items"]["value"], "#[10]")
            self.assertEqual(this["owner"]["value"], '"Ali"')
            r = c.request("evaluate", expression="price * 3", frameId=1, context="repl")
            self.assertEqual((r["result"], r["type"]), ("60", "int"))
            r = c.request("evaluate", expression="this.total()", frameId=1, context="hover", ok=False)
            self.assertFalse(r["success"])          # no calls on hover
            r = c.request("evaluate", expression="missing", frameId=1, context="repl", ok=False)
            self.assertIn("'missing' is not defined", r["message"])
            self.assertEqual(c.request("setVariable", variablesReference=ref, name="price", value="25")["value"], "25")
            r = c.request("setVariable", variablesReference=ref, name="price", value='"x"', ok=False)
            self.assertIn("cannot put a string", r["message"])
            scopes = c.request("scopes", frameId=1)["scopes"]
            glob = c.variables(next(s["variablesReference"] for s in scopes if s["name"] == "Globals"))
            self.assertIn("Cart", glob)
            main_vars = c.locals(2)[1]
            self.assertEqual(main_vars["total"]["value"], "3")
            self.assertEqual(main_vars["cart"]["type"], "Cart")

            c.request("stepOut", threadId=1)
            self.assertEqual(c.event("stopped")["reason"], "step")
            self.assertEqual(c.frames(), [("<main>", 14)])
            c.request("next", threadId=1)
            c.event("stopped")
            self.assertEqual(c.frames(), [("<main>", 13)])
            c.request("stepIn", threadId=1)
            c.event("stopped")
            self.assertEqual(c.frames(), [("<main>", 14)])
            c.request("stepIn", threadId=1)
            c.event("stopped")
            self.assertEqual(c.frames(), [("Cart.add", 13), ("<main>", 14)])

            c.request("continue", threadId=1)
            stop = c.event("stopped")              # breakpoint()
            self.assertEqual(c.frames(), [("<main>", 15)])
            c.request("continue", threadId=1)
            stop = c.event("stopped")
            self.assertEqual(stop["reason"], "exception")
            self.assertEqual(stop["description"], "Uncaught ValueException: too expensive: 999")
            self.assertEqual(c.frames(), [("Cart.add", 15), ("<main>", 21)])
            scopes = c.request("scopes", frameId=1)["scopes"]
            self.assertEqual(scopes[0]["name"], "Exception")
            self.assertEqual(c.variables(scopes[0]["variablesReference"])["message"]["value"],
                             '"too expensive: 999"')
            c.request("continue", threadId=1)
            self.assertEqual(c.event("exited")["exitCode"], 1)
            c.event("terminated")
            out = "".join(c.output)
            self.assertIn("total of Ali\n", out)          # the logpoint
            self.assertIn("total 3\ntotal of Ali\n65\ncaught too expensive: 500\n", out)    # price was set to 25
            self.assertIn("Uncaught ValueException: too expensive: 999", out)
        finally:
            self.assertEqual(c.close(), 1)

    def test_dap_stop_on_entry_pause_and_disconnect(self):
        c = DapClient()
        try:
            c.request("initialize", adapterID="barq")
            c.request("launch", program=os.path.join(DEBUG_DIR, "loop.bq"), stopOnEntry=True)
            c.event("initialized")
            c.request("configurationDone")
            self.assertEqual(c.event("stopped")["reason"], "entry")
            self.assertEqual(c.frames(), [("<main>", 1)])
            c.request("continue", threadId=1)
            time.sleep(0.3)
            c.request("pause", threadId=1)
            self.assertEqual(c.event("stopped")["reason"], "pause")
            n = int(c.request("evaluate", expression="n", frameId=1, context="watch")["result"])
            self.assertGreater(n, 0)
        finally:
            self.assertEqual(c.close(), 0)     # disconnect ends the program


def git_repo(path, files, tags=(), message="first"):
    """Make (or add a commit to) a git repo; tag the new commit with each tag."""
    os.makedirs(path, exist_ok=True)
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
    if not os.path.isdir(os.path.join(path, ".git")):
        subprocess.run(g + ["init", "-q"], cwd=path, check=True)
    for rel, text in files.items():
        p = os.path.join(path, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
    subprocess.run(g + ["add", "-A"], cwd=path, check=True)
    subprocess.run(g + ["commit", "-qm", message], cwd=path, check=True)
    for t in tags:
        subprocess.run(g + ["tag", "-a", t, "-m", t], cwd=path, check=True)


def pkg_manifest(name, version, namespace=None, requires=None):
    out = f'name = "{name}"\nversion = "{version}"\n'
    if namespace:
        out += f"namespace = '{namespace}'\n"
    if requires:
        out += "requires = #{\n" + ",\n".join(f'    "{k}": "{v}"' for k, v in requires.items()) + "\n}\n"
    return out


SHOUT = """namespace Acme\\Strings

class Shout

    #[Public]
    #[Static]
    #[string]
    karo it(string s)
        return str_upper(s) + "{bang}"
"""

TITLE = """namespace Acme\\Report

use Acme\\Strings\\Shout

class Title

    #[Public]
    #[Static]
    #[string]
    karo make(string s)
        return "== " + Shout.it(s) + " =="
"""


class Versions(unittest.TestCase):
    """SemVer ordering, constraints and barq.pkg parsing (barq/pkg.py)."""

    def test_order(self):
        from barq.pkg import version_key
        order = ["0.9.0", "1.0.0-alpha", "1.0.0-alpha.2", "1.0.0-alpha.10", "1.0.0-beta", "1.0.0", "v1.0.1", "1.10.0"]
        self.assertEqual(sorted(order, key=version_key), order)
        self.assertIsNone(version_key("main"))
        self.assertIsNone(version_key("1.0"))

    def test_constraints(self):
        from barq.pkg import parse_constraint, matches
        cases = {
            "1.2.0": (["1.2.0", "v1.2.0"], ["1.2.1", "1.2.0-alpha"]),
            "^1.2.0": (["1.2.0", "1.9.9"], ["1.1.9", "2.0.0", "2.0.0-alpha"]),
            "^0.2.3": (["0.2.3", "0.2.9"], ["0.3.0", "0.2.2"]),
            "^0.0.3": (["0.0.3"], ["0.0.4"]),
            "^0.21.0-alpha": (["0.21.0-alpha", "0.21.0", "0.21.4"], ["0.20.9", "0.22.0-alpha"]),
            "~1.2.3": (["1.2.3", "1.2.9"], ["1.3.0", "1.2.2"]),
            ">=1.0.0 <2.0.0": (["1.0.0", "1.5.0"], ["2.0.0", "0.9.0"]),
            ">=1.0, <2": (["1.0.0", "1.9.0"], ["2.0.0"]),
            "1.2.*": (["1.2.0", "1.2.7"], ["1.3.0"]),
            "1.2": (["1.2.5"], ["1.3.0"]),
            "1.x": (["1.0.0", "1.9.0"], ["2.0.0"]),
            ">1.2": (["1.3.0"], ["1.2.9"]),
            "<=1.2": (["1.2.9", "1.0.0"], ["1.3.0"]),
            "^1.0 || ^3.0": (["1.4.0", "3.1.0"], ["2.0.0"]),
            "*": (["0.0.1", "9.0.0-beta"], []),
        }
        for text, (yes, no) in cases.items():
            c = parse_constraint(text)
            self.assertIsNotNone(c, text)
            for v in yes:
                self.assertTrue(matches(c, v), f"{v} should fit {text}")
            for v in no:
                self.assertFalse(matches(c, v), f"{v} should not fit {text}")
        for ref in ("main", "feature/x", "abc1234", "release-1"):
            self.assertIsNone(parse_constraint(ref), ref)

    def test_manifest(self):
        from barq.pkg import read_manifest, check_manifest, PackageError
        m = read_manifest("""// a package
name        = "awesome-utils"
version     = "1.0.0"
namespace   = 'AwesomeUtils\\Text'
keywords    = ["a", "b",]
private     = false
requires    = #{
    "someone/helper-lib": ">=1.0.0",
    "barq": "^0.21.0-alpha"
}
""")
        self.assertEqual(m["namespace"], "AwesomeUtils\\Text")
        self.assertEqual(m["keywords"], ["a", "b"])
        self.assertIs(m["private"], False)
        self.assertEqual(m["requires"]["someone/helper-lib"], ">=1.0.0")
        self.assertEqual(check_manifest(m), [])
        bad = [
            ('name = "x"\nversion = "1.0"\n', "SemVer"),
            ('name = "x"\n', "'version' is required"),
            ('name = "x"\nversion = "1.0.0"\nnamespace = \'App\\Utils\'\n', "taken"),
            ('name = "x"\nversion = "1.0.0"\nrequires = #{"helper": "1.0.0"}\n', "user/name"),
        ]
        for text, msg in bad:
            self.assertIn(msg, " ".join(check_manifest(read_manifest(text))), text)
        for text, msg in [('name "x"\n', "expected '='"), ('name = "{x}"\n', "interpolation"),
                          ('name = "a"\nname = "b"\n', "set twice"), ('namespace = "A\\Utils"\n', "single quotes")]:
            with self.assertRaises(PackageError) as cm:
                read_manifest(text)
            self.assertIn(msg, str(cm.exception), text)


@unittest.skipUnless(shutil.which("git"), "needs git")
class PackageManager(unittest.TestCase):
    """barq install / update / remove / publish against git repos in a temp folder
    (BARQ_REGISTRY points there instead of GitHub)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="barq-pkg-")
        self.reg = os.path.join(self.dir, "registry")
        self.app = os.path.join(self.dir, "app")
        os.makedirs(self.app)
        self.strings = os.path.join(self.reg, "acme", "strings")
        self.report = os.path.join(self.reg, "acme", "report")
        for v, bang in (("1.0.0", "!"), ("1.1.0", "!!"), ("2.0.0", "!!!")):
            git_repo(self.strings, {"barq.pkg": pkg_manifest("strings", v, "Acme\\Strings"),
                                    "src/Shout.bq": SHOUT.replace("{bang}", bang)}, ["v" + v], v)
        git_repo(self.report, {"barq.pkg": pkg_manifest("report", "0.3.0", "Acme\\Report",
                                                        {"acme/strings": "^1.0", "barq": ">=0.20.0"}),
                               "src/Title.bq": TITLE}, ["v0.3.0"])
        with open(os.path.join(self.app, "main.bq"), "w") as f:
            f.write('use Acme\\Report\\Title\n\nprint(Title.make("hi"))\n')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def barq(self, *args, cwd=None, env=None):
        e = dict(devtest.barq_env(), BARQ_REGISTRY=os.path.join(self.reg, "{name}"),
                 BARQ_CACHE=os.path.join(self.dir, "cache"), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                 GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", **(env or {}))
        p = subprocess.run([sys.executable, "-m", "barq", *args], capture_output=True, text=True,
                           env=e, cwd=cwd or self.app, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def ok(self, *args, **kw):
        code, out, err = self.barq(*args, **kw)
        self.assertEqual(code, 0, out + err)
        return out

    def json(self, name):
        with open(os.path.join(self.app, name)) as f:
            return json.load(f)

    def test_install_run_and_lock(self):
        out = self.ok("install", "acme/report")
        self.assertIn("+ acme/report 0.3.0", out)
        self.assertIn("+ acme/strings 1.1.0", out)      # newest that fits ^1.0
        self.assertEqual(self.json("barq.json")["require"], {"acme/report": "^0.3.0"})
        lock = self.json("barq.lock")["packages"]
        self.assertEqual(sorted(lock), ["acme/report", "acme/strings"])
        self.assertEqual(lock["acme/strings"]["version"], "1.1.0")
        for engine in ("--vm", "--interp"):
            self.assertEqual(self.ok("run", engine, "main.bq"), "== HI!! ==\n")
        self.assertIn("2 files checked, no errors", self.ok("check", "main.bq", "vendor/acme/report/src/Title.bq"))
        self.assertIn("1 file checked", self.ok("check", "."))       # vendor/ is skipped
        self.assertIn("acme/strings 1.1.0  use Acme\\Strings\\...", self.ok("packages"))

        # A new release: barq install keeps barq.lock, barq update moves.
        git_repo(self.strings, {"barq.pkg": pkg_manifest("strings", "1.2.0", "Acme\\Strings"),
                                "src/Shout.bq": SHOUT.replace("{bang}", "?")}, ["v1.2.0"], "1.2")
        shutil.rmtree(os.path.join(self.app, "vendor"))
        self.assertIn("Installing from barq.lock", self.ok("install"))
        self.assertEqual(self.ok("run", "main.bq"), "== HI!! ==\n")
        self.assertIn("~ acme/strings 1.1.0 -> 1.2.0", self.ok("update"))
        self.assertEqual(self.ok("run", "main.bq"), "== HI? ==\n")
        self.assertIn("Everything is up to date", self.ok("update"))

    def test_constraints_conflicts_and_refs(self):
        self.ok("install", "acme/report")
        self.assertIn("~ acme/strings 1.1.0 -> 1.0.0", self.ok("install", "acme/strings@1.0.0"))
        self.assertEqual(self.json("barq.json")["require"]["acme/strings"], "1.0.0")
        self.assertEqual(self.ok("run", "main.bq"), "== HI! ==\n")
        code, out, err = self.barq("install", "acme/strings@^2.0")
        self.assertEqual(code, 1)
        self.assertIn("no version of acme/strings fits ^2.0 (wanted by barq.json), ^1.0 (wanted by acme/report)", err)
        self.assertEqual(self.json("barq.json")["require"]["acme/strings"], "1.0.0")    # unchanged
        code, _, err = self.barq("install", "acme/strings@main")
        self.assertIn("main is version 2.0.0, which does not fit ^1.0", err)
        # a branch of a package nobody else constrains
        self.ok("remove", "acme/strings")
        self.ok("remove", "acme/report")
        self.assertEqual(self.json("barq.lock")["packages"], {})
        self.assertFalse(os.path.exists(os.path.join(self.app, "vendor", "acme")))
        out = self.ok("install", "acme/strings@main")
        self.assertRegex(out, r"\+ acme/strings main \([0-9a-f]{7}\)")
        self.assertEqual(self.json("barq.lock")["packages"]["acme/strings"]["kind"], "branch")
        code, _, err = self.barq("install", "acme/strings@nope")
        self.assertIn("no tag, branch or commit 'nope'", err)
        code, _, err = self.barq("install", "nobody/missing")
        self.assertIn("cannot download nobody/missing", err)
        code, _, err = self.barq("install", "not-a-name")
        self.assertIn("packages are named user/name", err)

    def test_barq_version_and_untagged(self):
        git_repo(os.path.join(self.reg, "acme", "future"), {
            "barq.pkg": pkg_manifest("future", "1.0.0", None, {"barq": ">=99.0.0"})}, ["v1.0.0"])
        code, _, err = self.barq("install", "acme/future")
        self.assertIn("acme/future needs barq >=99.0.0", err)
        # no tags at all: the default branch, saved as "*"
        git_repo(os.path.join(self.reg, "acme", "draft"), {"barq.pkg": pkg_manifest("draft", "0.1.0")})
        self.assertRegex(self.ok("install", "acme/draft"), r"\+ acme/draft main \(")
        self.assertEqual(self.json("barq.json")["require"]["acme/draft"], "*")
        self.assertIn("Installing from barq.lock", self.ok("install"))
        # not a package
        git_repo(os.path.join(self.reg, "acme", "plain"), {"README.md": "hi"}, ["v1.0.0"])
        code, _, err = self.barq("install", "acme/plain")
        self.assertIn("is not a Barq package (it has no barq.pkg)", err)

    def test_plugin_package_in_vendor(self):
        init = ('NAME = "barq-thing"\n__version__ = "0.1.0"\nREQUIRES = []\n\n'
                'def register(hooks):\n    hooks.add_command("thing:hello", lambda cmd, args: print("hello from thing") or 0)\n')
        git_repo(os.path.join(self.reg, "acme", "thing"), {
            "barq.pkg": pkg_manifest("barq-thing", "0.1.0"), "barq_thing/__init__.py": init}, ["v0.1.0"])
        self.ok("install", "acme/thing")
        env = {"BARQ_PACKAGES": "barq_thing"}
        self.assertEqual(self.ok("thing:hello", env=env), "hello from thing\n")
        self.assertIn("barq-thing 0.1.0", self.ok("packages", env=env))

    def test_package_development_and_publish(self):
        # Inside the package itself, its own namespace resolves from src/.
        with open(os.path.join(self.report, "tests_main.bq"), "w") as f:
            f.write('use Acme\\Strings\\Shout\n\nprint(Shout.it("x"))\n')
        self.ok("install", "acme/strings@^1.0", cwd=self.report)
        self.assertEqual(self.ok("run", "tests_main.bq", cwd=self.report), "X!!\n")
        self.assertIn("1 file checked", self.ok("check", "src/Title.bq", cwd=self.report))
        os.remove(os.path.join(self.report, "tests_main.bq"))
        with open(os.path.join(self.report, ".gitignore"), "w") as f:
            f.write("/vendor/\n")
        subprocess.run(["git", "add", "-A"], cwd=self.report, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "deps"],
                       cwd=self.report, check=True)
        code, _, err = self.barq("publish", cwd=self.report)
        self.assertIn("v0.3.0 is already tagged", err)
        git_repo(self.report, {"barq.pkg": pkg_manifest("report", "0.4.0", "Acme\\Report", {"acme/strings": "^1.0"})},
                 message="0.4")
        self.assertIn("ready to publish as v0.4.0", self.ok("publish", "--dry-run", cwd=self.report))
        self.assertIn("Tagged v0.4.0", self.ok("publish", cwd=self.report))
        self.assertIn("+ acme/report 0.4.0", self.ok("install", "acme/report"))
        # errors in the code stop it
        git_repo(self.report, {"barq.pkg": pkg_manifest("report", "0.5.0", "Acme\\Report"),
                               "src/Broken.bq": "namespace Acme\\Report\n\nint x = \"no\"\n"}, message="broken")
        code, _, err = self.barq("publish", cwd=self.report)
        self.assertEqual(code, 1)
        self.assertIn("in the code; fix them first", err)


if __name__ == "__main__":
    unittest.main()
