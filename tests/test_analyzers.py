"""
Unit tests for the analysis rules and the engine that drives them.

Each test builds the smallest board that should trigger one specific rule, then
asserts both that the rule fires (its NTA code is present) and, where it matters,
that it stays quiet on a clean board. Negative assertions are as important as
positive ones: a checker that warns about everything is worthless.

Every test goes through :func:`analyze_board`, the engine's single public entry
point, so the GUI and CLI exercise exactly the code path tested here.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netlist_topology_analyzer.core.analyzers import (  # noqa: E402
    REGISTRY,
    Analyzer,
    _cap_findings,
    _truncate,
    register,
)
from netlist_topology_analyzer.core.config import AnalysisConfig  # noqa: E402
from netlist_topology_analyzer.core.engine import (  # noqa: E402
    analyze_board,
    analyzer_names,
    select_analyzers,
)
from netlist_topology_analyzer.core.model import (  # noqa: E402
    Finding,
    Net,
    NetRole,
    Severity,
)

from tests.fixtures import (  # noqa: E402
    codes,
    find,
    mk_board,
    mk_component,
    simple_powered_board,
)


class TestCleanBoard(unittest.TestCase):
    """A well-formed board must not produce spurious errors or warnings."""

    def setUp(self):
        self.result = analyze_board(simple_powered_board())

    def test_no_errors(self):
        self.assertEqual(self.result.count_by_severity()[Severity.ERROR], 0)

    def test_no_analyzer_crashed(self):
        self.assertEqual(self.result.errors, [])

    def test_no_connectivity_or_power_complaints(self):
        quiet = {"NTA-100", "NTA-101", "NTA-110", "NTA-111", "NTA-120",
                 "NTA-130", "NTA-140", "NTA-141", "NTA-142", "NTA-143",
                 "NTA-144", "NTA-145"}
        self.assertEqual(codes(self.result.findings) & quiet, set())

    def test_headline_metrics_are_populated(self):
        for key in ("components_total", "nets_total", "pads_total",
                    "electrical_islands", "analyzers_run", "analysis_time_s"):
            self.assertIn(key, self.result.metrics)

    def test_every_analyzer_ran(self):
        self.assertEqual(self.result.metrics["analyzers_run"], len(REGISTRY))


class TestBoardStatistics(unittest.TestCase):
    def test_role_counts_and_rail_inventory(self):
        result = analyze_board(simple_powered_board())
        self.assertEqual(result.metrics["components_total"], 7)
        self.assertEqual(result.metrics["power_nets"], 1)
        self.assertEqual(result.metrics["ground_nets"], 1)
        self.assertEqual(result.metrics["signal_nets"], 3)
        rails = dict((row["net"], row) for row in result.sections["rails"])
        self.assertEqual(sorted(rails), ["+3V3", "GND"])
        self.assertEqual(rails["GND"]["role"], NetRole.GROUND)

    def test_unconnected_pads_are_counted(self):
        # U1.8 and U2.8 have no net.
        self.assertEqual(analyze_board(simple_powered_board())
                         .metrics["pads_unconnected"], 2)


class TestConnectivityIslands(unittest.TestCase):
    def test_isolated_subcircuit_is_reported(self):
        board = mk_board([
            mk_component("U1", [(1, "+3V3"), (2, "GND"), (3, "SDA")]),
            mk_component("U2", [(1, "+3V3"), (2, "GND"), (3, "SDA")]),
            # A loop with no connection to the rest of the board.
            mk_component("J3", [(1, "ISO_A"), (2, "ISO_B")]),
            mk_component("R6", [(1, "ISO_A"), (2, "ISO_B")]),
        ])
        result = analyze_board(board)
        self.assertIn("NTA-100", codes(result.findings))
        self.assertEqual(result.metrics["electrical_islands"], 2)
        detail = find(result.findings, "NTA-100")[0].detail
        self.assertIn("J3", detail)

    def test_component_with_no_connected_pads_is_an_error(self):
        board = mk_board([
            mk_component("U1", [(1, "+3V3"), (2, "GND")]),
            mk_component("C1", [(1, "+3V3"), (2, "GND")]),
            mk_component("D2", [(1, ""), (2, "")]),
        ])
        result = analyze_board(board)
        finding = find(result.findings, "NTA-101")
        self.assertEqual(len(finding), 1)
        self.assertEqual(finding[0].severity, Severity.ERROR)
        self.assertEqual(finding[0].refs, ["D2"])

    def test_orphans_do_not_inflate_the_island_count(self):
        # A part with no nets is reported as NTA-101, not as a second island.
        board = mk_board([
            mk_component("U1", [(1, "+3V3"), (2, "GND")]),
            mk_component("C1", [(1, "+3V3"), (2, "GND")]),
            mk_component("D2", [(1, ""), (2, "")]),
        ])
        result = analyze_board(board)
        self.assertEqual(result.metrics["electrical_islands"], 1)
        self.assertNotIn("NTA-100", codes(result.findings))

    def test_single_island_board_is_quiet(self):
        result = analyze_board(simple_powered_board())
        self.assertEqual(result.metrics["electrical_islands"], 1)
        self.assertNotIn("NTA-100", codes(result.findings))


class TestFloatingNets(unittest.TestCase):
    def test_single_pad_net_is_flagged(self):
        board = mk_board([
            mk_component("U1", [(1, "+3V3"), (2, "GND")]),
            mk_component("C1", [(1, "+3V3"), (2, "GND")]),
            mk_component("TP9", [(1, "TP_SPARE")]),
        ])
        result = analyze_board(board)
        finding = find(result.findings, "NTA-110")
        self.assertEqual(len(finding), 1)
        self.assertEqual(finding[0].severity, Severity.WARNING)
        self.assertIn("TP_SPARE", finding[0].refs)
        self.assertEqual(result.metrics["single_pad_nets"], 1)

    def test_net_with_no_pads_is_a_note(self):
        board = mk_board([
            mk_component("U1", [(1, "+3V3"), (2, "GND")]),
            mk_component("C1", [(1, "+3V3"), (2, "GND")]),
        ])
        # A net record with no members, as KiCad can report after edits.
        board.nets.append(Net(name="ORPHAN_NET", code=99, role=NetRole.SIGNAL))
        result = analyze_board(board)
        self.assertIn("NTA-111", codes(result.findings))

    def test_findings_are_capped_with_a_summary(self):
        """Twenty dangling nets must not become twenty warnings."""
        components = [mk_component("U1", [(1, "+3V3"), (2, "GND")]),
                      mk_component("C1", [(1, "+3V3"), (2, "GND")])]
        components += [
            mk_component("TP{0}".format(i), [(1, "SPARE{0:02d}".format(i))])
            for i in range(20)
        ]
        result = analyze_board(mk_board(components), AnalysisConfig(
            max_findings_per_rule=5))
        self.assertEqual(len(find(result.findings, "NTA-110")), 5)
        summary = find(result.findings, "NTA-112")
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0].value, 15.0)
        self.assertEqual(summary[0].severity, Severity.INFO)
        # The metric still reports the true total, uncapped.
        self.assertEqual(result.metrics["single_pad_nets"], 20)


class TestUnroutedNets(unittest.TestCase):
    def test_unrouted_net_on_a_routed_board_is_flagged(self):
        board = mk_board(
            [
                mk_component("U1", [(1, "+3V3"), (2, "GND"), (3, "SPI_MISO")]),
                mk_component("C1", [(1, "+3V3"), (2, "GND")]),
                mk_component("U2", [(1, "+3V3"), (2, "GND"), (3, "SPI_MISO")]),
            ],
            route_all=True,
            unrouted=("SPI_MISO",),
        )
        result = analyze_board(board)
        finding = find(result.findings, "NTA-120")
        self.assertEqual(len(finding), 1)
        self.assertEqual(finding[0].refs, ["SPI_MISO"])
        self.assertEqual(result.metrics["unrouted_nets"], 1)

    def test_completely_unrouted_board_gives_one_note_not_many_warnings(self):
        board = mk_board([
            mk_component("U1", [(1, "+3V3"), (2, "GND"), (3, "SDA")]),
            mk_component("U2", [(1, "+3V3"), (2, "GND"), (3, "SDA")]),
            mk_component("C1", [(1, "+3V3"), (2, "GND")]),
        ])  # route_all defaults to False
        result = analyze_board(board)
        self.assertEqual(codes(result.findings) & {"NTA-120", "NTA-121"},
                         {"NTA-121"})
        self.assertEqual(find(result.findings, "NTA-121")[0].severity, Severity.INFO)

    def test_check_can_be_disabled(self):
        board = mk_board([
            mk_component("U1", [(1, "SDA")]),
            mk_component("U2", [(1, "SDA")]),
        ])
        result = analyze_board(board, AnalysisConfig(check_unrouted=False))
        self.assertEqual(codes(result.findings) & {"NTA-120", "NTA-121", "NTA-122"},
                         set())

    def test_fully_routed_board_is_quiet(self):
        result = analyze_board(simple_powered_board())
        self.assertEqual(result.metrics["unrouted_nets"], 0)
        self.assertEqual(codes(result.findings) & {"NTA-120", "NTA-121"}, set())


class TestFanout(unittest.TestCase):
    def _i2c_board(self, device_count):
        """One MCU plus ``device_count`` devices all sharing SDA."""
        components = [
            mk_component("U1", [(1, "+3V3"), (2, "GND"), (3, "SDA")], x=0, y=0),
            mk_component("C1", [(1, "+3V3"), (2, "GND")], x=1, y=0),
        ]
        for i in range(device_count):
            components.append(
                mk_component("U{0}".format(i + 2),
                             [(1, "+3V3"), (2, "GND"), (3, "SDA")],
                             x=float(i), y=5.0)
            )
        return mk_board(components, route_all=True)

    def test_high_fanout_signal_is_a_warning(self):
        # 1 MCU pad + 9 device pads = 10 > the default threshold of 8.
        result = analyze_board(self._i2c_board(9),
                               AnalysisConfig(rail_fanout_ratio=1.5))
        finding = find(result.findings, "NTA-130")
        self.assertEqual(len(finding), 1)
        self.assertEqual(finding[0].severity, Severity.WARNING)
        self.assertEqual(finding[0].value, 10.0)

    def test_threshold_is_exclusive(self):
        # Exactly 8 pads must not fire: the rule is "more than" the threshold.
        result = analyze_board(self._i2c_board(7),
                               AnalysisConfig(rail_fanout_ratio=1.5))
        self.assertEqual(result.metrics["max_signal_fanout"], 8)
        self.assertNotIn("NTA-130", codes(result.findings))

    def test_escalates_to_error_at_the_critical_threshold(self):
        result = analyze_board(self._i2c_board(20),
                               AnalysisConfig(rail_fanout_ratio=1.5))
        finding = find(result.findings, "NTA-130")[0]
        self.assertEqual(finding.severity, Severity.ERROR)
        self.assertGreaterEqual(finding.value, 16.0)

    def test_rails_are_never_flagged(self):
        """GND is high-fanout by design; flagging it would be pure noise."""
        result = analyze_board(self._i2c_board(20),
                               AnalysisConfig(rail_fanout_ratio=1.5))
        flagged = set()
        for finding in find(result.findings, "NTA-130"):
            flagged.update(finding.refs)
        self.assertNotIn("GND", flagged)
        self.assertNotIn("+3V3", flagged)

    def test_threshold_is_configurable(self):
        result = analyze_board(self._i2c_board(3),
                               AnalysisConfig(high_fanout_threshold=2,
                                              rail_fanout_ratio=1.5))
        self.assertIn("NTA-130", codes(result.findings))

    def test_fanout_section_is_ranked_descending(self):
        result = analyze_board(self._i2c_board(9),
                               AnalysisConfig(rail_fanout_ratio=1.5))
        pads = [row["pads"] for row in result.sections["fanout"]]
        self.assertEqual(pads, sorted(pads, reverse=True))


class TestPowerIntegrity(unittest.TestCase):
    def _ic(self, reference, power, ground, x=0.0, y=0.0):
        pads = [(1, power), (2, ground), (3, "SIG_A"), (4, "SIG_B"),
                (5, "SIG_C"), (6, "SIG_D")]
        return mk_component(reference, pads, value="MCU", x=x, y=y)

    def test_missing_ground_net(self):
        board = mk_board([self._ic("U1", "+3V3", "RETURN_PATH")])
        result = analyze_board(board)
        self.assertIn("NTA-140", codes(result.findings))

    def test_missing_power_net(self):
        board = mk_board([mk_component("R1", [(1, "GND"), (2, "SIG")]),
                          mk_component("R2", [(1, "GND"), (2, "SIG")])])
        result = analyze_board(board)
        self.assertIn("NTA-141", codes(result.findings))
        self.assertEqual(find(result.findings, "NTA-141")[0].severity, Severity.INFO)

    def test_ic_without_a_power_rail(self):
        board = mk_board([
            self._ic("U1", "SOME_SIGNAL", "GND"),
            mk_component("C1", [(1, "+3V3"), (2, "GND")]),
        ])
        result = analyze_board(board)
        finding = find(result.findings, "NTA-142")
        self.assertEqual(len(finding), 1)
        self.assertEqual(finding[0].refs, ["U1"])

    def test_ic_without_a_ground(self):
        board = mk_board([
            self._ic("U1", "+3V3", "SOME_SIGNAL"),
            mk_component("C1", [(1, "+3V3"), (2, "GND")]),
        ])
        result = analyze_board(board)
        self.assertEqual(find(result.findings, "NTA-143")[0].refs, ["U1"])

    def test_ic_with_no_decoupling_capacitor(self):
        """U4 is fed from a filtered rail no capacitor sits on."""
        board = mk_board([
            self._ic("U4", "+3V3_MEM", "GND"),
            mk_component("FB1", [(1, "+3V3"), (2, "+3V3_MEM")], value="600R"),
            mk_component("C1", [(1, "+3V3"), (2, "GND")], value="100nF"),
        ])
        result = analyze_board(board)
        finding = find(result.findings, "NTA-144")
        self.assertEqual(len(finding), 1)
        self.assertEqual(finding[0].refs, ["U4"])
        self.assertEqual(finding[0].severity, Severity.WARNING)

    def test_distant_decoupling_capacitor_is_a_note(self):
        """Sharing the right nets is necessary but not sufficient - loop area matters."""
        board = mk_board([
            self._ic("U8", "+3V3", "GND", x=0.0, y=0.0),
            mk_component("C7", [(1, "+3V3"), (2, "GND")], value="100nF",
                         x=15.0, y=0.0),
        ])
        result = analyze_board(board)
        finding = find(result.findings, "NTA-145")
        self.assertEqual(len(finding), 1)
        self.assertEqual(finding[0].value, 15.0)
        self.assertEqual(finding[0].refs, ["U8", "C7"])
        # Not a "missing capacitor" - one exists, it is just too far away.
        self.assertNotIn("NTA-144", codes(result.findings))

    def test_nearby_capacitor_is_accepted(self):
        board = mk_board([
            self._ic("U8", "+3V3", "GND", x=0.0, y=0.0),
            mk_component("C7", [(1, "+3V3"), (2, "GND")], x=2.0, y=0.0),
        ])
        result = analyze_board(board)
        self.assertEqual(codes(result.findings) & {"NTA-144", "NTA-145"}, set())

    def test_distance_check_can_be_disabled(self):
        board = mk_board([
            self._ic("U8", "+3V3", "GND", x=0.0, y=0.0),
            mk_component("C7", [(1, "+3V3"), (2, "GND")], x=40.0, y=0.0),
        ])
        result = analyze_board(board, AnalysisConfig(decoupling_ignore_distance=True))
        self.assertNotIn("NTA-145", codes(result.findings))

    def test_small_parts_are_not_checked_for_decoupling(self):
        """A 3-pin regulator or transistor does not need its own bypass cap."""
        board = mk_board([
            mk_component("Q1", [(1, "+3V3"), (2, "GND"), (3, "SIG")], value="NPN"),
            mk_component("R1", [(1, "SIG"), (2, "GND")]),
        ])
        result = analyze_board(board)
        self.assertEqual(result.metrics["ics_checked"], 0)
        self.assertNotIn("NTA-144", codes(result.findings))

    def test_power_section_records_the_nearest_capacitor(self):
        board = mk_board([
            self._ic("U1", "+3V3", "GND", x=0.0, y=0.0),
            mk_component("C1", [(1, "+3V3"), (2, "GND")], x=3.0, y=4.0),
            mk_component("C2", [(1, "+3V3"), (2, "GND")], x=20.0, y=0.0),
        ])
        result = analyze_board(board)
        row = result.sections["power"][0]
        self.assertEqual(row["component"], "U1")
        self.assertEqual(row["decoupling_caps"], 2)
        self.assertEqual(row["nearest_cap"], "C1")
        self.assertEqual(row["nearest_mm"], 5.0)  # 3-4-5 triangle


class TestSinglePointOfFailure(unittest.TestCase):
    def test_hub_component_is_an_articulation_point(self):
        result = analyze_board(simple_powered_board())
        finding = find(result.findings, "NTA-150")
        self.assertEqual(len(finding), 1)
        self.assertEqual(finding[0].refs, ["U1"])
        self.assertEqual(finding[0].severity, Severity.WARNING)
        self.assertEqual(result.metrics["articulation_points"], 1)

    def test_connectors_are_downgraded_to_info(self):
        """Being the interface to the outside world is a connector's job."""
        board = mk_board([
            mk_component("U1", [(1, "A"), (2, "B")]),
            mk_component("U2", [(1, "A"), (2, "B")]),
            # J1 is the sole link between the U1/U2 pair and U3.
            mk_component("J1", [(1, "B"), (2, "C")]),
            mk_component("U3", [(1, "C"), (2, "D")]),
            mk_component("U4", [(1, "D"), (2, "C")]),
        ])
        result = analyze_board(board)
        interface = find(result.findings, "NTA-151")
        self.assertEqual([f.refs[0] for f in interface], ["J1"])
        self.assertEqual(interface[0].severity, Severity.INFO)

    def test_bridges_are_reported_with_the_nets_that_form_them(self):
        result = analyze_board(simple_powered_board())
        self.assertIn("NTA-152", codes(result.findings))
        bridges = dict(((r["from"], r["to"]), r["via_nets"])
                       for r in result.sections["bridges"])
        self.assertIn(("J1", "U1"), bridges)
        self.assertEqual(bridges[("J1", "U1")], "TX")

    def test_redundant_topology_has_no_single_point_of_failure(self):
        # A ring of four parts: every part has a path around it.
        board = mk_board([
            mk_component("U1", [(1, "A"), (2, "B")]),
            mk_component("U2", [(1, "B"), (2, "C")]),
            mk_component("U3", [(1, "C"), (2, "D")]),
            mk_component("U4", [(1, "D"), (2, "A")]),
        ])
        result = analyze_board(board)
        self.assertEqual(result.metrics["articulation_points"], 0)
        self.assertEqual(result.metrics["bridge_connections"], 0)

    def test_tiny_graphs_are_skipped(self):
        board = mk_board([mk_component("U1", [(1, "A")]),
                          mk_component("U2", [(1, "A")])])
        result = analyze_board(board)
        self.assertEqual(codes(result.findings) & {"NTA-150", "NTA-151", "NTA-152"},
                         set())


