"""pytest plugin that evaluate.py loads into the TARGET's test run (pytest -p blast_radius_eval_plugin).

A shared fixture (scope session, module, package or class) runs once, during the first test that needs
it, so plain per-test coverage gives its lines to that one test. But when the fixture's code breaks,
every test that uses the fixture fails. So while a shared fixture sets up, this plugin switches the
coverage context to "fixture::<where>::<name>", and it writes down which tests use which shared
fixture. evaluate.py then credits the fixture's lines to all of those tests.
"""
import json
import os

import pytest

FIXTURE_CONTEXT = "fixture::{}::{}"


def _coverage():
    try:
        import coverage
    except ImportError:
        return None
    return coverage.Coverage.current()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    cov = _coverage()
    item = getattr(request, "_pyfuncitem", None)
    if cov is None or item is None or fixturedef.scope == "function":
        yield
        return
    cov.switch_context(FIXTURE_CONTEXT.format(fixturedef.baseid, fixturedef.argname))
    try:
        yield
    finally:
        cov.switch_context(f"{item.nodeid}|setup")   # back to what pytest-cov had set


_uses: dict[str, list[str]] = {}


def pytest_collection_modifyitems(items):
    for item in items:
        info = getattr(item, "_fixtureinfo", None)
        if info is None:
            continue
        shared = []
        for name in item.fixturenames:                 # includes fixtures that fixtures use
            defs = info.name2fixturedefs.get(name)
            if defs and defs[-1].scope != "function":  # [-1] is the definition this test really gets
                shared.append(FIXTURE_CONTEXT.format(defs[-1].baseid, name))
        _uses[item.nodeid] = shared


def pytest_sessionfinish(session):
    path = os.environ.get("BLAST_RADIUS_FIXTURES")
    if path:
        with open(path, "w") as f:
            json.dump(_uses, f)
