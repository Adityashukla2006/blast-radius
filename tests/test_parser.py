from blast_radius.parser.parser import EXPR_RECEIVER, PythonParser


def parse(source: str):
    return PythonParser().parse_file("m.py", source.encode())


def calls_of(parsed, qualname: str) -> list[tuple[str, str | None]]:
    fn = next(f for f in parsed.functions if f.qualname == qualname)
    return [(c.name, c.receiver) for c in fn.calls]


def test_receivers():
    parsed = parse(
        "def f(self):\n"
        "    query()\n"
        "    self.save()\n"
        "    db.query()\n"
        "    app.db.query()\n"
        "    super().save()\n"
        "    get_client().send()\n"
        "    items[0].save()\n"
        "    get().db.query()\n"
    )
    assert calls_of(parsed, "f") == [
        ("query", None),
        ("save", "self"),
        ("query", "db"),
        ("query", "app.db"),
        ("save", "super()"),
        ("super", None),             # the inner call is a call of its own
        ("send", EXPR_RECEIVER),     # was None before, so it looked like a bare call to send()
        ("get_client", None),
        ("save", EXPR_RECEIVER),
        ("query", EXPR_RECEIVER),
        ("get", None),
    ]


def test_classes_and_bases():
    parsed = parse(
        "import models\n"
        "class Base:\n"
        "    pass\n"
        "\n"
        "@dataclass\n"
        "class User(Base, models.Model, Generic[T], metaclass=Meta):\n"
        "    class Meta:\n"
        "        pass\n"
    )
    classes = {c.qualname: c for c in parsed.classes}
    assert set(classes) == {"Base", "User", "User.Meta"}
    assert classes["User"].bases == ["Base", "models.Model", "Generic"]
    assert classes["Base"].bases == []
    assert (classes["User"].start_line, classes["User"].end_line) == (5, 8)   # includes the decorator


def test_class_qualname_of_functions():
    parsed = parse(
        "def top():\n"
        "    def inner():\n"
        "        pass\n"
        "\n"
        "class A:\n"
        "    def m(self):\n"
        "        def helper():\n"
        "            self.x()\n"
        "    class B:\n"
        "        def n(self):\n"
        "            pass\n"
    )
    owner = {f.qualname: f.class_qualname for f in parsed.functions}
    assert owner == {
        "top": None,
        "top.inner": None,
        "A.m": "A",
        "A.m.helper": "A",           # a closure inside a method still sees the method's self
        "A.B.n": "A.B",
    }


def test_star_imports_are_kept_apart():
    parsed = parse(
        "from .core import *\n"
        "from pkg.utils import *\n"
        "from .db import query as q\n"
    )
    assert parsed.star_imports == [".core", "pkg.utils"]
    assert parsed.imports == {"q": (".db", "query")}
