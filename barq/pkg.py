"""The package manager: `barq install`, `update`, `remove` and `publish`.

A package is a git repository (on GitHub by default) with a `barq.pkg`
manifest at its top:

    name        = "awesome-utils"
    version     = "1.0.0"
    namespace   = "AwesomeUtils"       # `use AwesomeUtils\\X` reads src/X.bq
    requires    = #{"someone/helper-lib": "^1.0.0"}

A project lists what it needs in barq.json; `barq install` picks a version
for each (the highest git tag that fits every constraint), copies it into
vendor/<user>/<name>/ and writes the exact commits to barq.lock. While
barq.lock still fits barq.json, `barq install` reinstalls exactly those
commits without looking for newer ones; `barq update` looks again.

Packages come from https://github.com/<user>/<name>.git. BARQ_REGISTRY
changes that: a template with {name} (a URL or a folder of repositories).
Downloads are kept in a bare git clone per package under BARQ_CACHE
(default ~/.cache/barq). git must be installed.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from . import __version__

MANIFEST = "barq.pkg"
PROJECT = "barq.json"
LOCK = "barq.lock"
VENDOR = "vendor"
MARKER = ".barq-commit"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
NAMESPACE_RE = re.compile(r"^[A-Za-z_]\w*(\\[A-Za-z_]\w*)*$")


class PackageError(Exception):
    pass


# =========================================================================
# Versions and constraints (SemVer)
# =========================================================================

VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def _pre_key(pre):
    if pre is None:
        return (1, ())
    return (0, tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")))


def version_key(text):
    """A sortable key for "1.2.0" / "v1.2.0-beta.1", or None if it isn't a version.
    A pre-release sorts below its release: 1.0.0-alpha < 1.0.0."""
    m = VERSION_RE.match(text.strip()) if isinstance(text, str) else None
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) + _pre_key(m.group(4))


def _floor(a, b, c):
    """The lowest key of a.b.c, below all of its pre-releases (npm's a.b.c-0)."""
    return (a, b, c, 0, ())


COMPARATOR_RE = re.compile(r"^(\^|~|>=|<=|>|<|=)?v?(\d+)(?:\.(\d+|\*|x))?(?:\.(\d+|\*|x))?(?:-([0-9A-Za-z.-]+))?$")


def parse_constraint(text):
    """A version constraint as a list of alternatives (each a list of (op, key)),
    or None when the text is not a version constraint (then it names a branch,
    tag or commit). Supports 1.2.0, >=1.2.0, >, <, <=, ^1.2.0, ~1.2.0, 1.2.*, *,
    several separated by spaces or commas (all must hold) and `||` (either)."""
    text = (text or "").strip()
    if not text:
        return None
    alts = []
    for alt in text.split("||"):
        comps = []
        for tok in re.split(r"[\s,]+", alt.strip()):
            if not tok or tok in ("*", "x"):
                continue
            m = COMPARATOR_RE.match(tok)
            if not m:
                return None
            op, a, b, c, pre = m.groups()
            a = int(a)
            wild_b = b is None or b in ("*", "x")
            wild_c = wild_b or c is None or c in ("*", "x")
            b = 0 if wild_b else int(b)
            c = 0 if wild_c else int(c)
            if pre and (wild_b or wild_c):
                return None
            low = (a, b, c) + _pre_key(pre)
            if op in (None, "=") and (wild_b or wild_c):
                # 1 → 1.x, 1.2 / 1.2.* → 1.2.x
                comps.append((">=", _floor(a, b, c)))
                comps.append(("<", _floor(a + 1, 0, 0) if wild_b else _floor(a, b + 1, 0)))
            elif op in (None, "="):
                comps.append(("==", low))
            elif op == "^":
                comps.append((">=", low))
                if a > 0 or wild_b:
                    comps.append(("<", _floor(a + 1, 0, 0)))
                elif b > 0 or wild_c:
                    comps.append(("<", _floor(0, b + 1, 0)))
                else:
                    comps.append(("<", _floor(0, 0, c + 1)))
            elif op == "~":
                comps.append((">=", low))
                comps.append(("<", _floor(a + 1, 0, 0) if wild_b else _floor(a, b + 1, 0)))
            elif wild_c:
                # >=1.2 / <1.2 start at 1.2.0; >1.2 / <=1.2 take all of 1.2.x
                start = _floor(a, b, 0)
                after = _floor(a + 1, 0, 0) if wild_b else _floor(a, b + 1, 0)
                comps.append({">=": (">=", start), ">": (">=", after), "<": ("<", start), "<=": ("<", after)}[op])
            else:
                comps.append((op, low))
        alts.append(comps)
    return alts


def matches(constraint, version):
    """True when `version` (text) satisfies a parsed constraint."""
    key = version_key(version) if isinstance(version, str) else version
    if key is None:
        return False
    for comps in constraint:
        ok = True
        for op, k in comps:
            if not ((op == "==" and key == k) or (op == ">=" and key >= k) or (op == ">" and key > k)
                    or (op == "<" and key < k) or (op == "<=" and key <= k)):
                ok = False
                break
        if ok:
            return True
    return False


def is_ref(constraint):
    return parse_constraint(constraint) is None


# =========================================================================
# barq.pkg
# =========================================================================

def read_manifest(text, path=MANIFEST):
    """barq.pkg: one `key = value` per line, values written as Barq literals
    (strings, numbers, true/false/null, [lists] and #{dicts})."""
    from .errors import BarqError, SourceFile
    from .lexer import tokenize
    try:
        toks = [t for t in tokenize(SourceFile(path, text)) if t.type not in ("INDENT", "DEDENT")]
    except BarqError as e:
        hint = " (write namespaces in single quotes: 'Acme\\Utils')" if "escape" in e.message else ""
        raise PackageError(f"{path}:{e.line}: {e.message}{hint}") from None
    i = 0

    def fail(tok, what):
        raise PackageError(f"{path}:{tok.line}: {what}")

    def value():
        nonlocal i
        t = toks[i]
        while t.type == "NEWLINE":
            i += 1
            t = toks[i]
        i += 1
        if t.type in ("STRING", "RAWSTRING"):
            parts = t.value if isinstance(t.value, list) else [t.value]
            if not all(isinstance(p, str) for p in parts):
                fail(t, "strings in barq.pkg can't use {interpolation}")
            return "".join(parts)
        if t.type in ("INT", "FLOAT"):
            return t.value
        if t.type == "KEYWORD" and t.value in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[t.value]
        if t.type == "OP" and t.value == "-" and toks[i].type in ("INT", "FLOAT"):
            i += 1
            return -toks[i - 1].value
        if t.is_op("[", "#{"):
            closer = "]"
            if t.value == "#{":
                closer = "}"
            out = [] if closer == "]" else {}
            while True:
                while toks[i].type == "NEWLINE":
                    i += 1
                if toks[i].is_op(closer):
                    i += 1
                    return out
                item = value()
                if closer == "}":
                    while toks[i].type == "NEWLINE":
                        i += 1
                    if not toks[i].is_op(":"):
                        fail(toks[i], "expected ':' in #{...}")
                    i += 1
                    out[item] = value()
                else:
                    out.append(item)
                while toks[i].type == "NEWLINE":
                    i += 1
                if toks[i].is_op(","):
                    i += 1
                elif not toks[i].is_op(closer):
                    fail(toks[i], f"expected ',' or '{closer}'")
        fail(t, f"expected a value, got {t.value!r}" if t.value else "expected a value")

    out = {}
    while toks[i].type != "EOF":
        t = toks[i]
        if t.type == "NEWLINE":
            i += 1
            continue
        if t.type not in ("NAME", "KEYWORD"):
            fail(t, f"expected a setting name, got {t.value!r}")
        if not toks[i + 1].is_op("="):
            fail(toks[i + 1], f"expected '=' after '{t.value}'")
        i += 2
        if t.value in out:
            fail(t, f"'{t.value}' is set twice")
        out[t.value] = value()
        if toks[i].type not in ("NEWLINE", "EOF"):
            fail(toks[i], "expected the end of the line")
    return out


def check_manifest(m, path=MANIFEST):
    """Problems with a manifest, as a list of messages (empty when fine)."""
    errors = []
    for key in ("name", "version"):
        if not isinstance(m.get(key), str) or not m.get(key):
            errors.append(f"{path}: '{key}' is required")
    if isinstance(m.get("version"), str) and version_key(m["version"]) is None:
        errors.append(f"{path}: version '{m['version']}' is not a SemVer version like 1.0.0")
    ns = m.get("namespace")
    if ns is not None and (not isinstance(ns, str) or not NAMESPACE_RE.match(ns)):
        errors.append(f"{path}: namespace must look like Acme or Acme\\Utils")
    if ns and ns.split("\\")[0] in ("App", "Barq"):
        errors.append(f"{path}: namespace '{ns}' is taken (App is the project's, Barq the standard library's)")
    req = m.get("requires", {})
    if not isinstance(req, dict):
        errors.append(f"{path}: requires must be a #{{\"user/name\": \"^1.0.0\"}} dict")
    else:
        for name, c in req.items():
            if name != "barq" and not NAME_RE.match(str(name)):
                errors.append(f"{path}: '{name}' in requires should be user/name, the package's GitHub repository")
            if not isinstance(c, str) or not c.strip():
                errors.append(f"{path}: the constraint for '{name}' must be a string like \"^1.0.0\"")
    src = m.get("source", "src")
    if not isinstance(src, str) or src.startswith(("/", "\\")) or ".." in src.replace("\\", "/").split("/"):
        errors.append(f"{path}: source must be a folder inside the package")
    return errors


# =========================================================================
# Finding installed packages (used by `use`, barq check and the LSP)
# =========================================================================

_ns_cache = {}


def project_base(start):
    """The nearest folder at or above `start` holding barq.json, barq.pkg or vendor/."""
    d = os.path.abspath(start)
    while True:
        for f in (PROJECT, MANIFEST, VENDOR):
            if os.path.exists(os.path.join(d, f)):
                return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def _manifest_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return read_manifest(f.read(), path)
    except (OSError, PackageError):
        return None


def installed(base):
    """[(name, folder, manifest)] for every package in base/vendor/."""
    out = []
    vendor = os.path.join(base, VENDOR)
    if not os.path.isdir(vendor):
        return out
    for user in sorted(os.listdir(vendor)):
        udir = os.path.join(vendor, user)
        if user.startswith(".") or not os.path.isdir(udir):
            continue
        for name in sorted(os.listdir(udir)):
            pdir = os.path.join(udir, name)
            m = _manifest_file(os.path.join(pdir, MANIFEST)) if os.path.isdir(pdir) else None
            if m is not None:
                out.append((f"{user}/{name}", pdir, m))
    return out


def _stamp(base):
    parts = []
    for p in (os.path.join(base, MANIFEST), os.path.join(base, VENDOR)):
        try:
            parts.append(os.stat(p).st_mtime_ns)
        except OSError:
            parts.append(0)
    vendor = os.path.join(base, VENDOR)
    if os.path.isdir(vendor):
        for user in os.listdir(vendor):
            try:
                parts.append(os.stat(os.path.join(vendor, user)).st_mtime_ns)
            except OSError:
                pass
    return tuple(parts)


def namespace_dirs(root):
    """[(namespace parts, source folder)] that `use` can read from: the package
    being worked on (when root is in one) and every package in vendor/.
    Longest namespaces first."""
    base = project_base(root) if root else None
    if base is None:
        return []
    stamp = _stamp(base)
    hit = _ns_cache.get(base)
    if hit and hit[0] == stamp:
        return hit[1]
    found = []
    own = _manifest_file(os.path.join(base, MANIFEST))
    if own and isinstance(own.get("namespace"), str):
        found.append((own["namespace"].split("\\"), os.path.join(base, own.get("source", "src"))))
    for _, pdir, m in installed(base):
        if isinstance(m.get("namespace"), str):
            found.append((m["namespace"].split("\\"), os.path.join(pdir, m.get("source", "src"))))
    found.sort(key=lambda x: -len(x[0]))
    _ns_cache[base] = (stamp, found)
    return found


def find_file(root, parts):
    """The .bq file for a `use` path inside an installed package, or None."""
    for ns, src in namespace_dirs(root):
        if len(parts) > len(ns) and parts[:len(ns)] == ns:
            p = os.path.join(src, *parts[len(ns):]) + ".bq"
            if os.path.isfile(p):
                return p
    return None


def plugin_dirs(start=None):
    """vendor/<user>/ folders of the current project (for barq_* Python packages)."""
    base = project_base(start or os.getcwd())
    if base is None:
        return []
    vendor = os.path.join(base, VENDOR)
    if not os.path.isdir(vendor):
        return []
    return [os.path.join(vendor, u) for u in sorted(os.listdir(vendor))
            if not u.startswith(".") and os.path.isdir(os.path.join(vendor, u))]


# =========================================================================
# Git sources
# =========================================================================

def git(*args, cwd=None, binary=False):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", LC_ALL="C")
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=env)
    except FileNotFoundError:
        raise PackageError("git is not installed; barq install needs it to download packages") from None
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise PackageError(msg[-1] if msg else f"git {args[0]} failed")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def registry_url(name):
    tpl = os.environ.get("BARQ_REGISTRY", "").strip() or "https://github.com/{name}.git"
    if "{name}" not in tpl:
        tpl = tpl.rstrip("/") + "/{name}"
    return tpl.replace("{name}", name)


