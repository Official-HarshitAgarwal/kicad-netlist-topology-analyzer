"""
Graph construction: turning a netlist into graphs that algorithms can chew on.

A netlist is naturally a **hypergraph** - a net is a single hyperedge joining an
arbitrary number of pads, not a point-to-point link. Standard graph algorithms
need ordinary graphs, so we build two complementary views:

1. **Bipartite incidence graph** (:meth:`NetlistGraph.bipartite`)
   Two node kinds - components and nets - with an edge whenever a component has
   a pad on a net. This is the *lossless* encoding of the hypergraph: no
   connectivity information is invented or destroyed. It is what we use for
   whole-board connectivity questions ("is the board one electrical island?").

2. **Component projection** (:meth:`NetlistGraph.component_graph`)
   Components only, joined when they share a net. This is *lossy* - a 5-pad net
   becomes a 5-clique, so "who is adjacent to whom" survives but "which net
   joined them" does not. It is the right view for structural questions about
   parts ("which part is a single point of failure?").

Two modelling decisions carry real electronics reasoning:

* **Power and ground rails are excluded from the projection.** Every part on a
  board touches GND, so leaving rails in makes the projection nearly complete:
  every part becomes adjacent to every other, there are no articulation points,
  and centrality is uniform. All structural information about *signal* flow is
  destroyed. Removing rails is what makes the topology analysis meaningful.

* **Very high-fanout nets are skipped in the projection.** A net with N pads
  contributes N*(N-1)/2 edges, so a wide bus or a big shield net would dominate
  both runtime and structure. The threshold is configurable.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

from .algorithms import Adjacency, build_adjacency
from .config import AnalysisConfig
from .model import BoardData, Component, Net, NetRole

#: Node-ID prefixes for the bipartite graph. Using a prefixed string (rather
#: than a tuple) keeps nodes hashable, printable and easy to debug in reports.
COMPONENT_PREFIX = "C:"
NET_PREFIX = "N:"


def component_node(reference):
    # type: (str) -> str
    return COMPONENT_PREFIX + reference


def net_node(net_name):
    # type: (str) -> str
    return NET_PREFIX + net_name


def is_component_node(node):
    # type: (str) -> bool
    return node.startswith(COMPONENT_PREFIX)


def is_net_node(node):
    # type: (str) -> bool
    return node.startswith(NET_PREFIX)


def node_label(node):
    # type: (str) -> str
    """Strip the type prefix for display purposes."""
    if is_component_node(node):
        return node[len(COMPONENT_PREFIX):]
    if is_net_node(node):
        return node[len(NET_PREFIX):]
    return node


class NetlistGraph(object):
    """Graph views over a :class:`~.model.BoardData` snapshot.

    Both graphs are built lazily and cached, because a given analysis run may
    only need one of them and the projection is the more expensive to build.
    """

    def __init__(self, board, config=None):
        # type: (BoardData, Optional[AnalysisConfig]) -> None
        self.board = board
        self.config = config or AnalysisConfig()
        self._bipartite = None  # type: Optional[Adjacency]
        self._component_graph = None  # type: Optional[Adjacency]
        self._projection_nets = None  # type: Optional[List[Net]]
        self._shared_nets = None  # type: Optional[Dict[Tuple[str, str], List[str]]]

        # Parts that are electrically irrelevant are filtered out once, here, so
        # that every downstream view agrees on which components exist.
        self.components = [c for c in board.components if self._include_component(c)]
        self.component_refs = set(c.reference for c in self.components)

    # -- filtering --------------------------------------------------------

    def _include_component(self, component):
        # type: (Component) -> bool
        """Decide whether a component participates in the analysis.

        Mechanical items (mounting holes, fiducials, logos) carry no
        connectivity meaning. DNP / BOM-excluded parts are not fitted on the
        physical board, so including them would model a circuit that does not
        exist.
        """
        if self.config.is_mechanical(component.prefix):
            return False
        if self.config.ignore_dnp and (component.dnp or component.excluded_from_bom):
            return False
        return True

    def _include_net_in_projection(self, net):
        # type: (Net) -> bool
        """Whether a net should contribute edges to the component projection."""
        if self.config.exclude_rails_from_projection and net.is_rail:
            return False
        if net.role == NetRole.UNCONNECTED:
            return False
        # A net touching fewer than two *included* components links nothing.
        refs = net.references() & self.component_refs
        if len(refs) < 2:
            return False
        if net.fanout > self.config.projection_max_net_fanout:
            return False
        return True

    # -- graph views ------------------------------------------------------

    def bipartite(self):
        # type: () -> Adjacency
        """Component/net incidence graph - the lossless connectivity view."""
        if self._bipartite is not None:
            return self._bipartite

        nodes = [component_node(c.reference) for c in self.components]
        edges = []
        for net in self.board.nets:
            if net.role == NetRole.UNCONNECTED:
                continue
            refs = net.references() & self.component_refs
            if not refs:
                continue
            nodes.append(net_node(net.name))
            for ref in refs:
                edges.append((component_node(ref), net_node(net.name)))

        self._bipartite = build_adjacency(nodes, edges)
        return self._bipartite

    def projection_nets(self):
        # type: () -> List[Net]
        """Nets that contribute edges to the component projection."""
        if self._projection_nets is None:
            self._projection_nets = [
                n for n in self.board.nets if self._include_net_in_projection(n)
            ]
        return self._projection_nets

    def component_graph(self):
        # type: () -> Adjacency
        """Signal-topology graph over components (rails excluded)."""
        if self._component_graph is not None:
            return self._component_graph

        nodes = [component_node(c.reference) for c in self.components]
        edges = []
        shared = {}  # type: Dict[Tuple[str, str], List[str]]

        for net in self.projection_nets():
            refs = sorted(net.references() & self.component_refs)
            # Clique expansion of the hyperedge.
            for i in range(len(refs)):
                for j in range(i + 1, len(refs)):
                    a, b = refs[i], refs[j]
                    edges.append((component_node(a), component_node(b)))
                    shared.setdefault((a, b), []).append(net.name)

        self._component_graph = build_adjacency(nodes, edges)
        self._shared_nets = shared
        return self._component_graph

    def shared_nets(self, ref_a, ref_b):
        # type: (str, str) -> List[str]
        """Names of the projected nets that join two components."""
        if self._shared_nets is None:
            self.component_graph()
        key = tuple(sorted((ref_a, ref_b)))
        return list(self._shared_nets.get(key, ()))  # type: ignore[union-attr]

    # -- convenience ------------------------------------------------------

    def component_count(self):
        # type: () -> int
        return len(self.components)

    def net_count(self):
        # type: () -> int
        return sum(1 for n in self.board.nets if n.role != NetRole.UNCONNECTED)

    def describe(self):
        # type: () -> Dict[str, object]
        """Small summary of graph sizes, used in the report's metrics table."""
        bipartite = self.bipartite()
        projection = self.component_graph()
        bip_edges = sum(len(v) for v in bipartite.values()) // 2
        proj_edges = sum(len(v) for v in projection.values()) // 2
        return {
            "components_analyzed": len(self.components),
            "nets_analyzed": self.net_count(),
            "bipartite_nodes": len(bipartite),
            "bipartite_edges": bip_edges,
            "projection_nodes": len(projection),
            "projection_edges": proj_edges,
            "projection_nets_used": len(self.projection_nets()),
        }
