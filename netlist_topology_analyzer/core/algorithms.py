"""
Graph algorithms used by the analyzers.

Everything here is generic graph theory operating on an adjacency mapping::

    adj: Dict[str, Set[str]]

Nodes are opaque strings; nothing in this module knows what a net or a component
is. Keeping the algorithms free of domain concepts means they can be unit-tested
against textbook examples with known answers, which is exactly what ``tests/``
does.

Implementation notes
--------------------
* **No third-party dependencies.** KiCad ships its own bundled Python
  interpreter, and installing packages such as ``networkx`` into it is awkward
  and platform-specific. Everything below is therefore standard library only,
  so the plugin works on a stock KiCad install.
* **Depth-first search is implemented iteratively.** A recursive Tarjan would
  raise ``RecursionError`` on a large board, because CPython's default recursion
  limit (1000) is far below the number of nodes on a real design.
* Neighbour iteration is sorted so that results are deterministic across runs,
  which matters for reproducible reports and stable tests.
"""

from __future__ import annotations

from collections import deque
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

Adjacency = Dict[str, Set[str]]


# ---------------------------------------------------------------------------
# Union-Find (disjoint set union)
# ---------------------------------------------------------------------------


class UnionFind(object):
    """Disjoint-set forest with union by rank and path compression.

    Near-linear overall: ``O(n * alpha(n))`` where ``alpha`` is the inverse
    Ackermann function (effectively constant for any realistic board).

    We use union-find rather than a BFS flood fill for connected components
    because the natural input is a *stream of edges* (each net links the pads
    on it), and union-find consumes edges incrementally without first
    materialising an adjacency structure.
    """

    __slots__ = ("_parent", "_rank", "_count")

    def __init__(self, items=()):
        # type: (Iterable[str]) -> None
        self._parent = {}  # type: Dict[str, str]
        self._rank = {}  # type: Dict[str, int]
        self._count = 0
        for item in items:
            self.add(item)

    def add(self, item):
        # type: (str) -> None
        if item not in self._parent:
            self._parent[item] = item
            self._rank[item] = 0
            self._count += 1

    def find(self, item):
        # type: (str) -> str
        """Return the representative of ``item``'s set, compressing the path."""
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        # Second pass: point every node on the path straight at the root.
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a, b):
        # type: (str, str) -> bool
        """Merge the sets containing ``a`` and ``b``.

        Returns True if a merge actually happened (i.e. they were separate).
        """
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1
        self._count -= 1
        return True

    def connected(self, a, b):
        # type: (str, str) -> bool
        return self.find(a) == self.find(b)

    @property
    def group_count(self):
        # type: () -> int
        """Number of disjoint sets currently present."""
        return self._count

    def groups(self):
        # type: () -> List[List[str]]
        """All sets, each sorted, ordered by descending size then name."""
        buckets = {}  # type: Dict[str, List[str]]
        for item in self._parent:
            buckets.setdefault(self.find(item), []).append(item)
        result = [sorted(v) for v in buckets.values()]
        result.sort(key=lambda g: (-len(g), g[0] if g else ""))
        return result


# ---------------------------------------------------------------------------
# Basic graph utilities
# ---------------------------------------------------------------------------


def build_adjacency(nodes, edges):
    # type: (Iterable[str], Iterable[Tuple[str, str]]) -> Adjacency
    """Build an undirected adjacency map, ignoring self-loops."""
    adj = dict((n, set()) for n in nodes)  # type: Adjacency
    for a, b in edges:
        if a == b:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return adj


def connected_components(adj):
    # type: (Adjacency) -> List[List[str]]
    """Connected components, largest first; each component sorted.

    Isolated nodes appear as single-element components, which is what we want:
    an unconnected part is a legitimate finding, not something to drop.
    """
    uf = UnionFind(adj.keys())
    for node, neighbours in adj.items():
        for other in neighbours:
            uf.union(node, other)
    return uf.groups()


