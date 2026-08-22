"""
Command-line runner for the analysis engine.

The plugin's value does not depend on the GUI: the same engine can run head-less
against a netlist snapshot in JSON form. That matters for three reasons.

* **Testing** - the engine is exercised in CI on a stock CPython, no KiCad
  install and no display server required.
* **Regression tracking** - JSON output from two board revisions can be diffed
  to see which findings a change introduced or fixed.
* **Automation** - the exit code is non-zero when errors are found, so the
  analysis can gate a build pipeline.

Usage::

    python -m netlist_topology_analyzer.cli examples/demo_board.json
    python -m netlist_topology_analyzer.cli board.json --html report.html
    python -m netlist_topology_analyzer.cli board.json --json result.json --quiet
    python -m netlist_topology_analyzer.cli board.json --fail-on warning
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

# Allow running this file directly (``python cli.py ...``) as well as via
# ``python -m netlist_topology_analyzer.cli`` by making the package importable.
if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from netlist_topology_analyzer.core import (  # type: ignore
        AnalysisConfig,
        BoardData,
        NetlistGraph,
        Severity,
        analyze_board,
        analyzer_names,
        render_html,
        render_json,
        render_text,
    )
else:
    from .core import (
        AnalysisConfig,
        BoardData,
        NetlistGraph,
        Severity,
        analyze_board,
        analyzer_names,
        render_html,
        render_json,
        render_text,
    )


def build_parser():
    # type: () -> argparse.ArgumentParser
    parser = argparse.ArgumentParser(
        prog="netlist_topology_analyzer",
        description=(
            "Analyse the connectivity and topology of a KiCad netlist snapshot "
            "(JSON) without launching KiCad."
        ),
    )
    parser.add_argument(
        "board",
        nargs="?",
        # Optional so that --list-analyzers works on its own, as its help text
        # implies. main() enforces the argument for every other invocation.
        help="Path to a board snapshot in JSON form (see examples/demo_board.json).",
    )
    parser.add_argument("--html", metavar="PATH", help="Write an HTML report here.")
    parser.add_argument("--json", metavar="PATH", help="Write the JSON result here.")
    parser.add_argument("--text", metavar="PATH", help="Write the text report here.")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print the text report to stdout.",
    )
    parser.add_argument(
        "--no-sections",
        action="store_true",
        help="Omit the detail tables from the text report.",
    )
    parser.add_argument(
        "--analyzers",
        metavar="NAMES",
        help=(
            "Comma-separated subset of analyzers to run. Available: "
            + ", ".join(analyzer_names())
        ),
    )
    parser.add_argument(
        "--fanout-threshold",
        type=int,
        default=None,
        help="Override the high-fanout warning threshold.",
    )
    parser.add_argument(
        "--include-rails",
        action="store_true",
        help=(
            "Keep power/ground nets in the component projection. Mostly useful "
            "for demonstrating why excluding them is necessary."
        ),
    )
    parser.add_argument(
        "--fail-on",
        choices=("never", "error", "warning"),
        default="never",
        help="Exit with status 1 when findings at this level or worse exist.",
    )
    parser.add_argument(
        "--list-analyzers",
        action="store_true",
        help="Print the registered analyzers and exit.",
    )
    return parser


def load_board(path):
    # type: (str) -> BoardData
    with io.open(path, "r", encoding="utf-8") as handle:
        return BoardData.from_dict(json.load(handle))


def _write(path, text):
    # type: (str, str) -> None
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def main(argv=None):
    # type: (object) -> int
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_analyzers:
        for name in analyzer_names():
            sys.stdout.write(name + "\n")
        return 0

    # Enforced here rather than by argparse because --list-analyzers needs no
    # board; making the positional optional is what allows that.
    if not args.board:
        sys.stderr.write("error: a board snapshot path is required\n")
        return 2

    if not os.path.isfile(args.board):
        sys.stderr.write("error: no such file: {0}\n".format(args.board))
        return 2

    config = AnalysisConfig()
    if args.analyzers:
        requested = [n.strip() for n in args.analyzers.split(",") if n.strip()]
        unknown = [n for n in requested if n not in analyzer_names()]
        if unknown:
            sys.stderr.write(
                "error: unknown analyzer(s): {0}\n".format(", ".join(unknown))
            )
            return 2
        config.enabled_analyzers = requested
    if args.fanout_threshold is not None:
        config.high_fanout_threshold = args.fanout_threshold
    if args.include_rails:
        config.exclude_rails_from_projection = False

    board = load_board(args.board)
    result = analyze_board(board, config)

    text = render_text(result, include_sections=not args.no_sections)
    if not args.quiet:
        sys.stdout.write(text + "\n")

    if args.text:
        _write(args.text, text)
    if args.json:
        _write(args.json, render_json(result))
    if args.html:
        graph = NetlistGraph(board, config)
        _write(args.html, render_html(result, graph))

    for path, label in ((args.text, "text"), (args.json, "JSON"), (args.html, "HTML")):
        if path:
            sys.stderr.write("wrote {0} report: {1}\n".format(label, path))

    counts = result.count_by_severity()
    if args.fail_on == "error" and counts.get(Severity.ERROR, 0):
        return 1
    if args.fail_on == "warning" and (
        counts.get(Severity.ERROR, 0) or counts.get(Severity.WARNING, 0)
    ):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
