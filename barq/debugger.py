"""Step-through debugger for programs running on the Barq VM.

The compiler gives every generated instruction the Barq line it came from, so
CPython's own monitoring API (sys.monitoring, PEP 669) reports Barq lines
directly. The debugger only asks for line events where it needs them:

* in the files that have breakpoints (each code object of the file), and
* everywhere, while stepping or pausing.

With no breakpoints set the program runs at full speed. Exceptions are watched
through RAISE (all thrown exceptions) and PY_UNWIND (an exception leaving the
last Barq frame, i.e. one no Barq code catches).

A frontend talks to the user. It gets `stopped(stop)` on the program's thread
and returns what to do next: "continue", "next", "step", "out" or "quit".
Two frontends exist: the terminal prompt of `barq debug` (below) and the Debug
Adapter Protocol server of `barq dap` (dap.py), which editors use.
"""

import os
import re
import sys
import threading

from . import nodes as N
from .errors import BarqError, SourceFile
from .runtime import (
    BarqClass, BarqFunction, BarqObject, BarqSet, BarqThrow, BoundMethod, Env, ExitSignal,
    NativeFunction, to_display, type_name, type_str,
)

MON = sys.monitoring
EV = MON.events

# Statements a debugger can stop on (function and class declarations are not:
# they only define things).
STATEMENTS = (N.VarDecl, N.ConstDecl, N.Assign, N.ExprStmt, N.If, N.For, N.While, N.DoWhile,
              N.Break, N.Continue, N.Return, N.Raise, N.Switch, N.Try)

ACTIONS = ("continue", "next", "step", "out", "quit")


def statement_lines(program):
    """Lines a stop can happen on: where a statement starts, plus `elif` and
    `do ... while` conditions and the bodies of `kr(...) => expr` lambdas."""
    lines = set()
    stack = [program]
    while stack:
        x = stack.pop()
        if isinstance(x, (list, tuple)):
            stack.extend(x)
            continue
        if not isinstance(x, N.Node):
            continue
        if isinstance(x, STATEMENTS):
            lines.add(x.line)
        if isinstance(x, N.If):
            lines.update(cond.line for cond, _ in x.branches)
        elif isinstance(x, N.DoWhile) and x.cond is not None:
            lines.add(x.cond.line)
        elif isinstance(x, N.Lambda) and x.is_expr and isinstance(x.body, N.Node):
            lines.add(x.body.line)
        for f in x._fields:
            stack.append(getattr(x, f, None))
    lines.discard(0)
    lines.discard(None)
    return lines


def all_codes(code):
    out = [code]
    for c in code.co_consts:
        if isinstance(c, type(code)):
            out.extend(all_codes(c))
    return out


def real(path):
    return os.path.normcase(os.path.realpath(path))


# ---------------------------------------------------------------------------
# what the debugger knows about each loaded file
# ---------------------------------------------------------------------------

class FileInfo:
    def __init__(self, module, program, compiled):
        self.module = module
        self.path = module.path
        self.real = real(module.path)
        self.lines = statement_lines(program)
        self.sorted_lines = sorted(self.lines)
        self.codes = all_codes(compiled.code)
        self.code_file = compiled.code.co_filename
        self.debug = compiled.debug or {}
        self.names = compiled.names or {}
        self.program = program


class Breakpoint:
    def __init__(self, bid, path, line, condition=None, hit=None, log=None):
        self.id = bid
        self.path = path
        self.requested = line
        self.line = line          # the statement line it was moved to
        self.verified = False
        self.condition = condition or None
        self.hit = hit or None
        self.log = log or None
        self.hits = 0
        self.message = None

    def describe(self):
        extra = ""
        if self.condition:
            extra += f" if {self.condition}"
        if self.hit:
            extra += f" (hit {self.hit})"
        if self.log:
            extra += f" log {self.log!r}"
        return f"{self.id}: {display_path(self.path)}:{self.line}{extra}"


HIT_RE = re.compile(r"^\s*(>=|<=|==|>|<|%)?\s*(\d+)\s*$")


def hit_ok(spec, hits):
    m = HIT_RE.match(spec)
    if not m:
        return True
    op, n = m.group(1) or ">=", int(m.group(2))
    return {">=": hits >= n, "<=": hits <= n, "==": hits == n, ">": hits > n, "<": hits < n,
            "%": n > 0 and hits % n == 0}[op]


def display_path(path):
    try:
        rel = os.path.relpath(path)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


# ---------------------------------------------------------------------------
# stopped frames
# ---------------------------------------------------------------------------

