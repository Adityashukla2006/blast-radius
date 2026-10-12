from dataclasses import dataclass, field
from tree_sitter import Language, Node, Parser, Query, QueryCursor
import tree_sitter_python as tspython

PY_LANGUAGE = Language(tspython.language())

FUNCTION_QUERY = Query(
    PY_LANGUAGE,
    """
    (function_definition
        name: (identifier) @name
    ) @function
    """,
)
CALL_QUERY = Query(PY_LANGUAGE, "(call function: (_) @callee) @call")

CLASS_QUERY = Query(
    PY_LANGUAGE,
    """
    (class_definition
        name: (identifier) @name
    ) @class
    """,
)

EXPR_RECEIVER = "<expr>"   # receiver that is not a plain name chain: get_client().send(), items[0].save()

ASSIGN_QUERY = Query(PY_LANGUAGE, "(assignment) @assign")
RETURN_QUERY = Query(PY_LANGUAGE, "[(return_statement) (yield)] @ret")
IMPORT_QUERY = Query(PY_LANGUAGE, "[(import_statement) (import_from_statement)] @import")

EXPRESSION_QUERY = Query(
    PY_LANGUAGE,

    """
    (module
        (expression_statement
            (assignment
                left: (identifier) @name
            )
        ) @expression
    )
    """,
)

@dataclass
class CallSite:
    name: str
    receiver: str | None   # None: bare call. "self", "app.db", "super()", or EXPR_RECEIVER
    line: int

@dataclass
class ValueHint:
    """A clue about what a variable holds, taken from one assignment."""
    kind: str    # "annotation": x: Foo   "call": x = Foo() / x = load()   "name": x = other_variable
    text: str    # "Foo | None", "app.models.Foo", "db"

@dataclass
class Expression:
    name : str
    start_line : int
    end_line : int
    hints: list[ValueHint] = field(default_factory=list)

@dataclass
class FunctionInfo:
    name: str
    qualname: str
    start_line: int
    end_line: int
    calls: list[CallSite] = field(default_factory=list)
    class_qualname: str | None = None   # nearest enclosing class, so `self` inside it means that class
    params: dict[str, str | None] = field(default_factory=dict)              # name -> type hint, if any
    return_type: str | None = None
    local_types: dict[str, list[ValueHint]] = field(default_factory=dict)    # x = ... inside this function
    attr_assigns: dict[str, list[ValueHint]] = field(default_factory=dict)   # self.x = ... inside this function
    decorators: list[str] = field(default_factory=list)       # as written, without "@": "pytest.fixture(autouse=True)"
    return_hints: list[ValueHint] = field(default_factory=list)   # return User() / yield session
    refs: list[CallSite] = field(default_factory=list)        # names passed as values: to_thread(advise, ...)
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)   # imports inside this function

@dataclass
class ClassInfo:
    name: str
    qualname: str
    start_line: int
    end_line: int
    bases: list[str] = field(default_factory=list)   # as written: ["Base", "models.Model"]
    attrs: dict[str, list[ValueHint]] = field(default_factory=dict)   # class body: db: Database = ...

@dataclass
class ParsedFile:
    path: str
    functions: list[FunctionInfo]
    imports: dict[str, tuple[str, str | None]]
    expressions : list[Expression] = field(default_factory = list)
    classes: list[ClassInfo] = field(default_factory=list)
    star_imports: list[str] = field(default_factory=list)   # modules in `from X import *`, dots kept

def _text(node: Node, field_name: str) -> str:
    child = node.child_by_field_name(field_name)
    return child.text.decode() if child is not None else ""

