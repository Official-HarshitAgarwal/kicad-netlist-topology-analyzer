"""
KiCad Netlist Connectivity & Topology Analyzer.

A KiCad 9 action plugin that models a board's netlist as a graph and applies
graph-theoretic analysis to surface connectivity and topology problems that
KiCad's own DRC/ERC does not look for.

KiCad imports this package when it scans its plugin directories, so registration
happens here as an import side effect. Registration is wrapped in a broad
``try``/``except`` on purpose: an exception escaping a plugin's ``__init__``
during KiCad's scan can disrupt discovery of *other* plugins, so a failure here
is reported to stderr (visible in KiCad's Python console) rather than raised.
"""

from __future__ import annotations

__version__ = "0.1.0"
__author__ = "FOSSEE eSim Semester-Long Internship - Autumn 2026, Task 6"
__license__ = "MIT"

#: Minimum KiCad version this plugin targets.
KICAD_TARGET_VERSION = "9.0"

try:
    from .action_plugin import NetlistTopologyAnalyzerPlugin

    # ``register()`` is provided by pcbnew.ActionPlugin; it is absent when the
    # package is imported outside KiCad (during tests or head-less CLI use),
    # in which case there is simply nothing to register.
    if hasattr(NetlistTopologyAnalyzerPlugin, "register"):
        NetlistTopologyAnalyzerPlugin().register()
except Exception:  # pragma: no cover - never break KiCad's plugin scan
    import sys
    import traceback

    sys.stderr.write(
        "[netlist_topology_analyzer] failed to register the action plugin:\n"
        + traceback.format_exc()
    )
