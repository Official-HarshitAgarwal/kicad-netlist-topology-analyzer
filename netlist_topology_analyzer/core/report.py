"""
Report rendering: turning an :class:`~.model.AnalysisResult` into output.

Three formats are produced from the same result object:

* **Plain text** - shown in the GUI dialog and printed by the CLI.
* **HTML** - a self-contained file (inline CSS, inline SVG, no external
  requests) suitable for archiving with a design review or attaching to a
  report.
* **JSON** - machine-readable, for diffing between board revisions or wiring the
  analysis into CI.

The section renderer is deliberately *generic*: it takes a list of uniform dicts
and derives the table headers from the keys. That means a newly added analyzer's
tables appear in every output format without a single line of reporting code
being touched - the concrete payoff of the registry architecture.
"""

from __future__ import annotations

import datetime
import html
import math
from typing import Dict, List, Optional, Sequence

from .graph_model import NetlistGraph, component_node, node_label
from .model import AnalysisResult, Severity

#: Friendly headings for the known section keys; unknown keys fall back to a
#: title-cased version of the key itself.
SECTION_TITLES = {
    "rails": "Power and ground rails",
    "islands": "Electrical islands",
    "fanout": "Highest-fanout signal nets",
    "power": "Power and decoupling per active device",
    "spof": "Articulation points (single points of failure)",
    "bridges": "Bridge connections (no redundant path)",
    "degree": "Most-connected components",
    "centrality": "Most central components (betweenness)",
}

#: Order sections appear in reports. Keys not listed are appended afterwards.
SECTION_ORDER = (
    "rails",
    "islands",
    "power",
    "fanout",
    "spof",
    "bridges",
    "degree",
    "centrality",
)

METRIC_LABELS = {
    "board_name": "Board",
    "components_total": "Components (total)",
    "components_analyzed": "Components analysed",
    "nets_total": "Nets (total)",
    "nets_analyzed": "Nets analysed",
    "pads_total": "Pads (total)",
    "pads_unconnected": "Pads with no net",
    "power_nets": "Power nets",
    "ground_nets": "Ground nets",
    "signal_nets": "Signal nets",
    "unconnected_nets": "Unconnected nets",
    "routed_nets": "Routed nets",
    "unrouted_nets": "Unrouted nets",
    "single_pad_nets": "Single-pad nets",
    "total_track_length_mm": "Total track length (mm)",
    "electrical_islands": "Electrical islands",
    "max_signal_fanout": "Max signal fanout",
    "ics_checked": "Active devices checked",
    "capacitors_found": "Capacitors found",
    "articulation_points": "Articulation points",
    "bridge_connections": "Bridge connections",
    "signal_graph_diameter": "Signal graph diameter (hops)",
    "largest_signal_cluster": "Largest signal cluster",
    "projection_nodes": "Projection nodes",
    "projection_edges": "Projection edges",
    "bipartite_nodes": "Incidence graph nodes",
    "bipartite_edges": "Incidence graph edges",
    "projection_nets_used": "Nets used in projection",
    "analyzers_run": "Analyzers run",
    "analysis_time_s": "Analysis time (s)",
    "errors_found": "Errors",
    "warnings_found": "Warnings",
}

#: Metrics shown in the headline summary, in this order.
SUMMARY_METRICS = (
    "board_name",
    "components_analyzed",
    "nets_analyzed",
    "pads_total",
    "power_nets",
    "ground_nets",
    "signal_nets",
    "electrical_islands",
    "articulation_points",
    "bridge_connections",
    "max_signal_fanout",
    "signal_graph_diameter",
    "unrouted_nets",
    "total_track_length_mm",
    "analysis_time_s",
)


def _timestamp():
    # type: () -> str
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _metric_label(key):
    # type: (str) -> str
    return METRIC_LABELS.get(key, key.replace("_", " ").capitalize())


def _ordered_section_keys(sections):
    # type: (Dict[str, object]) -> List[str]
    known = [k for k in SECTION_ORDER if k in sections]
    extra = sorted(k for k in sections if k not in SECTION_ORDER)
    return known + extra


def _as_rows(value):
    # type: (object) -> Optional[List[Dict]]
    """Normalise a section payload to a list of uniform dicts, or None."""
    if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
        return value  # type: ignore[return-value]
    return None


