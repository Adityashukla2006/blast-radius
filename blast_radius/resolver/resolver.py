import os
from collections import Counter
from pathlib import Path

import networkx as nx

from ..parser.parser import FunctionInfo, ParsedFile, PythonParser

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "build", "dist", "node_modules"}

def module_name(rel_path : str) -> str:
    parts = Path(rel_path).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)

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
    def __init__(self, repo : str | Path):
        self.repo = Path(repo)
        self.files: dict[str, ParsedFile] = {}
        self.top_level: dict[str, dict[str, FunctionInfo]] = {}
        self.graph = nx.DiGraph()
        self.stats: Counter = Counter()

    @staticmethod
    def node_id(module: str, fn: FunctionInfo) -> str:
        return f"{module}:{fn.qualname}"

    def index(self) -> None:
        parser = PythonParser()
        for root, dirs, names in os.walk(self.repo):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]   # prune in place so os.walk skips them
            for name in names:
                if not name.endswith(".py"):
                    continue
                rel = (Path(root) / name).relative_to(self.repo).as_posix()
                mod = module_name(rel)
                parsed = parser.parse_file(rel, (self.repo / rel).read_bytes())

                self.files[mod] = parsed
                self.top_level[mod] = {f.name: f for f in parsed.functions if "." not in f.qualname}
                for fn in parsed.functions:
                    self.graph.add_node(self.node_id(mod, fn), file=rel, start=fn.start_line, end=fn.end_line)

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
                src_mod = absolute_module(mod, is_pkg, src)
                fn = self.top_level.get(src_mod, {}).get(symbol)
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