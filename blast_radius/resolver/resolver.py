import os
from collections import Counter
from pathlib import Path

import networkx as nx

from .parser import FunctionInfo, ParsedFile, PythonParser

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