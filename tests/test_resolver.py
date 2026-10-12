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


# ---- step 3: modules, classes, constructors, re-exports -----------------------------------------------

def labelled_edges(root: Path) -> set[tuple[str, str, str]]:
    g = Resolver(root, use_cache=False).build()
    return {(a, b, d["resolution"]) for a, b, d in g.edges(data=True)}


def test_module_attribute_calls(tmp_path):
    write(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/db.py": "def query():\n    pass\n",
        "pkg/a.py": "import pkg.db\n\ndef f():\n    pkg.db.query()\n",
        "pkg/b.py": "from pkg import db\n\ndef f():\n    db.query()\n",
        "pkg/c.py": "from . import db\n\ndef f():\n    db.query()\n",
        "pkg/d.py": "import pkg.db as store\n\ndef f():\n    store.query()\n",
    })
    assert labelled_edges(tmp_path) == {
        (f"pkg.{m}:f", "pkg.db:query", "module") for m in "abcd"
    }


def test_module_attribute_call_through_alias(tmp_path):
    write(tmp_path, {
        "backend/app/__init__.py": "",
        "backend/app/db.py": "def query():\n    pass\n",
        "backend/app/service.py": "from app import db\n\ndef get():\n    db.query()\n",
    })
    assert labelled_edges(tmp_path) == {("backend.app.service:get", "backend.app.db:query", "module")}


def test_class_method_call(tmp_path):
    write(tmp_path, {
        "models.py": "class User:\n    def create():\n        pass\n",
        "api.py": "from models import User\n\ndef signup():\n    User.create()\n",
    })
    assert labelled_edges(tmp_path) == {("api:signup", "models:User.create", "class")}


def test_constructor_links_class_and_init(tmp_path):
    write(tmp_path, {
        "models.py": "class User:\n    def __init__(self):\n        pass\n",
        "api.py": "from models import User\n\ndef signup():\n    User()\n",
    })
    assert labelled_edges(tmp_path) == {
        ("api:signup", "models:User", "constructor"),
        ("api:signup", "models:User.__init__", "constructor"),
    }


def test_class_without_init_links_only_the_class(tmp_path):
    # Pydantic models and dataclasses: the fields are the constructor
    write(tmp_path, {
        "schemas.py": "class Shift(BaseModel):\n    operator_id: str\n",
        "api.py": "from schemas import Shift\n\ndef make():\n    Shift(operator_id='a')\n",
    })
    assert labelled_edges(tmp_path) == {("api:make", "schemas:Shift", "constructor")}


def test_inherited_init_and_inherits_edge(tmp_path):
    write(tmp_path, {
        "base.py": "class User:\n    def __init__(self, name):\n        pass\n",
        "school.py": "from base import User\n\nclass Student(User):\n    def study(self):\n        pass\n",
        "api.py": "from school import Student\n\ndef enroll():\n    Student('Ana')\n",
    })
    assert labelled_edges(tmp_path) == {
        ("school:Student", "base:User", "inherits"),
        ("api:enroll", "school:Student", "constructor"),
        ("api:enroll", "base:User.__init__", "constructor"),
    }


def test_class_method_found_in_parent_class(tmp_path):
    write(tmp_path, {
        "m.py": "class A:\n    def make():\n        pass\n\nclass B(A):\n    pass\n\ndef f():\n    B.make()\n",
    })
    assert ("m:f", "m:A.make", "class") in labelled_edges(tmp_path)


def test_reexport_through_init(tmp_path):
    write(tmp_path, {
        "pkg/__init__.py": "from .core import run\n",
        "pkg/core.py": "def run():\n    pass\n",
        "api.py": "from pkg import run\n\ndef f():\n    run()\n",
    })
    assert labelled_edges(tmp_path) == {("api:f", "pkg.core:run", "import")}


def test_reexport_through_star_import(tmp_path):
    write(tmp_path, {
        "pkg/__init__.py": "from .core import *\n",
        "pkg/core.py": "def run():\n    pass\n",
        "api.py": "import pkg\n\ndef f():\n    pkg.run()\n",
    })
    assert labelled_edges(tmp_path) == {("api:f", "pkg.core:run", "module")}


def test_import_cycle_stops(tmp_path):
    write(tmp_path, {
        "a.py": "from b import thing\n",
        "b.py": "from a import thing\n",
        "c.py": "from a import thing\n\ndef f():\n    thing()\n",
    })
    r = Resolver(tmp_path, use_cache=False)
    assert set(r.build().edges) == set()
    assert r.stats["unresolved"] == 1


def test_changed_class_body_line_maps_to_class(tmp_path):
    from blast_radius.diff.diff import changed_functions
    write(tmp_path, {
        "schemas.py": "class Shift:\n    operator_id: str\n\n    def check(self):\n        pass\n",
    })
    g = Resolver(tmp_path, use_cache=False).build()
    assert changed_functions(g, {"schemas.py": {2}}) == {"schemas:Shift"}
    assert changed_functions(g, {"schemas.py": {5}}) == {"schemas:Shift.check"}


def test_calls_that_stay_unresolved(tmp_path):
    write(tmp_path, {
        "m.py": (
            "import json\n"
            "def query():\n    pass\n"
            "class A:\n"
            "    def m(self):\n"
            "        json.dumps({})\n"        # third-party module
            "        user = load()\n"
            "        user.save()\n"           # local variable
            "        get().query()\n"         # needs runtime knowledge
            "        self.m()\n"              # self: step 4
            "        query.attr()\n"          # attribute of a function
        ),
    })
    r = Resolver(tmp_path, use_cache=False)
    assert set(r.build().edges) == set()
