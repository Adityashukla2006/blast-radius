from ..diff.diff import changed_functions, changed_lines, git_diff
from ..impact.impact import blast_radius
from ..resolver.resolver import Resolver

class Analyze:
    def __init__(self):
        pass 
    @staticmethod
    def analyze(repo, base=None, max_depth=6):
        graph = Resolver(repo).build()
        changed = changed_functions(graph, changed_lines(git_diff(repo, base)))
        return changed, blast_radius(graph, changed, max_depth)