def degrees(adj):
    # type: (Adjacency) -> Dict[str, int]
    return dict((node, len(neigh)) for node, neigh in adj.items())


def bfs_distances(adj, source):
    # type: (Adjacency, str) -> Dict[str, int]
    """Hop counts from ``source`` to every reachable node. ``O(V + E)``."""
    if source not in adj:
        return {}
    dist = {source: 0}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for other in adj[node]:
            if other not in dist:
                dist[other] = dist[node] + 1
                queue.append(other)
    return dist


def shortest_path(adj, source, target):
    # type: (Adjacency, str, str) -> Optional[List[str]]
    """A shortest node path from ``source`` to ``target``, or None."""
    if source not in adj or target not in adj:
        return None
    if source == target:
        return [source]
    prev = {source: None}  # type: Dict[str, Optional[str]]
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for other in sorted(adj[node]):
            if other in prev:
                continue
            prev[other] = node
            if other == target:
                path = [target]
                while prev[path[-1]] is not None:
                    path.append(prev[path[-1]])
                path.reverse()
                return path
            queue.append(other)
    return None


def eccentricity_and_diameter(adj, nodes=None):
    # type: (Adjacency, Optional[Sequence[str]]) -> Tuple[Dict[str, int], int]
    """Per-node eccentricity and the graph diameter within one component.

    Cost is ``O(V * (V + E))`` because it runs a BFS from every node, so callers
    should restrict ``nodes`` to a single (bounded) component.
    """
    targets = list(nodes) if nodes is not None else list(adj.keys())
    ecc = {}  # type: Dict[str, int]
    diameter = 0
    for node in targets:
        dist = bfs_distances(adj, node)
        if not dist:
            continue
        far = max(dist.values())
        ecc[node] = far
        if far > diameter:
            diameter = far
    return ecc, diameter


# ---------------------------------------------------------------------------
# Tarjan lowlink: articulation points and bridges
# ---------------------------------------------------------------------------


class _LowlinkResult(object):
    """Discovery/lowlink bookkeeping from one iterative DFS sweep."""

    __slots__ = ("disc", "low", "tree_edges", "roots", "root_children")

    def __init__(self):
        self.disc = {}  # type: Dict[str, int]
        self.low = {}  # type: Dict[str, int]
        self.tree_edges = []  # type: List[Tuple[str, str]]
        self.roots = []  # type: List[str]
        self.root_children = {}  # type: Dict[str, int]


def _tarjan_lowlink(adj):
    # type: (Adjacency) -> _LowlinkResult
    """One iterative DFS computing discovery times and lowlink values.

    Both articulation points and bridges are derivable from the same numbers,
    so we traverse once and let the callers apply their own predicate. The DFS
    is explicit-stack based to avoid Python recursion limits.
    """
    res = _LowlinkResult()
    timer = 0

    for root in sorted(adj.keys()):
        if root in res.disc:
            continue
        res.roots.append(root)
        res.root_children[root] = 0
        res.disc[root] = res.low[root] = timer
        timer += 1
        # Stack frames: (node, parent, iterator over sorted neighbours)
        stack = [(root, None, iter(sorted(adj[root])))]

        while stack:
            node, parent, neighbours = stack[-1]
            descended = False

            for other in neighbours:
                if other == node:
                    continue  # ignore self-loops
                if other not in res.disc:
                    # Tree edge: descend.
                    res.disc[other] = res.low[other] = timer
                    timer += 1
                    res.tree_edges.append((node, other))
                    if node == root:
                        res.root_children[root] += 1
                    stack.append((other, node, iter(sorted(adj.get(other, ())))))
                    descended = True
                    break
                if other != parent and res.disc[other] < res.low[node]:
                    # Back edge to an ancestor: pull the lowlink down.
                    res.low[node] = res.disc[other]

            if descended:
                continue

            # Finished this node: propagate its lowlink to its parent.
            stack.pop()
            if stack:
                parent_node = stack[-1][0]
                if res.low[node] < res.low[parent_node]:
                    res.low[parent_node] = res.low[node]

    return res


