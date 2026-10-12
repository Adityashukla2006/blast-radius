from ..diff.diff import changed_functions, changed_lines, git_diff
from ..impact.impact import MIN_SCORE, blast_radius
from ..resolver.resolver import Resolver

class Analyze:
    def __init__(self):
        pass
    @staticmethod
    def analyze(repo, base=None, min_score=MIN_SCORE, use_cache=True):
        graph = Resolver(repo, use_cache).build()
        changed = changed_functions(graph, changed_lines(git_diff(repo, base)))
        return changed, blast_radius(graph, changed, min_score)
