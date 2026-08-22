"""
Unit tests for the generic graph algorithms.

These assert against textbook graphs whose articulation points, bridges and
centrality values can be derived by hand, so a regression in the algorithm layer
is caught independently of any circuit interpretation.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netlist_topology_analyzer.core.algorithms import (  # noqa: E402
    UnionFind,
    articulation_points,
    betweenness_centrality,
    bfs_distances,
    bridges,
    build_adjacency,
    connected_components,
    degrees,
    eccentricity_and_diameter,
    shortest_path,
    top_n,
)


def graph(*edges):
    """Build an adjacency map from ``"AB"``-style edge strings."""
    nodes = set()
    pairs = []
    for edge in edges:
        a, b = edge[0], edge[1]
        nodes.update((a, b))
        pairs.append((a, b))
    return build_adjacency(nodes, pairs)


class TestUnionFind(unittest.TestCase):
    def test_starts_fully_disjoint(self):
        uf = UnionFind("ABCD")
        self.assertEqual(uf.group_count, 4)
        self.assertFalse(uf.connected("A", "B"))

    def test_union_merges_and_is_idempotent(self):
        uf = UnionFind("ABCD")
        self.assertTrue(uf.union("A", "B"))
        self.assertFalse(uf.union("A", "B"), "second union should be a no-op")
        self.assertTrue(uf.connected("A", "B"))
        self.assertEqual(uf.group_count, 3)

    def test_transitivity(self):
        uf = UnionFind("ABC")
        uf.union("A", "B")
        uf.union("B", "C")
        self.assertTrue(uf.connected("A", "C"))
        self.assertEqual(uf.group_count, 1)

    def test_unknown_items_are_added_on_demand(self):
        uf = UnionFind()
        uf.union("X", "Y")
        self.assertTrue(uf.connected("X", "Y"))
        self.assertEqual(uf.group_count, 1)

    def test_groups_sorted_largest_first(self):
        uf = UnionFind("ABCDE")
        uf.union("A", "B")
        uf.union("B", "C")
        uf.union("D", "E")
        groups = uf.groups()
        self.assertEqual([len(g) for g in groups], [3, 2])
        self.assertEqual(groups[0], ["A", "B", "C"])


class TestConnectedComponents(unittest.TestCase):
    def test_single_component(self):
        self.assertEqual(connected_components(graph("AB", "BC")), [["A", "B", "C"]])

    def test_two_components_ordered_by_size(self):
        adj = graph("AB", "BC", "DE")
        self.assertEqual(connected_components(adj), [["A", "B", "C"], ["D", "E"]])

    def test_isolated_node_is_its_own_component(self):
        adj = build_adjacency(["A", "B", "Z"], [("A", "B")])
        self.assertEqual(connected_components(adj), [["A", "B"], ["Z"]])

    def test_empty_graph(self):
        self.assertEqual(connected_components({}), [])


class TestArticulationPoints(unittest.TestCase):
    def test_path_middle_node(self):
        # A-B-C: removing B disconnects A from C.
        self.assertEqual(articulation_points(graph("AB", "BC")), {"B"})

    def test_cycle_has_none(self):
        # Every node in a cycle has a redundant path around it.
        self.assertEqual(articulation_points(graph("AB", "BC", "CA")), set())

    def test_textbook_triangle_with_tail(self):
        # Triangle A-B-C plus tail C-D-E. Cutting C or D splits the graph.
        adj = graph("AB", "BC", "CA", "CD", "DE")
        self.assertEqual(articulation_points(adj), {"C", "D"})

    def test_star_centre(self):
        adj = graph("XA", "XB", "XC")
        self.assertEqual(articulation_points(adj), {"X"})

    def test_two_cycles_joined_by_a_node(self):
        adj = graph("AB", "BC", "CA", "CD", "DE", "EC")
        self.assertEqual(articulation_points(adj), {"C"})

    def test_disconnected_graph_handled_per_component(self):
        adj = graph("AB", "BC", "DE", "EF")
        self.assertEqual(articulation_points(adj), {"B", "E"})

    def test_deep_path_does_not_hit_recursion_limit(self):
        # A recursive Tarjan would raise RecursionError well before 5000 nodes.
        size = 5000
        nodes = [str(i) for i in range(size)]
        edges = [(str(i), str(i + 1)) for i in range(size - 1)]
        adj = build_adjacency(nodes, edges)
        cuts = articulation_points(adj)
        # Every interior node of a path is an articulation point.
        self.assertEqual(len(cuts), size - 2)
        self.assertNotIn("0", cuts)
        self.assertNotIn(str(size - 1), cuts)


class TestBridges(unittest.TestCase):
    def test_path_edges_are_all_bridges(self):
        self.assertEqual(bridges(graph("AB", "BC")), [("A", "B"), ("B", "C")])

    def test_cycle_has_no_bridges(self):
        self.assertEqual(bridges(graph("AB", "BC", "CA")), [])

    def test_triangle_with_tail(self):
        adj = graph("AB", "BC", "CA", "CD", "DE")
        self.assertEqual(bridges(adj), [("C", "D"), ("D", "E")])

    def test_each_bridge_reported_once(self):
        found = bridges(graph("AB", "BC", "CD"))
        self.assertEqual(len(found), len(set(found)))


class TestTraversal(unittest.TestCase):
    def test_bfs_distances(self):
        dist = bfs_distances(graph("AB", "BC", "CD"), "A")
        self.assertEqual(dist, {"A": 0, "B": 1, "C": 2, "D": 3})

    def test_bfs_from_unknown_source(self):
        self.assertEqual(bfs_distances(graph("AB"), "Z"), {})

    def test_bfs_does_not_cross_components(self):
        dist = bfs_distances(graph("AB", "CD"), "A")
        self.assertNotIn("C", dist)

    def test_shortest_path(self):
        self.assertEqual(shortest_path(graph("AB", "BC", "CD"), "A", "D"),
                         ["A", "B", "C", "D"])

    def test_shortest_path_prefers_the_shortcut(self):
        self.assertEqual(shortest_path(graph("AB", "BC", "AC"), "A", "C"), ["A", "C"])

    def test_shortest_path_same_node(self):
        self.assertEqual(shortest_path(graph("AB"), "A", "A"), ["A"])

    def test_shortest_path_unreachable(self):
        self.assertIsNone(shortest_path(graph("AB", "CD"), "A", "C"))

    def test_degrees(self):
        self.assertEqual(degrees(graph("AB", "BC")), {"A": 1, "B": 2, "C": 1})

    def test_diameter_of_path(self):
        adj = graph("AB", "BC", "CD")
        ecc, diameter = eccentricity_and_diameter(adj)
        self.assertEqual(diameter, 3)
        self.assertEqual(ecc["A"], 3)
        self.assertEqual(ecc["B"], 2)


class TestBetweenness(unittest.TestCase):
    def test_path_middle_node_is_fully_central(self):
        # In A-B-C the only non-trivial shortest path runs through B, so its
        # normalized betweenness is exactly 1.0.
        scores = betweenness_centrality(graph("AB", "BC"))
        self.assertAlmostEqual(scores["B"], 1.0, places=6)
        self.assertAlmostEqual(scores["A"], 0.0, places=6)
        self.assertAlmostEqual(scores["C"], 0.0, places=6)

    def test_star_centre_is_fully_central(self):
        scores = betweenness_centrality(graph("XA", "XB", "XC"))
        self.assertAlmostEqual(scores["X"], 1.0, places=6)
        for leaf in "ABC":
            self.assertAlmostEqual(scores[leaf], 0.0, places=6)

    def test_star_centre_unnormalized_counts_pairs(self):
        # Three leaves give C(3,2) = 3 shortest paths through the centre.
        scores = betweenness_centrality(graph("XA", "XB", "XC"), normalized=False)
        self.assertAlmostEqual(scores["X"], 3.0, places=6)

    def test_cycle_is_uniform(self):
        scores = betweenness_centrality(graph("AB", "BC", "CD", "DA"))
        values = sorted(round(v, 6) for v in scores.values())
        self.assertEqual(values[0], values[-1], "a 4-cycle is vertex-transitive")

    def test_ties_are_split_between_equal_paths(self):
        # A-B-D and A-C-D are both shortest, so B and C each carry half.
        scores = betweenness_centrality(graph("AB", "AC", "BD", "CD"),
                                        normalized=False)
        self.assertAlmostEqual(scores["B"], 0.5, places=6)
        self.assertAlmostEqual(scores["C"], 0.5, places=6)


class TestTopN(unittest.TestCase):
    def test_orders_by_score_then_name(self):
        self.assertEqual(
            top_n({"a": 1.0, "b": 3.0, "c": 3.0, "d": 2.0}, 3),
            [("b", 3.0), ("c", 3.0), ("d", 2.0)],
        )

    def test_minimum_filter_excludes_equal_values(self):
        self.assertEqual(top_n({"a": 0.0, "b": 1.0}, 5, minimum=0.0), [("b", 1.0)])

    def test_count_is_respected(self):
        self.assertEqual(len(top_n({str(i): float(i) for i in range(20)}, 4)), 4)


if __name__ == "__main__":
    unittest.main()
