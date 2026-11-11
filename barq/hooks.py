"""Extension points for Barq packages.

The language and its standard library run on their own. Packages such as
barq-framework (models, container, events), barq-auth and barq-permissions plug
in here when they are installed (see plugins.py). Every hook has a default
that works without any package.

A package's register(hooks) function (see plugins.py) fills these in:

    hooks.INSTALLERS        fn(interp): declare the package's built-ins
    hooks.add_command(...)  a `barq <name>` command, or every command with a prefix
    hooks.BOOT              fn(interp): runs before a program, test file or server
    hooks.RELATION_TYPES    method return types read as properties (user.posts)
    hooks.PARAM_EARLY       fn(i, cls, req, n) -> (True, value) | None   handler params (FormRequest)
    hooks.PARAM_BINDERS     fn(i, cls, raw, n) -> (True, value) | None   route params ({user} -> User)
    hooks.NEW_PROJECT       fn(files, opts): add {path: text} to what `barq new` writes
    hooks.VALIDATION_RULES  rule name -> fn(i, rule, args, value, field, n) -> bool
    and the single functions below (replace them by assignment).
"""

INSTALLERS = []
BOOT = []
COMMANDS = {}          # name -> (fn(cmd, args) -> exit code, usage text or None)
PREFIX_COMMANDS = {}   # "make:" -> fn(cmd, args)
USAGE = []             # extra blocks for `barq help`
RELATION_TYPES = set()
PARAM_EARLY = []
PARAM_BINDERS = []
VALIDATION_RULES = {}
NEW_PROJECT = []


def add_command(name, fn, usage=None):
    """name ending in ':' with prefix=True style: use add_prefix_command."""
    COMMANDS[name] = (fn, usage)


def add_prefix_command(prefix, fn):
    PREFIX_COMMANDS[prefix] = fn


def find_command(cmd):
    if cmd in COMMANDS:
        return COMMANDS[cmd][0]
    for prefix, fn in PREFIX_COMMANDS.items():
        if cmd.startswith(prefix):
            return fn
    return None


# -- single-function hooks ----------------------------------------------------

def property_access(i, obj, method, node):
    """obj.name found a method whose return type is in RELATION_TYPES:
    -> (True, value) to return value instead of the method."""
    return False, None


def response_status(i, obj):
    """HTTP status for an object a handler returned (it has toArray())."""
    return 200


def resolvable(i, cls):
    """Can a service container build cls for a handler parameter?"""
    return False


def container_make(i, cls, n):
    """Build a controller, middleware or service class. Without a container:
    call its constructor with no arguments."""
    return i.instantiate(cls, [], n)


def login_url(i, n):
    """Where web requests that need a logged-in user are sent (None: 401)."""
    return None


def default_db(i, n):
    """The default Database connection (shared with the ORM when installed)."""
    from .stdlib2 import database_connect
    db = getattr(i, "_hooks_default_db", None)
    if db is None or db.native.conn is None:
        db = i._hooks_default_db = database_connect(i, [], n)
    return db


def named_db(i, name, n):
    from .stdlib2 import database_connect
    cache = i.__dict__.setdefault("_hooks_named_db", {})
    db = cache.get(name)
    if db is None or db.native.conn is None:
        db = cache[name] = database_connect(i, [name], n)
    return db
