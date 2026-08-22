"""
Analyzers: the rules that turn graph facts into engineering findings.

Architecture
------------
Each analyzer is a small, self-contained class implementing
:meth:`Analyzer.analyze`, registered into :data:`REGISTRY` by the
:func:`register` decorator. The engine simply walks the registry, so adding a
new rule means adding one class and nothing else - no engine, UI, or report code
changes. That is the extensibility requirement satisfied concretely rather than
just asserted: the report renders whatever findings and sections it is handed.

Analyzers are also *isolated*: the engine catches exceptions per analyzer, so a
bug or an unexpected board construct degrades one rule instead of killing the
whole run.

Each analyzer receives an :class:`AnalysisContext` giving it the board data, the
prepared graph views, and the configuration, and returns
:class:`~.model.Finding` objects. Analyzers may also write structured tables
into ``ctx.result.sections`` for the report to render.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import algorithms
from .config import AnalysisConfig
from .graph_model import NetlistGraph, component_node, node_label
from .model import (
    AnalysisResult,
    BoardData,
    Component,
    Finding,
    Net,
    NetRole,
    Severity,
)


# ---------------------------------------------------------------------------
# Context and base class
# ---------------------------------------------------------------------------


class AnalysisContext(object):
    """Everything an analyzer needs, assembled once by the engine."""

    def __init__(self, board, graph, config, result):
        # type: (BoardData, NetlistGraph, AnalysisConfig, AnalysisResult) -> None
        self.board = board
        self.graph = graph
        self.config = config
        self.result = result
        self._components_by_ref = None  # type: Optional[Dict[str, Component]]

    @property
    def components(self):
        # type: () -> List[Component]
        """Electrically relevant components (mechanical/DNP already removed)."""
        return self.graph.components

    def component(self, reference):
        # type: (str) -> Optional[Component]
        if self._components_by_ref is None:
            self._components_by_ref = dict((c.reference, c) for c in self.components)
        return self._components_by_ref.get(reference)

    def nets(self, *roles):
        # type: (*str) -> List[Net]
        if not roles:
            return list(self.board.nets)
        wanted = set(roles)
        return [n for n in self.board.nets if n.role in wanted]


class Analyzer(object):
    """Base class for all analysis rules."""

    #: Stable machine name, used to enable/disable the analyzer in config.
    name = ""
    #: Human-readable heading used in the report.
    title = ""
    #: Grouping label applied to this analyzer's findings.
    category = "General"

    def analyze(self, ctx):
        # type: (AnalysisContext) -> Iterable[Finding]
        raise NotImplementedError


#: All registered analyzers, in registration order (which is execution order).
REGISTRY = []  # type: List[type]


def register(cls):
    """Class decorator adding an analyzer to :data:`REGISTRY`."""
    REGISTRY.append(cls)
    return cls


def _truncate(items, limit=12):
    # type: (List[str], int) -> str
    """Render a reference list for a finding, capping runaway output."""
    if len(items) <= limit:
        return ", ".join(items)
    return "{0}, ... (+{1} more)".format(", ".join(items[:limit]), len(items) - limit)


def _distance_mm(a, b):
    # type: (Component, Component) -> float
    return math.hypot(a.x - b.x, a.y - b.y)


def _cap_findings(findings, cap, summary_code, summary_title, summary_detail, category):
    # type: (List[Finding], int, str, str, str, str) -> List[Finding]
    """Limit one rule's output, replacing the tail with a summary finding.

    Rules that iterate over nets can legitimately produce hundreds of nearly
    identical findings on a large or early-stage board. Emitting all of them
    makes the report unreadable and trains the user to ignore it, so past the
    cap we collapse the remainder into a single note that still names the
    affected items.
    """
    if cap <= 0 or len(findings) <= cap:
        return findings
    kept = findings[:cap]
    remaining = findings[cap:]
    names = []
    for finding in remaining:
        names.append(finding.refs[0] if finding.refs else finding.title)
    kept.append(
        Finding(
            code=summary_code,
            title=summary_title.format(count=len(remaining)),
            severity=Severity.INFO,
            category=category,
            detail=summary_detail.format(
                count=len(remaining), items=_truncate(names, 30)
            ),
            value=float(len(remaining)),
        )
    )
    return kept


# ---------------------------------------------------------------------------
# 1. Board statistics
# ---------------------------------------------------------------------------


@register
class BoardStatisticsAnalyzer(Analyzer):
    """Collects headline counts and the net-role histogram.

    Emits no findings; its job is to populate the report's summary metrics so a
    reviewer can sanity-check that extraction saw the board they expected.
    """

    name = "statistics"
    title = "Board statistics"
    category = "Statistics"

    def analyze(self, ctx):
        board = ctx.board
        role_counts = {}  # type: Dict[str, int]
        for net in board.nets:
            role_counts[net.role] = role_counts.get(net.role, 0) + 1

        unconnected_pads = sum(1 for p in board.all_pads() if not p.is_connected)
        routed = [n for n in board.nets if n.is_routed]
        total_len = sum(n.track_length_mm for n in board.nets)

        ctx.result.metrics.update(
            {
                "board_name": board.name,
                "components_total": len(board.components),
                "components_analyzed": len(ctx.components),
                "nets_total": len(board.nets),
                "pads_total": board.pad_count,
                "pads_unconnected": unconnected_pads,
                "power_nets": role_counts.get(NetRole.POWER, 0),
                "ground_nets": role_counts.get(NetRole.GROUND, 0),
                "signal_nets": role_counts.get(NetRole.SIGNAL, 0),
                "unconnected_nets": role_counts.get(NetRole.UNCONNECTED, 0),
                "routed_nets": len(routed),
                "total_track_length_mm": round(total_len, 2),
            }
        )
        ctx.result.metrics.update(ctx.graph.describe())

        # Rail inventory is genuinely useful at a glance when reviewing a board.
        rails = ctx.nets(NetRole.POWER, NetRole.GROUND)
        ctx.result.sections["rails"] = [
            {
                "net": n.name,
                "role": n.role,
                "pads": n.pad_count,
                "components": len(n.references()),
                "routed_mm": round(n.track_length_mm, 2),
            }
            for n in sorted(rails, key=lambda n: (-n.pad_count, n.name))
        ]
        return []


# ---------------------------------------------------------------------------
# 2. Connectivity islands
# ---------------------------------------------------------------------------


@register
class ConnectivityIslandAnalyzer(Analyzer):
    """Finds electrically isolated sub-circuits using connected components.

    Runs union-find over the *bipartite* incidence graph, which is the lossless
    view of the netlist hypergraph, so "connected" here means genuinely
    electrically reachable.

    More than one island is not automatically an error: isolated grounds,
    opto-isolated or transformer-isolated barriers, and separate mounting/antenna
    structures are all legitimate. But an *unintended* island is a classic
    design mistake (a forgotten net tie, a symbol left unwired), so it is worth
    surfacing prominently for a human to judge.
    """

    name = "islands"
    title = "Electrical islands"
    category = "Connectivity"

    def analyze(self, ctx):
        findings = []
        bipartite = ctx.graph.bipartite()
        if not bipartite:
            return findings

        groups = algorithms.connected_components(bipartite)

        # Components with no connected pad at all show up as singleton islands.
        orphans = sorted(
            node_label(g[0])
            for g in groups
            if len(g) == 1 and g[0].startswith("C:")
        )
        for ref in orphans:
            findings.append(
                Finding(
                    code="NTA-101",
                    title="Component has no connected pads",
                    severity=Severity.ERROR,
                    category=self.category,
                    detail=(
                        "{0} is placed on the board but none of its pads belong "
                        "to a net, so it is electrically floating.".format(ref)
                    ),
                    refs=[ref],
                )
            )

        real_islands = [g for g in groups if not (len(g) == 1 and g[0].startswith("C:"))]
        island_rows = []
        for index, group in enumerate(real_islands):
            comps = sorted(node_label(n) for n in group if n.startswith("C:"))
            nets = sorted(node_label(n) for n in group if n.startswith("N:"))
            island_rows.append(
                {
                    "island": index + 1,
                    "components": len(comps),
                    "nets": len(nets),
                    "component_list": _truncate(comps, 20),
                    "net_list": _truncate(nets, 20),
                }
            )
        ctx.result.sections["islands"] = island_rows
        ctx.result.metrics["electrical_islands"] = len(real_islands)

        if len(real_islands) > 1:
            # Islands are sorted largest-first by connected_components().
            secondary = real_islands[1:]
            small = []
            for group in secondary:
                comps = sorted(node_label(n) for n in group if n.startswith("C:"))
                small.append(_truncate(comps, 8) or "(nets only)")
            findings.append(
                Finding(
                    code="NTA-100",
                    title="Board splits into {0} electrical islands".format(
                        len(real_islands)
                    ),
                    severity=Severity.WARNING,
                    category=self.category,
                    detail=(
                        "The netlist is not fully connected. This is expected for "
                        "galvanically isolated designs (opto-isolators, isolated "
                        "grounds, transformers) but otherwise usually indicates a "
                        "missing connection or an unwired symbol. Smaller islands "
                        "contain: {0}".format("; ".join(small))
                    ),
                    refs=[],
                    value=float(len(real_islands)),
                )
            )
        return findings


# ---------------------------------------------------------------------------
# 3. Floating / single-pad nets
# ---------------------------------------------------------------------------


@register
class FloatingNetAnalyzer(Analyzer):
    """Flags nets that reach only one pad.

    A net with a single pad carries no current anywhere: it is either an
    unfinished connection or a leftover label. This is one of the cheapest and
    highest-value checks available on a netlist.
    """

    name = "floating_nets"
    title = "Floating and single-pad nets"
    category = "Connectivity"

    def analyze(self, ctx):
        findings = []
        singles = [
            n
            for n in ctx.board.nets
            if n.role != NetRole.UNCONNECTED and n.pad_count == 1
        ]
        for net in sorted(singles, key=lambda n: n.name):
            pad = net.pads[0]
            findings.append(
                Finding(
                    code="NTA-110",
                    title="Net '{0}' reaches only one pad".format(net.name),
                    severity=Severity.WARNING,
                    category=self.category,
                    detail=(
                        "Only {0} is attached to this net. A single-pad net cannot "
                        "conduct anywhere - it is normally an incomplete "
                        "connection or an unused label.".format(pad.uid)
                    ),
                    refs=[net.name, pad.reference],
                    value=1.0,
                )
            )

        # Nets declared with no pad members at all. Deliberately *not* filtered
        # by role: the classifier assigns UNCONNECTED to every pad-less net, so
        # gating on role here would make this rule unreachable. The extractor
        # adds these nets on purpose (see board_extractor.extract_board), since
        # a named net with no members is usually a leftover from an edit.
        empties = [n for n in ctx.board.nets if n.pad_count == 0]
        findings = _cap_findings(
            findings,
            ctx.config.max_findings_per_rule,
            "NTA-112",
            "{count} further single-pad net(s) not listed individually",
            "Additional nets reaching only one pad: {items}",
            self.category,
        )
        if empties:
            findings.append(
                Finding(
                    code="NTA-111",
                    title="{0} net(s) defined with no pads".format(len(empties)),
                    severity=Severity.INFO,
                    category=self.category,
                    detail=(
                        "These nets exist in the netlist but have no pad members: "
                        "{0}".format(_truncate(sorted(n.name for n in empties)))
                    ),
                    refs=[n.name for n in empties[:20]],
                    value=float(len(empties)),
                )
            )

        ctx.result.metrics["single_pad_nets"] = len(singles)
        return findings


# ---------------------------------------------------------------------------
# 4. Routing completeness
# ---------------------------------------------------------------------------


@register
class UnroutedNetAnalyzer(Analyzer):
    """Reports nets with two or more pads but no copper.

    Deliberately degrades gracefully: on a board that has not been routed at all
    (or when routing data is unavailable, e.g. a netlist-only fixture) it emits
    one summary note instead of hundreds of near-identical warnings. Reports that
    drown the reader get ignored, which defeats the purpose of the check.
    """

    name = "unrouted"
    title = "Routing completeness"
    category = "Routing"

    def analyze(self, ctx):
        if not ctx.config.check_unrouted:
            return []

        candidates = [
            n
            for n in ctx.board.nets
            if n.role != NetRole.UNCONNECTED and n.pad_count >= 2
        ]
        if not candidates:
            return []

        unrouted = [n for n in candidates if not n.is_routed]
        ctx.result.metrics["unrouted_nets"] = len(unrouted)

        # Nothing routed anywhere -> almost certainly not a routing problem.
        if len(unrouted) == len(candidates):
            return [
                Finding(
                    code="NTA-121",
                    title="No routed copper found on any net",
                    severity=Severity.INFO,
                    category=self.category,
                    detail=(
                        "All {0} multi-pad nets are without copper. The board is "
                        "probably not routed yet, or connectivity was imported "
                        "from a netlist without routing data, so per-net routing "
                        "warnings are suppressed.".format(len(candidates))
                    ),
                    value=float(len(unrouted)),
                )
            ]

        findings = []
        for net in sorted(unrouted, key=lambda n: (-n.pad_count, n.name)):
            findings.append(
                Finding(
                    code="NTA-120",
                    title="Net '{0}' is unrouted".format(net.name),
                    severity=Severity.WARNING,
                    category=self.category,
                    detail=(
                        "The net joins {0} pads ({1}) but carries no copper "
                        "track.".format(
                            net.pad_count,
                            _truncate(sorted(p.uid for p in net.pads), 8),
                        )
                    ),
                    refs=[net.name],
                    value=float(net.pad_count),
                )
            )
        return _cap_findings(
            findings,
            ctx.config.max_findings_per_rule,
            "NTA-122",
            "{count} further unrouted net(s) not listed individually",
            "Additional nets without copper: {items}",
            self.category,
        )


# ---------------------------------------------------------------------------
# 5. Fanout / loading
# ---------------------------------------------------------------------------


@register
class FanoutAnalyzer(Analyzer):
    """Ranks signal nets by fanout and flags heavily loaded ones.

    Electronics rationale: every additional pad on a signal net adds input
    capacitance and, physically, another stub off the main trace. High fanout
    therefore slows edges, and the unterminated stubs cause reflections that show
    up as ringing and timing problems. A net feeding many loads usually wants a
    buffer, a proper daisy-chain with end termination, or a redriver.

    Rails are excluded: they are high-fanout by design, so flagging them would be
    pure noise.
    """

    name = "fanout"
    title = "Signal fanout"
    category = "Signal integrity"

    def analyze(self, ctx):
        signals = ctx.nets(NetRole.SIGNAL)
        if not signals:
            return []

        ranked = sorted(signals, key=lambda n: (-n.fanout, n.name))
        ctx.result.sections["fanout"] = [
            {
                "net": n.name,
                "pads": n.pad_count,
                "components": len(n.references()),
                "members": _truncate(sorted(p.uid for p in n.pads), 10),
            }
            for n in ranked[: ctx.config.top_n]
        ]
        ctx.result.metrics["max_signal_fanout"] = ranked[0].fanout if ranked else 0

        findings = []
        for net in ranked:
            if net.fanout <= ctx.config.high_fanout_threshold:
                break  # ranked descending: nothing further can exceed it
            critical = net.fanout >= ctx.config.critical_fanout_threshold
            findings.append(
                Finding(
                    code="NTA-130",
                    title="High-fanout signal net '{0}' ({1} pads)".format(
                        net.name, net.fanout
                    ),
                    severity=Severity.ERROR if critical else Severity.WARNING,
                    category=self.category,
                    detail=(
                        "This signal drives {0} pads ({1}). Each load adds input "
                        "capacitance and a trace stub, degrading edge rates and "
                        "causing reflections. Consider buffering, a daisy-chain "
                        "with end termination, or splitting the net.".format(
                            net.fanout, _truncate(sorted(p.uid for p in net.pads), 10)
                        )
                    ),
                    refs=[net.name],
                    value=float(net.fanout),
                )
            )
        return findings


# ---------------------------------------------------------------------------
# 6. Power distribution and decoupling
# ---------------------------------------------------------------------------


@register
class PowerIntegrityAnalyzer(Analyzer):
    """Checks power/ground reach and local decoupling for active parts.

    Three checks, in increasing subtlety:

    1. Does the board have a ground net at all?
    2. Does each IC actually connect to both a power rail and a ground?
    3. Does each IC have a capacitor sharing its power *and* ground nets, close
       enough to count as local decoupling?

    Check 3 is the interesting one. A decoupling capacitor supplies the transient
    current an IC demands when its outputs switch, and it only works if the loop
    it forms with the IC is physically small - loop inductance, not capacitance,
    is what limits high-frequency performance. So sharing the right net pair is
    necessary but not sufficient; proximity matters, and the distance threshold
    is configurable.
    """

    name = "power"
    title = "Power distribution and decoupling"
    category = "Power integrity"

    def analyze(self, ctx):
        findings = []
        cfg = ctx.config

        power_nets = set(n.name for n in ctx.nets(NetRole.POWER))
        ground_nets = set(n.name for n in ctx.nets(NetRole.GROUND))

        if not ground_nets:
            findings.append(
                Finding(
                    code="NTA-140",
                    title="No ground net identified",
                    severity=Severity.WARNING,
                    category=self.category,
                    detail=(
                        "No net matched the ground naming patterns. Every powered "
                        "circuit needs a return path, so this usually means the "
                        "board uses an unusual naming convention - extend "
                        "'ground_patterns' in the configuration if so."
                    ),
                )
            )
        if not power_nets:
            findings.append(
                Finding(
                    code="NTA-141",
                    title="No power net identified",
                    severity=Severity.INFO,
                    category=self.category,
                    detail=(
                        "No net matched the power naming patterns. This is normal "
                        "for a purely passive board; otherwise extend "
                        "'power_patterns' in the configuration."
                    ),
                )
            )

        capacitors = [c for c in ctx.components if cfg.is_capacitor(c.prefix)]
        ics = [
            c
            for c in ctx.components
            if cfg.is_ic(c.prefix) and c.pad_count >= cfg.decoupling_min_ic_pads
        ]

        rows = []
        for ic in sorted(ics, key=lambda c: c.reference):
            nets = ic.net_names()
            ic_power = sorted(nets & power_nets)
            ic_ground = sorted(nets & ground_nets)

            if not ic_power:
                findings.append(
                    Finding(
                        code="NTA-142",
                        title="{0} has no power-rail connection".format(ic.reference),
                        severity=Severity.WARNING,
                        category=self.category,
                        detail=(
                            "{0} ({1}, {2} pads) does not connect to any recognised "
                            "power rail. Either it is fed from a net the classifier "
                            "did not recognise as a rail, or its supply pin is "
                            "genuinely unconnected.".format(
                                ic.reference, ic.value or "?", ic.pad_count
                            )
                        ),
                        refs=[ic.reference],
                    )
                )
            if not ic_ground:
                findings.append(
                    Finding(
                        code="NTA-143",
                        title="{0} has no ground connection".format(ic.reference),
                        severity=Severity.WARNING,
                        category=self.category,
                        detail=(
                            "{0} ({1}) does not connect to any recognised ground "
                            "net, so it has no return path for its supply "
                            "current.".format(ic.reference, ic.value or "?")
                        ),
                        refs=[ic.reference],
                    )
                )

            # --- decoupling search ---
            nearest = None  # type: Optional[Tuple[float, str, str, str]]
            matches = []
            for cap in capacitors:
                cap_nets = cap.net_names()
                shared_p = cap_nets & set(ic_power)
                shared_g = cap_nets & set(ic_ground)
                if not shared_p or not shared_g:
                    continue
                dist = _distance_mm(ic, cap)
                entry = (dist, cap.reference, sorted(shared_p)[0], sorted(shared_g)[0])
                matches.append(entry)
                if nearest is None or dist < nearest[0]:
                    nearest = entry

            if ic_power and ic_ground:
                if not matches:
                    findings.append(
                        Finding(
                            code="NTA-144",
                            title="{0} has no decoupling capacitor".format(ic.reference),
                            severity=Severity.WARNING,
                            category=self.category,
                            detail=(
                                "No capacitor connects both a power rail ({0}) and a "
                                "ground ({1}) of {2}. Without local decoupling the "
                                "IC must draw switching transients through the whole "
                                "supply inductance, causing rail droop and "
                                "radiated noise.".format(
                                    ", ".join(ic_power),
                                    ", ".join(ic_ground),
                                    ic.reference,
                                )
                            ),
                            refs=[ic.reference],
                        )
                    )
                elif (
                    not cfg.decoupling_ignore_distance
                    and nearest is not None
                    and nearest[0] > cfg.decoupling_max_distance_mm
                ):
                    findings.append(
                        Finding(
                            code="NTA-145",
                            title="{0}: nearest decoupling capacitor is {1:.1f} mm away".format(
                                ic.reference, nearest[0]
                            ),
                            severity=Severity.INFO,
                            category=self.category,
                            detail=(
                                "The closest qualifying capacitor is {0} at "
                                "{1:.1f} mm, beyond the {2:.1f} mm guideline. "
                                "Decoupling effectiveness is set by the loop "
                                "inductance between capacitor and IC, so moving it "
                                "closer matters more than increasing its "
                                "value.".format(
                                    nearest[1],
                                    nearest[0],
                                    cfg.decoupling_max_distance_mm,
                                )
                            ),
                            refs=[ic.reference, nearest[1]],
                            value=round(nearest[0], 2),
                        )
                    )

            rows.append(
                {
                    "component": ic.reference,
                    "value": ic.value,
                    "pads": ic.pad_count,
                    "power": ", ".join(ic_power) or "-",
                    "ground": ", ".join(ic_ground) or "-",
                    "decoupling_caps": len(matches),
                    "nearest_cap": nearest[1] if nearest else "-",
                    "nearest_mm": round(nearest[0], 2) if nearest else "-",
                }
            )

        ctx.result.sections["power"] = rows
        ctx.result.metrics["ics_checked"] = len(ics)
        ctx.result.metrics["capacitors_found"] = len(capacitors)
        return findings


# ---------------------------------------------------------------------------
# 7. Single points of failure
# ---------------------------------------------------------------------------


@register
class SinglePointOfFailureAnalyzer(Analyzer):
    """Finds articulation points and bridges in the signal-topology graph.

    An articulation point is a component whose removal disconnects the circuit -
    every signal path between two parts of the design funnels through it. A
    bridge is the same idea for a connection: a link with no redundant path
    around it.

    Both are computed with Tarjan's lowlink algorithm in ``O(V + E)`` on the
    rail-free component projection. Excluding rails is essential: with GND in the
    graph almost nothing is an articulation point, because every part reaches
    every other through ground.

    Severity is deliberately graded. A connector or test point being an
    articulation point is structurally expected - that *is* its job as the
    interface to the outside world - so those are reported as INFO, while an
    unexpected in-circuit part is a WARNING worth a second look.
    """

    name = "spof"
    title = "Single points of failure"
    category = "Topology"

    def analyze(self, ctx):
        findings = []
        graph = ctx.graph.component_graph()
        if len(graph) < 3:
            return findings

        cuts = algorithms.articulation_points(graph)
        cut_refs = sorted(node_label(n) for n in cuts)

        rows = []
        for ref in cut_refs:
            comp = ctx.component(ref)
            prefix = comp.prefix if comp else ""
            is_interface = ctx.config.is_connector(prefix)
            neighbours = sorted(
                node_label(n) for n in graph.get(component_node(ref), ())
            )
            rows.append(
                {
                    "component": ref,
                    "value": comp.value if comp else "",
                    "neighbours": len(neighbours),
                    "neighbour_list": _truncate(neighbours, 10),
                    "kind": "interface" if is_interface else "in-circuit",
                }
            )
            findings.append(
                Finding(
                    code="NTA-151" if is_interface else "NTA-150",
                    title="{0} is a single point of failure".format(ref),
                    severity=Severity.INFO if is_interface else Severity.WARNING,
                    category=self.category,
                    detail=(
                        "Removing {0} would split the signal-topology graph into "
                        "disconnected parts: every signal path between the groups "
                        "it joins passes through it. {1} It connects to {2}.".format(
                            ref,
                            (
                                "For a connector or test point this is expected, "
                                "since it is the interface to the outside world."
                                if is_interface
                                else "Consider whether this concentration of "
                                "connectivity is intended."
                            ),
                            _truncate(neighbours, 10),
                        )
                    ),
                    refs=[ref],
                    value=float(len(neighbours)),
                )
            )

        cut_edges = algorithms.bridges(graph)
        bridge_rows = []
        for a, b in cut_edges:
            ref_a, ref_b = node_label(a), node_label(b)
            via = ctx.graph.shared_nets(ref_a, ref_b)
            bridge_rows.append(
                {
                    "from": ref_a,
                    "to": ref_b,
                    "via_nets": ", ".join(sorted(set(via))) or "-",
                }
            )

        ctx.result.sections["spof"] = rows
        ctx.result.sections["bridges"] = bridge_rows[: max(ctx.config.top_n, 20)]
        ctx.result.metrics["articulation_points"] = len(cut_refs)
        ctx.result.metrics["bridge_connections"] = len(cut_edges)

        if cut_edges:
            findings.append(
                Finding(
                    code="NTA-152",
                    title="{0} connection(s) have no redundant path".format(
                        len(cut_edges)
                    ),
                    severity=Severity.INFO,
                    category=self.category,
                    detail=(
                        "These component-to-component links are bridges: removing "
                        "one disconnects the graph. Example(s): {0}".format(
                            _truncate(
                                [
                                    "{0}-{1}".format(r["from"], r["to"])
                                    for r in bridge_rows
                                ],
                                10,
                            )
                        )
                    ),
                    value=float(len(cut_edges)),
                )
            )
        return findings


# ---------------------------------------------------------------------------
# 8. Topology metrics / centrality
# ---------------------------------------------------------------------------


@register
class TopologyMetricsAnalyzer(Analyzer):
    """Ranks components by betweenness centrality and measures graph shape.

    Betweenness counts the fraction of shortest signal paths passing through
    each component, so it identifies the structural hubs of a design - typically
    the MCU, an FPGA, or a bus buffer - without any simulation or knowledge of
    what the parts do.

    The diameter (longest shortest-path) indicates how "deep" the signal chain
    is: a long chain means many stages between input and output, which tends to
    accumulate delay and noise.

    Both measures are gated on graph size, since Brandes' algorithm is ``O(V*E)``
    and would make the GUI feel frozen on a very large board.
    """

    name = "topology_metrics"
    title = "Topology metrics"
    category = "Topology"

    def analyze(self, ctx):
        findings = []
        graph = ctx.graph.component_graph()
        if len(graph) < 3:
            return findings

        degree = algorithms.degrees(graph)
        hubs = algorithms.top_n(
            dict((node_label(k), float(v)) for k, v in degree.items()),
            ctx.config.top_n,
        )
        ctx.result.sections["degree"] = [
            {"component": ref, "connections": int(score)} for ref, score in hubs
        ]

        if len(graph) > ctx.config.centrality_max_components:
            ctx.result.metrics["centrality_skipped"] = True
            findings.append(
                Finding(
                    code="NTA-161",
                    title="Centrality analysis skipped (board too large)",
                    severity=Severity.INFO,
                    category=self.category,
                    detail=(
                        "The projection has {0} components, above the configured "
                        "limit of {1}. Betweenness centrality is O(V*E), so it is "
                        "skipped to keep the interface responsive. Raise "
                        "'centrality_max_components' to force it.".format(
                            len(graph), ctx.config.centrality_max_components
                        )
                    ),
                )
            )
            return findings

        centrality = algorithms.betweenness_centrality(graph, normalized=True)
        ranked = algorithms.top_n(
            dict((node_label(k), v) for k, v in centrality.items()),
            ctx.config.top_n,
            minimum=0.0,
        )
        ctx.result.sections["centrality"] = [
            {"component": ref, "betweenness": round(score, 4)} for ref, score in ranked
        ]

        # Diameter is measured on the largest component only; it is undefined
        # across disconnected parts.
        groups = algorithms.connected_components(graph)
        if groups:
            largest = groups[0]
            _, diameter = algorithms.eccentricity_and_diameter(graph, largest)
            ctx.result.metrics["signal_graph_diameter"] = diameter
            ctx.result.metrics["largest_signal_cluster"] = len(largest)

        if ranked:
            top_ref, top_score = ranked[0]
            comp = ctx.component(top_ref)
            findings.append(
                Finding(
                    code="NTA-160",
                    title="{0} is the most central component".format(top_ref),
                    severity=Severity.INFO,
                    category=self.category,
                    detail=(
                        "{0}{1} has the highest betweenness centrality "
                        "({2:.4f}), meaning the largest share of shortest signal "
                        "paths runs through it. It is the structural hub of the "
                        "design and the natural focus for signal-integrity and "
                        "reliability review.".format(
                            top_ref,
                            " ({0})".format(comp.value) if comp and comp.value else "",
                            top_score,
                        )
                    ),
                    refs=[top_ref],
                    value=round(top_score, 4),
                )
            )
        return findings
