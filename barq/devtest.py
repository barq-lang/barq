"""Test helpers shared by barq and the Barq packages' own test suites.

Every program runs twice — on the VM (compiled, the default) and on the
tree-walking interpreter (`--interp`) — and both must print exactly the same.

A package's test file sets which packages load and where its checkout is:

    from barq import devtest
    devtest.PACKAGES = "barq_auth"           # loads barq_framework too (REQUIRES)
    devtest.PATHS = [REPO]                    # folders to put on PYTHONPATH

    class GoldenOutput(unittest.TestCase):
        pass
    devtest.add_golden_tests(GoldenOutput, os.path.join(HERE, "cases"))
"""

import glob
import os
import subprocess
import sys

ENGINES = ("vm", "tree")
CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the barq checkout
PACKAGES = "none"       # BARQ_PACKAGES for every barq process the tests start
PATHS = []


def barq_env(cache=False):
    env = dict(os.environ, NO_COLOR="1", BARQ_PACKAGES=PACKAGES)
    parts = [CORE, *PATHS]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    # Package checkouts next to each other are found through BARQ_PATH.
    env["BARQ_PATH"] = os.pathsep.join(sorted({os.path.dirname(p) for p in PATHS}))
    env.pop("BARQ_ENGINE", None)
    if cache:
        env.pop("BARQ_NO_CACHE", None)
    else:
        env["BARQ_NO_CACHE"] = "1"
    return env


def run_barq(path, *args, engine="vm", cache=False):
    flag = "--interp" if engine == "tree" else "--vm"
    proc = subprocess.run([sys.executable, "-m", "barq", "run", flag, path, *args],
                          capture_output=True, text=True, env=barq_env(cache), timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def run_both(test, path):
    """Run on both engines, check they agree exactly, return the VM's result."""
    results = {e: run_barq(path, engine=e) for e in ENGINES}
    test.assertEqual(results["vm"], results["tree"], "the VM and the tree-walker disagree")
    return results["vm"]


def barq_cli(*args, cwd=None, timeout=60, stdin=None):
    proc = subprocess.run([sys.executable, "-m", "barq", *args], capture_output=True, text=True,
                          env=barq_env(), timeout=timeout, cwd=cwd, input=stdin)
    return proc.returncode, proc.stdout, proc.stderr


def add_golden_tests(cls, folder):
    """NAME.bq must run and print exactly NAME.out."""
    for path in sorted(glob.glob(os.path.join(folder, "*.bq"))):
        if not os.path.isfile(path[:-3] + ".out"):
            continue
        name = os.path.splitext(os.path.basename(path))[0]

        def test(self, path=path):
            with open(path[:-3] + ".out", encoding="utf-8") as f:
                expected = f.read()
            code, out, err = run_both(self, path)
            self.assertEqual(code, 0, f"exit code {code}\n{err}")
            self.assertEqual(out, expected)
        setattr(cls, f"test_{name}", test)


def add_error_tests(cls, folder):
    """NAME.bq must fail; its first line is `// expect: <text>` and that text must be in the error."""
    for path in sorted(glob.glob(os.path.join(folder, "*.bq"))):
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as f:
            first = f.readline().strip()
        assert first.startswith("// expect:"), f"{path} needs a '// expect:' first line"
        expected = first[len("// expect:"):].strip()

        def test(self, path=path, expected=expected):
            code, out, err = run_both(self, path)
            self.assertNotEqual(code, 0, f"expected failure, got success:\n{out}")
            self.assertIn(expected, err)
        setattr(cls, f"test_{name}", test)


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port, tries=100):
    import socket
    import time
    for _ in range(tries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def fetch(port, path, method="GET", body=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=body.encode() if body else None,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.read().decode()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode()
        finally:
            e.close()
