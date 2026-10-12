import os
import subprocess
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from ..cache.cache import FALLBACK_DIR, ParseCache
from ..parser.parser import EXPR_RECEIVER, CallSite, ClassInfo, Expression, FunctionInfo, ParsedFile, PythonParser

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "build", "dist", "node_modules", FALLBACK_DIR}
MAX_HOPS = 20          # import hops find_symbol may follow before giving up
NOT_RESOLVED_HERE = {"self", "cls", "super()", EXPR_RECEIVER}   # self/cls/super() come in step 4

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
    def __init__(self, repo : str | Path, use_cache: bool = True):
        self.repo = Path(repo)
        self.cache = ParseCache(self.repo) if use_cache else None
        self.files: dict[str, ParsedFile] = {}
        self.top_level: dict[str, dict[str, FunctionInfo]] = {}
        self.classes: dict[str, dict[str, ClassInfo]] = {}   # module -> class qualname -> class, nested too
        self.graph = nx.DiGraph()
        self.stats: Counter = Counter()
        self.variables: dict[str, dict[str, Expression]] = {}
        self.aliases: dict[str, str] = {}     # import name -> module name, e.g. "app.db" -> "backend.app.db"
        self.ambiguous: set[str] = set()      # import names that more than one file claims: never used
        self._bases: dict[Symbol, list[Symbol]] = {}

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
            src, symbol = parsed.imports[name]
            src_mod = self.canonical(absolute_module(module, self._is_pkg(module), src))
            if src_mod is None:
                return None                 # third-party or standard library
            if symbol is None:              # import x / import a.b as c: the name is a module
                return Symbol("module", src_mod)
            return self.find_symbol(src_mod, symbol, seen)
        for star in parsed.star_imports:
            src_mod = self.canonical(absolute_module(module, self._is_pkg(module), star))
            found = self.find_symbol(src_mod, name, seen) if src_mod else None
            if found is not None:
                return found
        return None

    def resolve_dotted(self, module: str, dotted: str) -> tuple[Symbol, str] | None:
        """Resolve a name as written in `module`, like "query", "db.query" or "User.create".

        Returns the symbol and the edge label: local, import, module or class.
        """
        first, *rest = dotted.split(".")
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

    def find_member(self, cls: Symbol, name: str) -> Symbol | None:
        """A method or nested class of `cls`, also when a parent class defines it."""
        for c in self.mro(cls):
            qualname = f"{c.qualname}.{name}"
            if self.graph.nodes.get(f"{c.module}:{qualname}", {}).get("kind") == "function":
                return Symbol("function", c.module, qualname)
            if qualname in self.classes[c.module]:
                return Symbol("class", c.module, qualname)
        return None

    # ---- edges -------------------------------------------------------------------------------------

    def resolve(self, mod: str, is_pkg: bool, parsed: ParsedFile, call: CallSite) -> list[tuple[str, str]]:
        """Targets of one call as (node, label). Empty when the call can't be resolved."""
        if call.receiver in NOT_RESOLVED_HERE:
            return []
        dotted = call.name if call.receiver is None else f"{call.receiver}.{call.name}"
        found = self.resolve_dotted(mod, dotted)
        if found is None:
            return []                       # builtins, third-party code, local variables
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
                    self.graph.add_edge(cls.node, base.node, resolution="inherits",
                                        line=classes[qualname].start_line)

        for mod, parsed in self.files.items():
            is_pkg = parsed.path.endswith("__init__.py")
            for fn in parsed.functions:
                caller = self.node_id(mod, fn)
                for call in fn.calls:
                    targets = self.resolve(mod, is_pkg, parsed, call)
                    if not targets:
                        self.stats["unresolved"] += 1
                        continue
                    self.stats[targets[0][1]] += 1      # count calls, not edges: Foo() can add two
                    for callee, rule in targets:
                        self.graph.add_edge(caller, callee, resolution=rule, line=call.line)


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
