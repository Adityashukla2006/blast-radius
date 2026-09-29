import heapq
from dataclasses import dataclass
from pathlib import PurePosixPath

import networkx as nx


@dataclass
class Impacted:
    node: str          # "pkg.service:get_user"
    depth: int         # hops away from the change: 1 = direct caller
    score: float       # 0 to 1: how likely it is to actually be affected
    path: list[str]    # changed function -> ... -> this node
    is_test: bool      # tests are what we'll actually run


CONFIDENCE = {"local": 0.9, "import": 0.9}   # add a line here for each new resolver rule
DEFAULT_CONFIDENCE = 0.5
DECAY = 0.8

def is_test(graph: nx.DiGraph, node: str) -> bool:
    file = PurePosixPath(graph.nodes[node]["file"])
    func = node.split(":", 1)[1].split(".")[-1]      # "tests.test_api:TestX.test_get" -> "test_get"
    in_test_file = file.name.startswith("test_") or file.name.endswith("_test.py") or "tests" in file.parts
    return in_test_file and func.startswith("test")


def blast_radius(graph: nx.DiGraph, changed: set[str], max_depth: int = 6) -> list[Impacted]:
    # heapq is a min-heap, so store -score to pop the HIGHEST score first
    heap = [(-1.0, 0, node, [node]) for node in changed if node in graph]
    heapq.heapify(heap)
    reported: set[str] = set()
    expanded_at: dict[str, int] = {}   # shallowest depth each node was expanded from
    results: list[Impacted] = []

    while heap:
        neg_score, depth, node, path = heapq.heappop(heap)
        if expanded_at.get(node, max_depth + 1) <= depth:
            continue                   # already expanded from here or shallower: nothing new to reach
        expanded_at[node] = depth
        score = -neg_score
        if node not in reported:       # first pop = best score, so report that path
            reported.add(node)
            if depth > 0:              # depth 0 = the changed function itself, not an "impact"
                results.append(Impacted(node, depth, round(score, 4), path, is_test(graph, node)))
        # a worse-scored but shallower path still gets expanded, so max_depth doesn't hide callers
        if depth == max_depth:
            continue
        for caller in graph.predecessors(node):
            if expanded_at.get(caller, max_depth + 1) <= depth + 1:
                continue
            rule = graph.edges[caller, node].get("resolution")
            new_score = score * CONFIDENCE.get(rule, DEFAULT_CONFIDENCE) * DECAY
            heapq.heappush(heap, (-new_score, depth + 1, caller, [*path, caller]))
    return results  