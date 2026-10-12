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
            "        self.db.query()\n"       # attribute of self: its type is unknown
            "        query.attr()\n"          # attribute of a function
        ),
    })
    r = Resolver(tmp_path, use_cache=False)
    assert set(r.build().edges) == set()


# ---- step 4: self, cls, super() ----------------------------------------------------------------------

def test_self_call_in_same_class(tmp_path):
    write(tmp_path, {
        "m.py": "class User:\n    def save(self):\n        self.validate()\n    def validate(self):\n        pass\n",
    })
    assert labelled_edges(tmp_path) == {("m:User.save", "m:User.validate", "self")}


def test_cls_call_in_classmethod(tmp_path):
    write(tmp_path, {
        "m.py": "class User:\n    @classmethod\n    def load(cls):\n        cls.make()\n"
                "    @classmethod\n    def make(cls):\n        pass\n",
    })
    assert labelled_edges(tmp_path) == {("m:User.load", "m:User.make", "self")}


def test_self_call_to_parent_method_in_other_file(tmp_path):
    write(tmp_path, {
        "base.py": "class User:\n    def validate(self):\n        pass\n",
        "school.py": "from base import User\n\nclass Student(User):\n    def save(self):\n        self.validate()\n",
    })
    assert ("school:Student.save", "base:User.validate", "inherited") in labelled_edges(tmp_path)


def test_super_skips_own_class_and_adds_no_overrides(tmp_path):
    write(tmp_path, {
        "m.py": (
            "class User:\n    def save(self):\n        pass\n\n"
            "class Student(User):\n    def save(self):\n        super().save()\n\n"
            "class Monitor(Student):\n    def save(self):\n        pass\n"
        ),
    })
    calls = {e for e in labelled_edges(tmp_path) if e[2] != "inherits"}
    assert calls == {("m:Student.save", "m:User.save", "inherited")}


def test_override_in_child_class(tmp_path):
    write(tmp_path, {
        "m.py": (
            "class User:\n    def save(self):\n        self.validate()\n    def validate(self):\n        pass\n\n"
            "class Student(User):\n    def validate(self):\n        pass\n\n"
            "class Monitor(Student):\n    def validate(self):\n        pass\n"
        ),
    })
    calls = {e for e in labelled_edges(tmp_path) if e[2] != "inherits"}
    assert calls == {
        ("m:User.save", "m:User.validate", "self"),
        ("m:User.save", "m:Student.validate", "override"),
        ("m:User.save", "m:Monitor.validate", "override"),     # grandchildren count too
    }


def test_method_only_children_define(tmp_path):
    # the "template method" pattern: the parent calls a step that only the children write
    write(tmp_path, {
        "m.py": (
            "class Job:\n    def run(self):\n        self.step()\n\n"
            "class Email(Job):\n    def step(self):\n        pass\n"
        ),
    })
    calls = {e for e in labelled_edges(tmp_path) if e[2] != "inherits"}
    assert calls == {("m:Job.run", "m:Email.step", "override")}


def test_self_in_closure_inside_method(tmp_path):
    write(tmp_path, {
        "m.py": "class A:\n    def m(self):\n        def helper():\n            self.n()\n    def n(self):\n        pass\n",
    })
    assert ("m:A.m.helper", "m:A.n", "self") in labelled_edges(tmp_path)


def test_self_outside_a_class_gives_no_edge(tmp_path):
    write(tmp_path, {"m.py": "def f(self):\n    self.save()\n\ndef save():\n    pass\n"})
    assert labelled_edges(tmp_path) == set()


# ---- issue 11: calls on variables ---------------------------------------------------------------------

MODELS = "class User:\n    def save(self):\n        pass\n\nclass Admin(User):\n    def save(self):\n        pass\n"


def call_edges(root: Path, guess: bool = True) -> set[tuple[str, str, str]]:
    g = Resolver(root, use_cache=False, guess=guess).build()
    return {(a, b, d["resolution"]) for a, b, d in g.edges(data=True)
            if d["resolution"] not in ("constructor", "inherits")}


def test_annotation_names():
    from blast_radius.resolver.resolver import annotation_names
    assert annotation_names("User") == ["User"]
    assert annotation_names("User | None") == ["User"]
    assert annotation_names("Optional[User]") == ["User"]
    assert annotation_names("typing.Optional['models.User']") == ["models.User"]
    assert annotation_names('"User | Admin"') == ["User", "Admin"]
    assert annotation_names("Annotated[User, Depends(x)]") == ["User"]
    assert annotation_names("list[User]") == ["list"]          # the variable is the list, not a User
    assert annotation_names("None") == []


