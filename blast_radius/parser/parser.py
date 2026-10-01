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
    receiver: str | None
    line: int

@dataclass
class Expression:
    name : str
    start_line : int
    end_line : int

@dataclass
class FunctionInfo: 
    name: str
    qualname: str      
    start_line: int   
    end_line: int
    calls: list[CallSite] = field(default_factory=list)

@dataclass
class ParsedFile:
    path: str
    functions: list[FunctionInfo]
    imports: dict[str, tuple[str, str | None]]
    expressions : list[Expression] = field(default_factory = list)

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

def _call_site(callee: Node, line: int) -> CallSite | None:
    if callee.type == "identifier":  # query(...)
        return CallSite(callee.text.decode(), None, line)
    if callee.type == "attribute":  # user.save(...), get_client().send(...)
        obj = callee.child_by_field_name("object")
        receiver = obj.text.decode() if obj.type == "identifier" else None
        return CallSite(_text(callee, "attribute"), receiver, line)
    return None  # funcs[0](), (lambda: 1)(): not statically nameable

class PythonParser:
    def __init__(self):
        self.parser = Parser(PY_LANGUAGE)

    def parse(self, source : bytes):
        return self.parser.parse(source)

    def parse_file(self, path: str, source: bytes) -> ParsedFile:
        root = self.parser.parse(source).root_node
        exp = self._get_expressions(root)
        by_node = self._get_functions(root)
        self._attach_calls(root, by_node)
        return ParsedFile(path, list(by_node.values()), self._collect_imports(root),list(exp.values()))
    
    def _get_expressions(self, root : Node) -> dict[int, Expression]:
        exp : dict[int, Expression] = {}
        for _, captures in QueryCursor(EXPRESSION_QUERY).matches(root):
            exp_node = captures["expression"][0]
            name = captures["name"][0].text.decode()
            exp[exp_node.start_byte] = Expression(
                name = name,
                start_line = exp_node.start_point.row + 1,
                end_line = exp_node.end_point.row + 1,
            )
        return exp

    def _get_functions(self, root: Node) -> dict[int, FunctionInfo]:
        results: dict[int, FunctionInfo] = {}
        for _, captures in QueryCursor(FUNCTION_QUERY).matches(root):
            fn_node = captures["function"][0]
            name = captures["name"][0].text.decode()
            span = fn_node.parent if fn_node.parent.type == "decorated_definition" else fn_node

            # keyed by the function_definition node (not the decorated span) to match _innermost_function
            results[fn_node.start_byte] = FunctionInfo(
                name=name,
                qualname=".".join([*self._enclosing_scopes(fn_node), name]),
                start_line=span.start_point.row + 1,
                end_line=span.end_point.row + 1,
            )
        return results

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


    def _collect_imports(self, root: Node) -> dict[str, tuple[str, str | None]]:
        imports: dict[str, tuple[str, str | None]] = {}
        for node in root.children:  # top-level only; function-local imports can come later
            if node.type == "import_statement":
                for child in node.named_children:
                    if child.type == "dotted_name":  # import a.b  ->  binds "a"
                        top = child.text.decode().split(".")[0]
                        imports[top] = (top, None)
                    elif child.type == "aliased_import":  # import a.b as c
                        imports[_text(child, "alias")] = (_text(child, "name"), None)
            elif node.type == "import_from_statement":
                module = _text(node, "module_name")  # keeps leading dots: ".db"
                for child in node.children_by_field_name("name"):
                    if child.type == "aliased_import":  # from x import y as z
                        imports[_text(child, "alias")] = (module, _text(child, "name"))
                    else:  # from x import y
                        imports[child.text.decode()] = (module, child.text.decode())
        return imports

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