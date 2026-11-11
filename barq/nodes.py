"""AST node classes. Every node records the line and column it started on."""


class Node:
    _fields = ()

    def __init__(self, *args, line=0, col=0):
        for name, value in zip(self._fields, args):
            setattr(self, name, value)
        for name in self._fields[len(args):]:
            setattr(self, name, None)
        self.line = line
        self.col = col

    def __repr__(self):
        inner = ", ".join(f"{f}={getattr(self, f)!r}" for f in self._fields)
        return f"{type(self).__name__}({inner})"


def node(name, fields):
    return type(name, (Node,), {"_fields": tuple(fields.split()) if fields else ()})


# -- types -------------------------------------------------------------------

# name: "int", "list", "User", "App\\Models\\User"; args: list of TypeRef;
# nullable: True for `?int` (null is also allowed), else None
TypeRef = node("TypeRef", "name args nullable")

# -- expressions -------------------------------------------------------------

Literal = node("Literal", "value")                 # int, float, bool, None, plain str
Interp = node("Interp", "parts")                   # parts: list of str | expr
Name = node("Name", "id")
This = node("This", "")
ListLit = node("ListLit", "items")
DictLit = node("DictLit", "pairs")                 # list of (key_expr, value_expr)
SetLit = node("SetLit", "items")
Unary = node("Unary", "op operand")
Binary = node("Binary", "op left right")
Logical = node("Logical", "op left right")         # and / or / ??
Compare = node("Compare", "op left right")         # == != < > <= >= in "not in"
IsType = node("IsType", "expr type")
Ternary = node("Ternary", "cond then other")
Cast = node("Cast", "type expr")
Call = node("Call", "func args")
Attr = node("Attr", "obj name safe")               # safe: True for ?.
Index = node("Index", "obj index")
StaticRef = node("StaticRef", "target name")       # target: Name | "parent" | "self"
New = node("New", "cls args")                      # cls: qualified name string
Lambda = node("Lambda", "params ret body is_expr") # body: expr (is_expr) or list of stmts
NamedArg = node("NamedArg", "name value")          # greet(name: "Ali")
Spread = node("Spread", "value")                   # sum(*numbers)

# -- statements --------------------------------------------------------------

# default: constant expr or None; variadic: `*int xs` (type is then list<int>);
# promote: "public"/"private"/"protected" for constructor promotion, else None
Param = node("Param", "type name default variadic promote")
VarDecl = node("VarDecl", "type name value")
ConstDecl = node("ConstDecl", "name value")
Assign = node("Assign", "target op value")         # op: "=", "+=", ...
ExprStmt = node("ExprStmt", "expr")
If = node("If", "branches other")                  # branches: list of (cond, body)
For = node("For", "key var iterable body")         # key may be None
While = node("While", "cond body")
DoWhile = node("DoWhile", "body cond")
Break = node("Break", "")
Continue = node("Continue", "")
Return = node("Return", "value")
Raise = node("Raise", "value")
Switch = node("Switch", "subject cases default")   # cases: list of (list[expr], body)
Try = node("Try", "body handlers final")           # handlers: list of (types, name, body)
FuncDecl = node("FuncDecl", "name params ret visibility is_static is_abstract body")
Property = node("Property", "visibility is_static type name value")
# kind: "class" | "interface" | "trait" | "enum"; interfaces: names after
# `implements` (or after `extends` for an interface)
ClassDecl = node("ClassDecl", "name parent is_abstract members is_enum backing cases interfaces kind")
UseTrait = node("UseTrait", "names rules")         # `use Timestampable, SoftDeletes` in a class body
# A rule in the indented block under `use A, B`:
#   A::hello insteadof B        trait="A" method="hello" insteadof=["B"]
#   B::hello as helloB          alias="helloB"
#   hello as protected          visibility="protected"
TraitRule = node("TraitRule", "trait method insteadof alias visibility")
EnumCase = node("EnumCase", "name value")              # value: Literal or None (pure enum)
Namespace = node("Namespace", "name")
Use = node("Use", "imports")                       # list of (qualified_name, alias)
Program = node("Program", "body")
