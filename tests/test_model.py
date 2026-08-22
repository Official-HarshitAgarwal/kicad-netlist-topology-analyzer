"""
Unit tests for the data model.

The model is the contract between the KiCad adapter and the engine, so these
tests pin down the derived properties the analyzers rely on and the JSON
round-trip that makes head-less example boards possible.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netlist_topology_analyzer.core.model import (  # noqa: E402
    AnalysisResult,
    BoardData,
    Component,
    Finding,
    Net,
    NetRole,
    Pad,
    Severity,
    build_nets_from_components,
)

from tests.fixtures import mk_board, mk_component, simple_powered_board  # noqa: E402


class TestPad(unittest.TestCase):
    def test_uid(self):
        self.assertEqual(Pad(reference="U1", number="7").uid, "U1.7")

    def test_alphanumeric_pad_number_survives(self):
        # BGA pads are named like "A12"; storing them as strings is deliberate.
        self.assertEqual(Pad(reference="U1", number="A12").uid, "U1.A12")

    def test_is_connected_requires_both_name_and_code(self):
        self.assertTrue(Pad(reference="U1", number="1", net_name="GND",
                            net_code=3).is_connected)
        # KiCad uses net code 0 as its "no net" sentinel.
        self.assertFalse(Pad(reference="U1", number="1", net_name="GND",
                             net_code=0).is_connected)
        self.assertFalse(Pad(reference="U1", number="1", net_name="",
                             net_code=3).is_connected)


class TestComponentPrefix(unittest.TestCase):
    def test_simple_prefixes(self):
        self.assertEqual(Component(reference="U1").prefix, "U")
        self.assertEqual(Component(reference="C12").prefix, "C")
        self.assertEqual(Component(reference="CN3").prefix, "CN")

    def test_prefix_is_uppercased(self):
        self.assertEqual(Component(reference="u5").prefix, "U")

    def test_prefix_stops_at_first_digit(self):
        # "R10A" must not become "RA" - only the leading letters count.
        self.assertEqual(Component(reference="R10A").prefix, "R")

    def test_prefix_of_all_letters(self):
        self.assertEqual(Component(reference="LOGO").prefix, "LOGO")

    def test_net_names_ignores_unconnected_pads(self):
        comp = mk_component("U1", [(1, "GND"), (2, ""), (3, "GND")])
        self.assertEqual(comp.net_names(), {"GND"})

    def test_pad_count_includes_unconnected_pads(self):
        # Pin count is used to tell ICs from passives, so every pad must count.
        self.assertEqual(mk_component("U1", [(1, "GND"), (2, "")]).pad_count, 2)


class TestNet(unittest.TestCase):
    def test_fanout_matches_pad_count(self):
        net = Net(name="SDA", pads=[Pad("U1", "3"), Pad("U2", "3")])
        self.assertEqual(net.fanout, 2)
        self.assertEqual(net.pad_count, 2)

    def test_references_deduplicates(self):
        net = Net(name="GND", pads=[Pad("U1", "2"), Pad("U1", "6"), Pad("C1", "2")])
        self.assertEqual(net.references(), {"U1", "C1"})

    def test_is_rail(self):
        self.assertTrue(Net(name="GND", role=NetRole.GROUND).is_rail)
        self.assertTrue(Net(name="+3V3", role=NetRole.POWER).is_rail)
        self.assertFalse(Net(name="SDA", role=NetRole.SIGNAL).is_rail)

    def test_is_routed(self):
        self.assertFalse(Net(name="SDA").is_routed)
        self.assertTrue(Net(name="SDA", track_count=1).is_routed)


class TestBuildNetsFromComponents(unittest.TestCase):
    def test_groups_pads_by_net_and_sorts_by_name(self):
        comps = [
            mk_component("U1", [(1, "VCC"), (2, "GND")]),
            mk_component("C1", [(1, "VCC"), (2, "GND")]),
        ]
        nets = build_nets_from_components(comps)
        self.assertEqual([n.name for n in nets], ["GND", "VCC"])
        self.assertEqual(nets[0].pad_count, 2)

    def test_unconnected_pads_are_skipped(self):
        nets = build_nets_from_components([mk_component("U1", [(1, "VCC"), (2, "")])])
        self.assertEqual([n.name for n in nets], ["VCC"])

    def test_nets_share_pad_objects_with_components(self):
        comps = [mk_component("U1", [(1, "VCC")])]
        nets = build_nets_from_components(comps)
        self.assertIs(nets[0].pads[0], comps[0].pads[0])


class TestBoardData(unittest.TestCase):
    def setUp(self):
        self.board = simple_powered_board()

    def test_lookup_helpers(self):
        self.assertIsNotNone(self.board.component_by_ref("U1"))
        self.assertIsNone(self.board.component_by_ref("U99"))
        self.assertIsNotNone(self.board.net_by_name("GND"))
        self.assertIsNone(self.board.net_by_name("NOPE"))

    def test_pad_counts(self):
        self.assertEqual(self.board.pad_count, 8 + 2 + 8 + 2 + 2 + 2 + 2)
        # U1.8 and U2.8 are deliberately left netless.
        self.assertEqual(self.board.connected_pad_count, self.board.pad_count - 2)

    def test_net_codes_are_one_based_like_kicad(self):
        codes = sorted(n.code for n in self.board.nets)
        self.assertEqual(codes, list(range(1, len(self.board.nets) + 1)))
        self.assertNotIn(0, codes)

    def test_nets_with_role(self):
        for net in self.board.nets:
            net.role = NetRole.GROUND if net.name == "GND" else NetRole.SIGNAL
        self.assertEqual(
            [n.name for n in self.board.nets_with_role(NetRole.GROUND)], ["GND"]
        )

    def test_json_round_trip_preserves_connectivity(self):
        clone = BoardData.from_json(self.board.to_json())
        self.assertEqual(clone.name, self.board.name)
        self.assertEqual(len(clone.components), len(self.board.components))
        self.assertEqual(len(clone.nets), len(self.board.nets))
        self.assertEqual(clone.pad_count, self.board.pad_count)
        original = dict((n.name, sorted(p.uid for p in n.pads))
                        for n in self.board.nets)
        restored = dict((n.name, sorted(p.uid for p in n.pads)) for n in clone.nets)
        self.assertEqual(restored, original)

    def test_round_trip_reuses_component_pad_objects(self):
        # Nets must hold references to the components' pads, not copies, so that
        # mutating a pad through either route stays consistent.
        clone = BoardData.from_json(self.board.to_json())
        net = clone.net_by_name("TX")
        owner_pads = []
        for comp in clone.components:
            owner_pads.extend(comp.pads)
        for pad in net.pads:
            self.assertTrue(any(pad is other for other in owner_pads))

    def test_round_trip_preserves_roles_and_routing(self):
        for net in self.board.nets:
            net.role = NetRole.POWER
        clone = BoardData.from_json(self.board.to_json())
        self.assertTrue(all(n.role == NetRole.POWER for n in clone.nets))
        self.assertTrue(all(n.track_count == 1 for n in clone.nets if n.pad_count >= 2))

    def test_from_dict_derives_nets_when_absent(self):
        data = self.board.to_dict()
        del data["nets"]
        clone = BoardData.from_dict(data)
        self.assertEqual(
            sorted(n.name for n in clone.nets),
            sorted(n.name for n in self.board.nets),
        )

    def test_empty_board_is_valid(self):
        board = mk_board([])
        self.assertEqual(board.pad_count, 0)
        self.assertEqual(board.nets, [])


class TestFindingsAndResult(unittest.TestCase):
    def test_severity_rank_orders_most_severe_first(self):
        self.assertLess(Severity.rank(Severity.ERROR), Severity.rank(Severity.WARNING))
        self.assertLess(Severity.rank(Severity.WARNING), Severity.rank(Severity.INFO))
        self.assertEqual(Severity.rank("BOGUS"), 99)

    def test_sorted_findings_by_severity_then_category_then_code(self):
        result = AnalysisResult()
        result.add(Finding(code="Z-1", title="i", severity=Severity.INFO, category="B"))
        result.add(Finding(code="A-1", title="e", severity=Severity.ERROR, category="B"))
        result.add(Finding(code="B-1", title="w", severity=Severity.WARNING, category="A"))
        self.assertEqual([f.code for f in result.sorted_findings()],
                         ["A-1", "B-1", "Z-1"])

    def test_count_by_severity_reports_zeroes(self):
        result = AnalysisResult()
        result.add(Finding(code="X", title="t", severity=Severity.WARNING))
        counts = result.count_by_severity()
        self.assertEqual(counts[Severity.WARNING], 1)
        self.assertEqual(counts[Severity.ERROR], 0)
        self.assertEqual(counts[Severity.INFO], 0)

    def test_to_json_is_serialisable_with_odd_metric_values(self):
        result = AnalysisResult(board_name="b")
        result.metrics["histogram"] = {"POWER": 1}
        result.metrics["weird"] = object()  # exercises the default=str fallback
        result.add(Finding(code="X", title="t"))
        text = result.to_json()
        self.assertIn('"board_name": "b"', text)
        self.assertIn('"summary"', text)


if __name__ == "__main__":
    unittest.main()