def _as_text(value):
    # type: (object) -> str
    """Flatten a non-tabular section payload into one readable string.

    Sections are normally a list of uniform dicts, but the report must never
    silently discard what an analyzer chose to publish - a new rule emitting a
    plain string or a scalar would otherwise vanish without trace. Anything
    that is not a table is rendered as text instead of being dropped. An empty
    payload returns ``""``, which callers treat as "omit the section".
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return ", ".join("{0}: {1}".format(k, value[k]) for k in sorted(value))
    return str(value)


def _section_title(key):
    # type: (str) -> str
    return SECTION_TITLES.get(key, key.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------


def render_text(result, include_sections=True):
    # type: (AnalysisResult, bool) -> str
    """Render a fixed-width text report."""
    lines = []
    add = lines.append

    add("=" * 78)
    add("KiCad Netlist Connectivity & Topology Analyzer")
    add("Board: {0}".format(result.board_name))
    add("Generated: {0}".format(_timestamp()))
    add("=" * 78)
    add("")

    counts = result.count_by_severity()
    add(
        "SUMMARY: {0} error(s), {1} warning(s), {2} note(s)".format(
            counts.get(Severity.ERROR, 0),
            counts.get(Severity.WARNING, 0),
            counts.get(Severity.INFO, 0),
        )
    )
    add("")

    add("-" * 78)
    add("METRICS")
    add("-" * 78)
    for key in SUMMARY_METRICS:
        if key in result.metrics:
            add("  {0:<34} {1}".format(_metric_label(key) + ":", result.metrics[key]))
    add("")

    add("-" * 78)
    add("FINDINGS")
    add("-" * 78)
    findings = result.sorted_findings()
    if not findings:
        add("  No findings. The netlist passed every enabled check.")
    else:
        current_category = None
        for finding in findings:
            if finding.category != current_category:
                current_category = finding.category
                add("")
                add("[{0}]".format(current_category))
            add(
                "  {0:<7} {1}  {2}".format(
                    finding.severity, finding.code, finding.title
                )
            )
            if finding.detail:
                for chunk in _wrap(finding.detail, 70):
                    add("          {0}".format(chunk))
    add("")

    if include_sections and result.sections:
        for key in _ordered_section_keys(result.sections):
            payload = result.sections[key]
            rows = _as_rows(payload)
            if rows is None:
                text = _as_text(payload)
                if not text:
                    continue
                add("-" * 78)
                add(_section_title(key).upper())
                add("-" * 78)
                for chunk in _wrap(text, 74):
                    add("  {0}".format(chunk))
                add("")
                continue
            add("-" * 78)
            add(_section_title(key).upper())
            add("-" * 78)
            add(_text_table(rows))
            add("")

    if result.errors:
        add("-" * 78)
        add("ANALYZER ERRORS")
        add("-" * 78)
        for err in result.errors:
            add("  {0}".format(err))
        add("")

    return "\n".join(lines)


def _wrap(text, width):
    # type: (str, int) -> List[str]
    words = text.split()
    out = []
    line = ""
    for word in words:
        candidate = (line + " " + word).strip()
        if len(candidate) > width and line:
            out.append(line)
            line = word
        else:
            line = candidate
    if line:
        out.append(line)
    return out


def _text_table(rows):
    # type: (Sequence[Dict]) -> str
    headers = list(rows[0].keys())
    widths = {}
    for header in headers:
        widths[header] = max(
            len(str(header)), max(len(str(r.get(header, ""))) for r in rows)
        )
        widths[header] = min(widths[header], 40)

    def fmt(values):
        cells = []
        for header in headers:
            text = str(values.get(header, ""))
            if len(text) > widths[header]:
                text = text[: widths[header] - 3] + "..."
            cells.append(text.ljust(widths[header]))
        return "  " + " | ".join(cells)

    out = [fmt(dict((h, h) for h in headers))]
    out.append("  " + "-+-".join("-" * widths[h] for h in headers))
    for row in rows:
        out.append(fmt(row))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# SVG topology diagram
# ---------------------------------------------------------------------------


def render_svg(graph, max_nodes=140, size=760):
    # type: (NetlistGraph, int, int) -> str
    """Render the signal-topology projection as a standalone SVG.

    A circular (shell) layout is used rather than a force-directed one: it needs
    no iterative solver, is fully deterministic, and makes node degree visually
    obvious. Nodes are ordered by degree so hubs land next to each other, which
    keeps the busiest edges short.

    Returns an empty string when the graph is empty or too large to be legible.
    """
    adj = graph.component_graph()
    if not adj:
        return ""

    degree = dict((n, len(v)) for n, v in adj.items())
    nodes = [n for n in adj if degree[n] > 0]
    if not nodes:
        return ""
    if len(nodes) > max_nodes:
        return ""

    nodes.sort(key=lambda n: (-degree[n], n))
    count = len(nodes)
    radius = size * 0.40
    centre = size / 2.0
    max_degree = max(degree[n] for n in nodes) or 1

    positions = {}
    for index, node in enumerate(nodes):
        angle = 2.0 * math.pi * index / count - math.pi / 2.0
        positions[node] = (
            centre + radius * math.cos(angle),
            centre + radius * math.sin(angle),
        )

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {0} {1}" '
        'width="100%" height="auto" role="img" '
        'aria-label="Signal topology graph">'.format(size, size),
        "<style>"
        ".edge{stroke:#c8ccd4;stroke-width:1}"
        ".node{fill:#3b6ea5;stroke:#fff;stroke-width:1.5}"
        ".hub{fill:#c1440e}"
        ".lbl{font:9px 'DejaVu Sans',sans-serif;fill:#333}"
        "</style>",
        '<rect width="{0}" height="{0}" fill="#fbfbfd"/>'.format(size),
    ]

    drawn = set()
    for node in nodes:
        x1, y1 = positions[node]
        for other in adj[node]:
            if other not in positions:
                continue
            key = tuple(sorted((node, other)))
            if key in drawn:
                continue
            drawn.add(key)
            x2, y2 = positions[other]
            parts.append(
                '<line class="edge" x1="{0:.1f}" y1="{1:.1f}" '
                'x2="{2:.1f}" y2="{3:.1f}"/>'.format(x1, y1, x2, y2)
            )

    for node in nodes:
        x, y = positions[node]
        deg = degree[node]
        r = 3.0 + 5.0 * (float(deg) / max_degree)
        cls = "node hub" if deg >= max(2, int(round(0.6 * max_degree))) else "node"
        label = html.escape(node_label(node))
        parts.append(
            '<circle class="{0}" cx="{1:.1f}" cy="{2:.1f}" r="{3:.1f}">'
            "<title>{4} - {5} connection(s)</title></circle>".format(
                cls, x, y, r, label, deg
            )
        )
        # Push labels outward along the radius so they do not overlap the node.
        lx = centre + (radius + 16) * math.cos(math.atan2(y - centre, x - centre))
        ly = centre + (radius + 16) * math.sin(math.atan2(y - centre, x - centre))
        anchor = "start" if lx >= centre else "end"
        parts.append(
            '<text class="lbl" x="{0:.1f}" y="{1:.1f}" text-anchor="{2}">'
            "{3}</text>".format(lx, ly + 3, anchor, label)
        )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_HTML_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin:0; padding:32px; background:#f5f6f8; color:#1d2228;
       font:14px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; }
.wrap { max-width:1080px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; letter-spacing:-.01em; }
h2 { font-size:16px; margin:32px 0 10px; padding-bottom:6px;
     border-bottom:2px solid #e3e6ea; }
.sub { color:#6b7280; font-size:13px; margin:0 0 24px; }
.card { background:#fff; border:1px solid #e3e6ea; border-radius:8px;
        padding:18px 20px; margin-bottom:18px; }
.badges { display:flex; gap:10px; flex-wrap:wrap; margin:0 0 22px; }
.badge { border-radius:6px; padding:8px 14px; font-weight:600; font-size:13px;
         border:1px solid transparent; }
.b-err { background:#fdeceb; color:#8c1d18; border-color:#f5c6c2; }
.b-warn{ background:#fff6e5; color:#7a4b00; border-color:#f5dfae; }
.b-info{ background:#eaf1fb; color:#1b4a80; border-color:#c5d8f1; }
.b-ok  { background:#e9f6ec; color:#1c5c2e; border-color:#bfe3c8; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th,td { text-align:left; padding:7px 10px; border-bottom:1px solid #edeff2;
        vertical-align:top; }
th { background:#f7f8fa; font-weight:600; color:#414a54;
     text-transform:uppercase; font-size:11px; letter-spacing:.04em; }
tr:last-child td { border-bottom:none; }
.metrics { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr));
           gap:10px; }
.metric { background:#fff; border:1px solid #e3e6ea; border-radius:6px;
          padding:10px 12px; }
.metric .k { font-size:11px; color:#6b7280; text-transform:uppercase;
             letter-spacing:.04em; }
.metric .v { font-size:17px; font-weight:600; margin-top:2px;
             word-break:break-word; }
.sev { font-weight:700; font-size:11px; padding:2px 7px; border-radius:4px;
       white-space:nowrap; }
.sev-ERROR { background:#fdeceb; color:#8c1d18; }
.sev-WARNING { background:#fff6e5; color:#7a4b00; }
.sev-INFO { background:#eaf1fb; color:#1b4a80; }
code { background:#f2f3f5; padding:1px 5px; border-radius:3px; font-size:12px; }
.detail { color:#4b5563; font-size:12.5px; }
.refs { color:#6b7280; font-size:12px; margin-top:3px; }
.empty { color:#6b7280; font-style:italic; }
.legend { color:#6b7280; font-size:12px; margin-top:8px; }
footer { color:#8b929c; font-size:12px; margin-top:36px; text-align:center; }
"""