class TestTopologyMetrics(unittest.TestCase):
    def test_hub_has_the_highest_centrality(self):
        result = analyze_board(simple_powered_board())
        finding = find(result.findings, "NTA-160")
        self.assertEqual(len(finding), 1)
        self.assertEqual(finding[0].refs, ["U1"])
        ranked = result.sections["centrality"]
        self.assertEqual(ranked[0]["component"], "U1")
        self.assertGreater(ranked[0]["betweenness"], 0.0)

    def test_degree_ranking_is_populated(self):
        result = analyze_board(simple_powered_board())
        top = result.sections["degree"][0]
        self.assertEqual(top["component"], "U1")
        # U1 is adjacent to U2, R1, R2 and J1 in the rail-free projection.
        self.assertEqual(top["connections"], 4)

    def test_diameter_is_measured_on_the_largest_cluster(self):
        result = analyze_board(simple_powered_board())
        self.assertEqual(result.metrics["signal_graph_diameter"], 2)
        self.assertEqual(result.metrics["largest_signal_cluster"], 5)

    def test_centrality_is_skipped_on_large_boards(self):
        components = [
            mk_component("U{0}".format(i),
                         [(1, "N{0}".format(i)), (2, "N{0}".format(i + 1))])
            for i in range(6)
        ]
        result = analyze_board(mk_board(components),
                               AnalysisConfig(centrality_max_components=3))
        self.assertIn("NTA-161", codes(result.findings))
        self.assertTrue(result.metrics["centrality_skipped"])
        self.assertNotIn("centrality", result.sections)