def cache_dir():
    base = os.environ.get("BARQ_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "barq")
    return base


class Candidate:
    """One installable state of a package: a tag, a branch head or a commit."""

    def __init__(self, source, ref, commit, version=None, kind="tag"):
        self.source = source
        self.name = source.name
        self.ref = ref
        self.commit = commit
        self.version = version        # from the tag (tags) or the manifest (others)
        self.kind = kind              # tag | branch | commit
        self._manifest = None

    @property
    def manifest(self):
        if self._manifest is None:
            self._manifest = self.source.manifest_at(self.commit)
            if self.kind != "tag":
                self.version = self._manifest.get("version")
        return self._manifest

    def label(self):
        if self.kind == "tag":
            return self.version
        return f"{self.ref} ({self.commit[:7]})"


class Source:
    """A package's git repository, mirrored in the cache."""

    def __init__(self, name):
        self.name = name
        self.url = registry_url(name)
        key = hashlib.sha1(self.url.encode()).hexdigest()[:10]
        self.dir = os.path.join(cache_dir(), "git", f"{name.replace('/', '-')}-{key}.git")
        self.fetched = False
        self._refs = None

    def fetch(self):
        if self.fetched:
            return
        try:
            if not os.path.isdir(self.dir):
                os.makedirs(os.path.dirname(self.dir), exist_ok=True)
                tmp = tempfile.mkdtemp(prefix=".clone-", dir=os.path.dirname(self.dir))
                try:
                    git("clone", "--bare", "--quiet", self.url, tmp)
                    os.replace(tmp, self.dir)
                except BaseException:
                    shutil.rmtree(tmp, ignore_errors=True)
                    raise
            else:
                git("fetch", "--quiet", "--prune", "--force", self.url,
                    "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*", cwd=self.dir)
        except PackageError as e:
            raise PackageError(f"cannot download {self.name} from {self.url}: {e}") from None
        self.fetched = True
        self._refs = None

    def has_commit(self, commit):
        if not os.path.isdir(self.dir):
            return False
        try:
            git("cat-file", "-e", commit + "^{commit}", cwd=self.dir)
            return True
        except PackageError:
            return False

    def refs(self):
        """(tags {name: commit}, branches {name: commit}, default branch name)."""
        if self._refs is None:
            self.fetch()
            tags, branches = {}, {}
            out = git("for-each-ref", "--format=%(refname)\t%(objectname)\t%(*objectname)",
                      "refs/tags", "refs/heads", cwd=self.dir)
            for line in out.splitlines():
                ref, obj, peeled = (line.split("\t") + ["", ""])[:3]
                if ref.startswith("refs/tags/"):
                    tags[ref[10:]] = peeled or obj
                elif ref.startswith("refs/heads/"):
                    branches[ref[11:]] = obj
            try:
                head = git("symbolic-ref", "--short", "HEAD", cwd=self.dir).strip()
            except PackageError:
                head = None
            if head not in branches:
                head = "main" if "main" in branches else next(iter(sorted(branches)), None)
            self._refs = (tags, branches, head)
        return self._refs

    def versions(self):
        """Tag candidates, newest first."""
        tags = self.refs()[0]
        out = [Candidate(self, t, c, t[1:] if t.startswith("v") else t) for t, c in tags.items()
               if version_key(t) is not None]
        out.sort(key=lambda c: version_key(c.version), reverse=True)
        return out

    def at_ref(self, ref):
        tags, branches, _ = self.refs()
        for t in (ref, "v" + ref):
            if t in tags:
                v = t[1:] if t.startswith("v") else t
                return Candidate(self, t, tags[t], v if version_key(v) else None,
                                 "tag" if version_key(v) else "commit")
        if ref in branches:
            return Candidate(self, ref, branches[ref], kind="branch")
        if re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
            try:
                commit = git("rev-parse", "--verify", "--quiet", ref + "^{commit}", cwd=self.dir).strip()
                return Candidate(self, ref, commit, kind="commit")
            except PackageError:
                pass
        raise PackageError(f"{self.name} has no tag, branch or commit '{ref}'")

    def default_head(self):
        _, branches, head = self.refs()
        if head is None:
            raise PackageError(f"{self.name} has no branches")
        return Candidate(self, head, branches[head], kind="branch")

    def manifest_at(self, commit):
        try:
            text = git("show", f"{commit}:{MANIFEST}", cwd=self.dir)
        except PackageError:
            raise PackageError(f"{self.name} at {commit[:7]} is not a Barq package (it has no {MANIFEST})") from None
        m = read_manifest(text, f"{self.name}/{MANIFEST}")
        problems = check_manifest(m, f"{self.name}/{MANIFEST}")
        if problems:
            raise PackageError(problems[0])
        return m

    def export(self, commit, dest):
        """Write the files of `commit` into dest (replacing it)."""
        data = git("archive", "--format=tar", commit, cwd=self.dir, binary=True)
        parent = os.path.dirname(dest)
        os.makedirs(parent, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=".install-", dir=parent)
        try:
            import io
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                tf.extractall(tmp, filter="data")
            with open(os.path.join(tmp, MARKER), "w") as f:
                f.write(commit + "\n")
            if os.path.exists(dest):
                shutil.rmtree(dest)
            os.replace(tmp, dest)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise


# =========================================================================
# Resolving
# =========================================================================

class Resolver:
    """Picks one version of every package. For each package it takes the
    newest tag that fits every constraint on it (from barq.json and from the
    packages that require it), preferring the locked one when it still fits.
    It does not backtrack: a conflict is reported, not worked around."""

    def __init__(self, locked=None, keep=None):
        self.sources = {}
        self.locked = locked or {}
        self.keep = keep           # names whose lock entry may be reused (None = all)

    def source(self, name):
        if name not in self.sources:
            self.sources[name] = Source(name)
        return self.sources[name]

    def choose(self, name, cons):
        src = self.source(name)
        refs = sorted({c for c, _ in cons if is_ref(c)})
        ranges = [(c, by) for c, by in cons if not is_ref(c)]
        lk = self.locked.get(name) if self.keep is None or name in self.keep else None
        if len(refs) > 1:
            raise PackageError(f"{name} is wanted at {' and '.join(refs)} — " + explain(cons))
        if refs:
            if lk and lk.get("ref") == refs[0] and lk.get("commit"):
                if not src.has_commit(lk["commit"]):
                    src.fetch()
                cand = Candidate(src, refs[0], lk["commit"], kind=lk.get("kind", "branch"))
            else:
                cand = src.at_ref(refs[0])
            version = cand.manifest.get("version")
            for c, by in ranges:
                if not matches(parse_constraint(c), version):
                    raise PackageError(f"{name} {refs[0]} is version {version}, which does not fit {c} "
                                       f"(wanted by {by}) — " + explain(cons))
            return cand
        parsed = [parse_constraint(c) for c, _ in ranges]
        tags = src.versions()
        fits = [t for t in tags if all(matches(p, t.version) for p in parsed)]
        if lk and lk.get("kind", "tag") == "tag":
            for t in fits:
                if t.version == lk.get("version"):
                    return t
        if fits:
            return fits[0]
        if not tags and all(c.strip() == "*" for c, _ in ranges):
            # no releases yet: the default branch (the locked commit of it, if any)
            if lk and lk.get("kind") == "branch" and lk.get("commit") and src.has_commit(lk["commit"]):
                return Candidate(src, lk["ref"], lk["commit"], kind="branch")
            return src.default_head()
        have = ", ".join(t.version for t in tags[:8]) or "no tagged versions"
        raise PackageError(f"no version of {name} fits {explain(cons)} (it has {have})")

    def resolve(self, require):
        chosen = {}
        for _ in range(64):
            cons = {}
            for n, c in require.items():
                cons.setdefault(n, []).append((c, PROJECT))
            for n, cand in chosen.items():
                for dep, c in (cand.manifest.get("requires") or {}).items():
                    cons.setdefault(dep, []).append((c, n))
            new = {}
            for n in sorted(cons):
                if n == "barq":
                    continue
                if not NAME_RE.match(n):
                    by = ", ".join(b for _, b in cons[n])
                    raise PackageError(f"'{n}' (wanted by {by}) should be user/name, the package's GitHub repository")
                new[n] = self.choose(n, cons[n])
            same = new.keys() == chosen.keys() and all(new[n].commit == chosen[n].commit for n in new)
            chosen = new
            if same:
                for c, by in cons.get("barq", []):
                    p = parse_constraint(c)
                    if p is None or not matches(p, __version__):
                        raise PackageError(f"{by} needs barq {c}; this is barq {__version__}")
                return chosen
        raise PackageError("package versions keep changing each other; check their requires")


def explain(cons):
    return ", ".join(f"{c} (wanted by {by})" for c, by in cons)


# =========================================================================
# barq.json, barq.lock and vendor/
# =========================================================================

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise PackageError(f"cannot read {what}: {e}") from None
    if not isinstance(data, dict):
        raise PackageError(f"{what} should hold a JSON object")
    return data


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def lock_entry(cand):
    m = cand.manifest
    e = {"version": cand.version or m.get("version"), "kind": cand.kind, "ref": cand.ref, "commit": cand.commit}
    if m.get("namespace"):
        e["namespace"] = m["namespace"]
    if m.get("requires"):
        e["requires"] = m["requires"]
    return e


def lock_fits(require, locked):
    """True when barq.lock already answers every constraint of barq.json and
    of the locked packages themselves, with nothing extra in it."""
    want = {}
    for n, c in require.items():
        want.setdefault(n, []).append(c)
    for e in locked.values():
        for n, c in (e.get("requires") or {}).items():
            want.setdefault(n, []).append(c)
    barq_cons = want.pop("barq", [])
    if set(want) != set(locked):
        return False
    for n, cs in want.items():
        e = locked[n]
        if not isinstance(e, dict) or not e.get("commit"):
            return False
        for c in cs:
            p = parse_constraint(c)
            if p is None:
                ok = e.get("ref") == c
            elif e.get("kind", "tag") == "tag":
                ok = matches(p, str(e.get("version") or ""))
            else:
                ok = c.strip() == "*"      # a branch head, picked because nothing was tagged
            if not ok:
                return False
    for c in barq_cons:
        p = parse_constraint(c)
        if p is None or not matches(p, __version__):
            return False
    return True


def vendor_dir(base, name):
    user, pkg = name.split("/", 1)
    return os.path.join(base, VENDOR, user, pkg)


def vendored_commit(base, name):
    try:
        with open(os.path.join(vendor_dir(base, name), MARKER)) as f:
            return f.read().strip()
    except OSError:
        return None


class Project:
    def __init__(self, base):
        self.base = base
        self.json_path = os.path.join(base, PROJECT)
        self.lock_path = os.path.join(base, LOCK)
        self.data = read_json(self.json_path, PROJECT)
        self.created = self.data is None
        if self.data is None:
            self.data = {"name": os.path.basename(base), "require": {}}
        req = self.data.setdefault("require", {})
        if not isinstance(req, dict):
            raise PackageError(f"'require' in {PROJECT} should be an object like {{\"user/name\": \"^1.0.0\"}}")
        lock = read_json(self.lock_path, LOCK) or {}
        self.locked = lock.get("packages") or {}
        if not isinstance(self.locked, dict):
            self.locked = {}

    @property
    def require(self):
        return self.data["require"]

    def save_json(self):
        write_json(self.json_path, self.data)

    def apply(self, chosen, out):
        """Make vendor/ and barq.lock match `chosen` {name: lock entry + Candidate}."""
        changes = 0
        for name in sorted(chosen):
            entry, cand = chosen[name]
            old = self.locked.get(name)
            dest = vendor_dir(self.base, name)
            if vendored_commit(self.base, name) != entry["commit"] or not os.path.isfile(os.path.join(dest, MANIFEST)):
                src = cand.source if cand is not None else Source(name)
                if not src.has_commit(entry["commit"]):
                    src.fetch()
                    if not src.has_commit(entry["commit"]):
                        raise PackageError(f"{name}: commit {entry['commit'][:7]} from {LOCK} is gone from {src.url}; "
                                           f"run barq update {name}")
                src.export(entry["commit"], dest)
                changes += 1
                label = entry["version"] if entry.get("kind", "tag") == "tag" else f"{entry['ref']} ({entry['commit'][:7]})"
                if old and old.get("commit") != entry["commit"]:
                    was = old.get("version") if old.get("kind", "tag") == "tag" else f"{old.get('ref')} ({str(old.get('commit'))[:7]})"
                    out(f"  ~ {name} {was} -> {label}" + (" (a new commit)" if was == label else ""))
                else:
                    out(f"  + {name} {label}")
        for name in sorted(set(self.locked) - set(chosen)):
            out(f"  - {name}")
            changes += 1
        self.prune(set(chosen))
        self.locked = {n: chosen[n][0] for n in sorted(chosen)}
        write_json(self.lock_path, {
            "_readme": "Written by barq install. Commit it: it pins every package to an exact commit.",
            "packages": self.locked,
        })
        self.warn_namespaces(out)
        return changes

    def prune(self, keep):
        vendor = os.path.join(self.base, VENDOR)
        if not os.path.isdir(vendor):
            return
        for user in os.listdir(vendor):
            udir = os.path.join(vendor, user)
            if user.startswith(".") or not os.path.isdir(udir):
                continue
            for pkg in os.listdir(udir):
                pdir = os.path.join(udir, pkg)
                if f"{user}/{pkg}" not in keep and os.path.isfile(os.path.join(pdir, MARKER)):
                    shutil.rmtree(pdir)
            if not os.listdir(udir):
                os.rmdir(udir)

    def warn_namespaces(self, out):
        seen = {}
        for name, _, m in installed(self.base):
            ns = m.get("namespace")
            if ns and ns in seen:
                out(f"  warning: {seen[ns]} and {name} both use namespace {ns}; {seen[ns]} wins")
            elif ns:
                seen[ns] = name

    def install(self, out, update=None):
        """update: None = reuse barq.lock where it fits; a set of names = look
        again for those; "all" = ignore barq.lock."""
        if update is None and self.locked and lock_fits(self.require, self.locked):
            chosen = {n: (e, None) for n, e in self.locked.items()}
            return self.apply(chosen, out)
        keep = None
        if update == "all":
            keep = set()
        elif update:
            keep = set(self.locked) - set(update)
        res = Resolver(self.locked, keep)
        picked = res.resolve(self.require)
        return self.apply({n: (lock_entry(c), c) for n, c in picked.items()}, out)


# =========================================================================
# Commands
# =========================================================================

INSTALL_HELP = """usage: barq install [user/name[@version] ...]

  barq install                    Install what barq.json lists (the versions in barq.lock
                                  while it still fits)
  barq install acme/utils         Add the newest version (saved as ^x.y.z in barq.json)
  barq install acme/utils@1.2.0   A version or constraint: 1.2.0, ^1.2, ~1.2.3, >=1.0 <2.0
  barq install acme/utils@main    A branch, tag or commit

Packages go in vendor/ (leave it out of git; commit barq.json and barq.lock).
They come from https://github.com/<user>/<name>; BARQ_REGISTRY changes that."""


def _project(create=False):
    base = d = os.getcwd()
    while not os.path.isfile(os.path.join(d, PROJECT)) and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    if os.path.isfile(os.path.join(d, PROJECT)):
        base = d
    if not create and not os.path.isfile(os.path.join(base, PROJECT)):
        raise PackageError(f"no {PROJECT} here; run barq install <user/name> to start one")
    return Project(base)


def _run(fn):
    try:
        return fn()
    except PackageError as e:
        sys.stdout.flush()
        sys.stderr.write(f"barq: {e}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\nbarq: stopped\n")
        return 130


def _split(arg):
    name, _, c = arg.partition("@")
    name = name.strip().strip("/")
    if name.startswith(("https://github.com/", "github.com/")):
        name = name.split("github.com/", 1)[1].removesuffix(".git")
    if not NAME_RE.match(name):
        raise PackageError(f"'{arg}' is not a package name; packages are named user/name, like barq-lang/barq-auth")
    return name, c.strip() or None


def cmd_install(argv):
    if any(a in ("-h", "--help") for a in argv):
        print(INSTALL_HELP)
        return 0
    for a in argv:
        if a.startswith("-"):
            sys.stderr.write(f"barq install: unknown option '{a}'\n")
            return 2

    def go():
        project = _project(create=bool(argv))
        wanted = [_split(a) for a in argv]
        before = dict(project.require)
        for name, c in wanted:
            project.require[name] = c or "*"
        update = {n for n, c in wanted if before.get(n) != project.require[n]} or None
        print("Resolving packages..." if (update or not project.locked) else "Installing from barq.lock...")
        project.install(print, update)
        # A bare `install acme/utils` is saved as ^<what was picked>.
        for name, c in wanted:
            e = project.locked.get(name)
            if c is None and e and e.get("kind", "tag") == "tag":
                project.require[name] = "^" + e["version"]
        if project.require != before or project.created:
            project.save_json()
            print(f"Wrote {PROJECT} and {LOCK}")
        n = len(project.locked)
        print(f"{n} package{'s' if n != 1 else ''} installed in {VENDOR}/")
        return 0
    return _run(go)


def cmd_update(argv):
    if any(a in ("-h", "--help") for a in argv):
        print("usage: barq update [user/name ...]\n\n"
              "Looks for the newest versions barq.json allows (of all packages, or the ones named)\n"
              "and installs them, rewriting barq.lock.")
        return 0

    def go():
        project = _project()
        names = [_split(a)[0] for a in argv]
        for n in names:
            if n not in project.locked and n not in project.require:
                raise PackageError(f"{n} is not installed")
        print("Resolving packages...")
        changes = project.install(print, set(names) if names else "all")
        if not changes:
            print("Everything is up to date")
        return 0
    return _run(go)


def cmd_remove(argv):
    if not argv or any(a in ("-h", "--help") for a in argv):
        print("usage: barq remove user/name ...\n\nTakes packages out of barq.json, vendor/ and barq.lock.")
        return 0 if argv else 2

    def go():
        project = _project()
        for a in argv:
            n = _split(a)[0]
            if n not in project.require:
                raise PackageError(f"{n} is not in {PROJECT}" + (" (another package needs it)" if n in project.locked else ""))
            del project.require[n]
        project.install(print)
        project.save_json()
        print(f"Wrote {PROJECT} and {LOCK}")
        return 0
    return _run(go)


def list_installed():
    """Lines describing vendor/ packages for `barq packages` (empty outside a project)."""
    base = project_base(os.getcwd())
    if base is None:
        return []
    lock = (read_json(os.path.join(base, LOCK), LOCK) or {}).get("packages") or {}
    lines = []
    for name, _, m in installed(base):
        e = lock.get(name) or {}
        label = e.get("version") or m.get("version", "")
        if e.get("kind", "tag") != "tag":
            label = f"{e.get('ref')} ({str(e.get('commit', ''))[:7]})"
        ns = f"  use {m['namespace']}\\..." if m.get("namespace") else ""
        lines.append(f"{name} {label}{ns}".rstrip())
    return lines


def cmd_publish(argv):
    """Check the package in the current folder and tag its version in git."""
    push = "--push" in argv
    dry = "--dry-run" in argv
    for a in argv:
        if a not in ("--push", "--dry-run"):
            if a in ("-h", "--help"):
                print("usage: barq publish [--dry-run] [--push]\n\n"
                      "Checks barq.pkg and the package's code, then tags the version in git (v<version>).\n"
                      "--push also pushes the tag; others can then barq install it.")
                return 0
            sys.stderr.write(f"barq publish: unknown option '{a}'\n")
            return 2

    def go():
        root = os.getcwd()
        path = os.path.join(root, MANIFEST)
        if not os.path.isfile(path):
            raise PackageError(f"no {MANIFEST} here; a package needs one (see barq install --help)")
        with open(path, encoding="utf-8") as f:
            m = read_manifest(f.read(), MANIFEST)
        problems = check_manifest(m)
        src = os.path.join(root, m.get("source", "src"))
        if m.get("namespace") and not os.path.isdir(src):
            problems.append(f"{MANIFEST}: namespace {m['namespace']} reads from {m.get('source', 'src')}/, which is missing")
        # a plugin package's Python __version__ should say the same
        for entry in sorted(os.listdir(root)):
            init = os.path.join(root, entry, "__init__.py")
            if entry.startswith("barq_") and os.path.isfile(init):
                with open(init, encoding="utf-8") as f:
                    v = re.search(r'^__version__\s*=\s*["\']([^"\']+)', f.read(), re.M)
                if v and v.group(1) != m.get("version"):
                    problems.append(f"{entry}/__init__.py says __version__ = \"{v.group(1)}\" but {MANIFEST} says {m.get('version')}")
        if problems:
            raise PackageError("\n  ".join(problems))
        if m.get("namespace"):
            from .check import check_text
            from .cli import bq_files, with_big_stack
            from .errors import report
            from .vm import MAX_DEPTH
            errors = []

            def scan():
                for p in bq_files([os.path.relpath(src)]):
                    with open(p, encoding="utf-8") as f:
                        errors.extend(check_text(f.read(), p, root))
            with_big_stack(scan, MAX_DEPTH * 3)
            for e in errors:
                report(e)
            if errors:
                raise PackageError(f"{len(errors)} error{'s' if len(errors) != 1 else ''} in the code; fix them first")
        tag = "v" + m["version"]
        git("rev-parse", "--is-inside-work-tree", cwd=root)
        if git("status", "--porcelain", cwd=root).strip():
            raise PackageError("the git working tree has uncommitted changes; commit them first")
        if git("tag", "--list", tag, cwd=root).strip():
            raise PackageError(f"{tag} is already tagged; raise version in {MANIFEST} first")
        if dry:
            print(f"{m['name']} {m['version']} is ready to publish as {tag}")
            return 0
        try:
            git("tag", "-a", tag, "-m", f"{m['name']} {m['version']}", cwd=root)
        except PackageError as e:
            if "ident" in str(e) or "tell me who you are" in str(e).lower():
                raise PackageError("git needs your name and email to tag: git config --global user.name \"...\" "
                                   "and git config --global user.email ...") from None
            raise
        print(f"Tagged {tag}")
        if push:
            git("push", "--quiet", "origin", tag, cwd=root)
            print(f"Pushed {tag}; install it with barq install <user>/{os.path.basename(root)}@{m['version']}")
        else:
            print(f"Push it to publish: git push origin {tag}")
        return 0
    return _run(go)
