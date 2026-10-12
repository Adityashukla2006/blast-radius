import os
import re
import subprocess
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import networkx as nx

from ..cache.cache import FALLBACK_DIR, ParseCache
from ..impact.impact import CONFIDENCE, DEFAULT_CONFIDENCE, looks_like_test
from ..parser.parser import (
    EXPR_RECEIVER,
    CallSite,
    ClassInfo,
    Expression,
    FunctionInfo,
    ParsedFile,
    PythonParser,
    ValueHint,
)

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "build", "dist", "node_modules", FALLBACK_DIR}
MAX_HOPS = 20          # import hops find_symbol may follow before giving up
MAX_TYPE_HOPS = 5      # x = y, y = z, ...: how far a variable's type is followed
SELF_RECEIVERS = {"self", "cls"}
# Methods that built-in types also have. A name-match guess on these would be wrong far too often.
BUILTIN_METHODS = {n for t in (object, dict, list, set, frozenset, str, bytes, tuple, int, float) for n in dir(t)}
# typing wrappers whose arguments are the real types: Optional[User] -> User
TYPE_WRAPPERS = {"Optional", "Union", "Annotated", "Final", "ClassVar"}
FIXTURE_DECORATOR = re.compile(r"^(pytest\.)?fixture\b")          # @pytest.fixture, @fixture(scope=...)
USEFIXTURES = re.compile(r"^(pytest\.)?mark\.usefixtures\(")
NOT_FIXTURES = {"self", "cls"}

def fixture_name(fn: FunctionInfo) -> tuple[str, bool] | None:
    """(name, autouse) when fn is a pytest fixture. @pytest.fixture(name="x") renames it."""
    for decorator in fn.decorators:
        if FIXTURE_DECORATOR.match(decorator):
            renamed = re.search(r"name\s*=\s*['\"](\w+)", decorator)
            return (renamed.group(1) if renamed else fn.name), "autouse=True" in decorator.replace(" ", "")
    return None

def _split_top(text: str, sep: str) -> list[str]:
    """Split on sep, but not inside brackets: "dict[str, A], B" on "," -> ["dict[str, A]", " B"]."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts

def annotation_names(text: str) -> list[str]:
    """Class names a type hint can mean: "User | None" -> ["User"], "Optional['User']" -> ["User"].

    A container like list[User] gives "list", not "User": the variable is the list, not a User.
    """
    names = []
    for part in _split_top(text.strip().strip("'\""), "|"):
        part = part.strip().strip("'\"")
        if part.endswith("]") and "[" in part:
            head, inner = part[: part.index("[")].strip(), part[part.index("[") + 1 : -1]
            if head.split(".")[-1] in TYPE_WRAPPERS:
                args = _split_top(inner, ",")
                for arg in args[:1] if head.endswith("Annotated") else args:
                    names += annotation_names(arg)
                continue
            part = head
        if part and part != "None" and all(p.isidentifier() for p in part.split(".")):
            names.append(part)
    return names

def python_files(repo: Path) -> list[str]:
    """Repo-relative .py paths. In a git checkout, .gitignore decides (so cloned bench targets stay out)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py"],
            capture_output=True, text=True, check=True,
        ).stdout
        paths = [p for p in out.split("\0") if p]
        # --cached still lists files deleted from disk but not yet staged
        return [p for p in paths if not SKIP_DIRS.intersection(p.split("/")) and (repo / p).is_file()]
    except (OSError, subprocess.CalledProcessError):
        pass   # not a git checkout: walk the folder instead
    files = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]   # prune in place so os.walk skips them
        files += [(Path(root) / n).relative_to(repo).as_posix() for n in names if n.endswith(".py")]
    return files

def module_name(rel_path : str) -> str:
    parts = Path(rel_path).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)

