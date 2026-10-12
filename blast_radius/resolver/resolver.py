import os
import subprocess
from collections import Counter
from pathlib import Path

import networkx as nx

from ..cache.cache import FALLBACK_DIR, ParseCache
from ..parser.parser import Expression, FunctionInfo, ParsedFile, PythonParser

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "build", "dist", "node_modules", FALLBACK_DIR}

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

class Resolver:
    def __init__(self, repo : str | Path, use_cache: bool = True):
        self.repo = Path(repo)
        self.cache = ParseCache(self.repo) if use_cache else None
        self.files: dict[str, ParsedFile] = {}
        self.top_level: dict[str, dict[str, FunctionInfo]] = {}
        self.graph = nx.DiGraph()
        self.stats: Counter = Counter()
        self.variables: dict[str, dict[str, Expression]] = {}
        self.aliases: dict[str, str] = {}     # import name -> module name, e.g. "app.db" -> "backend.app.db"
        self.ambiguous: set[str] = set()      # import names that more than one file claims: never used

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
            for fn in parsed.functions:
                if "." not in fn.qualname:          # only top-level functions are importable
                    self.top_level[mod][fn.name] = fn
                self.graph.add_node(self.node_id(mod, fn), file=rel, start=fn.start_line, end=fn.end_line,
                                    kind="function")
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

    def resolve(self, mod: str, is_pkg: bool, parsed: ParsedFile, call) -> tuple[str, str] | None:
        if call.receiver is not None:
            return None   # attribute calls come in later rules

        # Rule 1: defined at top level in this same module
        local = self.top_level[mod].get(call.name)
        if local is not None:
            return self.node_id(mod, local), "local"

        # Rule 2: imported by name: from .db import query [as q]
        if call.name in parsed.imports:
            src, symbol = parsed.imports[call.name]
            if symbol is not None:   # None means "import x" (a module), not a function
                src_mod = self.canonical(absolute_module(mod, is_pkg, src))
                fn = self.top_level.get(src_mod, {}).get(symbol) if src_mod else None
                if fn is not None:
                    return self.node_id(src_mod, fn), "import"

        return None   # builtins, third-party code, classes, dynamic calls

    def link(self) -> None:
        for mod, parsed in self.files.items():
            is_pkg = parsed.path.endswith("__init__.py")
            for fn in parsed.functions:
                caller = self.node_id(mod, fn)
                for call in fn.calls:
                    target = self.resolve(mod, is_pkg, parsed, call)
                    if target is None:
                        self.stats["unresolved"] += 1
                        continue
                    callee, rule = target
                    self.stats[rule] += 1
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