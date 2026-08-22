"""
Unit tests for graph construction.

Two modelling decisions carry the electronics reasoning and are therefore the
focus here: rails are excluded from the component projection (because GND touches
everything and would make the projection nearly complete), and mechanical/DNP
parts are excluded everywhere (because they are not part of the circuit).
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netlist_topology_analyzer.core.algorithms import articulation_points  # noqa: E402
from netlist_topology_analyzer.core.classifier import classify_board  # noqa: E402
from netlist_topology_analyzer.core.config import AnalysisConfig  # noqa: E402
from netlist_topology_analyzer.core.graph_model import (  # noqa: E402
    NetlistGraph,
    component_node,
    is_component_node,
    is_net_node,
    net_node,
    node_label,
)

from tests.fixtures import mk_board, mk_component, simple_powered_board  # noqa: E402


def build(board, config=None):
    """Classify then wrap a board, mirroring what the engine does."""
    cfg = config or AnalysisConfig()
    classify_board(board, cfg)
    return NetlistGraph(board, cfg)


class TestNodeIdentifiers(unittest.TestCase):
    def test_prefixes_keep_the_two_node_kinds_distinct(self):
        self.assertEqual(component_node("U1"), "C:U1")
        self.assertEqual(net_node("GND"), "N:GND")
        self.assertTrue(is_component_node("C:U1"))
        self.assertFalse(is_net_node("C:U1"))
        self.assertTrue(is_net_node("N:GND"))

    def test_a_component_and_a_net_of_the_same_name_do_not_collide(self):
        self.assertNotEqual(component_node("GND"), net_node("GND"))

    def test_node_label_strips_the_prefix(self):
        self.assertEqual(node_label("C:U1"), "U1")
        self.assertEqual(node_label("N:+3V3"), "+3V3")
        self.assertEqual(node_label("plain"), "plain")


class TestBipartiteGraph(unittest.TestCase):
    def setUp(self):
        self.graph = build(simple_powered_board())
        self.adj = self.graph.bipartite()

    def test_contains_both_node_kinds(self):
        comps = [n for n in self.adj if is_component_node(n)]
        nets = [n for n in self.adj if is_net_node(n)]
        self.assertEqual(len(comps), 7)
        self.assertEqual(sorted(node_label(n) for n in nets),
                         ["+3V3", "GND", "SCL", "SDA", "TX"])

    def test_incidence_edges_only(self):
        # A bipartite graph has no component-component or net-net edges.
        for node, neighbours in self.adj.items():
            for other in neighbours:
                self.assertNotEqual(is_component_node(node), is_component_node(other))

    def test_rails_are_kept_in_the_bipartite_view(self):
        # This view is the lossless one: nothing is filtered for connectivity.
        self.assertIn(net_node("GND"), self.adj)
        self.assertIn(net_node("+3V3"), self.adj)

    def test_component_degree_equals_distinct_nets_touched(self):
        # U1 touches +3V3, GND, SDA, SCL, TX = 5 distinct nets from 8 pads.
        self.assertEqual(len(self.adj[component_node("U1")]), 5)

    def test_result_is_cached(self):
        self.assertIs(self.graph.bipartite(), self.adj)


class TestComponentProjection(unittest.TestCase):
    def setUp(self):
        self.graph = build(simple_powered_board())
        self.adj = self.graph.component_graph()

    def test_only_component_nodes(self):
        self.assertTrue(all(is_component_node(n) for n in self.adj))

    def test_rails_contribute_no_edges(self):
        # C1 and C2 sit only on +3V3/GND, so with rails excluded they are
        # isolated in the signal-topology view.
        self.assertEqual(self.adj[component_node("C1")], set())
        self.assertEqual(self.adj[component_node("C2")], set())

    def test_shared_signal_nets_create_edges(self):
        self.assertIn(component_node("U2"), self.adj[component_node("U1")])
        self.assertIn(component_node("U1"), self.adj[component_node("R1")])
        self.assertIn(component_node("U1"), self.adj[component_node("J1")])

    def test_hyperedge_becomes_a_clique(self):
        # SDA joins U1, U2, R1: all three pairs must be adjacent.
        for a, b in (("U1", "U2"), ("U1", "R1"), ("U2", "R1")):
            self.assertIn(component_node(b), self.adj[component_node(a)])

    def test_shared_nets_are_recorded(self):
        self.assertEqual(sorted(self.graph.shared_nets("U1", "U2")), ["SCL", "SDA"])
        self.assertEqual(self.graph.shared_nets("U1", "J1"), ["TX"])
        self.assertEqual(self.graph.shared_nets("C1", "C2"), [])

    def test_shared_nets_is_order_independent(self):
        self.assertEqual(self.graph.shared_nets("U1", "J1"),
                         self.graph.shared_nets("J1", "U1"))

    def test_including_rails_destroys_the_structure(self):
        """Demonstrates *why* rails are excluded, rather than just asserting it.

        With GND and +3V3 in the projection every part is adjacent to nearly
        every other, so no component is an articulation point and the topology
        analysis has nothing left to say.
        """
        excluded = build(simple_powered_board())
        included = build(
            simple_powered_board(),
            AnalysisConfig(exclude_rails_from_projection=False),
        )
        cuts_excluded = articulation_points(excluded.component_graph())
        cuts_included = articulation_points(included.component_graph())
        self.assertEqual(sorted(node_label(n) for n in cuts_excluded), ["U1"])
        self.assertEqual(cuts_included, set())

        edges_excluded = excluded.describe()["projection_edges"]
        edges_included = included.describe()["projection_edges"]
        self.assertGreater(edges_included, edges_excluded * 2)

    def test_result_is_cached(self):
        self.assertIs(self.graph.component_graph(), self.adj)


class TestFiltering(unittest.TestCase):
    def test_mechanical_parts_are_excluded(self):
        board = mk_board([
            mk_component("U1", [(1, "SDA"), (2, "GND")]),
            mk_component("U2", [(1, "SDA"), (2, "GND")]),
            mk_component("H1", [(1, "GND")]),
        ])
        graph = build(board)
        self.assertEqual(sorted(c.reference for c in graph.components), ["U1", "U2"])
        self.assertNotIn(component_node("H1"), graph.bipartite())

    def test_dnp_parts_are_excluded(self):
        board = mk_board([
            mk_component("U1", [(1, "SDA")]),
            mk_component("R9", [(1, "SDA")], dnp=True),
        ])
        graph = build(board)
        self.assertEqual([c.reference for c in graph.components], ["U1"])

    def test_dnp_parts_can_be_kept(self):
        board = mk_board([
            mk_component("U1", [(1, "SDA")]),
            mk_component("R9", [(1, "SDA")], dnp=True),
        ])
        graph = build(board, AnalysisConfig(ignore_dnp=False))
        self.assertEqual(sorted(c.reference for c in graph.components), ["R9", "U1"])

    def test_bom_excluded_parts_are_dropped(self):
        board = mk_board([
            mk_component("U1", [(1, "SDA")]),
            mk_component("R9", [(1, "SDA")], excluded_from_bom=True),
        ])
        self.assertEqual([c.reference for c in build(board).components], ["U1"])

    def test_net_reaching_one_included_component_is_not_projected(self):
        # SDA touches U1 and the filtered-out H1, so it links nothing real.
        board = mk_board([
            mk_component("U1", [(1, "SDA"), (2, "CLK")]),
            mk_component("U2", [(1, "CLK")]),
            mk_component("H1", [(1, "SDA")]),
        ])
        graph = build(board)
        self.assertEqual([n.name for n in graph.projection_nets()], ["CLK"])

    def test_very_wide_nets_are_skipped_in_the_projection(self):
        """A wide bus would contribute O(N^2) edges and swamp the structure."""
        wide = [mk_component("U{0}".format(i), [(1, "SHIELD"), (2, "CLK")])
                for i in range(6)]
        board = mk_board(wide)
        graph = build(board, AnalysisConfig(projection_max_net_fanout=4,
                                            rail_fanout_ratio=1.5))
        used = [n.name for n in graph.projection_nets()]
        self.assertNotIn("SHIELD", used)
        self.assertNotIn("CLK", used)

    def test_unconnected_nets_never_enter_either_graph(self):
        board = mk_board([
            mk_component("U1", [(1, "unconnected-(U1-Pad1)"), (2, "CLK")]),
            mk_component("U2", [(1, "CLK")]),
        ])
        graph = build(board)
        labels = [node_label(n) for n in graph.bipartite() if is_net_node(n)]
        self.assertEqual(labels, ["CLK"])


class TestDescribe(unittest.TestCase):
    def test_metrics_are_self_consistent(self):
        graph = build(simple_powered_board())
        info = graph.describe()
        self.assertEqual(info["components_analyzed"], 7)
        self.assertEqual(info["nets_analyzed"], 5)
        self.assertEqual(info["bipartite_nodes"], 7 + 5)
        # One incidence edge per (component, distinct net) pair:
        # U1(5) + C1(2) + U2(4) + C2(2) + R1(2) + R2(2) + J1(2) = 19.
        self.assertEqual(info["bipartite_edges"], 19)
        # Projection: SDA and SCL each give a triangle sharing the U1-U2 edge
        # (5 distinct edges), plus TX giving J1-U1.
        self.assertEqual(info["projection_nodes"], 7)
        self.assertEqual(info["projection_edges"], 6)
        self.assertEqual(info["projection_nets_used"], 3)

    def test_empty_board_describes_cleanly(self):
        info = build(mk_board([])).describe()
        self.assertEqual(info["components_analyzed"], 0)
        self.assertEqual(info["projection_edges"], 0)


if __name__ == "__main__":
    unittest.main()
