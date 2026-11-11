"""Finds and loads Barq packages (barq-framework, barq-auth, barq-permissions...).

A package is a Python package named `barq_<name>` whose __init__.py has

    NAME = "barq-auth"
    REQUIRES = ["barq_framework"]      # loaded first
    def register(hooks): ...           # fills in barq.hooks

Where they are looked for, in order:
  1. BARQ_PATH — folders (separated by os.pathsep) holding package checkouts,
     e.g. BARQ_PATH=~/code   finds ~/code/barq-auth/barq_auth
  2. the folder this barq checkout sits in, so repos cloned side by side
     (barq/, barq-framework/, barq-auth/) work with no setup
  3. anything already importable (pip install, PYTHONPATH)

BARQ_PACKAGES=none loads no packages; BARQ_PACKAGES=barq_framework,barq_auth
loads only those (and what they require).
"""

import importlib
import os
import pkgutil
import sys

from . import hooks

LOADED = []      # package modules, in load order
_done = False


def checkout_dirs():
    dirs = [d for d in os.environ.get("BARQ_PATH", "").split(os.pathsep) if d]
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    dirs.append(here)
    return dirs


def discover():
    """Names of the barq_* packages that can be imported."""
    for base in checkout_dirs():
        base = os.path.expanduser(base)
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            repo = os.path.join(base, entry)
            if not os.path.isdir(repo):
                continue
            for sub in os.listdir(repo):
                if sub.startswith("barq_") and os.path.isfile(os.path.join(repo, sub, "__init__.py")):
                    if repo not in sys.path:
                        sys.path.append(repo)
    return sorted({m.name for m in pkgutil.iter_modules() if m.name.startswith("barq_") and m.ispkg})


def load():
    """Import and register every package once per process."""
    global _done
    if _done:
        return LOADED
    _done = True
    wanted = os.environ.get("BARQ_PACKAGES", "").strip()
    if wanted.lower() == "none":
        return LOADED
    names = discover() if not wanted else [w.strip() for w in wanted.split(",") if w.strip()]
    if wanted:
        discover()      # puts side-by-side checkouts on sys.path
    seen = set()

    def visit(name, chain=()):
        if name in seen:
            return
        if name in chain:
            raise RuntimeError("Barq packages require each other: " + " -> ".join(chain + (name,)))
        try:
            mod = importlib.import_module(name)
        except ImportError as e:
            raise RuntimeError(f"cannot load Barq package {name}: {e}") from e
        if not hasattr(mod, "register"):
            seen.add(name)
            return
        for dep in getattr(mod, "REQUIRES", []):
            visit(dep, chain + (name,))
        seen.add(name)
        mod.register(hooks)
        LOADED.append(mod)

    for name in names:
        visit(name)
    return LOADED


def install(interp):
    """Declare every package's built-ins in a new interpreter."""
    load()
    for fn in hooks.INSTALLERS:
        fn(interp)


def describe():
    load()
    return [(getattr(m, "NAME", m.__name__), getattr(m, "__version__", "")) for m in LOADED]