def import_name(rel_path: str, py_files: set[str]) -> str:
    """The name Python imports a file by: the path from the folder above its top package.

    backend/app/db.py -> "app.db" when backend/app/__init__.py exists but backend/__init__.py does not,
    because the code runs from inside backend/ and writes `from app.db import ...`.
    """
    parts = Path(rel_path).parts
    start = len(parts) - 1                    # index of the first path part kept; the file itself always stays
    while start > 0 and "/".join([*parts[:start], "__init__.py"]) in py_files:
        start -= 1                            # this folder is a package, so its name is part of the import name
    return module_name("/".join(parts[start:]))

def absolute_module(importer: str, is_package: bool, target: str) -> str:
    if not target.startswith("."):
        return target                              # already absolute: 'os', 'pkg.db'
    level = len(target) - len(target.lstrip("."))  # number of leading dots
    base = importer.split(".")
    if not is_package:
        base = base[:-1]                           # a module's "." is its parent package
    base = base[: len(base) - (level - 1)]         # each extra dot goes up one more
    rest = target.lstrip(".")
    return ".".join(p for p in [*base, rest] if p)


@dataclass(frozen=True)
class Symbol:
    """What a name means: a function, a class, or a whole module in this repo."""
    kind: str              # "function", "class" or "module"
    module: str
    qualname: str = ""     # empty for a module

    @property
    def node(self) -> str:
        return f"{self.module}:{self.qualname}"