def articulation_points(adj):
    # type: (Adjacency) -> Set[str]
    """Cut vertices: nodes whose removal increases the component count.

    Runs in ``O(V + E)``.

    In circuit terms an articulation point is a *single point of failure*: every
    signal path between the two halves of the circuit it separates must pass
    through that one part.
    """
    res = _tarjan_lowlink(adj)
    cuts = set()  # type: Set[str]

    for parent, child in res.tree_edges:
        # A non-root parent is a cut vertex when some child's subtree has no
        # back edge climbing above the parent.
        if parent in res.root_children:
            continue  # roots use the child-count rule below
        if res.low[child] >= res.disc[parent]:
            cuts.add(parent)

    # A DFS root is a cut vertex exactly when it has more than one DFS child.
    for root, children in res.root_children.items():
        if children > 1:
            cuts.add(root)

    return cuts


def bridges(adj):
    # type: (Adjacency) -> List[Tuple[str, str]]
    """Cut edges: edges whose removal increases the component count.

    Runs in ``O(V + E)``. Each bridge is returned once, as a sorted tuple.

    In circuit terms a bridge is a connection with no redundant path around it.
    """
    res = _tarjan_lowlink(adj)
    found = []
    for parent, child in res.tree_edges:
        if res.low[child] > res.disc[parent]:
            found.append(tuple(sorted((parent, child))))
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Betweenness centrality (Brandes)
# ---------------------------------------------------------------------------


def betweenness_centrality(adj, normalized=True):
    # type: (Adjacency, bool) -> Dict[str, float]
    """Brandes' betweenness centrality for an unweighted undirected graph.

    Runs in ``O(V * E)`` time and ``O(V + E)`` space.

    Betweenness counts the fraction of shortest paths passing through each node.
    Applied to a signal-topology graph it highlights the parts that most traffic
    must flow through - typically the MCU, a bus buffer, or a level shifter -
    which is a useful, purely structural notion of "importance" that needs no
    simulation.
    """
    centrality = dict((node, 0.0) for node in adj)

    for source in adj:
        # --- single-source shortest-path accumulation (BFS) ---
        stack = []  # nodes in non-decreasing distance order
        predecessors = dict((node, []) for node in adj)  # type: Dict[str, List[str]]
        sigma = dict((node, 0.0) for node in adj)  # number of shortest paths
        distance = dict((node, -1) for node in adj)
        sigma[source] = 1.0
        distance[source] = 0
        queue = deque([source])

        while queue:
            node = queue.popleft()
            stack.append(node)
            for other in adj[node]:
                if distance[other] < 0:
                    distance[other] = distance[node] + 1
                    queue.append(other)
                if distance[other] == distance[node] + 1:
                    sigma[other] += sigma[node]
                    predecessors[other].append(node)

        # --- back-propagate dependencies ---
        delta = dict((node, 0.0) for node in adj)
        while stack:
            node = stack.pop()
            for pred in predecessors[node]:
                if sigma[node] > 0.0:
                    delta[pred] += (sigma[pred] / sigma[node]) * (1.0 + delta[node])
            if node != source:
                centrality[node] += delta[node]

    # Each undirected pair is counted from both endpoints.
    for node in centrality:
        centrality[node] /= 2.0

    if normalized:
        n = len(adj)
        if n > 2:
            scale = 2.0 / ((n - 1) * (n - 2))
            for node in centrality:
                centrality[node] *= scale

    return centrality


def top_n(scores, count, minimum=None):
    # type: (Dict[str, float], int, Optional[float]) -> List[Tuple[str, float]]
    """Highest-scoring ``count`` entries, ties broken by name for determinism."""
    items = [(k, v) for k, v in scores.items() if minimum is None or v > minimum]
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    return items[:count]
