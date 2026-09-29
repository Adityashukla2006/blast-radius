import heapq
from dataclasses import dataclass
from pathlib import PurePosixPath

import networkx as nx


@dataclass
class Impacted:
    node: str          # "pkg.service:get_user"
    depth: int         # hops away from the change: 1 = direct caller
    score: float       # 0 to 1: how likely it is to actually be affected
    path: list[str]    # changed function -> ... -> this node
    is_test: bool      # tests are what we'll actually run