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
CheckAndPackages      barq check (--json) and barq packages.
Cache                 .bqc files are written, reused, and refreshed when the source changes.
Formatter             `barq fmt`: the style rules, every .bq file keeps its tokens and
                      formats the same twice, formatted test programs print the same.

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


for p in sorted(glob.glob(os.path.join(HERE, "cases", "*.bq"))):
    add_golden(p)
for p in sorted(glob.glob(os.path.join(HERE, "errors", "*.bq"))):
    add_error(p)
for p in sorted(glob.glob(os.path.join(ROOT, "examples", "**", "*.bq"), recursive=True)):
    if os.sep + "views" + os.sep not in p:   # templates, not programs
        add_example(p)


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


if __name__ == "__main__":
    unittest.main()