class Resolver:
    def __init__(self, repo : str | Path, use_cache: bool = True, guess: bool = True):
        self.repo = Path(repo)
        self.cache = ParseCache(self.repo) if use_cache else None
        self.guess = guess                    # False: no name-match edges, only ones the code proves
        self.functions: dict[str, FunctionInfo] = {}          # node id -> function
        self.methods: dict["Symbol", list[FunctionInfo]] = {}  # class -> methods written in its body
        self.method_owners: dict[str, list[str]] = {}         # method name -> node ids of every such method
        self.fixtures: dict[str, dict[str, str]] = {}         # module -> fixture name -> node id
        self.autouse: dict[str, list[str]] = {}               # module -> autouse fixture node ids
        self.ref_stats: Counter = Counter()                   # functions passed as values, by label
        self.files: dict[str, ParsedFile] = {}
        self.top_level: dict[str, dict[str, FunctionInfo]] = {}
        self.classes: dict[str, dict[str, ClassInfo]] = {}   # module -> class qualname -> class, nested too
        self.graph = nx.DiGraph()
        self.stats: Counter = Counter()
        self.variables: dict[str, dict[str, Expression]] = {}
        self.aliases: dict[str, str] = {}     # import name -> module name, e.g. "app.db" -> "backend.app.db"
        self.ambiguous: set[str] = set()      # import names that more than one file claims: never used
        self._bases: dict[Symbol, list[Symbol]] = {}
        self._children: dict[Symbol, list[Symbol]] = {}   # direct subclasses in this repo, filled by link()

    @staticmethod
    def node_id(module: str, fn: FunctionInfo) -> str:
        return f"{module}:{fn.qualname}"

    def index(self) -> None:
        parser = PythonParser()
        rels = python_files(self.repo)
        py_files = set(rels)
        for rel in rels:
            mod = module_name(rel)
            self._add_alias(import_name(rel, py_files), mod)
            if self.cache is not None:
                parsed = self.cache.get(rel, parser)
            else:
                parsed = parser.parse_file(rel, (self.repo / rel).read_bytes())

            self.files[mod] = parsed
            self.top_level[mod] = {}
            self.variables[mod] = {}
            self.classes[mod] = {}
            for fn in parsed.functions:
                if "." not in fn.qualname:          # only top-level functions are importable
                    self.top_level[mod][fn.name] = fn
                self.graph.add_node(self.node_id(mod, fn), file=rel, start=fn.start_line, end=fn.end_line,
                                    kind="function")
                self.functions[self.node_id(mod, fn)] = fn
                if fn.class_qualname is not None and fn.qualname == f"{fn.class_qualname}.{fn.name}":
                    self.methods.setdefault(Symbol("class", mod, fn.class_qualname), []).append(fn)
                    self.method_owners.setdefault(fn.name, []).append(self.node_id(mod, fn))
                fixture = fixture_name(fn) if "." not in fn.qualname else None
                if fixture is not None:
                    name, autouse = fixture
                    self.fixtures.setdefault(mod, {})[name] = self.node_id(mod, fn)
                    if autouse:
                        self.autouse.setdefault(mod, []).append(self.node_id(mod, fn))
            for cls in parsed.classes:
                self.classes[mod][cls.qualname] = cls
                self.graph.add_node(f"{mod}:{cls.qualname}", file=rel, start=cls.start_line, end=cls.end_line,
                                    kind="class")
            for exp in parsed.expressions:
                self.variables[mod][exp.name] = exp
                self.graph.add_node(f"{mod}:{exp.name}", file=rel, start=exp.start_line, end=exp.end_line,
                                    kind="variable")
        if self.cache is not None:
            self.cache.save()

    def _add_alias(self, name: str, mod: str) -> None:
        if name == mod or name in self.ambiguous:
            return
        if self.aliases.setdefault(name, mod) != mod:   # a second file wants the same name
            del self.aliases[name]
            self.ambiguous.add(name)                    # a wrong edge is worse than a missing one

    def canonical(self, name: str) -> str | None:
        """Module name for an import name, or None when it is not code in this repo (or is ambiguous)."""
        if name in self.files:
            return name
        return self.aliases.get(name)

    def _is_pkg(self, module: str) -> bool:
        return self.files[module].path.endswith("__init__.py")

    # ---- name lookup -------------------------------------------------------------------------------

    def find_symbol(self, module: str, name: str, seen: set | None = None,
                    submodules: bool = True) -> Symbol | None:
        """What `name` means inside `module`. Follows imports, so re-exports through __init__.py work."""
        seen = set() if seen is None else seen
        if (module, name) in seen or len(seen) > MAX_HOPS:
            return None                     # import cycle, or a chain too long to trust
        seen.add((module, name))

        fn = self.top_level.get(module, {}).get(name)
        if fn is not None:
            return Symbol("function", module, fn.qualname)
        if name in self.classes.get(module, {}):          # a name without dots only matches top-level classes
            return Symbol("class", module, name)
        if submodules:                                    # pkg + "db" -> the module pkg.db
            sub = self.canonical(f"{module}.{name}")
            if sub is not None:
                return Symbol("module", sub)

        parsed = self.files.get(module)
        if parsed is None:
            return None
        if name in parsed.imports:
            return self.import_symbol(module, *parsed.imports[name], seen)
        for star in parsed.star_imports:
            src_mod = self.canonical(absolute_module(module, self._is_pkg(module), star))
            found = self.find_symbol(src_mod, name, seen) if src_mod else None
            if found is not None:
                return found
        return None

    def import_symbol(self, module: str, src: str, symbol: str | None, seen: set | None = None) -> Symbol | None:
        """Follow one import written in `module`: `from src import symbol`, or `import src` when symbol is None."""
        src_mod = self.canonical(absolute_module(module, self._is_pkg(module), src))
        if src_mod is None:
            return None                     # third-party or standard library
        if symbol is None:                  # import x / import a.b as c: the name is a module
            return Symbol("module", src_mod)
        return self.find_symbol(src_mod, symbol, seen)

    def resolve_dotted(self, module: str, dotted: str, fn: FunctionInfo | None = None) -> tuple[Symbol, str] | None:
        """Resolve a name as written in `module` (inside `fn`, if given), like "query", "db.query" or "User.create".

        Returns the symbol and the edge label: local, import, module or class.
        """
        first, *rest = dotted.split(".")
        if fn is not None and first in fn.imports:      # an import inside the function hides outer names
            current = self.import_symbol(module, *fn.imports[first])
        else:
            # Python never binds a submodule by itself, so a bare name must come from this file or its imports
            current = self.find_symbol(module, first, submodules=False)
        if current is None:
            return None             # local variable, parameter, builtin or third-party name
        label = "local" if current.module == module else "import"

        for part in rest:
            if current.kind == "module":
                current, label = self.find_symbol(current.module, part), "module"
            elif current.kind == "class":
                current, label = self.find_member(current, part), "class"
            else:
                return None         # func.attr(): needs runtime knowledge
            if current is None:
                return None
        return current, label

    # ---- classes -----------------------------------------------------------------------------------

    def bases(self, cls: Symbol) -> list[Symbol]:
        """Parent classes that are in this repo, resolved in the file that defines `cls`."""
        if cls not in self._bases:
            self._bases[cls] = []                         # placeholder first: guards against class A(A)
            info = self.classes[cls.module][cls.qualname]
            found = [self.resolve_dotted(cls.module, base) for base in info.bases]
            self._bases[cls] = [s for s, _ in filter(None, found) if s.kind == "class"]
        return self._bases[cls]

    def mro(self, cls: Symbol) -> Iterator[Symbol]:
        """The class, then its parents: left to right, depth first (close to Python's MRO)."""
        stack, seen = [cls], set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            yield current
            stack.extend(reversed(self.bases(current)))

    def own_method(self, cls: Symbol, name: str) -> Symbol | None:
        """A method written in the body of `cls` itself, not inherited."""
        qualname = f"{cls.qualname}.{name}"
        if self.graph.nodes.get(f"{cls.module}:{qualname}", {}).get("kind") == "function":
            return Symbol("function", cls.module, qualname)
        return None

    def find_member(self, cls: Symbol, name: str) -> Symbol | None:
        """A method or nested class of `cls`, also when a parent class defines it."""
        for c in self.mro(cls):
            method = self.own_method(c, name)
            if method is not None:
                return method
            qualname = f"{c.qualname}.{name}"
            if qualname in self.classes[c.module]:
                return Symbol("class", c.module, qualname)
        return None

    def descendants(self, cls: Symbol) -> Iterator[Symbol]:
        """Every subclass of `cls` in this repo: children, grandchildren, and so on."""
        stack, seen = list(self._children.get(cls, [])), {cls}
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            yield current
            stack.extend(self._children.get(current, []))

    def resolve_self(self, cls: Symbol, name: str, is_super: bool) -> list[tuple[str, str]]:
        """self.name() / cls.name() / super().name() called inside class `cls`."""
        targets = []
        for c in self.mro(cls):
            if is_super and c == cls:
                continue                    # super() starts looking at the parents
            method = self.own_method(c, name)
            if method is not None:
                targets.append((method.node, "self" if c == cls else "inherited"))
                break
        if not is_super:
            # self may be a subclass instance, and then the subclass's own version runs instead
            for sub in self.descendants(cls):
                method = self.own_method(sub, name)
                if method is not None:
                    targets.append((method.node, "override"))
        return targets

    # ---- variable types ----------------------------------------------------------------------------
    # Each returns a list of repo classes, or None when the code gives no clue at all.
    # [] means "there is a clue, and it is not a repo class" (db: Session): never guess then.

    @staticmethod
    def _combine(results) -> list[Symbol] | None:
        results = list(results)
        classes = [c for r in results if r for c in r]
        if classes:
            return list(dict.fromkeys(classes))     # keep order, drop repeats
        return [] if any(r is not None for r in results) else None

    def annotation_classes(self, module: str, text: str, fn: FunctionInfo | None = None) -> list[Symbol]:
        found = (self.resolve_dotted(module, name, fn) for name in annotation_names(text))
        return [s for s, _ in filter(None, found) if s.kind == "class"]

    def hint_classes(self, module: str, fn: FunctionInfo | None, hint: ValueHint, hops: int) -> list[Symbol] | None:
        if hint.kind == "annotation":                 # x: User
            return self.annotation_classes(module, hint.text, fn)
        if hint.kind == "name":                       # x = other_variable
            return None if "." in hint.text else self.var_types(module, fn, hint.text, hops + 1)
        found = self.resolve_dotted(module, hint.text, fn)  # x = User() or x = load()
        if found is None:
            first = hint.text.split(".")[0]
            imports = {**self.files[module].imports, **(fn.imports if fn is not None else {})}
            if first in imports and self.canonical(
                    absolute_module(module, self._is_pkg(module), imports[first][0])) is None:
                return []                             # x = requests.get(): third-party value
            return None
        symbol = found[0]
        if symbol.kind == "class":
            return [symbol]
        if symbol.kind == "function":
            info = self.functions.get(symbol.node)
            return self.function_returns(symbol.module, info, hops) if info is not None else None
        return None

    def function_returns(self, module: str, info: FunctionInfo, hops: int) -> list[Symbol] | None:
        """Repo classes a function gives back: from `-> User`, else from its `return` / `yield` lines."""
        declared = self.annotation_classes(module, info.return_type, info) if info.return_type else []
        if declared:
            return declared
        if info.return_hints and hops < MAX_TYPE_HOPS:   # -> Iterator[Session] says little; `yield db` may say more
            found = self._combine(self.hint_classes(module, info, h, hops + 1) for h in info.return_hints)
            if found is not None:
                return found
        return [] if info.return_type else None

    def var_types(self, module: str, fn: FunctionInfo | None, name: str, hops: int = 0) -> list[Symbol] | None:
        """Repo classes that variable `name` can hold inside `fn` (or at module level when fn is None)."""
        if hops > MAX_TYPE_HOPS:
            return None
        if fn is not None:
            if name in fn.params:                     # a parameter hides any outer name
                annotation = fn.params[name]
                if annotation:
                    return self.annotation_classes(module, annotation, fn)
                fixture = self.find_fixture(module, name, self.node_id(module, fn)) if self.uses_fixtures(module, fn) else None
                if fixture is not None:               # def test_x(fake_llm): whatever the fixture gives back
                    return self.function_returns(fixture.split(":")[0], self.functions[fixture], hops + 1)
                return None
            if name in fn.local_types:
                return self._combine(self.hint_classes(module, fn, h, hops) for h in fn.local_types[name])
        var = self.variables.get(module, {}).get(name)
        if var is not None:
            return self._combine(self.hint_classes(module, None, h, hops) for h in var.hints)
        return None

    def attr_types(self, cls: Symbol, attr: str) -> list[Symbol] | None:
        """Repo classes that self.<attr> can hold, from the class body or any `self.attr = ...`."""
        for c in self.mro(cls):                       # the nearest class that sets the attribute wins
            hints = [(None, h) for h in self.classes[c.module][c.qualname].attrs.get(attr, [])]
            for method in self.methods.get(c, []):
                hints += [(method, h) for h in method.attr_assigns.get(attr, [])]
            if hints:                                 # self.db = db reads the method's own parameter db
                return self._combine(self.hint_classes(c.module, fn, h, 0) for fn, h in hints)
        return None

    def typed_targets(self, classes: list[Symbol] | None, name: str) -> list[tuple[str, str]]:
        """Edges for <variable>.name() once we know which classes the variable can hold."""
        if classes is None:
            return self.guess_targets(name)
        best: dict[str, str] = {}                     # node -> label; "typed" beats "override"
        for cls in classes:
            method = self.find_member(cls, name)
            if method is not None and method.kind == "function":
                best[method.node] = "typed"
            for sub in self.descendants(cls):         # the value may be a subclass with its own version
                own = self.own_method(sub, name)
                if own is not None:
                    best.setdefault(own.node, "override")
        return list(best.items())

    def guess_targets(self, name: str) -> list[tuple[str, str]]:
        """No clue about the type: if exactly one repo class has this method, guess it."""
        if not self.guess or name.startswith("__") or name in BUILTIN_METHODS:
            return []
        owners = self.method_owners.get(name, [])
        return [(owners[0], "name-match")] if len(owners) == 1 else []

    # ---- pytest fixtures ---------------------------------------------------------------------------

    def fixture_scopes(self, module: str) -> list[str]:
        """Where pytest looks for a fixture used in `module`: the module, then each conftest.py above it."""
        scopes = [module]
        for folder in PurePosixPath(self.files[module].path).parents:
            conftest = module_name("conftest.py" if str(folder) == "." else f"{folder}/conftest.py")
            if conftest in self.files and conftest != module:
                scopes.append(conftest)
        return scopes

    def find_fixture(self, module: str, name: str, skip: str | None = None) -> str | None:
        """Node id of the fixture `name` as seen from `module`; the closest one wins.

        `skip` lets a fixture that overrides one with the same name use the outer one.
        """
        for scope in self.fixture_scopes(module):
            node = self.fixtures.get(scope, {}).get(name)
            if node is not None and node != skip:
                return node
        return None

    def uses_fixtures(self, module: str, fn: FunctionInfo) -> bool:
        node = self.node_id(module, fn)
        return looks_like_test(self.files[module].path, fn.name) or node in self.fixtures.get(module, {}).values()

    def fixture_targets(self, module: str, fn: FunctionInfo) -> list[str]:
        """Fixtures pytest runs before `fn`: its parameters, usefixtures(...), and autouse fixtures."""
        node = self.node_id(module, fn)
        names = [p for p in fn.params if p not in NOT_FIXTURES]
        for decorator in fn.decorators:
            if USEFIXTURES.match(decorator):
                names += re.findall(r"['\"](\w+)['\"]", decorator)
        targets = [t for t in (self.find_fixture(module, n, node) for n in names) if t is not None]
        if looks_like_test(self.files[module].path, fn.name):
            for scope in self.fixture_scopes(module):
                targets += self.autouse.get(scope, [])
        return [t for t in dict.fromkeys(targets) if t != node]

    # ---- functions passed as values ----------------------------------------------------------------

    def resolve_ref(self, mod: str, caller: FunctionInfo, ref: CallSite) -> list[tuple[str, str]]:
        """to_thread(advise, ...), add_node("x", node_fn), FastAPI(lifespan=lifespan), Thread(target=self.run).

        Only names that turn out to be repo functions or classes count; variables are skipped.
        """
        if ref.receiver in SELF_RECEIVERS:
            if caller.class_qualname is None:
                return []
            method = self.find_member(Symbol("class", mod, caller.class_qualname), ref.name)
            return [(method.node, "reference")] if method is not None and method.kind == "function" else []
        first = ref.name if ref.receiver is None else ref.receiver.split(".")[0]
        if first in caller.params or first in caller.local_types:
            return []                       # a variable, or an attribute of one
        dotted = ref.name if ref.receiver is None else f"{ref.receiver}.{ref.name}"
        found = self.resolve_dotted(mod, dotted, caller)
        if found is None:
            return []
        symbol = found[0]
        if symbol.kind == "function":
            return [(symbol.node, "reference")]
        if symbol.kind == "class":          # to_thread(ShiftRuntime, ...) builds one later
            init = self.find_member(symbol, "__init__")
            targets = [(symbol.node, "reference")]
            if init is not None and init.kind == "function":
                targets.append((init.node, "reference"))
            return targets
        return []

    # ---- edges -------------------------------------------------------------------------------------

    def _add_edge(self, caller: str, callee: str, rule: str, line: int) -> None:
        """Add an edge. If one exists between the same two nodes, keep the more confident label."""
        if caller == callee:
            return
        if self.graph.has_edge(caller, callee):
            old = self.graph.edges[caller, callee]["resolution"]
            if CONFIDENCE.get(old, DEFAULT_CONFIDENCE) >= CONFIDENCE.get(rule, DEFAULT_CONFIDENCE):
                return
        self.graph.add_edge(caller, callee, resolution=rule, line=line)

    def resolve(self, mod: str, is_pkg: bool, parsed: ParsedFile, call: CallSite,
                caller: FunctionInfo | None = None) -> list[tuple[str, str]]:
        """Targets of one call as (node, label). Empty when the call can't be resolved."""
        if call.receiver in SELF_RECEIVERS or call.receiver == "super()":
            if caller is None or caller.class_qualname is None:
                return []                   # `self` outside a class: just a parameter with that name
            cls = Symbol("class", mod, caller.class_qualname)
            return self.resolve_self(cls, call.name, is_super=call.receiver == "super()")
        if call.receiver == EXPR_RECEIVER:
            return []                       # get().send(): needs runtime knowledge

        if call.receiver is not None:
            first, *rest = call.receiver.split(".")
            if first in SELF_RECEIVERS and len(rest) == 1:                 # self.db.query()
                if caller is None or caller.class_qualname is None:
                    return []
                cls = Symbol("class", mod, caller.class_qualname)
                return self.typed_targets(self.attr_types(cls, rest[0]), call.name)
            if caller is not None and (first in caller.params or first in caller.local_types):
                if rest:
                    return []               # user.profile.save(): two steps, not followed
                return self.typed_targets(self.var_types(mod, caller, first), call.name)

        dotted = call.name if call.receiver is None else f"{call.receiver}.{call.name}"
        found = self.resolve_dotted(mod, dotted, caller)
        if found is None:
            if call.receiver in self.variables.get(mod, {}):              # tracker = Tracker() at the top
                return self.typed_targets(self.var_types(mod, None, call.receiver), call.name)
            return []                       # builtins, third-party code
        symbol, label = found
        if symbol.kind == "function":
            return [(symbol.node, label)]
        if symbol.kind == "class":          # Foo() builds the object, so it depends on the class and its __init__
            targets = [(symbol.node, "constructor")]
            init = self.find_member(symbol, "__init__")
            if init is not None and init.kind == "function":
                targets.append((init.node, "constructor"))
            return targets
        return []                           # a module is not callable

    def link(self) -> None:
        for mod, classes in self.classes.items():
            for qualname in classes:
                cls = Symbol("class", mod, qualname)
                for base in self.bases(cls):     # a change to the parent class reaches its children
                    self._add_edge(cls.node, base.node, "inherits", classes[qualname].start_line)
                    self._children.setdefault(base, []).append(cls)

        for mod, parsed in self.files.items():
            is_pkg = parsed.path.endswith("__init__.py")
            for fn in parsed.functions:
                caller = self.node_id(mod, fn)
                for call in fn.calls:
                    targets = self.resolve(mod, is_pkg, parsed, call, fn)
                    if not targets:
                        self.stats["unresolved"] += 1
                        continue
                    self.stats[targets[0][1]] += 1      # count calls, not edges: Foo() can add two
                    for callee, rule in targets:
                        self._add_edge(caller, callee, rule, call.line)
                for ref in fn.refs:
                    for callee, rule in self.resolve_ref(mod, fn, ref):
                        self.ref_stats[rule] += 1
                        self._add_edge(caller, callee, rule, ref.line)
                if self.uses_fixtures(mod, fn):
                    for fixture in self.fixture_targets(mod, fn):
                        self.ref_stats["fixture"] += 1
                        self._add_edge(caller, fixture, "fixture", fn.start_line)


    def build(self) -> nx.DiGraph:
        self.index()
        self.link()
        return self.graph

def main():
    import sys
    r = Resolver(sys.argv[1] if len(sys.argv) > 1 else ".")   # usage: python -m blast_radius.resolver.resolver [repo]
    print(r.build())

if __name__ == "__main__":
    main()
