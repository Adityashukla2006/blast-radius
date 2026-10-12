import networkx as nx
import pytest

from blast_radius.impact import impact
from blast_radius.impact.impact import blast_radius


def make_graph(edges: list[tuple[str, str, str]]) -> nx.DiGraph:
    """edges are (caller, callee, rule); node names get a "m:" module prefix like real node ids."""
    g = nx.DiGraph()
    for caller, callee, rule in edges:
        g.add_edge(f"m:{caller}", f"m:{callee}", resolution=rule)
    for node in g:
        g.nodes[node]["file"] = "m.py"
    return g


def by_name(results) -> dict:
    return {r.node[2:]: r for r in results}


def test_score_multiplies_confidence_and_decay_per_edge():
    g = make_graph([("A", "C", "local"), ("B", "A", "import")])
    res = by_name(blast_radius(g, {"m:C"}))
    assert res["A"].score == 0.72 and res["A"].depth == 1
    assert res["B"].score == 0.5184 and res["B"].depth == 2
    assert res["B"].path == ["m:C", "m:A", "m:B"]


def test_changed_function_is_not_reported():
    g = make_graph([("A", "C", "local")])
    assert "C" not in by_name(blast_radius(g, {"m:C"}))


def test_results_come_out_highest_score_first():
    g = make_graph([("A", "C", "local"), ("B", "C", "weak"), ("D", "A", "local")])
    scores = [r.score for r in blast_radius(g, {"m:C"})]
    assert scores == sorted(scores, reverse=True)


def test_best_scored_path_wins_over_shorter_path(monkeypatch):
    # Path A: C <- A1 <- A2 <- A3 <- A4 <- X  (5 strong edges)
    # Path B: C <- B1 <- X                    (2 weak edges)
    monkeypatch.setitem(impact.CONFIDENCE, "weak", 0.1)
    chain = ["C", "A1", "A2", "A3", "A4", "X", "Y", "Z"]
    edges = [(caller, callee, "local") for callee, caller in zip(chain, chain[1:])]
    edges += [("B1", "C", "weak"), ("X", "B1", "weak")]
    res = by_name(blast_radius(make_graph(edges), {"m:C"}))

    assert res["X"].path == ["m:C", "m:A1", "m:A2", "m:A3", "m:A4", "m:X"]
    # Z sits 7 hops out along strong edges; a depth limit of 6 used to drop that path
    assert res["Z"].depth == 7 and res["Z"].score == pytest.approx(0.72 ** 7, abs=1e-4)


def test_min_score_stops_long_chains():
    chain = [f"F{i}" for i in range(12)]
    edges = [(caller, callee, "local") for callee, caller in zip(chain, chain[1:])]
    res = by_name(blast_radius(make_graph(edges), {"m:F0"}, min_score=0.05))
    assert "F9" in res         # 0.72 ** 9 ~= 0.052
    assert "F10" not in res    # 0.72 ** 10 ~= 0.037


def test_cycles_terminate():
    g = make_graph([("A", "C", "local"), ("C", "A", "local"), ("B", "A", "local")])
    assert set(by_name(blast_radius(g, {"m:C"}))) == {"A", "B"}


def test_test_functions_are_flagged():
    g = nx.DiGraph()
    g.add_node("pkg.db:run", file="pkg/db.py")
    g.add_node("tests.test_db:test_run", file="tests/test_db.py")
    g.add_node("tests.helpers:make_db", file="tests/helpers.py")
    g.add_edge("tests.test_db:test_run", "pkg.db:run", resolution="import")
    g.add_edge("tests.helpers:make_db", "pkg.db:run", resolution="import")
    flags = {r.node: r.is_test for r in blast_radius(g, {"pkg.db:run"})}
    assert flags == {"tests.test_db:test_run": True, "tests.helpers:make_db": False}
