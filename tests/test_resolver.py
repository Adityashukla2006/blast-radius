from pathlib import Path

from blast_radius.resolver.resolver import Resolver, import_name


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def edges(root: Path) -> set[tuple[str, str]]:
    return set(Resolver(root, use_cache=False).build().edges)


def test_import_name_starts_at_the_top_package():
    files = {"backend/app/__init__.py", "backend/app/db.py", "backend/app/agents/__init__.py",
             "backend/app/agents/coach.py", "backend/tests/test_db.py", "setup.py"}
    assert import_name("backend/app/db.py", files) == "app.db"
    assert import_name("backend/app/agents/coach.py", files) == "app.agents.coach"
    assert import_name("backend/app/__init__.py", files) == "app"
    assert import_name("backend/tests/test_db.py", files) == "test_db"   # not in a package
    assert import_name("setup.py", files) == "setup"


def test_subfolder_project_resolves_absolute_imports(tmp_path):
    # the cat-hackathon layout: the code runs from inside backend/
    write(tmp_path, {
        "backend/app/__init__.py": "",
        "backend/app/db.py": "def query():\n    pass\n",
        "backend/app/service.py": "from app.db import query\n\ndef get():\n    return query()\n",
        "backend/tests/test_service.py": "from app.service import get\n\ndef test_get():\n    get()\n",
    })
    assert edges(tmp_path) == {
        ("backend.app.service:get", "backend.app.db:query"),
        ("backend.tests.test_service:test_get", "backend.app.service:get"),
    }


def test_src_layout_resolves(tmp_path):
    write(tmp_path, {
        "src/mypkg/__init__.py": "",
        "src/mypkg/db.py": "def query():\n    pass\n",
        "src/mypkg/api.py": "from mypkg.db import query\n\ndef get():\n    return query()\n",
    })
    assert edges(tmp_path) == {("src.mypkg.api:get", "src.mypkg.db:query")}


def test_relative_imports_still_resolve(tmp_path):
    write(tmp_path, {
        "backend/app/__init__.py": "",
        "backend/app/db.py": "def query():\n    pass\n",
        "backend/app/service.py": "from .db import query\n\ndef get():\n    return query()\n",
    })
    assert edges(tmp_path) == {("backend.app.service:get", "backend.app.db:query")}


def test_package_at_repo_root_needs_no_alias(tmp_path):
    write(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/db.py": "def query():\n    pass\n",
        "pkg/api.py": "from pkg.db import query\n\ndef get():\n    return query()\n",
    })
    r = Resolver(tmp_path, use_cache=False)
    assert set(r.build().edges) == {("pkg.api:get", "pkg.db:query")}
    assert r.aliases == {}


def test_ambiguous_import_name_gives_no_edge(tmp_path):
    # two projects in one repo both have app/db.py: we can't tell which one `app.db` means
    write(tmp_path, {
        "backend/app/__init__.py": "",
        "backend/app/db.py": "def query():\n    pass\n",
        "worker/app/__init__.py": "",
        "worker/app/db.py": "def query():\n    pass\n",
        "worker/app/jobs.py": "from app.db import query\n\ndef run():\n    return query()\n",
    })
    r = Resolver(tmp_path, use_cache=False)
    assert set(r.build().edges) == set()
    assert "app.db" in r.ambiguous


def test_third_party_import_stays_unresolved(tmp_path):
    write(tmp_path, {
        "backend/app/__init__.py": "",
        "backend/app/service.py": "from json import dumps\n\ndef get():\n    return dumps({})\n",
    })
    r = Resolver(tmp_path, use_cache=False)
    assert set(r.build().edges) == set()
    assert r.stats["unresolved"] == 1
