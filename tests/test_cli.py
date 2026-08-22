"""
Unit tests for the head-less command-line runner and the shipped demo board.

The CLI is a real deliverable, not a debug aid: it is how the engine runs in CI
and how the analysis can gate a build. So its contract - exit codes, written
files, argument validation - is tested like any other public interface.

The demo board is also verified here. ``examples/make_demo_board.py`` seeds one
specific defect per analyzer and documents which finding each should produce, so
these tests assert that mapping actually holds. That makes the example board a
regression fixture rather than just a demo.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from netlist_topology_analyzer.cli import build_parser, load_board, main  # noqa: E402
from netlist_topology_analyzer.core.config import AnalysisConfig  # noqa: E402
from netlist_topology_analyzer.core.engine import (  # noqa: E402
    analyze_board,
    analyzer_names,
)
from netlist_topology_analyzer.core.graph_model import NetlistGraph  # noqa: E402
from netlist_topology_analyzer.core.model import Severity  # noqa: E402

from tests.fixtures import codes  # noqa: E402

DEMO_BOARD = os.path.join(REPO_ROOT, "examples", "demo_board.json")


class _CapturedStreams(object):
    """Swallow stdout/stderr so test output stays readable."""

    def __enter__(self):
        self._out, self._err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        return self

    def __exit__(self, *exc):
        self.out = sys.stdout.getvalue()
        self.err = sys.stderr.getvalue()
        sys.stdout, sys.stderr = self._out, self._err
        return False


def run_cli(*argv):
    """Run the CLI, returning ``(exit_code, stdout, stderr)``."""
    with _CapturedStreams() as streams:
        code = main(list(argv))
    return code, streams.out, streams.err


class TestParser(unittest.TestCase):
    def test_board_argument_is_required_for_analysis(self):
        # The positional is optional in the grammar so that --list-analyzers
        # works alone; main() rejects its absence for every other invocation.
        code, _, err = run_cli()
        self.assertEqual(code, 2)
        self.assertIn("required", err)

    def test_defaults(self):
        args = build_parser().parse_args(["board.json"])
        self.assertEqual(args.fail_on, "never")
        self.assertFalse(args.quiet)
        self.assertIsNone(args.html)

    def test_invalid_fail_on_is_rejected(self):
        with _CapturedStreams():
            self.assertRaises(
                SystemExit, build_parser().parse_args, ["b.json", "--fail-on", "maybe"]
            )


class TestCliBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nta-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_list_analyzers(self):
        code, out, _ = run_cli("--list-analyzers", "ignored.json")
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), analyzer_names())

    def test_list_analyzers_needs_no_board(self):
        """Its help text says "and exit", so it must work on its own."""
        code, out, _ = run_cli("--list-analyzers")
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), analyzer_names())

    def test_missing_file_is_a_usage_error(self):
        code, _, err = run_cli(self.path("nope.json"))
        self.assertEqual(code, 2)
        self.assertIn("no such file", err)

    def test_unknown_analyzer_is_rejected(self):
        code, _, err = run_cli(DEMO_BOARD, "--analyzers", "spof,not_a_rule")
        self.assertEqual(code, 2)
        self.assertIn("not_a_rule", err)

    def test_default_run_prints_a_report_and_succeeds(self):
        code, out, _ = run_cli(DEMO_BOARD)
        self.assertEqual(code, 0, "--fail-on defaults to never")
        self.assertIn("demo_sensor_board", out)
        self.assertIn("FINDINGS", out)

    def test_quiet_suppresses_stdout(self):
        code, out, _ = run_cli(DEMO_BOARD, "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_writes_all_three_formats(self):
        html, data, text = (self.path("r.html"), self.path("r.json"),
                            self.path("r.txt"))
        code, _, err = run_cli(DEMO_BOARD, "--quiet", "--html", html,
                               "--json", data, "--text", text)
        self.assertEqual(code, 0)
        for path in (html, data, text):
            self.assertTrue(os.path.isfile(path), path)
            self.assertGreater(os.path.getsize(path), 500)
            self.assertIn(os.path.basename(path), err)
        with io.open(html, encoding="utf-8") as handle:
            self.assertTrue(handle.read().lstrip().startswith("<!DOCTYPE html>"))
        with io.open(data, encoding="utf-8") as handle:
            self.assertIn("findings", json.load(handle))

    def test_creates_missing_output_directories(self):
        target = self.path(os.path.join("nested", "deeper", "r.json"))
        code, _, _ = run_cli(DEMO_BOARD, "--quiet", "--json", target)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(target))

    def test_fail_on_error_gates_the_pipeline(self):
        # The demo board deliberately contains one ERROR (D2 has no nets).
        code, _, _ = run_cli(DEMO_BOARD, "--quiet", "--fail-on", "error")
        self.assertEqual(code, 1)

    def test_fail_on_warning_is_at_least_as_strict(self):
        code, _, _ = run_cli(DEMO_BOARD, "--quiet", "--fail-on", "warning")
        self.assertEqual(code, 1)

    def test_fail_on_error_passes_when_only_warnings_exist(self):
        board = os.path.join(self.tmp, "warn_only.json")
        with io.open(DEMO_BOARD, encoding="utf-8") as handle:
            data = json.load(handle)
        # D2 is the sole ERROR source; removing it leaves warnings only.
        data["components"] = [c for c in data["components"]
                              if c["reference"] != "D2"]
        data.pop("nets", None)
        with io.open(board, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        code, _, _ = run_cli(board, "--quiet", "--fail-on", "error")
        self.assertEqual(code, 0)

    def test_analyzer_subset_limits_the_output(self):
        _, out, _ = run_cli(DEMO_BOARD, "--analyzers", "statistics")
        self.assertIn("No findings", out)

    def test_fanout_threshold_override(self):
        _, strict, _ = run_cli(DEMO_BOARD, "--fanout-threshold", "2")
        _, lax, _ = run_cli(DEMO_BOARD, "--fanout-threshold", "99")
        self.assertIn("NTA-130", strict)
        self.assertNotIn("NTA-130", lax)

    def test_no_sections_shortens_the_report(self):
        _, full, _ = run_cli(DEMO_BOARD)
        _, brief, _ = run_cli(DEMO_BOARD, "--no-sections")
        self.assertLess(len(brief), len(full))

    def test_include_rails_changes_the_reported_topology(self):
        """The flag must actually reach the config, not be silently ignored.

        Rails inflate the projection - every part touching GND becomes mutually
        adjacent - so the reported redundancy changes. On this board six links
        have no alternate path with rails excluded but only four with them
        included, because ground supplies the missing detour. See
        ``test_graph_model`` for the measurement of why that is misleading.
        """
        _, normal, _ = run_cli(DEMO_BOARD, "--no-sections")
        _, with_rails, _ = run_cli(DEMO_BOARD, "--no-sections", "--include-rails")
        self.assertIn("6 connection(s) have no redundant path", normal)
        self.assertIn("4 connection(s) have no redundant path", with_rails)

    def test_include_rails_inflates_the_projection(self):
        """The same flag, measured on the graph rather than the report text."""
        board = load_board(DEMO_BOARD)
        analyze_board(board)  # assigns net roles, which the projection needs
        excluded = NetlistGraph(board, AnalysisConfig()).describe()
        included = NetlistGraph(
            board, AnalysisConfig(exclude_rails_from_projection=False)
        ).describe()
        self.assertGreater(
            included["projection_edges"], 3 * excluded["projection_edges"]
        )


class TestDemoBoard(unittest.TestCase):
    """Every seeded defect in the demo board must produce its documented finding."""

    @classmethod
    def setUpClass(cls):
        cls.board = load_board(DEMO_BOARD)
        cls.result = analyze_board(cls.board)
        cls.codes = codes(cls.result.findings)

    def test_board_loads_with_the_expected_size(self):
        self.assertEqual(self.board.name, "demo_sensor_board")
        self.assertGreaterEqual(len(self.board.components), 38)
        self.assertGreaterEqual(len(self.board.nets), 33)

    def test_no_analyzer_crashed(self):
        self.assertEqual(self.result.errors, [])

    def test_isolated_loop_is_detected(self):
        self.assertIn("NTA-100", self.codes)

    def test_component_with_no_nets_is_detected(self):
        finding = [f for f in self.result.findings if f.code == "NTA-101"]
        self.assertEqual([f.refs[0] for f in finding], ["D2"])

    def test_single_pad_net_is_detected(self):
        flagged = set()
        for finding in self.result.findings:
            if finding.code == "NTA-110":
                flagged.add(finding.refs[0])
        self.assertIn("TP_SPARE", flagged)

    def test_unrouted_nets_are_detected(self):
        flagged = set()
        for finding in self.result.findings:
            if finding.code == "NTA-120":
                flagged.update(finding.refs)
        self.assertIn("SPI_MISO", flagged)
        self.assertIn("UART_RX", flagged)

    def test_high_fanout_i2c_bus_is_detected(self):
        flagged = set()
        for finding in self.result.findings:
            if finding.code == "NTA-130":
                flagged.update(finding.refs)
        self.assertTrue(flagged & {"SDA", "SCL"}, "expected an I2C net to be flagged")

    def test_ferrite_filtered_rail_has_no_decoupling(self):
        finding = [f for f in self.result.findings if f.code == "NTA-144"]
        self.assertIn("U4", [f.refs[0] for f in finding])

    def test_distant_capacitor_is_detected(self):
        finding = [f for f in self.result.findings if f.code == "NTA-145"]
        self.assertIn("U8", [f.refs[0] for f in finding])

    def test_mcu_is_the_hub(self):
        spof = [f for f in self.result.findings if f.code == "NTA-150"]
        self.assertIn("U1", [f.refs[0] for f in spof])
        central = [f for f in self.result.findings if f.code == "NTA-160"]
        self.assertEqual([f.refs[0] for f in central], ["U1"])

    def test_mounting_holes_are_filtered_out(self):
        # Roles were already assigned by analyze_board() in setUpClass.
        analysed = set(c.reference for c in NetlistGraph(self.board).components)
        self.assertNotIn("H1", analysed)
        self.assertNotIn("H2", analysed)
        self.assertIn("U1", analysed)

    def test_findings_are_not_overwhelming(self):
        """A report nobody reads is worthless, so keep the volume sane."""
        self.assertLessEqual(len(self.result.findings), 20)
        counts = self.result.count_by_severity()
        self.assertEqual(counts[Severity.ERROR], 1)


if __name__ == "__main__":
    unittest.main()
