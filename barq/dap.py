"""`barq dap`: the Debug Adapter Protocol server editors use to debug Barq.

The editor starts `barq dap` and talks JSON over stdin/stdout (the same
framing as the language server: a Content-Length header, then the body).
The program runs in this process, on its own thread, under the debugger of
debugger.py; this file only translates between the protocol and it.

* The program's output (print, errors) is sent as `output` events, so the
  protocol's stdout stays clean. Its stdin is empty.
* While the program is stopped, everything that reads its state (frames,
  variables, evaluate) runs on the program's thread: the protocol thread
  hands the work over and waits for the answer.
"""

import io
import json
import os
import queue
import sys
import threading

from .debugger import (
    Debugger, DebugError, children, describe_error, has_children, preview, set_child,
)
from .errors import BarqError
from .runtime import BarqThrow, type_name

THREAD_ID = 1

CAPABILITIES = {
    "supportsConfigurationDoneRequest": True,
    "supportsConditionalBreakpoints": True,
    "supportsHitConditionalBreakpoints": True,
    "supportsLogPoints": True,
    "supportsEvaluateForHovers": True,
    "supportsSetVariable": True,
    "supportsTerminateRequest": True,
    "supportsSingleThreadExecutionRequests": False,
    "exceptionBreakpointFilters": [
        {"filter": "uncaught", "label": "Uncaught Exceptions", "default": True,
         "description": "Stop when an exception is not caught by Barq code"},
        {"filter": "all", "label": "All Exceptions", "default": False,
         "description": "Stop on every raised exception, caught or not"},
    ],
}


class Output(io.TextIOBase):
    """A text stream that becomes DAP `output` events."""

    def __init__(self, server, category):
        self.server = server
        self.category = category

    def write(self, s):
        if s:
            self.server.event("output", {"category": self.category, "output": s})
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return "utf-8"