class StackFrame:
    """One Barq frame of a stopped program (innermost first in Stop.frames)."""

    def __init__(self, dbg, frame, info, line, col=0):
        self.dbg = dbg
        self.frame = frame
        self.info = info
        self.line = line
        self.col = col
        code = frame.f_code
        name = code.co_name
        if name == "__barq__":
            self.name = "<main>"
        else:
            self.name = info.names.get(name) or ("<lambda>" if name.startswith("_l") else name)
        self.path = info.path

    # -- variables -----------------------------------------------------------

    def candidates(self):
        """{Barq name: (python name, debug entry)} for the variables in scope."""
        frame = self.frame
        free = set(frame.f_code.co_freevars)
        best = {}
        try:
            local = dict(frame.f_locals)
        except Exception:
            return {}, {}
        for py, entry in self.info.debug.items():
            if py not in local:
                continue
            name, boxed, first, last = entry[0], entry[1], entry[2], entry[3]
            if py not in free and not (first <= self.line <= last):
                continue
            prev = best.get(name)
            if prev is None or first >= prev[1][2]:
                best[name] = (py, entry)
        return best, local

    def variables(self):
        """[(name, value)]: `this`, then the variables in scope."""
        best, local = self.candidates()
        out = []
        if "this" in local:
            out.append(("this", local["this"]))
        for name in sorted(best, key=lambda n: (best[n][1][2], n)):
            py, entry = best[name]
            out.append((name, unbox(local[py], entry[1])))
        return out

    def globals(self):
        """Top-level variables of this frame's file, for frames below <main>."""
        if self.frame.f_code.co_name == "__barq__":
            return []
        f = self.frame.f_back
        while f is not None:
            if f.f_code.co_name == "__barq__" and f.f_code.co_filename == self.info.code_file:
                local = dict(f.f_locals)
                out = []
                for py, entry in self.info.debug.items():
                    if py in local and entry[3] >= 10 ** 9:
                        out.append((entry[0], unbox(local[py], entry[1])))
                return sorted(out)
            f = f.f_back
        env = self.info.module.env
        return sorted((name, e[0]) for name, e in env.vars.items())

    def this(self):
        return self.frame.f_locals.get("this") if "this" in self.frame.f_code.co_varnames else None

    def cls(self):
        """The class whose method this frame runs (for visibility in evaluate)."""
        local = self.frame.f_locals
        cx = local.get("_cx") if "_cx" in self.frame.f_code.co_varnames else None
        if isinstance(cx, BarqClass):
            return cx
        display = self.info.names.get(self.frame.f_code.co_name) or ""
        for sep in (".", "::"):
            if sep in display:
                cls = self.info.module.classes.get(display.split(sep)[0])
                if cls is not None:
                    return cls
        this = self.this()
        return this.cls if isinstance(this, BarqObject) else None

    def func_name(self):
        display = self.info.names.get(self.frame.f_code.co_name)
        if display is None:
            return None
        return re.split(r"\.|::", display)[-1]

    def set_variable(self, name, value):
        best, local = self.candidates()
        found = best.get(name)
        if found is None:
            raise DebugError(f"there is no variable '{name}' here")
        py, entry = found
        t = entry[4] if len(entry) > 4 else None
        if len(entry) > 5 and entry[5]:
            raise DebugError(f"'{name}' is a constant")
        vm = self.dbg.vm
        if t is not None and not vm.matches(value, t, self.info.module):
            raise DebugError(f"cannot put a {type_name(value)} in '{name}' ({type_str(t)})")
        proxy = self.frame.f_locals
        if entry[1]:
            proxy[py][0] = value
        else:
            proxy[py] = value
        return value


def unbox(value, boxed):
    if boxed and type(value) is list and len(value) == 1:
        return value[0]
    return value


class Stop:
    def __init__(self, frames, reason, description=None, breakpoint=None, uncaught=False, thrown=None):
        self.frames = frames            # [StackFrame], innermost first
        self.reason = reason            # "entry" | "step" | "breakpoint" | "pause" | "exception"
        self.description = description
        self.breakpoint = breakpoint
        self.uncaught = uncaught
        self.thrown = thrown            # the Barq exception object for "exception" stops


class DebugError(Exception):
    pass


# ---------------------------------------------------------------------------
# showing values
# ---------------------------------------------------------------------------

def preview(v, depth=0, limit=120):
    """A short one-line form of a value for variable lists."""
    if isinstance(v, (list, dict, BarqSet)) and depth > 0:
        n = len(v.values()) if isinstance(v, BarqSet) else len(v)
        return f"{type_name(v)}({n})"
    if isinstance(v, list):
        s = "#[" + ", ".join(preview(x, depth + 1) for x in v[:20]) + (", …" if len(v) > 20 else "") + "]"
    elif isinstance(v, dict):
        items = list(v.items())
        s = "#{" + ", ".join(f"{to_display(k, None, True)}: {preview(x, depth + 1)}" for k, x in items[:20]) \
            + (", …" if len(items) > 20 else "") + "}"
    elif isinstance(v, BarqSet):
        vals = list(v.values())
        s = "#(" + ", ".join(preview(x, depth + 1) for x in vals[:20]) + (", …" if len(vals) > 20 else "") + ")"
    elif isinstance(v, BarqObject):
        if v.cls.is_enum:
            return f"{v.cls.name}::{v.fields.get('name')}"
        if depth > 0:
            return v.cls.name
        fields = children(v)
        if not fields:
            return v.cls.name + " {}"
        s = v.cls.name + " {" + ", ".join(f"{k}: {preview(x, 1)}" for k, x in fields[:8]) \
            + (", …" if len(fields) > 8 else "") + "}"
    elif isinstance(v, (BarqFunction, NativeFunction, BoundMethod)):
        return f"karo {v.name}()"
    else:
        s = to_display(v, None, True)
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return s