def render_html(result, graph=None):
    # type: (AnalysisResult, Optional[NetlistGraph]) -> str
    """Render a fully self-contained HTML report (no external resources)."""
    esc = html.escape
    counts = result.count_by_severity()
    out = []
    add = out.append

    add("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>")
    add("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    add("<title>Topology report - {0}</title>".format(esc(str(result.board_name))))
    add("<style>{0}</style></head><body><div class='wrap'>".format(_HTML_CSS))

    add("<h1>Netlist Connectivity &amp; Topology Report</h1>")
    add(
        "<p class='sub'>Board <strong>{0}</strong> &middot; generated {1}</p>".format(
            esc(str(result.board_name)), _timestamp()
        )
    )

    # Badges
    add("<div class='badges'>")
    n_err = counts.get(Severity.ERROR, 0)
    n_warn = counts.get(Severity.WARNING, 0)
    n_info = counts.get(Severity.INFO, 0)
    if n_err == 0 and n_warn == 0:
        add("<div class='badge b-ok'>All checks passed</div>")
    add("<div class='badge b-err'>{0} error(s)</div>".format(n_err))
    add("<div class='badge b-warn'>{0} warning(s)</div>".format(n_warn))
    add("<div class='badge b-info'>{0} note(s)</div>".format(n_info))
    add("</div>")

    # Metrics
    add("<h2>Summary metrics</h2><div class='metrics'>")
    for key in SUMMARY_METRICS:
        if key not in result.metrics:
            continue
        add(
            "<div class='metric'><div class='k'>{0}</div>"
            "<div class='v'>{1}</div></div>".format(
                esc(_metric_label(key)), esc(str(result.metrics[key]))
            )
        )
    add("</div>")

    # Topology diagram
    if graph is not None:
        svg = render_svg(graph)
        if svg:
            add("<h2>Signal topology</h2><div class='card'>")
            add(svg)
            add(
                "<p class='legend'>Components projected onto a signal-only graph "
                "(power and ground rails excluded). Node size and colour follow "
                "connection count; hover a node for its degree.</p>"
            )
            add("</div>")

    # Findings
    add("<h2>Findings</h2>")
    findings = result.sorted_findings()
    if not findings:
        add(
            "<div class='card'><p class='empty'>No findings. The netlist passed "
            "every enabled check.</p></div>"
        )
    else:
        add("<div class='card'><table>")
        add(
            "<tr><th>Severity</th><th>Code</th><th>Category</th>"
            "<th>Finding</th></tr>"
        )
        for finding in findings:
            add("<tr>")
            add(
                "<td><span class='sev sev-{0}'>{0}</span></td>".format(
                    esc(finding.severity)
                )
            )
            add("<td><code>{0}</code></td>".format(esc(finding.code)))
            add("<td>{0}</td>".format(esc(finding.category)))
            add("<td><strong>{0}</strong>".format(esc(finding.title)))
            if finding.detail:
                add("<div class='detail'>{0}</div>".format(esc(finding.detail)))
            if finding.refs:
                add(
                    "<div class='refs'>Refs: {0}</div>".format(
                        esc(", ".join(str(r) for r in finding.refs))
                    )
                )
            add("</td></tr>")
        add("</table></div>")

    # Sections
    for key in _ordered_section_keys(result.sections):
        payload = result.sections[key]
        rows = _as_rows(payload)
        if rows is None:
            text = _as_text(payload)
            if not text:
                continue
            add("<h2>{0}</h2>".format(esc(_section_title(key))))
            add("<div class='card'><p>{0}</p></div>".format(esc(text)))
            continue
        add("<h2>{0}</h2>".format(esc(_section_title(key))))
        add("<div class='card'><table>")
        headers = list(rows[0].keys())
        add(
            "<tr>{0}</tr>".format(
                "".join(
                    "<th>{0}</th>".format(esc(h.replace("_", " "))) for h in headers
                )
            )
        )
        for row in rows:
            add(
                "<tr>{0}</tr>".format(
                    "".join(
                        "<td>{0}</td>".format(esc(str(row.get(h, "")))) for h in headers
                    )
                )
            )
        add("</table></div>")

    if result.errors:
        add("<h2>Analyzer errors</h2><div class='card'><table>")
        for err in result.errors:
            add("<tr><td><code>{0}</code></td></tr>".format(esc(err)))
        add("</table></div>")

    add(
        "<footer>Generated by the KiCad Netlist Connectivity &amp; Topology "
        "Analyzer plugin.</footer>"
    )
    add("</div></body></html>")
    return "\n".join(out)


def render_json(result):
    # type: (AnalysisResult) -> str
    return result.to_json()
