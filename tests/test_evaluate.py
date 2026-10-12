import sqlite3
import sys
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import evaluate as ev  # noqa: E402

from blast_radius.resolver.resolver import Resolver  # noqa: E402


def test_numbits_to_lines():
    # byte 0 bit 1 -> line 1, byte 0 bit 3 -> line 3, byte 1 bit 0 -> line 8
    assert ev.numbits_to_lines(bytes([0b00001010, 0b00000001])) == [1, 3, 8]


def test_test_node_from_pytest_id():
    assert ev.test_node("tests/unit/test_x.py::test_a", "backend") == "backend.tests.unit.test_x:test_a"
    assert ev.test_node("tests/test_x.py::TestA::test_b[1-2]", ".") == "tests.test_x:TestA.test_b"


def test_passing_tests_reads_junit(tmp_path):
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "test_x.py").write_text(
        "class TestA:\n    def test_b(self):\n        pass\n\ndef test_c():\n    pass\n\ndef test_d():\n    pass\n")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_x.TestA" name="test_b[1]"/>'
        '<testcase classname="tests.test_x" name="test_c"/>'
        '<testcase classname="tests.test_x" name="test_d"><failure/></testcase>'
        '</testsuite></testsuites>')
    resolver = Resolver(tmp_path, use_cache=False)
    resolver.build()
    assert ev.passing_tests(junit, resolver, "backend") == {
        "backend.tests.test_x:TestA.test_b", "backend.tests.test_x:test_c"}


def test_covered_lines_reads_coverage_contexts(tmp_path):
    db = sqlite3.connect(tmp_path / ".coverage")
    db.executescript(
        "create table file (id integer primary key, path text);"
        "create table context (id integer primary key, context text);"
        "create table line_bits (file_id integer, context_id integer, numbits blob);"
        "insert into file values (1, '/repo/app.py');"
        "insert into context values (1, ''), (2, 'tests/test_x.py::test_a|run'), (3, 'tests/test_x.py::test_a|setup');")
    db.executemany("insert into line_bits values (?, ?, ?)",
                   [(1, 1, bytes([0b10])), (1, 2, bytes([0b100])), (1, 3, bytes([0b1000]))])
    db.commit()
    db.close()
    lines = ev.covered_lines(tmp_path / ".coverage")
    # line 1 ran only at import time (empty context), so no test owns it
    assert {line: tests for line, tests in lines["/repo/app.py"].items()} == {
        2: {"tests/test_x.py::test_a"}, 3: {"tests/test_x.py::test_a"}}


def test_evaluate_scores_recall_and_precision():
    g = nx.DiGraph()
    for node, file in [("app:f", "app.py"), ("tests.t:test_a", "tests/t.py"),
                       ("tests.t:test_b", "tests/t.py"), ("tests.t:test_c", "tests/t.py")]:
        g.add_node(node, file=file, kind="function")
    g.add_edge("tests.t:test_a", "app:f", resolution="import")
    g.add_edge("tests.t:test_c", "app:f", resolution="import")
    # really used by a and b; we pick a and c
    passed = {"tests.t:test_a", "tests.t:test_b", "tests.t:test_c"}
    result = ev.evaluate(g, {"app:f": {"tests.t:test_a", "tests.t:test_b"}}, passed, min_score=0.05)
    assert result["functions"] == 1
    assert result["recall"] == 0.5
    assert result["precision"] == 0.5
    assert result["rows"][0]["missed"] == ["tests.t:test_b"]


def test_evaluate_ignores_picks_that_did_not_pass():
    g = nx.DiGraph()
    for node, file in [("app:f", "app.py"), ("tests.t:test_a", "tests/t.py"), ("tests.t:test_db", "tests/t.py")]:
        g.add_node(node, file=file, kind="function")
    g.add_edge("tests.t:test_a", "app:f", resolution="import")
    g.add_edge("tests.t:test_db", "app:f", resolution="import")     # needs a database, errored
    result = ev.evaluate(g, {"app:f": {"tests.t:test_a"}}, {"tests.t:test_a"}, min_score=0.05)
    assert result["precision"] == 1.0