def children(v, cap=1000):
    """[(name, value)] inside a list, dict, set or object."""
    if isinstance(v, list):
        return [(str(i), x) for i, x in enumerate(v[:cap])]
    if isinstance(v, dict):
        return [(to_display(k, None, True), x) for k, x in list(v.items())[:cap]]
    if isinstance(v, BarqSet):
        return [(str(i), x) for i, x in enumerate(list(v.values())[:cap])]
    if isinstance(v, BarqObject):
        out = []
        seen = set()
        native = v.native
        attrs = getattr(native, "attrs", None)
        if isinstance(attrs, dict):     # model columns and loaded relations
            for name, x in list(attrs.items()) + list(getattr(native, "relations", {}).items()):
                out.append((name, x))
                seen.add(name)
        for cls in v.cls.chain():
            for name in cls.props:
                if name not in seen:
                    seen.add(name)
                    out.append((name, v.fields.get(name)))
        for name, x in v.fields.items():
            if name not in seen:
                out.append((name, x))
        return out
    return []


def has_children(v):
    if isinstance(v, (list, dict)):
        return len(v) > 0
    if isinstance(v, BarqSet):
        return len(v.values()) > 0
    if isinstance(v, BarqObject):
        return not v.cls.is_enum or bool(v.fields)
    return False


def set_child(container, name, value):
    """Change one item of a list, dict or object shown by children()."""
    if isinstance(container, list):
        container[int(name)] = value
        return value
    if isinstance(container, dict):
        for k in container:
            if to_display(k, None, True) == name:
                container[k] = value
                return value
        raise DebugError(f"no key {name}")
    if isinstance(container, BarqObject):
        attrs = getattr(container.native, "attrs", None)
        if isinstance(attrs, dict) and name in attrs:
            attrs[name] = value
        else:
            container.fields[name] = value
        return value
    raise DebugError("this value cannot be changed")


# ---------------------------------------------------------------------------
# the debugger
# ---------------------------------------------------------------------------

ASSIGN_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.+?)\s*$", re.S)


