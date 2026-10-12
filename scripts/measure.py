import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo root, so this runs without installing

from blast_radius.analyze.analyze import Analyze
from blast_radius.parser.parser import EXPR_RECEIVER
from blast_radius.resolver.resolver import Resolver

repo = sys.argv[1] if len(sys.argv) > 1 else "."   # usage: python scripts/measure.py [repo]
if not (Path(repo) / ".git").exists():
    sys.exit(f"{repo} is not a git repo checkout (clone it first, e.g. into bench/targets/)")

# 1. How well does the resolver cover real code?
r = Resolver(repo)
g = r.build()
total = sum(r.stats.values())
print(f"Graph: {g.number_of_nodes()} functions, {g.number_of_edges()} edges")
print(f"Parse cache: {r.cache.hits} reused, {r.cache.misses} parsed  ({r.cache.path})")
for rule, n in r.stats.most_common():
    print(f"  {rule:12} {n:6}  ({n / total:.0%})")

# 2. What KIND of calls are unresolved?
kinds = Counter()
for mod, parsed in r.files.items():
    is_pkg = parsed.path.endswith("__init__.py")
    for fn in parsed.functions:
        for call in fn.calls:
            if not r.resolve(mod, is_pkg, parsed, call, fn):
                if call.receiver is None:
                    kinds["bare name (builtins, third-party)"] += 1
                elif call.receiver in ("self", "cls", "super()"):
                    kinds["self/cls/super().method()"] += 1
                elif call.receiver == EXPR_RECEIVER:
                    kinds["get().method() (needs runtime)"] += 1
                else:
                    kinds["x.method() (local variable, third-party)"] += 1
print("\nUnresolved by kind:")
for kind, n in kinds.most_common():
    print(f"  {n:6}  {kind}")

# 3. What does the tool say about the current change?
changed, impacted = Analyze.analyze(repo)
tests = [i for i in impacted if i.is_test]
print(f"\nChanged: {sorted(changed)}")
print(f"Impacted: {len(impacted)} functions, {len(tests)} tests")
for i in impacted[:15]:
    print(f"  {i.score:.2f}  depth {i.depth}  {i.node}{'  [test]' if i.is_test else ''}")