class TestEngine(unittest.TestCase):
    def test_analyzer_names_are_unique(self):
        names = analyzer_names()
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("", names)

    def test_empty_selection_runs_everything(self):
        self.assertEqual(len(select_analyzers(AnalysisConfig())), len(REGISTRY))

    def test_subset_selection_preserves_registry_order(self):
        selected = select_analyzers(
            AnalysisConfig(enabled_analyzers=["spof", "statistics"]))
        self.assertEqual([c.name for c in selected], ["statistics", "spof"])

    def test_disabled_analyzers_produce_no_findings(self):
        result = analyze_board(simple_powered_board(),
                              AnalysisConfig(enabled_analyzers=["statistics"]))
        self.assertEqual(result.findings, [])
        self.assertEqual(result.metrics["analyzers_run"], 1)

    def test_empty_board_does_not_crash(self):
        result = analyze_board(mk_board([]))
        self.assertEqual(result.errors, [])

    def test_a_failing_analyzer_is_isolated(self):
        """One broken rule must degrade to an error entry, not kill the run."""

        @register
        class _ExplodingAnalyzer(Analyzer):
            name = "_exploding"
            title = "Deliberately broken"
            category = "Test"

            def analyze(self, ctx):
                raise ValueError("boom")

        try:
            result = analyze_board(simple_powered_board())
            self.assertTrue(any("boom" in e for e in result.errors))
            self.assertTrue(any("_exploding" in e for e in result.errors))
            # Other analyzers still contributed.
            self.assertIn("NTA-150", codes(result.findings))
        finally:
            REGISTRY.remove(_ExplodingAnalyzer)

    def test_severity_counters_match_the_findings(self):
        result = analyze_board(simple_powered_board())
        counts = result.count_by_severity()
        self.assertEqual(result.metrics["errors_found"], counts[Severity.ERROR])
        self.assertEqual(result.metrics["warnings_found"], counts[Severity.WARNING])