class Debugger:
    def __init__(self, vm, frontend, exceptions=("uncaught",)):
        self.vm = vm
        self.frontend = frontend
        self.files = {}           # code filename -> FileInfo
        self.by_real = {}         # real path -> FileInfo
        self.breakpoints = {}     # real path -> {line: Breakpoint}
        self.bp_seq = 0
        self.exceptions = set(exceptions)
        self.mode = None          # None | "step" | "next" | "out" | "pause" | "entry"
        self.depth0 = 0
        self.busy = False         # evaluating an expression: ignore events
        self.lock = threading.Lock()
        self.last_thrown = None
        self.stopped = None       # the Stop being shown
        self.tool = None
        self.parsed = {}          # path -> (mtime, lines) for files not loaded yet
        self.kept = {}            # real path -> Breakpoints added one by one (terminal debugger)
        self.entry = None         # the program's file, for function breakpoints set before it runs
        vm.debugger = self

    # -- monitoring ------------------------------------------------------------

    def start(self, stop_on_entry=False):
        tool = MON.DEBUGGER_ID
        while MON.get_tool(tool) is not None:
            tool += 1
            if tool > 5:
                raise RuntimeError("no free sys.monitoring tool id")
        MON.use_tool_id(tool, "barq-debugger")
        self.tool = tool
        MON.register_callback(tool, EV.LINE, self.on_line)
        MON.register_callback(tool, EV.RAISE, self.on_raise)
        MON.register_callback(tool, EV.PY_UNWIND, self.on_unwind)
        MON.register_callback(tool, EV.PY_RETURN, self.on_return)
        if stop_on_entry:
            self.mode = "entry"
        self.update_events()

    def close(self):
        if self.tool is not None:
            tool, self.tool = self.tool, None
            MON.set_events(tool, 0)
            for info in self.files.values():
                for code in info.codes:
                    try:
                        MON.set_local_events(tool, code, 0)
                    except ValueError:
                        pass
            MON.free_tool_id(tool)
        if self.vm.debugger is self:
            self.vm.debugger = None

    def exception_events(self):
        events = 0
        if "all" in self.exceptions:
            events |= EV.RAISE
        if self.exceptions & {"all", "uncaught"}:
            events |= EV.PY_UNWIND
        return events

    def update_events(self):
        if self.tool is None:
            return
        events = self.exception_events()
        if self.mode is not None:
            events |= EV.LINE
            if self.mode in ("step", "next", "out"):
                events |= EV.PY_RETURN
        MON.set_events(self.tool, events)

    def update_file_events(self, info):
        if self.tool is None:
            return
        events = EV.LINE if self.breakpoints.get(info.real) else 0
        for code in info.codes:
            MON.set_local_events(self.tool, code, events)

    def set_exceptions(self, filters):
        self.exceptions = set(filters)
        self.update_events()

    def pause(self):
        """Stop at the next statement (called from another thread)."""
        if self.mode is None:
            self.mode = "pause"
        self.update_events()

    # -- files and breakpoints ------------------------------------------------

    def loaded(self, module, program, compiled):
        info = FileInfo(module, program, compiled)
        self.files[info.code_file] = info
        self.by_real[info.real] = info
        bps = self.breakpoints.get(info.real)
        if bps:
            self.place(info.real, list(bps.values()), info.sorted_lines)
        self.update_file_events(info)

    def lines_of(self, path):
        info = self.by_real.get(real(path))
        if info is not None:
            return info.sorted_lines
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return None
        cached = self.parsed.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        from .parser import parse
        try:
            with open(path, encoding="utf-8") as f:
                program = parse(SourceFile(path, f.read()))
        except (OSError, BarqError):
            return None
        lines = sorted(statement_lines(program))
        self.parsed[path] = (mtime, lines)
        return lines

    def place(self, rp, bps, lines):
        table = {}
        for bp in bps:
            bp.line = bp.requested
            if lines is None:
                bp.verified = False
                bp.message = "the file could not be read or has a syntax error"
            else:
                after = [n for n in lines if n >= bp.requested]
                if after:
                    bp.line = after[0]
                    bp.verified = True
                    bp.message = None
                else:
                    bp.verified = False
                    bp.message = "no statement on or after this line"
            if bp.verified:
                table.setdefault(bp.line, bp)
        self.breakpoints[rp] = table
        return bps

    def set_breakpoints(self, path, specs):
        """Replace the breakpoints of one file. specs: dicts with line and
        optional condition, hitCondition, logMessage. Returns Breakpoints."""
        path = os.path.abspath(path)
        bps = []
        for spec in specs:
            bid = spec.get("id")
            if bid is None:
                self.bp_seq += 1
                bid = self.bp_seq
            bps.append(Breakpoint(bid, path, int(spec["line"]), spec.get("condition"),
                                  spec.get("hitCondition"), spec.get("logMessage")))
        rp = real(path)
        self.place(rp, bps, self.lines_of(path))
        info = self.by_real.get(rp)
        if info is not None:
            self.update_file_events(info)
        return bps

    def add_breakpoint(self, path, line, condition=None):
        """Add one breakpoint, keeping the file's others (terminal debugger)."""
        rp = real(os.path.abspath(path))
        keep = self.kept.get(rp, [])
        specs = [{"id": b.id, "line": b.requested, "condition": b.condition, "hitCondition": b.hit,
                  "logMessage": b.log} for b in keep]
        specs.append({"line": line, "condition": condition})
        bps = self.set_breakpoints(path, specs)
        self.kept[rp] = bps
        return bps[-1]

    def all_breakpoints(self):
        out = []
        for table in self.breakpoints.values():
            out.extend(table.values())
        return sorted(out, key=lambda b: b.id)

    def remove_breakpoint(self, bid=None):
        """Remove one breakpoint by id, or all of them."""
        found = False
        for rp, bps in self.kept.items():
            self.kept[rp] = [b for b in bps if bid is not None and b.id != bid]
        for rp, table in list(self.breakpoints.items()):
            for line, bp in list(table.items()):
                if bid is None or bp.id == bid:
                    del table[line]
                    found = True
            info = self.by_real.get(rp)
            if info is not None:
                self.update_file_events(info)
        return found

    def function_line(self, name, module):
        """First statement line of a function or method, for `break add` or
        `break User.save`."""
        target = None
        if "." in name or "::" in name:
            cname, mname = re.split(r"\.|::", name, maxsplit=1)
            cls = module.classes.get(cname) if module is not None else None
            if cls is None:
                cls = next((c for c in self.vm.classes.values() if c.name == cname), None)
            if cls is not None:
                target = cls.methods.get(mname)
        else:
            if module is not None:
                target = module.functions.get(name)
            if target is None:
                target = next((f for f in self.vm.functions.values() if f.name == name), None)
        if isinstance(target, BarqFunction) and target.module is not None:
            path, line = target.module.path, target.line
        else:
            # Not loaded yet (a breakpoint given before the program starts):
            # look for it in the file.
            path = module.path if module is not None else self.entry
            line = declared_line(path, name) if path else None
            if line is None:
                return None
        lines = self.lines_of(path) or []
        after = [n for n in lines if n > line]
        return (path, after[0]) if after else None

    # -- events ----------------------------------------------------------------

    def depth(self, frame):
        files = self.files
        n = 0
        while frame is not None:
            if frame.f_code.co_filename in files and frame.f_code.co_name != "<module>":
                n += 1
            frame = frame.f_back
        return n

    def live_frames(self, frame, line=None):
        """StackFrames from a running Python frame outwards."""
        out = []
        files = self.files
        first = True
        while frame is not None:
            info = files.get(frame.f_code.co_filename)
            if info is not None and frame.f_code.co_name != "<module>":
                if first and line is not None:
                    ln, col = line, 0
                else:
                    ln, col = self.vm.position(frame)
                out.append(StackFrame(self, frame, info, ln, col))
                first = False
            frame = frame.f_back
        return out

    def on_line(self, code, line):
        info = self.files.get(code.co_filename)
        if info is None or code.co_name == "<module>":
            return MON.DISABLE
        if self.busy or line not in info.lines:
            return None
        frame = sys._getframe(1)
        bp = None
        table = self.breakpoints.get(info.real)
        if table:
            bp = table.get(line)
        mode = self.mode
        if bp is not None and self.breakpoint_hit(bp, frame, info, line):
            self.suspend(self.live_frames(frame, line), "breakpoint", breakpoint=bp)
            return None
        if mode is None:
            return None
        if mode == "next":
            if self.depth(frame) > self.depth0:
                return None
        elif mode == "out":
            if self.depth(frame) >= self.depth0:
                return None
        reason = {"pause": "pause", "entry": "entry"}.get(mode, "step")
        self.suspend(self.live_frames(frame, line), reason)
        return None

    def on_return(self, code, offset, value):
        """Stepping out of a function stops in its caller, on the line of the
        call (a line event would only come at the caller's next line)."""
        info = self.files.get(code.co_filename)
        if info is None or code.co_name == "<module>":
            return MON.DISABLE
        mode = self.mode
        if self.busy or mode not in ("step", "next", "out"):
            return None
        frame = sys._getframe(1)
        if mode != "step" and self.depth(frame) != self.depth0:
            return None
        frames = self.live_frames(frame.f_back)
        if not frames:
            return None
        self.suspend(frames, "step")
        return None

    def breakpoint_hit(self, bp, frame, info, line):
        frames = None
        if bp.condition:
            frames = self.live_frames(frame, line)
            try:
                value = self.eval_in(bp.condition, frames[0])
                if value is not True and value is not False:
                    raise DebugError(f"the condition must be bool, got {type_name(value)}")
                if not value:
                    return False
            except Exception as e:
                self.frontend.log(f"breakpoint {bp.id}: the condition failed: {describe_error(e)}\n")
                return True
        bp.hits += 1
        if bp.hit and not hit_ok(bp.hit, bp.hits):
            return False
        if bp.log:
            frames = frames or self.live_frames(frame, line)
            text = bp.log.replace("\\", "\\\\").replace('"', '\\"')
            try:
                out = self.eval_in('"' + text + '"', frames[0])
            except Exception as e:
                out = f"{bp.log} ({describe_error(e)})"
            self.frontend.log(to_display(out) + "\n")
            return False
        return True

    def on_raise(self, code, offset, exc):
        if self.busy or "all" not in self.exceptions or not isinstance(exc, BarqThrow):
            return None
        if exc.obj is self.last_thrown:
            return None
        frames = self.live_frames(sys._getframe(1))
        if not frames:
            return None
        self.last_thrown = exc.obj
        self.suspend(frames, "exception", description=exception_text(exc.obj), thrown=exc.obj)
        return None

    def on_unwind(self, code, offset, exc):
        if self.busy or not isinstance(exc, BarqThrow) or not (self.exceptions & {"all", "uncaught"}):
            return None
        files = self.files
        if code.co_filename not in files or code.co_name == "<module>":
            return None
        frame = sys._getframe(1)
        f = frame.f_back
        while f is not None:
            if f.f_code.co_filename in files and f.f_code.co_name != "<module>":
                return None      # a Barq caller may still catch it
            f = f.f_back
        if exc.obj is self.last_thrown and "uncaught" not in self.exceptions:
            return None
        # The frames the exception came through are kept by its traceback.
        frames = []
        tb = exc.__traceback__
        while tb is not None:
            info = files.get(tb.tb_frame.f_code.co_filename)
            if info is not None and tb.tb_frame.f_code.co_name != "<module>":
                frames.append(StackFrame(self, tb.tb_frame, info, tb.tb_lineno or 0))
            tb = tb.tb_next
        frames.reverse()
        if not frames:
            return None
        self.last_thrown = exc.obj
        self.suspend(frames, "exception", description="Uncaught " + exception_text(exc.obj),
                     uncaught=True, thrown=exc.obj)
        return None

    def break_here(self):
        """The `breakpoint()` builtin: stop at the calling Barq line."""
        if self.busy or self.tool is None:
            return
        frames = self.live_frames(sys._getframe(1))
        if frames:
            self.suspend(frames, "breakpoint")

    # -- stopping ----------------------------------------------------------------

    def suspend(self, frames, reason, description=None, breakpoint=None, uncaught=False, thrown=None):
        stop = Stop(frames, reason, description, breakpoint, uncaught, thrown)
        with self.lock:
            self.mode = None
            self.stopped = stop
            try:
                self.vm.stdout.flush()
            except Exception:
                pass
            try:
                action = self.frontend.stopped(stop)
            finally:
                self.stopped = None
        if action not in ACTIONS:
            action = "continue"
        if action == "quit":
            self.mode = None
            self.update_events()
            raise ExitSignal(0)
        if reason == "exception" and uncaught:
            action = "continue"      # nothing left to step through
        if action in ("next", "out"):
            self.mode = action
            self.depth0 = self.depth(frames[0].frame)
        elif action == "step":
            self.mode = "step"
        else:
            self.mode = None
        self.update_events()

    # -- evaluating ----------------------------------------------------------------

    def env_for(self, sf):
        module = sf.info.module
        base = Env(self.vm.builtins)
        for name, value in list(module.functions.items()) + list(module.classes.items()):
            base.declare(name, value, None, const=True)
        glob = Env(base)
        for name, value in sf.globals():
            glob.declare(name, value)
        env = Env(glob, this=sf.this(), cls=sf.cls(), func=sf.func_name())
        for name, value in sf.variables():
            if name != "this":
                env.declare(name, value)
        return env

    def eval_in(self, text, sf):
        from .lexer import tokenize
        from .parser import Parser
        src = SourceFile("<debug>", "(" + text + ")\n")
        parser = Parser(tokenize(src), src)
        expr = parser.expression()
        if parser.tok.type not in ("NEWLINE", "EOF"):
            parser.error(f"unexpected {parser.tok.value!r} after the expression")
        vm = self.vm
        env = self.env_for(sf)
        prev_busy, prev_module = self.busy, vm.eval_module
        self.busy = True
        vm.eval_module = sf.info.module
        try:
            return vm.eval(expr, env)
        finally:
            self.busy, vm.eval_module = prev_busy, prev_module

    def evaluate(self, text, sf):
        """Evaluate a Barq expression in a stopped frame. `name = expr`
        changes a variable. Raises DebugError, BarqError or BarqThrow."""
        m = ASSIGN_RE.match(text)
        if m:
            value = self.eval_in(m.group(2), sf)
            return sf.set_variable(m.group(1), value)
        return self.eval_in(text, sf)


