import os
import shutil
import subprocess

import pytest

from blast_radius.cache import cache as cache_module
from blast_radius.cache.cache import ParseCache
from blast_radius.parser.parser import PythonParser
from blast_radius.resolver.resolver import Resolver, python_files


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "db.py").write_text("def query():\n    return run()\n\ndef run():\n    return 1\n")
    (tmp_path / "pkg" / "service.py").write_text("from .db import query\n\ndef get():\n    return query()\n")
    return tmp_path


def run_once(repo, path) -> ParseCache:
    cache = ParseCache(repo, path)
    parser = PythonParser()
    for rel in ["pkg/__init__.py", "pkg/db.py", "pkg/service.py"]:
        if (repo / rel).exists():
            cache.get(rel, parser)
    cache.save()
    return cache


def test_second_run_parses_nothing(repo, tmp_path):
    path = tmp_path / "cache.pickle"
    first = run_once(repo, path)
    assert (first.hits, first.misses) == (0, 3)
    second = run_once(repo, path)
    assert (second.hits, second.misses) == (3, 0)


def test_changed_file_is_parsed_again(repo, tmp_path):
    path = tmp_path / "cache.pickle"
    run_once(repo, path)
    (repo / "pkg" / "db.py").write_text("def query():\n    return 2\n")
    cache = ParseCache(repo, path)
    parsed = cache.get("pkg/db.py", PythonParser())
    assert cache.misses == 1
    assert [f.name for f in parsed.functions] == ["query"]


def test_new_mtime_with_same_content_is_reused(repo, tmp_path):
    path = tmp_path / "cache.pickle"
    run_once(repo, path)
    st = (repo / "pkg" / "db.py").stat()
    os.utime(repo / "pkg" / "db.py", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    cache = run_once(repo, path)
    assert (cache.hits, cache.misses) == (3, 0)
    assert cache.dirty      # the new mtime is saved, so the next run takes the fast path again


def test_deleted_file_drops_out_of_cache(repo, tmp_path):
    path = tmp_path / "cache.pickle"
    run_once(repo, path)
    (repo / "pkg" / "service.py").unlink()
    run_once(repo, path)
    assert set(ParseCache(repo, path).old) == {"pkg/__init__.py", "pkg/db.py"}


def test_parser_change_invalidates_cache(repo, tmp_path, monkeypatch):
    path = tmp_path / "cache.pickle"
    run_once(repo, path)
    monkeypatch.setattr(cache_module, "PARSER_VERSION", "different")
    assert ParseCache(repo, path).old == {}


def test_corrupt_cache_file_is_ignored(repo, tmp_path):
    path = tmp_path / "cache.pickle"
    path.write_bytes(b"not a pickle")
    assert ParseCache(repo, path).old == {}


def test_resolver_graph_is_the_same_with_and_without_cache(repo):
    fresh = Resolver(repo, use_cache=False).build()
    Resolver(repo).build()                  # fills the cache
    warm = Resolver(repo)
    cached = warm.build()
    assert warm.cache.misses == 0
    assert set(cached.nodes) == set(fresh.nodes)
    assert set(cached.edges) == set(fresh.edges) == {
        ("pkg.db:query", "pkg.db:run"), ("pkg.service:get", "pkg.db:query"),
    }


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_python_files_respects_gitignore(repo):
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("ignored/\n")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "clone.py").write_text("def x():\n    pass\n")
    assert sorted(python_files(repo)) == ["pkg/__init__.py", "pkg/db.py", "pkg/service.py"]
