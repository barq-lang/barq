"""S3 disks (v0.17.0): AWS S3 and S3-compatible stores (MinIO, Cloudflare R2, DigitalOcean Spaces...).

config/filesystems.bq:
    "s3": #{"driver": "s3", "key": env("AWS_ACCESS_KEY_ID"), "secret": env("AWS_SECRET_ACCESS_KEY"),
            "region": "us-east-1", "bucket": "my-app",
            "endpoint": null,        // "http://127.0.0.1:9000" for MinIO; null = AWS
            "path_style": null,      // bucket in the path instead of the host name; default: true with an endpoint
            "url": null,             // public base URL for url(), e.g. a CDN
            "root": null,            // a folder inside the bucket every path lives under
            "token": null, "timeout": 30}

Requests are signed with AWS Signature Version 4 using only the standard library.
"""

import datetime as dt
import email.utils
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

EMPTY_HASH = hashlib.sha256(b"").hexdigest()
UNSIGNED = "UNSIGNED-PAYLOAD"


# ===========================================================================
# Signature V4
# ===========================================================================

def quote(s, safe="-_.~"):
    return urllib.parse.quote(s, safe=safe)


def canonical_query(query):
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(pairs))


def signing_key(secret, date, region, service="s3"):
    k = hmac.new(("AWS4" + secret).encode(), date.encode(), hashlib.sha256).digest()
    for part in (region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return k


def string_to_sign(amz_date, scope, canonical):
    return "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])


def sign_headers(method, url, headers, payload_hash, key, secret, region, now, token=None):
    """-> the headers to send, with Authorization. `url` must already be URI-encoded."""
    parts = urllib.parse.urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    out = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    out["host"] = parts.netloc
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_hash
    if token:
        out["x-amz-security-token"] = token
    names = sorted(out)
    canonical = "\n".join([method, parts.path or "/", canonical_query(parts.query),
                           "".join(f"{k}:{out[k]}\n" for k in names), ";".join(names), payload_hash])
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    sig = hmac.new(signing_key(secret, amz_date[:8], region), string_to_sign(amz_date, scope, canonical).encode(),
                   hashlib.sha256).hexdigest()
    out["authorization"] = (f"AWS4-HMAC-SHA256 Credential={key}/{scope}, SignedHeaders={';'.join(names)}, "
                            f"Signature={sig}")
    return out


def presign(method, url, key, secret, region, now, expires, token=None):
    parts = urllib.parse.urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True) + [
        ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"), ("X-Amz-Credential", f"{key}/{scope}"),
        ("X-Amz-Date", amz_date), ("X-Amz-Expires", str(expires)), ("X-Amz-SignedHeaders", "host")]
    if token:
        params.append(("X-Amz-Security-Token", token))
    query = "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(params))
    canonical = "\n".join([method, parts.path or "/", query, f"host:{parts.netloc}\n", "host", UNSIGNED])
    sig = hmac.new(signing_key(secret, amz_date[:8], region), string_to_sign(amz_date, scope, canonical).encode(),
                   hashlib.sha256).hexdigest()
    return f"{parts.scheme}://{parts.netloc}{parts.path}?{query}&X-Amz-Signature={sig}"


# ===========================================================================
# the client
# ===========================================================================