def declared_line(path, name):
    """Line of `karo name` or `Class.method` declared in a file, or None."""
    from .parser import parse
    try:
        with open(path, encoding="utf-8") as f:
            program = parse(SourceFile(path, f.read()))
    except (OSError, BarqError):
        return None
    cname, _, mname = name.replace("::", ".").rpartition(".")
    for stmt in program.body:
        if not cname and isinstance(stmt, N.FuncDecl) and stmt.name == mname:
            return stmt.line
        if cname and isinstance(stmt, N.ClassDecl) and stmt.name == cname:
            for m in stmt.members:
                if isinstance(m, N.FuncDecl) and m.name == mname:
                    return m.line
    return None


def exception_text(obj):
    return f"{obj.cls.name}: {to_display(obj.fields.get('message', ''))}"


def describe_error(e):
    if isinstance(e, BarqThrow):
        return exception_text(e.obj)
    if isinstance(e, BarqError):
        return getattr(e, "message", None) or str(e)
    return str(e) or type(e).__name__


# ---------------------------------------------------------------------------
# `barq debug`: the terminal frontend
# ---------------------------------------------------------------------------

HELP = """Commands (unknown input is evaluated as an expression; Enter repeats the last command):
  n, next              run to the next line in this function (step over calls)
  s, step              run to the next line, going into calls
  o, out, finish       run until this function returns
  c, continue          run until the next breakpoint
  b, break [where] [if cond]
                       set a breakpoint: LINE, FILE:LINE or a function (add, User.save);
                       no argument lists the breakpoints
  cl, clear [n]        remove breakpoint n (or all of them)
  p, print EXPR        print a value (also: NAME = EXPR changes a variable)
  v, vars              show the variables of this frame
  l, list [line]       show the source around the current line
  bt, where            show the call stack
  u, up / d, down      move to the caller / back to the callee
  q, quit              stop the program
"""


