"""
wxPython results dialog.

KiCad bundles wxPython in its own Python environment (its whole GUI is built on
wxWidgets), so a plugin can rely on ``wx`` being importable without adding any
dependency. ``wx`` is nonetheless imported defensively here: the module must stay
importable outside KiCad so the rest of the package can be tested head-less.

The dialog is a thin presentation layer. It owns no analysis logic - it holds a
:class:`~..core.model.BoardData` snapshot plus an
:class:`~..core.config.AnalysisConfig` and calls the same
:func:`~..core.engine.analyze_board` entry point the CLI uses. That guarantees
the GUI and the command line can never disagree about results.

Two features are worth calling out:

* **Re-run with live thresholds.** The fanout threshold and the rail-exclusion
  switch are editable, so a user can see immediately how a heuristic changes the
  findings. Toggling rail exclusion off is also the clearest possible
  demonstration of *why* rails must be excluded: articulation points collapse to
  zero because ground connects everything to everything.

* **Cross-probing.** Selecting a finding selects and zooms to the referenced
  footprint in the PCB editor, which turns the report from a list of names into
  a navigable review tool.
"""

from __future__ import annotations

import os
import webbrowser
from typing import List, Optional

try:  # pragma: no cover - present inside KiCad
    import wx
except ImportError:  # pragma: no cover
    wx = None  # type: ignore

try:  # pragma: no cover - present inside KiCad
    import pcbnew
except ImportError:  # pragma: no cover
    pcbnew = None  # type: ignore

from ..core import (
    AnalysisConfig,
    NetlistGraph,
    Severity,
    analyze_board,
    render_html,
    render_json,
    render_text,
)
from ..core.report import METRIC_LABELS, SUMMARY_METRICS

#: Row background colours by severity, chosen to stay legible on light themes.
_SEVERITY_COLOURS = {
    Severity.ERROR: (253, 236, 235),
    Severity.WARNING: (255, 246, 229),
    Severity.INFO: (255, 255, 255),
}


def _monospace_font():
    # type: () -> object
    return wx.Font(
        9,
        wx.FONTFAMILY_TELETYPE,
        wx.FONTSTYLE_NORMAL,
        wx.FONTWEIGHT_NORMAL,
    )


