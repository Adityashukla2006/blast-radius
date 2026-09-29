import re
import subprocess
from collections import defaultdict
from pathlib import Path

import networkx as nx


def git_diff(repo: str | Path, base: str | None = None) -> str:
    """base=None: uncommitted changes vs HEAD. base='main': this branch vs main."""
    args = ["git", "-C", str(repo), "diff", "-U0", "--no-color", "--no-ext-diff"]
    args.append(f"{base}...HEAD" if base else "HEAD")
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(diff_text: str) -> dict[str, set[int]]:
    """{file: {changed line numbers in the new version}}"""
    result: dict[str, set[int]] = defaultdict(set)
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            # Skip deleted files (/dev/null) and non-Python files
            current = path[2:] if path.startswith("b/") and path.endswith(".py") else None
            continue
        if current is None:
            continue
        m = HUNK.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count == 0:
                result[current].add(max(start, 1))    # pure deletion: mark where it happened
            else:
                result[current].update(range(start, start + count))
    return dict(result)


def changed_functions(graph: nx.DiGraph, lines_by_file: dict[str, set[int]]) -> set[str]:
    """Map changed lines to the innermost function that contains each one."""
    # Group node spans by file once, so each lookup only scans that file's functions
    spans: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for node, data in graph.nodes(data=True):
        spans[data["file"]].append((data["start"], data["end"], node))

    changed: set[str] = set()
    for file, lines in lines_by_file.items():
        for line in lines:
            covering = [(end - start, node) for start, end, node in spans.get(file, []) if start <= line <= end]
            if covering:
                changed.add(min(covering)[1])    # smallest span = innermost function
    return changed