class TestHelpers(unittest.TestCase):
    def test_truncate_leaves_short_lists_alone(self):
        self.assertEqual(_truncate(["A", "B"], 5), "A, B")

    def test_truncate_caps_long_lists(self):
        text = _truncate([str(i) for i in range(10)], 3)
        self.assertTrue(text.startswith("0, 1, 2, ..."))
        self.assertIn("+7 more", text)

    def test_cap_findings_is_a_no_op_below_the_cap(self):
        findings = [Finding(code="X", title="t") for _ in range(3)]
        self.assertIs(_cap_findings(findings, 5, "S", "{count}", "{items}", "C"),
                      findings)

    def test_cap_of_zero_disables_capping(self):
        findings = [Finding(code="X", title="t") for _ in range(30)]
        self.assertEqual(len(_cap_findings(findings, 0, "S", "{count}", "{items}",
                                           "C")), 30)

    def test_cap_findings_names_the_omitted_items(self):
        findings = [Finding(code="X", title="t", refs=["N{0}".format(i)])
                    for i in range(6)]
        capped = _cap_findings(findings, 2, "S-1", "{count} more", "items: {items}",
                              "Cat")
        self.assertEqual(len(capped), 3)
        summary = capped[-1]
        self.assertEqual(summary.code, "S-1")
        self.assertEqual(summary.title, "4 more")
        self.assertIn("N5", summary.detail)


if __name__ == "__main__":
    unittest.main()