class ResultsDialog(wx.Dialog if wx is not None else object):  # type: ignore[misc]
    """Presents an analysis result and allows re-running and exporting it."""

    def __init__(self, parent, board_data, config=None, result=None):
        if wx is None:  # pragma: no cover
            raise RuntimeError("wxPython is unavailable; cannot show the dialog.")

        wx.Dialog.__init__(
            self,
            parent,
            title="Netlist Connectivity & Topology Analyzer",
            size=wx.Size(1000, 720),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )

        self.board_data = board_data
        self.config = config or AnalysisConfig()
        self.result = result or analyze_board(board_data, self.config)
        self._findings = self.result.sorted_findings()

        self._build_ui()
        self._populate()
        self.CentreOnParent()

    # -- construction -----------------------------------------------------

    def _build_ui(self):
        outer = wx.BoxSizer(wx.VERTICAL)

        # Header ----------------------------------------------------------
        self.header = wx.StaticText(self, label="")
        header_font = self.header.GetFont()
        header_font.SetPointSize(header_font.GetPointSize() + 2)
        header_font.SetWeight(wx.FONTWEIGHT_BOLD)
        self.header.SetFont(header_font)
        outer.Add(self.header, 0, wx.ALL, 12)

        self.subheader = wx.StaticText(self, label="")
        outer.Add(self.subheader, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        # Notebook --------------------------------------------------------
        self.notebook = wx.Notebook(self)

        # Findings tab
        findings_panel = wx.Panel(self.notebook)
        self.findings_list = wx.ListCtrl(
            findings_panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_NONE
        )
        for index, (title, width) in enumerate(
            (
                ("Severity", 90),
                ("Code", 80),
                ("Category", 130),
                ("Finding", 430),
                ("Refs", 180),
            )
        ):
            self.findings_list.InsertColumn(index, title, width=width)
        self.detail_text = wx.TextCtrl(
            findings_panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP | wx.BORDER_NONE,
        )
        self.detail_text.SetMinSize(wx.Size(-1, 110))

        findings_sizer = wx.BoxSizer(wx.VERTICAL)
        findings_sizer.Add(self.findings_list, 1, wx.EXPAND | wx.ALL, 4)
        findings_sizer.Add(
            wx.StaticText(findings_panel, label="Details"), 0, wx.LEFT | wx.TOP, 6
        )
        findings_sizer.Add(self.detail_text, 0, wx.EXPAND | wx.ALL, 4)
        findings_panel.SetSizer(findings_sizer)
        self.notebook.AddPage(findings_panel, "Findings")

        # Metrics tab
        metrics_panel = wx.Panel(self.notebook)
        self.metrics_list = wx.ListCtrl(
            metrics_panel, style=wx.LC_REPORT | wx.BORDER_NONE
        )
        self.metrics_list.InsertColumn(0, "Metric", width=340)
        self.metrics_list.InsertColumn(1, "Value", width=560)
        metrics_sizer = wx.BoxSizer(wx.VERTICAL)
        metrics_sizer.Add(self.metrics_list, 1, wx.EXPAND | wx.ALL, 4)
        metrics_panel.SetSizer(metrics_sizer)
        self.notebook.AddPage(metrics_panel, "Metrics")

        # Full report tab
        report_panel = wx.Panel(self.notebook)
        self.report_text = wx.TextCtrl(
            report_panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL | wx.BORDER_NONE,
        )
        self.report_text.SetFont(_monospace_font())
        report_sizer = wx.BoxSizer(wx.VERTICAL)
        report_sizer.Add(self.report_text, 1, wx.EXPAND | wx.ALL, 4)
        report_panel.SetSizer(report_sizer)
        self.notebook.AddPage(report_panel, "Full report")

        outer.Add(self.notebook, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        # Options ---------------------------------------------------------
        options = wx.StaticBoxSizer(
            wx.StaticBox(self, label="Analysis options"), wx.HORIZONTAL
        )
        options.Add(
            wx.StaticText(self, label="High-fanout threshold:"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT,
            6,
        )
        self.fanout_spin = wx.SpinCtrl(
            self, min=2, max=256, initial=self.config.high_fanout_threshold
        )
        options.Add(self.fanout_spin, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        self.exclude_rails_cb = wx.CheckBox(
            self, label="Exclude power/ground from topology"
        )
        self.exclude_rails_cb.SetValue(self.config.exclude_rails_from_projection)
        self.exclude_rails_cb.SetToolTip(
            "Rails connect nearly every part, so leaving them in makes the "
            "projection almost fully connected and hides all signal structure. "
            "Untick to see that effect."
        )
        options.Add(self.exclude_rails_cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        self.crossprobe_cb = wx.CheckBox(self, label="Select part on board")
        self.crossprobe_cb.SetValue(pcbnew is not None)
        self.crossprobe_cb.Enable(pcbnew is not None)
        self.crossprobe_cb.SetToolTip(
            "Selecting a finding also selects and zooms to the referenced "
            "footprint in the PCB editor."
        )
        options.Add(self.crossprobe_cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        self.rerun_button = wx.Button(self, label="Re-run analysis")
        options.Add(self.rerun_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        outer.Add(options, 0, wx.EXPAND | wx.ALL, 12)

        # Buttons ---------------------------------------------------------
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.html_button = wx.Button(self, label="Save HTML report...")
        self.json_button = wx.Button(self, label="Save JSON...")
        self.copy_button = wx.Button(self, label="Copy report")
        self.close_button = wx.Button(self, wx.ID_CLOSE, label="Close")
        buttons.Add(self.html_button, 0, wx.RIGHT, 8)
        buttons.Add(self.json_button, 0, wx.RIGHT, 8)
        buttons.Add(self.copy_button, 0, wx.RIGHT, 8)
        buttons.AddStretchSpacer()
        buttons.Add(self.close_button, 0)
        outer.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.SetSizer(outer)

        # Events ----------------------------------------------------------
        self.findings_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_select_finding)
        self.rerun_button.Bind(wx.EVT_BUTTON, self.on_rerun)
        self.html_button.Bind(wx.EVT_BUTTON, self.on_save_html)
        self.json_button.Bind(wx.EVT_BUTTON, self.on_save_json)
        self.copy_button.Bind(wx.EVT_BUTTON, self.on_copy)
        self.close_button.Bind(wx.EVT_BUTTON, lambda _evt: self.EndModal(wx.ID_CLOSE))

    # -- population -------------------------------------------------------

    def _populate(self):
        counts = self.result.count_by_severity()
        self.header.SetLabel(
            "{0}  -  {1} error(s), {2} warning(s), {3} note(s)".format(
                self.result.board_name,
                counts.get(Severity.ERROR, 0),
                counts.get(Severity.WARNING, 0),
                counts.get(Severity.INFO, 0),
            )
        )
        metrics = self.result.metrics
        self.subheader.SetLabel(
            "{0} components, {1} nets, {2} pads analysed in {3} s".format(
                metrics.get("components_analyzed", "?"),
                metrics.get("nets_analyzed", "?"),
                metrics.get("pads_total", "?"),
                metrics.get("analysis_time_s", "?"),
            )
        )

        # Findings
        self._findings = self.result.sorted_findings()
        self.findings_list.DeleteAllItems()
        for row, finding in enumerate(self._findings):
            self.findings_list.InsertItem(row, finding.severity)
            self.findings_list.SetItem(row, 1, finding.code)
            self.findings_list.SetItem(row, 2, finding.category)
            self.findings_list.SetItem(row, 3, finding.title)
            self.findings_list.SetItem(row, 4, ", ".join(str(r) for r in finding.refs))
            colour = _SEVERITY_COLOURS.get(finding.severity)
            if colour:
                self.findings_list.SetItemBackgroundColour(row, wx.Colour(*colour))
        if not self._findings:
            self.detail_text.SetValue(
                "No findings. The netlist passed every enabled check."
            )
        else:
            self.detail_text.SetValue("Select a finding to see its explanation.")

        # Metrics
        self.metrics_list.DeleteAllItems()
        ordered = [k for k in SUMMARY_METRICS if k in metrics]
        ordered += sorted(k for k in metrics if k not in SUMMARY_METRICS)
        for row, key in enumerate(ordered):
            self.metrics_list.InsertItem(
                row, METRIC_LABELS.get(key, key.replace("_", " ").capitalize())
            )
            self.metrics_list.SetItem(row, 1, str(metrics[key]))

        # Text report
        self.report_text.SetValue(render_text(self.result))
        self.report_text.SetInsertionPoint(0)

    # -- events -----------------------------------------------------------

    def on_select_finding(self, event):
        index = event.GetIndex()
        if index < 0 or index >= len(self._findings):
            return
        finding = self._findings[index]
        lines = ["{0}  {1}".format(finding.code, finding.title), ""]
        if finding.detail:
            lines.append(finding.detail)
        if finding.refs:
            lines.append("")
            lines.append("References: {0}".format(", ".join(str(r) for r in finding.refs)))
        self.detail_text.SetValue("\n".join(lines))

        if self.crossprobe_cb.IsChecked():
            self._cross_probe(finding.refs)

    def _cross_probe(self, refs):
        # type: (List[str]) -> None
        """Select and zoom to the first referenced footprint in the PCB editor.

        Entirely best-effort: cross-probing is a convenience, so any binding
        difference must not break the dialog. Failures are swallowed silently
        because a popup on every selection would be far more annoying than a
        feature quietly not working.
        """
        if pcbnew is None or not refs:
            return
        try:
            board = pcbnew.GetBoard()
            if board is None:
                return
            # Clear any previous selection so the new one is unambiguous.
            for footprint in board.GetFootprints():
                try:
                    footprint.ClearSelected()
                except Exception:
                    pass
            for ref in refs:
                footprint = None
                finder = getattr(board, "FindFootprintByReference", None)
                if finder is not None:
                    footprint = finder(str(ref))
                if footprint is None:
                    continue
                try:
                    footprint.SetSelected()
                except Exception:
                    pass
                focus = getattr(pcbnew, "FocusOnItem", None)
                if focus is not None:
                    focus(footprint)
                break
            refresh = getattr(pcbnew, "Refresh", None)
            if refresh is not None:
                refresh()
        except Exception:
            pass

    def on_rerun(self, _event):
        self.config.high_fanout_threshold = int(self.fanout_spin.GetValue())
        self.config.exclude_rails_from_projection = bool(
            self.exclude_rails_cb.GetValue()
        )
        busy = wx.BusyCursor()
        try:
            self.result = analyze_board(self.board_data, self.config)
        finally:
            del busy
        self._populate()

    def _suggested_name(self, extension):
        # type: (str) -> str
        base = str(self.result.board_name) or "board"
        return "{0}_topology_report.{1}".format(base, extension)

    def on_save_html(self, _event):
        graph = NetlistGraph(self.board_data, self.config)
        content = render_html(self.result, graph)
        path = self._ask_save_path(
            self._suggested_name("html"), "HTML files (*.html)|*.html"
        )
        if not path:
            return
        if self._write(path, content):
            self._offer_open(path)

    def on_save_json(self, _event):
        path = self._ask_save_path(
            self._suggested_name("json"), "JSON files (*.json)|*.json"
        )
        if not path:
            return
        self._write(path, render_json(self.result))

    def on_copy(self, _event):
        if not wx.TheClipboard.Open():
            return
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(render_text(self.result)))
        finally:
            wx.TheClipboard.Close()
        wx.MessageBox(
            "The text report has been copied to the clipboard.",
            "Copied",
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

    # -- helpers ----------------------------------------------------------

    def _ask_save_path(self, default_name, wildcard):
        # type: (str, str) -> Optional[str]
        dialog = wx.FileDialog(
            self,
            message="Save report",
            defaultFile=default_name,
            wildcard=wildcard,
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return None
            return dialog.GetPath()
        finally:
            dialog.Destroy()

    def _write(self, path, content):
        # type: (str, str) -> bool
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            return True
        except OSError as exc:
            wx.MessageBox(
                "Could not write the file:\n{0}".format(exc),
                "Save failed",
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return False

    def _offer_open(self, path):
        # type: (str) -> None
        answer = wx.MessageBox(
            "Report saved to:\n{0}\n\nOpen it in your browser now?".format(path),
            "Report saved",
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        )
        if answer == wx.YES:
            try:
                webbrowser.open("file://" + os.path.abspath(path))
            except Exception:
                pass


def show_results(board_data, config=None, result=None, parent=None):
    """Create and show the results dialog modally."""
    if wx is None:  # pragma: no cover
        raise RuntimeError("wxPython is unavailable; cannot show the dialog.")
    if parent is None:
        app = wx.GetApp()
        parent = app.GetTopWindow() if app is not None else None
    dialog = ResultsDialog(parent, board_data, config=config, result=result)
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()


def show_error(message, parent=None):
    """Report a fatal problem to the user with a message box."""
    if wx is None:  # pragma: no cover
        raise RuntimeError(message)
    if parent is None:
        app = wx.GetApp()
        parent = app.GetTopWindow() if app is not None else None
    wx.MessageBox(
        message,
        "Netlist Topology Analyzer",
        wx.OK | wx.ICON_ERROR,
        parent,
    )
