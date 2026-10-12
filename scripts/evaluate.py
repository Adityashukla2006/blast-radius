"""How accurate is blast-radius? Compare its test picks with the tests that really use each function.

Ground truth comes from ONE run of the target's test suite under coverage, with one coverage
"context" per test. For every function we then know which tests ran its body. Those are the tests
that can break when the function changes. Breaking each function and re-running the suite would
give the same answer, but needs one full test run per function.

For every function that some passing test runs:
    actual    = tests that ran the function's body
    predicted = tests that blast_radius() reports for a change to that function
    recall    = how many of the actual tests we found      (missed tests = broken code that ships)
    precision = how many of our picks were really needed   (extra tests = wasted CI time)

usage:
    python scripts/evaluate.py bench/targets/cat-hackathon --tests-dir backend \\
        --python bench/targets/cat-hackathon/backend/.venv/Scripts/python.exe

The target environment needs pytest and pytest-cov. The test run is saved in bench/results/<repo>/
and reused; pass --rerun after the target's code or tests change.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo root, so this runs without installing

import networkx as nx
from tree_sitter import Parser

from blast_radius.impact.impact import MIN_SCORE, blast_radius
from blast_radius.parser.parser import PY_LANGUAGE, _span
from blast_radius.resolver.resolver import Resolver, module_name

RESULTS = Path(__file__).resolve().parents[1] / "bench" / "results"


# ---- 1. run the target's tests once, under coverage -------------------------------------------------

def run_tests(python: str, tests_dir: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    cmd = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
           "--cov=.", "--cov-context=test", "--cov-report=", f"--junitxml={out / 'junit.xml'}"]
    print(f"Running the test suite once under coverage (cwd={tests_dir}) ...", flush=True)
    env = {**os.environ, "COVERAGE_FILE": str(out / ".coverage")}
    subprocess.run(cmd, cwd=tests_dir, env=env, check=False)   # failing tests are fine: they are dropped


def passing_tests(junit: Path, resolver: Resolver, tests_dir_rel: str) -> set[str]:
    """Graph nodes of the tests that passed, e.g. "backend.tests.unit.test_x:TestA.test_b"."""
    prefix = module_name(f"{tests_dir_rel}/x.py")[:-1] if tests_dir_rel not in ("", ".") else ""
    passed = set()
    for case in ET.parse(junit).iter("testcase"):
        if any(child.tag in ("failure", "error", "skipped") for child in case):
            continue
        # junit gives classname "tests.unit.test_x.TestA": the longest prefix that is a module is the file
        parts = case.get("classname", "").split(".")
        for k in range(len(parts), 0, -1):
            module = prefix + ".".join(parts[:k])
            if module in resolver.files:
                qualname = ".".join([*parts[k:], case.get("name", "").split("[")[0]])
                passed.add(f"{module}:{qualname}")
                break
    return passed


def numbits_to_lines(numbits: bytes) -> list[int]:
    """coverage.py stores the line set as a bitmap: bit i of byte j means line j * 8 + i."""
    return [j * 8 + i for j, byte in enumerate(numbits) for i in range(8) if byte & (1 << i)]


def covered_lines(coverage_file: Path) -> dict[str, dict[int, set[str]]]:
    """{absolute file path: {line: {pytest ids of the tests that ran it}}}"""
    with closing(sqlite3.connect(coverage_file)) as db:
        files = dict(db.execute("select id, path from file").fetchall())
        contexts = dict(db.execute("select id, context from context").fetchall())
        rows = db.execute("select file_id, context_id, numbits from line_bits").fetchall()
    result: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for file_id, context_id, numbits in rows:
        test = contexts[context_id].rsplit("|", 1)[0]     # "tests/x.py::test_a|setup" -> the test id
        if not test:
            continue                                      # import time, outside any test
        for line in numbits_to_lines(numbits):
            result[files[file_id]][line].add(test)
    return result


# ---- 2. turn the coverage into "function -> tests that use it" -----------------------------------------

def body_starts(path: Path) -> dict[int, int]:
    """{first line of a function (decorators included): first line of its body}.

    The `def` line and default values run when the module is imported, not when the function is
    called, so only body lines count as "this test used the function".
    """
    root = Parser(PY_LANGUAGE).parse(path.read_bytes()).root_node
    starts, stack = {}, [root]
    while stack:
        node = stack.pop()
        if node.type == "function_definition":
            starts[_span(node).start_point.row + 1] = node.child_by_field_name("body").start_point.row + 1
        stack.extend(node.children)
    return starts


def test_node(pytest_id: str, tests_dir_rel: str) -> str:
    """"tests/unit/test_x.py::TestA::test_b[1]" -> "backend.tests.unit.test_x:TestA.test_b" """
    file, *names = pytest_id.split("::")
    rel = f"{tests_dir_rel}/{file}" if tests_dir_rel not in ("", ".") else file
    return f"{module_name(rel)}:{'.'.join(n.split('[')[0] for n in names)}"


def ground_truth(resolver: Resolver, graph, tests_dir: Path, out: Path) -> dict[str, set[str]]:
    repo = resolver.repo.resolve()
    tests_dir_rel = tests_dir.resolve().relative_to(repo).as_posix()
    passed = passing_tests(out / "junit.xml", resolver, tests_dir_rel)
    lines = covered_lines(out / ".coverage")

    # functions per file, with the range of their body
    spans: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for node, data in graph.nodes(data=True):
        if data["kind"] == "function":
            spans[data["file"]].append((data["start"], data["end"], node))

    actual: dict[str, set[str]] = defaultdict(set)
    unknown_tests = set()
    for abs_path, by_line in lines.items():
        try:
            rel = Path(abs_path).resolve().relative_to(repo).as_posix()
        except ValueError:
            continue                                      # outside the repo (site-packages)
        if rel not in spans:
            continue
        starts = body_starts(repo / rel)
        body = [(starts.get(s, s), e, n) for s, e, n in spans[rel]]
        for line, tests in by_line.items():
            covering = [(e - s, n) for s, e, n in body if s <= line <= e]
            if not covering:
                continue
            fn = min(covering)[1]                         # innermost function, like diff.changed_functions
            for t in tests:
                node = test_node(t, tests_dir_rel)
                if node not in graph:
                    unknown_tests.add(t)
                elif node in passed:                      # a failing test can't tell us anything
                    actual[fn].add(node)
    print(f"  {len(passed)} passing tests; ground truth for {len(actual)} functions")
    if unknown_tests:
        print(f"  note: {len(unknown_tests)} test ids did not match a graph node, e.g. {sorted(unknown_tests)[:3]}")
    return actual


# ---- 3. compare -----------------------------------------------------------------------------------

def is_test_node(graph, node: str) -> bool:
    return "tests" in Path(graph.nodes[node]["file"]).parts or Path(graph.nodes[node]["file"]).name.startswith("test_")


def evaluate(graph, actual: dict[str, set[str]], min_score: float) -> dict:
    rows = []
    for fn, real in actual.items():
        if is_test_node(graph, fn):
            continue                                      # we score changes to app code, not to tests
        picked = {i.node for i in blast_radius(graph, {fn}, min_score) if i.is_test}
        hit = picked & real
        rows.append({
            "function": fn,
            "actual": len(real),
            "predicted": len(picked),
            "hit": len(hit),
            "recall": len(hit) / len(real),
            "precision": len(hit) / len(picked) if picked else None,
            "missed": sorted(real - picked)[:5],
            "picked": sorted(picked),
        })
    n = len(rows)
    with_picks = [r for r in rows if r["precision"] is not None]
    return {
        "functions": n,
        "recall": sum(r["recall"] for r in rows) / n,
        "precision": sum(r["precision"] for r in with_picks) / len(with_picks) if with_picks else 0.0,
        "all_found": sum(r["recall"] == 1.0 for r in rows) / n,
        "none_found": sum(r["hit"] == 0 for r in rows) / n,
        "avg_actual": sum(r["actual"] for r in rows) / n,
        "avg_predicted": sum(r["predicted"] for r in rows) / n,
        "rows": rows,
    }


def break_points(graph, actual: dict[str, set[str]], rows: list[dict]) -> Counter:
    """For every missed (function, test) pair: the highest function on the real call chain that the
    graph still reaches. Its caller is the missing link. The most frequent ones are the best next fixes."""
    ran = defaultdict(set)                                # test -> functions it ran
    for fn, tests in actual.items():
        for t in tests:
            ran[t].add(fn)
    blocks = Counter()
    for r in rows:
        fn = r["function"]
        reached = nx.ancestors(graph, fn) | {fn}
        for t in actual[fn] - set(r["picked"]):
            on_chain = reached & ran[t]
            blocks.update(n for n in on_chain if not any(p in ran[t] for p in graph.predecessors(n)))
    return blocks


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo")
    ap.add_argument("--tests-dir", default=".", help="folder to run pytest from, relative to the repo")
    ap.add_argument("--python", default=sys.executable, help="python of the target's environment")
    ap.add_argument("--rerun", action="store_true", help="run the test suite again instead of reusing it")
    ap.add_argument("--min-score", type=float, default=MIN_SCORE)
    ap.add_argument("--show", type=int, default=10, help="how many of the worst misses to list")
    args = ap.parse_args()

    repo = Path(args.repo)
    tests_dir = repo / args.tests_dir
    out = RESULTS / repo.resolve().name
    if args.rerun or not (out / ".coverage").exists():
        run_tests(str(Path(args.python).resolve()), tests_dir, out)

    results, actual = {}, None
    for guess in (False, True):
        resolver = Resolver(repo, guess=guess)
        graph = resolver.build()
        if actual is None:                                # the guess adds edges, never nodes: reuse it
            actual = ground_truth(resolver, graph, tests_dir, out)
        results["guess on" if guess else "guess off"] = evaluate(graph, actual, args.min_score)

    print(f"\nFunctions scored: {results['guess on']['functions']} (app functions that at least one passing test runs)")
    print(f"{'':12} {'recall':>7} {'precision':>10} {'all found':>10} {'none found':>11} {'tests: real':>12} {'picked':>7}")
    for name, r in results.items():
        print(f"{name:12} {r['recall']:7.1%} {r['precision']:10.1%} {r['all_found']:10.1%} {r['none_found']:11.1%}"
              f" {r['avg_actual']:12.1f} {r['avg_predicted']:7.1f}")

    worst = sorted(results["guess on"]["rows"], key=lambda r: (r["recall"], -r["actual"]))[: args.show]
    print("\nWorst misses (guess on):")
    for r in worst:
        print(f"  recall {r['recall']:4.0%}  found {r['hit']}/{r['actual']}  {r['function']}")
        for m in r["missed"][:2]:
            print(f"        missed {m}")

    print("\nWhere the call chains break most often (guess on). The caller of each is not linked:")
    for node, count in break_points(graph, actual, results["guess on"]["rows"]).most_common(args.show):
        print(f"  {count:5} missed tests  {node}")

    summary = {k: {m: v for m, v in r.items() if m != "rows"} for k, r in results.items()}
    (out / "eval.json").write_text(json.dumps({"summary": summary, "rows": results["guess on"]["rows"]}, indent=1))
    print(f"\nSaved {out / 'eval.json'}")


if __name__ == "__main__":
    main()