class S3Error(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class S3Client:
    def __init__(self, cfg):
        self.key = cfg.get("key") or ""
        self.secret = cfg.get("secret") or ""
        self.region = cfg.get("region") or "us-east-1"
        self.bucket = cfg.get("bucket") or ""
        self.token = cfg.get("token") or None
        self.endpoint = (cfg.get("endpoint") or "").rstrip("/") or None
        ps = cfg.get("path_style")
        self.path_style = (self.endpoint is not None) if ps is None else bool(ps)
        self.public = (cfg.get("url") or "").rstrip("/") or None
        self.root = "/".join(x for x in str(cfg.get("root") or "").split("/") if x)
        t = cfg.get("timeout")
        self.timeout = t if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 else 30

    # ---- addresses ----------------------------------------------------------

    def key_of(self, path):
        return (self.root + "/" + path) if self.root and path else (self.root or path)

    def base(self):
        if self.endpoint:
            if self.path_style:
                return f"{self.endpoint}/{quote(self.bucket)}"
            p = urllib.parse.urlsplit(self.endpoint)
            return f"{p.scheme}://{self.bucket}.{p.netloc}"
        if self.path_style:
            return f"https://s3.{self.region}.amazonaws.com/{quote(self.bucket)}"
        return f"https://{self.bucket}.s3.{self.region}.amazonaws.com"

    def object_url(self, path):
        return self.base() + "/" + quote(self.key_of(path), safe="/-_.~")

    def public_url(self, path):
        if self.public:
            return self.public + "/" + quote(path, safe="/-_.~")
        return self.object_url(path)

    def temporary_url(self, path, seconds, now=None):
        return presign("GET", self.object_url(path), self.key, self.secret, self.region,
                       now or dt.datetime.now(dt.timezone.utc), seconds, self.token)

    # ---- requests -----------------------------------------------------------

    def send(self, method, url, body=b"", headers=None):
        """-> (status, headers dict with lower-case names, body bytes). Raises S3Error / OSError."""
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_HASH
        signed = sign_headers(method, url, headers or {}, payload_hash, self.key, self.secret, self.region,
                              dt.datetime.now(dt.timezone.utc), self.token)
        req = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None, method=method)
        for k, v in signed.items():
            if k != "host":
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            data = e.read() if method != "HEAD" else b""
            return e.code, {k.lower(): v for k, v in e.headers.items()}, data

    def check(self, status, data, ok=(200, 204)):
        if status in ok:
            return
        code, message = f"HTTP{status}", f"S3 answered {status}"
        try:
            root = ET.fromstring(data)
            code = find(root, "Code") or code
            message = find(root, "Message") or message
        except ET.ParseError:
            pass
        raise S3Error(status, code, message)

    def head(self, path):
        status, headers, data = self.send("HEAD", self.object_url(path))
        if status == 404:
            return None
        self.check(status, data)
        return headers

    def get(self, path):
        status, _, data = self.send("GET", self.object_url(path))
        if status == 404:
            return None
        self.check(status, data)
        return data

    def put(self, path, data, mime):
        status, _, body = self.send("PUT", self.object_url(path), data, {"content-type": mime})
        self.check(status, body)

    def delete(self, path):
        status, _, body = self.send("DELETE", self.object_url(path))
        self.check(status, body, ok=(200, 204, 404))

    def copy(self, src, dst):
        source = "/" + quote(self.bucket) + "/" + quote(self.key_of(src), safe="/-_.~")
        status, _, body = self.send("PUT", self.object_url(dst), b"", {"x-amz-copy-source": source})
        self.check(status, body)
        if b"<Error>" in body:          # S3 can answer 200 and still fail a copy
            self.check(500, body)

    def list(self, prefix, delimiter):
        """-> (keys, common prefixes), relative to the disk root."""
        keys, prefixes, token = [], [], None
        full = self.key_of(prefix) if prefix else (self.root + "/" if self.root else "")
        cut = len(self.root) + 1 if self.root else 0
        while True:
            q = {"list-type": "2", "prefix": full}
            if delimiter:
                q["delimiter"] = "/"
            if token:
                q["continuation-token"] = token
            url = self.base() + "/?" + "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(q.items()))
            status, _, data = self.send("GET", url)
            self.check(status, data)
            root = ET.fromstring(data)
            for el in root:
                tag = el.tag.split("}")[-1]
                if tag == "Contents":
                    keys.append(find(el, "Key")[cut:])
                elif tag == "CommonPrefixes":
                    prefixes.append(find(el, "Prefix")[cut:].rstrip("/"))
            if find(root, "IsTruncated") != "true":
                break
            token = find(root, "NextContinuationToken")
            if not token:
                break
        return keys, prefixes


def find(el, name):
    for child in el:
        if child.tag.split("}")[-1] == name:
            return child.text or ""
    return None


def modified_of(headers):
    value = headers.get("last-modified")
    if not value:
        return 0
    try:
        return int(email.utils.parsedate_to_datetime(value).timestamp())
    except (TypeError, ValueError):
        return 0