def _innermost_function(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None:
        if parent.type == "function_definition":
            return parent
        parent = parent.parent
    return None

def _dotted(node: Node) -> str | None:
    """`app.db` -> "app.db"; None when any part is not a plain name, like `get().db` or `x[0].db`."""
    if node.type == "identifier":
        return node.text.decode()
    if node.type == "attribute":
        obj = _dotted(node.child_by_field_name("object"))
        return f"{obj}.{_text(node, 'attribute')}" if obj is not None else None
    return None

def _receiver(obj: Node) -> str:
    if obj.type == "call" and _text(obj, "function") == "super":
        return "super()"                      # super().save(): the parent class's method
    return _dotted(obj) or EXPR_RECEIVER

def _call_site(callee: Node, line: int) -> CallSite | None:
    if callee.type == "identifier":  # query(...)
        return CallSite(callee.text.decode(), None, line)
    if callee.type == "attribute":  # user.save(...), app.db.query(...), get_client().send(...)
        receiver = _receiver(callee.child_by_field_name("object"))
        return CallSite(_text(callee, "attribute"), receiver, line)
    return None  # funcs[0](), (lambda: 1)(): not statically nameable

def _span(node: Node) -> Node:
    """Include the decorators, so a change to `@app.get(...)` maps to the function below it."""
    return node.parent if node.parent.type == "decorated_definition" else node

def _base_names(class_node: Node) -> list[str]:
    args = class_node.child_by_field_name("superclasses")
    if args is None:
        return []
    bases = []
    for arg in args.named_children:           # keyword_argument (metaclass=...) is skipped
        if arg.type == "subscript":           # Base[T] -> Base
            arg = arg.child_by_field_name("value")
        name = _dotted(arg)
        if name is not None:
            bases.append(name)
    return bases

def _params(fn_node: Node) -> dict[str, str | None]:
    params: dict[str, str | None] = {}
    for p in fn_node.child_by_field_name("parameters").named_children:
        if p.type == "identifier":                            # def f(user)
            params[p.text.decode()] = None
        elif p.type == "default_parameter":                   # def f(user=None)
            params[_text(p, "name")] = None
        elif p.type == "typed_parameter":                     # def f(user: User)
            ident = p.named_children[0]
            if ident.type == "identifier":                    # skip *args: T and **kw: T
                params[ident.text.decode()] = _text(p, "type")
        elif p.type == "typed_default_parameter":             # def f(user: User = None)
            params[_text(p, "name")] = _text(p, "type")
    return params

def _value_hints(value: Node | None) -> list[ValueHint]:
    """Clues in a value: `load()` is a call, `other` is a name."""
    if value is not None and value.type == "await":           # await load()
        value = value.named_children[0] if value.named_children else None
    if value is not None and value.type == "call":
        callee = _dotted(value.child_by_field_name("function"))
        if callee is not None:
            return [ValueHint("call", callee)]
    elif value is not None and value.type in ("identifier", "attribute"):
        name = _dotted(value)
        if name is not None:
            return [ValueHint("name", name)]
    return []

def _hints(assign: Node) -> list[ValueHint]:
    """Clues in one assignment: `x: Foo = load()` gives an annotation and a call."""
    hints = []
    annotation = assign.child_by_field_name("type")
    if annotation is not None:
        hints.append(ValueHint("annotation", annotation.text.decode()))
    return hints + _value_hints(assign.child_by_field_name("right"))

def _decorators(fn_node: Node) -> list[str]:
    if fn_node.parent.type != "decorated_definition":
        return []
    return [d.named_children[0].text.decode() for d in fn_node.parent.children
            if d.type == "decorator" and d.named_children]

def _refs(call: Node, line: int) -> list[CallSite]:
    """Plain names given to a call as arguments: to_thread(advise, db) -> advise, db.

    The callee may call them later. Most will be ordinary variables; the resolver keeps only the
    ones that turn out to be functions or classes.
    """
    args = call.child_by_field_name("arguments")
    if args is None or args.type != "argument_list":
        return []
    refs = []
    for arg in args.named_children:
        if arg.type == "keyword_argument":                    # FastAPI(lifespan=lifespan)
            arg = arg.child_by_field_name("value")
        dotted = _dotted(arg) if arg is not None else None
        if dotted is not None:
            receiver, _, name = dotted.rpartition(".")
            refs.append(CallSite(name, receiver or None, line))
    return refs

def _bind_import(node: Node, imports: dict[str, tuple[str, str | None]], star_imports: list[str]) -> None:
    """Record the names one import statement binds: local name -> (module, name or None for a module)."""
    if node.type == "import_statement":
        for child in node.named_children:
            if child.type == "dotted_name":  # import a.b  ->  binds "a"
                top = child.text.decode().split(".")[0]
                imports[top] = (top, None)
            elif child.type == "aliased_import":  # import a.b as c
                imports[_text(child, "alias")] = (_text(child, "name"), None)
        return
    module = _text(node, "module_name")  # keeps leading dots: ".db"
    if any(child.type == "wildcard_import" for child in node.children):
        star_imports.append(module)      # from x import *
        return
    for child in node.children_by_field_name("name"):
        if child.type == "aliased_import":  # from x import y as z
            imports[_text(child, "alias")] = (module, _text(child, "name"))
        else:  # from x import y
            imports[child.text.decode()] = (module, child.text.decode())

def _innermost_class(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None:
        if parent.type == "class_definition":
            return parent
        parent = parent.parent
    return None

class PythonParser:
    def __init__(self):
        self.parser = Parser(PY_LANGUAGE)

    def parse(self, source : bytes):
        return self.parser.parse(source)

    def parse_file(self, path: str, source: bytes) -> ParsedFile:
        root = self.parser.parse(source).root_node
        exp = self._get_expressions(root)
        by_node = self._get_functions(root)
        classes = self._get_classes(root)
        self._attach_calls(root, by_node)
        self._attach_assignments(root, by_node, classes)
        imports, star_imports = self._collect_imports(root, by_node)
        return ParsedFile(path, list(by_node.values()), imports, list(exp.values()),
                          classes=list(classes.values()), star_imports=star_imports)
    
    def _get_expressions(self, root : Node) -> dict[int, Expression]:
        exp : dict[int, Expression] = {}
        for _, captures in QueryCursor(EXPRESSION_QUERY).matches(root):
            exp_node = captures["expression"][0]
            name = captures["name"][0].text.decode()
            exp[exp_node.start_byte] = Expression(
                name = name,
                start_line = exp_node.start_point.row + 1,
                end_line = exp_node.end_point.row + 1,
                hints = _hints(exp_node.named_children[0]),
            )
        return exp

    def _get_functions(self, root: Node) -> dict[int, FunctionInfo]:
        results: dict[int, FunctionInfo] = {}
        for _, captures in QueryCursor(FUNCTION_QUERY).matches(root):
            fn_node = captures["function"][0]
            name = captures["name"][0].text.decode()
            span = _span(fn_node)

            # keyed by the function_definition node (not the decorated span) to match _innermost_function
            results[fn_node.start_byte] = FunctionInfo(
                name=name,
                qualname=".".join([*self._enclosing_scopes(fn_node), name]),
                start_line=span.start_point.row + 1,
                end_line=span.end_point.row + 1,
                class_qualname=self._enclosing_class(fn_node),
                params=_params(fn_node),
                return_type=_text(fn_node, "return_type") or None,
                decorators=_decorators(fn_node),
            )
        return results

    def _get_classes(self, root: Node) -> dict[int, ClassInfo]:
        classes: dict[int, ClassInfo] = {}
        for _, captures in QueryCursor(CLASS_QUERY).matches(root):
            cls_node = captures["class"][0]
            name = captures["name"][0].text.decode()
            span = _span(cls_node)
            classes[cls_node.start_byte] = ClassInfo(
                name=name,
                qualname=".".join([*self._enclosing_scopes(cls_node), name]),
                start_line=span.start_point.row + 1,
                end_line=span.end_point.row + 1,
                bases=_base_names(cls_node),
            )
        return classes

    def _attach_assignments(self, root: Node, by_node: dict[int, FunctionInfo],
                            classes: dict[int, ClassInfo]) -> None:
        for _, caps in QueryCursor(ASSIGN_QUERY).matches(root):
            assign = caps["assign"][0]
            hints = _hints(assign)
            if not hints:
                continue
            left = assign.child_by_field_name("left")
            owner = _innermost_function(assign)
            if owner is not None:
                fn = by_node[owner.start_byte]
                if left.type == "identifier":                       # x = User()
                    fn.local_types.setdefault(left.text.decode(), []).extend(hints)
                elif left.type == "attribute" and _text(left, "object") == "self":   # self.db = Database()
                    fn.attr_assigns.setdefault(_text(left, "attribute"), []).extend(hints)
                continue
            cls = _innermost_class(assign)
            if cls is not None and left.type == "identifier":       # class body: db: Database
                classes[cls.start_byte].attrs.setdefault(left.text.decode(), []).extend(hints)
            # module level: _get_expressions records those

    @classmethod
    def _enclosing_class(cls, node: Node) -> str | None:
        """Qualname of the nearest class around node, also through nested functions (closures keep `self`)."""
        parent = node.parent
        while parent is not None:
            if parent.type == "class_definition":
                return ".".join([*cls._enclosing_scopes(parent), _text(parent, "name")])
            parent = parent.parent
        return None

    @staticmethod
    def _enclosing_scopes(node : Node):
        scopes = []
        parent = node.parent
        while parent is not None:
            if parent.type in ("class_definition","function_definition"):
                scopes.append(parent.child_by_field_name("name").text.decode())
            parent = parent.parent
        return scopes[::-1]

    def _attach_calls(self, root: Node, by_node: dict[int, FunctionInfo]) -> None:
        for _, caps in QueryCursor(CALL_QUERY).matches(root):
            call, callee = caps["call"][0], caps["callee"][0]
            owner = _innermost_function(call)
            if owner is None:
                continue  # module-level call, e.g. `app = create_app()`
            site = _call_site(callee, call.start_point.row + 1)
            if site is not None:
                by_node[owner.start_byte].calls.append(site)
            by_node[owner.start_byte].refs.extend(_refs(call, call.start_point.row + 1))
        for _, caps in QueryCursor(RETURN_QUERY).matches(root):
            ret = caps["ret"][0]
            owner = _innermost_function(ret)
            if owner is not None and ret.named_children:
                by_node[owner.start_byte].return_hints.extend(_value_hints(ret.named_children[0]))


    def _collect_imports(self, root: Node, by_node: dict[int, FunctionInfo]
                         ) -> tuple[dict[str, tuple[str, str | None]], list[str]]:
        """Module imports (also inside `if`/`try` at the top), and imports inside each function."""
        imports: dict[str, tuple[str, str | None]] = {}
        star_imports: list[str] = []
        for _, caps in QueryCursor(IMPORT_QUERY).matches(root):
            node = caps["import"][0]
            owner = _innermost_function(node)
            if owner is not None:                    # def f(): from app.x import y  (often to avoid a cycle)
                _bind_import(node, by_node[owner.start_byte].imports, [])
            else:
                _bind_import(node, imports, star_imports)
        return imports, star_imports

# def main():
#     p = PythonParser()
#     with open(
#     r"C:\Users\yashi\OneDrive\Documents\blast-radius\bench\targets\cat-hackathon\backend\app\agents\assistant.py",
#     "rb"
#         ) as f:
#         source = f.read()
    
#     result = p.parse_file(r"C:\Users\yashi\OneDrive\Documents\blast-radius\bench\targets\cat-hackathon\backend\app\agents\assistant.py",source)

# if __name__=="__main__":
#     main()