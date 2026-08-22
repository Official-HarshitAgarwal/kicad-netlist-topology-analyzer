"""
Unit tests for the report writers.

The report layer must render whatever the engine hands it, including sections
from analyzers it has never heard of - that generic behaviour is what makes the
plugin extensible, so it is tested explicitly. HTML escaping is tested because
net names legitimately contain ``<``, ``>`` and ``&``.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netlist_topology_analyzer.core.classifier import classify_board  # noqa: E402
from netlist_topology_analyzer.core.config import AnalysisConfig  # noqa: E402
from netlist_topology_analyzer.core.engine import analyze_board  # noqa: E402
from netlist_topology_analyzer.core.graph_model import NetlistGraph  # noqa: E402
from netlist_topology_analyzer.core.model import (  # noqa: E402
    AnalysisResult,
    Finding,
    Severity,
)
from netlist_topology_analyzer.core.report import (  # noqa: E402
    render_html,
    render_json,
    render_svg,
    render_text,
)

from tests.fixtures import mk_board, mk_component, simple_powered_board  # noqa: E402


def analysed():
    """A board, its analysis result and its graph - what the writers consume."""
    board = simple_powered_board()
    cfg = AnalysisConfig()
    result = analyze_board(board, cfg)
    graph = NetlistGraph(board, cfg)
    return board, result, graph


class TestRenderText(unittest.TestCase):
    def setUp(self):
        _, self.result, _ = analysed()
        self.text = render_text(self.result)

    def test_includes_the_board_name_and_a_summary(self):
        self.assertIn("simple_powered_board", self.text)
        self.assertIn("WARNING", self.text)

    def test_includes_finding_codes_and_titles(self):
        self.assertIn("NTA-150", self.text)
        self.assertIn("most central component", self.text)

    def test_renders_section_tables(self):
        self.assertIn("+3V3", self.text)  # rail inventory
        self.assertIn("GND", self.text)

    def test_sections_can_be_suppressed(self):
        brief = render_text(self.result, include_sections=False)
        self.assertLess(len(brief), len(self.text))
        self.assertIn("NTA-150", brief)

    def test_lines_stay_readable(self):
        # A fixed-width report is useless if it wraps unpredictably in a dialog.
        for line in self.text.splitlines():
            self.assertLessEqual(len(line), 120, line)

    def test_empty_result_renders_without_error(self):
        text = render_text(AnalysisResult(board_name="empty"))
        self.assertIn("empty", text)

    def test_errors_are_surfaced_not_hidden(self):
        result = AnalysisResult(board_name="b")
        result.errors.append("Analyzer 'x' failed: ValueError: boom")
        self.assertIn("boom", render_text(result))


class TestRenderJson(unittest.TestCase):
    def setUp(self):
        _, self.result, _ = analysed()
        self.data = json.loads(render_json(self.result))

    def test_top_level_shape_is_stable(self):
        for key in ("board_name", "findings", "metrics", "sections", "summary",
                    "errors"):
            self.assertIn(key, self.data)

    def test_findings_are_sorted_by_severity(self):
        ranks = [Severity.rank(f["severity"]) for f in self.data["findings"]]
        self.assertEqual(ranks, sorted(ranks))

    def test_finding_fields_are_complete(self):
        finding = self.data["findings"][0]
        for key in ("code", "title", "severity", "category", "detail", "refs",
                    "value"):
            self.assertIn(key, finding)

    def test_summary_matches_the_findings_list(self):
        counted = {}
        for finding in self.data["findings"]:
            counted[finding["severity"]] = counted.get(finding["severity"], 0) + 1
        for severity, total in self.data["summary"].items():
            self.assertEqual(counted.get(severity, 0), total)

    def test_output_is_deterministic(self):
        # Stable output is what makes JSON diffs between revisions meaningful.
        board = simple_powered_board()
        first = json.loads(render_json(analyze_board(board)))
        second = json.loads(render_json(analyze_board(simple_powered_board())))
        first["metrics"].pop("analysis_time_s", None)
        second["metrics"].pop("analysis_time_s", None)
        self.assertEqual(first, second)


class TestRenderSvg(unittest.TestCase):
    def test_output_is_well_formed_xml(self):
        _, _, graph = analysed()
        svg = render_svg(graph)
        self.assertTrue(svg.startswith("<svg"))
        root = ET.fromstring(svg)
        self.assertTrue(root.tag.endswith("svg"))

    def test_every_projected_component_is_drawn(self):
        _, _, graph = analysed()
        svg = render_svg(graph)
        for ref in ("U1", "U2", "R1", "R2", "J1"):
            self.assertIn(">{0}<".format(ref), svg)

    def test_empty_graph_renders_nothing(self):
        board = mk_board([])
        classify_board(board, AnalysisConfig())
        self.assertEqual(render_svg(NetlistGraph(board, AnalysisConfig())), "")

    def test_oversized_graph_is_skipped(self):
        _, _, graph = analysed()
        self.assertEqual(render_svg(graph, max_nodes=2), "")

    def test_layout_is_deterministic(self):
        _, _, graph_a = analysed()
        _, _, graph_b = analysed()
        self.assertEqual(render_svg(graph_a), render_svg(graph_b))


class TestRenderHtml(unittest.TestCase):
    def setUp(self):
        _, self.result, self.graph = analysed()
        self.html = render_html(self.result, self.graph)

    def test_is_a_complete_document(self):
        self.assertTrue(self.html.lstrip().startswith("<!DOCTYPE html>"))
        self.assertIn("</html>", self.html)

    def test_is_self_contained(self):
        """No external requests: the report must open offline, from anywhere.

        The SVG's ``xmlns`` namespace URI is not a fetch, so this looks for the
        constructs that actually load something instead of banning ``http``.
        """
        self.assertNotIn("<script", self.html.lower())
        self.assertNotIn("<link", self.html.lower())
        self.assertNotIn("@import", self.html.lower())
        self.assertNotIn("src=", self.html.lower())
        self.assertNotIn("href=", self.html.lower())
        self.assertIn("<style", self.html.lower())

    def test_declares_light_colour_scheme(self):
        self.assertIn("color-scheme", self.html)

    def test_contains_findings_and_metrics(self):
        self.assertIn("NTA-150", self.html)
        self.assertIn("simple_powered_board", self.html)

    def test_embeds_the_topology_diagram(self):
        self.assertIn("<svg", self.html)

    def test_works_without_a_graph(self):
        html = render_html(self.result)
        self.assertIn("NTA-150", html)

    def test_escapes_html_metacharacters(self):
        """Net names may legitimately contain <, > and &, e.g. bus slices."""
        result = AnalysisResult(board_name="board & co")
        result.add(
            Finding(
                code="NTA-999",
                title="Net '<DATA&BUS>' is odd",
                severity=Severity.WARNING,
                category="Test",
                detail="Raw <b>markup</b> must not be interpreted.",
                refs=["<DATA&BUS>"],
            )
        )
        result.sections["custom"] = [{"net": "<DATA&BUS>", "pads": 2}]
        html = render_html(result)
        self.assertNotIn("<DATA&BUS>", html)
        self.assertIn("&lt;DATA&amp;BUS&gt;", html)
        self.assertNotIn("<b>markup</b>", html)
        self.assertIn("board &amp; co", html)

    def test_unknown_sections_render_generically(self):
        """A new analyzer's section must appear with no reporting changes.

        This is the Open/Closed principle made concrete: the renderer derives
        table headers from the dict keys it is given.
        """
        result = AnalysisResult(board_name="b")
        result.sections["brand_new_check"] = [
            {"component": "U7", "margin_mV": 42},
            {"component": "U8", "margin_mV": 17},
        ]
        html = render_html(result)
        self.assertIn("U7", html)
        self.assertIn("42", html)
        # The heading falls back to a humanised version of the section key.
        self.assertIn("Brand New Check", html)
        text = render_text(result)
        self.assertIn("U8", text)
        self.assertIn("margin_mV", text)

    def test_scalar_section_payload_does_not_break_rendering(self):
        result = AnalysisResult(board_name="b")
        result.sections["note"] = "just a string"
        self.assertIn("just a string", render_html(result))
        self.assertIn("just a string", render_text(result))


class TestReportsAgreeWithEachOther(unittest.TestCase):
    def test_all_three_formats_report_the_same_findings(self):
        _, result, graph = analysed()
        expected = set(f.code for f in result.findings)
        text = render_text(result)
        html = render_html(result, graph)
        data = json.loads(render_json(result))
        self.assertEqual(set(f["code"] for f in data["findings"]), expected)
        for code in expected:
            self.assertIn(code, text)
            self.assertIn(code, html)


if __name__ == "__main__":
    unittest.main()