class Console:
    """Reads commands from a terminal (or any text stream) when the program stops."""

    def __init__(self, dbg_input=None, out=None):
        self.input = dbg_input or sys.stdin
        self.out = out or sys.stdout
        self.dbg = None
        self.last = ""
        self.list_from = None

    def write(self, text):
        self.out.write(text)
        self.out.flush()

    def log(self, text):
        self.write(text)

    def readline(self, prompt):
        if self.input is sys.stdin and sys.stdin.isatty():
            try:
                return input(prompt)
            except EOFError:
                return None
        self.write(prompt)
        line = self.input.readline()
        if not line:
            return None
        line = line.rstrip("\n")
        if not (self.input is sys.stdin and sys.stdin.isatty()):
            self.write(line + "\n")      # echo, so a piped session reads like a terminal one
        return line

    def stopped(self, stop):
        self.stop = stop
        self.cur = 0
        self.list_from = None
        if stop.reason == "breakpoint" and stop.breakpoint is not None:
            self.write(f"Breakpoint {stop.breakpoint.id} hit\n")
        elif stop.reason == "exception":
            self.write(stop.description + "\n")
        elif stop.reason == "pause":
            self.write("Paused\n")
        self.show_location()
        while True:
            line = self.readline("(barq) ")
            if line is None:
                self.write("\n")
                return "quit"
            line = line.strip()
            if not line:
                line = self.last
                if not line:
                    continue
            action = self.command(line)
            if line.split()[0] in ("n", "next", "s", "step", "o", "out", "finish", "c", "continue", "l", "list"):
                self.last = line if line.split()[0] not in ("l", "list") else "l"
            else:
                self.last = ""
            if action is not None:
                return action

    @property
    def frame(self):
        return self.stop.frames[self.cur]

    def source_line(self, sf, n):
        return sf.info.module.source.line(n)

    def show_location(self):
        sf = self.frame
        self.write(f"→ {display_path(sf.path)}:{sf.line} in {sf.name}\n")
        self.write(f"  {sf.line:>4} │ {self.source_line(sf, sf.line)}\n")

    def command(self, line):
        parts = line.split(None, 1)
        cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
        if cmd in ("c", "continue", "cont"):
            return "continue"
        if cmd in ("n", "next"):
            return "next"
        if cmd in ("s", "step"):
            return "step"
        if cmd in ("o", "out", "finish"):
            return "out"
        if cmd in ("q", "quit", "exit"):
            return "quit"
        if cmd in ("h", "help", "?"):
            self.write(HELP)
        elif cmd in ("b", "break"):
            self.cmd_break(arg)
        elif cmd in ("cl", "clear"):
            self.cmd_clear(arg)
        elif cmd in ("bt", "where", "w", "backtrace"):
            for i, sf in enumerate(self.stop.frames):
                mark = "→" if i == self.cur else " "
                self.write(f"{mark} #{i} {sf.name} at {display_path(sf.path)}:{sf.line}\n")
        elif cmd in ("u", "up"):
            if self.cur + 1 < len(self.stop.frames):
                self.cur += 1
                self.list_from = None
                self.show_location()
            else:
                self.write("already at the outermost frame\n")
        elif cmd in ("d", "down"):
            if self.cur > 0:
                self.cur -= 1
                self.list_from = None
                self.show_location()
            else:
                self.write("already at the innermost frame\n")
        elif cmd in ("v", "vars", "locals"):
            items = self.frame.variables()
            if not items:
                self.write("(no variables)\n")
            for name, value in items:
                self.write(f"  {name} = {preview(value)}\n")
        elif cmd in ("l", "list"):
            self.cmd_list(arg)
        elif cmd in ("p", "print") and arg:
            self.show_eval(arg)
        else:
            self.show_eval(line)
        return None

    def show_eval(self, text):
        try:
            value = self.dbg.evaluate(text, self.frame)
            self.write(preview(value, limit=2000) + "\n")
        except (DebugError, BarqError, BarqThrow) as e:
            self.write(f"error: {describe_error(e)}\n")

    def cmd_list(self, arg):
        sf = self.frame
        if arg.strip().isdigit():
            start = max(1, int(arg) - 5)
        elif self.list_from is not None:
            start = self.list_from
        else:
            start = max(1, sf.line - 5)
        lines = sf.info.module.source.lines
        end = min(len(lines), start + 10)
        bps = self.dbg.breakpoints.get(sf.info.real, {})
        for n in range(start, end + 1):
            mark = "→" if n == sf.line else ("●" if n in bps else " ")
            self.write(f"{mark} {n:>4} │ {lines[n - 1]}\n")
        self.list_from = end + 1

    def cmd_break(self, arg):
        dbg = self.dbg
        if not arg:
            bps = dbg.all_breakpoints()
            if not bps:
                self.write("no breakpoints\n")
            for bp in bps:
                self.write(f"  {bp.describe()}\n")
            return
        cond = None
        m = re.match(r"^(.*?)\s+if\s+(.+)$", arg)
        if m:
            arg, cond = m.group(1).strip(), m.group(2)
        sf = self.frame
        path, line = parse_location(arg, sf.path, dbg, sf.info.module)
        if line is None:
            self.write(f"error: cannot find {arg!r} (use LINE, FILE:LINE or a function name)\n")
            return
        bp = dbg.add_breakpoint(path, line, cond)
        if bp.verified:
            self.write(f"Breakpoint {bp.id} at {display_path(bp.path)}:{bp.line}\n")
        else:
            self.write(f"Breakpoint {bp.id} at {display_path(bp.path)}:{bp.requested} is not active: {bp.message}\n")

    def cmd_clear(self, arg):
        arg = arg.strip()
        if not arg:
            self.dbg.remove_breakpoint(None)
            self.write("removed all breakpoints\n")
        elif arg.isdigit():
            if self.dbg.remove_breakpoint(int(arg)):
                self.write(f"removed breakpoint {arg}\n")
            else:
                self.write(f"error: no breakpoint {arg}\n")
        else:
            self.write("usage: clear [number]\n")