def test_variable_made_from_a_class(tmp_path):
    write(tmp_path, {"models.py": MODELS,
                     "api.py": "from models import User\n\ndef f():\n    u = User()\n    u.save()\n"})
    assert call_edges(tmp_path) == {("api:f", "models:User.save", "typed"),
                                    ("api:f", "models:Admin.save", "override")}


def test_parameter_type_hint(tmp_path):
    write(tmp_path, {"models.py": MODELS,
                     "api.py": "from models import Admin\n\ndef f(a: Admin | None = None):\n    a.save()\n"})
    assert call_edges(tmp_path) == {("api:f", "models:Admin.save", "typed")}


def test_local_type_hint_and_return_type(tmp_path):
    write(tmp_path, {
        "models.py": MODELS + "\ndef load() -> 'Admin':\n    pass\n",
        "api.py": "from models import Admin, load\n\ndef f():\n    a: Admin = get()\n    a.save()\n"
                  "\ndef g():\n    b = load()\n    b.save()\n",
    })
    edges = call_edges(tmp_path)
    assert ("api:f", "models:Admin.save", "typed") in edges
    assert ("api:g", "models:Admin.save", "typed") in edges


def test_self_attribute_from_init_parameter(tmp_path):
    write(tmp_path, {
        "models.py": MODELS,
        "svc.py": "from models import Admin\n\nclass Svc:\n    def __init__(self, a: Admin):\n        self.a = a\n"
                  "    def run(self):\n        self.a.save()\n",
    })
    assert call_edges(tmp_path) == {("svc:Svc.run", "models:Admin.save", "typed")}


def test_self_attribute_from_class_body(tmp_path):
    write(tmp_path, {
        "models.py": MODELS,
        "svc.py": "from models import Admin\n\nclass Svc:\n    a: Admin\n    def run(self):\n        self.a.save()\n",
    })
    assert call_edges(tmp_path) == {("svc:Svc.run", "models:Admin.save", "typed")}


def test_variable_at_top_of_file(tmp_path):
    write(tmp_path, {"models.py": MODELS,
                     "api.py": "from models import Admin\n\nadmin = Admin()\n\ndef f():\n    admin.save()\n"})
    assert call_edges(tmp_path) == {("api:f", "models:Admin.save", "typed")}


def test_variable_copied_from_another(tmp_path):
    write(tmp_path, {"models.py": MODELS,
                     "api.py": "from models import Admin\n\ndef f(a: Admin):\n    b = a\n    b.save()\n"})
    assert call_edges(tmp_path) == {("api:f", "models:Admin.save", "typed")}


def test_third_party_type_gives_no_edge_and_no_guess(tmp_path):
    write(tmp_path, {
        "models.py": "class Store:\n    def commit(self):\n        pass\n",
        "api.py": "from sqlalchemy.orm import Session\nimport requests\n\n"
                  "def f(db: Session):\n    db.commit()\n\ndef g():\n    r = requests.get('x')\n    r.commit()\n",
    })
    assert call_edges(tmp_path) == set()


def test_name_match_guess_and_switch(tmp_path):
    write(tmp_path, {
        "models.py": "class Report:\n    def publish(self):\n        pass\n",
        "api.py": "def f(report):\n    report.publish()\n",
    })
    assert call_edges(tmp_path) == {("api:f", "models:Report.publish", "name-match")}
    assert call_edges(tmp_path, guess=False) == set()


def test_no_guess_when_several_classes_or_builtin_name(tmp_path):
    write(tmp_path, {
        "models.py": MODELS + "\nclass Cache:\n    def get(self):\n        pass\n",
        "api.py": "def f(x):\n    x.save()\n    x.get()\n",     # save: two classes. get: dict has it too
    })
    assert call_edges(tmp_path) == set()


def test_local_variable_hides_module_with_same_name(tmp_path):
    write(tmp_path, {
        "models.py": MODELS,
        "db.py": "def save():\n    pass\n",
        "api.py": "import db\nfrom models import Admin\n\ndef f():\n    db = Admin()\n    db.save()\n",
    })
    assert call_edges(tmp_path) == {("api:f", "models:Admin.save", "typed")}
