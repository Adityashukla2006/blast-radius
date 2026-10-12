import heapq
from dataclasses import dataclass
from pathlib import PurePosixPath

import networkx as nx


@dataclass
class Impacted:
    node: str          # "pkg.service:get_user"
    depth: int         # hops away from the change along the best-scored path: 1 = direct caller
    score: float       # 0 to 1: how likely it is to actually be affected
    path: list[str]    # changed function -> ... -> this node
    is_test: bool      # tests are what we'll actually run


# Keep CONFIDENCE * DECAY <= 1 for every rule: the search relies on scores never growing along a path
CONFIDENCE = {   # add a line here for each new resolver rule
    "local": 0.9,         # query(), defined in the same file
    "import": 0.9,        # query(), imported
    "module": 0.9,        # db.query()
    "class": 0.9,         # User.create()
    "constructor": 0.9,   # User() -> the class and its __init__
    "inherits": 0.9,      # class Student(User): Student -> User
    "self": 0.9,          # self.validate(), written in the same class
    "inherited": 0.8,     # self.validate() or super().save(), written in a parent class
    "override": 0.5,      # self.validate() runs a child's version only when self is that child
}
DEFAULT_CONFIDENCE = 0.5
DECAY = 0.8
MIN_SCORE = 0.05   # 0.72 ** 9 ~= 0.052, so a chain of strong edges stops after about 9 hops

def is_test(graph: nx.DiGraph, node: str) -> bool:
    file = PurePosixPath(graph.nodes[node]["file"])
    func = node.split(":", 1)[1].split(".")[-1]      # "tests.test_api:TestX.test_get" -> "test_get"
    in_test_file = file.name.startswith("test_") or file.name.endswith("_test.py") or "tests" in file.parts
    return in_test_file and func.startswith("test")


def blast_radius(graph: nx.DiGraph, changed: set[str], min_score: float = MIN_SCORE) -> list[Impacted]:
    # heapq is a min-heap, so store -score to pop the HIGHEST score first
    heap = [(-1.0, 0, node, [node]) for node in changed if node in graph]
    heapq.heapify(heap)
    done: set[str] = set()
    results: list[Impacted] = []

    while heap:
        neg_score, depth, node, path = heapq.heappop(heap)
        if node in done:
            continue                   # scores only shrink along a path, so the first pop was the best
        done.add(node)
        score = -neg_score
        if depth > 0:                  # depth 0 = the changed function itself, not an "impact"
            results.append(Impacted(node, depth, round(score, 4), path, is_test(graph, node)))
        for caller in graph.predecessors(node):
            if caller in done:
                continue
            rule = graph.edges[caller, node].get("resolution")
            new_score = score * CONFIDENCE.get(rule, DEFAULT_CONFIDENCE) * DECAY
            if new_score >= min_score:
                heapq.heappush(heap, (-new_score, depth + 1, caller, [*path, caller]))
    return results    # already ordered from highest to lowest score