def parse_location(text, default_path, dbg, module=None):
    """'12', 'app/x.bq:12' or a function name -> (path, line)."""
    text = text.strip()
    if text.isdigit():
        return default_path, int(text)
    m = re.match(r"^(.+):(\d+)$", text)
    if m:
        path = m.group(1)
        if not os.path.isabs(path):
            for base in (os.getcwd(), dbg.vm.root):
                if os.path.isfile(os.path.join(base, path)):
                    path = os.path.join(base, path)
                    break
        return os.path.abspath(path), int(m.group(2))
    found = dbg.function_line(text, module)
    if found:
        return found
    return default_path, None


DEBUG_USAGE = """usage: barq debug [options] <file.bq> [args...]

Runs a program on the VM and stops on its first line, so you can set
breakpoints and step through it. Type 'help' at the (barq) prompt.

Options:
  -b, --break WHERE    set a breakpoint first (LINE, FILE:LINE or a function name; repeatable)
  -c, --continue       don't stop on the first line (run to the first breakpoint)
  --exceptions MODE    when to stop on a thrown exception: uncaught (default), all, none
  --no-cache           don't read or write .bqc cache files
"""


def cmd_debug(argv):
    from .cli import run_guarded
    from .vm import VM, MAX_DEPTH
    breaks, stop_entry, exceptions, cache = [], True, ("uncaught",), True
    rest = list(argv)
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in ("-h", "--help"):
            print(DEBUG_USAGE, end="")
            return 0
        if flag in ("-b", "--break") and rest:
            breaks.append(rest.pop(0))
        elif flag in ("-c", "--continue"):
            stop_entry = False
        elif flag == "--exceptions" and rest:
            mode = rest.pop(0)
            if mode not in ("uncaught", "all", "none"):
                sys.stderr.write("barq debug: --exceptions is uncaught, all or none\n")
                return 2
            exceptions = () if mode == "none" else (mode,)
        elif flag == "--no-cache":
            cache = False
        else:
            sys.stderr.write(f"barq debug: unknown option '{flag}'\n{DEBUG_USAGE}")
            return 2
    if not rest:
        sys.stderr.write(DEBUG_USAGE)
        return 2
    path, args = rest[0], rest[1:]
    if not os.path.isfile(path):
        sys.stderr.write(f"barq: file not found: {path}\n")
        return 2
    path = os.path.abspath(path)
    vm = VM(root=os.path.dirname(path), argv=args, cache=cache)
    console = Console()
    dbg = Debugger(vm, console, exceptions)
    console.dbg = dbg
    dbg.entry = path
    for where in breaks:
        cond = None
        m = re.match(r"^(.*?)\s+if\s+(.+)$", where)
        if m:
            where, cond = m.group(1).strip(), m.group(2)
        bpath, line = parse_location(where, path, dbg)
        if line is None:
            sys.stderr.write(f"barq debug: cannot find {where!r} (use LINE, FILE:LINE or a function name)\n")
            return 2
        bp = dbg.add_breakpoint(bpath, line, cond)
        if not bp.verified:
            sys.stderr.write(f"barq debug: breakpoint at {display_path(bpath)}:{line} is not active: {bp.message}\n")
    from .project import boot, load_dotenv
    if run_guarded(lambda: load_dotenv(vm)) != 0:
        return 1
    console.write(f"Debugging {display_path(path)} — type 'help' for commands.\n")

    def go():
        boot(vm)
        dbg.start(stop_on_entry=stop_entry)
        try:
            vm.run_file(path)
        finally:
            dbg.close()

    result = []
    threading.stack_size(512 * 1024 * 1024)
    sys.setrecursionlimit(MAX_DEPTH * 3)
    t = threading.Thread(target=lambda: result.append(run_guarded(go)), daemon=True)
    t.start()
    while t.is_alive():
        try:
            t.join(0.25)
        except KeyboardInterrupt:
            if dbg.stopped is None:       # Ctrl+C while running: stop at the next line
                dbg.pause()
    sys.stdout.flush()
    code = result[0] if result else 1
    console.write(f"Program finished (exit code {code}).\n")
    return code
