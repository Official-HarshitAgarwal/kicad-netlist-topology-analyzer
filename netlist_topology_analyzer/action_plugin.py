"""
KiCad action-plugin entry point.

An "action plugin" is the standard way to add a user-invokable command to the
KiCad PCB editor: subclass ``pcbnew.ActionPlugin``, describe yourself in
:meth:`defaults`, do the work in :meth:`Run`, and call ``.register()`` on an
instance at import time. KiCad scans its plugin directories at start-up (and on
*Tools > External Plugins > Refresh Plugins*), imports each package it finds, and
picks up whatever registered itself.

The plugin then appears under *Tools > External Plugins*, and - because
``show_toolbar_button`` is set - as a toolbar button too.

``Run`` deliberately does very little itself. It wires three collaborators
together and translates any failure into a message box:

1. :mod:`..core.board_extractor` converts the live board into neutral data.
2. :func:`..core.engine.analyze_board` performs the analysis.
3. :mod:`..ui.results_dialog` presents the result.

Keeping the entry point this thin is what makes the rest of the plugin testable
without KiCad, and it means a crash surfaces as an explanatory dialog rather
than a silent failure in KiCad's log.
"""

from __future__ import annotations

import os
import traceback

try:  # pragma: no cover - only importable inside KiCad
    import pcbnew

    _ActionPluginBase = pcbnew.ActionPlugin
except (ImportError, AttributeError):  # pragma: no cover
    pcbnew = None  # type: ignore
    _ActionPluginBase = object  # type: ignore

PLUGIN_NAME = "Netlist Connectivity && Topology Analyzer"
PLUGIN_DESCRIPTION = (
    "Builds a graph model of the board's netlist and applies graph algorithms to "
    "find electrical islands, floating nets, single points of failure, "
    "high-fanout signals, missing decoupling and topology hot-spots."
)


def _resource(*parts):
    # type: (*str) -> str
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)


class NetlistTopologyAnalyzerPlugin(_ActionPluginBase):  # type: ignore[misc,valid-type]
    """Registers the analyzer as a PCB-editor action."""

    def defaults(self):
        """Describe the plugin to KiCad.

        Called by KiCad immediately after instantiation, before registration.
        ``icon_file_name`` must be an absolute path; a 24x24 PNG is the
        convention for the toolbar. ``dark_icon_file_name`` is honoured by
        KiCad 7+ and harmless on older versions.
        """
        self.name = PLUGIN_NAME
        self.category = "Analysis"
        self.description = PLUGIN_DESCRIPTION
        self.show_toolbar_button = True
        icon = _resource("resources", "icon.png")
        self.icon_file_name = icon
        self.dark_icon_file_name = icon

    def Run(self):
        """Entry point invoked when the user triggers the plugin."""
        # Imported here rather than at module scope so that a syntax or import
        # error in the analysis code cannot prevent KiCad from registering the
        # plugin - the user gets a readable dialog instead of a missing menu
        # entry.
        try:
            from .core.board_extractor import ExtractionError, extract_board
            from .core.config import AnalysisConfig
            from .core.engine import analyze_board
            from .ui.results_dialog import show_error, show_results
        except Exception:  # pragma: no cover
            self._fallback_error(
                "The plugin failed to load its own modules.\n\n"
                + traceback.format_exc()
            )
            return

        try:
            board_data = extract_board()
            config = AnalysisConfig()
            result = analyze_board(board_data, config)
            show_results(board_data, config=config, result=result)
        except ExtractionError as exc:
            show_error(str(exc))
        except Exception:
            show_error(
                "The analysis failed unexpectedly.\n\n"
                "Please report this along with the details below.\n\n"
                + traceback.format_exc()
            )

    @staticmethod
    def _fallback_error(message):  # pragma: no cover
        """Last-resort error reporting when even the UI module is unavailable."""
        try:
            import wx

            wx.MessageBox(message, "Netlist Topology Analyzer", wx.OK | wx.ICON_ERROR)
        except Exception:
            import sys

            sys.stderr.write(message + "\n")