class Server:
    def __init__(self, inp, out):
        self.inp = inp
        self.out = out
        self.seq = 0
        self.write_lock = threading.Lock()
        self.vm = None
        self.dbg = None
        self.launch_args = None
        self.program_thread = None
        self.stop = None              # debugger.Stop while the program is stopped
        self.tasks = queue.Queue()    # work for the stopped program thread
        self.refs = {}                # variablesReference -> ("scope", sf, kind) | ("value", v)
        self.ref_seq = 0
        self.exceptions = ("uncaught",)
        self.pending_breakpoints = []
        self.done = False
        self.exit_code = None
        self.no_debug = False

    # -- the wire ------------------------------------------------------------

    def read(self):
        length = None
        while True:
            line = self.inp.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                if length is not None:
                    break
                continue
            name, _, value = line.decode("ascii", "replace").partition(":")
            if name.lower() == "content-length":
                length = int(value.strip())
        body = b""
        while len(body) < length:
            chunk = self.inp.read(length - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body.decode("utf-8"))

    def send(self, msg):
        with self.write_lock:
            self.seq += 1
            msg["seq"] = self.seq
            data = json.dumps(msg).encode("utf-8")
            self.out.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
            self.out.flush()

    def event(self, name, body=None):
        msg = {"type": "event", "event": name}
        if body is not None:
            msg["body"] = body
        self.send(msg)

    def respond(self, req, body=None, success=True, message=None):
        msg = {"type": "response", "request_seq": req.get("seq", 0), "command": req.get("command"),
               "success": success}
        if body is not None:
            msg["body"] = body
        if message:
            msg["message"] = message
        self.send(msg)

    # -- main loop -----------------------------------------------------------

    def serve(self):
        while not self.done:
            req = self.read()
            if req is None:
                break
            if req.get("type") != "request":
                continue
            handler = getattr(self, "on_" + req.get("command", ""), None)
            if handler is None:
                self.respond(req, success=False, message=f"unsupported request '{req.get('command')}'")
                continue
            try:
                handler(req, req.get("arguments") or {})
            except (DebugError, BarqError, BarqThrow) as e:
                self.respond(req, success=False, message=describe_error(e))
            except Exception as e:  # never let one bad request end the session
                self.respond(req, success=False, message=f"{type(e).__name__}: {e}")
        return self.exit_code or 0

    # -- running work on the stopped program thread --------------------------

    def in_program(self, fn):
        if self.stop is None:
            raise DebugError("the program is running")
        box = []
        done = threading.Event()
        self.tasks.put((fn, box, done))
        if not done.wait(60):
            raise DebugError("the program did not answer")
        kind, value = box[0]
        if kind == "err":
            raise value
        return value

    # Called by the Debugger on the program thread.
    def stopped(self, stop):
        self.stop = stop
        self.refs = {}
        body = {"reason": stop.reason, "threadId": THREAD_ID, "allThreadsStopped": True}
        if stop.description:
            body["description"] = stop.description
            body["text"] = stop.description
        if stop.breakpoint is not None:
            body["hitBreakpointIds"] = [stop.breakpoint.id]
        self.event("stopped", body)
        while True:
            item = self.tasks.get()
            if item[0] == "resume":
                self.stop = None
                self.refs = {}
                return item[1]
            fn, box, done = item
            try:
                box.append(("ok", fn()))
            except BaseException as e:   # noqa: BLE001 - handed back to the protocol thread
                box.append(("err", e))
            done.set()

    def log(self, text):
        self.event("output", {"category": "console", "output": text})

    def resume(self, action):
        if self.stop is None:
            return False
        self.tasks.put(("resume", action))
        return True

    # -- requests ------------------------------------------------------------

    def on_initialize(self, req, args):
        self.respond(req, CAPABILITIES)

    def on_launch(self, req, args):
        program = args.get("program")
        if not program:
            raise DebugError("launch needs a 'program' (the .bq file to run)")
        cwd = args.get("cwd")
        if cwd:
            os.chdir(cwd)
        program = os.path.abspath(program)
        if not os.path.isfile(program):
            raise DebugError(f"file not found: {program}")
        for k, v in (args.get("env") or {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        from .vm import VM
        self.no_debug = bool(args.get("noDebug"))
        self.vm = VM(root=os.path.dirname(program), argv=[str(a) for a in args.get("args") or []],
                     stdout=Output(self, "stdout"), stdin=io.StringIO(""))
        sys.stdout = Output(self, "stdout")
        sys.stderr = Output(self, "stderr")
        self.dbg = Debugger(self.vm, self, self.exceptions)
        self.launch_args = dict(args, program=program)
        for path, specs, _ in self.pending_breakpoints:
            self.dbg.set_breakpoints(path, specs)
        self.respond(req)
        self.event("initialized")

    def on_attach(self, req, args):
        raise DebugError("Barq can only launch programs (use \"request\": \"launch\")")

    def on_setBreakpoints(self, req, args):
        path = (args.get("source") or {}).get("path")
        specs = args.get("breakpoints")
        if specs is None:
            specs = [{"line": n} for n in args.get("lines") or []]
        if not path:
            self.respond(req, {"breakpoints": [{"verified": False} for _ in specs]})
            return
        if self.dbg is None:
            self.pending_breakpoints.append((path, specs, None))
            self.respond(req, {"breakpoints": [{"verified": False, "line": s.get("line")} for s in specs]})
            return
        bps = self.dbg.set_breakpoints(path, specs)
        out = []
        for bp in bps:
            item = {"id": bp.id, "verified": bp.verified, "line": bp.line,
                    "source": {"path": bp.path, "name": os.path.basename(bp.path)}}
            if bp.message:
                item["message"] = bp.message
            out.append(item)
        self.respond(req, {"breakpoints": out})

    def on_setExceptionBreakpoints(self, req, args):
        filters = tuple(f for f in args.get("filters") or [] if f in ("uncaught", "all"))
        self.exceptions = filters
        if self.dbg is not None:
            self.dbg.set_exceptions(filters)
        self.respond(req)

    def on_configurationDone(self, req, args):
        self.respond(req)
        if self.vm is not None and self.program_thread is None:
            self.start_program()

    def start_program(self):
        from .cli import run_guarded
        from .project import boot, load_dotenv
        from .vm import MAX_DEPTH
        vm, dbg, args = self.vm, self.dbg, self.launch_args

        def go():
            load_dotenv(vm)
            boot(vm)
            if not self.no_debug:
                dbg.start(stop_on_entry=bool(args.get("stopOnEntry")))
            try:
                vm.run_file(args["program"])
            finally:
                dbg.close()

        def main():
            code = 1
            try:
                code = run_guarded(go)
            except BaseException as e:   # noqa: BLE001
                self.log(f"barq dap: {type(e).__name__}: {e}\n")
            self.exit_code = code
            self.event("exited", {"exitCode": code})
            self.event("terminated")

        threading.stack_size(512 * 1024 * 1024)
        sys.setrecursionlimit(MAX_DEPTH * 3)
        self.program_thread = threading.Thread(target=main, daemon=True)
        self.program_thread.start()

    def on_threads(self, req, args):
        self.respond(req, {"threads": [{"id": THREAD_ID, "name": "main"}]})

    def on_stackTrace(self, req, args):
        stop = self.stop
        if stop is None:
            self.respond(req, {"stackFrames": [], "totalFrames": 0})
            return
        start = int(args.get("startFrame") or 0)
        levels = int(args.get("levels") or 0)
        frames = stop.frames[start:start + levels] if levels else stop.frames[start:]
        out = []
        for i, sf in enumerate(frames, start):
            out.append({"id": i + 1, "name": sf.name, "line": sf.line, "column": (sf.col or 0) + 1,
                        "source": {"name": os.path.basename(sf.path), "path": sf.path}})
        self.respond(req, {"stackFrames": out, "totalFrames": len(stop.frames)})

    def frame(self, frame_id):
        stop = self.stop
        if stop is None:
            raise DebugError("the program is running")
        i = int(frame_id or 1) - 1
        if not 0 <= i < len(stop.frames):
            raise DebugError("no such frame")
        return stop.frames[i]

    def new_ref(self, item):
        self.ref_seq += 1
        self.refs[self.ref_seq] = item
        return self.ref_seq

    def on_scopes(self, req, args):
        sf = self.frame(args.get("frameId"))
        scopes = [{"name": "Locals", "presentationHint": "locals", "expensive": False,
                   "variablesReference": self.new_ref(("scope", sf, "locals"))}]
        if sf.name != "<main>":
            scopes.append({"name": "Globals", "expensive": False,
                           "variablesReference": self.new_ref(("scope", sf, "globals"))})
        stop = self.stop
        if stop is not None and stop.thrown is not None and sf is stop.frames[0]:
            scopes.insert(0, {"name": "Exception", "expensive": False,
                              "variablesReference": self.new_ref(("value", stop.thrown))})
        self.respond(req, {"scopes": scopes})

    def variable(self, name, value, container=None):
        item = {"name": name, "value": preview(value), "type": type_name(value), "variablesReference": 0}
        if has_children(value):
            item["variablesReference"] = self.new_ref(("value", value))
            n = len(children(value))
            if isinstance(value, list):
                item["indexedVariables"] = n
            else:
                item["namedVariables"] = n
        return item

    def on_variables(self, req, args):
        item = self.refs.get(int(args.get("variablesReference") or 0))
        if item is None:
            self.respond(req, {"variables": []})
            return

        def work():
            if item[0] == "scope":
                _, sf, kind = item
                pairs = sf.variables() if kind == "locals" else sf.globals()
            else:
                pairs = children(item[1])
            start = int(args.get("start") or 0)
            count = int(args.get("count") or 0)
            if start or count:
                pairs = pairs[start:start + count] if count else pairs[start:]
            return [self.variable(name, value) for name, value in pairs]
        self.respond(req, {"variables": self.in_program(work)})

    def on_setVariable(self, req, args):
        item = self.refs.get(int(args.get("variablesReference") or 0))
        if item is None:
            raise DebugError("that variable is gone")
        name, text = args.get("name"), args.get("value", "")
        stop = self.stop

        def work():
            if item[0] == "scope":
                sf = item[1]
                value = self.dbg.eval_in(text, sf)
                if item[2] == "globals":
                    raise DebugError("change top-level variables from the <main> frame")
                sf.set_variable(name, value)
            else:
                value = self.dbg.eval_in(text, stop.frames[0])
                set_child(item[1], name, value)
            return self.variable(name, value)
        v = self.in_program(work)
        self.respond(req, {"value": v["value"], "type": v["type"], "variablesReference": v["variablesReference"]})

    def on_evaluate(self, req, args):
        text = args.get("expression", "")
        context = args.get("context")
        if context == "hover" and ("(" in text or "=" in text):
            raise DebugError("not evaluated on hover")   # never run calls just because of a hover
        sf = self.frame(args.get("frameId"))

        def work():
            value = self.dbg.evaluate(text, sf)
            v = self.variable("", value)
            return {"result": v["value"], "type": v["type"], "variablesReference": v["variablesReference"]}
        self.respond(req, self.in_program(work))

    def step(self, req, action, body=None):
        if not self.resume(action):
            raise DebugError("the program is not stopped")
        self.respond(req, body)

    def on_continue(self, req, args):
        self.step(req, "continue", {"allThreadsContinued": True})

    def on_next(self, req, args):
        self.step(req, "next")

    def on_stepIn(self, req, args):
        self.step(req, "step")

    def on_stepOut(self, req, args):
        self.step(req, "out")

    def on_pause(self, req, args):
        if self.dbg is not None and self.stop is None:
            self.dbg.pause()
        self.respond(req)

    def on_terminate(self, req, args):
        self.respond(req)
        self.finish()

    def on_disconnect(self, req, args):
        self.respond(req)
        self.finish()

    def finish(self):
        """End the session. The program runs in this process, so it ends too."""
        self.done = True
        running = self.program_thread is not None and self.program_thread.is_alive()
        if running:
            if not self.resume("quit"):
                self.event("terminated")
                self.flush_and_exit(self.exit_code or 0)
            else:
                self.program_thread.join(5)
                if self.program_thread.is_alive():
                    self.flush_and_exit(self.exit_code or 0)

    def flush_and_exit(self, code):
        try:
            self.out.flush()
        finally:
            os._exit(code)


def cmd_dap(argv):
    if argv and argv[0] in ("-h", "--help"):
        print("usage: barq dap\n\nStarts the Barq debug adapter on stdin/stdout (Debug Adapter Protocol).\n"
              "Editors run this; see https://github.com/barq-lang/vscode-barq.")
        return 0
    # The protocol owns the real stdin/stdout. Anything else that writes to
    # file descriptor 1 or 2 (a child process, a stray print) is captured and
    # sent as output, and the program reads an empty stdin.
    proto_in = os.fdopen(os.dup(0), "rb", buffering=0)
    proto_out = os.fdopen(os.dup(1), "wb", buffering=0)
    server = Server(io.BufferedReader(proto_in), proto_out)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    sys.stdin = io.StringIO("")
    for fd, category in ((1, "stdout"), (2, "stderr")):
        r, w = os.pipe()
        os.dup2(w, fd)
        os.close(w)

        def pump(r=r, category=category):
            with os.fdopen(r, "rb", buffering=0) as f:
                while True:
                    data = f.read(4096)
                    if not data:
                        return
                    server.event("output", {"category": category, "output": data.decode("utf-8", "replace")})
        threading.Thread(target=pump, daemon=True).start()
    sys.stdout = Output(server, "stdout")
    sys.stderr = Output(server, "stderr")
    code = server.serve()
    return code
